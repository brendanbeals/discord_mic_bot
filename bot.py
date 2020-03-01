import copy
import json
import configparser
from collections import deque
import aiohttp
import websockets
import asyncio
import urllib.parse
from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume
import time

class DiscordClient:
    def __init__(self, BOT_TOKEN, USER_ID, BOT_ID, _BASE_API_URL='https://discordapp.com/api/', loop: asyncio.AbstractEventLoop = None):
        self.TARGET_USER_ID = USER_ID
        self.BOT_USER_ID = BOT_ID
        self._voice_state_update_success = False
        self._channel_id = None
        self._voice_session_id = None
        if loop is None:
            self.loop = asyncio.get_event_loop()
        else:
            self.loop = loop
        self._headers = {'User-Agent': 'DiscordBot (NA, 0.1)'}
        self._voice_state_update_template = {
            "op": 4,
            "d": {
                'guild_id': None,
                'channel_id': None,
                'self_mute': False,
                'self_deaf': False
            }
        }
        self._identify_payload = {
            'op': 2,
            'd': {
                'token': BOT_TOKEN,
                'properties': {
                    '$os': 'linux',
                    '$browser': 'discord_mic_bot',
                    '$device': 'discord_mic_bot'
                },
                'compress': False,
                'large_threshold': 250
            }
        }
        self._voice_identify_payload_template = {
            "op": 0,
            "d": {
                "server_id": None,
                "user_id": None,
                "session_id": None,
                "token": None
            }
        }
        self._main_heartbeat_task = None
        self._voice_heartbeat_task = None
        self._speaker_monitor_loop_task = None
        self._voice_ws_loop_task = None
        self._volume_change_task = None
        self._volume_change_loop_task = None
        self._speakers = set()
        self._speaking_states = deque((False, False), 2)
        self.http = None
        self.main_ws = None
        self.voice_ws = None
        self._ws_choices = {'MAIN', 'VOICE'}
        self.BOT_TOKEN = BOT_TOKEN
        self._BASE_API_URL = _BASE_API_URL
        self._GET_GATEWAY_URL = urllib.parse.urljoin(self._BASE_API_URL, 'gateway')
        self._LAST_SEQUENCE_NUMBER_LOCK = asyncio.Lock()
        self._MAIN_WS_HEARTBEAT_INTERVAL_LOCK = asyncio.Lock()
        self._VOICE_WS_HEARTBEAT_INTERVAL_LOCK = asyncio.Lock()
        self._SPEAKERS_LOCK = asyncio.Lock()
        self._SPEAKING_STATES_LOCK = asyncio.Lock()
        self._MAIN_HEARTBEAT_INTERVAL = 0
        self._VOICE_HEARTBEAT_INTERVAL = 0
        self._LAST_SEQUENCE_NUMBER = 0

    async def volume_change(self, target_volume, over_seconds=None, step_time=0.1):
        print(target_volume)
        sessions = AudioUtilities.GetAllSessions()
        for session in sessions:
            volume = session._ctl.QueryInterface(ISimpleAudioVolume)
            current_volume_level = volume.GetMasterVolume()
            if session.Process and 'discord' not in session.Process.name().lower():
                if over_seconds is not None:
                    step_count = over_seconds / step_time
                    if target_volume < current_volume_level:
                        volume_step_size = (current_volume_level - target_volume) / step_count
                    else:
                        volume_step_size = (target_volume - current_volume_level) / step_count
                    for _ in range(int(step_count)):
                        if target_volume > current_volume_level:
                            current_volume_level = current_volume_level + volume_step_size
                        else:
                            current_volume_level = current_volume_level - volume_step_size
                        await asyncio.sleep(step_time)
                        print(f'{current_volume_level}')
                        volume.SetMasterVolume(current_volume_level, None)
                else:
                    volume.SetMasterVolume(target_volume, None)

    async def speaker_monitor_loop(self):
        while True:
            async with self._SPEAKERS_LOCK:
                any_speakers = len(self._speakers) > 0
                async with self._SPEAKING_STATES_LOCK:
                    self._speaking_states.append(any_speakers)
                    await asyncio.sleep(0.5)

    async def volume_change_loop(self):
        while True:
            async with self._SPEAKING_STATES_LOCK:
                if self._speaking_states == deque((False, False)):
                    pass
                elif self._speaking_states == deque((False, True)):
                    await self.volume_change(0.5)
                elif self._speaking_states == deque((True, False)):
                    await self.volume_change(1.0)
                await asyncio.sleep(0.1)

    async def get_gateway(self):
        self.http = aiohttp.ClientSession(headers=self._headers)
        async with self.http.get(self._GET_GATEWAY_URL) as gateway_resp:
            gateway_resp_json = await gateway_resp.json()
            return gateway_resp_json.get('url')

    async def send_heartbeat(self, ws_choice):
        while True:
            if ws_choice == 'MAIN':
                async with self._MAIN_WS_HEARTBEAT_INTERVAL_LOCK:
                    await asyncio.sleep(self._MAIN_HEARTBEAT_INTERVAL / 1000)
                async with self._LAST_SEQUENCE_NUMBER_LOCK:
                    await self.main_ws.send(json.dumps({
                        "op": 1,
                        "d": self._LAST_SEQUENCE_NUMBER}))
            elif ws_choice == 'VOICE':
                async with self._VOICE_WS_HEARTBEAT_INTERVAL_LOCK:
                    await asyncio.sleep(self._VOICE_HEARTBEAT_INTERVAL / 1000)
                await self.voice_ws.send(json.dumps({
                    "op": 3,
                    "d": int(time.time())}))

    async def send_voice_state_update(self, guild_id, voice_channel_id):
        voice_state_update_request = copy.deepcopy(self._voice_state_update_template)
        voice_state_update_request['d']['guild_id'] = guild_id
        voice_state_update_request['d']['channel_id'] = voice_channel_id
        await self.main_ws.send(json.dumps(voice_state_update_request))

    async def send_voice_identify_payload(self, voice_identify_token, voice_session_id, guild_id):
        voice_identify_payload = copy.deepcopy(self._voice_identify_payload_template)
        voice_identify_payload['d']['server_id'] = guild_id
        voice_identify_payload['d']['user_id'] = self._bot_id
        voice_identify_payload['d']['session_id'] = voice_session_id
        voice_identify_payload['d']['token'] = voice_identify_token
        await self.voice_ws.send(json.dumps(voice_identify_payload))

    async def setup_heartbeat_task(self, heartbeat_interval, which_ws):
        if which_ws not in self._ws_choices:
            raise AssertionError(f'Websocket choice not in {self._ws_choices}!')
        if which_ws == 'MAIN':
            async with self._MAIN_WS_HEARTBEAT_INTERVAL_LOCK:
                self._MAIN_HEARTBEAT_INTERVAL = heartbeat_interval
            if self._main_heartbeat_task is not None:
                self._main_heartbeat_task.cancel()
            self._main_heartbeat_task = asyncio.create_task(self.send_heartbeat(which_ws))
        elif which_ws == 'VOICE':
            async with self._VOICE_WS_HEARTBEAT_INTERVAL_LOCK:
                self._VOICE_HEARTBEAT_INTERVAL = heartbeat_interval
            if self._voice_heartbeat_task is not None:
                self._voice_heartbeat_task.cancel()
            self._voice_heartbeat_task = asyncio.create_task(self.send_heartbeat(which_ws))

    async def login(self, hello_msg):
        await self.setup_heartbeat_task(hello_msg['d']['heartbeat_interval'], 'MAIN')
        await self.main_ws.send(json.dumps(self._identify_payload))

    async def voice_ws_loop(self, voice_gateway, voice_identify_token, voice_session_id, guild_id):
        voice_uri = f'wss://{voice_gateway.split(":")[0]}/?v=4'
        self.voice_ws = await websockets.connect(voice_uri)
        await self.send_voice_identify_payload(voice_identify_token, voice_session_id, guild_id)
        self._speaker_monitor_loop_task = asyncio.Task(self.speaker_monitor_loop())
        self._volume_change_loop_task = asyncio.Task(self.volume_change_loop())
        async for msg_raw in self.voice_ws:
            msg = json.loads(msg_raw)
            if msg['op'] == 8:
                await self.setup_heartbeat_task(msg['d']['heartbeat_interval'], 'VOICE')
            elif msg['op'] == 5:
                if msg['d']['speaking'] == 1:
                    self._speakers.add(msg['d']['user_id'])
                if msg['d']['speaking'] == 0:
                    try:
                        self._speakers.remove(msg['d']['user_id'])
                    except KeyError:
                        pass
            else:
                print(f'voice_ws_loop: {msg}')

    async def main_ws_loop(self):
        gateway_uri = await self.get_gateway()
        self.main_ws = await websockets.connect(gateway_uri)
        async for msg_raw in self.main_ws:
            msg = json.loads(msg_raw)
            async with self._LAST_SEQUENCE_NUMBER_LOCK:
                self._LAST_SEQUENCE_NUMBER = msg['s']
            if msg['op'] == 10:
                await self.login(msg)
            elif msg['t'] == 'GUILD_CREATE':
                pass
            elif msg['t'] == 'READY':
                self._bot_id = msg['d']['user']['id']
                print(f"{msg['d']['user']['username']} successfully logged in!")
            elif msg['t'] == 'VOICE_STATE_UPDATE':
                if msg['d']['user_id'] == self.TARGET_USER_ID and msg['d'][
                    'channel_id'] is not None and not self._channel_id:
                    self._channel_id = msg['d']['channel_id']
                    await self.send_voice_state_update(msg['d']['guild_id'], msg['d']['channel_id'])
                elif msg['d']['user_id'] == self.BOT_USER_ID and msg['d']['channel_id'] == self._channel_id:
                    self._voice_session_id = msg['d']['session_id']
                    self._voice_state_update_success = True
            elif msg['t'] == 'VOICE_SERVER_UPDATE' and self._voice_state_update_success:
                self._voice_ws_loop_task = asyncio.Task(
                    self.voice_ws_loop(msg['d']['endpoint'], msg['d']['token'], self._voice_session_id,
                                       msg['d']['guild_id']))
            elif msg['op'] == 11:
                pass
            else:
                print(f'main_ws_loop: {msg}')

    async def logout(self):
        if self.http is not None:
            await self.http.close()
        if self._main_heartbeat_task is not None:
            self._main_heartbeat_task.cancel()
        if self._voice_heartbeat_task is not None:
            self._voice_heartbeat_task.cancel()
        if self.main_ws is not None and self.main_ws.open:
            await self.main_ws.close()
        if self.voice_ws is not None and self.voice_ws.open:
            await self.voice_ws.close()
        if self._voice_ws_loop_task is not None:
            self._voice_ws_loop_task.cancel()
        if self._speaker_monitor_loop_task is not None:
            self._speaker_monitor_loop_task.cancel()
        if self._volume_change_task is not None:
            self._volume_change_task.cancel()
        if self._volume_change_loop_task is not None:
            self._volume_change_loop_task.cancel()

if __name__ == '__main__':
    config = configparser.ConfigParser()
    with open('bot_conf.ini', 'r') as f:
        config.read_file(f)
    dc = DiscordClient(config['DEFAULT']['BOT_TOKEN'], config['DEFAULT']['USER_ID'], config['DEFAULT']['BOT_ID'])
    try:
        dc.loop.run_until_complete(dc.main_ws_loop())
    except KeyboardInterrupt:
        dc.loop.run_until_complete(dc.logout())
    finally:
        dc.loop.close()
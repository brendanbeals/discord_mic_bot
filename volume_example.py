from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume
import time

# while True:
#     sessions = AudioUtilities.GetAllSessions()
#
#     for session in sessions:
#         volume = session._ctl.QueryInterface(ISimpleAudioVolume)
#         if session.Process and 'discord' not in session.Process.name().lower():
#             print("volume.GetMasterVolume(): %s" % volume.GetMasterVolume())
#             volume.SetMasterVolume(1.0, None)
#     time.sleep(0.25)

target_volume = 1
over_seconds = 2
step_time = 0.1

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
                time.sleep(step_time)
                print(f'{session.Process.name()}: {volume_step_size}: {time.time()}: current_volume_level: {current_volume_level}')
                volume.SetMasterVolume(current_volume_level, None)
        else:
            volume.SetMasterVolume(target_volume, None)

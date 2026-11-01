import ctypes
from pathlib import Path
import time

import psutil

stop_file = Path('D:/AI/deployment/allow-sleep')
try:
    while not stop_file.exists():
        battery = psutil.sensors_battery()
        flags = 0x80000001 if battery is None or battery.power_plugged else 0x80000000
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
        time.sleep(30)
finally:
    ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)

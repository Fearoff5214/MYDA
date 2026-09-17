"""Machine state: lock and sleep."""
from __future__ import annotations

import ctypes
import platform
import subprocess

from .guard import Refused

IS_WINDOWS = platform.system() == "Windows"


def lock(allow: dict, args: dict) -> dict:
    if IS_WINDOWS:
        if not ctypes.windll.user32.LockWorkStation():
            raise Refused("Windows wouldn't lock the screen.")
    else:
        subprocess.run(["loginctl", "lock-session"], check=False, timeout=10)
    return {"speech": "Locked."}


def sleep_machine(allow: dict, args: dict) -> dict:
    """Suspend. Note this drops the node's connection until the machine wakes,
    which is expected -- the hub will report it offline."""
    if IS_WINDOWS:
        # SetSuspendState(hibernate=False, force=False, wakeupEventsDisabled=False)
        ctypes.windll.powrprof.SetSuspendState(0, 0, 0)
    else:
        subprocess.run(["systemctl", "suspend"], check=False, timeout=10)
    return {"speech": "Going to sleep."}

"""
Volume and media transport.

Uses the OS virtual key codes rather than a library, so there is no extra
dependency and it works with whatever is currently playing -- Spotify,
a browser tab, VLC. Windows exposes volume in 2% steps per keypress, so
absolute levels are approximated by counting steps; pycaw would give exact
control but is not worth a dependency for a voice interface.
"""
from __future__ import annotations

import ctypes
import platform
import time

from .guard import Refused

IS_WINDOWS = platform.system() == "Windows"

VK = {
    "volume_up": 0xAF,
    "volume_down": 0xAE,
    "mute": 0xAD,
    "play_pause": 0xB3,
    "next": 0xB0,
    "previous": 0xB1,
    "stop": 0xB2,
}

KEYEVENTF_KEYUP = 0x0002
STEP_PERCENT = 2  # Windows moves the master volume 2% per keypress


def _tap(vk: int, times: int = 1) -> None:
    if not IS_WINDOWS:
        raise Refused("Media keys are only wired up on Windows so far.")
    user32 = ctypes.windll.user32
    for _ in range(times):
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.005)


def volume(allow: dict, args: dict) -> dict:
    """direction: up | down | mute, or level: 0-100."""
    level = args.get("level")
    direction = (args.get("direction") or "").lower().strip()

    if level is not None:
        try:
            level = max(0, min(100, int(level)))
        except (TypeError, ValueError) as exc:
            raise Refused("I didn't understand that volume level.") from exc
        # Drop to zero, then step up. Crude but dependency-free and accurate
        # enough that nobody can tell by ear.
        _tap(VK["volume_down"], times=50)
        _tap(VK["volume_up"], times=level // STEP_PERCENT)
        return {"level": level, "speech": f"Volume set to {level} percent."}

    if direction in ("up", "louder", "raise", "increase"):
        amount = int(args.get("amount", 10))
        _tap(VK["volume_up"], times=max(1, amount // STEP_PERCENT))
        return {"speech": "Turned it up."}

    if direction in ("down", "quieter", "lower", "decrease"):
        amount = int(args.get("amount", 10))
        _tap(VK["volume_down"], times=max(1, amount // STEP_PERCENT))
        return {"speech": "Turned it down."}

    if direction in ("mute", "unmute", "toggle"):
        _tap(VK["mute"])
        return {"speech": "Muted." if direction == "mute" else "Toggled the mute."}

    raise Refused("Tell me up, down, mute, or a level from zero to a hundred.")


def media_key(allow: dict, args: dict) -> dict:
    action = (args.get("action") or "").lower().strip().replace(" ", "_")
    synonyms = {
        "play": "play_pause", "pause": "play_pause", "resume": "play_pause",
        "skip": "next", "next_track": "next", "forward": "next",
        "back": "previous", "previous_track": "previous", "back_track": "previous",
    }
    action = synonyms.get(action, action)

    if action not in ("play_pause", "next", "previous", "stop"):
        raise Refused("I can play, pause, skip, or go back.")

    _tap(VK[action])
    spoken = {
        "play_pause": "Done.",
        "next": "Skipped.",
        "previous": "Went back.",
        "stop": "Stopped.",
    }
    return {"action": action, "speech": spoken[action]}

"""
Tier 1 router tests.

A false Tier 1 match silently does the wrong thing, so the "must fall through
to the LLM" cases below matter more than the hits.

    python scripts/test_router.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub.brain import rules  # noqa: F401 -- registers the rules
from hub.brain.router import match

# (utterance, expected tool or None, expected args subset)
HITS = [
    ("set a timer for 5 minutes", "set_timer", {"seconds": 300}),
    ("set a timer for five minutes", "set_timer", {"seconds": 300}),
    ("timer for 90 seconds", "set_timer", {"seconds": 90}),
    ("set a timer for two hours", "set_timer", {"seconds": 7200}),
    ("hey jarvis set a timer for ten minutes", "set_timer", {"seconds": 600}),
    ("time me for 30 seconds", "set_timer", {"seconds": 30}),
    ("wake me in twenty five minutes", "set_timer", {"seconds": 1500}),
    ("cancel the timers", "cancel_timers", {}),
    ("stop timer", "cancel_timers", {}),
    ("what timers are running", "list_timers", {}),
    ("how much time is left", "list_timers", {}),

    ("open chrome", "open_app", {"name": "chrome"}),
    ("launch spotify", "open_app", {"name": "spotify"}),
    ("open chrome on the bedroom laptop", "open_app",
     {"name": "chrome", "device": "bedroom laptop"}),
    ("please open firefox", "open_app", {"name": "firefox"}),
    ("close chrome", "close_app", {"name": "chrome"}),

    ("lock the screen", "lock_computer", {}),
    ("lock the computer on the work laptop", "lock_computer", {"device": "work laptop"}),
    ("what computers are online", "list_computers", {}),
    ("what's connected", "list_computers", {}),

    ("volume 40", "set_volume", {"level": 40}),
    ("set the volume to 70 percent", "set_volume", {"level": 70}),
    ("turn it up", "set_volume", {"direction": "up"}),
    ("louder", "set_volume", {"direction": "up"}),
    ("quieter", "set_volume", {"direction": "down"}),
    ("mute", "set_volume", {"direction": "mute"}),
    ("mute the sound on the desktop", "set_volume",
     {"direction": "mute", "device": "desktop"}),

    ("pause", "media_control", {"action": "play_pause"}),
    ("play the music", "media_control", {"action": "play_pause"}),
    ("skip", "media_control", {"action": "next"}),
    ("next track", "media_control", {"action": "next"}),
    ("go back", "media_control", {"action": "previous"}),
    # smart home -- the highest-value fast path in the whole table. A light
    # switch routed through the LLM costs 2-4s for what should feel instant.
    ("turn off the bedroom lights", "control_device",
     {"name": "bedroom lights", "state": "off"}),
    ("turn on the kitchen plug", "control_device",
     {"name": "kitchen plug", "state": "on"}),
    ("switch off the fan", "control_device", {"name": "fan", "state": "off"}),
    ("turn the lights off", "control_device", {"name": "lights", "state": "off"}),
    ("hey jarvis turn on the lamp", "control_device",
     {"name": "lamp", "state": "on"}),
    ("toggle the fan", "control_device", {"name": "fan", "state": "toggle"}),
    ("dim the bedroom lights to 30 percent", "set_brightness",
     {"name": "bedroom lights", "percent": 30}),
    ("set the lamp to fifty", "set_brightness", {"name": "lamp", "percent": 50}),
    ("are the bedroom lights on", "device_state", {"name": "bedroom lights"}),
    ("what lights do i have", "list_smart_devices", {}),

    # notes / lists / reminders. Promoted here because Tier 2 got them WRONG,
    # not merely slowly -- qwen2.5:3b answered "Here are your recent notes:"
    # and called nothing on 4 of 5 attempts.
    ("read me my notes", "read_notes", {}),
    ("what are my notes", "read_notes", {}),
    ("read my notes back to me", "read_notes", {}),
    ("add milk to the shopping list", "add_to_list",
     {"item": "milk", "list_name": "shopping"}),
    ("put eggs on the shopping list", "add_to_list",
     {"item": "eggs", "list_name": "shopping"}),
    ("what's on the shopping list", "read_list", {"list_name": "shopping"}),
    ("read the packing list", "read_list", {"list_name": "packing"}),
    ("what reminders do i have", "list_reminders", {}),

    # these must keep their existing meaning now that turn/switch are claimed
    ("what devices are online", "list_computers", {}),
    ("stop the timer", "cancel_timers", {}),
]

# These must NOT match Tier 1 -- they need the LLM.
FALLTHROUGH = [
    "what's the weather like tomorrow",
    "open my downloads folder",
    "open the documents folder on the laptop",
    "what's in my downloads",
    "email mum about dinner",
    "remind me to call the landlord at six",
    "how many grams in an ounce",
    "play something upbeat on spotify",
    "delete the old screenshots",
    "turn that song up a bit",
    "is dinner ready",
    "what is the weather like on friday",
    # needs the model to pull the note text out of the sentence
    "make a note that the boiler needs servicing",
    "add milk",
    "",
    "uh",
]


def main() -> int:
    failures = 0

    print("expected Tier 1 hits")
    for text, tool, expected_args in HITS:
        hit = match(text)
        if hit is None:
            print(f"  [FAIL] {text!r:<46} fell through, wanted {tool}")
            failures += 1
            continue
        if hit.tool != tool:
            print(f"  [FAIL] {text!r:<46} -> {hit.tool}, wanted {tool}")
            failures += 1
            continue
        wrong = {k: (hit.args.get(k), v) for k, v in expected_args.items()
                 if hit.args.get(k) != v}
        if wrong:
            print(f"  [FAIL] {text!r:<46} -> {hit.tool} args {hit.args} "
                  f"mismatched {wrong}")
            failures += 1
            continue
        print(f"  [ok  ] {text!r:<46} -> {hit.tool}({hit.args})")

    print("\nmust fall through to the LLM")
    for text in FALLTHROUGH:
        hit = match(text)
        if hit is None:
            print(f"  [ok  ] {text!r:<46} -> LLM")
        else:
            print(f"  [FAIL] {text!r:<46} wrongly matched {hit.tool}({hit.args})")
            failures += 1

    total = len(HITS) + len(FALLTHROUGH)
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

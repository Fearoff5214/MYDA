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
]

# These must NOT match Tier 1 -- they need the LLM.
FALLTHROUGH = [
    "what's the weather like tomorrow",
    "open my downloads folder",
    "open the documents folder on the laptop",
    "what's in my downloads",
    "email mum about dinner",
    "remind me to call the landlord at six",
    "turn off the bedroom lights",
    "how many grams in an ounce",
    "play something upbeat on spotify",
    "delete the old screenshots",
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

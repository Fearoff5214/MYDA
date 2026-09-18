"""
Tier 1 rules: the commands that must never wait for an LLM.

Each rule is a regex over the normalised transcript whose named groups become
tool arguments. These cover the handful of things said many times a day --
lights, volume, apps, timers. Everything else falls through to Tier 2.

Promote a rule here when the audit log shows Tier 2 resolving the same
utterance shape repeatedly. Keep the patterns tight: a false Tier 1 match
silently does the wrong thing, whereas a miss merely costs a second.
"""
from __future__ import annotations

import re

from .router import rule

# ---------------------------------------------------------------------------
# spoken numbers

UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
    # Whisper often writes these as digits, but not always.
    "a": 1, "an": 1, "couple": 2, "few": 3, "half": 0,
}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
        "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}

NUMBER_WORDS = "|".join([*UNITS, *TENS])


def to_number(text: str, default: int = 1) -> int:
    """'twenty five' -> 25, '90' -> 90."""
    text = (text or "").strip().lower().replace("-", " ")
    if not text:
        return default
    if text.isdigit():
        return int(text)

    total = 0
    matched = False
    for word in text.split():
        if word in TENS:
            total += TENS[word]
            matched = True
        elif word in UNITS:
            total += UNITS[word]
            matched = True
        elif word.isdigit():
            total += int(word)
            matched = True
    return total if matched else default


NUM = rf"(?P<count>\d+|(?:{NUMBER_WORDS})(?:[\s-](?:{NUMBER_WORDS}))?)"

# ---------------------------------------------------------------------------
# timers

_UNIT_SECONDS = {"second": 1, "seconds": 1, "sec": 1, "secs": 1,
                 "minute": 60, "minutes": 60, "min": 60, "mins": 60,
                 "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600}


def _timer_args(groups: dict[str, str]) -> dict:
    count = to_number(groups.get("count", ""), default=1)
    unit = (groups.get("unit") or "minute").rstrip(".")
    return {"seconds": count * _UNIT_SECONDS.get(unit, 60)}


UNITS_RE = "|".join(_UNIT_SECONDS)

rule(rf"(?:set |start )?(?:a |an )?timer (?:for |of )?{NUM} (?P<unit>{UNITS_RE})",
     "set_timer", transform=_timer_args)
rule(rf"time (?:me )?(?:for )?{NUM} (?P<unit>{UNITS_RE})",
     "set_timer", transform=_timer_args)
rule(rf"(?:remind|wake) me in {NUM} (?P<unit>{UNITS_RE})",
     "set_timer", transform=_timer_args)
rule(r"(?:cancel|stop|clear) (?:the |all |my )?timers?", "cancel_timers")
rule(r"(?:what|which|how many) timers?(?: are)?(?: running| left| going)?",
     "list_timers")
rule(r"(?:how (?:much|long) (?:time )?(?:is )?left|time left)(?: on the timer)?",
     "list_timers")

# ---------------------------------------------------------------------------
# applications

DEVICE_TAIL = r"(?: on (?:the )?(?P<device>[\w\s-]+?))?"

# "open my downloads folder" is a file request, not an app request. Excluding
# those words here keeps it falling through to the LLM instead of being sent
# to the node as a bogus app name.
NOT_A_FILE = r"(?!.*\b(?:folder|directory|file|files|document|documents)\b)"

rule(rf"(?:open|launch|start|run) (?:the )?(?P<name>{NOT_A_FILE}[\w\s.+-]+?){DEVICE_TAIL}",
     "open_app")
rule(rf"(?:close|quit|kill) (?:the )?(?P<name>{NOT_A_FILE}[\w\s.+-]+?){DEVICE_TAIL}",
     "close_app")

# ---------------------------------------------------------------------------
# machine state

rule(rf"lock (?:the )?(?:screen|computer|pc|machine|laptop|desktop){DEVICE_TAIL}",
     "lock_computer")
rule(r"(?:what|which) (?:computers?|machines?|devices?) (?:are )?(?:online|connected|up|available)",
     "list_computers")
rule(r"(?:what'?s|what is) (?:online|connected)", "list_computers")

# ---------------------------------------------------------------------------
# volume and media


def _level(groups: dict[str, str]) -> dict:
    out = {"level": to_number(groups.get("count", ""), default=50)}
    if device := groups.get("device"):
        out["device"] = device
    return out


rule(rf"(?:set (?:the )?)?volume (?:to |at )?{NUM}(?: percent)?{DEVICE_TAIL}",
     "set_volume", transform=_level)
rule(rf"(?:turn (?:it |the volume )?up|louder|volume up){DEVICE_TAIL}",
     "set_volume", direction="up")
rule(rf"(?:turn (?:it |the volume )?down|quieter|volume down){DEVICE_TAIL}",
     "set_volume", direction="down")
rule(rf"(?:mute|unmute|silence)(?: (?:it|the volume|the sound))?{DEVICE_TAIL}",
     "set_volume", direction="mute")

rule(rf"(?:play|pause|resume)(?: (?:it|the music|music|the song))?{DEVICE_TAIL}",
     "media_control", action="play_pause")
rule(rf"(?:next|skip)(?: (?:track|song|this))?{DEVICE_TAIL}",
     "media_control", action="next")
rule(rf"(?:previous|last|go back)(?: (?:track|song))?{DEVICE_TAIL}",
     "media_control", action="previous")

# ---------------------------------------------------------------------------
# smart home
#
# These matter more than anything else here. "Turn off the lights" is the most
# spoken command in a house, and routing it through a 14B model costs 2-4
# seconds for what should be instant. The device name is passed through
# verbatim -- tools/home.py fuzzy-matches it against Home Assistant's live
# friendly names, so nothing here needs to know what devices exist.

# "turn off the timer" should reach cancel_timers, not hunt for a smart plug
# called "timer". Same for the volume rules above, which own "turn it up".
NOT_A_TIMER = r"(?!(?:the )?timers?\b)"
DEVICE_NAME = rf"{NOT_A_TIMER}[\w\s'-]+?"

rule(rf"(?:turn|switch) (?P<state>on|off) (?:the |my )?(?P<name>{DEVICE_NAME})",
     "control_device")
rule(rf"(?:turn|switch) (?:the |my )?(?P<name>{DEVICE_NAME}) (?P<state>on|off)",
     "control_device")
rule(rf"(?:put )(?:the |my )?(?P<name>{DEVICE_NAME}) (?P<state>on|off)",
     "control_device")
rule(rf"toggle (?:the |my )?(?P<name>{DEVICE_NAME})",
     "control_device", state="toggle")


def _brightness(groups: dict[str, str]) -> dict:
    return {"name": groups.get("name", "").strip(),
            "percent": to_number(groups.get("count", ""), default=50)}


rule(rf"(?:dim|set|brighten) (?:the |my )?(?P<name>{DEVICE_NAME}) to {NUM}(?: percent)?",
     "set_brightness", transform=_brightness)

rule(rf"(?:are|is) (?:the |my )?(?P<name>{DEVICE_NAME}) (?:on|off)",
     "device_state")
rule(r"(?:what|which) (?:smart )?(?:devices?|lights?|things?)"
     r"(?: can (?:you|i) control| are there| do i have)?",
     "list_smart_devices")

# ---------------------------------------------------------------------------
# notes, lists and reminders
#
# These are here because Tier 2 got them wrong, not merely slowly. Asked to
# "read me my notes", qwen2.5:3b replied "Here are your recent notes:" and
# called nothing -- promising content it then never delivered, on 4 of 5
# attempts. A small model is unreliable at tools whose whole job is to fetch
# something, because answering *looks* like succeeding.
#
# The phrasings are fixed and few, so a regex is strictly better than a model
# here: correct every time, and ~1ms instead of 3-26s.

LIST_NAME = r"(?P<list_name>[\w\s'-]+?)"

rule(r"(?:read|say|tell me)(?: me)?(?: back)?(?: my| the)? notes(?: back(?: to me)?)?",
     "read_notes")
rule(r"(?:what|which) (?:are |were )?(?:my |the )?notes(?: again)?",
     "read_notes")
rule(r"(?:what'?s|what is|what have i got) (?:in |on )?(?:my |the )?notes",
     "read_notes")

rule(rf"(?:what'?s|what is) on (?:my |the )?{LIST_NAME}(?: list)?", "read_list")
rule(rf"read (?:me )?(?:my |the )?{LIST_NAME} list", "read_list")
rule(rf"(?:what'?s|what is) (?:left )?on (?:my |the )?{LIST_NAME} list", "read_list")

rule(rf"add (?P<item>[\w\s'-]+?) to (?:my |the )?{LIST_NAME}(?: list)?",
     "add_to_list")
rule(rf"put (?P<item>[\w\s'-]+?) on (?:my |the )?{LIST_NAME}(?: list)?",
     "add_to_list")

rule(r"(?:what|which) reminders?(?: are set| do i have| have i got)?", "list_reminders")
rule(r"(?:what'?s|what is) (?:my |the )?next reminder", "list_reminders")

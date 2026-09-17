"""
Spoken confirmation for destructive actions.

Anything marked confirm=True in the registry stops here first. The pending
call is parked on the session and Jarvis asks a yes/no question; the next
utterance is interpreted as the answer rather than as a new command.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

# Deliberately strict. An ambiguous mumble must not be read as consent, so
# anything not clearly affirmative is treated as a cancellation.
_YES = re.compile(
    r"^\s*(yes|yeah|yep|yup|sure|ok|okay|do it|go ahead|confirm(ed)?|"
    r"affirmative|please do|correct|right)\b", re.I)
_NO = re.compile(
    r"^\s*(no|nope|nah|cancel|stop|don'?t|abort|never ?mind|forget it)\b", re.I)

PENDING_TTL = 60.0  # seconds; an old confirmation must not fire later


@dataclass(slots=True)
class PendingAction:
    tool: str
    args: dict[str, Any]
    question: str
    created: float = field(default_factory=time.monotonic)

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.created > PENDING_TTL


def interpret(text: str) -> bool | None:
    """True = confirmed, False = cancelled, None = not a yes/no answer."""
    if _YES.match(text):
        return True
    if _NO.match(text):
        return False
    return None


# Spoken out loud, so the generic "close app with name notepad" phrasing reads
# badly. Each destructive tool gets a sentence a person would actually say.
# Missing keys fall back to the generic form rather than raising.
_PHRASING: dict[str, str] = {
    "close_app": "Close {name}?",
    "delete_files": "Permanently delete {count_paths}? This can't be undone.",
    "send_text": "Send {message} to {number}?",
    "call_number": "Call {number}?",
    "clear_list": "Clear the whole {list_name} list?",
    "send_email": "Send an email to {to}, subject {subject}?",
    "create_event": "Add {summary} to your calendar?",
}


def _fill(template: str, args: dict[str, Any]) -> str | None:
    values = dict(args)
    # Reading out twelve file paths is useless; a count is what matters.
    for key, value in list(args.items()):
        if isinstance(value, (list, tuple)):
            n = len(value)
            values[f"count_{key}"] = (
                f"{n} files" if n != 1 else str(value[0]).rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
            )
    try:
        return template.format(**values)
    except (KeyError, IndexError):
        return None


def question_for(tool_name: str, args: dict[str, Any]) -> str:
    """Read the action back so the user confirms what will actually happen."""
    template = _PHRASING.get(tool_name)
    if template and (spoken := _fill(template, args)):
        return spoken

    detail = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in args.items() if v)
    action = tool_name.replace("_", " ").strip()
    return f"You want me to {action}{' with ' + detail if detail else ''}. Should I go ahead?"

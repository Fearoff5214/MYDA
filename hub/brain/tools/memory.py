"""
Notes, lists and reminders. Entirely local -- this data never leaves the hub.

Reminders are persisted, unlike timers: "remind me at six" must survive a
restart, whereas a reboot should not resurrect yesterday's pasta timer.
hub.server re-arms outstanding reminders at startup via rearm_reminders().
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timedelta

from ..context import Context
from ..registry import ToolResult, tool

log = logging.getLogger("jarvis.memory")

MAX_SPOKEN_ITEMS = 12


def _spoken_list(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    head = items[:MAX_SPOKEN_ITEMS]
    more = len(items) - len(head)
    joined = f"{', '.join(head[:-1])} and {head[-1]}"
    return joined + (f", plus {more} more" if more > 0 else "")


# ---------------------------------------------------------------------------
# notes


@tool(
    name="add_note",
    description="Save a short note for the user to read back later.",
    parameters={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
)
async def add_note(ctx: Context, text: str) -> ToolResult:
    text = (text or "").strip()
    if not text:
        return ToolResult.fail("What should the note say?")
    ctx.db.execute(
        "INSERT INTO notes (body, created, device) VALUES (?, ?, ?)",
        (text, time.time(), ctx.device),
    )
    ctx.db.commit()
    return ToolResult.say("Noted.")


@tool(
    name="read_notes",
    description="Read back the user's most recent notes.",
    parameters={
        "type": "object",
        "properties": {"count": {"type": "integer", "description": "How many, default 5"}},
    },
)
async def read_notes(ctx: Context, count: int = 5) -> ToolResult:
    try:
        count = max(1, min(20, int(count)))
    except (TypeError, ValueError):
        count = 5
    rows = ctx.db.execute(
        "SELECT body FROM notes ORDER BY created DESC LIMIT ?", (count,)
    ).fetchall()
    if not rows:
        return ToolResult.say("You have no notes.")
    bodies = [r["body"] for r in rows]
    if len(bodies) == 1:
        return ToolResult.say(f"One note: {bodies[0]}")
    return ToolResult.say(
        f"Your last {len(bodies)} notes: " + ". ".join(bodies), data=bodies
    )


# ---------------------------------------------------------------------------
# lists


@tool(
    name="add_to_list",
    description=(
        "Add an item to a named list, such as a shopping or packing list. "
        "Use for 'add milk to the shopping list'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "list_name": {"type": "string", "description": "e.g. shopping"},
            "item": {"type": "string"},
        },
        "required": ["list_name", "item"],
    },
)
async def add_to_list(ctx: Context, list_name: str, item: str) -> ToolResult:
    list_name = (list_name or "").strip().lower().removesuffix(" list").strip()
    item = (item or "").strip()
    if not list_name or not item:
        return ToolResult.fail("Which list, and what should I add?")

    ctx.db.execute(
        "INSERT INTO list_items (list_name, item, done, created) VALUES (?, ?, 0, ?)",
        (list_name, item, time.time()),
    )
    ctx.db.commit()
    return ToolResult.say(f"Added {item} to the {list_name} list.")


@tool(
    name="read_list",
    description="Read back what is on a named list.",
    parameters={
        "type": "object",
        "properties": {"list_name": {"type": "string"}},
        "required": ["list_name"],
    },
)
async def read_list(ctx: Context, list_name: str) -> ToolResult:
    list_name = (list_name or "").strip().lower().removesuffix(" list").strip()
    rows = ctx.db.execute(
        "SELECT item FROM list_items WHERE list_name = ? AND done = 0 "
        "ORDER BY created", (list_name,)
    ).fetchall()
    if not rows:
        return ToolResult.say(f"The {list_name} list is empty.")
    items = [r["item"] for r in rows]
    return ToolResult.say(
        f"{len(items)} item{'s' if len(items) != 1 else ''} on the {list_name} list: "
        f"{_spoken_list(items)}.",
        data=items,
    )


@tool(
    name="remove_from_list",
    description="Tick off or remove one item from a named list.",
    parameters={
        "type": "object",
        "properties": {
            "list_name": {"type": "string"},
            "item": {"type": "string"},
        },
        "required": ["list_name", "item"],
    },
)
async def remove_from_list(ctx: Context, list_name: str, item: str) -> ToolResult:
    list_name = (list_name or "").strip().lower().removesuffix(" list").strip()
    item = (item or "").strip()
    # LIKE so "milk" ticks off "semi skimmed milk".
    cur = ctx.db.execute(
        "UPDATE list_items SET done = 1 WHERE list_name = ? AND done = 0 "
        "AND item LIKE ? ", (list_name, f"%{item}%"),
    )
    ctx.db.commit()
    if not cur.rowcount:
        return ToolResult.fail(f"I couldn't find {item} on the {list_name} list.")
    return ToolResult.say(f"Removed {item} from the {list_name} list.")


@tool(
    name="clear_list",
    description="Empty a named list completely.",
    parameters={
        "type": "object",
        "properties": {"list_name": {"type": "string"}},
        "required": ["list_name"],
    },
    confirm=True,
)
async def clear_list(ctx: Context, list_name: str) -> ToolResult:
    list_name = (list_name or "").strip().lower().removesuffix(" list").strip()
    cur = ctx.db.execute(
        "UPDATE list_items SET done = 1 WHERE list_name = ? AND done = 0", (list_name,)
    )
    ctx.db.commit()
    if not cur.rowcount:
        return ToolResult.say(f"The {list_name} list was already empty.")
    return ToolResult.say(f"Cleared the {list_name} list.")


# ---------------------------------------------------------------------------
# reminders


@tool(
    name="set_reminder",
    description=(
        "Remind the user of something at a specific time. Unlike a timer this "
        "survives a restart. Work out the absolute local date and time from "
        "whatever the user said and pass it as ISO 8601."
    ),
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "What to remind them of"},
            "when": {"type": "string",
                     "description": "Local time, ISO 8601, e.g. 2026-09-17T18:00:00"},
        },
        "required": ["text", "when"],
    },
)
async def set_reminder(ctx: Context, text: str, when: str) -> ToolResult:
    text = (text or "").strip()
    if not text:
        return ToolResult.fail("What should I remind you about?")

    try:
        due = datetime.fromisoformat(when.replace("Z", "").strip())
    except (ValueError, AttributeError):
        return ToolResult.fail("I didn't catch when you wanted that.")

    if due <= datetime.now():
        return ToolResult.fail("That time has already passed.")

    cur = ctx.db.execute(
        "INSERT INTO reminders (body, due, device, fired) VALUES (?, ?, ?, 0)",
        (text, due.timestamp(), ctx.device),
    )
    ctx.db.commit()
    row_id = cur.lastrowid

    ctx.scheduler.at(due, ctx.device, f"Reminder: {text}", prefix=f"reminder{row_id}")
    when_spoken = due.strftime("%I:%M %p").lstrip("0").lower()
    same_day = due.date() == datetime.now().date()
    phrase = f"at {when_spoken}" if same_day else due.strftime("on %A at ") + when_spoken
    return ToolResult.say(f"I'll remind you {phrase}.", data={"id": row_id})


@tool(
    name="list_reminders",
    description="Say what reminders are set.",
)
async def list_reminders(ctx: Context) -> ToolResult:
    rows = ctx.db.execute(
        "SELECT body, due FROM reminders WHERE fired = 0 AND due > ? ORDER BY due",
        (time.time(),),
    ).fetchall()
    if not rows:
        return ToolResult.say("You have no reminders set.")
    bits = [
        f"{r['body']} at {datetime.fromtimestamp(r['due']).strftime('%I:%M %p').lstrip('0').lower()}"
        for r in rows[:5]
    ]
    return ToolResult.say(f"{len(rows)} reminder{'s' if len(rows) != 1 else ''}: "
                          + "; ".join(bits) + ".")


def rearm_reminders(db: sqlite3.Connection, scheduler) -> int:
    """Re-schedule reminders that outlived a restart. Called at startup.

    Anything already overdue is marked fired rather than announced -- being
    told about a 6am reminder at noon is worse than silence.
    """
    now = time.time()
    overdue = db.execute(
        "UPDATE reminders SET fired = 1 WHERE fired = 0 AND due <= ?", (now,)
    ).rowcount
    rows = db.execute(
        "SELECT id, body, due, device FROM reminders WHERE fired = 0"
    ).fetchall()
    db.commit()

    for row in rows:
        scheduler.at(
            datetime.fromtimestamp(row["due"]),
            row["device"] or "desktop",
            f"Reminder: {row['body']}",
            prefix=f"reminder{row['id']}",
        )
    if overdue:
        log.info("marked %d overdue reminders as fired", overdue)
    if rows:
        log.info("re-armed %d reminders", len(rows))
    return len(rows)

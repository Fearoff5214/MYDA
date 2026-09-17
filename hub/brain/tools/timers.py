"""Timers -- the one tool Phase 1 ships, to prove the whole pipeline end to end."""
from __future__ import annotations

from ..context import Context
from ..registry import ToolResult, tool

MAX_SECONDS = 24 * 3600


def spoken_duration(seconds: int) -> str:
    """120 -> 'two minutes'. Voice output should never contain '120s'."""
    words = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
             7: "seven", 8: "eight", 9: "nine", 10: "ten", 15: "fifteen",
             20: "twenty", 30: "thirty", 45: "forty-five", 60: "sixty"}

    def unit(n: int, name: str) -> str:
        label = words.get(n, str(n))
        return f"{label} {name}{'' if n == 1 else 's'}"

    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    parts = [unit(v, n) for v, n in ((hours, "hour"), (minutes, "minute"), (secs, "second")) if v]
    if not parts:
        return "no time at all"
    if len(parts) == 1:
        return parts[0]
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


@tool(
    name="set_timer",
    description=(
        "Set a countdown timer that speaks out loud when it finishes. Use for "
        "'set a timer for 5 minutes', 'time 90 seconds', 'wake me in an hour'. "
        "Convert whatever the user said into a total number of seconds."
    ),
    parameters={
        "type": "object",
        "properties": {
            "seconds": {"type": "integer", "description": "Total duration in seconds"},
            "label": {"type": "string", "description": "Optional name, e.g. 'pasta'"},
        },
        "required": ["seconds"],
    },
)
async def set_timer(ctx: Context, seconds: int, label: str = "") -> ToolResult:
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return ToolResult.fail("I didn't understand how long to set that for.")
    if seconds <= 0:
        return ToolResult.fail("That timer needs to be longer than zero.")
    if seconds > MAX_SECONDS:
        return ToolResult.fail("I can only set timers up to a day.")

    name = label.strip()
    done = f"Your {name} timer is up." if name else f"Your {spoken_duration(seconds)} timer is up."
    job_id = ctx.scheduler.after(seconds, ctx.device, done, prefix="timer")

    said = f"{name} timer set for {spoken_duration(seconds)}." if name \
        else f"Timer set for {spoken_duration(seconds)}."
    return ToolResult.say(said, data={"job_id": job_id, "seconds": seconds})


@tool(
    name="list_timers",
    description="Say what timers are currently running and how long is left on each.",
)
async def list_timers(ctx: Context) -> ToolResult:
    jobs = ctx.scheduler.pending("timer")
    if not jobs:
        return ToolResult.say("You have no timers running.")

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    bits = []
    for job in jobs:
        when = job["when"]
        if when is None:
            continue
        left = int((when - now.astimezone(when.tzinfo)).total_seconds())
        if left > 0:
            bits.append(spoken_duration(left))
    if not bits:
        return ToolResult.say("You have no timers running.")
    if len(bits) == 1:
        return ToolResult.say(f"One timer, {bits[0]} left.")
    return ToolResult.say(f"{len(bits)} timers: {', '.join(bits)} remaining.")


@tool(
    name="cancel_timers",
    description="Cancel every running timer.",
)
async def cancel_timers(ctx: Context) -> ToolResult:
    n = ctx.scheduler.cancel_all("timer")
    if not n:
        return ToolResult.say("There were no timers to cancel.")
    return ToolResult.say("Timer cancelled." if n == 1 else f"Cancelled {n} timers.")

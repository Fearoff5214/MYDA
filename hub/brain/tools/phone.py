"""
Phone actions, dispatched to the Android client over the node protocol.

The phone registers as a node like any PC, so these tools are thin wrappers
around the same nodes.send() used by pc.py. Anything outward-facing -- texts
and calls -- is marked confirm=True, because a misheard name should never
result in a message actually leaving the device.
"""
from __future__ import annotations

from typing import Any

from ..context import Context
from ..registry import ToolResult, tool

# The phone is found by capability rather than by name, so renaming the device
# does not break these tools.
PHONE_CAPABILITY = "send_sms"


def phone_connected(ctx: Context) -> bool:
    """Used as the availability predicate for every tool in this module."""
    return _find_phone(ctx) is not None


def _find_phone(ctx: Context) -> str | None:
    for node in ctx.nodes.describe():
        if PHONE_CAPABILITY in node.get("capabilities", []):
            return node["device"]
    return None


async def _phone(ctx: Context, action: str, args: dict[str, Any],
                 timeout: float = 30.0) -> ToolResult:
    device = _find_phone(ctx)
    if device is None:
        return ToolResult.fail("Your phone isn't connected right now.")

    reply = await ctx.nodes.send(device, action, args, timeout=timeout)
    if not reply.get("ok"):
        return ToolResult.fail(reply.get("error") or "That didn't work on the phone.")

    data = reply.get("data") or {}
    return ToolResult.say(data.get("speech") or "Done.", data=data)


@tool(
    name="send_text",
    description=(
        "Send an SMS from the user's phone. Use for 'text Amma I'm running "
        "late'. Needs the actual phone number."
    ),
    parameters={
        "type": "object",
        "properties": {
            "number": {"type": "string", "description": "Phone number to text"},
            "message": {"type": "string"},
        },
        "required": ["number", "message"],
    },
    confirm=True,
    available=phone_connected,
)
async def send_text(ctx: Context, number: str, message: str) -> ToolResult:
    return await _phone(ctx, "send_sms", {"number": number, "message": message})


@tool(
    name="read_texts",
    description="Read out the user's most recent text messages.",
    parameters={
        "type": "object",
        "properties": {"limit": {"type": "integer", "description": "How many, default 5"}},
    },
    available=phone_connected,
)
async def read_texts(ctx: Context, limit: int = 5) -> ToolResult:
    return await _phone(ctx, "read_sms", {"limit": limit})


@tool(
    name="call_number",
    description="Start a phone call from the user's phone.",
    parameters={
        "type": "object",
        "properties": {"number": {"type": "string"}},
        "required": ["number"],
    },
    confirm=True,
    available=phone_connected,
)
async def call_number(ctx: Context, number: str) -> ToolResult:
    return await _phone(ctx, "call", {"number": number})


@tool(
    name="set_phone_alarm",
    description=(
        "Set an alarm on the user's phone for a clock time, in 24-hour form. "
        "Use for 'wake me at seven', which is hour 7 minute 0."
    ),
    parameters={
        "type": "object",
        "properties": {
            "hour": {"type": "integer", "description": "0 to 23"},
            "minute": {"type": "integer", "description": "0 to 59"},
        },
        "required": ["hour"],
    },
    available=phone_connected,
)
async def set_phone_alarm(ctx: Context, hour: int, minute: int = 0) -> ToolResult:
    try:
        hour = int(hour) % 24
        minute = max(0, min(59, int(minute)))
    except (TypeError, ValueError):
        return ToolResult.fail("What time should the alarm be?")
    return await _phone(ctx, "set_alarm", {"hour": hour, "minute": minute})


@tool(
    name="read_notifications",
    description="Read out the notifications currently on the user's phone.",
    available=phone_connected,
)
async def read_notifications(ctx: Context) -> ToolResult:
    return await _phone(ctx, "notifications", {})


@tool(
    name="phone_battery",
    description="Say how much battery the user's phone has left.",
    available=phone_connected,
)
async def phone_battery(ctx: Context) -> ToolResult:
    return await _phone(ctx, "battery", {})


@tool(
    name="find_phone",
    description=(
        "Make the user's phone ring and vibrate so they can find it, even if "
        "it is on silent."
    ),
    available=phone_connected,
)
async def find_phone(ctx: Context) -> ToolResult:
    return await _phone(ctx, "find_phone", {})

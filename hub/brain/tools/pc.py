"""
Controlling the user's computers.

Each tool resolves a spoken machine name to a connected node, dispatches, and
turns the node's reply into something worth saying out loud. Nodes return a
`speech` field for exactly this reason -- the machine that did the work knows
best how to describe what happened.
"""
from __future__ import annotations

from typing import Any

from ..context import Context
from ..registry import ToolResult, tool

DEVICE_PARAM = {
    "type": "string",
    "description": (
        "Which computer, as the user said it, e.g. 'the bedroom laptop'. "
        "Leave empty to use the machine they are speaking to."
    ),
}


def any_node(ctx: Context) -> bool:
    """Availability predicate: no connected PCs means these all fail."""
    return bool(ctx.nodes.online())


def _target(ctx: Context, device: str) -> tuple[str | None, str | None]:
    """Resolve a device, preferring the one the user is speaking to."""
    hint = (device or "").strip()
    if not hint:
        # No machine named: the one they are talking to, if it runs a node.
        if ctx.device in ctx.nodes.online():
            return ctx.device, None
        resolved = ctx.nodes.resolve(None)
        if resolved:
            return resolved, None
        return None, "None of your computers are connected right now."

    resolved = ctx.nodes.resolve(hint)
    if resolved:
        return resolved, None

    online = ctx.nodes.online()
    if not online:
        return None, "None of your computers are connected right now."
    return None, f"I don't know a machine called {hint}. I can see {', '.join(online)}."


async def _run(ctx: Context, device: str, action: str, args: dict[str, Any],
               timeout: float = 20.0) -> ToolResult:
    target, problem = _target(ctx, device)
    if problem:
        return ToolResult.fail(problem)

    reply = await ctx.nodes.send(target, action, args, timeout=timeout)
    if not reply.get("ok"):
        return ToolResult.fail(reply.get("error") or "That didn't work.")

    data = reply.get("data") or {}
    speech = data.get("speech") or "Done."
    # Name the machine when it is not the one being spoken to, so the user
    # knows where the thing actually happened.
    if target != ctx.device and not speech.endswith(f"on {target}."):
        speech = speech.rstrip(".") + f", on {target}."
    return ToolResult.say(speech, data=data)


@tool(
    name="open_app",
    description=(
        "Open an application on one of the user's computers. Use for "
        "'open Chrome', 'launch Spotify on the bedroom laptop'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Application name, e.g. 'chrome'"},
            "device": DEVICE_PARAM,
        },
        "required": ["name"],
    },
    available=any_node,
)
async def open_app(ctx: Context, name: str, device: str = "") -> ToolResult:
    return await _run(ctx, device, "open_app", {"name": name})


@tool(
    name="close_app",
    description="Close a running application on one of the user's computers.",
    parameters={
        "type": "object",
        "properties": {"name": {"type": "string"}, "device": DEVICE_PARAM},
        "required": ["name"],
    },
    confirm=True,
    available=any_node,
)
async def close_app(ctx: Context, name: str, device: str = "") -> ToolResult:
    return await _run(ctx, device, "close_app", {"name": name})


@tool(
    name="list_folder",
    description=(
        "Say what is in a folder on one of the user's computers. Use for "
        "'what's in my downloads', 'check my documents folder'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "Full folder path, e.g. C:/Users/param/Downloads"},
            "device": DEVICE_PARAM,
        },
        "required": ["path"],
    },
    available=any_node,
)
async def list_folder(ctx: Context, path: str, device: str = "") -> ToolResult:
    return await _run(ctx, device, "list_dir", {"path": path})


@tool(
    name="find_files",
    description="Search for files by name pattern inside a folder, e.g. all PDFs in Downloads.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Folder to search in"},
            "pattern": {"type": "string", "description": "Glob such as *.pdf"},
            "recursive": {"type": "boolean", "description": "Search subfolders too"},
            "device": DEVICE_PARAM,
        },
        "required": ["path", "pattern"],
    },
    available=any_node,
)
async def find_files(ctx: Context, path: str, pattern: str,
                     recursive: bool = False, device: str = "") -> ToolResult:
    return await _run(ctx, device, "find_files",
                      {"path": path, "pattern": pattern, "recursive": recursive},
                      timeout=45.0)


@tool(
    name="open_file",
    description="Open a specific file or folder in its default application.",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string"}, "device": DEVICE_PARAM},
        "required": ["path"],
    },
    available=any_node,
)
async def open_file(ctx: Context, path: str, device: str = "") -> ToolResult:
    return await _run(ctx, device, "open_path", {"path": path})


@tool(
    name="delete_files",
    description=(
        "Permanently delete files. Only works inside folders the user marked "
        "writable. Always confirmed out loud first."
    ),
    parameters={
        "type": "object",
        "properties": {
            "paths": {"type": "array", "items": {"type": "string"},
                      "description": "Full paths of the files to delete"},
            "device": DEVICE_PARAM,
        },
        "required": ["paths"],
    },
    confirm=True,
    available=any_node,
)
async def delete_files(ctx: Context, paths: list[str], device: str = "") -> ToolResult:
    return await _run(ctx, device, "delete_files", {"paths": paths})


@tool(
    name="set_volume",
    description=(
        "Change the volume on one of the user's computers. Either a direction "
        "('up', 'down', 'mute') or an absolute level from 0 to 100."
    ),
    parameters={
        "type": "object",
        "properties": {
            "direction": {"type": "string", "enum": ["up", "down", "mute"]},
            "level": {"type": "integer", "description": "0 to 100"},
            "device": DEVICE_PARAM,
        },
    },
    available=any_node,
)
async def set_volume(ctx: Context, direction: str = "", level: int | None = None,
                     device: str = "") -> ToolResult:
    args: dict[str, Any] = {}
    if level is not None:
        args["level"] = level
    if direction:
        args["direction"] = direction
    if not args:
        return ToolResult.fail("Up, down, mute, or a number from zero to a hundred?")
    return await _run(ctx, device, "volume", args)


@tool(
    name="media_control",
    description="Play, pause, skip or go back on whatever is playing on a computer.",
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["play_pause", "next", "previous", "stop"]},
            "device": DEVICE_PARAM,
        },
        "required": ["action"],
    },
    available=any_node,
)
async def media_control(ctx: Context, action: str, device: str = "") -> ToolResult:
    return await _run(ctx, device, "media_key", {"action": action})


@tool(
    name="lock_computer",
    description="Lock the screen on one of the user's computers.",
    parameters={
        "type": "object",
        "properties": {"device": DEVICE_PARAM},
    },
    available=any_node,
)
async def lock_computer(ctx: Context, device: str = "") -> ToolResult:
    return await _run(ctx, device, "lock", {})


@tool(
    name="list_computers",
    description=(
        "Say which of the user's computers are currently connected and "
        "reachable. Use when they ask what is online, or when a machine "
        "they named cannot be found."
    ),
)
async def list_computers(ctx: Context) -> ToolResult:
    machines = ctx.nodes.describe()
    if not machines:
        return ToolResult.say("None of your computers are connected right now.")
    names = [m["device"] for m in machines]
    if len(names) == 1:
        return ToolResult.say(f"Only {names[0]} is connected.")
    return ToolResult.say(f"{', '.join(names[:-1])} and {names[-1]} are connected.",
                          data=machines)

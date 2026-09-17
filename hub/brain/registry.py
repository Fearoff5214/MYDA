"""
Tool registry.

Every capability Jarvis has is a plain function decorated with @tool. The
decorator records a JSON Schema that is handed to Ollama verbatim as its tool
catalogue, so adding a capability means adding one function and importing its
module -- there is no second place to register it and no schema to keep in sync.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from typing import Any, Callable

from .context import Context

log = logging.getLogger("jarvis.registry")

EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


@dataclass(slots=True)
class ToolResult:
    """What a tool gives back. `speech` is what Jarvis says out loud."""

    ok: bool
    speech: str
    data: Any = None

    @classmethod
    def say(cls, speech: str, data: Any = None) -> "ToolResult":
        return cls(True, speech, data)

    @classmethod
    def fail(cls, speech: str) -> "ToolResult":
        return cls(False, speech)


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., Any]
    confirm: bool      # destructive -> require a spoken "yes" before running
    expose: bool       # False hides it from the LLM (Tier 1 fast path only)
    available: Callable[[Any], bool] | None  # None means always offered

    @property
    def schema(self) -> dict[str, Any]:
        """Ollama / OpenAI-style function schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


REGISTRY: dict[str, Tool] = {}


def tool(
    *,
    name: str,
    description: str,
    parameters: dict[str, Any] | None = None,
    confirm: bool = False,
    expose: bool = True,
    available: Callable[[Any], bool] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a function as a Jarvis tool.

    The function is called as fn(ctx, **args) and must return a ToolResult.

    Set confirm=True for anything destructive or outward-facing (deleting
    files, sending mail or SMS) -- the agent will then require a spoken
    confirmation before it runs.

    Pass `available` to hide the tool when its backing service is absent --
    offering the model a tool that can only fail wastes prompt budget and
    invites it to pick the wrong one.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        if name in REGISTRY:
            raise ValueError(f"duplicate tool name: {name!r}")
        REGISTRY[name] = Tool(
            name=name,
            description=description,
            parameters=parameters or EMPTY_SCHEMA,
            fn=fn,
            confirm=confirm,
            expose=expose,
            available=available,
        )
        log.debug("registered tool %s", name)
        return fn

    return decorator


def catalogue(ctx: Any = None) -> list[dict[str, Any]]:
    """The tool list sent to the LLM.

    Tier-1-only tools are omitted, and so is anything whose backing service is
    unavailable right now -- no phone connected, no Home Assistant configured.
    On a 14B model the full catalogue is ~2900 tokens, which costs latency and
    measurably degrades tool choice, so keeping it to what is actually
    reachable matters.
    """
    out: list[dict[str, Any]] = []
    for t in REGISTRY.values():
        if not t.expose:
            continue
        if t.available is not None and ctx is not None:
            try:
                if not t.available(ctx):
                    continue
            except Exception:  # noqa: BLE001 - a broken predicate must not hide everything
                log.exception("availability check for %s failed; offering it anyway", t.name)
        out.append(t.schema)
    return out


def get(name: str) -> Tool | None:
    return REGISTRY.get(name)


def _missing_required(tool_: Tool, args: dict[str, Any]) -> list[str]:
    required = tool_.parameters.get("required", [])
    return [k for k in required if k not in args or args[k] is None]


async def invoke(name: str, args: dict[str, Any], ctx: Context) -> ToolResult:
    """Run a tool by name. Never raises -- failures come back as ToolResult."""
    tool_ = REGISTRY.get(name)
    if tool_ is None:
        log.warning("unknown tool %r", name)
        return ToolResult.fail(f"I don't have a tool called {name}.")

    missing = _missing_required(tool_, args)
    if missing:
        return ToolResult.fail(f"I need to know the {missing[0].replace('_', ' ')}.")

    # Drop anything the schema doesn't declare; small models hallucinate extra keys.
    allowed = set(tool_.parameters.get("properties", {}))
    clean = {k: v for k, v in args.items() if k in allowed}
    if dropped := set(args) - allowed:
        log.debug("tool %s: dropped unknown args %s", name, sorted(dropped))

    try:
        result = tool_.fn(ctx, **clean)
        if inspect.isawaitable(result):
            result = await result
    except asyncio.TimeoutError:
        log.exception("tool %s timed out", name)
        return ToolResult.fail("That took too long, so I stopped waiting.")
    except Exception as exc:  # noqa: BLE001 - a broken tool must not kill the turn
        log.exception("tool %s raised", name)
        return ToolResult.fail(f"That didn't work: {exc}")

    if not isinstance(result, ToolResult):
        log.error("tool %s returned %r, expected ToolResult", name, type(result))
        return ToolResult.fail("That tool misbehaved.")
    return result

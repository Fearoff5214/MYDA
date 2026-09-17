"""
Execution context handed to every tool invocation.

Tools never import the server. They receive a Context, which is the only
surface through which they can reach the outside world -- speak to the user,
dispatch a command to a node agent, or touch the database. That keeps tools
trivially testable: construct a Context with fakes and call the function.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol


class NodeDispatcher(Protocol):
    """Sends a command to a connected node agent and waits for its result."""

    async def send(self, device: str, action: str, args: dict[str, Any],
                   timeout: float = 20.0) -> dict[str, Any]: ...

    def online(self) -> list[str]:
        """Device ids currently connected."""
        ...

    def resolve(self, hint: str | None) -> str | None:
        """Map a spoken name ("the bedroom laptop") to a connected device id."""
        ...


@dataclass(slots=True)
class Context:
    device: str                                   # device that produced the utterance
    config: dict[str, Any]
    nodes: NodeDispatcher
    db: sqlite3.Connection
    speak: Callable[[str], Awaitable[None]]       # push a spoken line to this device
    announce: Callable[[str, str], Awaitable[None]]  # (device, text) -- for deferred
                                                  # events like timers firing, whose
                                                  # session is long gone
    scheduler: Any = None                         # hub.scheduler.Scheduler
    transcript: str = ""                          # the raw utterance, for logging
    extra: dict[str, Any] = field(default_factory=dict)

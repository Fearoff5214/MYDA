"""
Per-device conversation state.

One session per connected voice client. Holds the short rolling history the
LLM sees, any pending confirmation, and the cancellation handle used for
barge-in (talking over Jarvis stops it mid-sentence).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from .brain.confirm import PendingAction

log = logging.getLogger("jarvis.session")

MAX_HISTORY_TURNS = 6


@dataclass
class Session:
    device: str
    history: list[dict[str, Any]] = field(default_factory=list)
    pending: PendingAction | None = None
    # The whole in-flight turn -- transcribe, think, speak -- as one
    # cancellable task. Barge-in means cancelling this.
    turn: asyncio.Task[Any] | None = None

    def remember(self, user_text: str, reply: str) -> None:
        """Keep a short rolling window. Long history costs latency and, on a
        small model, actively degrades tool-call accuracy."""
        self.history.append({"role": "user", "content": user_text})
        self.history.append({"role": "assistant", "content": reply})
        excess = len(self.history) - MAX_HISTORY_TURNS * 2
        if excess > 0:
            del self.history[:excess]

    def reset(self) -> None:
        self.history.clear()
        self.pending = None

    async def cancel_turn(self) -> None:
        """Barge-in: abandon the in-flight turn for this device."""
        task = self.turn
        self.turn = None
        if task is None:
            return
        if task.done():
            # Consume any exception, or asyncio logs "never retrieved" noise.
            if not task.cancelled() and task.exception() is not None:
                log.warning("turn on %s had already failed: %s",
                            self.device, task.exception())
            return
        task.cancel()
        # gather(return_exceptions=True) collects the child's CancelledError as
        # a result instead of raising it here. A bare `except CancelledError`
        # would also swallow our *own* cancellation during shutdown.
        await asyncio.gather(task, return_exceptions=True)
        log.info("barge-in: abandoned turn on %s", self.device)


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def get(self, device: str) -> Session:
        if device not in self._sessions:
            self._sessions[device] = Session(device=device)
        return self._sessions[device]

    def drop(self, device: str) -> None:
        self._sessions.pop(device, None)

    def all(self) -> list[Session]:
        return list(self._sessions.values())

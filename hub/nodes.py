"""
Live registry of connected node agents.

Nodes dial *out* to the hub and hold the socket open, so no laptop ever needs
an inbound firewall rule and roaming between networks just reconnects. The hub
tracks who is connected and what each can do, and turns a spoken device name
("the bedroom laptop") into a concrete connection.
"""
from __future__ import annotations

import asyncio
import difflib
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("jarvis.nodes")

DEFAULT_TIMEOUT = 20.0


@dataclass
class NodeConn:
    device: str
    ws: Any                              # fastapi.WebSocket
    platform: str = "unknown"
    capabilities: set[str] = field(default_factory=set)
    aliases: list[str] = field(default_factory=list)
    connected_at: float = field(default_factory=time.monotonic)

    @property
    def spoken_names(self) -> list[str]:
        return [self.device, *self.aliases]


class NodeRegistry:
    """Implements the NodeDispatcher protocol that tools depend on."""

    def __init__(self) -> None:
        self._conns: dict[str, NodeConn] = {}
        # req_id -> (device waiting on, future). The device is tracked so a
        # disconnect only fails that machine's requests.
        self._waiting: dict[str, tuple[str, asyncio.Future[dict[str, Any]]]] = {}

    # ---- connection lifecycle -------------------------------------------------

    def add(self, conn: NodeConn) -> None:
        if old := self._conns.get(conn.device):
            log.info("node %s reconnected, dropping stale entry", conn.device)
            del self._conns[old.device]
        self._conns[conn.device] = conn
        log.info("node %s online (%s) caps=%s", conn.device, conn.platform,
                 sorted(conn.capabilities))

    def remove(self, device: str) -> None:
        self._conns.pop(device, None)
        # Fail anything waiting on *this* node rather than hanging. Requests
        # to other machines must be left alone.
        for req_id, (waiting_on, fut) in list(self._waiting.items()):
            if waiting_on != device:
                continue
            if not fut.done():
                fut.set_result({"ok": False, "error": f"{device} disconnected"})
            self._waiting.pop(req_id, None)
        log.info("node %s offline", device)

    def online(self) -> list[str]:
        return sorted(self._conns)

    def describe(self) -> list[dict[str, Any]]:
        return [
            {"device": c.device, "platform": c.platform,
             "capabilities": sorted(c.capabilities), "aliases": c.aliases,
             "uptime_s": round(time.monotonic() - c.connected_at)}
            for c in self._conns.values()
        ]

    # ---- name resolution ------------------------------------------------------

    def resolve(self, hint: str | None) -> str | None:
        """Turn a spoken device name into a connected device id.

        With no hint, resolve to the only connected node if there is exactly
        one -- otherwise return None so the caller can ask which machine.
        """
        if not self._conns:
            return None
        if not hint:
            return next(iter(self._conns)) if len(self._conns) == 1 else None

        needle = hint.lower().strip()
        for filler in ("the ", "my ", "on ", "laptop called "):
            needle = needle.removeprefix(filler)

        candidates: dict[str, str] = {}
        for conn in self._conns.values():
            for name in conn.spoken_names:
                candidates[name.lower()] = conn.device

        if needle in candidates:
            return candidates[needle]
        for name, device in candidates.items():
            if needle in name or name in needle:
                return device
        close = difflib.get_close_matches(needle, list(candidates), n=1, cutoff=0.7)
        return candidates[close[0]] if close else None

    # ---- request/response -----------------------------------------------------

    async def send(self, device: str, action: str, args: dict[str, Any],
                   timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
        conn = self._conns.get(device)
        if conn is None:
            return {"ok": False, "error": f"{device} is not connected"}

        req_id = uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._waiting[req_id] = (device, fut)

        try:
            await conn.ws.send_json({"type": "command", "id": req_id,
                                     "action": action, "args": args})
        except Exception as exc:  # noqa: BLE001
            self._waiting.pop(req_id, None)
            log.warning("send to %s failed: %s", device, exc)
            return {"ok": False, "error": f"lost connection to {device}"}

        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            log.warning("node %s did not answer %s in %.0fs", device, action, timeout)
            return {"ok": False, "error": f"{device} did not respond in time"}
        finally:
            self._waiting.pop(req_id, None)

    def deliver_result(self, msg: dict[str, Any]) -> None:
        """Called by the node WebSocket handler when a result arrives."""
        entry = self._waiting.get(msg.get("id", ""))
        if entry is None:
            log.debug("result for unknown/expired request %s", msg.get("id"))
            return
        _device, fut = entry
        if not fut.done():
            fut.set_result(msg)

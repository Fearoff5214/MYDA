"""
Jarvis node agent.

Runs on every PC you want to control by voice. Dials *out* to the hub and
holds the socket open, so no machine needs an inbound firewall rule and
roaming between networks simply reconnects.

Everything it will do is constrained by node/allowlist.yaml. Anything not
named there is refused, and the refusal is spoken back to you.

    python -m node.agent --hub ws://100.x.y.z:8080 --device laptop-1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import platform
import sys
from pathlib import Path

import websockets
import yaml

from .actions.guard import Refused

HERE = Path(__file__).resolve().parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("jarvis.node")


def load_allowlist(path: Path) -> dict:
    if not path.exists():
        example = path.with_name("allowlist.example.yaml")
        sys.exit(
            f"No allowlist at {path}.\n"
            f"Copy {example.name} to {path.name} and edit it for this machine."
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data.setdefault("apps", {})
    data.setdefault("closable", [])
    data.setdefault("paths", {}).setdefault("readable", [])
    data["paths"].setdefault("writable", [])
    data.setdefault("limits", {})
    data.setdefault("shell", {"enabled": False, "allowed": []})
    return data


class NodeAgent:
    def __init__(self, args: argparse.Namespace, allow: dict) -> None:
        self.args = args
        self.allow = allow
        self.url = (
            f"{args.hub.rstrip('/')}/ws/node"
            f"?device={args.device}&token={args.token}"
        )
        # Imported lazily so a missing optional dependency disables one action
        # group rather than preventing the agent from starting at all.
        from .actions import apps, files, media, window

        self.handlers = {
            "open_app": apps.open_app,
            "close_app": apps.close_app,
            "list_apps": apps.list_apps,
            "list_dir": files.list_dir,
            "find_files": files.find_files,
            "read_file": files.read_file,
            "open_path": files.open_path,
            "delete_files": files.delete_files,
            "volume": media.volume,
            "media_key": media.media_key,
            "lock": window.lock,
            "sleep": window.sleep_machine,
        }

    def capabilities(self) -> list[str]:
        caps = sorted(self.handlers)
        if self.allow["shell"].get("enabled"):
            caps.append("shell")
        return caps

    async def _dispatch(self, action: str, args: dict) -> dict:
        handler = self.handlers.get(action)
        if handler is None:
            return {"ok": False, "error": f"this machine cannot do '{action}'"}
        try:
            data = await asyncio.to_thread(handler, self.allow, args)
            return {"ok": True, "data": data}
        except Refused as exc:
            log.warning("refused %s %s: %s", action, args, exc)
            return {"ok": False, "error": str(exc), "refused": True}
        except Exception as exc:  # noqa: BLE001 - never let one action kill the agent
            log.exception("action %s failed", action)
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    async def _handle(self, ws, msg: dict) -> None:
        req_id = msg.get("id")
        action = msg.get("action", "")
        args = msg.get("args") or {}
        log.info("-> %s %s", action, args or "")
        result = await self._dispatch(action, args)
        log.info("<- %s ok=%s %s", action, result["ok"],
                 result.get("error") or str(result.get("data"))[:80])
        await ws.send(json.dumps({"type": "result", "id": req_id, **result}))

    async def _heartbeat(self, ws) -> None:
        while True:
            await asyncio.sleep(30)
            await ws.send(json.dumps({"type": "ping"}))

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                log.info("connecting to %s", self.args.hub)
                async with websockets.connect(self.url, max_size=4 * 1024 * 1024) as ws:
                    await ws.send(json.dumps({
                        "type": "hello",
                        "platform": f"{platform.system()} {platform.release()}",
                        "capabilities": self.capabilities(),
                        "aliases": self.allow.get("aliases", []),
                    }))
                    ack = json.loads(await ws.recv())
                    if ack.get("type") != "hello_ok":
                        log.error("hub refused the handshake: %s", ack)
                        return
                    backoff = 1.0
                    log.info("registered as %s with %d actions",
                             self.args.device, len(self.handlers))

                    heartbeat = asyncio.create_task(self._heartbeat(ws))
                    try:
                        async for raw in ws:
                            msg = json.loads(raw)
                            if msg.get("type") == "command":
                                # Concurrent so a slow action cannot block the rest.
                                asyncio.create_task(self._handle(ws, msg))
                    finally:
                        heartbeat.cancel()

            except websockets.exceptions.InvalidStatus as exc:
                log.error("hub rejected this device (bad token?): %s", exc)
                await asyncio.sleep(30)
            except (OSError, websockets.exceptions.WebSocketException) as exc:
                log.warning("disconnected (%s); retrying in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            except asyncio.CancelledError:
                raise


def resolve_token(explicit: str | None) -> str:
    if explicit:
        return explicit
    if env := os.environ.get("JARVIS_TOKEN"):
        return env
    token_file = HERE / "token"
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    sys.exit("No token. Pass --token, set JARVIS_TOKEN, or write node/token.")


def main() -> None:
    p = argparse.ArgumentParser(description="Jarvis node agent")
    p.add_argument("--hub", default=os.environ.get("JARVIS_HUB", "ws://127.0.0.1:8080"))
    p.add_argument("--device", default=os.environ.get("JARVIS_DEVICE", platform.node().lower()))
    p.add_argument("--token", default=None)
    p.add_argument("--allowlist", type=Path, default=HERE / "allowlist.yaml")
    args = p.parse_args()
    args.token = resolve_token(args.token)

    allow = load_allowlist(args.allowlist)
    agent = NodeAgent(args, allow)
    log.info("allowlist: %d apps, %d readable paths, %d writable paths, shell=%s",
             len(allow["apps"]), len(allow["paths"]["readable"]),
             len(allow["paths"]["writable"]), allow["shell"].get("enabled", False))
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        log.info("bye")


if __name__ == "__main__":
    main()

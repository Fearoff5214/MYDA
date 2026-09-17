"""
The Jarvis hub.

Two WebSocket endpoints:
  /ws/voice  voice clients: audio in, transcript + spoken reply out
  /ws/node   node agents: hold the socket open, receive commands, return results

Run with:  python -m hub.server        (from the repo root, venv active)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from .auth import Auth
from .brain import tools as _tools  # noqa: F401  -- import registers every tool
from .brain.context import Context
from .brain.dispatch import Brain
from .brain.registry import REGISTRY
from .brain.tools.memory import rearm_reminders
from .db import connect as db_connect
from .nodes import NodeConn, NodeRegistry
from .scheduler import Scheduler
from .session import SessionStore
from .stt import Transcriber
from .tts import Synthesizer

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-16s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("jarvis.server")

# 30 seconds of 16kHz int16 mono. A hard cap so a stuck client cannot
# grow the buffer without bound.
MAX_UTTERANCE_BYTES = 16000 * 2 * 30
MIN_UTTERANCE_BYTES = 3200          # 0.1s -- below this it was a stray keypress
TTS_CHUNK = 4096


def load_config() -> dict[str, Any]:
    """config.yaml, with hub/secrets.yaml merged over the top."""
    cfg = yaml.safe_load((HERE / "config.yaml").read_text(encoding="utf-8"))
    secrets_file = HERE / "secrets.yaml"
    if secrets_file.exists():
        secrets = yaml.safe_load(secrets_file.read_text(encoding="utf-8")) or {}
        for key, value in secrets.items():
            if key == "devices":
                continue            # handled by Auth
            if isinstance(value, dict):
                cfg[key] = {**cfg.get(key, {}), **value}
            else:
                cfg[key] = value
    return cfg


class VoiceGateway:
    """Tracks live voice clients so the hub can also speak unprompted."""

    def __init__(self, tts: Synthesizer) -> None:
        self.tts = tts
        self._conns: dict[str, WebSocket] = {}

    def add(self, device: str, ws: WebSocket) -> None:
        self._conns[device] = ws

    def remove(self, device: str) -> None:
        self._conns.pop(device, None)

    def online(self) -> list[str]:
        return sorted(self._conns)

    async def send_speech(self, device: str, text: str, kind: str = "reply") -> None:
        """Synthesise and stream audio to one device. No-op if it has gone."""
        ws = self._conns.get(device)
        if ws is None or ws.client_state is not WebSocketState.CONNECTED:
            log.info("cannot speak to %s (not connected): %r", device, text[:60])
            return
        try:
            await ws.send_json({"type": kind, "text": text})
            await ws.send_json({
                "type": "audio_start",
                "sample_rate": self.tts.sample_rate,
                "channels": 1,
                "format": "pcm_s16le",
            })
            async for pcm in self.tts.stream(text):
                for i in range(0, len(pcm), TTS_CHUNK):
                    await ws.send_bytes(pcm[i:i + TTS_CHUNK])
            await ws.send_json({"type": "audio_end"})
        except asyncio.CancelledError:
            # Barge-in: tell the client to drop whatever it still has buffered.
            if ws.client_state is WebSocketState.CONNECTED:
                try:
                    await ws.send_json({"type": "audio_cancel"})
                except Exception:  # noqa: BLE001
                    pass
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("speech to %s failed: %s", device, exc)

    async def announce(self, device: str, text: str) -> None:
        """Deferred speech, e.g. a timer firing.

        If the device that set the timer has gone, say it on whatever is
        listening -- silently swallowing a fired timer is worse than
        announcing it in the wrong room.
        """
        target = device if device in self._conns else None
        if target is None and self._conns:
            target = next(iter(self._conns))
            log.info("%s has gone; announcing on %s instead", device, target)
        if target is None:
            log.warning("nobody is listening; dropped announcement: %r", text)
            return
        await self.send_speech(target, text, kind="announce")


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config()
    app.state.cfg = cfg

    app.state.auth = Auth(HERE / "secrets.yaml")
    app.state.db = db_connect(HERE / "db.sqlite")
    app.state.nodes = NodeRegistry()
    app.state.sessions = SessionStore()

    # Loading Whisper and Piper together takes tens of seconds. Do it in
    # threads so startup does not block the event loop.
    stt = Transcriber(cfg["stt"])
    tts = Synthesizer(cfg["tts"])
    onnx, conf = await tts.ensure_voice()
    await asyncio.gather(
        asyncio.to_thread(stt.load),
        asyncio.to_thread(tts.load, onnx, conf),
    )
    await stt.warmup()
    app.state.stt = stt
    app.state.tts = tts

    app.state.voice = VoiceGateway(tts)
    app.state.scheduler = Scheduler(app.state.voice.announce)
    app.state.scheduler.start()

    # Reminders outlive a restart; timers deliberately do not.
    rearm_reminders(app.state.db, app.state.scheduler)

    app.state.brain = Brain(cfg, ROOT / cfg["security"]["audit_log"])
    if await app.state.brain.agent.health():
        await app.state.brain.agent.preload()
    else:
        log.error("Ollama is not ready -- Tier 2 will fail until it is.")

    log.info(
        "hub ready: %d tools, listening on %s:%s",
        len(REGISTRY), cfg["server"]["host"], cfg["server"]["port"],
    )
    try:
        yield
    finally:
        app.state.scheduler.shutdown()
        await app.state.brain.aclose()
        app.state.db.close()
        log.info("hub stopped")


app = FastAPI(title="Jarvis hub", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "ok": True,
        "tools": sorted(REGISTRY),
        "nodes": app.state.nodes.describe(),
        "voice_clients": app.state.voice.online(),
        "timers": len(app.state.scheduler.pending("timer")),
    }


def _make_context(device: str) -> Context:
    async def speak(text: str) -> None:
        await app.state.voice.send_speech(device, text)

    return Context(
        device=device,
        config=app.state.cfg,
        nodes=app.state.nodes,
        db=app.state.db,
        speak=speak,
        announce=app.state.voice.announce,
        scheduler=app.state.scheduler,
    )


async def _handle_turn(ws: WebSocket, device: str, transcript: str, t_start: float) -> None:
    session = app.state.sessions.get(device)
    ctx = _make_context(device)
    ctx.transcript = transcript

    await ws.send_json({"type": "transcript", "text": transcript})
    reply = await app.state.brain.handle(ctx, transcript, session)

    # Speaking is its own task so the next utterance can cancel it (barge-in).
    session.speaking = asyncio.create_task(
        app.state.voice.send_speech(device, reply)
    )
    try:
        await session.speaking
    except asyncio.CancelledError:
        log.info("speech cancelled on %s", device)
    finally:
        session.speaking = None

    ms = round((time.perf_counter() - t_start) * 1000)
    log.info("turn complete on %s in %dms", device, ms)
    if ws.client_state is WebSocketState.CONNECTED:
        await ws.send_json({"type": "turn_end", "latency_ms": ms})


@app.websocket("/ws/voice")
async def voice_endpoint(
    ws: WebSocket,
    device: str = Query(...),
    token: str = Query(...),
) -> None:
    if not app.state.auth.check(device, token):
        await ws.close(code=1008, reason="unauthorised")
        return
    await ws.accept()
    app.state.voice.add(device, ws)
    session = app.state.sessions.get(device)
    log.info("voice client %s connected", device)

    buf = bytearray()
    capturing = False
    truncated = False
    t_start = 0.0

    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break

            payload = msg.get("bytes")
            if payload is not None:
                if not capturing:
                    continue
                if len(buf) + len(payload) > MAX_UTTERANCE_BYTES:
                    truncated = True
                else:
                    buf.extend(payload)
                continue

            raw = msg.get("text")
            if raw is None:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("non-JSON text frame from %s", device)
                continue

            kind = event.get("type")

            if kind == "utterance_start":
                await session.stop_speaking()      # barge-in
                buf.clear()
                capturing = True
                truncated = False
                t_start = time.perf_counter()

            elif kind == "utterance_end":
                capturing = False
                if truncated:
                    log.warning("utterance from %s hit the 30s cap", device)
                if len(buf) < MIN_UTTERANCE_BYTES:
                    await ws.send_json({"type": "turn_end", "latency_ms": 0,
                                        "note": "too short"})
                    buf.clear()
                    continue
                audio = app.state.stt.pcm16_to_float32(bytes(buf))
                buf.clear()
                transcript = await app.state.stt.transcribe(audio)
                if not transcript:
                    await ws.send_json({"type": "no_speech"})
                    continue
                await _handle_turn(ws, device, transcript, t_start)

            elif kind == "text":
                # Typed input. Lets the entire brain be tested without a mic.
                text = (event.get("text") or "").strip()
                if text:
                    await session.stop_speaking()
                    await _handle_turn(ws, device, text, time.perf_counter())

            elif kind == "cancel":
                await session.stop_speaking()

            elif kind == "reset":
                session.reset()
                await ws.send_json({"type": "reset_ok"})

            elif kind == "ping":
                await ws.send_json({"type": "pong"})

    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("voice session %s died", device)
    finally:
        app.state.voice.remove(device)
        await session.stop_speaking()
        log.info("voice client %s disconnected", device)


@app.websocket("/ws/node")
async def node_endpoint(
    ws: WebSocket,
    device: str = Query(...),
    token: str = Query(...),
) -> None:
    if not app.state.auth.check(device, token):
        await ws.close(code=1008, reason="unauthorised")
        return
    await ws.accept()

    try:
        hello = await asyncio.wait_for(ws.receive_json(), timeout=10.0)
    except asyncio.TimeoutError:
        await ws.close(code=1002, reason="expected hello")
        return
    except Exception:
        await ws.close(code=1002, reason="bad hello")
        return

    conn = NodeConn(
        device=device,
        ws=ws,
        platform=hello.get("platform", "unknown"),
        capabilities=set(hello.get("capabilities", [])),
        aliases=hello.get("aliases") or app.state.auth.aliases(device),
    )
    app.state.nodes.add(conn)
    await ws.send_json({"type": "hello_ok"})

    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "result":
                app.state.nodes.deliver_result(msg)
            elif msg.get("type") == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("node session %s died", device)
    finally:
        app.state.nodes.remove(device)


def main() -> None:
    import uvicorn

    cfg = load_config()["server"]
    uvicorn.run(app, host=cfg["host"], port=cfg["port"], log_level="warning")


if __name__ == "__main__":
    main()

"""
Jarvis voice client.

Runs on the desktop and both laptops. Captures microphone audio, ships it to
the hub over a WebSocket, and plays back the spoken reply.

Two ways to start talking:
  * push-to-talk -- hold a hotkey (default ctrl+alt+space). Always available.
  * wake word    -- say "hey jarvis". Enabled with --wake (Phase 3).

Usage:
    python -m client.listener --hub ws://100.x.y.z:8080 --device laptop-1
The token is read from client/token, or --token, or JARVIS_TOKEN.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import queue
import sys
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
import websockets

HERE = Path(__file__).resolve().parent

SAMPLE_RATE = 16000        # what the hub and Whisper expect
BLOCK = 480                # 30ms at 16kHz
CHANNELS = 1

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("jarvis.client")


class Player:
    """Plays PCM pushed from the network, and can be flushed for barge-in."""

    def __init__(self) -> None:
        self._q: queue.Queue[bytes | None] = queue.Queue()
        self._stream: sd.RawOutputStream | None = None
        self._rate = 22050
        self._lock = threading.Lock()

    def start(self, sample_rate: int) -> None:
        with self._lock:
            if self._stream is not None and self._rate == sample_rate:
                return
            self.stop()
            self._rate = sample_rate
            self._stream = sd.RawOutputStream(
                samplerate=sample_rate, channels=1, dtype="int16", blocksize=1024,
            )
            self._stream.start()
            threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        stream = self._stream
        while stream is not None and not stream.closed:
            try:
                chunk = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if chunk is None:
                break
            try:
                stream.write(chunk)
            except Exception:  # noqa: BLE001 - stream torn down mid-write
                break

    def feed(self, pcm: bytes) -> None:
        self._q.put(pcm)

    def flush(self) -> None:
        """Barge-in: drop everything queued and silence the device now."""
        drained = 0
        while True:
            try:
                self._q.get_nowait()
                drained += 1
            except queue.Empty:
                break
        with self._lock:
            if self._stream is not None:
                try:
                    self._stream.abort()
                    self._stream.start()
                except Exception:  # noqa: BLE001
                    pass
        if drained:
            log.debug("flushed %d queued audio chunks", drained)

    def stop(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                pass


class Microphone:
    """Continuous capture into an asyncio queue, so nothing is missed between
    the wake word firing and the stream being opened."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.frames: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
        self._stream: sd.RawInputStream | None = None

    def _callback(self, indata, _frames, _time, status) -> None:
        if status:
            log.debug("input status: %s", status)
        data = bytes(indata)
        try:
            self.loop.call_soon_threadsafe(self.frames.put_nowait, data)
        except RuntimeError:
            pass  # loop closing
        except asyncio.QueueFull:
            pass  # we are behind; dropping the oldest audio is correct here

    def start(self) -> None:
        self._stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16",
            blocksize=BLOCK, callback=self._callback,
        )
        self._stream.start()
        log.info("microphone open at %d Hz", SAMPLE_RATE)

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def drain(self) -> None:
        while not self.frames.empty():
            try:
                self.frames.get_nowait()
            except asyncio.QueueEmpty:
                break


class WakeWord:
    """openWakeWord wrapper. Optional -- absent unless --wake is passed."""

    def __init__(self, model: str, threshold: float) -> None:
        from openwakeword.model import Model

        self.threshold = threshold
        self.model = Model(wakeword_models=[model], inference_framework="onnx")
        self.name = model
        self._last_fire = 0.0
        log.info("wake word %r armed at threshold %.2f", model, threshold)

    def feed(self, pcm: bytes) -> bool:
        samples = np.frombuffer(pcm, dtype=np.int16)
        scores = self.model.predict(samples)
        score = max(scores.values()) if scores else 0.0
        if score < self.threshold:
            return False
        # Debounce: one utterance can cross the threshold on several frames.
        now = time.monotonic()
        if now - self._last_fire < 2.0:
            return False
        self._last_fire = now
        self.model.reset()
        log.info("wake word fired (%.2f)", score)
        return True


class Client:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.url = (
            f"{args.hub.rstrip('/')}/ws/voice"
            f"?device={args.device}&token={args.token}"
        )
        self.player = Player()
        self.talking = asyncio.Event()      # set while capturing an utterance
        self.ws = None
        self.mic: Microphone | None = None
        self.wake: WakeWord | None = None

    # ---- hotkey ---------------------------------------------------------------

    def _install_hotkey(self, loop: asyncio.AbstractEventLoop) -> None:
        """Hold-to-talk. pynput needs no admin rights, unlike the keyboard lib."""
        from pynput import keyboard

        combo = set(self.args.hotkey.split("+"))
        name_map = {
            "ctrl": keyboard.Key.ctrl, "alt": keyboard.Key.alt,
            "shift": keyboard.Key.shift, "space": keyboard.Key.space,
            "cmd": keyboard.Key.cmd,
        }
        wanted = {name_map.get(part, part) for part in combo}
        held: set = set()

        def canon(key):
            for base, variants in (
                (keyboard.Key.ctrl, (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r)),
                (keyboard.Key.alt, (keyboard.Key.alt_l, keyboard.Key.alt_r, keyboard.Key.alt_gr)),
                (keyboard.Key.shift, (keyboard.Key.shift_l, keyboard.Key.shift_r)),
            ):
                if key in variants:
                    return base
            return key

        def on_press(key):
            held.add(canon(key))
            if wanted <= held and not self.talking.is_set():
                loop.call_soon_threadsafe(self.talking.set)

        def on_release(key):
            held.discard(canon(key))
            if self.talking.is_set() and not wanted <= held:
                loop.call_soon_threadsafe(self.talking.clear)

        keyboard.Listener(on_press=on_press, on_release=on_release, daemon=True).start()
        log.info("push-to-talk: hold %s", self.args.hotkey)

    # ---- network --------------------------------------------------------------

    async def _receive_loop(self) -> None:
        async for message in self.ws:
            if isinstance(message, bytes):
                self.player.feed(message)
                continue
            try:
                event = json.loads(message)
            except json.JSONDecodeError:
                continue

            kind = event.get("type")
            if kind == "transcript":
                log.info("you said: %s", event["text"])
            elif kind in ("reply", "announce"):
                marker = "jarvis" if kind == "reply" else "jarvis (timer)"
                log.info("%s: %s", marker, event["text"])
            elif kind == "audio_start":
                self.player.start(event.get("sample_rate", 22050))
            elif kind == "audio_cancel":
                self.player.flush()
            elif kind == "no_speech":
                log.info("(heard nothing)")
            elif kind == "turn_end":
                log.info("round trip %d ms", event.get("latency_ms", 0))

    async def _capture_loop(self) -> None:
        """One iteration per utterance."""
        assert self.mic is not None
        capturing = False

        while True:
            frame = await self.mic.frames.get()

            if not capturing:
                triggered = self.talking.is_set()
                if not triggered and self.wake is not None:
                    triggered = self.wake.feed(frame)
                    if triggered:
                        self.talking.set()
                if not triggered:
                    continue

                self.player.flush()                      # barge-in
                await self.ws.send(json.dumps({"type": "cancel"}))
                await self.ws.send(json.dumps(
                    {"type": "utterance_start", "sample_rate": SAMPLE_RATE}))
                capturing = True
                log.info("listening...")

            await self.ws.send(frame)

            # Push-to-talk ends on key release. Wake-word mode ends after a
            # fixed window in Phase 1; Phase 3 replaces this with VAD.
            if self.wake is not None and not self.args.hold_to_talk:
                if not hasattr(self, "_wake_started"):
                    self._wake_started = time.monotonic()
                if time.monotonic() - self._wake_started > self.args.wake_window:
                    del self._wake_started
                    self.talking.clear()

            if not self.talking.is_set():
                await self.ws.send(json.dumps({"type": "utterance_end"}))
                capturing = False
                self.mic.drain()
                log.info("thinking...")

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        self.mic = Microphone(loop)
        self.mic.start()
        self._install_hotkey(loop)

        if self.args.wake:
            try:
                self.wake = WakeWord(self.args.wake_model, self.args.wake_threshold)
            except Exception as exc:  # noqa: BLE001
                log.error("wake word unavailable (%s); push-to-talk still works", exc)

        backoff = 1.0
        while True:
            try:
                log.info("connecting to %s", self.args.hub)
                async with websockets.connect(self.url, max_size=8 * 1024 * 1024) as ws:
                    self.ws = ws
                    backoff = 1.0
                    log.info("connected as %s -- ready", self.args.device)
                    await asyncio.gather(self._receive_loop(), self._capture_loop())
            except (OSError, websockets.exceptions.WebSocketException) as exc:
                log.warning("connection lost (%s); retrying in %.0fs", exc, backoff)
            except asyncio.CancelledError:
                raise
            finally:
                self.ws = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


def resolve_token(args: argparse.Namespace) -> str:
    if args.token:
        return args.token
    if env := os.environ.get("JARVIS_TOKEN"):
        return env
    token_file = HERE / "token"
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    sys.exit("No token. Pass --token, set JARVIS_TOKEN, or write client/token.")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Jarvis voice client")
    p.add_argument("--hub", default=os.environ.get("JARVIS_HUB", "ws://127.0.0.1:8080"))
    p.add_argument("--device", default=os.environ.get("JARVIS_DEVICE", "desktop"))
    p.add_argument("--token", default=None)
    p.add_argument("--hotkey", default="ctrl+alt+space")
    p.add_argument("--hold-to-talk", action="store_true", default=True)
    p.add_argument("--wake", action="store_true", help="enable the wake word")
    p.add_argument("--wake-model", default="hey_jarvis")
    p.add_argument("--wake-threshold", type=float, default=0.6)
    p.add_argument("--wake-window", type=float, default=6.0,
                   help="seconds to record after the wake word (Phase 1; VAD in Phase 3)")
    p.add_argument("--list-devices", action="store_true")
    args = p.parse_args()
    if args.list_devices:
        print(sd.query_devices())
        sys.exit(0)
    args.token = resolve_token(args)
    return args


def main() -> None:
    args = parse_args()
    client = Client(args)
    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        log.info("bye")
    finally:
        client.player.stop()
        if client.mic is not None:
            client.mic.stop()


if __name__ == "__main__":
    main()

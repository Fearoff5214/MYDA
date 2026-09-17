"""
Talk to the hub by typing instead of speaking.

The whole brain -- routing, tools, confirmation, TTS -- runs exactly as it does
for real speech; only the microphone is bypassed. This is the fastest way to
test tools, and it is how you check the hub works before any audio hardware is
involved.

    python scripts/say.py                      # interactive
    python scripts/say.py "set a timer for 10 seconds"
    python scripts/say.py --save reply.wav "what timers are running"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import wave
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent


def resolve_token(explicit: str | None) -> str:
    if explicit:
        return explicit
    if env := os.environ.get("JARVIS_TOKEN"):
        return env
    for candidate in (ROOT / "client" / "token", ROOT / "scripts" / "token"):
        if candidate.exists():
            return candidate.read_text(encoding="utf-8").strip()
    sys.exit("No token. Pass --token, set JARVIS_TOKEN, or write client/token.")


async def one_turn(ws, text: str, save: Path | None) -> None:
    started = time.perf_counter()
    await ws.send(json.dumps({"type": "text", "text": text}))

    audio = bytearray()
    rate = 22050
    first_audio: float | None = None

    while True:
        message = await ws.recv()
        if isinstance(message, bytes):
            if first_audio is None:
                first_audio = time.perf_counter() - started
            audio.extend(message)
            continue

        event = json.loads(message)
        kind = event.get("type")
        if kind == "transcript":
            pass                      # we typed it; no need to echo
        elif kind in ("reply", "announce"):
            print(f"  jarvis> {event['text']}")
        elif kind == "audio_start":
            rate = event.get("sample_rate", 22050)
        elif kind == "turn_end":
            total = event.get("latency_ms", 0)
            extra = f", first audio at {first_audio * 1000:.0f}ms" if first_audio else ""
            print(f"  [{total}ms{extra}, {len(audio) / 2 / rate:.1f}s of speech]")
            break
        elif kind == "no_speech":
            print("  [hub heard nothing]")
            break

    if save and audio:
        with wave.open(str(save), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(bytes(audio))
        print(f"  [wrote {save}]")


async def run(args: argparse.Namespace) -> None:
    url = f"{args.hub.rstrip('/')}/ws/voice?device={args.device}&token={args.token}"
    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        print(f"connected to {args.hub} as {args.device}")

        if args.text:
            await one_turn(ws, " ".join(args.text), args.save)
            if args.wait:
                print(f"waiting {args.wait}s for deferred announcements...")
                try:
                    await asyncio.wait_for(_drain(ws), timeout=args.wait)
                except asyncio.TimeoutError:
                    pass
            return

        print("type a command, or 'quit'")
        loop = asyncio.get_running_loop()
        while True:
            line = await loop.run_in_executor(None, lambda: input("you> ").strip())
            if line.lower() in ("quit", "exit", ""):
                break
            await one_turn(ws, line, args.save)


async def _drain(ws) -> None:
    """Keep printing anything the hub pushes, e.g. a timer firing."""
    while True:
        message = await ws.recv()
        if isinstance(message, bytes):
            continue
        event = json.loads(message)
        if event.get("type") == "announce":
            print(f"  jarvis> {event['text']}")


def main() -> None:
    p = argparse.ArgumentParser(description="Type at the Jarvis hub")
    p.add_argument("text", nargs="*", help="one-shot command; omit for interactive")
    p.add_argument("--hub", default=os.environ.get("JARVIS_HUB", "ws://127.0.0.1:8080"))
    p.add_argument("--device", default=os.environ.get("JARVIS_DEVICE", "desktop"))
    p.add_argument("--token", default=None)
    p.add_argument("--save", type=Path, default=None, help="write the spoken reply to a wav")
    p.add_argument("--wait", type=float, default=0,
                   help="after the reply, keep listening N seconds for timers to fire")
    args = p.parse_args()
    args.token = resolve_token(args.token)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        sys.exit(f"could not reach the hub at {args.hub}: {exc}")


if __name__ == "__main__":
    main()

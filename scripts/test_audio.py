"""
Audio round-trip test: real PCM in, transcript and spoken reply out.

Everything else so far has injected typed text, which skips the half of the
pipeline most likely to be wrong -- framing, buffering, resampling, and
Whisper itself. This synthesises speech locally, streams it to the hub as
16 kHz PCM exactly the way client/listener.py does, and checks what comes back.

What this does NOT prove: real microphone acoustics. Synthesised speech is
clean, close-miked and has no room or background noise, so a pass here means
the plumbing is right, not that recognition will hold up across the room.

    python scripts/test_audio.py                 # needs the hub running
    python scripts/test_audio.py --keep out.wav  # save what was sent
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import wave
from pathlib import Path

import numpy as np
import websockets

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hub import sapi

TARGET_RATE = 16000     # what the hub and Whisper expect
FRAME = 480 * 2         # 30ms of int16, matching the real client

# Transcription need not be exact -- Whisper punctuates and capitalises freely.
# Each case lists words that must appear, and the tool the brain must reach.
CASES = [
    ("Set a timer for five minutes.", ["timer", "five"], "set_timer"),
    ("What timers are running?", ["timer"], "list_timers"),
    ("Open notepad.", ["open", "notepad"], "open_app"),
    ("Cancel the timers.", ["cancel", "timer"], "cancel_timers"),
]


def resample(pcm: bytes, src: int, dst: int) -> bytes:
    """Linear resample int16 mono. Good enough for speech at these rates."""
    if src == dst:
        return pcm
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    n_out = int(len(samples) * dst / src)
    resampled = np.interp(
        np.linspace(0, len(samples) - 1, n_out, dtype=np.float64),
        np.arange(len(samples), dtype=np.float64),
        samples,
    )
    return resampled.astype(np.int16).tobytes()


def speak_to_pcm(text: str) -> bytes:
    pcm = sapi.synthesize(text)
    if not pcm:
        sys.exit("Windows speech produced nothing; cannot build the test audio.")
    return resample(pcm, sapi.SAMPLE_RATE, TARGET_RATE)


def resolve_token(explicit: str | None) -> str:
    if explicit:
        return explicit
    if env := os.environ.get("JARVIS_TOKEN"):
        return env
    token_file = ROOT / "client" / "token"
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    sys.exit("No token. Run: python scripts/new_device.py desktop")


async def send_utterance(ws, pcm: bytes) -> dict:
    """Stream PCM the way the real client does, then collect the reply."""
    await ws.send(json.dumps({"type": "utterance_start", "sample_rate": TARGET_RATE}))
    for i in range(0, len(pcm), FRAME):
        await ws.send(pcm[i:i + FRAME])
    await ws.send(json.dumps({"type": "utterance_end"}))

    out = {"transcript": "", "reply": "", "audio_bytes": 0, "ms": 0, "brain_ms": 0}
    while True:
        message = await ws.recv()
        if isinstance(message, bytes):
            out["audio_bytes"] += len(message)
            continue
        event = json.loads(message)
        kind = event.get("type")
        if kind == "transcript":
            out["transcript"] = event["text"]
        elif kind in ("reply", "announce"):
            out["reply"] = event["text"]
        elif kind == "no_speech":
            return out
        elif kind == "turn_end":
            out["ms"] = event.get("latency_ms", 0)
            out["brain_ms"] = event.get("brain_ms", 0)
            return out


async def run(args: argparse.Namespace) -> int:
    url = f"{args.hub.rstrip('/')}/ws/voice?device={args.device}&token={args.token}"
    failures = 0

    print(f"synthesising test speech via Windows SAPI @ {sapi.SAMPLE_RATE} Hz "
          f"-> {TARGET_RATE} Hz\n")

    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        for spoken, must_contain, expect_tool in CASES:
            pcm = speak_to_pcm(spoken)
            secs = len(pcm) / 2 / TARGET_RATE
            result = await send_utterance(ws, pcm)

            heard = result["transcript"].lower()
            got_words = [w for w in must_contain if w in heard]
            ok = len(got_words) == len(must_contain) and bool(result["reply"])
            failures += not ok

            print(f"  [{'ok  ' if ok else 'FAIL'}] said {spoken!r} ({secs:.1f}s)")
            print(f"         heard   {result['transcript']!r}")
            print(f"         replied {result['reply'][:62]!r}")
            print(f"         {result['ms']}ms total, {result['brain_ms']}ms brain, "
                  f"{result['audio_bytes'] / 2 / 22050:.1f}s spoken back")
            if not ok:
                missing = [w for w in must_contain if w not in heard]
                print(f"         missing from transcript: {missing}")

            if args.keep:
                path = Path(args.keep)
                with wave.open(str(path), "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(TARGET_RATE)
                    wf.writeframes(pcm)
                print(f"         wrote {path}")

    print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
    if failures:
        print("Note: synthesised speech is unusually clean. A failure here means "
              "the pipeline is wrong, not that the model is weak.")
    return 1 if failures else 0


def main() -> None:
    p = argparse.ArgumentParser(description="Jarvis audio round-trip test")
    p.add_argument("--hub", default=os.environ.get("JARVIS_HUB", "ws://127.0.0.1:8080"))
    p.add_argument("--device", default=os.environ.get("JARVIS_DEVICE", "desktop"))
    p.add_argument("--token", default=None)
    p.add_argument("--keep", default=None, help="save the last utterance as a wav")
    args = p.parse_args()
    args.token = resolve_token(args.token)
    try:
        sys.exit(asyncio.run(run(args)))
    except OSError as exc:
        sys.exit(f"could not reach the hub at {args.hub}: {exc}")


if __name__ == "__main__":
    main()

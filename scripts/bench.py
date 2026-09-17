"""
Latency board.

Replays a fixed set of utterances through the real hub -- real router, real
model, real tools -- and reports p50/p95 per tier. This is the regression gate:
the whole design rests on Tier 1 being fast, so if that number drifts, the
premise has broken.

Tier 1 and Tier 2 are reported separately because mixing them produces a
meaningless average.

    python scripts/bench.py                 # everything
    python scripts/bench.py --tier 1        # fast path only
    python scripts/bench.py --repeat 3
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Utterances that must hit the deterministic fast path.
TIER1 = [
    "set a timer for 5 minutes",
    "what timers are running",
    "cancel the timers",
    "lock the screen",
    "what computers are online",
    "turn it up",
    "mute",
    "volume 40",
    "pause",
    "skip",
]

# Utterances that must go to the LLM.
TIER2 = [
    "what's the capital of Australia",
    "how many grams are in an ounce",
    "add milk to the shopping list",
    "what's on the shopping list",
    "make a note that the boiler needs servicing",
    "read me my notes",
    "how long does it take to boil an egg",
    "what's in my downloads folder",
    "remind me to call the landlord at six tonight",
    "tell me a very short joke",
]


def resolve_token(explicit: str | None) -> str:
    if explicit:
        return explicit
    if env := os.environ.get("JARVIS_TOKEN"):
        return env
    for candidate in (ROOT / "client" / "token", ROOT / "scripts" / "token"):
        if candidate.exists():
            return candidate.read_text(encoding="utf-8").strip()
    sys.exit("No token. Pass --token, set JARVIS_TOKEN, or write client/token.")


async def one(ws, text: str) -> dict:
    """Run a single turn and record where the time went."""
    started = time.perf_counter()
    await ws.send(json.dumps({"type": "text", "text": text}))

    first_audio: float | None = None
    reply = ""
    audio_bytes = 0
    total_ms = 0
    brain_ms = 0

    while True:
        message = await ws.recv()
        if isinstance(message, bytes):
            if first_audio is None:
                first_audio = (time.perf_counter() - started) * 1000
            audio_bytes += len(message)
            continue
        event = json.loads(message)
        kind = event.get("type")
        if kind in ("reply", "announce"):
            reply = event["text"]
        elif kind == "turn_end":
            total_ms = event.get("latency_ms", 0)
            brain_ms = event.get("brain_ms", 0)
            break
        elif kind == "no_speech":
            break

    return {
        "text": text,
        "reply": reply,
        "total_ms": total_ms or round((time.perf_counter() - started) * 1000),
        "brain_ms": brain_ms,
        "first_audio_ms": round(first_audio) if first_audio else None,
        "audio_s": round(audio_bytes / 2 / 22050, 1),
    }


def report(label: str, rows: list[dict], budget_ms: int) -> bool:
    if not rows:
        return True
    totals = sorted(r["total_ms"] for r in rows)
    brains = sorted(r["brain_ms"] for r in rows if r["brain_ms"])
    firsts = [r["first_audio_ms"] for r in rows if r["first_audio_ms"]]
    p50 = statistics.median(totals)
    p95 = totals[min(len(totals) - 1, int(len(totals) * 0.95))]

    print(f"\n{label}  ({len(rows)} turns)")
    print(f"  total      p50 {p50:>6.0f} ms   p95 {p95:>6.0f} ms   max {totals[-1]:>6.0f} ms")
    if brains:
        b50 = statistics.median(brains)
        print(f"  brain      p50 {b50:>6.0f} ms   routing + tool, excluding speech")
        print(f"  speech     p50 {p50 - b50:>6.0f} ms   Piper is ~10x faster than the SAPI fallback")
    if firsts:
        print(f"  to speech  p50 {statistics.median(firsts):>6.0f} ms")

    # The gate is brain time. How fast speech synthesises depends on which
    # backend the machine will actually run, which is not a property of the
    # design -- a machine forced onto the SAPI fallback should not read as a
    # regression in the router.
    gate = statistics.median(brains) if brains else p50
    print(f"  budget     {budget_ms} ms on brain  ->  "
          f"{'PASS' if gate <= budget_ms else 'OVER BUDGET'}")

    slowest = sorted(rows, key=lambda r: -r["total_ms"])[:3]
    for r in slowest:
        print(f"    {r['total_ms']:>6} ms total / {r['brain_ms']:>4} ms brain  "
              f"{r['text'][:38]:<38} {r['reply'][:30]}")
    return gate <= budget_ms


async def run(args: argparse.Namespace) -> int:
    url = f"{args.hub.rstrip('/')}/ws/voice?device={args.device}&token={args.token}"

    sets = []
    if args.tier in (0, 1):
        sets.append(("TIER 1  deterministic fast path", TIER1, args.budget1))
    if args.tier in (0, 2):
        sets.append(("TIER 2  local LLM tool loop", TIER2, args.budget2))

    ok = True
    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        print(f"connected to {args.hub} as {args.device}")
        for label, utterances, budget in sets:
            rows = []
            for _ in range(args.repeat):
                for text in utterances:
                    rows.append(await one(ws, text))
                    print(".", end="", flush=True)
            print()
            ok &= report(label, rows, budget)

        # Leave no timers behind from the benchmark itself.
        await one(ws, "cancel the timers")

    print("\n" + ("all tiers within budget" if ok else "at least one tier is over budget"))
    return 0 if ok else 1


def main() -> None:
    p = argparse.ArgumentParser(description="Jarvis latency board")
    p.add_argument("--hub", default=os.environ.get("JARVIS_HUB", "ws://127.0.0.1:8080"))
    p.add_argument("--device", default=os.environ.get("JARVIS_DEVICE", "desktop"))
    p.add_argument("--token", default=None)
    p.add_argument("--tier", type=int, choices=[0, 1, 2], default=0,
                   help="0 = both (default)")
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--budget1", type=int, default=500, help="Tier 1 p50 budget in ms")
    p.add_argument("--budget2", type=int, default=4000, help="Tier 2 p50 budget in ms")
    args = p.parse_args()
    args.token = resolve_token(args.token)
    try:
        sys.exit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        sys.exit(130)
    except OSError as exc:
        sys.exit(f"could not reach the hub at {args.hub}: {exc}")


if __name__ == "__main__":
    main()

"""
Compare local models on the only thing that matters here: picking the right
tool, with the right arguments, quickly.

General LLM leaderboards are close to useless for this. What decides whether
Jarvis feels good is narrow: given the real tool catalogue and a spoken
sentence, does the model call the correct function? A model that writes
beautiful prose but answers "Here are your notes:" without calling read_notes
is worse than useless -- it lies confidently.

So this scores against the actual registry, with the actual system prompt.

    python scripts/compare_models.py                       # every pulled model
    python scripts/compare_models.py qwen2.5:3b llama3.2:3b
    python scripts/compare_models.py --repeat 3            # catch flakiness

Needs Ollama running. Pull candidates first with `ollama pull <name>`.
Each model is loaded in turn, so allow a minute per model for cold start.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
import sys
import time
import types
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.disable(logging.INFO)

from hub.brain.agent import SYSTEM_PROMPT
from hub.brain.registry import catalogue
from hub.brain.tools import home  # noqa: F401 -- registers tools

# (utterance, expected tool or None for "answer directly", required args)
# Deliberately the hard cases: the ones Tier 1 does NOT cover, because those
# are the only ones a model is responsible for.
CASES: list[tuple[str, str | None, dict]] = [
    ("make a note that the boiler needs servicing", "add_note", {}),
    ("remind me to call the landlord at six tonight", "set_reminder", {}),
    ("what's the weather in Bangalore", "web_search", {}),
    ("how many grams are in an ounce", None, {}),
    ("what is the capital of Australia", None, {}),
    ("turn the kitchen plug off", "control_device", {"state": "off"}),
    ("set the bedroom lights to forty percent", "set_brightness", {"percent": 40}),
    ("is the coffee machine on", "device_state", {}),
    ("what smart devices do I have", "list_smart_devices", {}),
    ("search for the train times to Mysore", "web_search", {}),
]


def _arg_matches(got: object, want: object) -> bool:
    """Compare the way the tools do, not the way Python does.

    Models often return "40" where the schema says integer. Every tool here
    coerces with int()/float() inside a try, so a quoted number works
    perfectly at runtime -- counting it as a failure would measure JSON
    formatting rather than whether the model understood the request.
    """
    if type(got) is type(want) and got == want:
        return True
    # bool is a subclass of int in Python, so True == 1. A model answering
    # `true` where a number was asked for is a genuine mistake, not a
    # formatting quirk, so neither side may be a bool here.
    if isinstance(want, (int, float)) and not isinstance(want, bool) \
            and not isinstance(got, bool):
        try:
            return float(got) == float(want)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
    if isinstance(want, str) and isinstance(got, str):
        return got.strip().lower() == want.strip().lower()
    return False


def build_ctx() -> types.SimpleNamespace:
    """A context where every tool is 'available', so the catalogue is full."""
    nodes = types.SimpleNamespace(
        online=lambda: ["desktop"],
        describe=lambda: [{"device": "desktop", "capabilities": ["open_app", "send_sms"],
                           "aliases": [], "platform": "test", "uptime_s": 1}],
        resolve=lambda hint: "desktop",
    )
    return types.SimpleNamespace(
        device="desktop", nodes=nodes, db=None, scheduler=None, extra={},
        transcript="",
        config={"home_assistant": {"base_url": "http://127.0.0.1:8123", "token": "x"},
                "google": {"refresh_token": "x"}},
    )


async def installed(client: httpx.AsyncClient) -> list[str]:
    resp = await client.get("/api/tags", timeout=10)
    resp.raise_for_status()
    return sorted(m.get("name") or m.get("model", "") for m in resp.json().get("models", []))


async def ask(client: httpx.AsyncClient, model: str, text: str,
              tools: list[dict]) -> tuple[str | None, dict, float, int, str]:
    """Returns (tool_called, args, seconds, generated_tokens, content)."""
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": text}],
        "tools": tools,
        "stream": False,
        "keep_alive": "10m",
        "options": {"temperature": 0.3, "num_ctx": 8192},
    }
    started = time.perf_counter()
    try:
        resp = await client.post("/api/chat", json=payload, timeout=300)
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:  # noqa: BLE001
        return "ERROR", {}, time.perf_counter() - started, 0, str(exc)[:60]

    elapsed = time.perf_counter() - started
    msg = body.get("message", {})
    calls = msg.get("tool_calls") or []
    tokens = body.get("eval_count", 0)
    content = (msg.get("content") or "").strip()

    if not calls:
        return None, {}, elapsed, tokens, content
    fn = calls[0].get("function", {})
    args = fn.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    return fn.get("name"), args, elapsed, tokens, content


async def score(client: httpx.AsyncClient, model: str, tools: list[dict],
                repeat: int) -> dict:
    hits = 0
    total = 0
    times: list[float] = []
    tokens: list[int] = []
    problems: list[str] = []

    for text, want, want_args in CASES:
        for _ in range(repeat):
            got, args, secs, toks, content = await ask(client, model, text, tools)
            total += 1
            times.append(secs)
            tokens.append(toks)

            if got == "ERROR":
                problems.append(f"{text[:26]!r}: {content}")
                continue

            if got != want:
                shown = got or "no tool"
                problems.append(f"{text[:26]!r}: {shown}, wanted {want or 'no tool'}")
                # The dangerous failure: it answered instead of fetching, so it
                # sounds confident and is simply wrong.
                if want and got is None and content:
                    problems[-1] += f"  -- said {content[:34]!r}"
                continue

            bad = {k: (args.get(k), v) for k, v in want_args.items()
                   if not _arg_matches(args.get(k), v)}
            if bad:
                problems.append(f"{text[:26]!r}: right tool, wrong args {bad}")
                continue
            hits += 1

    return {
        "model": model,
        "accuracy": hits / total if total else 0.0,
        "hits": hits,
        "total": total,
        "p50": statistics.median(times) if times else 0.0,
        "max": max(times) if times else 0.0,
        "tokens": statistics.median(tokens) if tokens else 0,
        "problems": problems,
    }


async def run(args: argparse.Namespace) -> int:
    async with httpx.AsyncClient(base_url=args.ollama) as client:
        try:
            have = await installed(client)
        except Exception as exc:  # noqa: BLE001
            sys.exit(f"Ollama not reachable at {args.ollama}: {type(exc).__name__}")

        wanted = args.models or have
        missing = [m for m in wanted if m not in have]
        if missing:
            print(f"not pulled, skipping: {', '.join(missing)}")
            print(f"  pull with: ollama pull {missing[0]}\n")
        wanted = [m for m in wanted if m in have]
        if not wanted:
            sys.exit("No models to test. Pull one with: ollama pull qwen2.5:3b")

        tools = catalogue(build_ctx())
        size = len(json.dumps(tools)) // 4
        print(f"{len(CASES)} cases x {args.repeat}, {len(tools)} tools "
              f"(~{size} prompt tokens)\n")

        rows = []
        for model in wanted:
            print(f"  testing {model} ...", end="", flush=True)
            row = await score(client, model, tools, args.repeat)
            rows.append(row)
            print(f" {row['accuracy']:.0%}, p50 {row['p50']:.1f}s")

    rows.sort(key=lambda r: (-r["accuracy"], r["p50"]))
    print(f"\n{'model':<22}{'tool accuracy':>15}{'p50':>9}{'worst':>9}{'tokens':>9}")
    print("-" * 64)
    for r in rows:
        print(f"{r['model']:<22}{r['hits']:>7}/{r['total']:<7}{r['p50']:>8.1f}s"
              f"{r['max']:>8.1f}s{r['tokens']:>9}")

    for r in rows:
        if r["problems"]:
            print(f"\n{r['model']} got these wrong:")
            for p in dict.fromkeys(r["problems"]):     # dedupe, keep order
                print(f"  {p}")

    print("\nAccuracy first, then p50. A fast model that picks the wrong tool is")
    print("worse than a slow one that picks the right tool.")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Compare local models on tool calling")
    p.add_argument("models", nargs="*", help="model names; default is everything pulled")
    p.add_argument("--ollama", default="http://127.0.0.1:11434")
    p.add_argument("--repeat", type=int, default=1, help="runs per case, to catch flakiness")
    args = p.parse_args()
    try:
        sys.exit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()

"""
Web search tests.

The cleaner tests run offline and are the important ones: search snippets are
written to be skim-read, and reading one aloud verbatim produces things like
"105.4 km2 (40.7 sq mi) ... Rubber duck". Every case below came from a real
result during development.

With SearXNG running it also does a live search:
    cd infra && docker compose up -d searxng

    python scripts/test_web.py
"""
from __future__ import annotations

import asyncio
import logging
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.disable(logging.INFO)

from hub.brain.tools import web
from hub.server import load_config

# (raw snippet, substring that must be gone, substring that must survive)
CLEAN_CASES = [
    ("Paris[a] is the capital of France.", "[a]", "Paris"),
    ("Some fact[3] with a citation.", "[3]", "Some fact"),
    ("An area of 105.4 km2 (40.7 sq mi), as of 2026.", "sq mi", "105.4 km2"),
    ("The bridge is 200 ft (61 m) long.", "(61 m)", "200 ft"),
    ("acceptable for most ... Rubber duck racing", "...", "Rubber duck racing"),
    ("<b>Bold</b> and <i>italic</i> markup.", "<b>", "Bold"),
    ("Spaces   collapsed  here .", "  ", "Spaces collapsed here."),
    # the rule must be narrow: only number+unit brackets, not any bracket
    ("Frank Herbert (born 1920) wrote Dune.", "  ", "(born 1920)"),
    ("The pub (now closed) was there.", "  ", "(now closed)"),
]


def test_clean() -> int:
    failures = 0
    print("snippet cleaning (all cases seen in real results)")
    for raw, gone, kept in CLEAN_CASES:
        out = web._clean(raw)
        ok = gone not in out and kept in out
        failures += not ok
        print(f"  [{'ok  ' if ok else 'FAIL'}] {out[:58]!r}")
        if not ok:
            print(f"         {gone!r} should be gone, {kept!r} should remain")
    return failures


def test_trim() -> int:
    failures = 0
    print("\ntrimming never cuts mid-word")
    long = ("First sentence here. Second sentence follows on. "
            "Third sentence is considerably longer than the others and runs on.")
    for limit in (20, 40, 60, 80, 200):
        out = web._spoken_snippet(long, limit)
        # either it ended on punctuation, or it was elided explicitly
        clean_end = out.endswith((".", "!", "?", "...")) or out == long
        no_partial = out.rstrip(".") == "" or not out.rstrip(".").endswith(" ")
        ok = clean_end and no_partial and len(out) <= max(limit + 4, 24)
        failures += not ok
        print(f"  [{'ok  ' if ok else 'FAIL'}] limit {limit:>3} -> {out!r}")
    return failures


async def test_live() -> int:
    failures = 0
    cfg = load_config()
    ctx = types.SimpleNamespace(device="desktop", config=cfg, nodes=None, db=None,
                                scheduler=None, extra={}, transcript="")

    print("\nlive search")
    result = await web.web_search(ctx, "capital of France")
    if not result.ok:
        print(f"  [skip] SearXNG unavailable: {result.speech}")
        print("         start it with: cd infra && docker compose up -d searxng")
        return 0

    checks = [
        ("finds Paris", "paris" in result.speech.lower()),
        ("no markup left", "<" not in result.speech),
        ("no footnote markers", "[" not in result.speech),
        ("says something usable", len(result.speech) > 40),
    ]
    for label, ok in checks:
        failures += not ok
        print(f"  [{'ok  ' if ok else 'FAIL'}] {label}")
    print(f"         said: {result.speech[:110]}")

    print("\nfailure modes")
    empty = await web.web_search(ctx, "")
    failures += empty.ok
    print(f"  [{'ok  ' if not empty.ok else 'FAIL'}] empty query refused: {empty.speech!r}")

    down = types.SimpleNamespace(**{**ctx.__dict__,
                                   "config": {"search": {"base_url": "http://127.0.0.1:9"}}})
    dead = await web.web_search(down, "anything")
    ok = not dead.ok and "search service" in dead.speech.lower()
    failures += not ok
    print(f"  [{'ok  ' if ok else 'FAIL'}] service down explained: {dead.speech!r}")
    return failures


def main() -> int:
    failures = test_clean() + test_trim() + asyncio.run(test_live())
    print(f"\n{'all checks passed' if not failures else f'{failures} FAILURES'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

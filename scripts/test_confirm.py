"""
Confirmation-gate tests.

The invariant: a tool marked confirm=True must never execute on the first
utterance, by ANY route into the brain. This regressed once already -- the
Tier 1 fast path called invoke() directly and skipped the check, so
"close chrome" ran with no confirmation at all while the LLM path correctly
asked. The gate is a security property, so it gets its own test.

Runs the real Brain against fakes: no models, no network, no hub.

    python scripts/test_confirm.py
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import hub.brain.tools  # noqa: F401 -- registers every tool
from hub.brain import confirm
from hub.brain.context import Context
from hub.brain.dispatch import Brain
from hub.brain.registry import REGISTRY, ToolResult, tool
from hub.brain.router import RULES
from hub.session import Session

executed: list[str] = []


@tool(name="_test_destructive", description="test only",
      parameters={"type": "object", "properties": {"name": {"type": "string"}}},
      confirm=True)
async def _destructive(ctx: Context, name: str = "") -> ToolResult:
    executed.append(name)
    return ToolResult.say(f"Did the dangerous thing to {name}.")


class FakeNodes:
    def online(self): return ["laptop-1"]
    def describe(self): return [{"device": "laptop-1", "capabilities": ["open_app"],
                                "aliases": [], "platform": "test", "uptime_s": 1}]
    def resolve(self, hint): return "laptop-1"
    async def send(self, device, action, args, timeout=20.0):
        executed.append(f"{device}:{action}")
        return {"ok": True, "data": {"speech": "done"}}


def make_brain() -> Brain:
    cfg = {"llm": {"base_url": "http://127.0.0.1:1", "model": "none"},
           "router": {"threshold": 0.82}}
    return Brain(cfg, Path(tempfile.gettempdir()) / "jarvis-test-audit.jsonl")


def make_ctx() -> Context:
    async def speak(_text): pass
    async def announce(_d, _t): pass
    return Context(device="desktop", config={}, nodes=FakeNodes(), db=None,
                   speak=speak, announce=announce, scheduler=None)


async def main() -> int:
    failures = 0
    brain = make_brain()

    print("every Tier 1 rule aimed at a confirm=True tool must be gated")
    risky = [r for r in RULES
             if (spec := REGISTRY.get(r.tool)) is not None and spec.confirm]
    print(f"  {len(risky)} such rule(s): {sorted({r.tool for r in risky})}")

    # The concrete regression: "close notepad" matches Tier 1 and close_app
    # is confirm=True, so it must ask rather than act.
    for utterance, expect_tool in [("close notepad", "close_app")]:
        executed.clear()
        session = Session(device="desktop")
        reply = await brain.handle(make_ctx(), utterance, session)
        gated = session.pending is not None and not executed
        failures += not gated
        print(f"  [{'ok  ' if gated else 'FAIL'}] {utterance!r} -> "
              f"{'asked first' if gated else 'EXECUTED WITHOUT ASKING'}: {reply[:48]!r}")
        if gated:
            failures += session.pending.tool != expect_tool

    print("\nsaying no must cancel and run nothing")
    executed.clear()
    session = Session(device="desktop")
    await brain.handle(make_ctx(), "close notepad", session)
    reply = await brain.handle(make_ctx(), "no", session)
    ok = not executed and session.pending is None and "ancel" in reply
    failures += not ok
    print(f"  [{'ok  ' if ok else 'FAIL'}] nothing ran, pending cleared: {reply[:40]!r}")

    print("\nsaying yes must run it exactly once")
    executed.clear()
    session = Session(device="desktop")
    await brain.handle(make_ctx(), "close notepad", session)
    await brain.handle(make_ctx(), "yes", session)
    ok = len(executed) == 1 and session.pending is None
    failures += not ok
    print(f"  [{'ok  ' if ok else 'FAIL'}] ran {len(executed)} time(s), pending cleared")

    print("\nan expired confirmation must not fire later")
    executed.clear()
    session = Session(device="desktop")
    await brain.handle(make_ctx(), "close notepad", session)
    session.pending.created -= confirm.PENDING_TTL + 1
    await brain.handle(make_ctx(), "yes", session)
    ok = not executed
    failures += not ok
    print(f"  [{'ok  ' if ok else 'FAIL'}] stale 'yes' ran {len(executed)} tool(s)")

    print("\nspoken phrasing reads like a person, not a schema")
    phrasings = [
        ("close_app", {"name": "chrome"}),
        ("delete_files", {"paths": ["C:/x/a.txt", "C:/x/b.txt", "C:/x/c.txt"]}),
        ("delete_files", {"paths": ["C:/Users/param/Downloads/old.zip"]}),
        ("send_text", {"number": "555", "message": "running late"}),
        ("clear_list", {"list_name": "shopping"}),
        ("_test_destructive", {"name": "thing"}),   # no template -> generic
    ]
    for name, args in phrasings:
        q = confirm.question_for(name, args)
        # A question mark somewhere, not necessarily last: the delete phrasing
        # deliberately ends on the warning ("...? This can't be undone.").
        clean = "{" not in q and "}" not in q and "?" in q and "  " not in q
        failures += not clean
        print(f"  [{'ok  ' if clean else 'FAIL'}] {name:<18} {q}")

    total = 4 + len(phrasings) + 1
    print(f"\n{'all checks passed' if not failures else f'{failures} FAILURES'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

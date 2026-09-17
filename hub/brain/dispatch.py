"""
Turns a transcript into a spoken reply.

This is where the two tiers meet. Order matters: confirmation answers first
(so "yes" is never mistaken for a command), then the Tier 1 fast path, then
the LLM. Every resolution is written to the audit log.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from . import confirm, router
from . import rules  # noqa: F401 -- import populates router.RULES
from .agent import Agent, AgentReply
from .context import Context
from .registry import get, invoke

log = logging.getLogger("jarvis.dispatch")


class Brain:
    def __init__(self, cfg: dict[str, Any], audit_path: Path) -> None:
        self.cfg = cfg
        self.agent = Agent(cfg["llm"])
        self.threshold = cfg.get("router", {}).get("threshold", 0.82)
        self.audit_path = audit_path
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)

    async def aclose(self) -> None:
        await self.agent.aclose()

    def _audit(self, **fields: Any) -> None:
        fields["ts"] = time.time()
        try:
            with self.audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(fields, default=str) + "\n")
        except OSError:
            log.exception("could not write audit log")

    async def handle(self, ctx: Context, transcript: str, session: Any) -> str:
        started = time.perf_counter()
        tier = "tier2"

        # 1. Is this an answer to a pending confirmation?
        if session.pending is not None:
            pending = session.pending
            session.pending = None
            if pending.expired:
                log.info("pending %s expired, treating as a fresh command", pending.tool)
            else:
                answer = confirm.interpret(transcript)
                if answer is True:
                    ctx.extra["confirmed"] = True
                    result = await invoke(pending.tool, pending.args, ctx)
                    session.remember(transcript, result.speech)
                    self._audit(device=ctx.device, transcript=transcript, tier="confirm",
                                tool=pending.tool, args=pending.args, ok=result.ok,
                                ms=round((time.perf_counter() - started) * 1000))
                    return result.speech
                if answer is False:
                    self._audit(device=ctx.device, transcript=transcript, tier="confirm",
                                tool=pending.tool, cancelled=True)
                    return "Cancelled."
                # Neither yes nor no -- fall through and treat it as a new command.
                log.info("ambiguous confirmation answer %r, cancelling and continuing", transcript)

        # 2. Tier 1 fast path.
        hit = router.match(transcript, self.threshold)
        if hit is not None:
            # confirm=True must hold on every route into a tool, not just the
            # LLM one. A rule pointing at a destructive tool (close_app) would
            # otherwise execute it with no spoken confirmation at all, which
            # silently voids the guarantee.
            spec = get(hit.tool)
            if spec is not None and spec.confirm and not ctx.extra.get("confirmed"):
                question = confirm.question_for(hit.tool, hit.args)
                session.pending = confirm.PendingAction(
                    tool=hit.tool, args=hit.args, question=question)
                self._audit(device=ctx.device, transcript=transcript, tier="tier1",
                            tool=hit.tool, args=hit.args, awaiting_confirmation=True,
                            ms=round((time.perf_counter() - started) * 1000))
                log.info("tier1 tool %s needs confirmation", hit.tool)
                return question

            result = await invoke(hit.tool, hit.args, ctx)
            # Tier 1 turns go into history too, or a follow-up that does reach
            # the LLM ("close it") has no idea what just happened.
            session.remember(transcript, result.speech)
            ms = round((time.perf_counter() - started) * 1000)
            self._audit(device=ctx.device, transcript=transcript, tier="tier1",
                        tool=hit.tool, args=hit.args, ok=result.ok, ms=ms)
            log.info("tier1 answered in %dms", ms)
            return result.speech

        # 3. Tier 2: the LLM.
        reply: AgentReply = await self.agent.run(ctx, transcript, session.history)
        if reply.pending is not None:
            session.pending = reply.pending

        session.remember(transcript, reply.speech)
        ms = round((time.perf_counter() - started) * 1000)
        self._audit(device=ctx.device, transcript=transcript, tier=tier,
                    tools=reply.tools_used, reply=reply.speech,
                    awaiting_confirmation=reply.pending is not None, ms=ms)
        log.info("tier2 answered in %dms", ms)
        return reply.speech

"""
Tier 2: the LLM tool-calling loop against a local Ollama server.

This is the slow, capable path. Tier 1 (router.py) handles the common commands
without ever coming here. Design notes that matter for a 14B local model:

- Thinking mode is disabled. Qwen3 will happily spend 2000 tokens reasoning
  about turning a light on, which is unacceptable for voice.
- Temperature is low and the tool catalogue is small. Reliability of tool
  selection beats eloquence.
- max_tool_rounds is a hard stop: a confused small model will otherwise call
  the same tool forever.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from . import confirm
from .context import Context
from .registry import ToolResult, catalogue, get, invoke

log = logging.getLogger("jarvis.agent")

SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "system.txt").read_text(encoding="utf-8")


@dataclass(slots=True)
class AgentReply:
    speech: str
    pending: confirm.PendingAction | None = None
    tools_used: list[str] | None = None


class Agent:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.model = cfg["model"]
        self._client = httpx.AsyncClient(base_url=cfg["base_url"], timeout=120.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def health(self) -> bool:
        """Is Ollama up and is our model actually pulled?"""
        try:
            resp = await self._client.get("/api/tags", timeout=5.0)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            log.error("ollama unreachable at %s: %s", self.cfg["base_url"], exc)
            return False

        # Parsing stays inside a guard too: Ollama has used both "name" and
        # "model" keys across versions, and a KeyError here would abort hub
        # startup rather than just degrading Tier 2.
        try:
            models = resp.json().get("models", [])
            names = {m.get("name") or m.get("model", "") for m in models}
        except Exception as exc:  # noqa: BLE001
            log.error("could not read Ollama's model list: %s", exc)
            return False

        # Ollama reports "qwen3:14b"; accept a bare name matching any tag too.
        if self.model in names or any(n.split(":")[0] == self.model.split(":")[0] for n in names):
            return True
        log.error("model %r not pulled. Run: ollama pull %s", self.model, self.model)
        return False

    async def preload(self) -> None:
        """Load weights into VRAM now so the first real request is not cold."""
        try:
            await self._client.post(
                "/api/chat",
                json={"model": self.model, "messages": [], "keep_alive": self.cfg.get("keep_alive", "60m")},
                timeout=300.0,
            )
            log.info("ollama model %s preloaded", self.model)
        except Exception as exc:  # noqa: BLE001
            log.warning("preload failed (not fatal): %s", exc)

    async def _chat(self, messages: list[dict[str, Any]], tools: list[dict]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "keep_alive": self.cfg.get("keep_alive", "60m"),
            "think": False,  # Qwen3 reasoning mode: far too slow for voice
            "options": {
                "temperature": self.cfg.get("temperature", 0.3),
                "num_ctx": self.cfg.get("num_ctx", 8192),
            },
        }
        if tools:
            payload["tools"] = tools
        started = time.perf_counter()
        resp = await self._client.post("/api/chat", json=payload)
        resp.raise_for_status()
        body = resp.json()
        log.info("llm round in %.2fs", time.perf_counter() - started)
        return body.get("message", {})

    async def run(self, ctx: Context, transcript: str,
                  history: list[dict[str, Any]] | None = None) -> AgentReply:
        # The model has no clock of its own, so anything time-relative
        # ("remind me at six", "what's on tomorrow") needs this injected.
        now = datetime.now()
        system = (
            f"{SYSTEM_PROMPT}\n\n"
            f"The current local date and time is {now.strftime('%A %d %B %Y, %H:%M')}."
        )
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        messages.extend(history or [])
        messages.append({"role": "user", "content": transcript})

        tools = catalogue(ctx)
        used: list[str] = []

        for round_no in range(self.cfg.get("max_tool_rounds", 5)):
            try:
                message = await self._chat(messages, tools)
            except httpx.HTTPError as exc:
                log.exception("ollama call failed")
                return AgentReply(f"I couldn't reach my language model. {exc.__class__.__name__}.")

            calls = message.get("tool_calls") or []
            if not calls:
                speech = (message.get("content") or "").strip()
                return AgentReply(speech or "Sorry, I didn't catch that.", tools_used=used)

            messages.append(message)
            results: list[ToolResult] = []

            for call in calls:
                fn = call.get("function", {})
                name = fn.get("name", "")
                args = fn.get("arguments") or {}
                if isinstance(args, str):  # some builds return a JSON string
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}

                spec = get(name)
                if spec is not None and spec.confirm and not ctx.extra.get("confirmed"):
                    question = confirm.question_for(name, args)
                    log.info("tool %s needs confirmation", name)
                    return AgentReply(
                        question,
                        pending=confirm.PendingAction(tool=name, args=args, question=question),
                        tools_used=used,
                    )

                result = await invoke(name, args, ctx)
                results.append(result)
                used.append(name)
                log.info("tool %s -> ok=%s %r", name, result.ok, result.speech[:80])
                messages.append({
                    "role": "tool",
                    "name": name,
                    "content": json.dumps({"ok": result.ok, "result": result.speech,
                                           "data": result.data}, default=str)[:4000],
                })

            # A single successful action needs no second LLM round -- the tool
            # already produced a natural spoken confirmation. This saves ~1.5s
            # on the overwhelmingly common one-tool case. Read from `results`
            # rather than the loop variable so this cannot pick up a result
            # from an earlier round.
            if len(results) == 1 and results[0].ok and results[0].speech:
                return AgentReply(results[0].speech, tools_used=used)

        log.warning("hit max_tool_rounds without a final answer")
        return AgentReply("I got stuck working that out.", tools_used=used)

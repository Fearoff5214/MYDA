"""
Tier 1: the deterministic fast path.

A 14B model needs ~1.5s to emit a tool call. For "turn off the lights" that is
the difference between a product and a demo. Everything here bypasses the LLM
entirely and answers in well under half a second.

Phase 3 populates RULES from real usage -- the audit log records which Tier 2
resolutions happen often, and those get promoted here. Until then this module
is live but empty, and every utterance falls through to the LLM.
"""
from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger("jarvis.router")


@dataclass(slots=True)
class Match:
    tool: str
    args: dict[str, Any]
    score: float


@dataclass(slots=True)
class Rule:
    """A regex whose named groups become tool arguments."""
    pattern: re.Pattern[str]
    tool: str
    fixed: dict[str, Any]
    transform: Callable[[dict[str, str]], dict[str, Any]] | None = None


RULES: list[Rule] = []


def rule(pattern: str, tool: str, *, transform=None, **fixed: Any) -> None:
    RULES.append(Rule(re.compile(pattern, re.I), tool, fixed, transform))


def normalise(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"^(hey |ok |okay )?jarvis[,\s]*", "", text)
    text = re.sub(r"^(please|could you|can you|would you)\s+", "", text)
    return re.sub(r"[^\w\s:%-]", "", text).strip()


def match(transcript: str, threshold: float = 0.82) -> Match | None:
    """Return a Tier 1 match, or None to fall through to the LLM."""
    text = normalise(transcript)
    if not text:
        return None

    for r in RULES:
        m = r.pattern.fullmatch(text)
        if not m:
            continue
        args = dict(r.fixed)
        groups = {k: v for k, v in m.groupdict().items() if v is not None}
        args.update(r.transform(groups) if r.transform else groups)
        log.info("tier1 hit %r -> %s(%s)", text, r.tool, args)
        return Match(r.tool, args, 1.0)

    return None


def fuzzy_pick(text: str, options: list[str], threshold: float) -> str | None:
    """Shared helper for matching a spoken name against a known set."""
    hit = difflib.get_close_matches(text, options, n=1, cutoff=threshold)
    return hit[0] if hit else None

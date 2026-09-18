"""
Web search, via a local SearXNG instance.

SearXNG runs in Docker on the hub and queries public engines on your behalf,
so no single search provider sees your query history and there is no API key
to manage. It is the one tool here that needs the internet -- when it is
unavailable it says so plainly rather than hanging, because a voice assistant
that goes quiet is worse than one that admits it cannot reach something.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from ..context import Context
from ..registry import ToolResult, tool

log = logging.getLogger("jarvis.web")

DEFAULT_URL = "http://127.0.0.1:8888"
MAX_RESULTS = 4
SNIPPET_CHARS = 320

_TAGS = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    """Search snippets are written to be skim-read, not spoken.

    Real examples this fixes, all from live results:
      "105.4 km2 (40.7 sq mi), as of"   -> the unit conversion is noise aloud
      "acceptable ... Rubber duck"      -> SearXNG joins fragments with "..."
      "[3]"                             -> citation markers
    """
    text = _TAGS.sub("", text or "")
    # Citation and footnote markers: "[3]" and Wikipedia's lettered "Paris[a]".
    text = re.sub(r"\[(?:\d{1,3}|[a-z]{1,2})\]", "", text)
    text = re.sub(r"\s*\.{3,}\s*", ". ", text)             # fragment joins
    # Unit conversions in brackets. Matching only "number + unit" keeps real
    # parenthetical text -- "(born 1947)", "(now closed)" -- while dropping
    # "(40.7 sq mi)" and "(61 m)", which are noise when read aloud.
    text = re.sub(
        r"\s*\(\s*(?:approx\.?\s*|about\s*|~)?[\d.,]+\s*"
        r"(?:sq\s?mi|mi2?|km2?|cm|mm|km|m|ft|in|yd|lbs?|kg|g|oz|°?[CF])\s*\)",
        "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text)
    return re.sub(r"\s+([.,;:])", r"\1", text).strip()


def _spoken_snippet(text: str, limit: int) -> str:
    """Trim to `limit` on a sentence boundary, never mid-word."""
    text = _clean(text)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # Prefer ending on a full sentence; fall back to the last whole word.
    for end in (". ", "! ", "? "):
        if (i := cut.rfind(end)) > limit // 2:
            return cut[:i + 1].strip()
    return cut[:cut.rfind(" ")].strip() + "..." if " " in cut else cut.strip()


@tool(
    name="web_search",
    description=(
        "Search the web for current information the user asks about -- news, "
        "prices, opening times, facts you are unsure of. Do not use it for "
        "things you already know."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for"},
        },
        "required": ["query"],
    },
)
async def web_search(ctx: Context, query: str) -> ToolResult:
    query = (query or "").strip()
    if not query:
        return ToolResult.fail("What should I search for?")

    base = (ctx.config.get("search", {}) or {}).get("base_url", DEFAULT_URL)

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.get(
                f"{base.rstrip('/')}/search",
                params={"q": query, "format": "json", "safesearch": 1},
            )
            resp.raise_for_status()
            payload: dict[str, Any] = resp.json()
    except httpx.ConnectError:
        log.warning("searxng not reachable at %s", base)
        return ToolResult.fail(
            "My search service isn't running, so I can't look that up."
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 403:
            # SearXNG ships with json disabled and answers 403 without it. The
            # SEARXNG_SEARCH_FORMATS env var does not enable it; only
            # infra/searxng/settings.yml does. Easy hours to lose.
            log.error("searxng refused json. Add 'json' under search.formats "
                      "in infra/searxng/settings.yml and restart the container.")
            return ToolResult.fail(
                "My search service is refusing requests. It needs JSON output "
                "switched on."
            )
        log.warning("search failed: %s", exc)
        return ToolResult.fail("I couldn't reach the internet to check that.")
    except httpx.HTTPError as exc:
        log.warning("search failed: %s", exc)
        return ToolResult.fail("I couldn't reach the internet to check that.")
    except ValueError:
        # SearXNG returns HTML when the json format is not enabled.
        return ToolResult.fail(
            "My search service isn't set up to return results I can read."
        )

    results = payload.get("results") or []
    if not results:
        return ToolResult.fail(f"I found nothing useful about {query}.")

    # An instant answer, when there is one, is exactly what should be spoken.
    if answers := payload.get("answers"):
        answer = _clean(str(answers[0]))
        if answer:
            return ToolResult.say(answer, data={"source": "instant answer"})

    top = results[:MAX_RESULTS]
    lead = _spoken_snippet(top[0].get("content", ""), SNIPPET_CHARS)
    if not lead:
        lead = _clean(top[0].get("title", ""))

    others = [_clean(r.get("title", "")) for r in top[1:] if r.get("title")]
    speech = lead
    if others:
        speech += " Other results mention " + ", ".join(others[:2]) + "."

    return ToolResult.say(
        speech,
        data=[{"title": _clean(r.get("title", "")), "url": r.get("url", "")} for r in top],
    )

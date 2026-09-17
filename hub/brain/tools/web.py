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
    """Search snippets contain markup and citation numbers that read badly."""
    text = _TAGS.sub("", text or "")
    text = re.sub(r"\[\d+\]", "", text)
    return re.sub(r"\s+", " ", text).strip()


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
    lead = _clean(top[0].get("content", ""))[:SNIPPET_CHARS]
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

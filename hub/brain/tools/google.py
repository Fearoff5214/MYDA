"""
Gmail and Google Calendar, over the raw REST APIs.

These are the only cloud services Jarvis talks to. "Fully local" governs the
models -- speech recognition, reasoning and synthesis never leave the desktop
-- not your mail provider: reading your Gmail means asking Google, and there is
no honest way around that short of running your own mail server. Notes, lists,
reminders and timers stay local; this module does not.

Deliberately no google-api-python-client / google-auth / google-auth-oauthlib.
Between them those pull in a large dependency tree to do what is really one
POST to refresh a token plus a handful of GETs, so this talks to the REST
endpoints with httpx, exactly as tools/home.py talks to Home Assistant.

Configure it by running, once, on the hub:

    python scripts/google_auth.py

which walks the OAuth consent screen in your browser and writes into
hub/secrets.yaml:

    google:
      client_id: "xxxx.apps.googleusercontent.com"
      client_secret: "xxxx"
      refresh_token: "1//xxxx"
      calendar_id: "primary"       # optional
      timezone: "Asia/Kolkata"     # optional

The refresh token is long-lived; the access token it buys lasts an hour and is
cached here until shortly before it expires, so a normal turn costs no extra
round trip.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import html
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any
from urllib.parse import quote

import httpx

from ..context import Context
from ..registry import ToolResult, tool

log = logging.getLogger("jarvis.google")

TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
CALENDAR = "https://www.googleapis.com/calendar/v3"

# Everything this module needs and nothing more. Kept in step with the list in
# scripts/google_auth.py -- widening it there means consenting again.
SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
)

DEFAULT_TZ = "Asia/Kolkata"
TOKEN_MARGIN = 120.0        # refresh this many seconds before Google expires it
HTTP_TIMEOUT = 15.0

MAX_SPOKEN_MESSAGES = 3     # a spoken list longer than this stops being useful
MAX_SUBJECT_CHARS = 90
MAX_BODY_CHARS = 700        # roughly 45 seconds of Piper: long enough, not absurd
MAX_SPOKEN_EVENTS = 6

NOT_CONFIGURED = (
    "Google isn't set up yet, so I can't reach your mail or calendar. "
    "Run the Google sign-in script on the hub first."
)
SIGN_IN_EXPIRED = (
    "My Google sign-in has stopped working. You'll need to run the Google "
    "sign-in script on the hub again."
)


class AuthError(RuntimeError):
    """The refresh token no longer buys an access token."""


class ApiError(RuntimeError):
    """Google answered, but not with what we asked for."""


# ---------------------------------------------------------------------------
# client


class Google:
    """Thin REST client for Gmail and Calendar with an access-token cache."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.client_id = cfg.get("client_id") or ""
        self.client_secret = cfg.get("client_secret") or ""
        self.refresh_token = cfg.get("refresh_token") or ""
        self.calendar_id = cfg.get("calendar_id") or "primary"
        self.tz_name = cfg.get("timezone") or DEFAULT_TZ
        self._client: httpx.AsyncClient | None = None
        self._token = ""
        self._expires = 0.0
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.refresh_token)

    @property
    def tz(self) -> Any:
        return _zone(self.tz_name)

    @property
    def events_url(self) -> str:
        return f"{CALENDAR}/calendars/{quote(self.calendar_id, safe='')}/events"

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=HTTP_TIMEOUT)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def token(self, force: bool = False) -> str:
        """A valid access token, minted from the refresh token when stale."""
        async with self._lock:
            if self._token and not force and time.monotonic() < self._expires:
                return self._token

            resp = await self._http().post(TOKEN_URL, data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            })
            if resp.status_code >= 400:
                # invalid_grant means revoked, expired, or the password changed.
                # No amount of retrying fixes that; the user must consent again.
                log.error("google token refresh refused: %s %s",
                          resp.status_code, resp.text[:200])
                raise AuthError(resp.text[:200])

            payload = resp.json()
            self._token = payload.get("access_token") or ""
            if not self._token:
                raise AuthError("no access_token in the token response")
            lifetime = float(payload.get("expires_in") or 3600)
            self._expires = time.monotonic() + max(30.0, lifetime - TOKEN_MARGIN)
            log.info("google access token refreshed, good for %.0fs", lifetime)
            return self._token

    async def request(self, method: str, url: str, *,
                      params: dict[str, Any] | None = None,
                      json: Any = None) -> dict[str, Any]:
        """One authenticated call, retried once if the token was rejected."""
        for attempt in (0, 1):
            access = await self.token(force=bool(attempt))
            resp = await self._http().request(
                method, url, params=params, json=json,
                headers={"Authorization": f"Bearer {access}"},
            )
            if resp.status_code == 401 and attempt == 0:
                continue        # token died early: mint a fresh one and retry
            if resp.status_code >= 400:
                detail = _api_message(resp)
                log.warning("google %s %s -> %s: %s",
                            method, url, resp.status_code, detail)
                raise ApiError(detail)
            if not resp.content:
                return {}
            try:
                return resp.json()
            except ValueError as exc:
                raise ApiError("an answer I couldn't read") from exc
        raise ApiError("authorisation kept being refused")

    async def get(self, url: str, **params: Any) -> dict[str, Any]:
        return await self.request(
            "GET", url, params={k: v for k, v in params.items() if v is not None})

    async def post(self, url: str, body: Any) -> dict[str, Any]:
        return await self.request("POST", url, json=body)


def _api_message(resp: httpx.Response) -> str:
    """Google's error bodies are JSON; pull the human sentence out of them."""
    try:
        return str(resp.json()["error"]["message"])[:200]
    except (ValueError, KeyError, TypeError):
        return f"error {resp.status_code}"


_google: Google | None = None


def client(ctx: Context) -> Google | None:
    global _google
    if _google is None or not _google.configured:
        _google = Google(ctx.config.get("google") or {})
    return _google if _google.configured else None


def google_configured(ctx: Context) -> bool:
    """Availability predicate: no credentials means no mail or calendar tools."""
    return client(ctx) is not None


def _fail(exc: Exception) -> ToolResult:
    """Every failure becomes a plain spoken sentence, never a stack trace.

    Nothing here ever guesses at data it could not fetch -- a tool that admits
    it failed is worth more than one that invents an inbox.
    """
    if isinstance(exc, AuthError):
        return ToolResult.fail(SIGN_IN_EXPIRED)
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return ToolResult.fail("I couldn't reach Google. Check the internet.")
    if isinstance(exc, httpx.TimeoutException):
        return ToolResult.fail("Google took too long to answer, so I gave up.")
    if isinstance(exc, ApiError):
        return ToolResult.fail(f"Google turned that down: {exc}")
    log.exception("unexpected google failure")
    return ToolResult.fail("Something went wrong talking to Google.")


# ---------------------------------------------------------------------------
# speech shaping


def _zone(name: str) -> Any:
    """The user's timezone.

    Asia/Kolkata has no daylight saving, so the fixed offset is exact rather
    than approximate if the tz database is missing -- Windows ships none of its
    own and tzdata is not a dependency of this project.
    """
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - a missing tz database must not break speech
        log.warning("no tz database for %s; using a fixed UTC+05:30 offset", name)
        return timezone(timedelta(hours=5, minutes=30))


_ONES = ("", "one", "two", "three", "four", "five", "six", "seven", "eight",
         "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
         "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
         "twenty one", "twenty two", "twenty three", "twenty four",
         "twenty five", "twenty six", "twenty seven", "twenty eight",
         "twenty nine")


def _count_word(n: int) -> str:
    return _ONES[n] if 1 <= n < len(_ONES) else str(n)


def _cap(text: str) -> str:
    """Start a sentence properly; the count words are lower case."""
    return text[:1].upper() + text[1:]


def _spoken_time(when: datetime) -> str:
    """Natural spoken clock time: "half past two in the afternoon".

    Piper reads "14:30" woodenly and an RFC3339 stamp is unlistenable, so times
    are spelled the way somebody would actually say them.
    """
    hour, minute = when.hour, when.minute

    def name(h24: int) -> str:
        return _ONES[h24 % 12 or 12]

    if minute == 0:
        core = f"{name(hour)} o'clock"
    elif minute == 15:
        core = f"quarter past {name(hour)}"
    elif minute == 30:
        core = f"half past {name(hour)}"
    elif minute == 45:
        core = f"quarter to {name(hour + 1)}"
    elif minute < 30:
        unit = "" if minute % 5 == 0 else " minutes"
        core = f"{_count_word(minute)}{unit} past {name(hour)}"
    else:
        left = 60 - minute
        unit = "" if left % 5 == 0 else " minutes"
        core = f"{_count_word(left)}{unit} to {name(hour + 1)}"

    if hour < 12:
        part = "in the morning"
    elif hour < 17:
        part = "in the afternoon"
    elif hour < 21:
        part = "in the evening"
    else:
        part = "at night"
    return f"{core} {part}"


def _spoken_day(day: date, today: date) -> str:
    """"today", "tomorrow", "on Friday", or "on the 3rd of October"."""
    delta = (day - today).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta == -1:
        return "yesterday"
    if 0 < delta < 7:
        return day.strftime("on %A")
    suffix = "th" if 11 <= day.day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(
        day.day % 10, "th")
    return f"on the {day.day}{suffix} of {day.strftime('%B')}"


def _spoken_list(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _sender_name(raw: str) -> str:
    """"Ada Lovelace <ada@x.com>" -> "Ada Lovelace".

    Raw addresses read terribly out loud, so when there is no display name the
    local part gets its punctuation smoothed away instead.
    """
    name, addr = parseaddr(raw or "")
    name = name.strip().strip('"')
    if name:
        return name
    local = (addr or raw or "someone").split("@", 1)[0]
    return re.sub(r"[._\-+]+", " ", local).strip() or "someone"


def _tidy(text: str, limit: int) -> str:
    """Collapse whitespace, then cut at a word boundary."""
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "..."


_BLOCKS = re.compile(r"<(script|style|head)\b.*?</\1>", re.S | re.I)
_BREAKS = re.compile(r"<(br|/p|/div|/tr|/li)\b[^>]*>", re.I)
_TAGS = re.compile(r"<[^>]+>")
_QUOTED = re.compile(r"^(>|On .{0,80}wrote:|-{2,}\s*Original Message)", re.I)


def _html_to_text(markup: str) -> str:
    """No parser dependency: block tags become newlines, the rest vanish."""
    text = _BLOCKS.sub(" ", markup or "")
    text = _BREAKS.sub("\n", text)
    text = _TAGS.sub(" ", text)
    return html.unescape(text)


def _readable_body(text: str) -> str:
    """Drop quoted replies, signatures and link soup before reading aloud."""
    lines: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line in ("--", "-- "):
            break                   # signature block
        if _QUOTED.match(line):
            break                   # quoted reply; only the new part was asked for
        lines.append(line)
    body = "\n".join(lines)
    # Bare URLs are unspeakable; say a link was there instead of reciting it.
    body = re.sub(r"https?://\S+", "a link", body)
    return re.sub(r"\s+", " ", body).strip()


def _b64url(data: str) -> bytes:
    """Gmail strips base64 padding, and a broken part must not kill the turn."""
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        log.warning("undecodable message part")
        return b""


# ---------------------------------------------------------------------------
# gmail helpers


def _headers(message: dict[str, Any]) -> dict[str, str]:
    raw = (message.get("payload") or {}).get("headers") or []
    return {h.get("name", "").lower(): h.get("value", "") for h in raw}


def _extract_body(payload: dict[str, Any]) -> str:
    """Walk the MIME tree for the best readable part: plain text, else HTML."""
    plain, rich = "", ""

    def walk(part: dict[str, Any]) -> None:
        nonlocal plain, rich
        mime = (part.get("mimeType") or "").lower()
        data = (part.get("body") or {}).get("data") or ""
        if data and mime == "text/plain" and not plain:
            plain = _b64url(data).decode("utf-8", "replace")
        elif data and mime == "text/html" and not rich:
            rich = _html_to_text(_b64url(data).decode("utf-8", "replace"))
        for child in part.get("parts") or []:
            walk(child)

    walk(payload or {})
    return plain or rich


async def _summary(g: Google, message_id: str) -> dict[str, str]:
    """From and subject for one message. Metadata format, so it stays cheap."""
    msg = await g.get(
        f"{GMAIL}/messages/{message_id}",
        format="metadata",
        metadataHeaders=["From", "Subject"],
    )
    head = _headers(msg)
    return {
        "id": message_id,
        "from": _sender_name(head.get("from", "")),
        "subject": _tidy(head.get("subject", ""), MAX_SUBJECT_CHARS) or "no subject",
    }


async def _summaries(g: Google, ids: list[str]) -> list[dict[str, str]]:
    """Fetch several summaries at once, skipping any single one that fails."""
    results = await asyncio.gather(
        *(_summary(g, mid) for mid in ids), return_exceptions=True)
    out: list[dict[str, str]] = []
    for item in results:
        if isinstance(item, BaseException):
            log.warning("skipped a message I couldn't read: %s", item)
            continue
        out.append(item)
    return out


def _spoken_senders(messages: list[dict[str, str]]) -> str:
    return _spoken_list([f"{m['from']}, about {m['subject']}" for m in messages])


# ---------------------------------------------------------------------------
# mail tools


@tool(
    name="check_email",
    description=(
        "Say how many unread emails are in the inbox and who the most recent "
        "ones are from. Use for 'any new email?', 'do I have mail?', "
        "'check my inbox'."
    ),
    parameters={"type": "object", "properties": {}},
    available=google_configured,
)
async def check_email(ctx: Context) -> ToolResult:
    g = client(ctx)
    if g is None:
        return ToolResult.fail(NOT_CONFIGURED)

    try:
        listing = await g.get(f"{GMAIL}/messages", q="is:unread in:inbox",
                              maxResults=MAX_SPOKEN_MESSAGES)
        ids = [m["id"] for m in listing.get("messages") or []]
        if not ids:
            return ToolResult.say("No unread email. Your inbox is clear.",
                                  data={"unread": 0})
        # The label carries the real total; the listing above is capped.
        label = await g.get(f"{GMAIL}/labels/INBOX")
        recent = await _summaries(g, ids)
    except (AuthError, ApiError, httpx.HTTPError) as exc:
        return _fail(exc)

    unread = int(label.get("messagesUnread") or len(ids))

    if unread == 1 and recent:
        one = recent[0]
        return ToolResult.say(
            f"One unread email, from {one['from']}, about {one['subject']}.",
            data={"unread": 1, "messages": recent})

    lead = f"You have {_count_word(unread)} unread emails."
    if not recent:
        return ToolResult.say(lead, data={"unread": unread})
    shown = ("The latest" if len(recent) == 1
             else f"The latest {_count_word(len(recent))}")
    return ToolResult.say(f"{lead} {shown}: {_spoken_senders(recent)}.",
                          data={"unread": unread, "messages": recent})


@tool(
    name="search_email",
    description=(
        "Search the user's email and summarise what turns up. Use for 'any "
        "email from Priya?', 'find the mail about the invoice'. Plain words "
        "work, and so does Gmail syntax like from: or subject:."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "Sender, subject or keyword, e.g. 'from:priya invoice'"},
            "count": {"type": "integer",
                      "description": "How many to summarise, default 3"},
        },
        "required": ["query"],
    },
    available=google_configured,
)
async def search_email(ctx: Context, query: str,
                       count: int = MAX_SPOKEN_MESSAGES) -> ToolResult:
    g = client(ctx)
    if g is None:
        return ToolResult.fail(NOT_CONFIGURED)

    query = (query or "").strip()
    if not query:
        return ToolResult.fail("What should I look for in your email?")
    try:
        count = max(1, min(5, int(count)))
    except (TypeError, ValueError):
        count = MAX_SPOKEN_MESSAGES

    try:
        listing = await g.get(f"{GMAIL}/messages", q=query, maxResults=count)
        ids = [m["id"] for m in listing.get("messages") or []]
        if not ids:
            return ToolResult.say(f"Nothing in your email matching {query}.",
                                  data={"results": []})
        found = await _summaries(g, ids)
    except (AuthError, ApiError, httpx.HTTPError) as exc:
        return _fail(exc)

    if not found:
        return ToolResult.fail("I found some messages but couldn't read them.")
    if len(found) == 1:
        one = found[0]
        return ToolResult.say(f"One match: {one['from']}, about {one['subject']}.",
                              data={"results": found})
    return ToolResult.say(
        _cap(f"{_count_word(len(found))} matches: {_spoken_senders(found)}."),
        data={"results": found})


@tool(
    name="read_email",
    description=(
        "Read an email out loud. With no query it reads the most recent unread "
        "message; with one it reads the best match. Use for 'read my latest "
        "email', 'read the one from the bank'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "Optional: which message, e.g. 'from:bank' or 'invoice'"},
        },
    },
    available=google_configured,
)
async def read_email(ctx: Context, query: str = "") -> ToolResult:
    g = client(ctx)
    if g is None:
        return ToolResult.fail(NOT_CONFIGURED)

    asked = (query or "").strip()
    try:
        listing = await g.get(f"{GMAIL}/messages",
                              q=asked or "is:unread in:inbox", maxResults=1)
        ids = [m["id"] for m in listing.get("messages") or []]
        if not ids:
            # An empty inbox is an answer, not a failure.
            if asked:
                return ToolResult.say(f"I couldn't find an email matching {asked}.")
            return ToolResult.say("You have no unread email to read.")
        msg = await g.get(f"{GMAIL}/messages/{ids[0]}", format="full")
    except (AuthError, ApiError, httpx.HTTPError) as exc:
        return _fail(exc)

    head = _headers(msg)
    sender = _sender_name(head.get("from", ""))
    subject = _tidy(head.get("subject", ""), MAX_SUBJECT_CHARS) or "no subject"
    body = _readable_body(_extract_body(msg.get("payload") or {}))
    if not body:
        body = html.unescape(msg.get("snippet") or "")

    spoken = _tidy(body, MAX_BODY_CHARS)
    if not spoken:
        return ToolResult.say(
            f"There's a message from {sender} about {subject}, but I can't read "
            "the body. It's probably just an attachment.")

    trailer = " That's as far as I'll read." if len(body) > MAX_BODY_CHARS else ""
    return ToolResult.say(
        f"From {sender}, about {subject}. {spoken}{trailer}",
        data={"id": ids[0], "from": sender, "subject": subject, "body": body})


@tool(
    name="send_email",
    description=(
        "Send an email on the user's behalf. Needs a full address, a subject "
        "and the message text. Use only when the user clearly asks to send "
        "mail, and never guess at the address."
    ),
    parameters={
        "type": "object",
        "properties": {
            "to": {"type": "string",
                   "description": "Recipient address, e.g. sam@example.com"},
            "subject": {"type": "string"},
            "body": {"type": "string", "description": "The message itself"},
        },
        "required": ["to", "subject", "body"],
    },
    confirm=True,       # outward-facing and unrecallable: a misheard address leaks
    available=google_configured,
)
async def send_email(ctx: Context, to: str, subject: str, body: str) -> ToolResult:
    g = client(ctx)
    if g is None:
        return ToolResult.fail(NOT_CONFIGURED)

    to = (to or "").strip()
    _, addr = parseaddr(to)
    # Dictated addresses arrive mangled ("sam at example dot com"). Refuse
    # rather than repair: mail to the wrong person cannot be taken back.
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", addr):
        return ToolResult.fail(
            f"{to} doesn't look like an email address, so I haven't sent anything.")

    subject = (subject or "").strip()
    body = (body or "").strip()
    if not body:
        return ToolResult.fail("What should the email say?")

    mail = EmailMessage()
    mail["To"] = addr
    mail["Subject"] = subject or "(no subject)"
    mail.set_content(body)
    raw = base64.urlsafe_b64encode(mail.as_bytes()).decode("ascii")

    try:
        await g.post(f"{GMAIL}/messages/send", {"raw": raw})
    except (AuthError, ApiError, httpx.HTTPError) as exc:
        return _fail(exc)

    log.info("sent mail to %s (%d characters)", addr, len(body))
    return ToolResult.say(f"Sent to {addr}.", data={"to": addr, "subject": subject})


# ---------------------------------------------------------------------------
# calendar helpers


def _event_start(event: dict[str, Any], tz: Any) -> tuple[datetime | None, bool]:
    """(local start, all_day).

    Calendar returns RFC3339 with an offset for timed events and a bare date
    for all-day ones, so both shapes have to be handled.
    """
    start = event.get("start") or {}
    stamp = start.get("dateTime")
    if stamp:
        try:
            return datetime.fromisoformat(stamp).astimezone(tz), False
        except ValueError:
            log.warning("unparseable event start %r", stamp)
            return None, False
    day = start.get("date")
    if day:
        try:
            return datetime.fromisoformat(day).replace(tzinfo=tz), True
        except ValueError:
            return None, True
    return None, False


def _event_phrase(event: dict[str, Any], tz: Any, *,
                  with_day: bool = False, today: date | None = None) -> str:
    title = _tidy(event.get("summary") or "", 80) or "an untitled event"
    when, all_day = _event_start(event, tz)
    if when is None:
        return title

    bits = [title]
    # Saying "today" against every item in a list of today's events is noise;
    # it only earns its place when the event is on some other day.
    if with_day:
        reference = today or datetime.now(tz).date()
        if when.date() != reference:
            bits.append(_spoken_day(when.date(), reference))
    bits.append("all day" if all_day else f"at {_spoken_time(when)}")
    where = _tidy(event.get("location") or "", 40)
    if where and not where.endswith("..."):
        bits.append(f"at {where}")
    return " ".join(bits)


async def _events_between(g: Google, start: datetime, end: datetime | None,
                          limit: int) -> list[dict[str, Any]]:
    """singleEvents expands recurrences, which is what "today" has to mean."""
    payload = await g.get(
        g.events_url,
        timeMin=start.isoformat(),
        timeMax=end.isoformat() if end else None,
        singleEvents="true",
        orderBy="startTime",
        maxResults=limit,
    )
    return payload.get("items") or []


def _day_bounds(day: date, tz: Any) -> tuple[datetime, datetime]:
    start = datetime.combine(day, datetime.min.time()).replace(tzinfo=tz)
    return start, start + timedelta(days=1)


# ---------------------------------------------------------------------------
# calendar tools


@tool(
    name="todays_agenda",
    description=(
        "Say what is on the user's calendar today. Use for 'what's on today?', "
        "'what does my day look like?', 'am I busy this afternoon?'."
    ),
    parameters={"type": "object", "properties": {}},
    available=google_configured,
)
async def todays_agenda(ctx: Context) -> ToolResult:
    g = client(ctx)
    if g is None:
        return ToolResult.fail(NOT_CONFIGURED)

    tz = g.tz
    now = datetime.now(tz)
    start, end = _day_bounds(now.date(), tz)

    try:
        items = await _events_between(g, start, end, MAX_SPOKEN_EVENTS + 4)
    except (AuthError, ApiError, httpx.HTTPError) as exc:
        return _fail(exc)

    if not items:
        return ToolResult.say("Nothing on your calendar today.", data={"events": []})

    phrases = [_event_phrase(e, tz) for e in items[:MAX_SPOKEN_EVENTS]]
    more = len(items) - len(phrases)
    noun = "thing" if len(items) == 1 else "things"
    tail = f", plus {_count_word(more)} more" if more > 0 else ""
    return ToolResult.say(
        f"You have {_count_word(len(items))} {noun} today: "
        f"{_spoken_list(phrases)}{tail}.",
        data={"events": items})


@tool(
    name="upcoming_events",
    description=(
        "Say what is coming up on the calendar. Leave the date out for the "
        "next few events whenever they are; pass a date for one particular "
        "day. Use for 'what's next?', 'what have I got on Friday?'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "date": {"type": "string",
                     "description": "Optional single day as ISO 8601, e.g. 2026-09-19. "
                                    "Work it out from today's date, given above."},
            "count": {"type": "integer", "description": "How many events, default 3"},
        },
    },
    available=google_configured,
)
async def upcoming_events(ctx: Context, date: str = "", count: int = 3) -> ToolResult:
    g = client(ctx)
    if g is None:
        return ToolResult.fail(NOT_CONFIGURED)

    tz = g.tz
    now = datetime.now(tz)
    try:
        count = max(1, min(MAX_SPOKEN_EVENTS, int(count)))
    except (TypeError, ValueError):
        count = 3

    wanted = (date or "").strip()[:10]
    day = None
    if wanted:
        try:
            day = datetime.strptime(wanted, "%Y-%m-%d").date()
        except ValueError:
            return ToolResult.fail("I didn't catch which day you meant.")
        start, end = _day_bounds(day, tz)
        if day == now.date():
            start = max(start, now)     # "later today", not this morning
    else:
        start, end = now, None

    try:
        items = await _events_between(g, start, end, count)
    except (AuthError, ApiError, httpx.HTTPError) as exc:
        return _fail(exc)

    if not items:
        if day is not None:
            return ToolResult.say(
                f"Nothing on your calendar {_spoken_day(day, now.date())}.",
                data={"events": []})
        return ToolResult.say("Nothing coming up on your calendar.",
                             data={"events": []})

    if day is not None:
        phrases = [_event_phrase(e, tz) for e in items]
        noun = "thing" if len(items) == 1 else "things"
        return ToolResult.say(
            _cap(f"{_count_word(len(items))} {noun} {_spoken_day(day, now.date())}: "
                 f"{_spoken_list(phrases)}."),
            data={"events": items})

    phrases = [_event_phrase(e, tz, with_day=True, today=now.date()) for e in items]
    if len(items) == 1:
        return ToolResult.say(f"Next up: {phrases[0]}.", data={"events": items})
    return ToolResult.say(f"Next {_count_word(len(items))}: {_spoken_list(phrases)}.",
                          data={"events": items})


@tool(
    name="create_event",
    description=(
        "Put something on the user's calendar. Work out the absolute local "
        "start time from whatever they said and pass it as ISO 8601. Use for "
        "'put dentist in my calendar at four on Thursday'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "What the event is called"},
            "start": {"type": "string",
                      "description": "Local start, ISO 8601, e.g. 2026-09-19T16:00:00. "
                                     "A bare date like 2026-09-19 makes it all day."},
            "duration_minutes": {"type": "integer", "description": "Default 60"},
            "location": {"type": "string", "description": "Optional, where it is"},
        },
        "required": ["title", "start"],
    },
    confirm=True,       # writes to a calendar other people may be looking at
    available=google_configured,
)
async def create_event(ctx: Context, title: str, start: str,
                       duration_minutes: int = 60, location: str = "") -> ToolResult:
    g = client(ctx)
    if g is None:
        return ToolResult.fail(NOT_CONFIGURED)

    title = (title or "").strip()
    if not title:
        return ToolResult.fail("What should I call the event?")

    tz = g.tz
    raw = (start or "").strip().replace("Z", "")
    all_day = len(raw) == 10
    try:
        begins = datetime.fromisoformat(raw)
    except (ValueError, AttributeError):
        return ToolResult.fail("I didn't catch when that should be.")
    begins = begins.replace(tzinfo=tz) if begins.tzinfo is None else begins.astimezone(tz)

    try:
        minutes = max(5, min(24 * 60, int(duration_minutes)))
    except (TypeError, ValueError):
        minutes = 60

    body: dict[str, Any] = {"summary": title}
    if (location or "").strip():
        body["location"] = location.strip()
    if all_day:
        body["start"] = {"date": begins.date().isoformat()}
        body["end"] = {"date": (begins.date() + timedelta(days=1)).isoformat()}
    else:
        body["start"] = {"dateTime": begins.isoformat(), "timeZone": g.tz_name}
        body["end"] = {"dateTime": (begins + timedelta(minutes=minutes)).isoformat(),
                       "timeZone": g.tz_name}

    try:
        created = await g.post(g.events_url, body)
    except (AuthError, ApiError, httpx.HTTPError) as exc:
        return _fail(exc)

    when = "all day" if all_day else f"at {_spoken_time(begins)}"
    return ToolResult.say(
        f"Added {title} {_spoken_day(begins.date(), datetime.now(tz).date())}, {when}.",
        data={"id": created.get("id"), "title": title})

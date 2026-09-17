"""
One-time Google sign-in for the Gmail and Calendar tools.

    python scripts/google_auth.py
    python scripts/google_auth.py --check        # test what is already stored

Run this on the hub, once. It opens your browser, you approve the scopes, and
the long-lived refresh token is written into hub/secrets.yaml under `google:`.
After that hub/brain/tools/google.py trades that token for a one-hour access
token whenever it needs one, and you never do this again unless you revoke
access or change your Google password.

Why the loopback flow, not the device-code flow
-----------------------------------------------
Google's device-code flow ("go to google.com/device and type this code") is
meant for input-constrained devices and only issues a small, fixed set of
scopes -- Gmail and Calendar are not among them, so it cannot grant what this
needs. The loopback flow is the documented choice for an installed/desktop app:
we bind a throwaway HTTP server on 127.0.0.1, send you to Google with that as
the redirect, and catch the `code` Google sends back. The hub runs on the
desktop, which has a browser, so there is nothing awkward about it. PKCE is
used as well as the client secret, because a secret shipped inside a desktop
app is not really a secret and Google recommends it for this client type.

Before running this, in the Google Cloud Console:

  1. Create (or pick) a project.
  2. APIs & Services -> Library: enable the **Gmail API** and the
     **Google Calendar API**.
  3. APIs & Services -> OAuth consent screen: External, add yourself as a Test
     user. A test-user token is fine here; it just needs re-consent every so
     often if the app stays unpublished.
  4. APIs & Services -> Credentials -> Create credentials -> OAuth client ID ->
     Application type **Desktop app**. No redirect URI to register: loopback
     addresses are accepted for desktop clients on any port.
  5. Copy the client ID and client secret; paste them in when asked below.

Nothing is written anywhere but hub/secrets.yaml, and the existing contents of
that file -- your per-device tokens especially -- are read, merged and written
back rather than replaced.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import secrets
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ROOT / "hub" / "secrets.yaml"

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
CALENDARS_URL = "https://www.googleapis.com/calendar/v3/users/me/calendarList"

# Must match SCOPES in hub/brain/tools/google.py. Widening this list means
# every user of it has to consent again, so keep it to what the tools call.
SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
)

WAIT_SECONDS = 300.0

PAGE = """<!doctype html>
<title>Jarvis</title>
<body style="font-family: system-ui; margin: 4rem; max-width: 30rem">
<h2>{heading}</h2>
<p>{detail}</p>
<p>You can close this tab and go back to the terminal.</p>
</body>"""


# ---------------------------------------------------------------------------
# secrets.yaml, read-modify-write


def load_secrets() -> dict:
    """The whole file. Never assume it only holds what we care about."""
    if not SECRETS.exists():
        return {}
    return yaml.safe_load(SECRETS.read_text(encoding="utf-8")) or {}


def save_google(entry: dict) -> None:
    """Merge our key in, leaving `devices:` and everything else untouched.

    Clobbering this file would lock every registered device out of the hub, so
    it is read first and only the `google` key is replaced -- and even that is
    merged, so hand-added extras like calendar_id survive.
    """
    data = load_secrets()
    existing = data.get("google") if isinstance(data.get("google"), dict) else {}
    data["google"] = {**existing, **entry}
    SECRETS.parent.mkdir(parents=True, exist_ok=True)
    SECRETS.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# plumbing


def post_form(url: str, fields: dict[str, str]) -> dict:
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"Google refused the request ({exc.code}):\n{detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Couldn't reach Google: {exc.reason}") from exc


def get_json(url: str, access_token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


class Catcher(BaseHTTPRequestHandler):
    """Catches the single redirect Google makes back to us."""

    result: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        got = {k: v[0] for k, v in query.items()}
        if "code" in got or "error" in got:
            Catcher.result = got

        if got.get("error"):
            page = PAGE.format(heading="Sign-in refused",
                               detail=f"Google said: {got['error']}")
        elif "code" in got:
            page = PAGE.format(heading="Signed in",
                               detail="Jarvis has the authorisation code.")
        else:
            page = PAGE.format(heading="Nothing to do here",
                               detail="This page is only used during sign-in.")

        payload = page.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args) -> None:
        pass            # the default logger writes to stderr and is just noise


def wait_for_code(server: HTTPServer, state: str) -> str:
    """Serve requests until the browser comes back, or we give up."""
    server.timeout = WAIT_SECONDS
    done = threading.Event()

    def serve() -> None:
        while not done.is_set() and not Catcher.result:
            server.handle_request()
        done.set()

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    done.wait(WAIT_SECONDS)

    got = Catcher.result
    if not got:
        raise SystemExit(
            "Timed out waiting for the browser. Run it again, and if the browser "
            "did not open, copy the URL above into it by hand."
        )
    if got.get("error"):
        raise SystemExit(f"Google refused: {got['error']}")
    if got.get("state") != state:
        # Mismatched state means the response did not come from the request we
        # made. Refuse it rather than exchange somebody else's code.
        raise SystemExit("The reply did not match the request. Nothing was saved.")
    return got["code"]


# ---------------------------------------------------------------------------
# flows


def consent(client_id: str, client_secret: str, port: int) -> dict:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(24)

    server = HTTPServer(("127.0.0.1", port), Catcher)
    redirect_uri = f"http://localhost:{server.server_address[1]}/"

    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",       # this is what yields a refresh token
        "prompt": "consent",            # ...and what makes Google re-issue one
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })

    print(f"Listening on {redirect_uri}")
    print("Opening your browser. Approve the four scopes:")
    for scope in SCOPES:
        print(f"  - {scope.rsplit('/', 1)[1]}")
    print()
    print("If the browser does not open, paste this into it:")
    print(url)
    print()
    try:
        webbrowser.open(url)
    except Exception as exc:  # noqa: BLE001 - a headless box has no browser
        print(f"(couldn't launch a browser: {exc})")

    code = wait_for_code(server, state)
    server.server_close()

    print("Got the code. Exchanging it for a refresh token...")
    tokens = post_form(TOKEN_URL, {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    })
    if not tokens.get("refresh_token"):
        raise SystemExit(
            "Google returned an access token but no refresh token, which means "
            "this account has already consented to this client. Revoke it at "
            "https://myaccount.google.com/permissions and run this again."
        )
    return tokens


def verify(client_id: str, client_secret: str, refresh_token: str) -> None:
    """Prove the stored token actually works, so the first voice command doesn't
    have to be the test."""
    tokens = post_form(TOKEN_URL, {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    })
    access = tokens.get("access_token")
    if not access:
        raise SystemExit("The refresh token did not yield an access token.")

    try:
        profile = get_json(PROFILE_URL, access)
        print(f"Gmail   : {profile.get('emailAddress')} "
              f"({profile.get('messagesTotal', '?')} messages)")
    except urllib.error.HTTPError as exc:
        print(f"Gmail   : FAILED ({exc.code}). Is the Gmail API enabled?")
    try:
        calendars = get_json(CALENDARS_URL, access)
        names = [c.get("summary", "?") for c in calendars.get("items", [])][:5]
        print(f"Calendar: {', '.join(names) or 'no calendars visible'}")
    except urllib.error.HTTPError as exc:
        print(f"Calendar: FAILED ({exc.code}). Is the Calendar API enabled?")


# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(
        description="Authorise Jarvis to read your Gmail and Calendar")
    p.add_argument("--client-id", default="", help="OAuth client ID (Desktop app)")
    p.add_argument("--client-secret", default="",
                   help="OAuth client secret; prompted for if omitted")
    p.add_argument("--port", type=int, default=0,
                   help="loopback port for the redirect, default: any free one")
    p.add_argument("--timezone", default="Asia/Kolkata",
                   help="how calendar times are spoken, default Asia/Kolkata")
    p.add_argument("--calendar", default="primary",
                   help="calendar id to read and write, default primary")
    p.add_argument("--check", action="store_true",
                   help="test the stored credentials and exit")
    args = p.parse_args()

    if args.check:
        google = load_secrets().get("google") or {}
        missing = [k for k in ("client_id", "client_secret", "refresh_token")
                   if not google.get(k)]
        if missing:
            print(f"hub/secrets.yaml has no google {', '.join(missing)}. "
                  "Run this script with no arguments first.")
            return 1
        verify(google["client_id"], google["client_secret"], google["refresh_token"])
        return 0

    client_id = args.client_id.strip() or input("Client ID    : ").strip()
    # getpass so the secret does not end up in the shell history or on screen.
    client_secret = args.client_secret.strip() or getpass.getpass("Client secret: ").strip()
    if not client_id or not client_secret:
        print("Both the client ID and the client secret are needed.")
        return 1

    tokens = consent(client_id, client_secret, args.port)

    save_google({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": tokens["refresh_token"],
        "calendar_id": args.calendar,
        "timezone": args.timezone,
    })
    print(f"Saved to {SECRETS}")
    print()
    verify(client_id, client_secret, tokens["refresh_token"])
    print()
    print("The hub reads secrets.yaml at startup -- restart it to pick this up.")
    print("Then try: python scripts/say.py \"do I have any new email\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())

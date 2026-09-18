"""
Onboard a fresh Home Assistant container without a browser, and mint a
long-lived token for Jarvis.

A brand-new Home Assistant has no user, and until it has one every REST call
returns 401 -- so the usual "create a long-lived token on your profile page"
instruction is unreachable from a headless box. HA exposes the same steps the
onboarding wizard uses as a REST API, which is what this drives.

    D:\\jarvis\\.venv\\Scripts\\python.exe infra/onboard_ha.py

Writes the credentials to logs/ha_credentials.txt (gitignored) and prints the
long-lived token. Safe to re-run: if HA is already onboarded it says so and
exits rather than clobbering anything.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import sys
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8123"
CLIENT_ID = f"{BASE}/"
CREDS = ROOT / "logs" / "ha_credentials.txt"

NAME = "Jarvis Owner"
USERNAME = "jarvis"
LANGUAGE = "en"


async def onboard() -> tuple[str, str]:
    """Returns (password, long_lived_token)."""
    password = "jarvis-" + secrets.token_urlsafe(9)

    async with httpx.AsyncClient(base_url=BASE, timeout=60.0) as http:
        steps = (await http.get("/api/onboarding")).json()
        done = {s["step"]: s["done"] for s in steps}
        print(f"onboarding steps: {done}")
        if done.get("user"):
            sys.exit(
                "Home Assistant already has an owner account. Delete\n"
                "  infra/homeassistant/.storage\n"
                "and restart the container to start over, or mint a token by hand."
            )

        resp = await http.post("/api/onboarding/users", json={
            "client_id": CLIENT_ID,
            "name": NAME,
            "username": USERNAME,
            "password": password,
            "language": LANGUAGE,
        })
        resp.raise_for_status()
        auth_code = resp.json()["auth_code"]
        print("owner created")

        resp = await http.post("/auth/token", data={
            "grant_type": "authorization_code",
            "code": auth_code,
            "client_id": CLIENT_ID,
        })
        resp.raise_for_status()
        access_token = resp.json()["access_token"]
        print("exchanged auth_code for an access token")

        auth = {"Authorization": f"Bearer {access_token}"}

        # Remaining wizard steps. Each is best-effort: a step that is already
        # done, or that this HA version does not have, must not abort the run.
        for step, payload in (
            ("core_config", {}),
            ("analytics", {}),
            ("integration", {"client_id": CLIENT_ID, "redirect_uri": CLIENT_ID}),
        ):
            r = await http.post(f"/api/onboarding/{step}", json=payload, headers=auth)
            print(f"  {step}: {r.status_code}")

    # A long-lived token is the point of the exercise: the access token above
    # expires in 30 minutes, which is no good for a service. Only the
    # WebSocket API can mint one.
    async with websockets.connect(f"ws://127.0.0.1:8123/api/websocket") as ws:
        hello = json.loads(await ws.recv())
        assert hello["type"] == "auth_required", hello
        await ws.send(json.dumps({"type": "auth", "access_token": access_token}))
        ok = json.loads(await ws.recv())
        assert ok["type"] == "auth_ok", ok
        await ws.send(json.dumps({
            "id": 1,
            "type": "auth/long_lived_access_token",
            "client_name": "Jarvis hub",
            "lifespan": 3650,          # days
        }))
        result = json.loads(await ws.recv())
        if not result.get("success"):
            sys.exit(f"could not mint a long-lived token: {result}")
        long_lived = result["result"]
        print("minted a long-lived token (3650 days)")

    return password, long_lived


def main() -> None:
    password, token = asyncio.run(onboard())
    CREDS.parent.mkdir(parents=True, exist_ok=True)
    CREDS.write_text(
        "Home Assistant (jarvis-homeassistant container)\n"
        f"  URL      : {BASE}\n"
        f"  Username : {USERNAME}\n"
        f"  Password : {password}\n"
        f"  Name     : {NAME}\n"
        "\n"
        "Throwaway credentials, created by infra/onboard_ha.py. Change the\n"
        "password from the profile page if this instance ever sees real devices.\n"
        "\n"
        "Long-lived access token used by the hub (also in hub/secrets.yaml\n"
        "under home_assistant.token):\n"
        f"  {token}\n",
        encoding="utf-8",
    )
    print(f"\ncredentials written to {CREDS}")
    print(f"token: {token[:12]}...{token[-6:]}")


if __name__ == "__main__":
    main()

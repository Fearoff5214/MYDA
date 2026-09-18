"""
Smart home, via Home Assistant's REST API.

The tool catalogue is *not* hard-coded to your devices. It reads the live
entity list from Home Assistant and matches spoken names against it, so
pairing a new plug makes it controllable without touching this file.

A note on what is reachable. Tuya/Smart Life and TP-Link Kasa gear re-pairs
into Home Assistant and is controlled locally -- fast and reliable. Devices
bound only to an Amazon Echo or Google Home are cloud-locked and cannot be
controlled locally at all; they need either re-pairing into Home Assistant, or
the unofficial alexa_media_player integration, which authenticates with an
Amazon cookie and breaks whenever Amazon changes their login flow. If a device
is unreachable, that is almost always why.

Configure in hub/secrets.yaml:

    home_assistant:
      base_url: "http://127.0.0.1:8123"
      token: "<long-lived access token from your HA profile page>"
"""
from __future__ import annotations

import asyncio
import difflib
import logging
import time
from typing import Any

import httpx

from ..context import Context
from ..registry import ToolResult, tool

log = logging.getLogger("jarvis.home")

# Domains worth exposing to voice. Sensors are excluded deliberately: there
# are usually hundreds and they drown out the things you can actually control.
CONTROLLABLE = ("light", "switch", "fan", "media_player", "climate",
                "cover", "scene", "script", "input_boolean")

ENTITY_TTL = 60.0   # seconds; a newly paired device appears within a minute


class HomeAssistant:
    """Thin REST client with a short-lived entity cache."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.base_url = (cfg.get("base_url") or "").rstrip("/")
        self.token = cfg.get("token") or ""
        self._client: httpx.AsyncClient | None = None
        self._entities: list[dict[str, Any]] = []
        self._fetched = 0.0
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=15.0,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def entities(self, force: bool = False) -> list[dict[str, Any]]:
        async with self._lock:
            fresh = time.monotonic() - self._fetched < ENTITY_TTL
            if self._entities and fresh and not force:
                return self._entities

            resp = await self._http().get("/api/states")
            resp.raise_for_status()
            states = resp.json()

            self._entities = [
                {
                    "entity_id": s["entity_id"],
                    "domain": s["entity_id"].split(".", 1)[0],
                    "name": s.get("attributes", {}).get("friendly_name")
                            or s["entity_id"].split(".", 1)[1].replace("_", " "),
                    "state": s.get("state"),
                    "attributes": s.get("attributes", {}),
                }
                for s in states
                if s["entity_id"].split(".", 1)[0] in CONTROLLABLE
            ]
            self._fetched = time.monotonic()
            log.info("home assistant: %d controllable entities", len(self._entities))
            return self._entities

    async def find(self, spoken: str, domain: str | None = None) -> dict[str, Any] | None:
        """Match a spoken device name against friendly names."""
        entities = await self.entities()
        if domain:
            entities = [e for e in entities if e["domain"] == domain]
        if not entities:
            return None

        needle = (spoken or "").lower().strip()
        for filler in ("the ", "my "):
            needle = needle.removeprefix(filler)
        if not needle:
            return None

        by_name = {e["name"].lower(): e for e in entities}

        if needle in by_name:
            return by_name[needle]
        # Substring, preferring the shortest match so "bedroom" does not pick
        # "bedroom lamp behind the wardrobe" over plain "bedroom".
        subs = [e for n, e in by_name.items() if needle in n or n in needle]
        if subs:
            return min(subs, key=lambda e: len(e["name"]))
        close = difflib.get_close_matches(needle, list(by_name), n=1, cutoff=0.6)
        return by_name[close[0]] if close else None

    async def state(self, entity_id: str) -> str | None:
        """Live state of one entity, bypassing the cache.

        The entity cache exists so the tool catalogue and name matching do not
        re-fetch hundreds of entities per turn, but a *state* question must
        never be answered from it -- "are the lights on?" seconds after turning
        them on would otherwise say off.
        """
        try:
            resp = await self._http().get(f"/api/states/{entity_id}")
            resp.raise_for_status()
            return resp.json().get("state")
        except httpx.HTTPError as exc:
            log.warning("could not read %s: %s", entity_id, exc)
            return None

    async def call(self, domain: str, service: str, data: dict[str, Any]) -> None:
        resp = await self._http().post(f"/api/services/{domain}/{service}", json=data)
        resp.raise_for_status()
        # We just changed something, so the cached states are now wrong. Expire
        # them rather than serving a snapshot that contradicts what we just did.
        self._fetched = 0.0


_ha: HomeAssistant | None = None


def client(ctx: Context) -> HomeAssistant | None:
    global _ha
    if _ha is None:
        cfg = ctx.config.get("home_assistant") or {}
        _ha = HomeAssistant(cfg)
    return _ha if _ha.configured else None


def ha_configured(ctx: Context) -> bool:
    """Availability predicate: no Home Assistant means no smart home tools."""
    return client(ctx) is not None


NOT_CONFIGURED = (
    "Home Assistant isn't set up yet, so I can't reach your devices."
)


@tool(
    name="control_device",
    description=(
        "Turn a smart home device on or off, or toggle it. Works for lights, "
        "plugs, switches, fans and scenes. Use for 'turn off the bedroom "
        "lights', 'switch on the kettle', 'turn on the fan'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string",
                     "description": "The device as the user said it, e.g. 'bedroom lights'"},
            "state": {"type": "string", "enum": ["on", "off", "toggle"]},
        },
        "required": ["name", "state"],
    },
    available=ha_configured,
)
async def control_device(ctx: Context, name: str, state: str) -> ToolResult:
    ha = client(ctx)
    if ha is None:
        return ToolResult.fail(NOT_CONFIGURED)

    try:
        entity = await ha.find(name)
    except httpx.HTTPError as exc:
        log.warning("home assistant unreachable: %s", exc)
        return ToolResult.fail("I couldn't reach Home Assistant.")

    if entity is None:
        return ToolResult.fail(f"I couldn't find a device called {name}.")

    domain = entity["domain"]
    if domain == "scene":
        service = "turn_on"       # scenes only activate
    else:
        service = {"on": "turn_on", "off": "turn_off",
                   "toggle": "toggle"}.get(state, "toggle")

    try:
        # homeassistant.turn_on works across every domain, which keeps this
        # generic instead of needing a branch per device type.
        await ha.call("homeassistant", service, {"entity_id": entity["entity_id"]})
    except httpx.HTTPError as exc:
        log.warning("service call failed: %s", exc)
        return ToolResult.fail(f"Home Assistant refused that: {exc}")

    verb = {"turn_on": "on", "turn_off": "off", "toggle": "toggled"}[service]
    if verb == "toggled":
        return ToolResult.say(f"Toggled the {entity['name']}.")
    return ToolResult.say(f"Turned the {entity['name']} {verb}.")


@tool(
    name="set_brightness",
    description="Set a light's brightness as a percentage from 1 to 100.",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "percent": {"type": "integer", "description": "1 to 100"},
        },
        "required": ["name", "percent"],
    },
    available=ha_configured,
)
async def set_brightness(ctx: Context, name: str, percent: int) -> ToolResult:
    ha = client(ctx)
    if ha is None:
        return ToolResult.fail(NOT_CONFIGURED)

    try:
        percent = max(1, min(100, int(percent)))
    except (TypeError, ValueError):
        return ToolResult.fail("I didn't understand that brightness.")

    entity = await ha.find(name, domain="light")
    if entity is None:
        return ToolResult.fail(f"I couldn't find a light called {name}.")

    try:
        await ha.call("light", "turn_on",
                      {"entity_id": entity["entity_id"], "brightness_pct": percent})
    except httpx.HTTPError as exc:
        return ToolResult.fail(f"Home Assistant refused that: {exc}")
    return ToolResult.say(f"{entity['name']} at {percent} percent.")


@tool(
    name="set_temperature",
    description="Set a thermostat's target temperature in degrees Celsius.",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "celsius": {"type": "number"},
        },
        "required": ["celsius"],
    },
    available=ha_configured,
)
async def set_temperature(ctx: Context, celsius: float, name: str = "") -> ToolResult:
    ha = client(ctx)
    if ha is None:
        return ToolResult.fail(NOT_CONFIGURED)

    entities = [e for e in await ha.entities() if e["domain"] == "climate"]
    if not entities:
        return ToolResult.fail("You don't have a thermostat set up.")
    entity = await ha.find(name, domain="climate") if name else entities[0]
    if entity is None:
        return ToolResult.fail(f"I couldn't find a thermostat called {name}.")

    try:
        await ha.call("climate", "set_temperature",
                      {"entity_id": entity["entity_id"], "temperature": float(celsius)})
    except httpx.HTTPError as exc:
        return ToolResult.fail(f"Home Assistant refused that: {exc}")
    return ToolResult.say(f"{entity['name']} set to {celsius:.0f} degrees.")


@tool(
    name="device_state",
    description=(
        "Say whether a smart home device is currently on or off, or what a "
        "thermostat is set to. Use for 'are the lights on?'."
    ),
    parameters={
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    },
    available=ha_configured,
)
async def device_state(ctx: Context, name: str) -> ToolResult:
    ha = client(ctx)
    if ha is None:
        return ToolResult.fail(NOT_CONFIGURED)

    entity = await ha.find(name)
    if entity is None:
        return ToolResult.fail(f"I couldn't find a device called {name}.")

    # Read through to Home Assistant: the cached entity list is up to
    # ENTITY_TTL seconds old, and answering "is it on?" from a stale snapshot
    # is worse than being slightly slower.
    state = await ha.state(entity["entity_id"]) or entity["state"]
    if entity["domain"] == "climate":
        target = entity["attributes"].get("temperature")
        current = entity["attributes"].get("current_temperature")
        parts = [f"{entity['name']} is {state}"]
        if current is not None:
            parts.append(f"currently {current:.0f} degrees")
        if target is not None:
            parts.append(f"set to {target:.0f}")
        return ToolResult.say(", ".join(parts) + ".")

    return ToolResult.say(f"The {entity['name']} is {state}.")


@tool(
    name="list_smart_devices",
    description=(
        "Say what smart home devices exist. Use when the user asks what you "
        "can control, or when a device they named cannot be found."
    ),
    parameters={
        "type": "object",
        "properties": {
            "kind": {"type": "string",
                     "description": "Optional filter: light, switch, fan, climate, scene"},
        },
    },
    available=ha_configured,
)
async def list_smart_devices(ctx: Context, kind: str = "") -> ToolResult:
    ha = client(ctx)
    if ha is None:
        return ToolResult.fail(NOT_CONFIGURED)

    try:
        entities = await ha.entities(force=True)
    except httpx.HTTPError:
        return ToolResult.fail("I couldn't reach Home Assistant.")

    if kind:
        entities = [e for e in entities if e["domain"] == kind.rstrip("s").lower()]
    if not entities:
        return ToolResult.say("I can't see any devices like that.")

    names = [e["name"] for e in entities]
    spoken = ", ".join(names[:10])
    more = f", and {len(names) - 10} more" if len(names) > 10 else ""
    return ToolResult.say(f"I can control {spoken}{more}.", data=names)

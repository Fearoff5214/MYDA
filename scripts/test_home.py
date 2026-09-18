"""
Smart home tests against a live Home Assistant.

Calls the tools in-process rather than through voice, so this works without
Ollama running (most smart-home phrasings reach Tier 2, and the fast-path
rules only cover the common ones).

Needs Home Assistant up and `home_assistant:` in hub/secrets.yaml:
    cd infra && docker compose up -d homeassistant
    python infra/onboard_ha.py

    python scripts/test_home.py

The assertions check the SPOKEN string, not just that the call succeeded --
a tool that works but says something unusable is still broken here.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.disable(logging.INFO)

from hub.brain.registry import catalogue
from hub.brain.tools import home
from hub.server import load_config

failures = 0


def make_ctx(config: dict) -> types.SimpleNamespace:
    async def _noop(*_a, **_k): pass
    return types.SimpleNamespace(
        device="desktop", config=config, nodes=None, db=None,
        speak=_noop, announce=_noop, scheduler=None, extra={}, transcript="",
    )


async def expect(label: str, coro, *, ok: bool = True, contains: str = "") -> None:
    global failures
    result = await coro
    good = result.ok is ok and (not contains or contains.lower() in result.speech.lower())
    failures += not good
    print(f"  [{'ok  ' if good else 'FAIL'}] {label:<40} {result.speech[:56]!r}")
    if not good and contains:
        print(f"         wanted ok={ok} and {contains!r} in the reply")


async def main() -> int:
    global failures
    cfg = load_config()
    ctx = make_ctx(cfg)

    if not home.ha_configured(ctx):
        sys.exit("Home Assistant is not configured. Run infra/onboard_ha.py first.")

    print("listing what exists")
    await expect("list_smart_devices", home.list_smart_devices(ctx), contains="bedroom")
    await expect("list only lights", home.list_smart_devices(ctx, kind="light"),
                 contains="lamp")

    print("\nturning things on and off")
    await expect("bedroom lights on", home.control_device(ctx, "bedroom lights", "on"),
                 contains="on")
    await expect("state reads back on", home.device_state(ctx, "bedroom lights"),
                 contains="on")
    await expect("bedroom lights off", home.control_device(ctx, "bedroom lights", "off"),
                 contains="off")
    await expect("state reads back off", home.device_state(ctx, "bedroom lights"),
                 contains="off")
    await expect("toggle the fan", home.control_device(ctx, "living room fan", "toggle"),
                 contains="fan")
    await expect("switch on the coffee machine",
                 home.control_device(ctx, "coffee machine", "on"), contains="coffee")
    await expect("plug exposed directly",
                 home.control_device(ctx, "kitchen plug", "on"), contains="kitchen")
    await expect("activate a scene",
                 home.control_device(ctx, "movie night", "on"), contains="movie")

    print("\nfuzzy name matching (this is the whole interface)")
    for spoken in ["the bedroom lights", "bedroom light", "Bedroom Lights",
                   "living room lamp", "the lamp", "coffee"]:
        await expect(f"{spoken!r}", home.control_device(ctx, spoken, "off"))

    print("\nbrightness")
    await expect("lamp to 30 percent", home.set_brightness(ctx, "living room lamp", 30),
                 contains="30")
    await expect("clamps above 100", home.set_brightness(ctx, "living room lamp", 500),
                 contains="100")
    await expect("rejects nonsense", home.set_brightness(ctx, "living room lamp", "loud"),
                 ok=False)

    print("\nthermostat")
    await expect("set to 23", home.set_temperature(ctx, 23), contains="23")
    await expect("thermostat state", home.device_state(ctx, "hallway thermostat"),
                 contains="degrees")

    print("\nthings that must fail cleanly, not hang or invent")
    await expect("device that does not exist",
                 home.control_device(ctx, "disco ball", "on"), ok=False,
                 contains="couldn't find")
    await expect("brightness on a non-light",
                 home.set_brightness(ctx, "coffee machine", 50), ok=False)
    await expect("state of nothing", home.device_state(ctx, "nonsense"), ok=False)

    print("\nthe availability predicate gates the tools")
    bare = make_ctx({})
    home._ha = None                     # drop the cached client
    gated = not home.ha_configured(bare)
    names = {t["function"]["name"] for t in catalogue(bare)}
    hidden = not (names & {"control_device", "set_brightness", "set_temperature",
                           "device_state", "list_smart_devices"})
    failures += not (gated and hidden)
    print(f"  [{'ok  ' if gated and hidden else 'FAIL'}] "
          f"unconfigured -> tools gated={gated}, all 5 hidden from catalogue={hidden}")

    home._ha = None                     # restore for any later use
    print(f"\n{'all checks passed' if not failures else f'{failures} FAILURES'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

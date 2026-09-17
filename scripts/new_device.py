"""
Register a device and mint its token.

    python scripts/new_device.py desktop --alias "the desktop"
    python scripts/new_device.py laptop-1 --alias "bedroom laptop" --alias "the small one"

Writes hub/secrets.yaml (gitignored) and prints the token once. Copy it to the
device -- into client/token on that machine, or its JARVIS_TOKEN env var.
"""
from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ROOT / "hub" / "secrets.yaml"


def load() -> dict:
    if not SECRETS.exists():
        return {"devices": {}}
    data = yaml.safe_load(SECRETS.read_text(encoding="utf-8")) or {}
    data.setdefault("devices", {})
    return data


def main() -> int:
    p = argparse.ArgumentParser(description="Add a Jarvis device")
    p.add_argument("name", help="device id, e.g. laptop-1 (no spaces)")
    p.add_argument("--alias", action="append", default=[],
                   help="spoken name, repeatable: --alias 'bedroom laptop'")
    p.add_argument("--rotate", action="store_true", help="replace an existing token")
    p.add_argument("--list", action="store_true", help="list devices and exit")
    args = p.parse_args()

    data = load()

    if args.list:
        for name, meta in data["devices"].items():
            aliases = ", ".join(meta.get("aliases", [])) or "-"
            print(f"{name:<16} aliases: {aliases}")
        return 0

    if " " in args.name:
        print("Device id must not contain spaces. Use --alias for spoken names.")
        return 1

    existing = data["devices"].get(args.name)
    if existing and not args.rotate:
        print(f"{args.name} already exists. Use --rotate to issue a new token.")
        return 1

    token = secrets.token_urlsafe(32)
    data["devices"][args.name] = {
        "token": token,
        "aliases": args.alias or (existing or {}).get("aliases", []),
    }

    SECRETS.parent.mkdir(parents=True, exist_ok=True)
    SECRETS.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    print(f"Device : {args.name}")
    print(f"Aliases: {', '.join(data['devices'][args.name]['aliases']) or '(none)'}")
    print(f"Token  : {token}")
    print()
    print("On that machine, run one of:")
    print(f"  echo {token} > client/token")
    print(f"  setx JARVIS_TOKEN {token}        (Windows, new shell needed)")
    print()
    print("The hub reads secrets.yaml at startup -- restart it to pick this up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
Allowlist enforcement.

Every action funnels through here before touching the machine. The rules are
deliberately boring: resolve to a real absolute path, then require that path
to sit inside a directory the user explicitly listed. No globs, no "..",
no symlink escapes.
"""
from __future__ import annotations

import difflib
from pathlib import Path


class Refused(Exception):
    """Spoken back to the user verbatim, so the message must read aloud well."""


def _roots(allow: dict, kind: str) -> list[Path]:
    out = []
    for entry in allow.get("paths", {}).get(kind, []):
        try:
            out.append(Path(entry).expanduser().resolve(strict=False))
        except (OSError, ValueError):
            continue
    return out


def check_path(allow: dict, raw: str, *, write: bool = False) -> Path:
    """Resolve `raw` and confirm it is inside an allowed root."""
    if not raw or not str(raw).strip():
        raise Refused("I need a folder or file name.")

    try:
        # strict=False so we can still validate a path that does not exist yet
        # (creating a file in Downloads, for example).
        target = Path(str(raw)).expanduser().resolve(strict=False)
    except (OSError, ValueError, RuntimeError) as exc:
        raise Refused(f"That path doesn't make sense: {exc}") from exc

    kind = "writable" if write else "readable"
    roots = _roots(allow, kind)
    # Anything writable is implicitly readable too.
    if not write:
        roots = roots + _roots(allow, "writable")

    if not roots:
        raise Refused(f"No {kind} folders are configured on this machine.")

    for root in roots:
        try:
            target.relative_to(root)
            return target
        except ValueError:
            continue

    verb = "write to" if write else "look at"
    raise Refused(f"I'm not allowed to {verb} that folder on this machine.")


def resolve_app(allow: dict, spoken: str) -> tuple[str, str]:
    """Match a spoken app name against the allowlist. Returns (name, target)."""
    apps: dict[str, str] = allow.get("apps", {})
    if not apps:
        raise Refused("No applications are allowed on this machine yet.")

    needle = (spoken or "").lower().strip()
    for filler in ("the ", "app ", "application "):
        needle = needle.removeprefix(filler)
    needle = needle.removesuffix(" app").strip()

    if not needle:
        raise Refused("Which application?")

    if needle in apps:
        return needle, apps[needle]

    for name, target in apps.items():
        if needle in name.lower() or name.lower() in needle:
            return name, target

    close = difflib.get_close_matches(needle, list(apps), n=1, cutoff=0.7)
    if close:
        return close[0], apps[close[0]]

    known = ", ".join(sorted(apps)[:6])
    raise Refused(f"{spoken} isn't on the allowed list. I can open {known}.")


def check_closable(allow: dict, spoken: str) -> str:
    closable = [c.lower() for c in allow.get("closable", [])]
    if not closable:
        raise Refused("I'm not allowed to close anything on this machine.")
    needle = (spoken or "").lower().strip().removesuffix(".exe")
    if needle in closable:
        return needle
    close = difflib.get_close_matches(needle, closable, n=1, cutoff=0.7)
    if close:
        return close[0]
    raise Refused(f"I'm not allowed to close {spoken} on this machine.")


def limit(allow: dict, key: str, default: int) -> int:
    try:
        return int(allow.get("limits", {}).get(key, default))
    except (TypeError, ValueError):
        return default

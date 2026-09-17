"""
File actions, confined to the allowlisted roots.

Everything here is read-only except delete_files, which the hub gates behind
a spoken confirmation. Results carry a `speech` field because a folder listing
has to be said out loud, not printed -- so the counts matter more than the
full contents.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path

from .guard import Refused, check_path, limit

IS_WINDOWS = platform.system() == "Windows"


def _human_size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def list_dir(allow: dict, args: dict) -> dict:
    target = check_path(allow, args.get("path", ""))
    if not target.exists():
        raise Refused("That folder doesn't exist.")
    if not target.is_dir():
        raise Refused("That's a file, not a folder.")

    max_entries = limit(allow, "max_list_entries", 60)
    dirs: list[str] = []
    files: list[dict] = []
    try:
        for entry in os.scandir(target):
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                dirs.append(entry.name)
            else:
                try:
                    files.append({"name": entry.name, "size": entry.stat().st_size})
                except OSError:
                    files.append({"name": entry.name, "size": 0})
    except PermissionError as exc:
        raise Refused("Windows won't let me read that folder.") from exc

    files.sort(key=lambda f: f["name"].lower())
    dirs.sort(key=str.lower)

    total = len(dirs) + len(files)
    if total == 0:
        speech = f"{target.name} is empty."
    else:
        newest = sorted(
            files, key=lambda f: f["name"].lower()
        )[:5]
        bits = []
        if dirs:
            bits.append(f"{len(dirs)} folder{'s' if len(dirs) != 1 else ''}")
        if files:
            bits.append(f"{len(files)} file{'s' if len(files) != 1 else ''}")
        listing = ", ".join(f["name"] for f in newest)
        speech = f"{target.name} has {' and '.join(bits)}."
        if listing:
            speech += f" The first few are {listing}."

    return {
        "path": str(target),
        "dirs": dirs[:max_entries],
        "files": files[:max_entries],
        "truncated": total > max_entries,
        "speech": speech,
    }


def find_files(allow: dict, args: dict) -> dict:
    """Glob within one allowed root. Deliberately not recursive by default --
    a recursive search over a whole drive is a good way to hang the agent."""
    target = check_path(allow, args.get("path", ""))
    pattern = (args.get("pattern") or "*").strip()
    if ".." in pattern or pattern.startswith(("/", "\\")):
        raise Refused("That search pattern isn't allowed.")

    recursive = bool(args.get("recursive"))
    max_entries = limit(allow, "max_list_entries", 60)

    glob = target.rglob(pattern) if recursive else target.glob(pattern)
    hits: list[dict] = []
    for path in glob:
        try:
            # Re-check: rglob can follow a symlink outside the root.
            check_path(allow, str(path))
        except Refused:
            continue
        if path.is_file():
            hits.append({"name": path.name, "path": str(path),
                         "size": path.stat().st_size})
        if len(hits) >= max_entries:
            break

    if not hits:
        speech = f"I found nothing matching {pattern}."
    elif len(hits) == 1:
        speech = f"One match: {hits[0]['name']}."
    else:
        speech = f"{len(hits)} matches, including {', '.join(h['name'] for h in hits[:4])}."

    return {"matches": hits, "speech": speech}


def read_file(allow: dict, args: dict) -> dict:
    target = check_path(allow, args.get("path", ""))
    if not target.is_file():
        raise Refused("That isn't a file I can read.")

    max_chars = limit(allow, "max_read_chars", 4000)
    try:
        text = target.read_text(encoding="utf-8", errors="replace")[:max_chars]
    except OSError as exc:
        raise Refused(f"I couldn't read that file: {exc}") from exc

    return {"path": str(target), "text": text,
            "speech": text[:600] if text.strip() else "That file is empty."}


def open_path(allow: dict, args: dict) -> dict:
    """Open a file or folder in its default application / Explorer."""
    target = check_path(allow, args.get("path", ""))
    if not target.exists():
        raise Refused("That file or folder doesn't exist.")
    try:
        if IS_WINDOWS:
            os.startfile(str(target))  # noqa: S606 - path validated above
        else:
            subprocess.Popen(["xdg-open", str(target)])
    except OSError as exc:
        raise Refused(f"I couldn't open that: {exc}") from exc
    return {"opened": str(target), "speech": f"Opened {target.name}."}


def delete_files(allow: dict, args: dict) -> dict:
    """Delete inside a writable root. The hub requires spoken confirmation
    before this is ever reached."""
    raw = args.get("paths") or ([args["path"]] if args.get("path") else [])
    if not raw:
        raise Refused("I need to know which files to delete.")

    max_batch = limit(allow, "max_delete_batch", 10)
    if len(raw) > max_batch:
        raise Refused(f"That's more than {max_batch} files. I won't do that in one go.")

    targets = [check_path(allow, p, write=True) for p in raw]
    deleted: list[str] = []
    for target in targets:
        if not target.exists():
            continue
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            deleted.append(target.name)
        except OSError as exc:
            raise Refused(f"I couldn't delete {target.name}: {exc}") from exc

    if not deleted:
        return {"deleted": [], "speech": "There was nothing there to delete."}
    if len(deleted) == 1:
        return {"deleted": deleted, "speech": f"Deleted {deleted[0]}."}
    return {"deleted": deleted, "speech": f"Deleted {len(deleted)} items."}

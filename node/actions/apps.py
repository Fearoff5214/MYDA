"""Launching and closing applications, within the allowlist."""
from __future__ import annotations

import os
import platform
import subprocess
import sys

from .guard import Refused, resolve_app, check_closable

IS_WINDOWS = platform.system() == "Windows"


def open_app(allow: dict, args: dict) -> dict:
    name, target = resolve_app(allow, args.get("name", ""))

    try:
        if target.startswith(("shell:", "http://", "https://", "ms-")):
            # Store apps and URLs go through the shell handler.
            if IS_WINDOWS:
                os.startfile(target)  # noqa: S606 - target came from the allowlist
            else:
                subprocess.Popen(["xdg-open", target])
        else:
            # DETACHED so the app outlives the agent; no shell involved, and
            # the executable path is one we wrote into the allowlist ourselves.
            flags = subprocess.DETACHED_PROCESS if IS_WINDOWS else 0
            subprocess.Popen(
                [target],
                creationflags=flags,
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except FileNotFoundError as exc:
        raise Refused(f"{name} is on the list but isn't installed here.") from exc
    except OSError as exc:
        raise Refused(f"I couldn't start {name}: {exc}") from exc

    return {"opened": name, "speech": f"Opened {name}."}


def close_app(allow: dict, args: dict) -> dict:
    name = check_closable(allow, args.get("name", ""))
    exe = name if name.endswith(".exe") else f"{name}.exe"

    if IS_WINDOWS:
        proc = subprocess.run(
            ["taskkill", "/IM", exe, "/F"],
            capture_output=True, text=True, timeout=15,
        )
    else:
        proc = subprocess.run(["pkill", "-f", name], capture_output=True,
                              text=True, timeout=15)

    if proc.returncode != 0:
        return {"closed": None, "speech": f"{name} wasn't running."}
    return {"closed": name, "speech": f"Closed {name}."}


def list_apps(allow: dict, args: dict) -> dict:
    """What this machine is allowed to open. Useful when a request is refused."""
    names = sorted(allow.get("apps", {}))
    if not names:
        return {"apps": [], "speech": "No applications are set up on this machine."}
    spoken = ", ".join(names[:12])
    more = f", and {len(names) - 12} more" if len(names) > 12 else ""
    return {"apps": names, "speech": f"I can open {spoken}{more}."}

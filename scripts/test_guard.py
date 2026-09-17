"""
Security tests for the node allowlist.

This is the boundary that stops a misheard command or a confused model from
reaching arbitrary software and files, so it gets tested on its own, with no
network, no models and no hub.

    python scripts/test_guard.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from node.actions.guard import Refused, check_closable, check_path, resolve_app

ALLOW = {
    "apps": {"chrome": "C:/x/chrome.exe", "spotify": "shell:app", "notepad": "notepad.exe"},
    "closable": ["chrome", "notepad"],
    "paths": {
        "readable": ["C:/Users/param/Downloads", "D:/jarvis"],
        "writable": ["C:/Users/param/Documents/jarvis-scratch"],
    },
    "limits": {},
}

PATH_CASES = [
    # (path, write, expected)
    ("C:/Users/param/Downloads/a.pdf", False, "ALLOW"),
    ("C:/Users/param/Downloads/sub/deep.txt", False, "ALLOW"),
    ("C:/Users/param/Documents/jarvis-scratch/x", True, "ALLOW"),
    # writable roots are implicitly readable
    ("C:/Users/param/Documents/jarvis-scratch/x", False, "ALLOW"),
    # traversal must not escape the root
    ("C:/Users/param/Downloads/../../../Windows/System32", False, "REFUSE"),
    ("C:/Users/param/Downloads/../.ssh/id_rsa", False, "REFUSE"),
    ("C:/Windows/System32/config", False, "REFUSE"),
    # readable is not writable
    ("C:/Users/param/Downloads/a.pdf", True, "REFUSE"),
    ("", False, "REFUSE"),
    (None, False, "REFUSE"),
]

APP_CASES = [
    ("chrome", "ALLOW"), ("Chrome", "ALLOW"), ("the chrome app", "ALLOW"),
    ("chrom", "ALLOW"), ("spotify", "ALLOW"),
    ("regedit", "REFUSE"), ("cmd", "REFUSE"), ("", "REFUSE"),
]

CLOSE_CASES = [("chrome", "ALLOW"), ("notepad", "ALLOW"),
               ("spotify", "REFUSE"), ("explorer", "REFUSE")]


def verdict(fn, *a, **kw) -> str:
    try:
        fn(*a, **kw)
        return "ALLOW"
    except Refused:
        return "REFUSE"


def main() -> int:
    failures = 0

    print("path allowlist")
    for raw, write, expected in PATH_CASES:
        got = verdict(check_path, ALLOW, raw, write=write)
        ok = got == expected
        failures += not ok
        mode = "w" if write else "r"
        print(f"  [{'ok ' if ok else 'FAIL'}] ({mode}) {str(raw)[:50]:<50} {got}")

    print("\napp allowlist")
    for spoken, expected in APP_CASES:
        got = verdict(resolve_app, ALLOW, spoken)
        ok = got == expected
        failures += not ok
        print(f"  [{'ok ' if ok else 'FAIL'}] {spoken!r:<20} {got}")

    print("\nclosable list (narrower than launchable on purpose)")
    for spoken, expected in CLOSE_CASES:
        got = verdict(check_closable, ALLOW, spoken)
        ok = got == expected
        failures += not ok
        print(f"  [{'ok ' if ok else 'FAIL'}] {spoken!r:<20} {got}")

    total = len(PATH_CASES) + len(APP_CASES) + len(CLOSE_CASES)
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

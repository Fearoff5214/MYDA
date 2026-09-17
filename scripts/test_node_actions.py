"""
Node action tests against the real filesystem.

`test_guard.py` checks the allowlist logic in isolation with a synthetic
config. This runs the actual handlers against this machine's real
`node/allowlist.yaml` and real directories, which is where path-resolution
mistakes actually show up (drive letters, case, UNC, trailing slashes).

Read-only except for one delete, which is performed inside the configured
writable root on a file this script creates itself.

    python scripts/test_node_actions.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from node.actions import apps, files
from node.actions.guard import Refused
from node.agent import load_allowlist

ALLOWLIST = ROOT / "node" / "allowlist.yaml"


def run(label: str, fn, args: dict, expect: str) -> bool:
    """expect is 'ok' or 'refuse'."""
    try:
        data = fn(ALLOW, args)
        got = "ok"
        detail = str(data.get("speech", data))[:66]
    except Refused as exc:
        got = "refuse"
        detail = str(exc)[:66]
    except Exception as exc:  # noqa: BLE001 - an unexpected raise is a failure
        got = f"raised {type(exc).__name__}"
        detail = str(exc)[:66]

    ok = got == expect
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label:<44} {got:<8} {detail}")
    return ok


if not ALLOWLIST.exists():
    sys.exit(f"No {ALLOWLIST}. Copy allowlist.example.yaml and edit it first.")

ALLOW = load_allowlist(ALLOWLIST)
READABLE = ALLOW["paths"]["readable"]
WRITABLE = ALLOW["paths"]["writable"]


def main() -> int:
    print(f"allowlist: {len(ALLOW['apps'])} apps, "
          f"{len(READABLE)} readable, {len(WRITABLE)} writable\n")
    passed = 0
    total = 0

    print("listing and reading inside allowed roots")
    cases = [
        ("list an allowed folder", files.list_dir, {"path": READABLE[0]}, "ok"),
        ("list a system folder", files.list_dir, {"path": "C:/Windows/System32"}, "refuse"),
        ("list outside any root", files.list_dir, {"path": "C:/Users/param/.ssh"}, "refuse"),
        ("traversal out of a root", files.list_dir,
         {"path": READABLE[0] + "/../../../Windows"}, "refuse"),
        ("list a missing folder", files.list_dir,
         {"path": READABLE[0] + "/definitely-not-here"}, "refuse"),
        ("glob inside a root", files.find_files,
         {"path": READABLE[0], "pattern": "*.*"}, "ok"),
        ("glob with a traversal pattern", files.find_files,
         {"path": READABLE[0], "pattern": "../*"}, "refuse"),
    ]
    for label, fn, args, expect in cases:
        total += 1
        passed += run(label, fn, args, expect)

    print("\nwrite boundary (readable is not writable)")
    scratch = Path(WRITABLE[0])
    scratch.mkdir(parents=True, exist_ok=True)
    victim = scratch / "delete-me.txt"
    victim.write_text("temporary test file", encoding="utf-8")

    protected = Path(READABLE[0]) / "should-never-be-deleted.txt"
    write_cases = [
        ("delete inside writable root", files.delete_files,
         {"paths": [str(victim)]}, "ok"),
        ("delete inside readable-only root", files.delete_files,
         {"paths": [str(protected)]}, "refuse"),
        ("delete a system file", files.delete_files,
         {"paths": ["C:/Windows/System32/drivers/etc/hosts"]}, "refuse"),
        ("delete more than the batch limit", files.delete_files,
         {"paths": [str(scratch / f"f{i}") for i in range(99)]}, "refuse"),
    ]
    for label, fn, args, expect in write_cases:
        total += 1
        passed += run(label, fn, args, expect)

    deleted = not victim.exists()
    total += 1
    passed += deleted
    print(f"  [{'ok  ' if deleted else 'FAIL'}] {'the deletable file is actually gone':<44} "
          f"{'yes' if deleted else 'STILL THERE'}")

    print("\napp resolution against this machine's allowlist")
    app_cases = [
        ("a listed app", apps.list_apps, {}, "ok"),
    ]
    for label, fn, args, expect in app_cases:
        total += 1
        passed += run(label, fn, args, expect)

    print(f"\n{passed}/{total} passed")
    return 1 if passed != total else 0


if __name__ == "__main__":
    sys.exit(main())

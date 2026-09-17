"""
Preflight check. Run this first on any machine before anything else.

Tells you exactly what is missing and the command to fix it, instead of
leaving you to decode a stack trace from `python -m hub.server`.

    python scripts/doctor.py            # this machine, whatever role
    python scripts/doctor.py --hub      # also check hub-only requirements
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

OK, WARN, BAD = "ok", "warn", "MISSING"
results: list[tuple[str, str, str]] = []


def check(label: str, status: str, detail: str = "") -> None:
    results.append((label, status, detail))


def py_version() -> None:
    v = sys.version_info
    if v[:2] == (3, 13):
        check("Python 3.13", OK, platform.python_version())
    elif v[:2] == (3, 14):
        check("Python", BAD, f"{platform.python_version()} -- no CTranslate2 wheels. Use 3.13.")
    elif v < (3, 11):
        check("Python", BAD, f"{platform.python_version()} -- too old, needs 3.11+")
    else:
        check("Python", WARN, f"{platform.python_version()} -- 3.13 is what this is tested on")


def modules(names: list[str], group: str) -> None:
    for name in names:
        try:
            importlib.import_module(name)
            check(f"{group}: {name}", OK)
        except ImportError as exc:
            hint = str(exc)
            if "Application Control policy" in hint:
                check(f"{group}: {name}", WARN,
                      "blocked by Windows Smart App Control (see README)")
            else:
                check(f"{group}: {name}", BAD,
                      f"pip install -r requirements-{'hub' if group == 'hub' else 'client'}.txt")


def binaries() -> None:
    for exe, why in (("ffmpeg", "audio decoding"), ("tailscale", "reaching your other devices")):
        path = shutil.which(exe)
        check(exe, OK if path else WARN, path or f"not installed -- needed for {why}")


def smart_app_control() -> None:
    if platform.system() != "Windows":
        return
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-ItemProperty 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\CI\\Policy' "
             "-Name VerifiedAndReputablePolicyState -EA SilentlyContinue)"
             ".VerifiedAndReputablePolicyState"],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return
    if out == "1":
        check("Smart App Control", WARN,
              "ENFORCED -- blocks Piper speech and the wake word; falls back to Windows speech")
    elif out in ("0", "2", ""):
        check("Smart App Control", OK, "off -- Piper and wake word can run")


def gpu() -> None:
    try:
        import ctranslate2
    except ImportError:
        return
    n = ctranslate2.get_cuda_device_count()
    if n:
        try:
            name = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=20).stdout.strip().splitlines()[0]
        except (OSError, subprocess.SubprocessError, IndexError):
            name = f"{n} device(s)"
        check("CUDA", OK, name)
        if "MiB" in name:
            try:
                mb = int(name.split(",")[-1].strip().split()[0])
                if mb < 12000:
                    check("VRAM", WARN,
                          f"{mb} MiB -- use qwen3:4b or 8b, not 14b; "
                          "and stt.model distil-large-v3")
                elif mb < 20000:
                    check("VRAM", OK, f"{mb} MiB -- qwen3:14b fits")
                else:
                    check("VRAM", OK, f"{mb} MiB -- qwen3:32b fits, edit llm.model")
            except (ValueError, IndexError):
                pass
    else:
        check("CUDA", WARN, "no GPU visible -- set stt.device to cpu (much slower)")


def ollama(base_url: str, want_model: str) -> None:
    try:
        with urllib.request.urlopen(f"{base_url}/api/tags", timeout=5) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError):
        check("Ollama", BAD,
              f"not reachable at {base_url} -- install from https://ollama.com/download")
        return
    names = {m.get("name") or m.get("model", "") for m in data.get("models", [])}
    check("Ollama", OK, f"{len(names)} model(s) pulled")
    if any(n.split(":")[0] == want_model.split(":")[0] for n in names):
        check(f"model {want_model}", OK)
    else:
        check(f"model {want_model}", BAD,
              f"ollama pull {want_model}   (or edit llm.model in hub/config.yaml)")


def config_and_secrets(is_hub: bool) -> None:
    cfg_path = ROOT / "hub" / "config.yaml"
    if not cfg_path.exists():
        check("hub/config.yaml", BAD, "missing")
        return
    import yaml
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    check("hub/config.yaml", OK)

    host = cfg.get("server", {}).get("host", "")
    if host in ("127.0.0.1", "localhost"):
        check("server.host", WARN,
              "127.0.0.1 -- only this machine can connect. Set your tailnet IP "
              "(tailscale ip -4) once other devices need in.")
    elif host == "0.0.0.0":
        check("server.host", WARN, "0.0.0.0 exposes the hub on every interface; prefer the tailnet IP")
    else:
        check("server.host", OK, host)

    secrets = ROOT / "hub" / "secrets.yaml"
    if not secrets.exists():
        check("device tokens", BAD if is_hub else WARN,
              "none -- run: python scripts/new_device.py <name>")
    else:
        data = yaml.safe_load(secrets.read_text(encoding="utf-8")) or {}
        devices = list(data.get("devices", {}))
        check("device tokens", OK, f"{len(devices)}: {', '.join(devices)}")
        for extra, why in (("google", "Gmail and Calendar"),
                           ("home_assistant", "smart home")):
            check(extra, OK if extra in data else WARN,
                  "configured" if extra in data else f"not set up -- {why} tools stay hidden")

    return cfg


def node_allowlist() -> None:
    path = ROOT / "node" / "allowlist.yaml"
    if not path.exists():
        check("node/allowlist.yaml", WARN,
              "absent -- this machine cannot be controlled. "
              "Copy allowlist.example.yaml and edit it.")
        return
    import yaml
    a = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    apps = a.get("apps", {}) or {}
    missing = [n for n, t in apps.items()
               if not str(t).startswith(("shell:", "http", "ms-"))
               and not Path(str(t)).exists() and not shutil.which(str(t))]
    check("node/allowlist.yaml", OK,
          f"{len(apps)} apps, {len(a.get('paths', {}).get('writable', []))} writable roots")
    if missing:
        check("allowlisted apps present", WARN,
              f"not installed here: {', '.join(missing)}")
    if (a.get("shell") or {}).get("enabled"):
        check("shell execution", WARN, "ENABLED -- voice can run arbitrary commands here")


def microphone() -> None:
    try:
        import sounddevice as sd
        default = sd.query_devices(kind="input")
        check("microphone", OK, default["name"][:48])
    except Exception as exc:  # noqa: BLE001
        check("microphone", WARN, f"no input device ({type(exc).__name__})")


def main() -> int:
    p = argparse.ArgumentParser(description="Jarvis preflight check")
    p.add_argument("--hub", action="store_true", help="also check hub-only requirements")
    args = p.parse_args()

    print(f"Jarvis doctor -- {platform.node()} ({platform.system()} {platform.release()})\n")

    py_version()
    smart_app_control()
    binaries()
    modules(["websockets", "numpy", "yaml", "httpx"], "core")
    modules(["fastapi", "faster_whisper", "piper", "apscheduler"], "hub")
    modules(["sounddevice", "pynput", "openwakeword"], "client")
    gpu()
    microphone()
    cfg = config_and_secrets(args.hub)
    node_allowlist()
    if args.hub and cfg:
        llm = cfg.get("llm", {})
        ollama(llm.get("base_url", "http://127.0.0.1:11434"), llm.get("model", "qwen3:14b"))

    width = max(len(l) for l, _, _ in results) + 2
    for label, status, detail in results:
        mark = {OK: "  ok  ", WARN: " warn ", BAD: "MISSING"}[status]
        print(f"[{mark}] {label:<{width}} {detail}")

    bad = sum(1 for _, s, _ in results if s == BAD)
    warn = sum(1 for _, s, _ in results if s == WARN)
    print(f"\n{len(results) - bad - warn} ok, {warn} warning(s), {bad} blocking")
    if bad:
        print("\nFix the MISSING lines above; warnings are usually fine to start with.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

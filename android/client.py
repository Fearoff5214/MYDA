"""
Jarvis Android client, for Termux.

This deliberately speaks the *same* WebSocket protocol as the PC node agents,
so the hub needs no Android-specific code at all. It runs two connections:

  /ws/node   receives commands  -> SMS, calls, alarms, notifications, clipboard
  /ws/voice  sends microphone audio, plays the spoken reply

Why Termux rather than a native app: it gives a persistent connection, real
microphone access, and every phone action below, with no Android development,
no Play Store, and no signing key. The cost is that Android's battery manager
will kill it unless you exempt it (see setup.sh).

Install Termux from F-Droid or GitHub -- NOT the Play Store. That build is
abandoned and its API bridge does not work.

    python client.py --hub ws://100.x.y.z:8080 --device phone
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import websockets

HERE = Path(__file__).resolve().parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("jarvis.android")

RECORD_SECONDS = 8
TERMUX_TIMEOUT = 25


class Refused(Exception):
    """Spoken back to the user verbatim."""


def termux(command: list[str], timeout: int = TERMUX_TIMEOUT) -> dict:
    """Run a termux-api command and parse its JSON output if there is any."""
    binary = command[0]
    if shutil.which(binary) is None:
        raise Refused(
            f"{binary} isn't installed. Run: pkg install termux-api, "
            "and install the Termux:API app."
        )
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise Refused("The phone didn't respond in time.") from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:160]
        raise Refused(f"That failed on the phone: {detail}")

    out = (proc.stdout or "").strip()
    if not out:
        return {}
    try:
        return {"result": json.loads(out)}
    except json.JSONDecodeError:
        return {"result": out}


# ---------------------------------------------------------------------------
# phone actions


def send_sms(args: dict) -> dict:
    number = str(args.get("number") or "").strip()
    message = str(args.get("message") or "").strip()
    if not number or not message:
        raise Refused("I need a number and a message.")
    termux(["termux-sms-send", "-n", number, message])
    return {"speech": "Message sent."}


def read_sms(args: dict) -> dict:
    limit = int(args.get("limit") or 5)
    out = termux(["termux-sms-list", "-l", str(limit)]).get("result") or []
    if not out:
        return {"speech": "No recent messages."}
    bits = [f"{m.get('number', 'unknown')} said {m.get('body', '')}" for m in out[:limit]]
    return {"messages": out, "speech": ". ".join(bits)}


def call_number(args: dict) -> dict:
    number = str(args.get("number") or "").strip()
    if not number:
        raise Refused("Who should I call?")
    termux(["termux-telephony-call", number])
    return {"speech": f"Calling {number}."}


def set_alarm(args: dict) -> dict:
    """termux-job-scheduler cannot set clock alarms, so this goes through the
    Android alarm intent, which any clock app handles."""
    hour = args.get("hour")
    minute = args.get("minute", 0)
    if hour is None:
        raise Refused("What time should the alarm be?")
    termux([
        "am", "start", "-a", "android.intent.action.SET_ALARM",
        "--ei", "android.intent.extra.alarm.HOUR", str(int(hour)),
        "--ei", "android.intent.extra.alarm.MINUTES", str(int(minute)),
        "--ez", "android.intent.extra.alarm.SKIP_UI", "true",
    ])
    return {"speech": f"Alarm set for {int(hour):02d}:{int(minute):02d}."}


def notifications(args: dict) -> dict:
    out = termux(["termux-notification-list"]).get("result") or []
    if not out:
        return {"speech": "Nothing new."}
    bits = [
        f"{n.get('title') or n.get('packageName', 'something')}: {n.get('content', '')}"
        for n in out[:6]
    ]
    return {"notifications": out,
            "speech": f"{len(out)} notifications. " + ". ".join(bits)}


def battery(args: dict) -> dict:
    info = termux(["termux-battery-status"]).get("result") or {}
    pct = info.get("percentage", "?")
    status = str(info.get("status", "")).lower()
    tail = " and charging" if status == "charging" else ""
    return {"battery": info, "speech": f"The phone is at {pct} percent{tail}."}


def clipboard_set(args: dict) -> dict:
    text = str(args.get("text") or "")
    if not text:
        raise Refused("What should I copy?")
    termux(["termux-clipboard-set", text])
    return {"speech": "Copied to the phone's clipboard."}


def toast(args: dict) -> dict:
    termux(["termux-toast", str(args.get("text") or "")])
    return {"speech": "Shown."}


def find_phone(args: dict) -> dict:
    """Ring at full volume even if silenced -- the point is to locate it."""
    termux(["termux-volume", "music", "15"])
    termux(["termux-vibrate", "-d", "3000"])
    return {"speech": "Ringing your phone."}


HANDLERS = {
    "send_sms": send_sms,
    "read_sms": read_sms,
    "call": call_number,
    "set_alarm": set_alarm,
    "notifications": notifications,
    "battery": battery,
    "clipboard_set": clipboard_set,
    "toast": toast,
    "find_phone": find_phone,
}


# ---------------------------------------------------------------------------
# connections


class AndroidClient:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        base = args.hub.rstrip("/")
        self.node_url = f"{base}/ws/node?device={args.device}&token={args.token}"
        self.voice_url = f"{base}/ws/voice?device={args.device}&token={args.token}"

    async def _node_loop(self) -> None:
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.node_url) as ws:
                    await ws.send(json.dumps({
                        "type": "hello",
                        "platform": "android-termux",
                        "capabilities": sorted(HANDLERS),
                        "aliases": self.args.alias or ["my phone", "the phone"],
                    }))
                    ack = json.loads(await ws.recv())
                    if ack.get("type") != "hello_ok":
                        log.error("hub refused the handshake: %s", ack)
                        return
                    backoff = 1.0
                    log.info("node channel up: %d phone actions", len(HANDLERS))

                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("type") != "command":
                            continue
                        action = msg.get("action", "")
                        handler = HANDLERS.get(action)
                        if handler is None:
                            reply = {"ok": False, "error": f"the phone cannot do '{action}'"}
                        else:
                            try:
                                data = await asyncio.to_thread(handler, msg.get("args") or {})
                                reply = {"ok": True, "data": data}
                            except Refused as exc:
                                reply = {"ok": False, "error": str(exc), "refused": True}
                            except Exception as exc:  # noqa: BLE001
                                log.exception("phone action %s failed", action)
                                reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                        await ws.send(json.dumps({"type": "result", "id": msg.get("id"), **reply}))

            except (OSError, websockets.exceptions.WebSocketException) as exc:
                log.warning("node channel lost (%s); retrying in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _record(self, path: Path, seconds: int) -> bytes:
        """Record via termux-microphone-record, then transcode to the 16kHz
        mono PCM the hub expects."""
        wav = path.with_suffix(".wav")
        raw = path.with_suffix(".pcm")
        for f in (wav, raw):
            f.unlink(missing_ok=True)

        await asyncio.to_thread(
            termux,
            ["termux-microphone-record", "-f", str(wav), "-l", str(seconds),
             "-r", "16000", "-c", "1", "-e", "wav"],
            seconds + 10,
        )
        await asyncio.sleep(seconds + 0.5)
        await asyncio.to_thread(termux, ["termux-microphone-record", "-q"], 10)

        if shutil.which("ffmpeg") is None:
            raise Refused("ffmpeg isn't installed. Run: pkg install ffmpeg")
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav),
             "-f", "s16le", "-ar", "16000", "-ac", "1", str(raw)],
            check=True, timeout=60,
        )
        return raw.read_bytes()

    async def ask(self, seconds: int) -> None:
        """One push-to-talk turn: record, send, speak the reply."""
        tmp = Path(os.environ.get("TMPDIR", "/data/data/com.termux/files/usr/tmp"))
        tmp.mkdir(parents=True, exist_ok=True)
        pcm = await self._record(tmp / "jarvis_utterance", seconds)
        log.info("recorded %.1fs", len(pcm) / 2 / 16000)

        async with websockets.connect(self.voice_url, max_size=8 * 1024 * 1024) as ws:
            await ws.send(json.dumps({"type": "utterance_start", "sample_rate": 16000}))
            for i in range(0, len(pcm), 8192):
                await ws.send(pcm[i:i + 8192])
            await ws.send(json.dumps({"type": "utterance_end"}))

            audio = bytearray()
            rate = 22050
            while True:
                message = await ws.recv()
                if isinstance(message, bytes):
                    audio.extend(message)
                    continue
                event = json.loads(message)
                kind = event.get("type")
                if kind == "transcript":
                    log.info("you said: %s", event["text"])
                elif kind == "reply":
                    log.info("jarvis: %s", event["text"])
                elif kind == "audio_start":
                    rate = event.get("sample_rate", 22050)
                elif kind in ("turn_end", "no_speech"):
                    break

        if audio:
            await asyncio.to_thread(self._play, bytes(audio), rate)

    @staticmethod
    def _play(pcm: bytes, rate: int) -> None:
        if shutil.which("ffplay") is None:
            log.warning("ffplay missing; cannot speak the reply")
            return
        subprocess.run(
            ["ffplay", "-autoexit", "-nodisp", "-loglevel", "quiet",
             "-f", "s16le", "-ar", str(rate), "-ac", "1", "-"],
            input=pcm, timeout=120,
        )

    async def run_daemon(self) -> None:
        log.info("phone node running as %s", self.args.device)
        await self._node_loop()


def resolve_token(explicit: str | None) -> str:
    if explicit:
        return explicit
    if env := os.environ.get("JARVIS_TOKEN"):
        return env
    token_file = HERE / "token"
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    sys.exit("No token. Pass --token, set JARVIS_TOKEN, or write android/token.")


def main() -> None:
    p = argparse.ArgumentParser(description="Jarvis Android client (Termux)")
    p.add_argument("--hub", default=os.environ.get("JARVIS_HUB", "ws://127.0.0.1:8080"))
    p.add_argument("--device", default=os.environ.get("JARVIS_DEVICE", "phone"))
    p.add_argument("--token", default=None)
    p.add_argument("--alias", action="append", default=[])
    p.add_argument("--ask", action="store_true",
                   help="record one utterance and exit (bind this to a widget)")
    p.add_argument("--seconds", type=int, default=RECORD_SECONDS)
    args = p.parse_args()
    args.token = resolve_token(args.token)

    client = AndroidClient(args)
    try:
        if args.ask:
            asyncio.run(client.ask(args.seconds))
        else:
            asyncio.run(client.run_daemon())
    except KeyboardInterrupt:
        log.info("bye")
    except Refused as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()

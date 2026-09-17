"""
Windows SAPI speech, used as a fallback when Piper cannot run.

Why this exists: Piper ships an unsigned native DLL (`espeakbridge`). On a
machine with Smart App Control or a WDAC user-mode policy enforced, Windows
refuses to load it and Piper fails at synthesis time with
"An Application Control policy has blocked this file". Smart App Control
cannot be switched back on once disabled without resetting Windows, so
demanding the user turn it off is not a reasonable requirement.

System.Speech is part of Windows, is signed, needs no download, and is
entirely local -- which is the constraint that actually matters here. It
sounds markedly worse than Piper. It is a fallback, not a preference.

Driven through PowerShell rather than pywin32 to avoid another dependency.
"""
from __future__ import annotations

import logging
import subprocess
import tempfile
import wave
from pathlib import Path

log = logging.getLogger("jarvis.tts.sapi")

SAMPLE_RATE = 22050   # matches Piper's medium voices, so clients need no change

# Text is passed via a file, never interpolated into the script, so quotes and
# apostrophes in a spoken reply cannot break or inject into the command.
_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$text = [System.IO.File]::ReadAllText($args[0], [System.Text.Encoding]::UTF8)
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    if ($args[2]) {
        try { $synth.SelectVoice($args[2]) } catch { }
    }
    $synth.Rate = [int]$args[3]
    $fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
        %RATE%,
        [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
        [System.Speech.AudioFormat.AudioChannel]::Mono)
    $synth.SetOutputToWaveFile($args[1], $fmt)
    $synth.Speak($text)
} finally {
    $synth.Dispose()
}
""".replace("%RATE%", str(SAMPLE_RATE))


def available() -> bool:
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Add-Type -AssemblyName System.Speech; "
             "(New-Object System.Speech.Synthesis.SpeechSynthesizer).Dispose(); 'ok'"],
            capture_output=True, text=True, timeout=30,
        )
        return proc.returncode == 0 and "ok" in proc.stdout
    except (OSError, subprocess.SubprocessError):
        return False


def voices() -> list[str]:
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Add-Type -AssemblyName System.Speech; "
             "(New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices() "
             "| ForEach-Object { $_.VoiceInfo.Name }"],
            capture_output=True, text=True, timeout=30,
        )
        return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    except (OSError, subprocess.SubprocessError):
        return []


def synthesize(text: str, voice: str = "", rate: int = 1) -> bytes:
    """Return int16 mono PCM at SAMPLE_RATE. Blocking; call in a thread.

    `rate` is SAPI's own -10..10 scale, not a multiplier.
    """
    text = (text or "").strip()
    if not text:
        return b""

    with tempfile.TemporaryDirectory(prefix="jarvis-sapi-") as tmp:
        tmpdir = Path(tmp)
        txt = tmpdir / "say.txt"
        wav = tmpdir / "say.wav"
        txt.write_text(text, encoding="utf-8")

        script = tmpdir / "say.ps1"
        script.write_text(_SCRIPT, encoding="utf-8")

        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(script), str(txt), str(wav), voice, str(int(rate))],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0 or not wav.exists():
            detail = (proc.stderr or proc.stdout or "").strip()[:200]
            raise RuntimeError(f"SAPI synthesis failed: {detail}")

        with wave.open(str(wav), "rb") as wf:
            if wf.getframerate() != SAMPLE_RATE or wf.getsampwidth() != 2:
                log.warning("SAPI returned %d Hz / %d-byte samples, not %d / 2",
                            wf.getframerate(), wf.getsampwidth(), SAMPLE_RATE)
            return wf.readframes(wf.getnframes())

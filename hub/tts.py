"""
Text to speech, via Piper.

Piper runs on CPU and is fast enough that we can synthesise sentence by
sentence and start playing the first one while the rest is still being made.
That is most of the perceived-latency win, so the public API is a generator
rather than a "give me the whole wav" call.

Piper's Python API changed shape at 1.3 (synthesize_stream_raw -> synthesize
yielding AudioChunk). Both are supported here because which one you get
depends on the wheel pip happens to resolve.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

import httpx

log = logging.getLogger("jarvis.tts")

VOICE_REPO = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
MODELS_DIR = Path(__file__).resolve().parent.parent / "models" / "piper"

# Split on sentence end, but keep short fragments glued to the next sentence --
# synthesising "Yes." alone wastes more time in overhead than it saves.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_MIN_CHUNK = 25


def _voice_url(voice: str) -> str:
    """en_GB-alan-medium -> en/en_GB/alan/medium/en_GB-alan-medium"""
    lang_country, speaker, quality = voice.split("-", 2)
    lang = lang_country.split("_")[0]
    return f"{VOICE_REPO}/{lang}/{lang_country}/{speaker}/{quality}/{voice}"


def split_sentences(text: str) -> list[str]:
    out: list[str] = []
    for part in _SENTENCE_END.split(text.strip()):
        if not part:
            continue
        if out and len(out[-1]) < _MIN_CHUNK:
            out[-1] = f"{out[-1]} {part}"
        else:
            out.append(part)
    return out


class Synthesizer:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.voice_name = cfg.get("voice", "en_GB-alan-medium")
        self.cfg = cfg
        self._voice = None
        self.sample_rate = 22050
        self._lock = asyncio.Lock()

    async def ensure_voice(self) -> tuple[Path, Path]:
        """Download the voice on first run. ~60MB, once."""
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        onnx = MODELS_DIR / f"{self.voice_name}.onnx"
        conf = MODELS_DIR / f"{self.voice_name}.onnx.json"
        base = _voice_url(self.voice_name)

        async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
            for path, url in ((conf, f"{base}.onnx.json"), (onnx, f"{base}.onnx")):
                if path.exists() and path.stat().st_size > 0:
                    continue
                log.info("downloading %s", path.name)
                resp = await client.get(url)
                resp.raise_for_status()
                path.write_bytes(resp.content)
                log.info("saved %s (%.1f MB)", path.name, len(resp.content) / 1e6)
        return onnx, conf

    def load(self, onnx: Path, conf: Path) -> None:
        """Blocking. Call once at startup."""
        from piper import PiperVoice

        self._voice = PiperVoice.load(str(onnx), config_path=str(conf))
        self.sample_rate = json.loads(conf.read_text(encoding="utf-8"))["audio"]["sample_rate"]
        log.info("piper voice %s loaded @ %d Hz", self.voice_name, self.sample_rate)

    def _synth_sync(self, text: str) -> bytes:
        """Return raw int16 mono PCM for one sentence."""
        voice = self._voice
        if voice is None:
            raise RuntimeError("Synthesizer.load() was never called")

        # piper-tts >= 1.3
        if hasattr(voice, "synthesize") and not hasattr(voice, "synthesize_stream_raw"):
            chunks: list[bytes] = []
            for chunk in voice.synthesize(text):
                chunks.append(getattr(chunk, "audio_int16_bytes", None) or bytes(chunk))
            return b"".join(chunks)

        # piper-tts < 1.3
        return b"".join(voice.synthesize_stream_raw(text))

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        """Yield PCM for each sentence as it is ready."""
        for sentence in split_sentences(text):
            async with self._lock:
                pcm = await asyncio.to_thread(self._synth_sync, sentence)
            if pcm:
                yield pcm

    async def synthesize(self, text: str) -> bytes:
        return b"".join([chunk async for chunk in self.stream(text)])

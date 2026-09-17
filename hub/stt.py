"""
Speech to text, via faster-whisper.

The model is loaded once at startup and stays resident -- reloading per
utterance would add seconds. Because there is a single GPU model, transcription
is serialised behind a lock and run in a worker thread so the event loop
(which is also serving WebSockets and TTS) never blocks.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import numpy as np

log = logging.getLogger("jarvis.stt")

# Whisper hallucinates these on silence or noise. Dropping them stops the
# assistant reacting to a door closing.
HALLUCINATIONS = {
    "", ".", "you", "thank you.", "thanks for watching!", "bye.",
    "thank you for watching!", "please subscribe.", "[blank_audio]",
    "subtitles by the amara.org community",
}


class Transcriber:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.sample_rate = 16000
        self._model = None
        self._lock = asyncio.Lock()

    def load(self) -> None:
        """Blocking. Call once at startup, off the event loop."""
        from faster_whisper import WhisperModel

        started = time.perf_counter()
        self._model = WhisperModel(
            self.cfg["model"],
            device=self.cfg.get("device", "cuda"),
            compute_type=self.cfg.get("compute_type", "float16"),
        )
        log.info(
            "whisper %s loaded on %s in %.1fs",
            self.cfg["model"], self.cfg.get("device"), time.perf_counter() - started,
        )

    async def warmup(self) -> None:
        """First inference is always slow; pay that cost at boot, not mid-sentence."""
        silence = np.zeros(self.sample_rate // 2, dtype=np.float32)
        await self.transcribe(silence)
        log.info("whisper warm")

    @staticmethod
    def pcm16_to_float32(raw: bytes) -> np.ndarray:
        """Clients send int16 PCM; Whisper wants float32 in [-1, 1]."""
        return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    async def transcribe(self, audio: np.ndarray) -> str:
        if self._model is None:
            raise RuntimeError("Transcriber.load() was never called")
        async with self._lock:
            return await asyncio.to_thread(self._transcribe_sync, audio)

    def _transcribe_sync(self, audio: np.ndarray) -> str:
        started = time.perf_counter()
        segments, _info = self._model.transcribe(
            audio,
            language=self.cfg.get("language", "en"),
            beam_size=self.cfg.get("beam_size", 1),
            vad_filter=self.cfg.get("vad_filter", True),
            condition_on_previous_text=False,  # stops drift across utterances
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        elapsed = time.perf_counter() - started
        duration = len(audio) / self.sample_rate
        log.info("stt %.2fs audio -> %.2fs compute: %r", duration, elapsed, text)

        if text.lower().strip() in HALLUCINATIONS:
            log.debug("discarded hallucination %r", text)
            return ""
        return text

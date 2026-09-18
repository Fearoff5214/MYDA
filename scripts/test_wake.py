"""
Repeatable check that the "hey jarvis" wake word loads and behaves.

Run:
    .venv\\Scripts\\python.exe scripts\\test_wake.py
    .venv\\Scripts\\python.exe scripts\\test_wake.py --vad-threshold 0.5
    .venv\\Scripts\\python.exe scripts\\test_wake.py --wav some_real_recording.wav

There is no microphone available to this test and nobody to speak into it, so
the positive clips are synthesised with Windows SAPI (hub/sapi.py, imported
read-only). **Synthetic speech is a weak proxy for a human voice.** A detection
here proves the model loads, runs, and responds to input that sounds like the
wake phrase -- it does not prove the detector will fire for a particular person
in a particular room. Treat the negative results (silence, noise, unrelated
speech) as the more trustworthy half of this report: a detector that fires on
"what is the weather tomorrow" is broken regardless of how it scores on a
synthesised "hey jarvis".

Pass `--wav path.wav` to score a real 16-bit mono recording instead; that is
the only way to get a conclusive answer.
"""
from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from client.wakeword import WakeWord, install_sklearn_shim, models_dir  # noqa: E402

SAMPLE_RATE = 16000
FRAME = 480          # 30 ms -- exactly what client/listener.py feeds

# Should fire.
POSITIVES = [
    "hey jarvis",
    "Hey Jarvis.",
    "hey jarvis, what time is it",
    "hey jarvis turn off the lights",
]

# Should not fire. Includes near-misses on purpose.
NEGATIVES = [
    "what is the weather tomorrow",
    "hello there, how are you doing today",
    "okay google, set a timer",
    "hey siri",
    "the quick brown fox jumps over the lazy dog",
    "hey there",                       # first word only
    "heavy harvest",                   # phonetic near-miss
    "hey charles",
    "play some music please",
]

# Reported but not scored either way, because there is no single right answer.
# The bare name "jarvis" is two thirds of the wake phrase and the model rates it
# very highly -- often above threshold. Whether that is a bug or a feature
# depends on how often you say "jarvis" in conversation without meaning to
# summon it. Recorded here so the behaviour is visible rather than a surprise.
BORDERLINE = [
    "jarvis",
    "hey jarvis is the name of the assistant",
]


def resample(pcm: np.ndarray, src_rate: int, dst_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Linear resample. Crude, but the wake word frontend only sees a mel
    spectrogram and this is good enough not to distort the phonetics."""
    if src_rate == dst_rate:
        return pcm.astype(np.int16)
    n_out = int(round(len(pcm) * dst_rate / src_rate))
    x_out = np.linspace(0, len(pcm) - 1, n_out)
    return np.interp(x_out, np.arange(len(pcm)), pcm.astype(np.float64)).astype(np.int16)


def pad(pcm: np.ndarray, seconds: float = 1.0) -> np.ndarray:
    """Silence either side, so the streaming feature buffer has room to settle."""
    quiet = np.zeros(int(SAMPLE_RATE * seconds), dtype=np.int16)
    return np.concatenate([quiet, pcm, quiet])


def stream(wake: WakeWord, pcm: np.ndarray) -> tuple[float, int]:
    """Feed a clip in 30 ms frames like the real client does.

    Returns (peak score, number of frames that would have fired).
    """
    wake.reset()
    wake._last_fire = 0.0  # noqa: SLF001 - clear the refractory period between clips
    peak = 0.0
    fires = 0
    for start in range(0, len(pcm) - FRAME + 1, FRAME):
        frame = pcm[start:start + FRAME].tobytes()
        if wake.feed(frame):
            fires += 1
            wake._last_fire = 0.0  # noqa: SLF001 - count every crossing, not one per clip
        peak = max(peak, wake.last_score)
    return peak, fires


def sapi_clip(text: str, voice: str, rate: int) -> np.ndarray:
    from hub import sapi

    raw = sapi.synthesize(text, voice=voice, rate=rate)
    pcm = np.frombuffer(raw, dtype=np.int16)
    return pad(resample(pcm, sapi.SAMPLE_RATE))


def load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as f:
        if f.getsampwidth() != 2:
            sys.exit(f"{path}: need 16-bit PCM")
        data = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16)
        if f.getnchannels() > 1:
            data = data.reshape(-1, f.getnchannels())[:, 0].copy()
        return pad(resample(data, f.getframerate()))


def row(label: str, peak: float, fires: int, threshold: float, want_fire: bool | None) -> bool:
    if want_fire is None:
        verdict = "    "
        ok = True
    else:
        ok = (fires > 0) == want_fire
        verdict = " ok " if ok else "FAIL"
    print(f"  [{verdict}] {peak:7.4f}  fires={fires:<3d} {label}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="hey_jarvis")
    ap.add_argument("--threshold", type=float, default=0.6)
    ap.add_argument("--vad-threshold", type=float, default=0.0)
    ap.add_argument("--voice", default="", help="SAPI voice name; default is the system voice")
    ap.add_argument("--rates", default="-2,0,2", help="SAPI speaking rates to try")
    ap.add_argument("--wav", action="append", default=[],
                    help="score a real recording instead of / as well as SAPI clips")
    args = ap.parse_args()

    print("== environment ==")
    print(f"  python           {sys.version.split()[0]}")
    shimmed = install_sklearn_shim()
    print(f"  sklearn shim     {'installed' if shimmed else 'not needed'}")
    try:
        import onnxruntime
        print(f"  onnxruntime      {onnxruntime.__version__}")
    except Exception as exc:  # noqa: BLE001
        print(f"  onnxruntime      UNAVAILABLE: {exc}")
        return 1

    print("\n== loading model ==")
    try:
        wake = WakeWord(args.model, args.threshold, vad_threshold=args.vad_threshold)
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED: {type(exc).__name__}: {exc}")
        return 1
    print(f"  models dir       {models_dir()}")
    print(f"  sklearn imported {'sklearn' in sys.modules}  (must be False)")
    print(f"  threshold        {args.threshold}   vad={args.vad_threshold}")

    passed = True

    print("\n== non-speech (must never fire) ==")
    rng = np.random.default_rng(0)
    silence = np.zeros(SAMPLE_RATE * 5, dtype=np.int16)
    passed &= row("digital silence, 5 s", *stream(wake, silence), args.threshold, False)
    noise = (rng.normal(0, 3000, SAMPLE_RATE * 5)).astype(np.int16)
    passed &= row("white noise, 5 s", *stream(wake, noise), args.threshold, False)
    t = np.arange(SAMPLE_RATE * 3) / SAMPLE_RATE
    tone = (8000 * np.sin(2 * np.pi * 440 * t)).astype(np.int16)
    passed &= row("440 Hz tone, 3 s", *stream(wake, tone), args.threshold, False)

    for path in args.wav:
        print(f"\n== recording: {path} ==")
        row(Path(path).name, *stream(wake, load_wav(Path(path))), args.threshold, None)

    from hub import sapi
    if not sapi.available():
        print("\nSAPI unavailable; skipping synthesised speech.")
        return 0 if passed else 1

    voices = sapi.voices()
    voice = args.voice or (voices[0] if voices else "")
    rates = [int(r) for r in args.rates.split(",")]
    print(f"\n== synthesised speech (SAPI voice {voice!r}, rates {rates}) ==")
    print("   Synthetic speech is NOT a good stand-in for a human voice.")

    hits = 0
    total = 0
    for rate in rates:
        print(f"\n  -- should fire, rate {rate:+d} --")
        for text in POSITIVES:
            peak, fires = stream(wake, sapi_clip(text, voice, rate))
            total += 1
            hits += fires > 0
            row(repr(text), peak, fires, args.threshold, None)

        print(f"  -- must NOT fire, rate {rate:+d} --")
        for text in NEGATIVES:
            peak, fires = stream(wake, sapi_clip(text, voice, rate))
            passed &= row(repr(text), peak, fires, args.threshold, False)

        print(f"  -- borderline (informational), rate {rate:+d} --")
        for text in BORDERLINE:
            peak, fires = stream(wake, sapi_clip(text, voice, rate))
            row(repr(text), peak, fires, args.threshold, None)

    print("\n== summary ==")
    print(f"  false positives  {'none' if passed else 'PRESENT -- unusable'}")
    print(f"  synth detection  {hits}/{total} clips "
          f"({'encouraging' if hits else 'no detections'}; not conclusive -- "
          f"re-run with --wav on a real recording)")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())

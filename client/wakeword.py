"""
Wake word detection ("hey jarvis") for the Jarvis client.

Why this module exists
----------------------
This machine (and any Windows 11 box with Smart App Control enabled, or a WDAC
user-mode policy) refuses to load unsigned native extension DLLs. Check with::

    reg query "HKLM\\SYSTEM\\CurrentControlSet\\Control\\CI\\Policy" /v VerifiedAndReputablePolicyState

A value of 1 means enforced. Blocks show up in the Windows event log under
`Microsoft-Windows-CodeIntegrity/Operational` as event IDs 3033 and 3077.
Smart App Control cannot be re-enabled once turned off without reinstalling
Windows, so "just disable it" is not an acceptable fix.

openWakeWord itself is pure Python and its inference runs on **onnxruntime**,
which is signed and loads fine here. The problem is one line in the package's
`__init__.py`::

    from openwakeword.custom_verifier_model import train_custom_verifier

and `custom_verifier_model.py` in turn does, at module level::

    import scipy
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import FunctionTransformer, StandardScaler

scikit-learn's C extensions (`sklearn/utils/_isfinite.pyd`,
`sklearn/utils/sparsefuncs_fast.pyd`, ...) are unsigned, so the import dies
with "An Application Control policy has blocked this file" and the whole
package becomes unimportable -- even though nothing on the inference path
touches sklearn at all.

`custom_verifier_model` is a *training* helper. It builds a small personalised
logistic-regression classifier from your own recordings to reduce false
positives. We do not use it, we do not ship one, and `Model.predict` only
consults verifier models when you pass `custom_verifier_models=...` (we never
do). So the fix is to make that import a no-op.

What the shim below actually does
---------------------------------
Before `openwakeword` is imported for the first time, we insert a stub module
under the name `openwakeword.custom_verifier_model` into `sys.modules`. Python's
import machinery finds the name already present and never reads the real file,
so scipy and scikit-learn are never imported. Everything else in the package --
`Model`, `VAD`, `AudioFeatures`, the ONNX runners -- loads and runs unmodified.

This is a compatibility shim, not a fork: we do not patch, copy or vendor any
openWakeWord source. The one thing it costs us is `openwakeword.train_custom_verifier`,
which becomes a function that raises a clear error. If we ever want custom
verifier models on a machine where sklearn can load, drop the shim.

The shim is deliberately conservative: if `openwakeword` has already been
imported by someone else in this process, it does nothing and gets out of the
way.
"""
from __future__ import annotations

import logging
import sys
import time
import types
import urllib.request
from pathlib import Path

import numpy as np

log = logging.getLogger("jarvis.client.wake")

# openWakeWord's release assets. Its own downloader fetches both the tflite and
# the ONNX build of every model; we only ever run ONNX, and only one wake word,
# so we fetch exactly what we need.
_RELEASE = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1"

# Always needed: the shared audio frontend (mel spectrogram -> speech embedding).
_FEATURE_ASSETS = ("melspectrogram.onnx", "embedding_model.onnx")
# Only needed when --wake-vad-threshold is used.
_VAD_ASSET = "silero_vad.onnx"

# Wake word name -> release asset. Mirrors openwakeword.MODELS, but we cannot
# read that dict without importing the package, which is what we are trying to
# make safe in the first place.
_WAKE_ASSETS = {
    "alexa": "alexa_v0.1.onnx",
    "hey_mycroft": "hey_mycroft_v0.1.onnx",
    "hey_jarvis": "hey_jarvis_v0.1.onnx",
    "hey_rhasspy": "hey_rhasspy_v0.1.onnx",
    "timer": "timer_v0.1.onnx",
    "weather": "weather_v0.1.onnx",
}


def install_sklearn_shim() -> bool:
    """Neutralise openWakeWord's module-level scikit-learn import.

    Returns True if the stub was installed, False if it was not needed or came
    too late (openwakeword already imported). Safe to call more than once.
    """
    name = "openwakeword.custom_verifier_model"
    if name in sys.modules:
        return False
    if "openwakeword" in sys.modules:
        # Already imported successfully by someone else -- sklearn evidently
        # loads on this machine. Leave the real module alone.
        return False

    stub = types.ModuleType(name)
    stub.__doc__ = (
        "Stub installed by client/wakeword.py. The real module imports "
        "scikit-learn, whose unsigned native extensions Smart App Control "
        "blocks. Nothing on the wake word inference path needs it."
    )

    def train_custom_verifier(*_args, **_kwargs):
        raise NotImplementedError(
            "openwakeword.train_custom_verifier is disabled by the Jarvis "
            "client's scikit-learn shim (see client/wakeword.py). Train "
            "verifier models on a machine without Smart App Control."
        )

    stub.train_custom_verifier = train_custom_verifier
    sys.modules[name] = stub
    return True


def models_dir() -> Path:
    """Where openWakeWord looks for its ONNX files (inside the installed package)."""
    import openwakeword  # safe: the shim is installed before we get here

    return Path(openwakeword.__file__).resolve().parent / "resources" / "models"


def _fetch(asset: str, dest: Path, timeout: float = 120.0) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    url = f"{_RELEASE}/{asset}"
    log.info("downloading %s", asset)
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
        tmp.write_bytes(response.read())
    tmp.replace(dest)


def ensure_models(wake_model: str, want_vad: bool = False) -> Path:
    """Download the ONNX assets this wake word needs, if they are missing.

    Each file is around 1-2 MB. Returns the models directory.
    """
    if wake_model not in _WAKE_ASSETS:
        # A path to a custom .onnx model. Only the shared frontend is needed.
        wanted = list(_FEATURE_ASSETS)
    else:
        wanted = [*_FEATURE_ASSETS, _WAKE_ASSETS[wake_model]]
    if want_vad:
        wanted.append(_VAD_ASSET)

    directory = models_dir()
    for asset in wanted:
        target = directory / asset
        if target.exists() and target.stat().st_size > 0:
            continue
        _fetch(asset, target)
    return directory


class WakeWord:
    """openWakeWord wrapper. Optional -- absent unless --wake is passed.

    `feed` takes raw int16 PCM at 16 kHz. Frames need not be 80 ms: openWakeWord
    accumulates internally, at the cost of up to 80 ms of extra latency.
    """

    def __init__(
        self,
        model: str,
        threshold: float,
        vad_threshold: float = 0.0,
        refractory: float = 2.0,
    ) -> None:
        install_sklearn_shim()
        ensure_models(model, want_vad=vad_threshold > 0)

        from openwakeword.model import Model

        self.threshold = threshold
        self.name = model
        self.last_score = 0.0
        self._refractory = refractory
        self._last_fire = 0.0
        self.model = Model(
            wakeword_models=[model],
            inference_framework="onnx",
            vad_threshold=vad_threshold,
        )
        log.info(
            "wake word %r armed at threshold %.2f%s",
            model,
            threshold,
            f", VAD gate {vad_threshold:.2f}" if vad_threshold > 0 else "",
        )

    def score(self, pcm: bytes | np.ndarray) -> float:
        """Run one frame and return the highest model score, without firing."""
        samples = (
            pcm if isinstance(pcm, np.ndarray) else np.frombuffer(pcm, dtype=np.int16)
        )
        scores = self.model.predict(samples)
        self.last_score = float(max(scores.values())) if scores else 0.0
        return self.last_score

    def feed(self, pcm: bytes) -> bool:
        if self.score(pcm) < self.threshold:
            return False
        # Debounce: one utterance can cross the threshold on several frames.
        now = time.monotonic()
        if now - self._last_fire < self._refractory:
            return False
        self._last_fire = now
        self.model.reset()
        log.info("wake word fired (%.2f)", self.last_score)
        return True

    def reset(self) -> None:
        self.model.reset()

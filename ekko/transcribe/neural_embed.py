"""Neural speaker embeddings for diarization — fully local, no HF token.

Uses a small CAM++ speaker-verification model (3D-Speaker, trained on VoxCeleb)
run via onnxruntime. Unlike the hand-crafted MFCC fingerprints, these embeddings
are trained to be *speaker-discriminative*, so they actually separate voices on
real recordings (meetings, podcasts, YouTube) where MFCC features can't.

The model (~28 MB) is downloaded once to ~/.ekko/models and then runs offline.
Everything here is best-effort: if onnxruntime is missing, the download fails, or
inference errors, callers fall back to the MFCC path (see local_diarize).

Preprocessing mirrors what the model was trained on: audio → 16 kHz → 80-dim
kaldi-style log-mel fbank → per-utterance mean normalization → embedding.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import numpy as np

MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
             "speaker-recongition-models/"
             "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx")
MODEL_PATH = Path(os.path.expanduser("~/.ekko/models")) / "campplus_voxceleb_16k.onnx"

# Cosine-distance threshold below which two segments are treated as the same
# speaker (tuned for these CAM++ embeddings; MFCC uses a different, smaller one).
THRESHOLD = 0.65

_session = None
_lock = threading.Lock()


def _ensure_model() -> Path:
    if MODEL_PATH.exists() and MODEL_PATH.stat().st_size > 1_000_000:
        return MODEL_PATH
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    import urllib.request
    tmp = MODEL_PATH.with_suffix(".part")
    with urllib.request.urlopen(MODEL_URL, timeout=60) as r, open(tmp, "wb") as f:
        while chunk := r.read(1 << 16):
            f.write(chunk)
    if tmp.stat().st_size < 1_000_000:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("speaker model download looks truncated")
    tmp.replace(MODEL_PATH)
    return MODEL_PATH


def _get_session():
    global _session
    with _lock:
        if _session is None:
            import onnxruntime as ort
            path = _ensure_model()
            _session = ort.InferenceSession(str(path),
                                            providers=["CPUExecutionProvider"])
        return _session


# --- feature extraction (kaldi-fbank compatible, pure numpy) ----------------
def _resample_16k(sig: np.ndarray, sr: int) -> np.ndarray:
    if sr == 16000:
        return sig.astype(np.float64)
    if sr % 16000 == 0:                              # e.g. 48k, 32k → clean decimation
        f = sr // 16000
        taps = 8 * f + 1
        n = np.arange(taps) - (taps - 1) / 2
        h = np.sinc(n / f) / f * np.hamming(taps)
        h /= h.sum()
        return np.convolve(sig, h, mode="same")[::f]
    n_out = int(round(len(sig) * 16000 / sr))        # generic linear fallback
    return np.interp(np.linspace(0, len(sig), n_out, endpoint=False),
                     np.arange(len(sig)), sig)


def _mel_fb(sr: int, nfft: int, n_mels: int, low: float = 20.0,
            high: float | None = None) -> np.ndarray:
    high = high or sr / 2
    hz2mel = lambda f: 1127.0 * np.log(1.0 + f / 700.0)
    nbins = nfft // 2 + 1
    fftfreq = np.arange(nbins) * sr / nfft
    mlow, mhigh = hz2mel(low), hz2mel(high)
    delta = (mhigh - mlow) / (n_mels + 1)
    melf = hz2mel(fftfreq)
    fb = np.zeros((n_mels, nbins))
    for m in range(n_mels):
        left = mlow + m * delta
        center = left + delta
        right = left + 2 * delta
        fb[m] = np.maximum(0.0, np.minimum((melf - left) / (center - left),
                                           (right - melf) / (right - center)))
    return fb


def _fbank(sig: np.ndarray, sr: int = 16000, n_mels: int = 80,
           flen: int = 400, fshift: int = 160, preemph: float = 0.97):
    sig = sig * 32768.0                              # kaldi works in int16 range
    if len(sig) < flen:
        return None
    n_frames = 1 + (len(sig) - flen) // fshift       # snip_edges
    idx = np.arange(flen)[None, :] + fshift * np.arange(n_frames)[:, None]
    fr = sig[idx].astype(np.float64)
    fr = fr - fr.mean(axis=1, keepdims=True)         # remove_dc_offset
    pre = np.empty_like(fr)                          # preemphasis
    pre[:, 0] = fr[:, 0] - preemph * fr[:, 0]
    pre[:, 1:] = fr[:, 1:] - preemph * fr[:, :-1]
    fr = pre
    n = np.arange(flen)
    fr = fr * (0.5 - 0.5 * np.cos(2 * np.pi * n / (flen - 1))) ** 0.85   # povey window
    nfft = 1 << int(np.ceil(np.log2(flen)))
    power = np.abs(np.fft.rfft(fr, nfft)) ** 2
    return np.log(np.maximum(power @ _mel_fb(sr, nfft, n_mels).T, 1e-10))


def _embed_clip(sig16: np.ndarray):
    fb = _fbank(sig16)
    if fb is None or len(fb) < 10:                   # need ~0.1s of speech
        return None
    fb = fb - fb.mean(axis=0, keepdims=True)         # global-mean CMN (per the model)
    out = _get_session().run(None, {"x": fb[None].astype(np.float32)})[0][0]
    norm = float(np.linalg.norm(out))
    return out / norm if norm > 1e-8 else None       # L2-normalized → cosine space


def embed_segments(audio_path: Path, segments):
    """Return a list (aligned with `segments`) of L2-normalized 512-d speaker
    embeddings, or None per segment that's too short. Raises if the model/runtime
    is unavailable so the caller can fall back. Audio is read and resampled once."""
    import soundfile as sf
    y, sr = sf.read(str(audio_path), dtype="float32", always_2d=False)
    if getattr(y, "ndim", 1) > 1:
        y = y.mean(axis=1)
    y16 = _resample_16k(y, sr)
    _get_session()                                   # trigger download/load up front
    embs = []
    for s in segments:
        a = int(max(0.0, s.start) * 16000)
        b = int(max(s.start, s.end) * 16000)
        embs.append(_embed_clip(y16[a:b]) if b > a else None)
    return embs

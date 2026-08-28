"""Fully-local speaker diarization — no pyannote, no torch, no Hugging Face
token, no network.

It clusters the transcript's segments into Speaker A / B / C… by voice timbre,
using MFCC features (computed in pure numpy) and agglomerative clustering with
an automatic, data-driven guess at how many speakers there are. This is what
lets an online meeting captured as ONE mixed stream still come out with
distinct speakers — you just rename the anonymous clusters afterwards.

Quality is approximate: it reliably separates a handful of clearly-different
voices but won't match a trained model on overlaps or similar voices. For best
accuracy use the pyannote backend (`method = "pyannote"`). Everything here is
best-effort — any failure falls back to a single "Speaker A" rather than
breaking the pipeline.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from ..models import Transcript


def _letter(i: int) -> str:
    return chr(ord("A") + i) if i < 26 else f"S{i + 1}"


# --- MFCC (pure numpy) ------------------------------------------------------
def _mel_filterbank(sr: int, n_fft: int, n_mels: int) -> np.ndarray:
    def hz2mel(f):
        return 2595.0 * np.log10(1.0 + f / 700.0)

    def mel2hz(m):
        return 700.0 * (10.0 ** (m / 2595.0) - 1.0)

    n_bins = n_fft // 2 + 1
    mel_pts = np.linspace(hz2mel(0), hz2mel(sr / 2), n_mels + 2)
    bins = np.floor((n_fft + 1) * mel2hz(mel_pts) / sr).astype(int)
    bins = np.clip(bins, 0, n_bins - 1)
    fb = np.zeros((n_mels, n_bins), dtype=np.float32)
    for m in range(1, n_mels + 1):
        lo, ce, hi = bins[m - 1], bins[m], bins[m + 1]
        ce = max(ce, lo + 1)
        hi = max(hi, ce + 1)
        for k in range(lo, min(ce, n_bins)):
            fb[m - 1, k] = (k - lo) / (ce - lo)
        for k in range(ce, min(hi, n_bins)):
            fb[m - 1, k] = (hi - k) / (hi - ce)
    return fb


def _dct_matrix(n_mfcc: int, n_mels: int) -> np.ndarray:
    m = np.arange(n_mels)
    k = np.arange(n_mfcc)[:, None]
    d = np.cos(np.pi / n_mels * (m + 0.5) * k) * np.sqrt(2.0 / n_mels)
    d[0] *= 1.0 / np.sqrt(2.0)
    return d.astype(np.float32)      # n_mfcc x n_mels


def _mfcc(y: np.ndarray, sr: int, n_mfcc: int = 20, n_mels: int = 40):
    """MFCC frames (n_frames x n_mfcc) for a mono clip, or None if too short."""
    win = max(256, int(0.025 * sr))
    hop = max(128, int(0.010 * sr))
    n_fft = 1 << int(np.ceil(np.log2(win)))
    if y.size < win:
        return None
    y = np.append(y[0], y[1:] - 0.97 * y[:-1])          # pre-emphasis
    n_frames = 1 + (len(y) - win) // hop
    if n_frames < 1:
        return None
    idx = np.arange(win)[None, :] + hop * np.arange(n_frames)[:, None]
    frames = y[idx] * np.hamming(win)
    mag = np.abs(np.fft.rfft(frames, n_fft))
    powspec = (mag ** 2) / n_fft
    mel = powspec @ _mel_filterbank(sr, n_fft, n_mels).T
    logmel = np.log(mel + 1e-8)
    return logmel @ _dct_matrix(n_mfcc, n_mels).T


def _embed(clip: np.ndarray, sr: int):
    """A fixed-length voice fingerprint for a clip: mean+std of MFCCs (minus the
    c0 energy term, so it's loudness-robust), or None if the clip's too short."""
    m = _mfcc(clip, sr)
    if m is None or len(m) < 2:
        return None
    c = m[:, 1:]                                          # drop c0 (energy)
    return np.concatenate([c.mean(0), c.std(0)]).astype(np.float32)


# --- clustering -------------------------------------------------------------
def _l2(emb: np.ndarray) -> np.ndarray:
    # Plain L2 normalization → cosine distance. Deliberately NOT z-scored across
    # segments: with a single speaker the across-segment spread is just noise,
    # and dividing by it would amplify that noise into fake speakers.
    norm = np.linalg.norm(emb, axis=1, keepdims=True)
    norm[norm < 1e-8] = 1.0
    return emb / norm


def _cluster(emb: np.ndarray, max_speakers: int = 8, threshold: float = 0.30):
    """Agglomerative (average-linkage, cosine) clustering with an absolute
    distance threshold: merge clusters while their voices stay closer than
    `threshold`, stop once the nearest pair is farther apart (that's a real
    speaker change). One speaker → everything merges into one cluster; the
    `max_speakers` cap bounds over-splitting. Returns a per-segment label list."""
    n = len(emb)
    if n <= 1:
        return [0] * n
    x = _l2(emb)
    d = 1.0 - (x @ x.T)                                   # cosine distance
    np.fill_diagonal(d, np.inf)
    sizes = np.ones(n)
    active = np.ones(n, dtype=bool)
    members = {i: [i] for i in range(n)}
    remaining = n
    while remaining > 1:
        idx = np.where(active)[0]
        sub = d[np.ix_(idx, idx)]
        flat = int(np.argmin(sub))
        i, j = idx[flat // len(idx)], idx[flat % len(idx)]
        # Once the closest two clusters are distinct voices, stop — unless we're
        # still over the speaker cap, in which case keep merging.
        if float(d[i, j]) > threshold and remaining <= max_speakers:
            break
        ni, nj = sizes[i], sizes[j]
        d[i, :] = (ni * d[i, :] + nj * d[j, :]) / (ni + nj)   # Lance–Williams avg
        d[:, i] = d[i, :]
        d[i, i] = np.inf
        d[j, :] = np.inf
        d[:, j] = np.inf
        sizes[i] = ni + nj
        members[i] = members[i] + members[j]
        active[j] = False
        remaining -= 1

    labels = [0] * n
    for ci, root in enumerate(np.where(active)[0]):
        for mem in members[root]:
            labels[mem] = ci
    return labels


def _merge_singletons(emb: np.ndarray, labels: list[int]) -> list[int]:
    """Fold one-segment clusters into their nearest larger cluster — they're
    almost always a stray outlier (a laugh, music sting, a noisy segment), not a
    real extra speaker. Skipped when everything is a singleton."""
    from collections import Counter
    counts = Counter(labels)
    smalls = {c for c, n in counts.items() if n == 1}
    if not smalls or len(smalls) == len(counts):
        return labels
    x = _l2(emb)
    centroids = {c: _l2(np.stack([x[i] for i, l in enumerate(labels) if l == c])
                        .mean(0)[None])[0]
                 for c in counts if c not in smalls}
    out = list(labels)
    for i, l in enumerate(labels):
        if l in smalls:
            out[i] = max(centroids, key=lambda c: float(x[i] @ centroids[c]))
    return out


# --- entry point ------------------------------------------------------------
def diarize_local(audio_path: Path, transcript: Transcript,
                  max_speakers: int = 8, threshold: float | None = None) -> Transcript:
    segs = transcript.segments
    if not segs:
        return transcript

    def all_a():
        for s in segs:
            s.speaker = "Speaker A"
        return transcript

    # Prefer neural speaker embeddings (real separation); fall back to the
    # MFCC fingerprints if onnxruntime / the model / inference isn't available.
    embs, backend_threshold = None, 0.30
    try:
        from .neural_embed import THRESHOLD, embed_segments
        embs = embed_segments(audio_path, segs)
        backend_threshold = THRESHOLD
        if sum(e is not None for e in embs) < 2:     # nothing usable → try MFCC
            embs = None
    except Exception:
        embs = None

    if embs is None:                                 # MFCC fallback
        try:
            import soundfile as sf
            y, sr = sf.read(str(audio_path), dtype="float32", always_2d=False)
        except Exception:
            return all_a()
        if getattr(y, "ndim", 1) > 1:
            y = y.mean(axis=1)
        embs = []
        for s in segs:
            a = int(max(0.0, s.start) * sr)
            b = int(max(s.start, s.end) * sr)
            embs.append(_embed(y[a:b], sr) if b > a else None)
        backend_threshold = 0.30

    valid = [i for i, e in enumerate(embs) if e is not None]
    if len(valid) < 2:
        return all_a()

    thr = backend_threshold if threshold is None else threshold
    E = np.stack([embs[i] for i in valid])
    labels = _merge_singletons(E, _cluster(E, max_speakers, thr))
    by_seg = {vi: lab for vi, lab in zip(valid, labels)}

    # Renumber clusters to Speaker A/B/C in first-appearance order.
    order: dict[int, int] = {}
    for i in range(len(segs)):
        if i in by_seg and by_seg[i] not in order:
            order[by_seg[i]] = len(order)

    # Fill segments too short to fingerprint with the previous known speaker.
    last = by_seg[valid[0]]
    for i, s in enumerate(segs):
        if i in by_seg:
            last = by_seg[i]
        s.speaker = f"Speaker {_letter(order.get(last, 0))}"
    return transcript

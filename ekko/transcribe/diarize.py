"""Diarization overlay — assigns Speaker A/B/C to transcript segments.

Kept separate from transcription so it's a swappable/optional stage. Three
backends, selected by `method` (see [diarize] in config):

  * "local"    — fully on-device, no token, no extra installs (default). Clusters
                 segments by voice timbre; approximate but works out of the box,
                 even on a single mixed online-meeting stream. See local_diarize.
  * "pyannote" — best accuracy, but needs `pip install pyannote.audio` and a
                 Hugging Face token (the model is gated).
  * "off"      — every segment -> "Speaker A" (rename by hand later).

Naming those anonymous clusters with REAL names happens later, in identify/.
Every backend is best-effort: on any error it degrades rather than raising, so
a diarization problem never loses you the transcript.
"""
from __future__ import annotations

from pathlib import Path

from ..models import Transcript


class Diarizer:
    def __init__(self, method: str = "local", hf_token: str | None = None,
                 max_speakers: int = 8, threshold: float = 0.30):
        self.method = method
        self.hf_token = hf_token
        self.max_speakers = max_speakers
        self.threshold = threshold
        self._pipeline = None

    # --- routing ------------------------------------------------------------
    def apply(self, audio_path: Path, transcript: Transcript) -> Transcript:
        if not transcript.segments:
            return transcript
        if self.method == "off":
            return self._single(transcript)
        if self.method == "pyannote":
            try:
                return self._pyannote(audio_path, transcript)
            except Exception:
                return self._local(audio_path, transcript)   # still try to split
        # default: local
        return self._local(audio_path, transcript)

    # --- backends -----------------------------------------------------------
    def _single(self, transcript: Transcript) -> Transcript:
        for seg in transcript.segments:
            seg.speaker = "Speaker A"
        return transcript

    def _local(self, audio_path: Path, transcript: Transcript) -> Transcript:
        try:
            from .local_diarize import diarize_local
            return diarize_local(audio_path, transcript, self.max_speakers,
                                 self.threshold)
        except Exception:
            return self._single(transcript)

    def _load_pyannote(self):
        if self._pipeline is None:
            from pyannote.audio import Pipeline
            self._pipeline = Pipeline.from_pretrained(
                "pyannote/speaker-diarization-3.1", use_auth_token=self.hf_token)
        return self._pipeline

    def _pyannote(self, audio_path: Path, transcript: Transcript) -> Transcript:
        diar = self._load_pyannote()(str(audio_path))
        # Label each transcript segment with the speaker turn it overlaps most.
        turns = [(t.start, t.end, spk) for t, _, spk in diar.itertracks(yield_label=True)]
        for seg in transcript.segments:
            best, best_overlap = "Speaker A", 0.0
            for start, end, spk in turns:
                overlap = max(0.0, min(seg.end, end) - max(seg.start, start))
                if overlap > best_overlap:
                    best, best_overlap = f"Speaker {spk.split('_')[-1]}", overlap
            seg.speaker = best
        return transcript

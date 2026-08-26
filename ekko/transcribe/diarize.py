"""Optional diarization overlay — assigns Speaker A/B/C to transcript segments.

Kept separate from transcription so it's a swappable/optional stage. v1 can
run without it (every segment -> "Speaker A") and still produce useful notes;
turn it on once you've set up a Hugging Face token for pyannote.

Naming those anonymous clusters with REAL names happens later, in identify/.
"""
from __future__ import annotations

from pathlib import Path

from ..models import Transcript


class Diarizer:
    def __init__(self, hf_token: str | None = None, enabled: bool = False):
        self.hf_token = hf_token
        self.enabled = enabled
        self._pipeline = None

    def _load(self):
        if self._pipeline is None:
            from pyannote.audio import Pipeline
            self._pipeline = Pipeline.from_pretrained(
                "pyannote/speaker-diarization-3.1", use_auth_token=self.hf_token)
        return self._pipeline

    def apply(self, audio_path: Path, transcript: Transcript) -> Transcript:
        if not self.enabled:
            for seg in transcript.segments:
                seg.speaker = "Speaker A"
            return transcript

        diar = self._load()(str(audio_path))
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

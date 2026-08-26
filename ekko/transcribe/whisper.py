"""Local transcription with faster-whisper — runs fully on-device.

This is the privacy-critical stage: audio -> text never leaves the machine.
Diarization (who-spoke-when) is applied as an optional overlay in diarize.py.
"""
from __future__ import annotations

from pathlib import Path

from ..models import Segment, Transcript


class WhisperTranscriber:
    def __init__(self, model_size: str = "small", compute_type: str = "int8"):
        # "small"/"base" are the sweet spot on a 16GB M2; "large-v3" is best
        # quality but ~3GB resident — fine, but run stages sequentially.
        self.model_size = model_size
        self.compute_type = compute_type
        self._model = None

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            # CPU/Metal via CTranslate2; int8 keeps memory small.
            self._model = WhisperModel(self.model_size, device="auto",
                                       compute_type=self.compute_type)
        return self._model

    def transcribe(self, audio_path: Path) -> Transcript:
        model = self._load()
        segments, _info = model.transcribe(str(audio_path), vad_filter=True)
        out = Transcript()
        for s in segments:
            out.segments.append(Segment(start=s.start, end=s.end,
                                        text=s.text.strip()))
        return out

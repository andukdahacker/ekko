"""Manual speaker naming (v1 default, used for in-person).

Applies an optional {cluster_label: real_name} map. Anything unmapped keeps
its anonymous label, which you can rename after the fact. This is the simple,
honest v1 answer for in-person where there's no screen to read.

Later, a VoiceprintIdentifier can pre-fill this map by matching enrolled voice
embeddings, so recurring teammates get named automatically.
"""
from __future__ import annotations

from ..models import Transcript
from .base import SpeakerIdentifier


class ManualIdentifier(SpeakerIdentifier):
    def __init__(self, name_map: dict[str, str] | None = None):
        self.name_map = name_map or {}

    def name_speakers(self, transcript: Transcript) -> Transcript:
        for seg in transcript.segments:
            if seg.speaker in self.name_map:
                seg.speaker = self.name_map[seg.speaker]
        return transcript

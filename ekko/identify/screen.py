"""Online speaker naming via screen-watching (v2 stub, interface locked in v1).

The plan (see design notes): while an online meeting runs, periodically
screenshot the Meet/Teams window, detect the highlighted active-speaker tile,
OCR the name off it, and record (name, timestamp) events. name_speakers() then
maps each transcript segment to whoever's tile was lit during that segment.

Left as a stub so v1 stays laptop+manual, but the class exists so wiring it in
later is a config swap, not a refactor.
"""
from __future__ import annotations

from ..models import Transcript
from .base import SpeakerIdentifier


class ScreenIdentifier(SpeakerIdentifier):
    def __init__(self, active_speaker_events: list[tuple[float, str]] | None = None):
        # (timestamp_seconds, name) samples captured during the meeting.
        self.events = sorted(active_speaker_events or [])

    def name_speakers(self, transcript: Transcript) -> Transcript:
        if not self.events:
            return transcript  # nothing captured -> leave labels untouched
        for seg in transcript.segments:
            mid = (seg.start + seg.end) / 2
            name = self._name_at(mid)
            if name:
                seg.speaker = name
        return transcript

    def _name_at(self, t: float) -> str | None:
        chosen = None
        for ts, name in self.events:
            if ts <= t:
                chosen = name
            else:
                break
        return chosen

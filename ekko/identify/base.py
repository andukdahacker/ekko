"""Speaker identification interface — turns "Speaker A/B" into real names.

This is the seam for the online screen-watching trick (read the active-speaker
tile + OCR the name, correlate by timestamp) vs. the in-person manual/voiceprint
path. v1 ships the manual identifier; ScreenIdentifier is stubbed for online.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Transcript


class SpeakerIdentifier(ABC):
    @abstractmethod
    def name_speakers(self, transcript: Transcript) -> Transcript:
        """Replace anonymous `Speaker X` labels with real names in place."""

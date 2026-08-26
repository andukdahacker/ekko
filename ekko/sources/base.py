"""Audio source interface.

This is the seam that keeps "phone or laptop" a one-line swap. v1 ships the
laptop implementation; a phone source (watched-folder upload) implements the
same interface later with zero pipeline changes.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..models import MeetingKind


class AudioSource(ABC):
    """Produces a single mixed audio file for one meeting.

    We deliberately capture ONE stream (no dual-device), so downstream only
    ever handles one timeline — no cross-device clock alignment needed.
    """

    @abstractmethod
    def start(self, kind: MeetingKind) -> None:
        """Begin capturing. For ONLINE, implementations may capture system
        audio + mic mixed; for IN_PERSON, mic only."""

    @abstractmethod
    def stop(self) -> Path:
        """Stop capturing and return the path to the recorded audio file."""

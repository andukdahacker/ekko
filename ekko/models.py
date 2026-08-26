"""Core data structures passed between pipeline stages.

Everything downstream of capture speaks in these types, so the pipeline
doesn't care whether audio came from a laptop or a phone, or whether names
were resolved by screen-watching or left as anonymous speakers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path


class MeetingKind(str, Enum):
    ONLINE = "online"       # Meet / Teams — system audio, screen name-resolution available
    IN_PERSON = "in_person"  # single mic, no screen to read


@dataclass
class Segment:
    """One contiguous chunk of speech from a single speaker."""
    start: float            # seconds from meeting start
    end: float
    text: str
    speaker: str = "Speaker ?"   # "Speaker A" until identify stage names it, then a real name

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class Transcript:
    segments: list[Segment] = field(default_factory=list)

    def as_dialogue(self) -> str:
        """Flatten to `Name: text` lines — the format the summarizer consumes."""
        return "\n".join(f"{s.speaker}: {s.text}" for s in self.segments)


@dataclass
class ActionItem:
    text: str
    owner: str | None = None     # resolved to a real name when attribution succeeds


@dataclass
class Summary:
    tldr: str = ""
    key_points: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    action_items: list[ActionItem] = field(default_factory=list)


@dataclass
class Meeting:
    """The canonical record for one meeting, carried through the whole pipeline."""
    title: str
    kind: MeetingKind
    started_at: datetime
    audio_path: Path | None = None
    transcript: Transcript = field(default_factory=Transcript)
    summary: Summary | None = None
    id: int | None = None        # set once persisted

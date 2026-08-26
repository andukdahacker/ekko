"""Pipeline orchestration — wires the pluggable stages into one flow.

    audio source -> whisper -> diarize -> identify -> summarize -> store -> integrations

Every stage is an interface, so this file is the only place that knows the
concrete implementations, and it gets them via `build_pipeline` from config.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from .integrations.base import Integration
from .identify.base import SpeakerIdentifier
from .models import Meeting, MeetingKind
from .store.sqlite import SqliteStore
from .summarize.base import Summarizer
from .transcribe.diarize import Diarizer
from .transcribe.whisper import WhisperTranscriber


class Pipeline:
    def __init__(self, transcriber: WhisperTranscriber, diarizer: Diarizer,
                 identifier: SpeakerIdentifier, summarizer: Summarizer,
                 store: SqliteStore, integrations: list[Integration]):
        self.transcriber = transcriber
        self.diarizer = diarizer
        self.identifier = identifier
        self.summarizer = summarizer
        self.store = store
        self.integrations = integrations

    def process(self, audio_path, *, title: str, kind: MeetingKind,
                started_at: datetime | None = None,
                on_progress: Callable[[int, int, str], None] | None = None) -> Meeting:
        """Run a recorded audio file through the whole pipeline.

        `on_progress(step, total, label)` is called before each stage; it defaults
        to printing `[step/total] label…` so the CLI is unchanged. The TUI passes
        its own callback to render progress in a panel instead of stdout.
        """
        def emit(step: int, label: str) -> None:
            if on_progress is not None:
                on_progress(step, 5, label)
            else:
                print(f"[{step}/5] {label}…")

        m = Meeting(title=title, kind=kind,
                    started_at=started_at or datetime.now(), audio_path=audio_path)

        emit(1, "transcribing (local)")
        m.transcript = self.transcriber.transcribe(audio_path)

        emit(2, "diarizing")
        m.transcript = self.diarizer.apply(audio_path, m.transcript)

        emit(3, "naming speakers")
        m.transcript = self.identifier.name_speakers(m.transcript)

        emit(4, "summarizing")
        m.summary = self.summarizer.summarize(m.transcript)

        emit(5, "storing + publishing")
        self.store.save(m)
        for integ in self.integrations:
            integ.publish(m)
        return m

"""Local SQLite store — the canonical meeting record lives on-device.

Kept deliberately thin. This is the boundary that would later become a
self-hosted "notes service": swap this class for an HTTP client and the rest
of the pipeline is unchanged (audio still never leaves the device — only the
derived text would sync).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from ..models import (ActionItem, Meeting, MeetingKind, Segment, Summary,
                      Transcript)


class SqliteStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the TUI writes from a background worker thread
        # while reading from the UI thread. Access is effectively serialized (one
        # exclusive worker at a time), so this is safe here.
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS meetings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT, kind TEXT, started_at TEXT,
                audio_path TEXT, transcript_json TEXT, summary_json TEXT
            )""")
        self.db.commit()

    def save(self, m: Meeting) -> int:
        cur = self.db.execute(
            "INSERT INTO meetings (title, kind, started_at, audio_path, "
            "transcript_json, summary_json) VALUES (?,?,?,?,?,?)",
            (m.title, m.kind.value, m.started_at.isoformat(),
             str(m.audio_path) if m.audio_path else None,
             json.dumps([asdict(s) for s in m.transcript.segments]),
             json.dumps(asdict(m.summary)) if m.summary else None),
        )
        self.db.commit()
        m.id = cur.lastrowid
        return m.id

    def list_meetings(self) -> list[tuple[int, str, str]]:
        return list(self.db.execute(
            "SELECT id, started_at, title FROM meetings ORDER BY id DESC"))

    def get(self, meeting_id: int) -> Meeting | None:
        """Reconstruct a full Meeting (transcript + summary) from storage."""
        row = self.db.execute(
            "SELECT id, title, kind, started_at, audio_path, transcript_json, "
            "summary_json FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        if row is None:
            return None
        (mid, title, kind, started_at, audio_path, transcript_json,
         summary_json) = row

        segments = [Segment(**s) for s in json.loads(transcript_json or "[]")]
        summary = None
        if summary_json:
            d = json.loads(summary_json)
            summary = Summary(
                tldr=d.get("tldr", ""),
                key_points=d.get("key_points", []),
                decisions=d.get("decisions", []),
                action_items=[ActionItem(**a) for a in d.get("action_items", [])],
            )
        return Meeting(
            title=title, kind=MeetingKind(kind),
            started_at=datetime.fromisoformat(started_at),
            audio_path=Path(audio_path) if audio_path else None,
            transcript=Transcript(segments=segments), summary=summary, id=mid)

    def delete(self, meeting_id: int) -> None:
        self.db.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
        self.db.commit()

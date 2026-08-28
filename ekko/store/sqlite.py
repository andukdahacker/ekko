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

    def search(self, query: str) -> list[tuple[int, str, str]]:
        """Like list_meetings, but only rows matching `query` (case-insensitive).
        Matches the title, the date, and the *content* — transcript and summary
        are stored as JSON text, so a LIKE over those columns finds spoken words
        and summary points too, no separate full-text index needed."""
        like = f"%{query}%"
        return list(self.db.execute(
            "SELECT id, started_at, title FROM meetings "
            "WHERE title LIKE ? OR started_at LIKE ? "
            "OR transcript_json LIKE ? OR summary_json LIKE ? "
            "ORDER BY id DESC", (like, like, like, like)))

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

    def rename_speakers(self, meeting_id: int, mapping: dict[str, str]) -> None:
        """Rename speakers in a stored meeting in place: rewrites the transcript's
        speaker labels (and any action-item owner that matches a renamed speaker)
        per `mapping` = {old_label: new_name}. Persists without re-processing."""
        if not mapping:
            return
        row = self.db.execute(
            "SELECT transcript_json, summary_json FROM meetings WHERE id = ?",
            (meeting_id,)).fetchone()
        if row is None:
            return
        transcript_json, summary_json = row

        segs = json.loads(transcript_json or "[]")
        for s in segs:
            if s.get("speaker") in mapping:
                s["speaker"] = mapping[s["speaker"]]
        new_transcript = json.dumps(segs)

        new_summary = summary_json
        if summary_json:
            d = json.loads(summary_json)
            touched = False
            for a in d.get("action_items", []):
                if a.get("owner") in mapping:
                    a["owner"] = mapping[a["owner"]]
                    touched = True
            if touched:
                new_summary = json.dumps(d)

        self.db.execute(
            "UPDATE meetings SET transcript_json = ?, summary_json = ? WHERE id = ?",
            (new_transcript, new_summary, meeting_id))
        self.db.commit()

    def delete(self, meeting_id: int) -> None:
        self.db.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
        self.db.commit()

"""Markdown / Obsidian adapter (v1 integration).

Writes one Markdown file per meeting into a folder (point it at an Obsidian
vault). Zero external dependency, fully private — the simplest possible proof
of the adapter pattern.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..models import Meeting
from .base import Integration


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "meeting"


def render_note(m: Meeting) -> str:
    """Render a meeting to Markdown. Shared by the file integration and the TUI
    viewer so both show an identical note."""
    L: list[str] = []
    L.append(f"# {m.title}\n")
    L.append(f"- **When:** {m.started_at:%Y-%m-%d %H:%M}")
    L.append(f"- **Type:** {m.kind.value}\n")

    s = m.summary
    if s:
        if s.tldr:
            L.append(f"## TL;DR\n{s.tldr}\n")
        if s.key_points:
            L.append("## Key points")
            L += [f"- {p}" for p in s.key_points]
            L.append("")
        if s.decisions:
            L.append("## Decisions")
            L += [f"- {d}" for d in s.decisions]
            L.append("")
        if s.action_items:
            L.append("## Action items")
            for a in s.action_items:
                who = f" — **{a.owner}**" if a.owner else ""
                L.append(f"- [ ] {a.text}{who}")
            L.append("")

    L.append("## Transcript")
    # Group consecutive segments from the same speaker into one turn, and render
    # each turn as its own paragraph (blank line between) so speaker changes
    # actually break onto a new line — adjacent Markdown lines would otherwise
    # collapse into a single run-on paragraph.
    turns: list[tuple[str, list[str]]] = []
    for seg in m.transcript.segments:
        text = seg.text.strip()
        if turns and turns[-1][0] == seg.speaker:
            turns[-1][1].append(text)
        else:
            turns.append((seg.speaker, [text]))
    for speaker, texts in turns:
        L.append("")                                   # blank line = new paragraph
        L.append(f"**{speaker}:** {' '.join(texts).strip()}")
    return "\n".join(L) + "\n"


class MarkdownIntegration(Integration):
    def __init__(self, vault_dir: Path):
        self.vault_dir = vault_dir

    def publish(self, meeting: Meeting) -> None:
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        fname = f"{meeting.started_at:%Y-%m-%d}-{_slug(meeting.title)}.md"
        path = self.vault_dir / fname
        path.write_text(render_note(meeting), encoding="utf-8")
        print(f"[markdown] wrote {path}")

    def _render(self, m: Meeting) -> str:      # back-compat shim
        return render_note(m)

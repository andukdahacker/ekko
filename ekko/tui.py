"""ekko's terminal UI — a lazygit-style front end.

Layout:
  ┌ Meetings ─────┐┌ Details ──────────────┐
  │ list of past  ││ rendered note for the │
  │ meetings      ││ highlighted meeting   │
  ├ Audio ────────┤│                       │
  │ device status ││                       │
  │ + rec state   ││                       │
  └───────────────┘└───────────────────────┘
   r record · o online · s stop · p process · x delete · ? help · q quit

It drives the same building blocks as the CLI (config → source/pipeline/store,
audio_setup routing), so there's no duplicated logic. Blocking work (Whisper,
Gemini) runs in a thread worker via @work so the UI stays responsive.
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Label, Markdown, Static

from . import audio_setup as A
from .config import build_pipeline, build_source, load_config
from .integrations.markdown import render_note
from .models import MeetingKind
from .store.sqlite import SqliteStore


class ConfirmScreen(ModalScreen[bool]):
    """A tiny yes/no modal."""
    BINDINGS = [Binding("y", "yes", "Yes"), Binding("n,escape", "no", "No")]

    def __init__(self, message: str):
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._message)
            yield Label("[dim]y = yes   ·   n / esc = no[/]")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class PromptScreen(ModalScreen[str | None]):
    """A single-line text prompt (used for 'process file')."""
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, prompt: str, placeholder: str = ""):
        super().__init__()
        self._prompt = prompt
        self._placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._prompt)
            yield Input(placeholder=self._placeholder, id="prompt_input")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class EkkoApp(App):
    TITLE = "ekko"
    SUB_TITLE = "meeting notes"

    CSS = """
    #main { height: 1fr; }
    #left { width: 38%; }
    #meetings { height: 1fr; border: round $primary; }
    #audio { height: auto; min-height: 6; border: round $primary; padding: 0 1; }
    #details { width: 1fr; border: round $primary; padding: 0 1; }
    #dialog {
        width: 60; height: auto; padding: 1 2;
        border: thick $accent; background: $surface;
        align: center middle;
    }
    ConfirmScreen, PromptScreen { align: center middle; }
    """

    BINDINGS = [
        Binding("r", "record('in_person')", "Record"),
        Binding("o", "record('online')", "Online"),
        Binding("s", "stop", "Stop"),
        Binding("p", "process_file", "Process"),
        Binding("x", "delete", "Delete"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("tab", "focus_next", "Focus", show=False),
        Binding("?", "help", "Help"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, config_path: Path | None = None):
        super().__init__()
        self._config_path = config_path
        self.cfg: dict = {}
        self.store: SqliteStore | None = None
        self.pipeline = None
        self.pipeline_error: str | None = None
        self.selected_id: int | None = None
        # recording state
        self.recording = False
        self.busy = False
        self.source = None
        self.rec_kind: MeetingKind | None = None
        self.rec_started_at: datetime | None = None
        self.rec_started_mono: float = 0.0
        self.rec_prev_output = None                  # opaque restore token from the backend
        self._audio_status: A.AudioStatus | None = None
        self._progress: str = ""
        self.last_error: str | None = None

    # --- layout -------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield DataTable(id="meetings", cursor_type="row", zebra_stripes=True)
                yield Static("", id="audio")
            yield Markdown("", id="details")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#meetings", DataTable).border_title = "Meetings"
        self.query_one("#audio", Static).border_title = "Audio"
        self.query_one("#details", Markdown).border_title = "Details"

        try:
            self.cfg = load_config(self._config_path)
        except SystemExit as e:
            self.notify(str(e), severity="error", timeout=10)
            self.cfg = {}

        # Silence Hugging Face / tqdm progress bars: they'd corrupt the TUI
        # display, and (via a multiprocessing lock) their setup spawns a
        # subprocess that fails under redirected stdio. Disabling them removes
        # both problems — the model is cached, so there's no progress to show.
        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
        try:
            from huggingface_hub.utils import disable_progress_bars
            disable_progress_bars()
        except Exception:
            pass

        data_dir = Path(self.cfg.get("data_dir", "~/.ekko")).expanduser()
        self.store = SqliteStore(data_dir / "ekko.db")

        try:
            self.pipeline = build_pipeline(self.cfg)
        except Exception as e:                    # e.g. missing GEMINI_API_KEY
            self.pipeline_error = str(e)

        table = self.query_one("#meetings", DataTable)
        table.add_columns("ID", "Date", "Title")
        self.refresh_meetings()
        self.refresh_audio()
        self.set_interval(1.0, self._tick)

    # --- data ---------------------------------------------------------------
    def refresh_meetings(self) -> None:
        table = self.query_one("#meetings", DataTable)
        table.clear()
        rows = self.store.list_meetings() if self.store else []
        for mid, when, title in rows:
            date = when[:16].replace("T", " ")
            table.add_row(str(mid), date, title, key=str(mid))
        if rows:
            table.move_cursor(row=0)
            self.show_details(int(rows[0][0]))
        else:
            self.selected_id = None
            self.query_one("#details", Markdown).update(
                "_No meetings yet. Press **r** (in-person) or **o** (online) to record._")

    def show_details(self, meeting_id: int) -> None:
        self.selected_id = meeting_id
        m = self.store.get(meeting_id) if self.store else None
        md = render_note(m) if m else "_Not found._"
        self.query_one("#details", Markdown).update(md)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key and event.row_key.value:
            self.show_details(int(event.row_key.value))

    # --- audio panel --------------------------------------------------------
    def refresh_audio(self) -> None:
        try:
            self._audio_status = A.check()
        except Exception:
            self._audio_status = None
        self._render_audio()

    def _render_audio(self) -> None:
        s = self._audio_status
        lines: list[str] = []
        if s is None:
            lines.append("[dim]audio status unavailable[/]")
        else:
            tick = lambda ok: "[green]✓[/]" if ok else "[red]✗[/]"
            lines.append("  ".join(f"{tick(c.ok)} {c.label.split('(')[0].strip()}"
                                   for c in s.checks))
            if s.detail:
                lines.append(f"[dim]{s.detail}[/]")

        if self.recording:
            elapsed = int(time.monotonic() - self.rec_started_mono)
            kind = self.rec_kind.value if self.rec_kind else ""
            lines.append(f"[bold red]● REC[/] {kind}  "
                         f"{elapsed // 60:02d}:{elapsed % 60:02d}   [dim](s to stop)[/]")
        elif self.busy:
            lines.append(f"[yellow]{self._progress or '⚙ working…'}[/]")
        else:
            lines.append("[dim]● idle[/]")
        self.query_one("#audio", Static).update("\n".join(lines))

    def _tick(self) -> None:
        if self.recording or self.busy:
            self._render_audio()

    # --- navigation ---------------------------------------------------------
    def action_cursor_down(self) -> None:
        self.query_one("#meetings", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#meetings", DataTable).action_cursor_up()

    def action_help(self) -> None:
        self.notify(
            "r/o record (in-person/online) · s stop · p process a file · "
            "x delete · j/k move · tab focus · q quit",
            title="ekko keys", timeout=8)

    # --- recording ----------------------------------------------------------
    def action_record(self, kind: str) -> None:
        if self.recording:
            self.notify("Already recording — press s to stop.", severity="warning")
            return
        if self.busy:
            self.notify("Busy processing a meeting — try again shortly.",
                        severity="warning")
            return
        if self.pipeline is None:
            self.notify(f"Can't record: {self.pipeline_error or 'pipeline unavailable'}",
                        severity="error", timeout=10)
            return

        mkind = MeetingKind(kind)
        try:
            self.source = build_source(self.cfg, mkind)   # macOS online -> tap source
        except Exception as e:
            self.notify(f"Capture setup failed: {e}", severity="error")
            return

        self.rec_prev_output = None
        if mkind is MeetingKind.ONLINE:
            A.prepare_capture(mkind)                 # e.g. Linux points PULSE_SOURCE at the mix
            r = A.route_output_for_capture()         # macOS: switch default output
            self.rec_prev_output = r.token
            if r.routed_to:
                self.notify(f"Routed output → {r.routed_to} (still audible).")

        self.source.start(mkind)
        self.recording = True
        self.rec_kind = mkind
        self.rec_started_at = datetime.now()
        self.rec_started_mono = time.monotonic()
        self._render_audio()
        self.notify(f"● Recording ({kind}). Press s to stop.")

    def action_stop(self) -> None:
        if not self.recording:
            self.notify("Not recording.", severity="warning")
            return
        audio_path = self.source.stop()
        A.restore_output(self.rec_prev_output)
        A.finish_capture(self.rec_kind)
        self.recording = False
        title = f"{self.rec_kind.value} meeting {self.rec_started_at:%Y-%m-%d %H:%M}"
        self._process_audio(audio_path, title, self.rec_kind, self.rec_started_at)

    # --- process an existing file ------------------------------------------
    def action_process_file(self) -> None:
        if self.busy or self.recording:
            self.notify("Busy — finish the current recording first.",
                        severity="warning")
            return
        if self.pipeline is None:
            self.notify(f"Can't process: {self.pipeline_error or 'pipeline unavailable'}",
                        severity="error", timeout=10)
            return
        self.push_screen(PromptScreen("Path to an audio file:", "~/meeting.wav"),
                         self._on_file_path)

    def _on_file_path(self, path: str | None) -> None:
        if not path:
            return
        p = Path(path).expanduser()
        if not p.exists():
            self.notify(f"No such file: {p}", severity="error")
            return
        self._process_audio(p, p.stem, MeetingKind.IN_PERSON, datetime.now())

    # --- shared processing worker ------------------------------------------
    @work(thread=True, exclusive=True)
    def _process_audio(self, audio_path: Path, title: str,
                       kind: MeetingKind, started_at: datetime) -> None:
        self.busy = True
        self.call_from_thread(self._set_progress, "⚙ [1/5] transcribing…")

        def prog(step: int, total: int, label: str) -> None:
            self.call_from_thread(self._set_progress, f"⚙ [{step}/{total}] {label}…")

        try:
            self.pipeline.process(audio_path, title=title, kind=kind,
                                  started_at=started_at, on_progress=prog)
            self.call_from_thread(self._on_processed, title)
        except Exception as e:
            import traceback
            self.last_error = traceback.format_exc()
            self.call_from_thread(self.notify, f"Processing failed: {e}",
                                  severity="error", timeout=10)
        finally:
            self.busy = False
            self.call_from_thread(self._set_progress, "")

    def _set_progress(self, text: str) -> None:
        self._progress = text
        self._render_audio()

    def _on_processed(self, title: str) -> None:
        self.notify(f"✓ Saved “{title}”.")
        self.refresh_meetings()

    # --- delete -------------------------------------------------------------
    def action_delete(self) -> None:
        if self.selected_id is None:
            self.notify("No meeting selected.", severity="warning")
            return
        mid = self.selected_id
        m = self.store.get(mid)
        name = m.title if m else f"#{mid}"
        self.push_screen(ConfirmScreen(f"Delete “{name}”?"),
                         lambda ok: self._after_delete(mid, ok))

    def _after_delete(self, mid: int, ok: bool) -> None:
        if not ok:
            return
        self.store.delete(mid)
        self.notify("Deleted.")
        self.refresh_meetings()


def prewarm() -> None:
    """Start multiprocessing's resource_tracker now, while stdio is still the
    real terminal. Loading the Whisper model (in a worker thread) lazily creates
    a tqdm multiprocessing lock, which spawns the tracker subprocess — and that
    spawn fails once Textual has redirected stdio. Launching the tracker up front
    means the later lock reuses it instead of spawning again.
    """
    try:
        import multiprocessing
        lock = multiprocessing.Lock()
        del lock
    except Exception:
        pass


def run(config_path: Path | None = None) -> None:
    prewarm()
    EkkoApp(config_path=config_path).run()


if __name__ == "__main__":
    run()

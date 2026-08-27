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
from textual.containers import Horizontal, Vertical, VerticalScroll
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


class HelpScreen(ModalScreen):
    """Keybinding cheatsheet (dismiss with any key)."""
    BINDINGS = [Binding("escape,q,question_mark", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        yield Static(
            "[b]ekko — keys[/b]\n\n"
            "  [b]r[/b] record in-person    [b]o[/b] record online    [b]s[/b] stop\n"
            "  [b]p[/b] play / stop audio   [b]v[/b] reveal on disk    [b]R[/b] re-process\n"
            "  [b]f[/b] process a file      [b]y[/b] copy note         [b]x[/b] delete\n"
            "  [b]/[/b] search              [b]j/k[/b] move            [b]tab[/b] focus\n"
            "  [b]?[/b] help                [b]q[/b] quit\n\n"
            "[dim]scroll Details with the mouse, or focus it (tab) and use\n"
            "arrows · j/k · PgUp/PgDn.  y copies the whole note.\n"
            "esc / q to close[/]", id="help")

    def on_key(self, event) -> None:                 # any key closes
        self.dismiss()


class EkkoApp(App):
    TITLE = "ekko"
    SUB_TITLE = "meeting notes"

    CSS = """
    #main { height: 1fr; }
    #left { width: 38%; }
    #meetings { height: 1fr; border: round $primary; }
    #audio { height: auto; min-height: 6; border: round $primary; padding: 0 1; }
    #details { width: 1fr; border: round $primary; padding: 0 1; }
    #details:focus { border: round $accent; }
    #note { height: auto; }
    #dialog {
        width: 60; height: auto; padding: 1 2;
        border: thick $accent; background: $surface;
        align: center middle;
    }
    ConfirmScreen, PromptScreen, HelpScreen { align: center middle; }
    #help { width: 64; height: auto; padding: 1 2; border: thick $accent;
            background: $surface; }
    """

    BINDINGS = [
        Binding("r", "record('in_person')", "Rec"),
        Binding("o", "record('online')", "Online"),
        Binding("s", "stop", "Stop"),
        Binding("p", "play", "Play/Stop"),
        Binding("y", "copy", "Copy"),
        Binding("R", "reprocess", "Reprocess"),
        Binding("slash", "search", "Search"),
        Binding("x", "delete", "Delete"),
        Binding("f", "process_file", "File", show=False),
        Binding("v", "reveal", "Reveal", show=False),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("tab", "focus_next", "Focus", show=False),
        Binding("question_mark", "help", "Help"),
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
        self.filter: str = ""
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
        self._player = None                          # running audio-playback process, if any

    # --- layout -------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield DataTable(id="meetings", cursor_type="row", zebra_stripes=True)
                yield Static("", id="audio")
            with VerticalScroll(id="details"):
                yield Markdown("", id="note")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#meetings", DataTable).border_title = "Meetings"
        self.query_one("#audio", Static).border_title = "Audio"
        self.query_one("#details", VerticalScroll).border_title = "Details"

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
        if self.filter:
            f = self.filter.lower()
            rows = [r for r in rows if f in r[2].lower() or f in r[1].lower()]
        table.border_title = f"Meetings  (/{self.filter})" if self.filter else "Meetings"
        for mid, when, title in rows:
            date = when[:16].replace("T", " ")
            table.add_row(str(mid), date, title, key=str(mid))
        if rows:
            table.move_cursor(row=0)
            self.show_details(int(rows[0][0]))
        else:
            self.selected_id = None
            msg = (f"_No meetings match “{self.filter}”._" if self.filter else
                   "_No meetings yet. Press **r** (in-person) or **o** (online) to record._")
            self.query_one("#note", Markdown).update(msg)

    def show_details(self, meeting_id: int) -> None:
        self.selected_id = meeting_id
        m = self.store.get(meeting_id) if self.store else None
        md = render_note(m) if m else "_Not found._"
        self.query_one("#note", Markdown).update(md)
        self.query_one("#details", VerticalScroll).scroll_home(animate=False)

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
        # When the details pane is focused, j/k scroll it; otherwise move the list.
        if isinstance(self.focused, VerticalScroll):
            self.focused.scroll_down()
        else:
            self.query_one("#meetings", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        if isinstance(self.focused, VerticalScroll):
            self.focused.scroll_up()
        else:
            self.query_one("#meetings", DataTable).action_cursor_up()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_search(self) -> None:
        self.push_screen(PromptScreen("Filter meetings (empty = clear):",
                                      self.filter or "title or date…"),
                         self._on_search)

    def _on_search(self, text: str | None) -> None:
        self.filter = (text or "").strip()
        self.refresh_meetings()

    # --- per-meeting actions (play / reveal / copy / re-process) -------------
    def _selected_meeting(self):
        if self.selected_id is None or not self.store:
            self.notify("No meeting selected.", severity="warning")
            return None
        return self.store.get(self.selected_id)

    def action_play(self) -> None:
        # p toggles: if something's already playing, stop it.
        if self._player is not None and self._player.poll() is None:
            self._stop_playback()
            self.notify("■ Stopped.")
            return
        m = self._selected_meeting()
        if not m:
            return
        if not m.audio_path or not Path(m.audio_path).exists():
            self.notify("No audio file for this meeting.", severity="warning")
            return
        from .sysutil import play_audio
        proc = play_audio(Path(m.audio_path))
        if proc is not None:
            self._player = proc
            self.notify("▶ Playing… (p to stop)")
        else:
            self.notify("No audio player found.", severity="warning")

    def _stop_playback(self) -> None:
        """Stop the current playback process, if any. Safe to call anytime."""
        p, self._player = self._player, None
        if p is None or p.poll() is not None:
            return
        try:
            p.terminate()
            try:
                p.wait(timeout=1.0)
            except Exception:
                p.kill()
        except Exception:
            pass

    def on_unmount(self) -> None:
        # Don't leave afplay/ffplay running after the TUI exits.
        self._stop_playback()

    def action_reveal(self) -> None:
        m = self._selected_meeting()
        if not m or not m.audio_path:
            self.notify("No audio file for this meeting.", severity="warning")
            return
        from .sysutil import reveal_file
        if not reveal_file(Path(m.audio_path)):
            self.notify("Couldn't open the file manager.", severity="warning")

    def action_copy(self) -> None:
        m = self._selected_meeting()
        if not m:
            return
        from .sysutil import copy_text
        if copy_text(render_note(m)):
            self.notify("Copied note to clipboard.")
        else:
            self.notify("No clipboard tool found.", severity="warning")

    def action_reprocess(self) -> None:
        if self.busy or self.recording:
            self.notify("Busy — try again shortly.", severity="warning")
            return
        if self.pipeline is None:
            self.notify(f"Can't re-process: {self.pipeline_error or 'pipeline unavailable'}",
                        severity="error", timeout=10)
            return
        m = self._selected_meeting()
        if not m:
            return
        if not m.audio_path or not Path(m.audio_path).exists():
            self.notify("Original audio is gone — can't re-process.", severity="warning")
            return
        self._process_audio(Path(m.audio_path), m.title, m.kind, m.started_at,
                            replace_id=m.id)

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

        # Local (Ollama) summarizer: warn now if the daemon/model isn't ready, so
        # it doesn't only fail at summarize time — after the whole recording.
        self._warn_if_summarizer_unready()

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
        self._live_preview()              # live transcript (best-effort; no-ops if unsupported)

    def _warn_if_summarizer_unready(self) -> None:
        """Non-blocking preflight for the local summarizer (no-op for others)."""
        from .summarize.local import LocalSummarizer
        summarizer = getattr(self.pipeline, "summarizer", None)
        if isinstance(summarizer, LocalSummarizer):
            problem = summarizer.preflight()
            if problem:
                self.notify(problem, severity="warning", timeout=10)

    # --- live transcript preview --------------------------------------------
    @work(thread=True, group="preview", exclusive=True)
    def _live_preview(self) -> None:
        import tempfile
        import time as _time

        import numpy as np
        import soundfile as sf
        tr = None
        tmp = Path(tempfile.gettempdir()) / "ekko-live.wav"
        marker = 0                                   # audio cursor, fed back each read
        pending = np.zeros(0, dtype=np.float32)      # new audio not yet transcribed
        transcript = ""                              # accumulated, from the start
        self.call_from_thread(self._show_live, "")
        while self.recording:
            _time.sleep(3.0)
            if not self.recording:
                break
            try:
                got = self.source.read_new_audio(marker)
            except Exception:
                got = None
            if got:
                data, rate, marker = got
                if data is not None and data.size:
                    pending = np.concatenate([pending, data])
            else:
                rate = None
            # Transcribe once we've gathered a chunky, non-overlapping window —
            # bigger windows read far better than 3s slivers.
            if rate is None or pending.size < rate * 6.0:
                continue
            if tr is None:
                from .transcribe.whisper import WhisperTranscriber
                tr = WhisperTranscriber(model_size="tiny")
            chunk, pending = pending, np.zeros(0, dtype=np.float32)
            try:
                sf.write(tmp, chunk, rate, subtype="PCM_16")
                text = " ".join(s.text for s in tr.transcribe(str(tmp)).segments).strip()
            except Exception:
                continue
            if text:
                transcript = f"{transcript} {text}".strip()
                self.call_from_thread(self._show_live, transcript)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass

    def _show_live(self, text: str) -> None:
        if self.recording:
            self.query_one("#note", Markdown).update(
                f"## ● Live transcript\n\n{text or '_…listening…_'}")
            # Follow the tail as new text streams in.
            self.query_one("#details", VerticalScroll).scroll_end(animate=False)

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
    def _process_audio(self, audio_path: Path, title: str, kind: MeetingKind,
                       started_at: datetime, replace_id: int | None = None) -> None:
        self.busy = True
        self.call_from_thread(self._set_progress, "⚙ [1/5] transcribing…")

        def prog(step: int, total: int, label: str) -> None:
            self.call_from_thread(self._set_progress, f"⚙ [{step}/{total}] {label}…")

        try:
            self.pipeline.process(audio_path, title=title, kind=kind,
                                  started_at=started_at, on_progress=prog)
            self.call_from_thread(self._on_processed, title, replace_id)
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

    def _on_processed(self, title: str, replace_id: int | None = None) -> None:
        if replace_id is not None and self.store:      # re-process: drop the old row
            try:
                self.store.delete(replace_id)
            except Exception:
                pass
        self.notify(f"✓ {'Re-processed' if replace_id else 'Saved'} “{title}”.")
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

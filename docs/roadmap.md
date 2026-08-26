# Feature roadmap

Everything here is **deferred by design** — the point of the interface seams
(see [architecture.md](architecture.md)) is that each of these plugs into a seam
that already exists, so none of them requires reworking the pipeline. Ordering
below is rough priority, not a commitment.

Most items are **pipeline seams** (they implement an existing stage interface).
The TUI below is different — it's a **frontend**, a peer of the `cli.py` entry
point that drives the same building blocks.

## ✅ Terminal UI (TUI) — SHIPPED

Built with [Textual](https://textual.textualize.io/) as `ekko/tui.py`, launched
via `ekko tui`. A lazygit-style frontend (peer of `cli.py`) that reuses the same
building blocks — see [implementation.md](implementation.md#terminal-ui-ekko-tui).
Ships with: browsable meetings list, Markdown note viewer, live audio-status
panel, start/stop recording with a live timer and online output auto-routing,
process-an-existing-file, delete-with-confirm, **audio playback + reveal-on-disk**,
**re-process a meeting**, **search/filter**, **copy-note-to-clipboard**, a **help
modal**, and a **live transcript preview during recording** (rolling-window tiny
model, shown while you record). Processing runs in a worker thread with stage
progress via the pipeline's `on_progress` callback.

**Still open (deferred):** inline speaker renaming + re-identify (pairs with
diarization); in-app config editor; a true *streaming* transcript (the current
preview re-transcribes a rolling window rather than streaming).

## 1. Phone-as-source (watched-folder upload)

**Seam:** `sources/base.py::AudioSource`

Record on your phone, drop the file into a watched folder, and ekko processes it
like any other audio — useful when the laptop isn't in the room. Implement a new
`AudioSource` (or a lighter "ingest" path that skips live capture and hands
`process()` an existing file, which the `process` CLI command already supports).
The pipeline is already source-agnostic, so nothing downstream changes.

## 2. Online speaker naming via screen-watch + OCR

**Seam:** `identify/screen.py::ScreenIdentifier` (stub exists, interface locked)

For Meet/Teams, the participant tiles show names on screen. A `ScreenIdentifier`
would periodically capture the meeting window, OCR the visible name labels, and
correlate them with active-speaker cues to fill the `{cluster_label: real_name}`
map automatically — replacing manual naming for online meetings. Because the
interface is already locked in, this plugs in without pipeline changes; `config.py`
just constructs `ScreenIdentifier` instead of `ManualIdentifier` when
`kind == ONLINE`.

## 3. Voiceprint enrollment (auto-naming recurring teammates)

**Seam:** `identify/base.py::SpeakerIdentifier`

Enroll a short voice sample per teammate once; a `VoiceprintIdentifier` then
matches enrolled embeddings against diarization clusters and pre-fills the name
map, so recurring people get named automatically with no per-meeting map. Works
for both in-person and online, and composes with diarization.

## 4. Local 8B summarizer (fully-offline option)

**Seam:** `summarize/base.py::Summarizer`

A local model implementing the same `Summarizer` interface removes the one
remaining off-device hop, making ekko fully offline end-to-end. The shared
`SUMMARY_INSTRUCTIONS` prompt already standardizes the requested JSON output, so
this is a drop-in. **Measure the quality gap vs Gemini first** before defaulting
to it — the honest bar is "notes you'd actually trust."

## 5. Self-hosted notes backend (multi-device / team sharing)

**Seam:** `store/sqlite.py::SqliteStore`

Swap the SQLite store for an HTTP client against a self-hosted service to sync
across devices or share within a team. Critically, this preserves the privacy
model: audio still never leaves the device — only the derived text would sync.

## 6. More integrations (Notion, Slack, Asana)

**Seam:** `integrations/base.py::Integration`

Each new destination implements the same `Integration.publish(meeting)` adapter
that `MarkdownIntegration` does. Integrations are a list, so these run *alongside*
Markdown, not instead of it — one meeting can land in Obsidian, Notion, and a
Slack channel at once. `config.py` appends whichever are enabled.

## Cross-cutting notes

- **Model churn is expected.** Google rev's the Gemini Flash line; the model id
  is a config value precisely so this stays a one-line change. (The default moved
  from `gemini-2.5-flash` → `gemini-3.6-flash` when the former 404'd for new
  users.) A provider-swap to Claude or a local model is the same shape of change.
- **Diarization quality** would improve with a channel prior — if a future source
  keeps mic and system audio on separate tracks, the diarizer could use that
  instead of relying solely on pyannote clustering.

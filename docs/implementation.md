# Current implementation

This documents what ships **today** (v1). For where each stage is headed, see
[roadmap.md](roadmap.md); for the interface seams, see
[architecture.md](architecture.md).

## Status at a glance

| Stage | Implementation | Status | Verified |
|-------|----------------|--------|----------|
| source | `LaptopSource` | ✅ shipped | ✅ in-person + online aggregate (3ch→mono) captured & transcribed on a real Mac |
| transcribe | `WhisperTranscriber` (faster-whisper) | ✅ shipped | ✅ end-to-end on real speech |
| diarize | `Diarizer` (pyannote) | ⚙️ off by default | Disabled path ✅; enabled path needs an HF token |
| identify | `ManualIdentifier` | ✅ shipped | ✅ name-map applied |
| identify (online) | `ScreenIdentifier` | 🚧 stub | Interface locked, not implemented |
| summarize | `GeminiSummarizer` | ✅ shipped | ✅ real Gemini call end-to-end |
| store | `SqliteStore` | ✅ shipped | ✅ save + list round-trip |
| integrations | `MarkdownIntegration` | ✅ shipped | ✅ note written |
| TUI | `EkkoApp` (Textual) | ✅ shipped | ✅ browse/delete + full record→note cycle (Pilot) |

## Stage by stage

### source — `sources/laptop.py::LaptopSource`

Records a single mono WAV at 16 kHz (what Whisper wants; avoids a resample step).
Audio is captured via `sounddevice` in a callback that pushes buffers onto a
queue; a background writer thread drains the queue to disk via `soundfile`.
`stop()` closes the stream, joins the writer, and returns the WAV path.

- **IN_PERSON:** the built-in mic (default input device) is enough.
- **ONLINE:** macOS won't let apps grab system output directly. The standard
  workaround is a virtual loopback device (e.g. BlackHole) combined with your mic
  into an **Aggregate Device**, so one input device carries both "everyone else"
  (system output) and "you" (mic). Point `capture.input_device` at that
  aggregate.

**Multichannel downmix (what makes online capture actually work).** An Aggregate
Device does *not* pre-mix its inputs — it presents them as separate channels
(e.g. ch1–2 = BlackHole system audio, ch3 = mic). So `LaptopSource` captures
*all* of the device's input channels (`_device_input_channels()` queries the
count) and **averages them to mono** in the audio callback. A naive mono capture
would grab only channel 1 and silently drop the mic (or the room), which is the
classic "I recorded the meeting but not myself" failure. Downmixing keeps both in
one track and keeps the pipeline source-agnostic (it still sees one mono WAV).

### transcribe — `transcribe/whisper.py::WhisperTranscriber`

Local, on-device transcription with `faster-whisper` (CTranslate2 backend,
CPU/Metal via `device="auto"`, `int8` to keep memory small). The model is
lazy-loaded on first `transcribe()`. VAD filtering is on. Each Whisper segment
becomes a `Segment` with `start`/`end`/`text` (speaker left unset here — that's
diarize's job).

- **Model size** (`whisper.model_size`): `small`/`base` are the sweet spot on a
  16 GB M2; `large-v3` is best quality but ~3 GB resident. `tiny` is fast but
  mishears (e.g. "beta"→"bait") — fine for smoke tests, not for real notes.
- This is the **privacy-critical stage**: audio → text never leaves the machine.

### diarize — `transcribe/diarize.py::Diarizer`

Optional overlay that assigns `Speaker A/B/C` to segments. **Off by default** —
v1 works without it (every segment → `Speaker A`) and still produces useful
notes.

- **Disabled** (default): stamps every segment `"Speaker A"` and returns.
- **Enabled**: lazy-loads `pyannote/speaker-diarization-3.1` (needs a Hugging
  Face token), runs diarization, then labels each transcript segment with the
  speaker turn it overlaps most (max-overlap assignment). Cluster labels come out
  as `"Speaker <N>"`.

Kept separate from transcription precisely so it's swappable/optional. Naming
those anonymous clusters with real names is a *different* stage (identify).

### identify — `identify/manual.py::ManualIdentifier`

v1 default, aimed at in-person where there's no screen to read. Applies an
optional `{cluster_label: real_name}` map from config (`[speakers.name_map]`);
anything unmapped keeps its anonymous label, which you can rename after the fact.
Honest, simple, zero-dependency.

`identify/screen.py::ScreenIdentifier` is a **v2 stub** — the interface is locked
in so online screen-watch + OCR naming plugs in later without pipeline changes.

### summarize — `summarize/gemini.py::GeminiSummarizer`

Provider-agnostic by design (`summarize/base.py::Summarizer`). v1 default is
Gemini Flash — cheap, fast, large context. Uses JSON mode
(`response_mime_type: application/json`) so structured output comes back without
brittle parsing, then maps it into a `Summary`.

- The **shared prompt** (`SUMMARY_INSTRUCTIONS` in `summarize/base.py`) asks for
  exactly `tldr`, `key_points`, `decisions`, and `action_items` (each with
  `text` + `owner`), and instructs the model to attribute owners only when clear
  and never invent action items. Every provider gets asked the same thing.
- **API key resolution:** constructor arg → `[summarize].api_key` in config →
  `GEMINI_API_KEY` env var. Missing key raises a clear `RuntimeError` at
  construction. Store the key in `~/.ekko/config.toml` to avoid `export` on every
  run.
- **Model id is configurable** because Google rev's the Flash line. Current
  default: **`gemini-3.6-flash`**. (The older `gemini-2.5-flash` now 404s for new
  users — hence the configurable default.)
- **Privacy note:** this is the one stage that sends text off-device (transcript
  only — audio stays local). Use a billing-enabled key so the provider doesn't
  train on your data.

### store — `store/sqlite.py::SqliteStore`

The canonical meeting record lives on-device in SQLite. Deliberately thin: one
`meetings` table (title, kind, started_at, audio_path, transcript_json,
summary_json). `save()` inserts and sets `meeting.id`; `list_meetings()` returns
`(id, started_at, title)` newest-first.

This is the boundary a future self-hosted "notes service" would replace: swap
this class for an HTTP client and the rest of the pipeline is unchanged (audio
still never leaves the device — only derived text would sync).

### integrations — `integrations/markdown.py::MarkdownIntegration`

Writes one Markdown file per meeting into a folder (point it at an Obsidian
vault). Filename is `YYYY-MM-DD-<slug>.md`. The note renders: title, when/type
metadata, then TL;DR, Key points, Decisions, Action items (as checkboxes with
`— **owner**` when attributed), and the full speaker-labeled transcript. Zero
external dependency, fully private — the simplest proof of the adapter pattern.

Integrations are a **list** — the pipeline publishes to all of them, so
Notion/Slack/Asana adapters add alongside Markdown, they don't replace it.

## CLI (`cli.py`)

Manual trigger by design — no calendar watcher. `record` covers both online and
in-person via `--kind`.

```bash
ekko record --kind in_person --title "1:1 with Priya"
#  ● Recording… press Enter to stop → transcribe → summarize → writes a note

ekko audio setup                             # one-time: create online-capture devices
ekko audio check                             # report BlackHole / capture / output status
ekko audio teardown                          # remove the ekko-managed devices
ekko record --kind online                    # online: auto-routes output, captures both sides
ekko process meeting.wav --kind online      # process an existing audio file
ekko devices                                 # list input devices (find your aggregate)
ekko devices --test 3 --seconds 3            # record 3s from device 3, report the level
ekko list                                    # past meetings, newest first
ekko tui                                     # open the terminal UI
ekko update                                  # upgrade ekko in place
```

All commands accept `--config <path>` (defaults to `~/.ekko/config.toml`).
`record` and `process` accept `--kind {online,in_person}` and an optional
`--title` (defaults to a timestamped name or the file stem).

- **`record --device <index|name>`** overrides `capture.input_device` for one
  run. When `--kind online` and no device is set, `record` warns that it's about
  to capture the mic only (and notes if no loopback/aggregate device exists).
- **`devices`** (`sources/devices.py`) lists every input-capable device with its
  index, channels, and sample rate; marks the default; and flags names that look
  like a loopback/aggregate (BlackHole, Aggregate, Loopback, …). `--test
  <index|name>` records a few seconds and prints a peak/RMS level bar so you can
  confirm a device actually carries audio before relying on it — the fast way to
  validate an online-capture aggregate.
- **`audio setup` / `audio check`** (`audio_setup.py` + `sources/coreaudio.py`)
  automate the macOS online-capture rig — see below.

## Online-capture setup automation (`ekko audio`)

Online capture is the one platform-specific area, isolated behind an
**`AudioBackend` seam** (`ekko/audio/`): `get_backend()` returns a
`CoreAudioBackend` (macOS) or `PulseAudioBackend` (Linux); `audio_setup.py` is a
thin facade so the CLI/TUI stay platform-agnostic. In-person capture uses no
backend. See [linux.md](linux.md) for the Linux implementation and its
verification status.

### macOS 14.4+ — system-audio process tap (default)

On macOS 14.4+ the Core Audio **process-tap** API captures the system audio mix
*in place*, so ekko needs **no BlackHole, no virtual devices, and no output
rerouting** — your speakers and volume keys are untouched, and there's nothing to
`ekko audio setup`. `sources/coreaudio_tap.py` (ctypes + the Obj-C runtime, no
PyObjC) implements it:

- `TapSource(AudioSource)` creates a global stereo `CATapDescription`, an
  `AudioHardwareCreateProcessTap`, and a *private* aggregate combining the tap
  with your mic — the mic sub-device provides the driving clock (a tap-only
  aggregate won't run IO). A Core Audio **IOProc** reads mic + tap channels,
  downmixes to mono, and a writer thread streams to a WAV (same queue pattern as
  `LaptopSource`). On stop it tears down the IOProc, aggregate, and tap.
- `config.build_source(cfg, kind)` returns this for ONLINE meetings when
  `tap_supported()` (macOS 14.4+); the `CoreAudioBackend` reports it as ready in
  `check()`, makes `setup()`/routing no-ops, and `online_source()` hands back a
  `TapSource`. `--device` still forces plain `LaptopSource` capture.

Older macOS (<14.4) falls back to the BlackHole rig below.

### macOS <14.4 — BlackHole aggregate (fallback)

`sources/coreaudio.py` is a `ctypes` wrapper over the macOS Core Audio HAL
(no PyObjC dependency). It can enumerate devices (id/uid/name/in/out), get and
set the default input/output device, and **create/destroy Aggregate and
Multi-Output devices** (`AudioHardwareCreateAggregateDevice` — the `stacked` flag
is the only difference between the two). Device-list updates arrive
asynchronously, so `wait_for_uid()` polls after a create/destroy.

`audio_setup.py` builds on it:

- **`check()`** reports whether BlackHole, the ekko capture aggregate, and the
  ekko output multi-output exist, plus whether system output is routed for
  capture. Backs `ekko audio check`.
- **`setup()`** idempotently creates two ekko-managed devices (matched by a
  stable UID, so re-running reuses them and hand-made devices are never touched):
  - **"ekko capture"** — an input Aggregate of BlackHole + your mic (drift
    correction on BlackHole). ekko records from this; `capture.input_device` is
    written to config automatically.
  - **"ekko output"** — a Multi-Output ("stacked") device of your speakers +
    BlackHole, so meeting audio is captured *and* stays audible.

  Installing the BlackHole *driver* is the one prerequisite this can't do (needs
  `brew install blackhole-2ch`); `setup()` detects its absence and says so.
- **`teardown()`** removes the two ekko-managed devices (by UID only — hand-made
  devices are never touched). If the system default output is currently "ekko
  output", it moves the default back to a physical output first so audio keeps
  working. Backs `ekko audio teardown`.

**Auto-routing on record.** `ekko record --kind online` switches the system
default output to "ekko output" for the duration of the recording and **restores
your previous output when capture ends** (in a `finally`, so an interrupt still
restores it). Pass `--no-auto-route` to opt out. This removes the per-meeting
chore of manually flipping output devices.

## Terminal UI (`ekko tui`)

A lazygit-style Textual app (`tui.py`) — a *frontend* peer of the CLI that drives
the same `config`/`build_source`/`build_pipeline`/`SqliteStore`/`audio_setup`
building blocks (no duplicated logic). Layout: a **Meetings** table + **Audio**
status panel on the left, a **Details** Markdown viewer on the right, and a
`Footer` of context keybindings.

- **Browse** — the meetings table loads from `SqliteStore.list_meetings()`;
  highlighting a row renders that meeting's note (via the shared
  `integrations.markdown.render_note`, reconstructed with `SqliteStore.get()`).
- **Record** — `r` (in-person) / `o` (online) start capture; a live `● REC mm:ss`
  timer runs in the Audio panel; `s` stops. Online recordings auto-route output
  through "ekko output" and restore it on stop (the same `audio_setup` helpers
  the CLI uses).
- **Process a file** — `p` opens a prompt for an audio path.
- **Delete** — `x` with a yes/no confirm modal (`SqliteStore.delete()`).
- **Navigate** — `j`/`k` move, `Tab` switches focus, `?` shows keys, `q` quits.

The blocking pipeline (Whisper, Gemini) runs in a Textual `@work(thread=True)`
worker, with stage progress reported back to the Audio panel via the pipeline's
`on_progress` callback and `call_from_thread`. Two implementation notes that
matter:

- **SQLite across threads.** The store is opened with `check_same_thread=False`
  because the worker writes while the UI thread reads; access is serialized (one
  exclusive worker at a time).
- **Multiprocessing pre-warm.** Loading the Whisper model lazily creates a tqdm
  `multiprocessing` lock, which launches the resource-tracker subprocess — and
  that launch fails once Textual has redirected stdio. `run()` calls
  `prewarm()` first (creating a throwaway lock while stdio is still the real
  terminal) so the worker reuses the running tracker. HF/tqdm progress bars are
  also disabled in the TUI (they'd corrupt the display).

Verified headlessly with Textual's `run_test`/Pilot: browse + navigate + delete
(with confirm/cancel), and a full online record → stop → transcribe → summarize →
save → render cycle.

## Configuration (`~/.ekko/config.toml`)

Copy `config.example.toml` to `~/.ekko/config.toml` and edit. Keys:

| Section | Key | Purpose |
|---------|-----|---------|
| top-level | `data_dir` | Where the SQLite db + audio live (default `~/.ekko`). |
| `[capture]` | `audio_dir`, `input_device` | WAV output dir; input device (unset = default mic; an Aggregate Device for online). |
| `[whisper]` | `model_size`, `compute_type` | e.g. `small` / `int8`. |
| `[diarize]` | `enabled`, `hf_token` | Off by default; token needed when on (or `HF_TOKEN` env). |
| `[summarize]` | `model`, `api_key` | `gemini-3.6-flash`; key here or via `GEMINI_API_KEY`. |
| `[speakers.name_map]` | `"Speaker A" = "Duc"` | Rename anonymous clusters to real names. |
| `[markdown]` | `enabled`, `vault_dir` | Where notes are written (point at an Obsidian vault). |

## Runtime / dependencies

Python 3.14 verified. `requirements.txt` covers the v1 core: `sounddevice`,
`soundfile`, `faster-whisper`, `google-genai`. `pyannote.audio` is commented out
— install it only when enabling diarization.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
mkdir -p ~/.ekko && cp config.example.toml ~/.ekko/config.toml   # then edit
```

## Known limitations today

- **Downmixed mono track** — multichannel devices (an online aggregate) are
  averaged to one mono channel at capture, so the separate mic/system channels
  aren't preserved. Diarization therefore relies entirely on pyannote's
  clustering (no channel prior). Keeping the raw channels for a channel-based
  speaker prior is a possible future improvement (see roadmap).
- **No live-mic / diarization CI coverage** — both are hardware/credential-gated;
  they're verified by hand, not the smoke test.
- **`ScreenIdentifier` is a stub** — online meetings currently fall back to
  manual naming like in-person.
- **Cloud summarizer sends text off-device** — by design, and only the
  transcript, but worth stating plainly.

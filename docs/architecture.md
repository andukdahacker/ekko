# Architecture

## Design principle: every stage is a swappable interface

ekko is a linear pipeline where each stage is an abstract interface with one
concrete implementation today. The pipeline orchestrator knows only the
interfaces; `config.py` is the single place that picks concrete implementations.
Swapping Gemini for Claude, or adding a Notion integration, is an edit to
`config.py` — never to the pipeline.

Two invariants drive the whole design:

1. **Privacy boundary.** Audio and transcription happen on-device. Only derived
   summary *text* ever leaves the machine, and only if you choose a cloud
   summarizer. The `store` stage is the seam where a future self-hosted backend
   would sync text — audio still never leaves.
2. **Source-agnostic core.** Everything downstream of capture speaks in the
   shared data types (`Transcript`, `Segment`, `Summary`, `Meeting`), so the
   pipeline doesn't care whether audio came from a laptop or a phone, or whether
   speaker names were resolved by a screen-watcher or left anonymous.

## The pipeline

```
audio source ─▶ whisper ─▶ diarize ─▶ identify ─▶ summarize ─▶ store ─▶ integrations
  (laptop)     (local)    (optional)  (names)     (Gemini)    (sqlite)   (markdown)
```

| Stage | Interface | Responsibility |
|-------|-----------|----------------|
| **source** | `sources/base.py::AudioSource` | Capture a single mixed WAV. `start(kind)` / `stop() -> Path`. |
| **transcribe** | `WhisperTranscriber` | Audio → `Transcript` of `Segment`s, on-device. The privacy-critical stage. |
| **diarize** | `Diarizer` | Optional overlay: assign `Speaker A/B/C` to segments. Off by default. |
| **identify** | `identify/base.py::SpeakerIdentifier` | Turn anonymous cluster labels into real names. |
| **summarize** | `summarize/base.py::Summarizer` | `Transcript` → structured `Summary`. Provider-agnostic. |
| **store** | `SqliteStore` | Persist the canonical `Meeting` record on-device. |
| **integrations** | `integrations/base.py::Integration` | Publish the meeting outward (Markdown today). A list — many can run. |

`pipeline.py::Pipeline.process()` runs these in order and is the only code that
knows the sequence. It's told concrete implementations via constructor
injection; it never imports a provider directly.

## How the pieces wire together

```
cli.py
  └─ load_config(path)                 # reads ~/.ekko/config.toml (TOML)
  └─ build_source(cfg)  ──────────────▶ LaptopSource
  └─ build_pipeline(cfg) ─────────────▶ Pipeline(
                                           WhisperTranscriber,
                                           Diarizer,
                                           ManualIdentifier,
                                           GeminiSummarizer,
                                           SqliteStore,
                                           [MarkdownIntegration, ...])
```

- **`cli.py`** — the entry point and the *only* code that deals with the
  user. Three subcommands: `record`, `process`, `list`. It's a thin shell around
  config-load → build → `pipeline.process()`.
- **`config.py`** — the composition root. Reads TOML and constructs every
  concrete implementation. This is the dependency-injection boundary: "which
  provider / which source" is decided here and nowhere else.
- **`pipeline.py`** — orchestration only. Holds references to the interfaces and
  runs them in sequence.

## Data model (`models.py`)

The types every stage passes between each other:

- **`MeetingKind`** — `ONLINE` (Meet/Teams; system audio, a screen to read names
  from) or `IN_PERSON` (single mic, no screen). Drives capture and, later,
  identification strategy.
- **`Segment`** — one contiguous chunk of speech: `start`, `end`, `text`,
  `speaker` (defaults to `"Speaker ?"`, becomes `"Speaker A"` after diarize,
  then a real name after identify). Has a `duration` property.
- **`Transcript`** — a list of `Segment`s. `as_dialogue()` flattens to
  `Name: text` lines — the exact format the summarizer consumes.
- **`Summary`** — the structured output: `tldr`, `key_points[]`, `decisions[]`,
  `action_items[]`.
- **`ActionItem`** — `text` + optional `owner` (a real speaker name when
  attribution succeeds, else `None`).
- **`Meeting`** — the canonical record carried through the whole pipeline:
  `title`, `kind`, `started_at`, `audio_path`, `transcript`, `summary`, and `id`
  (set once persisted).

The speaker field's lifecycle is the clearest illustration of the seams:
`"Speaker ?"` (fresh) → `"Speaker A"` (diarize) → `"Priya"` (identify). Each
stage only advances it; nothing downstream cares which stage did the naming.

## Directory layout

```
ekko/
  cli.py            # entry point: record / process / list
  config.py         # composition root — TOML → concrete implementations
  pipeline.py       # stage orchestration
  models.py         # shared data types
  sources/          # AudioSource — laptop.py (v1), base.py
  transcribe/       # whisper.py (local), diarize.py (optional overlay)
  identify/         # SpeakerIdentifier — manual.py (v1), screen.py (v2 stub), base.py
  summarize/        # Summarizer — gemini.py (v1), base.py (+ shared prompt)
  store/            # sqlite.py
  integrations/     # Integration — markdown.py (v1), base.py
```

## Why this shape

- **Provider churn is isolated.** When Google rev's the Flash model line, or you
  want Claude instead, the change is one line in `config.py` (or a config value)
  — the pipeline, data model, and every other stage are untouched.
- **Optionality is free.** Diarization can be off (everyone → `Speaker A`) and
  the pipeline still produces useful notes. Turning it on is a config flag, not a
  code path change.
- **Deferred features already have a home.** Phone-as-source, screen-based
  naming, a local summarizer, and new integrations each implement an interface
  that already exists — see [roadmap.md](roadmap.md).

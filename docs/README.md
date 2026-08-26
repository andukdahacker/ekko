# ekko documentation

**ekko** is a local-first, privacy-preserving meeting notetaker. Audio capture and
transcription stay on your device; only derived summary text is sent to a cloud
model (and only if you choose a cloud summarizer). No meeting bots — it captures
audio locally, so it works for Google Meet, Microsoft Teams, and in-person
meetings alike.

## Docs index

| Doc | What's in it |
|-----|--------------|
| [architecture.md](architecture.md) | The pipeline, the interface seams, data model, and the design principles that keep every stage swappable. |
| [implementation.md](implementation.md) | What ships today — each stage's concrete implementation, config surface, CLI, and current limits. |
| [roadmap.md](roadmap.md) | Deferred-by-design features and the seam each one plugs into. |
| [linux.md](linux.md) | Cross-platform support — the audio-backend seam, Linux online capture (PulseAudio/PipeWire), and its verification status. |

## Quick links

- Project README (setup + usage): [`../README.md`](../README.md)
- Example config: [`../config.example.toml`](../config.example.toml)
- Source: [`../ekko/`](../ekko/)

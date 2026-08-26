# Linux support

ekko runs on Linux. Everything except the *online* system-audio rig is
cross-platform already; the OS-specific part lives behind an audio-backend seam,
and — like macOS 14.4+ — Linux captures system audio **in place with no setup**.

## What works where

| Capability | macOS | Linux |
|------------|-------|-------|
| In-person recording, transcribe, summarize, store, markdown, CLI, TUI | ✅ | ✅ |
| Online system-audio capture | ✅ process tap (14.4+) | ✅ PulseAudio/PipeWire monitor |
| Setup needed for online? | ❌ none | ❌ none (just `parec`) |
| Output rerouting / volume impact | none | none |

## The audio-backend seam

`ekko/audio/` defines an `AudioBackend` interface, picked by `get_backend()`:

- **macOS** → `CoreAudioBackend`: process tap on 14.4+ (no BlackHole), BlackHole
  aggregate fallback on older macOS.
- **Linux** → `PulseAudioBackend`: records monitor + mic in place (below).
- **Other** → `UnsupportedBackend`: in-person still works.

In-person capture (`sources/laptop.py`, sounddevice) needs no backend anywhere.

## Linux online capture — how it works

Every output sink exposes a `.monitor` source, so there's nothing to install or
configure beyond the `parec` recorder. `sources/pulse_monitor.MonitorSource`:

1. Finds the default sink's monitor (`pactl get-default-sink` → `<sink>.monitor`)
   and the default mic (`pactl get-default-source`).
2. Records **both at once** with two `parec` streams
   (`parec -d <src> --rate 16000 --channels 1 --format s16le`).
3. On stop, mixes them to one mono 16 kHz WAV.

No PulseAudio modules are loaded, your output device is never touched, and there
are no persistent devices to clean up — capture is create-on-record /
destroy-on-stop, exactly like the macOS tap. `config.build_source(cfg, ONLINE)`
returns a `MonitorSource` when `parec` is present.

`parec` comes from **pulseaudio-utils** and also drives PipeWire via
`pipewire-pulse`.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/andukdahacker/ekko/main/install.sh | bash
# online meetings need parec:
sudo apt install pulseaudio-utils      # or your distro's equivalent / pipewire-pulse
ekko                                    # opens the TUI
```

No `ekko audio setup` needed. `ekko audio check` confirms the pieces
(`pactl` + `parec` + a monitor source). `ekko audio teardown` only removes the
null-sink/loopbacks that *older* ekko builds created — current ekko creates none.

## ⚠️ Verification status

The Linux backend was **developed and unit-tested on macOS** — the `parec`
invocation and the stereo mixing are verified (an FFT check confirms both the
system-monitor and mic signals survive the mix), but it has **not been run on a
live PulseAudio/PipeWire server**. Verify on real Linux:

1. `ekko audio check` → `pactl`, `parec`, and a monitor source all ✓.
2. `ekko record --kind online`, play audio + talk a few seconds, stop.
3. The saved note's transcript contains **both** the system audio and your voice;
   your output device and volume were never touched.

Likely gotchas: on a pure-PipeWire box without `pulseaudio-utils`, `parec` may be
absent (install it, or a `pw-record` backend is the clean follow-up); and if your
default source is a monitor, ekko records system audio only (no mic).

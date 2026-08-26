"""Audio-device discovery + a capture self-test.

Online recording lives or dies on picking the right input device — an Aggregate
Device that carries both system output (everyone else) and your mic. You can't
set `capture.input_device` sensibly without seeing what's available, so this
module backs the `ekko devices` command:

  - list_input_devices()  -> what can I record from, and which looks like a loopback?
  - probe_level()         -> does that device actually carry audio right now?

Kept separate from LaptopSource so the capture path stays lean; both just use
sounddevice.
"""
from __future__ import annotations

from dataclasses import dataclass

import sounddevice as sd

# Substrings that flag a device as a system-audio loopback or an aggregate that
# likely contains one. Used only to hint the user — never to auto-select.
_LOOPBACK_HINTS = ("blackhole", "aggregate", "loopback", "soundflower", "vb-cable",
                   "multi-output", "monitor", "ekko_mix", "ekko capture")


@dataclass
class InputDevice:
    index: int
    name: str
    channels: int          # max input channels
    samplerate: int        # device default sample rate
    is_default: bool
    looks_like_loopback: bool


def list_input_devices() -> list[InputDevice]:
    """Every input-capable device, with a hint at which carry system audio."""
    default_in = sd.default.device[0]
    out: list[InputDevice] = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] <= 0:
            continue
        name = d["name"]
        out.append(InputDevice(
            index=i,
            name=name,
            channels=d["max_input_channels"],
            samplerate=int(d["default_samplerate"]),
            is_default=(i == default_in),
            looks_like_loopback=any(h in name.lower() for h in _LOOPBACK_HINTS),
        ))
    return out


def has_loopback_device() -> bool:
    """True if any device looks like a system-audio loopback/aggregate."""
    return any(d.looks_like_loopback for d in list_input_devices())


@dataclass
class LevelReading:
    seconds: float
    samplerate: int
    channels: int
    peak: float            # max abs sample, 0.0–1.0
    rms: float             # root-mean-square, 0.0–1.0
    device: int | str | None

    @property
    def silent(self) -> bool:
        # Below this the "signal" is almost certainly noise floor / nothing wired up.
        return self.peak < 0.002


def probe_level(device: int | str | None = None, *, seconds: float = 3.0,
                samplerate: int = 16_000) -> LevelReading:
    """Record a short clip and report its level, so you can confirm a device is
    actually carrying audio before you rely on it for a real meeting.

    Captures ALL of the device's input channels and downmixes to mono — the same
    thing LaptopSource does — so the level reflects what a real recording would
    contain. (Probing an aggregate mono would only see channel 1 and could read
    "live" while the mic channel is silently dropped.)

    For an online-capture aggregate, play some audio AND talk during the probe:
    a correctly wired device reads well above the silence threshold; a mis-wired
    one reads flat.
    """
    import numpy as np

    resolved = sd.default.device[0] if device is None else device
    ch = max(1, int(sd.query_devices(resolved)["max_input_channels"]))
    frames = int(seconds * samplerate)
    rec = sd.rec(frames, samplerate=samplerate, channels=ch,
                 dtype="float32", device=device)
    sd.wait()
    mono = rec.mean(axis=1) if rec.ndim > 1 and rec.shape[1] > 1 else rec.ravel()
    peak = float(np.max(np.abs(mono))) if mono.size else 0.0
    rms = float(np.sqrt(np.mean(np.square(mono)))) if mono.size else 0.0
    return LevelReading(seconds=seconds, samplerate=samplerate, channels=ch,
                        peak=peak, rms=rms, device=device)

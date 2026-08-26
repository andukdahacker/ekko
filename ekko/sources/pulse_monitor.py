"""Linux system-audio capture via PulseAudio/PipeWire monitor sources.

The Linux counterpart to the macOS process tap: capture "everyone else" in place
with **no setup, no virtual devices, and no output rerouting**. Every output sink
already exposes a `.monitor` source, so ekko records the default sink's monitor
(system audio) and your mic **simultaneously** with two `parec` streams, then
mixes them to one mono WAV on stop. Nothing is created or torn down.

`parec` (pulseaudio-utils) works on PulseAudio and, via `pipewire-pulse`, on
PipeWire. Everything is create-on-record / destroy-on-stop.

NOTE: developed on macOS; the `parec` invocation + mixing are unit-tested, but
run it on a live PulseAudio/PipeWire box before trusting it (see docs/linux.md).
"""
from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path

import numpy as np
import soundfile as sf

from ..models import MeetingKind
from .base import AudioSource

RATE = 16_000        # what Whisper wants; parec resamples for us


def _pactl(*args: str) -> str | None:
    try:
        return subprocess.run(["pactl", *args], capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def parec_available() -> bool:
    return shutil.which("parec") is not None


def pactl_available() -> bool:
    return shutil.which("pactl") is not None and _pactl("info") is not None


def default_monitor() -> str | None:
    """The default output sink's monitor source (system audio)."""
    sink = _pactl("get-default-sink")
    return f"{sink}.monitor" if sink else None


def default_mic() -> str | None:
    """The default input source, unless it's a monitor (then we have no mic)."""
    src = _pactl("get-default-source")
    if not src or src.endswith(".monitor"):
        return None
    return src


class MonitorSource(AudioSource):
    """Records system audio (sink monitor) + mic via two parec streams and mixes
    them to a mono WAV. No PulseAudio modules, no rerouting."""

    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self._path: Path | None = None
        self._procs: list[tuple[subprocess.Popen, Path, object]] = []
        self._sys_raw: Path | None = None   # system-monitor raw, for live preview

    def latest_audio(self, seconds: float):
        # Read the tail of the system-monitor raw file that parec is writing.
        if not self._sys_raw or not self._sys_raw.exists():
            return None
        try:
            nbytes = int(seconds * RATE) * 2          # s16le mono
            with open(self._sys_raw, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - nbytes))
                raw = f.read()
        except OSError:
            return None
        if len(raw) < 2:
            return None
        data = np.frombuffer(raw[: len(raw) // 2 * 2], dtype=np.int16)
        return (data.astype(np.float32) / 32768.0), RATE

    def _parec(self, source: str, raw_path: Path):
        f = open(raw_path, "wb")
        p = subprocess.Popen(
            ["parec", "-d", source, "--rate", str(RATE), "--channels", "1",
             "--format", "s16le"],
            stdout=f, stderr=subprocess.DEVNULL)
        return p, raw_path, f

    def start(self, kind: MeetingKind) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        from datetime import datetime
        stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
        self._path = self.out_dir / f"meeting-{stamp}.wav"

        monitor = default_monitor()
        mic = default_mic()
        if not monitor and not mic:
            raise OSError("no monitor/mic source found (is PulseAudio/PipeWire running?)")

        self._procs = []
        if monitor:
            self._sys_raw = self.out_dir / f".sys-{stamp}.raw"
            self._procs.append(self._parec(monitor, self._sys_raw))
        if mic:
            self._procs.append(self._parec(mic, self.out_dir / f".mic-{stamp}.raw"))

    def _read_raw(self, path: Path) -> np.ndarray:
        try:
            data = np.frombuffer(path.read_bytes(), dtype=np.int16)
        except Exception:
            return np.zeros(0, dtype=np.float32)
        return (data.astype(np.float32) / 32768.0)

    def stop(self) -> Path:
        streams: list[np.ndarray] = []
        for p, raw_path, f in self._procs:
            try:
                p.terminate()
                p.wait(timeout=2)
            except Exception:
                p.kill()
            try:
                f.close()
            except Exception:
                pass
            streams.append(self._read_raw(raw_path))

        streams = [s for s in streams if s.size]
        if streams:
            n = min(len(s) for s in streams)
            mixed = np.mean([s[:n] for s in streams], axis=0).astype(np.float32)
        else:
            mixed = np.zeros(0, dtype=np.float32)

        assert self._path is not None
        sf.write(self._path, mixed, RATE, subtype="PCM_16")

        for _p, raw_path, _f in self._procs:      # tidy up raw temp files
            try:
                raw_path.unlink(missing_ok=True)
            except Exception:
                pass
        return self._path

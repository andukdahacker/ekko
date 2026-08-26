"""Laptop audio capture (v1).

macOS reality check:
  - IN_PERSON: the built-in mic is enough — capture the default input device.
  - ONLINE (Meet/Teams): macOS won't let apps grab system output directly.
    The standard workaround is a virtual loopback device (e.g. BlackHole)
    combined into an Aggregate Device with your mic, so ONE input device
    carries both "everyone else" (system output) and "you" (mic). Point
    `input_device` at that aggregate. See README for the 5-minute setup.

Either way this class records a single mixed WAV — the pipeline stays
source-agnostic.
"""
from __future__ import annotations

import queue
import threading
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf

from ..models import MeetingKind
from .base import AudioSource
from .ringbuffer import RingBuffer as _RingBuffer

SAMPLE_RATE = 16_000   # what Whisper wants; avoids a resample step
CHANNELS = 1           # we ALWAYS write mono (Whisper wants mono)


class LaptopSource(AudioSource):
    def __init__(self, out_dir: Path, input_device: int | str | None = None):
        self.out_dir = out_dir
        self.input_device = input_device   # None = system default input
        self._q: queue.Queue = queue.Queue()
        self._stream: sd.InputStream | None = None
        self._writer: threading.Thread | None = None
        self._stop = threading.Event()
        self._path: Path | None = None
        self._ring = _RingBuffer(SAMPLE_RATE)   # recent audio for live preview

    def start(self, kind: MeetingKind) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        # Timestamp comes from the caller via filename; Date.now-style calls are
        # fine here (real runtime, not a workflow script).
        from datetime import datetime
        self._path = self.out_dir / f"meeting-{datetime.now():%Y%m%d-%H%M%S}.wav"
        self._stop.clear()

        # Capture ALL of the device's input channels, then downmix to mono. This
        # is what makes an online Aggregate Device work: it presents system audio
        # and the mic as SEPARATE channels (e.g. ch1-2 = BlackHole, ch3 = mic),
        # so a naive mono capture would grab only the first channel and silently
        # drop everyone (or you). Averaging the channels keeps both in one track.
        in_channels = self._device_input_channels()

        self._ring.clear()

        def on_audio(indata, frames, time_info, status):  # sounddevice callback
            if status:
                print(f"[capture] {status}")
            # indata is (frames, in_channels) float32 -> mono (frames, 1).
            mono = indata.mean(axis=1, keepdims=True) if indata.shape[1] > 1 else indata
            self._q.put(mono.copy())
            self._ring.push(mono[:, 0])

        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=in_channels,
            device=self.input_device,
            dtype="float32",
            callback=on_audio,
        )
        self._stream.start()
        self._writer = threading.Thread(target=self._drain_to_file, daemon=True)
        self._writer.start()

    def latest_audio(self, seconds: float):
        return self._ring.latest(seconds)

    def _device_input_channels(self) -> int:
        """How many input channels the chosen device exposes (>=1).

        None means the system default input; sounddevice resolves a name or
        index the same way it does for the stream itself.
        """
        dev = self.input_device
        if dev is None:
            dev = sd.default.device[0]
        info = sd.query_devices(dev)
        return max(1, int(info["max_input_channels"]))

    def _drain_to_file(self) -> None:
        with sf.SoundFile(self._path, mode="w", samplerate=SAMPLE_RATE,
                          channels=CHANNELS, subtype="PCM_16") as f:
            while not (self._stop.is_set() and self._q.empty()):
                try:
                    f.write(self._q.get(timeout=0.2))
                except queue.Empty:
                    continue

    def stop(self) -> Path:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
        self._stop.set()
        if self._writer is not None:
            self._writer.join()
        assert self._path is not None
        return self._path

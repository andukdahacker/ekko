"""A tiny thread-safe rolling audio buffer for the live-transcript preview.

Capture callbacks push mono float32 frames; the TUI pulls roughly the last N
seconds. Capped so memory stays bounded during a long meeting.
"""
from __future__ import annotations

import threading
from collections import deque

import numpy as np


class RingBuffer:
    def __init__(self, sample_rate: int, cap_seconds: float = 120.0):
        self.sample_rate = sample_rate
        self._cap = int(cap_seconds * sample_rate)
        self._chunks: deque[np.ndarray] = deque()
        self._n = 0
        self._total = 0                       # samples ever pushed (a monotonic cursor)
        self._lock = threading.Lock()

    def clear(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._n = 0
            self._total = 0

    def push(self, mono: np.ndarray) -> None:
        with self._lock:
            self._chunks.append(np.asarray(mono, dtype=np.float32).copy())
            self._n += len(mono)
            self._total += len(mono)
            while self._n > self._cap and self._chunks:
                self._n -= len(self._chunks.popleft())

    def latest(self, seconds: float):
        """(mono_float32, sample_rate) for ~the last `seconds`, or None if empty."""
        with self._lock:
            if not self._chunks:
                return None
            data = np.concatenate(list(self._chunks))
        n = int(seconds * self.sample_rate)
        return data[-n:], self.sample_rate

    def read_from(self, marker: int):
        """Everything pushed since sample-cursor `marker`, for accumulating a
        live transcript. Returns `(mono_float32, sample_rate, new_marker)` or
        None if there's nothing new. Pass 0 the first time, then feed back the
        returned cursor. If the buffer overflowed past `marker` the gap is
        skipped (bounded memory wins over completeness)."""
        with self._lock:
            if not self._chunks:
                return None
            data = np.concatenate(list(self._chunks))
            oldest = self._total - self._n        # cursor of data[0]
            total = self._total
        start = max(0, marker - oldest)
        if start >= len(data):
            return None
        return data[start:], self.sample_rate, total


# Back-compat alias used inside laptop.py
_RingBuffer = RingBuffer

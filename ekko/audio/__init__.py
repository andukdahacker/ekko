"""Platform audio backend selection.

`get_backend()` returns the right AudioBackend for the current OS. Backend
modules are imported lazily so importing this package never pulls in
platform-specific bindings (e.g. macOS Core Audio ctypes) on the wrong OS.
"""
from __future__ import annotations

import sys

from .base import (AudioBackend, AudioStatus, Check, RouteResult, SetupResult,
                   TeardownResult)
from .config_writer import set_capture_input_device

_backend: AudioBackend | None = None


class UnsupportedBackend(AudioBackend):
    """Fallback for platforms without an online-capture implementation. In-person
    recording still works (that path never touches a backend)."""
    platform = sys.platform
    supports_online = False

    def check(self) -> AudioStatus:
        return AudioStatus(
            ready=False,
            message=f"online capture isn't supported on {self.platform} yet — "
                    f"in-person recording works.")


def get_backend() -> AudioBackend:
    global _backend
    if _backend is None:
        if sys.platform == "darwin":
            from .macos import CoreAudioBackend
            _backend = CoreAudioBackend()
        elif sys.platform.startswith("linux"):
            from .linux import PulseAudioBackend
            _backend = PulseAudioBackend()
        else:
            _backend = UnsupportedBackend()
    return _backend


__all__ = ["AudioBackend", "AudioStatus", "Check", "RouteResult", "SetupResult",
           "TeardownResult", "set_capture_input_device", "get_backend"]

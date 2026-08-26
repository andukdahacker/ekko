"""Platform audio-control backend interface.

Everything in ekko is cross-platform except *controlling* the OS audio graph for
online capture (creating a device that carries system audio + mic, and — on
macOS — routing output through it). That platform-specific piece lives behind
this interface, with one implementation per OS:

  - macOS: Core Audio Aggregate + Multi-Output devices (see macos.py)
  - Linux: PulseAudio/PipeWire null-sink + loopbacks (see linux.py)

In-person capture needs none of this — `sources/laptop.py` (sounddevice) already
works everywhere. Pick a backend with `get_backend()` in this package's __init__.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Check:
    """One line in an `audio check` report."""
    label: str
    ok: bool


@dataclass
class AudioStatus:
    ready: bool                                   # is online capture ready to go?
    checks: list[Check] = field(default_factory=list)
    detail: str | None = None                     # e.g. "output → Multi-Output Device"
    message: str | None = None                    # next-step / how-to-fix hint


@dataclass
class SetupResult:
    ok: bool
    message: str
    capture_device: str | None = None             # value written to config.input_device
    created: tuple[str, ...] = ()
    reused: tuple[str, ...] = ()


@dataclass
class TeardownResult:
    removed: tuple[str, ...] = ()
    note: str | None = None


@dataclass
class RouteResult:
    """Result of routing output for capture. `token` is opaque, handed back to
    restore_output(); `routed_to` is a display name (None = nothing changed)."""
    token: object | None = None
    routed_to: str | None = None


class AudioBackend(ABC):
    platform: str = "unknown"
    supports_online: bool = False

    @property
    def capture_device_name(self) -> str | None:
        """What to write to config `capture.input_device` after setup, or None to
        leave it at the system default."""
        return None

    def online_source(self, out_dir):
        """An AudioSource for ONLINE capture that this backend provides directly
        (e.g. macOS process-tap capture), or None to use the default LaptopSource.
        `out_dir` is where the WAV should be written."""
        return None

    @abstractmethod
    def check(self) -> AudioStatus:
        """Report readiness of the online-capture rig."""

    def setup(self, *, write_config: bool = True,
              config_path: Path | None = None) -> SetupResult:
        return SetupResult(ok=False,
                           message=f"online-capture setup isn't supported on "
                                   f"{self.platform} yet.")

    def teardown(self) -> TeardownResult:
        return TeardownResult(removed=(), note=f"nothing to remove on {self.platform}.")

    # --- capture-time hooks (defaults are no-ops) ---------------------------
    def prepare_capture(self, kind) -> None:
        """Called just before a recording starts (e.g. Linux sets PULSE_SOURCE)."""

    def finish_capture(self, kind) -> None:
        """Called just after a recording ends (undo prepare_capture)."""

    def route_output_for_capture(self) -> RouteResult:
        """macOS switches the default output to a multi-output; other platforms
        don't need to (they tap a monitor source), so this is a no-op."""
        return RouteResult()

    def restore_output(self, token) -> bool:
        return False

    def online_hint(self) -> str | None:
        """One-line hint about the online-capture prerequisite, if unmet."""
        return None

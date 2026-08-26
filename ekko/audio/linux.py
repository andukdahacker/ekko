"""Linux online-capture backend — PulseAudio / PipeWire, in place.

Mirrors the macOS process-tap experience: **no setup, no virtual devices, no
output rerouting**. Linux exposes a `.monitor` source for every output sink, so
ekko records the default sink's monitor (system audio) + your mic directly and
mixes them (see `sources/pulse_monitor.MonitorSource`). Nothing is created or
torn down; you don't route your output.

`teardown()` still removes the null-sink + loopbacks that *older* ekko builds
created, so upgrading users can clean those up.

NOTE: developed on macOS; verify on a live PulseAudio/PipeWire box — see
docs/linux.md.
"""
from __future__ import annotations

import subprocess

from ..sources.pulse_monitor import (default_mic, default_monitor,
                                      pactl_available, parec_available)
from .base import AudioBackend, AudioStatus, Check, SetupResult, TeardownResult

OLD_SINK_NAME = "ekko_mix"          # created by the pre-monitor ekko approach


class PulseAudioBackend(AudioBackend):
    platform = "Linux"
    supports_online = True

    def online_source(self, out_dir):
        from ..sources.pulse_monitor import MonitorSource
        return MonitorSource(out_dir) if parec_available() else None

    def online_hint(self) -> str:
        return ("Needs PulseAudio/PipeWire tools (`pactl` + `parec`). Install e.g.: "
                "sudo apt install pulseaudio-utils   (or pipewire-pulse)")

    # --- status -------------------------------------------------------------
    def check(self) -> AudioStatus:
        have_pactl = pactl_available()
        have_parec = parec_available()
        monitor = default_monitor() if have_pactl else None
        mic = default_mic() if have_pactl else None
        checks = [
            Check("PulseAudio/PipeWire (pactl)", have_pactl),
            Check("parec recorder", have_parec),
            Check("system-audio monitor source", bool(monitor)),
            Check("microphone", bool(mic)),
        ]
        ready = bool(have_pactl and have_parec and monitor)
        msg = None
        if not (have_pactl and have_parec):
            msg = self.online_hint()
        elif not monitor:
            msg = "No default output sink found — start audio playback once, then retry."
        return AudioStatus(
            ready=ready, checks=checks,
            detail=(f"records {monitor} + mic in place — no setup, no rerouting"
                    if monitor else None),
            message=msg)

    # --- setup / teardown ---------------------------------------------------
    def setup(self, *, write_config: bool = True, config_path=None) -> SetupResult:
        if not (pactl_available() and parec_available()):
            return SetupResult(ok=False, message=self.online_hint())
        return SetupResult(ok=True, message="no setup needed — ekko records the "
                           "system monitor + mic directly (no virtual devices).")

    def teardown(self) -> TeardownResult:
        # Remove leftover modules from the older null-sink+loopback approach.
        if not pactl_available():
            return TeardownResult(removed=(), note=self.online_hint())
        ids = self._old_module_ids()
        for mid in ids:
            try:
                subprocess.run(["pactl", "unload-module", mid], check=False,
                               capture_output=True)
            except OSError:
                pass
        return TeardownResult(removed=(OLD_SINK_NAME,) if ids else (),
                              note=None if ids else "nothing to remove.")

    def _old_module_ids(self) -> list[str]:
        try:
            out = subprocess.run(["pactl", "list", "short", "modules"],
                                 capture_output=True, text=True, check=True).stdout
        except (OSError, subprocess.CalledProcessError):
            return []
        ids = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and OLD_SINK_NAME in parts[2]:
                ids.append(parts[0])
        return ids

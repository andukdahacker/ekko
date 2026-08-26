"""Facade over the platform audio backend (see the `audio` package).

Kept as a stable import surface for the CLI and TUI: `from . import audio_setup
as A; A.check()`. All real work is delegated to the OS-specific backend chosen by
`audio.get_backend()` (macOS Core Audio, Linux PulseAudio/PipeWire, …).
"""
from __future__ import annotations

from pathlib import Path

from .audio import (AudioStatus, RouteResult, SetupResult, TeardownResult,
                    get_backend, set_capture_input_device)

__all__ = ["AudioStatus", "SetupResult", "TeardownResult", "RouteResult",
           "set_capture_input_device", "backend", "check", "setup", "teardown",
           "route_output_for_capture", "restore_output", "prepare_capture",
           "finish_capture", "capture_device_name", "online_hint", "supports_online"]


def backend():
    return get_backend()


def check() -> AudioStatus:
    return get_backend().check()


def setup(*, write_config: bool = True, config_path: Path | None = None) -> SetupResult:
    return get_backend().setup(write_config=write_config, config_path=config_path)


def teardown() -> TeardownResult:
    return get_backend().teardown()


def route_output_for_capture() -> RouteResult:
    return get_backend().route_output_for_capture()


def restore_output(token) -> bool:
    return get_backend().restore_output(token)


def prepare_capture(kind) -> None:
    get_backend().prepare_capture(kind)


def finish_capture(kind) -> None:
    get_backend().finish_capture(kind)


def capture_device_name() -> str | None:
    return get_backend().capture_device_name


def online_hint() -> str | None:
    return get_backend().online_hint()


def supports_online() -> bool:
    return get_backend().supports_online

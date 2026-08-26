"""macOS online-capture backend — Core Audio Aggregate + Multi-Output devices.

`setup()` creates two ekko-managed devices (idempotent, matched by a stable UID,
never touching hand-made devices):

  - "ekko capture" (input Aggregate): BlackHole + your mic — ekko records here.
  - "ekko output"  (Multi-Output):    BlackHole + your speakers — captured AND
    still audible.

Recording online also routes the system default output to "ekko output" and
restores it afterward. Installing the BlackHole driver is the one prerequisite
this can't do (`brew install blackhole-2ch`).
"""
from __future__ import annotations

from pathlib import Path

from ..sources import coreaudio as ca
from .base import (AudioBackend, AudioStatus, Check, RouteResult, SetupResult,
                   TeardownResult)
from .config_writer import set_capture_input_device

BLACKHOLE_NAME = "BlackHole"
CAPTURE_NAME = "ekko capture"
CAPTURE_UID = "com.ekko.capture"
OUTPUT_NAME = "ekko output"
OUTPUT_UID = "com.ekko.output"


class CoreAudioBackend(AudioBackend):
    platform = "macOS"
    supports_online = True

    def __init__(self):
        # macOS 14.4+ can tap system audio in place — no BlackHole, no rerouting,
        # volume unaffected. Older macOS falls back to the BlackHole rig.
        try:
            from ..sources.coreaudio_tap import tap_supported
            self.tap = tap_supported()
        except Exception:
            self.tap = False

    @property
    def capture_device_name(self) -> str | None:
        return None if self.tap else CAPTURE_NAME   # tap source ignores input_device

    def online_source(self, out_dir):
        if not self.tap:
            return None
        from ..sources.coreaudio_tap import TapSource
        return TapSource(out_dir)

    # --- device selection ---------------------------------------------------
    def _blackhole(self) -> ca.CADevice | None:
        return ca.find_by_name(BLACKHOLE_NAME, need_input=True)

    def _pick_physical_mic(self) -> ca.CADevice | None:
        d = ca.default_device(output=False)
        if d and d.has_input and not d.is_aggregate \
                and BLACKHOLE_NAME.lower() not in d.name.lower():
            return d
        return (ca.find_by_name("MacBook Pro Microphone", need_input=True)
                or ca.find_by_name("Microphone", need_input=True))

    def _pick_physical_speakers(self) -> ca.CADevice | None:
        d = ca.default_device(output=True)
        if d and d.has_output and not d.is_aggregate \
                and BLACKHOLE_NAME.lower() not in d.name.lower():
            return d
        return (ca.find_by_name("MacBook Pro Speakers", need_output=True)
                or ca.find_by_name("Speakers", need_output=True)
                or ca.find_by_name("Headphones", need_output=True))

    def online_hint(self) -> str:
        return "BlackHole not found. Install it with:  brew install blackhole-2ch"

    # --- status -------------------------------------------------------------
    def check(self) -> AudioStatus:
        if self.tap:
            mic = self._pick_physical_mic()
            return AudioStatus(
                ready=bool(mic),
                checks=[Check("system-audio tap (macOS 14.4+)", True),
                        Check("microphone", bool(mic))],
                detail="captures system audio in place — no BlackHole, no output "
                       "rerouting",
                message=None if mic else "No microphone found.")
        bh = self._blackhole()
        cap = ca.find_by_uid(CAPTURE_UID)
        out = ca.find_by_uid(OUTPUT_UID)
        default_out = ca.default_device(output=True)
        routed = bool(default_out and (default_out.uid == OUTPUT_UID
                                       or default_out.is_aggregate))
        checks = [
            Check("BlackHole driver", bool(bh)),
            Check("ekko capture (system + mic)", bool(cap)),
            Check("ekko output (speakers + BlackHole)", bool(out)),
            Check("system output routed for capture", routed),
        ]
        ready = bool(bh and cap and out)
        msg = None
        if not bh:
            msg = f"{self.online_hint()}\nThen run `ekko audio setup`."
        elif not ready:
            msg = "Run `ekko audio setup` to create the missing device(s)."
        return AudioStatus(
            ready=ready, checks=checks,
            detail=f"output → {default_out.name}" if default_out else None,
            message=msg)

    # --- setup --------------------------------------------------------------
    def setup(self, *, write_config: bool = True,
              config_path: Path | None = None) -> SetupResult:
        if self.tap:
            return SetupResult(ok=True, message="no setup needed — macOS 14.4+ "
                               "captures system audio via a process tap (no "
                               "BlackHole, no device to create).")
        bh = self._blackhole()
        if not bh:
            return SetupResult(ok=False, message=self.online_hint())

        created: list[str] = []
        reused: list[str] = []

        cap = ca.find_by_uid(CAPTURE_UID)
        if cap:
            reused.append(CAPTURE_NAME)
        else:
            mic = self._pick_physical_mic()
            if not mic:
                return SetupResult(ok=False, message="No physical microphone found "
                                   "to pair with BlackHole.")
            ca.create_aggregate(CAPTURE_NAME, CAPTURE_UID, [bh.uid, mic.uid],
                                master_uid=mic.uid, stacked=False, drift_uids={bh.uid})
            cap = ca.wait_for_uid(CAPTURE_UID)
            created.append(f"{CAPTURE_NAME} (BlackHole + {mic.name})")

        out = ca.find_by_uid(OUTPUT_UID)
        if out:
            reused.append(OUTPUT_NAME)
        else:
            spk = self._pick_physical_speakers()
            if not spk:
                return SetupResult(ok=False, message="No physical speakers/headphones "
                                   "found to pair with BlackHole.")
            ca.create_aggregate(OUTPUT_NAME, OUTPUT_UID, [spk.uid, bh.uid],
                                master_uid=spk.uid, stacked=True, drift_uids={bh.uid})
            out = ca.wait_for_uid(OUTPUT_UID)
            created.append(f"{OUTPUT_NAME} (BlackHole + {spk.name})")

        if not (cap and out):
            return SetupResult(ok=False, message="Devices were created but did not "
                               "appear in time; re-run `ekko audio check`.",
                               created=tuple(created), reused=tuple(reused))

        if write_config:
            set_capture_input_device(config_path, CAPTURE_NAME)

        parts = []
        if created:
            parts.append("created " + "; ".join(created))
        if reused:
            parts.append("reused " + ", ".join(reused))
        return SetupResult(ok=True, message="; ".join(parts) or "already set up",
                           capture_device=CAPTURE_NAME,
                           created=tuple(created), reused=tuple(reused))

    # --- teardown -----------------------------------------------------------
    def teardown(self) -> TeardownResult:
        # Always attempt to remove the BlackHole-based ekko devices by UID — even
        # on tap-capable macOS, so users can clean up a pre-tap setup. (Tap
        # capture itself creates no persistent devices.)
        removed: list[str] = []
        note: str | None = None

        out = ca.find_by_uid(OUTPUT_UID)
        if out:
            cur = ca.default_device(output=True)
            if cur and cur.id == out.id:
                spk = self._pick_physical_speakers()
                if spk:
                    ca.set_default_device(spk.id, output=True)
                    note = f"system output moved to \"{spk.name}\""
            ca.destroy_aggregate(out.id)
            ca.wait_for_uid(OUTPUT_UID, present=False)
            removed.append(OUTPUT_NAME)

        cap = ca.find_by_uid(CAPTURE_UID)
        if cap:
            ca.destroy_aggregate(cap.id)
            ca.wait_for_uid(CAPTURE_UID, present=False)
            removed.append(CAPTURE_NAME)

        return TeardownResult(removed=tuple(removed), note=note)

    # --- output routing -----------------------------------------------------
    def route_output_for_capture(self) -> RouteResult:
        if self.tap:
            return RouteResult()          # tap capture never touches the output
        try:
            out = ca.find_by_uid(OUTPUT_UID)
            if not out:
                return RouteResult()
            cur = ca.default_device(output=True)
            if cur and cur.id == out.id:
                return RouteResult()
            ca.set_default_device(out.id, output=True)
            return RouteResult(token=(cur.id if cur else None), routed_to=out.name)
        except Exception:
            return RouteResult()

    def restore_output(self, token) -> bool:
        if token is None:
            return False
        try:
            ca.set_default_device(int(token), output=True)
            return True
        except Exception:
            return False

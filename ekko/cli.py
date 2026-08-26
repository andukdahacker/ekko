"""CLI entry point.

Commands:
  ekko record            # start capturing; press Enter to stop, then process
  ekko process <file>    # process an already-recorded audio file
  ekko list              # list past meetings

Manual trigger by design (no calendar watcher). `record` covers both online and
in-person via --kind.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from .config import build_pipeline, build_source, load_config
from .models import MeetingKind


def _device_arg(value: str | None) -> int | str | None:
    """A device may be given as an index ('3') or a name ('Aggregate Device')."""
    if value is None:
        return None
    return int(value) if value.isdigit() else value


def _route_output_for_capture():
    """CLI wrapper around the backend's output routing that prints what it did.
    Returns the opaque restore token (or None)."""
    from . import audio_setup as A
    r = A.route_output_for_capture()
    if r.routed_to:
        print(f"↪ routed system output to \"{r.routed_to}\" (you'll still hear it)")
    return r.token


def _restore_output(token) -> None:
    from . import audio_setup as A
    if A.restore_output(token):
        print("↩ restored system output")


def _record(args) -> None:
    cfg = load_config(Path(args.config) if args.config else None)
    pipeline = build_pipeline(cfg)
    kind = MeetingKind(args.kind)

    from . import audio_setup as A
    override = _device_arg(args.device)
    if override is not None:
        source = build_source(cfg)                  # explicit device -> plain capture
        source.input_device = override
    else:
        source = build_source(cfg, kind)            # macOS online -> tap source
    if kind is MeetingKind.ONLINE and override is None and not A.check().ready:
        print("⚠ Online meeting, but the capture rig isn't set up — you may only "
              "record your mic.\n  Run `ekko audio setup` (one-time), or pass --device.")
        hint = A.online_hint()
        if hint:
            print(f"  {hint}")

    token = None
    if kind is MeetingKind.ONLINE:
        A.prepare_capture(kind)                     # e.g. Linux points PULSE_SOURCE at the mix
        if not args.no_auto_route:
            token = _route_output_for_capture()     # macOS: switch default output

    print(f"● Recording ({kind.value}). Press Enter to stop…")
    started = datetime.now()
    source.start(kind)
    try:
        input()
    except KeyboardInterrupt:
        pass
    finally:
        audio_path = source.stop()
        _restore_output(token)                      # restore as soon as capture ends
        A.finish_capture(kind)
    print(f"■ Saved audio: {audio_path}")

    title = args.title or f"{kind.value} meeting {started:%Y-%m-%d %H:%M}"
    pipeline.process(audio_path, title=title, kind=kind, started_at=started)
    print("✓ Done.")


def _process(args) -> None:
    cfg = load_config(Path(args.config) if args.config else None)
    pipeline = build_pipeline(cfg)
    kind = MeetingKind(args.kind)
    title = args.title or Path(args.file).stem
    pipeline.process(Path(args.file), title=title, kind=kind)
    print("✓ Done.")


def _devices(args) -> None:
    from .sources.devices import list_input_devices, probe_level

    devs = list_input_devices()
    if not devs:
        print("No input-capable audio devices found.")
        return

    print("Input devices (record with --device <index|name>):")
    for d in devs:
        tags = []
        if d.is_default:
            tags.append("default")
        if d.looks_like_loopback:
            tags.append("loopback?")
        tag_str = f"  [{', '.join(tags)}]" if tags else ""
        print(f"  [{d.index:2}] {d.name:<40} IN:{d.channels} {d.samplerate}Hz{tag_str}")

    if not any(d.looks_like_loopback for d in devs):
        print("\nNo loopback/aggregate device detected. For ONLINE meetings you need "
              "system audio —\nsee the README \"Online meetings\" setup (BlackHole + "
              "Aggregate Device).")

    if args.test is not None:
        dev = _device_arg(args.test)
        print(f"\nRecording {args.seconds:g}s from device {dev!r} to measure level…")
        print("  (For an online aggregate, play some audio or talk during this.)")
        r = probe_level(dev, seconds=args.seconds)
        bar = "█" * min(40, int(r.peak * 40))
        mix = f" ({r.channels}ch → mono)" if r.channels > 1 else ""
        print(f"  peak={r.peak:.3f} rms={r.rms:.3f}{mix}  |{bar:<40}|")
        if r.silent:
            print("  ✗ Essentially silent — this device isn't carrying audio right now.")
        else:
            print("  ✓ Audio detected — this device is live.")


def _audio_check(args) -> None:
    from . import audio_setup as A
    s = A.check()
    tick = lambda ok: "✓" if ok else "✗"
    for c in s.checks:
        print(f"  {tick(c.ok)} {c.label}")
    if s.detail:
        print(f"  {s.detail}")
    if s.ready:
        print("\nReady for online meetings. `ekko record --kind online` will "
              "capture both sides.")
    elif s.message:
        print(f"\n{s.message}")


def _audio_setup(args) -> None:
    from . import audio_setup as A
    r = A.setup(write_config=True,
                config_path=Path(args.config) if args.config else None)
    if not r.ok:
        print(f"✗ {r.message}")
        return
    for c in r.created:
        print(f"  + created {c}")
    for name in r.reused:
        print(f"  · reused {name}")
    if r.capture_device:
        print(f"\n✓ Online capture ready. Config set to input_device = \"{r.capture_device}\".")
        print("  `ekko record --kind online` now captures system audio + your mic.")
    else:
        print(f"\n✓ {r.message}")


def _audio_teardown(args) -> None:
    from . import audio_setup as A
    r = A.teardown()
    if not r.removed:
        print(r.note or "No ekko-managed audio devices to remove.")
        return
    for name in r.removed:
        print(f"  - removed {name}")
    if r.note:
        print(f"  ↩ {r.note}")
    print("\n✓ Removed ekko-managed audio devices.")


def _tui(args) -> None:
    from .tui import run
    run(Path(args.config) if args.config else None)


def _update(args) -> None:
    """Upgrade ekko in place. Reinstalls from wherever pip recorded the install
    origin (direct_url.json — PEP 610), falling back to the baked repo URL.
    Source checkouts / editable installs are told to `git pull` instead.
    Override the source with EKKO_SPEC."""
    import importlib.metadata as im
    import json
    import os
    import subprocess
    import sys
    import sysconfig
    from pathlib import Path

    import ekko

    try:
        print(f"current version: {im.version('ekko')}")
    except im.PackageNotFoundError:
        pass

    # An editable install / plain checkout lives in the source tree, not under
    # site-packages — upgrading it via pip would be wrong; point at git instead.
    pkg = Path(ekko.__file__).resolve().parent
    purelib = Path(sysconfig.get_paths()["purelib"]).resolve()
    installed = purelib in pkg.parents or "site-packages" in pkg.parts
    if not installed:
        print("Running from a source checkout (editable/dev). Update with:")
        print(f"  git -C {pkg.parent} pull")
        return

    spec = os.environ.get("EKKO_SPEC")
    if not spec:
        durl = None
        try:
            durl = im.distribution("ekko").read_text("direct_url.json")
        except Exception:
            durl = None
        if durl:
            d = json.loads(durl)
            if d.get("dir_info", {}).get("editable"):
                path = d.get("url", "").replace("file://", "") or str(pkg.parent)
                print(f"Editable install. Update with:\n  git -C {path} pull")
                return
            spec = (f"{d['vcs_info']['vcs']}+{d['url']}"
                    if "vcs_info" in d else d["url"])       # git URL, or local path
        else:
            spec = f"git+{ekko.REPO_URL}"                   # baked fallback (never bare PyPI)

    print(f"updating from: {spec}")
    rc = subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade",
                         "--quiet", spec]).returncode
    if rc == 0:
        print("✓ updated. Restart ekko to use the new version.")
    else:
        print("✗ update failed — see pip output above.", file=sys.stderr)
        raise SystemExit(rc)


def _uninstall(args) -> None:
    """Remove the ekko install: audio devices, the `ekko` launcher, and the venv.
    With --purge, also delete ~/.ekko data (config, db, recordings)."""
    import shutil
    import sys as _sys

    import ekko
    pkg = Path(ekko.__file__).resolve().parent
    if "site-packages" not in pkg.parts:
        print("Running from a source checkout — nothing to uninstall here.")
        print("  Remove the launcher + venv manually, e.g.:")
        print("    rm -f ~/.local/bin/ekko && rm -rf ./.venv")
        return

    venv = Path(_sys.prefix).resolve()                 # the isolated venv
    home = Path("~/.ekko").expanduser()
    launcher = Path("~/.local/bin/ekko").expanduser()

    # what we'll remove
    targets = []
    if launcher.exists() or launcher.is_symlink():
        try:
            points_into_venv = str(launcher.resolve()).startswith(str(venv))
        except Exception:
            points_into_venv = False
        if points_into_venv:                           # don't touch an unrelated launcher
            targets.append(("launcher", launcher))
    if venv.exists():
        targets.append(("venv", venv))
    if args.purge and home.exists():
        targets.append(("data (config, db, recordings)", home))

    print("This will remove:")
    for label, path in targets:
        print(f"  - {label}: {path}")
    if not args.purge and home.exists():
        print(f"  (keeping your data at {home} — use --purge to remove it too)")

    if not args.yes:
        try:
            if input("\nProceed? [y/N] ").strip().lower() not in ("y", "yes"):
                print("Aborted.")
                return
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return

    # 1. tear down any audio devices/modules ekko created
    try:
        from . import audio_setup as A
        A.teardown()
        print("  ✓ audio devices cleaned up")
    except Exception:
        pass

    # 2. remove launcher + venv (+ data). Removing our own venv mid-run is fine on
    # Unix — open files persist until exit.
    for label, path in targets:
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            else:
                shutil.rmtree(path)
            print(f"  ✓ removed {label}")
        except Exception as e:
            print(f"  ✗ couldn't remove {label}: {e}")

    print("\nekko uninstalled." + ("" if args.purge else
          f" Your data remains at {home}."))


def _list(args) -> None:
    cfg = load_config(Path(args.config) if args.config else None)
    from .store.sqlite import SqliteStore
    store = SqliteStore(Path(cfg.get("data_dir", "~/.ekko")).expanduser()
                        / "ekko.db")
    for mid, when, title in store.list_meetings():
        print(f"  [{mid}] {when}  {title}")


def main() -> None:
    p = argparse.ArgumentParser(prog="ekko")
    p.add_argument("--config", help="path to config.toml")
    # No subcommand required: bare `ekko` opens the TUI (like `lazygit`).
    sub = p.add_subparsers(dest="cmd")

    r = sub.add_parser("record", help="capture a meeting from the laptop")
    r.add_argument("--kind", choices=["online", "in_person"], default="in_person")
    r.add_argument("--title")
    r.add_argument("--device", help="input device index or name (overrides config)")
    r.add_argument("--no-auto-route", action="store_true",
                   help="don't switch system output to the ekko multi-output while recording")
    r.set_defaults(func=_record)

    dv = sub.add_parser("devices", help="list audio input devices (+ optional level test)")
    dv.add_argument("--test", metavar="INDEX_OR_NAME",
                    help="record a few seconds from this device and report its level")
    dv.add_argument("--seconds", type=float, default=3.0,
                    help="test duration (default 3)")
    dv.set_defaults(func=_devices)

    pr = sub.add_parser("process", help="process an existing audio file")
    pr.add_argument("file")
    pr.add_argument("--kind", choices=["online", "in_person"], default="in_person")
    pr.add_argument("--title")
    pr.set_defaults(func=_process)

    au = sub.add_parser("audio", help="set up / check online-capture devices (macOS + Linux)")
    au_sub = au.add_subparsers(dest="audio_cmd", required=True)
    au_sub.add_parser("check", help="report online-capture device status") \
          .set_defaults(func=_audio_check)
    au_sub.add_parser("setup", help="create the online-capture devices, write config") \
          .set_defaults(func=_audio_setup)
    au_sub.add_parser("teardown", help="remove the ekko-managed capture devices") \
          .set_defaults(func=_audio_teardown)

    ls = sub.add_parser("list", help="list past meetings")
    ls.set_defaults(func=_list)

    tu = sub.add_parser("tui", help="open the terminal UI")
    tu.set_defaults(func=_tui)

    up = sub.add_parser("update", help="upgrade ekko in place")
    up.set_defaults(func=_update)

    un = sub.add_parser("uninstall", help="remove ekko (launcher, venv; --purge for data)")
    un.add_argument("--purge", action="store_true",
                    help="also delete ~/.ekko data (config, db, recordings)")
    un.add_argument("--yes", action="store_true", help="skip confirmation")
    un.set_defaults(func=_uninstall)

    args = p.parse_args()
    cmd = getattr(args, "cmd", None)

    # Kick a best-effort update check in the background so it runs *during* the
    # command and never adds latency; the nudge below reads from its cache.
    if cmd != "update":
        try:
            import threading
            from .update_check import refresh_if_stale
            threading.Thread(target=refresh_if_stale, daemon=True).start()
        except Exception:
            pass

    func = getattr(args, "func", _tui)   # bare `ekko` -> TUI
    func(args)

    if cmd != "update":
        try:
            from .update_check import nudge_line
            line = nudge_line()
            if line:
                print(line, file=sys.stderr)
        except Exception:
            pass


if __name__ == "__main__":
    main()

"""Small cross-platform desktop helpers (play audio, reveal a file, copy text).

All best-effort and non-blocking: they launch a detached helper and return
whether one was found, so the TUI can show a friendly message on failure.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def _spawn(cmd: list[str]) -> subprocess.Popen | None:
    try:
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except OSError:
        return None


def play_audio(path: Path) -> subprocess.Popen | None:
    """Play an audio file via the system player. Returns the player process so
    the caller can stop it (and not leave it playing after quit), or None if no
    player was found. `xdg-open` hands off to another app, so it can't be
    stopped and is only a last resort."""
    if sys.platform == "darwin":
        return _spawn(["afplay", str(path)])
    for player, args in (("paplay", []), ("ffplay", ["-nodisp", "-autoexit",
                          "-loglevel", "quiet"]), ("mpv", ["--no-video"]),
                         ("aplay", [])):
        if shutil.which(player):
            return _spawn([player, *args, str(path)])
    if shutil.which("xdg-open"):
        return _spawn(["xdg-open", str(path)])
    return None


def reveal_file(path: Path) -> bool:
    """Reveal a file in Finder / the file manager."""
    if sys.platform == "darwin":
        return _spawn(["open", "-R", str(path)])
    if shutil.which("xdg-open"):
        return _spawn(["xdg-open", str(path.parent)])
    return False


def copy_text(text: str) -> bool:
    """Copy text to the clipboard."""
    if sys.platform == "darwin":
        candidates = [["pbcopy"]]
    else:
        candidates = [["wl-copy"], ["xclip", "-selection", "clipboard"],
                      ["xsel", "--clipboard", "--input"]]
    for cmd in candidates:
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text.encode(), check=True)
                return True
            except (OSError, subprocess.CalledProcessError):
                continue
    return False

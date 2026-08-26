"""Best-effort 'a newer ekko is available' nudge.

Design (same as npm/brew/gh): never block a command on the network. We cache the
latest known version in ~/.ekko/.update-check.json and only re-fetch when that
cache is stale (once/day), time-boxed to a short timeout. The nudge is shown from
the cache, so a fast command is never slowed by the check.

Version source: the latest GitHub release/tag for REPO_URL (this is a git-based
distribution). Silently no-ops when: the check is disabled, the repo is the
unconfigured `OWNER/ekko` placeholder, offline, or anything errors.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

CACHE = Path(os.path.expanduser("~/.ekko/.update-check.json"))
MAX_AGE = 24 * 3600          # re-check at most once a day
TIMEOUT = 1.5                # seconds; keep the occasional check snappy


def installed_version() -> str | None:
    try:
        import importlib.metadata as im
        return im.version("ekko")
    except Exception:
        return None


def _disabled() -> bool:
    from . import REPO_URL
    return (os.environ.get("EKKO_NO_UPDATE_CHECK") not in (None, "", "0")
            or "OWNER/" in REPO_URL)          # unconfigured fork → don't nudge


def _repo_slug() -> str | None:
    from . import REPO_URL
    m = re.search(r"github\.com[/:]([^/]+/[^/.]+)", REPO_URL)
    return m.group(1) if m else None


def _read_cache() -> dict:
    try:
        return json.loads(CACHE.read_text())
    except Exception:
        return {}


def _write_cache(data: dict) -> None:
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(data))
    except Exception:
        pass


def _fetch_latest() -> str | None:
    """Latest release tag (fallback: newest tag) for the repo, or None."""
    slug = _repo_slug()
    if not slug:
        return None
    import urllib.request
    headers = {"User-Agent": "ekko-update-check",
               "Accept": "application/vnd.github+json"}

    def _get(url):
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.load(r)

    try:
        tag = _get(f"https://api.github.com/repos/{slug}/releases/latest").get("tag_name")
    except Exception:
        tag = None
    if not tag:
        try:
            tags = _get(f"https://api.github.com/repos/{slug}/tags")
            tag = tags[0]["name"] if tags else None
        except Exception:
            tag = None
    return tag.lstrip("vV") if tag else None


def refresh_if_stale(*, force: bool = False) -> None:
    """Re-fetch the latest version if the cache is older than MAX_AGE. Best-effort
    and time-boxed; safe to call on every command."""
    if _disabled():
        return
    cache = _read_cache()
    if not force and (time.time() - cache.get("checked_at", 0)) < MAX_AGE:
        return
    latest = _fetch_latest()
    _write_cache({"checked_at": time.time(),
                  "latest": latest or cache.get("latest")})


def _is_newer(latest: str, current: str) -> bool:
    try:
        from packaging.version import Version
        return Version(latest) > Version(current)
    except Exception:
        nums = lambda v: [int(x) for x in re.findall(r"\d+", v)]
        return nums(latest) > nums(current)


def available_update() -> str | None:
    """The cached latest version if it's newer than what's installed, else None."""
    if _disabled():
        return None
    latest = _read_cache().get("latest")
    cur = installed_version()
    if latest and cur and _is_newer(latest, cur):
        return latest
    return None


def nudge_line() -> str | None:
    latest = available_update()
    if not latest:
        return None
    return (f"↑ ekko {latest} is available (you have {installed_version()}). "
            f"Run `ekko update`.")

"""Config loading + pipeline assembly.

Reads a TOML file (see config.example.toml) and constructs the concrete
implementations behind each interface. This is the single place where "which
provider / which source" is decided — swapping Gemini for Claude, or adding a
Notion integration, is an edit here, not in the pipeline.
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

from .identify.manual import ManualIdentifier
from .integrations.markdown import MarkdownIntegration
from .pipeline import Pipeline
from .sources.laptop import LaptopSource
from .store.sqlite import SqliteStore
from .summarize import build_summarizer
from .transcribe.diarize import Diarizer
from .transcribe.whisper import WhisperTranscriber

DEFAULT_CONFIG = Path.home() / ".ekko" / "config.toml"


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(p)).resolve()


def load_config(path: Path | None = None) -> dict:
    path = path or DEFAULT_CONFIG
    if not path.exists():
        raise SystemExit(f"No config at {path}. Copy config.example.toml there "
                         f"and edit it.")
    with open(path, "rb") as f:
        return tomllib.load(f)


def build_source(cfg: dict, kind=None):
    """The capture source. For ONLINE meetings the platform audio backend may
    supply a dedicated source (e.g. macOS process-tap capture); otherwise we use
    the mic/aggregate via LaptopSource."""
    s = cfg.get("capture", {})
    out_dir = _expand(s.get("audio_dir", "~/.ekko/audio"))
    from .models import MeetingKind
    if kind is MeetingKind.ONLINE:
        from .audio import get_backend
        src = get_backend().online_source(out_dir)
        if src is not None:
            return src
    return LaptopSource(out_dir=out_dir, input_device=s.get("input_device"))


def build_pipeline(cfg: dict) -> Pipeline:
    data_dir = _expand(cfg.get("data_dir", "~/.ekko"))

    whisper_cfg = cfg.get("whisper", {})
    transcriber = WhisperTranscriber(
        model_size=whisper_cfg.get("model_size", "small"),
        compute_type=whisper_cfg.get("compute_type", "int8"))

    diar_cfg = cfg.get("diarize", {})
    method = diar_cfg.get("method")
    if method is None:                       # back-compat with the old `enabled` bool
        method = "pyannote" if diar_cfg.get("enabled", False) else "local"
    diarizer = Diarizer(method=method,
                        hf_token=diar_cfg.get("hf_token") or os.environ.get("HF_TOKEN"),
                        max_speakers=diar_cfg.get("max_speakers", 8),
                        threshold=diar_cfg.get("threshold", 0.30))

    # v1: manual naming (in-person). Screen-based naming plugs in here later.
    identifier = ManualIdentifier(name_map=cfg.get("speakers", {}).get("name_map", {}))

    # provider = "gemini" (cloud, default) or "local" (Ollama, fully offline).
    summarizer = build_summarizer(cfg.get("summarize", {}))

    store = SqliteStore(db_path=data_dir / "ekko.db")

    integrations = []
    md = cfg.get("markdown", {})
    if md.get("enabled", True):
        integrations.append(MarkdownIntegration(
            vault_dir=_expand(md.get("vault_dir", "~/Notes/meetings"))))

    return Pipeline(transcriber, diarizer, identifier, summarizer, store, integrations)

"""Summarizer package — provider selection lives here.

`build_summarizer` is the one place that maps `summarize.provider` in config to a
concrete implementation. Adding Claude or another backend is a new branch here
plus a module next to gemini.py / local.py — the pipeline never changes.
"""
from __future__ import annotations

from .base import Summarizer


def build_summarizer(cfg: dict) -> Summarizer:
    """Construct the configured summarizer from the [summarize] config table.

    provider: "gemini" (cloud, default) sends transcript text off-device;
              "local" (Ollama) keeps everything on the machine.
    """
    provider = str(cfg.get("provider", "gemini")).lower()

    if provider == "local":
        from .local import DEFAULT_MODEL, LocalSummarizer
        return LocalSummarizer(model=cfg.get("model") or DEFAULT_MODEL,
                               host=cfg.get("host"))

    if provider == "gemini":
        from .gemini import GeminiSummarizer
        return GeminiSummarizer(model=cfg.get("model") or "gemini-3.6-flash",
                                api_key=cfg.get("api_key"))

    raise SystemExit(
        f"Unknown summarize.provider {provider!r}. Use 'gemini' or 'local'.")

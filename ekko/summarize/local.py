"""Local summarizer via Ollama — the fully-offline option.

Talks to a local Ollama daemon over HTTP (default http://localhost:11434), so
nothing leaves the device: with this provider selected, audio, transcript, AND
the derived summary all stay on your machine. No cloud key, no off-device hop.

Deliberately dependency-free — we speak Ollama's HTTP API with stdlib `urllib`
rather than pulling in a client library, keeping ekko's install light. The model
is a config value (`ollama pull <model>` first); `qwen2.5:7b` is a good 8B-class
default. We pass SUMMARY_JSON_SCHEMA as Ollama's `format` so the daemon
constrains decoding to valid JSON — same reliability as Gemini's JSON mode.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from ..models import Summary, Transcript
from .base import (SUMMARY_INSTRUCTIONS, SUMMARY_JSON_SCHEMA, Summarizer,
                   summary_from_json)

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5:7b"


class LocalSummarizer(Summarizer):
    def __init__(self, model: str = DEFAULT_MODEL, host: str | None = None,
                 timeout: float = 300.0):
        self.model = model
        # OLLAMA_HOST mirrors Ollama's own env var so a non-default daemon just works.
        self.host = (host or os.environ.get("OLLAMA_HOST") or DEFAULT_HOST).rstrip("/")
        self.timeout = timeout

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            f"{self.host}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Can't reach Ollama at {self.host} ({e}). Is it running? "
                f"Install from https://ollama.com, then `ollama pull {self.model}`."
            ) from e

    def preflight(self) -> str | None:
        """Cheap readiness check: is the daemon up and the model pulled?

        Returns None when ready, else a human-readable problem string. Callers
        (the TUI) use this to warn BEFORE a recording, so a down daemon or an
        un-pulled model doesn't only surface at summarize time — after you've
        already recorded the whole meeting.
        """
        try:
            req = urllib.request.Request(f"{self.host}/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                names = {m.get("name", "") for m in json.loads(resp.read()).get("models", [])}
        except urllib.error.URLError as e:
            return (f"Summary will fail: Ollama not reachable at {self.host} ({e.reason}). "
                    f"Start it, then `ollama pull {self.model}`.")
        except Exception:
            return None  # unexpected shape — don't cry wolf, let summarize() report real errors
        # /api/tags lists tagged names ("qwen2.5:7b"); a bare model id means ":latest".
        want = self.model if ":" in self.model else f"{self.model}:latest"
        if want not in names and self.model not in names:
            return (f"Summary will fail: Ollama is up but model {self.model!r} isn't "
                    f"pulled. Run `ollama pull {self.model}`.")
        return None

    def summarize(self, transcript: Transcript) -> Summary:
        # /api/chat with stream=False returns the whole message at once; `format`
        # set to a JSON schema makes Ollama emit schema-valid JSON in the content.
        data = self._post("/api/chat", {
            "model": self.model,
            "stream": False,
            "format": SUMMARY_JSON_SCHEMA,
            "options": {"temperature": 0},   # notes should be deterministic, not creative
            "messages": [
                {"role": "system", "content": SUMMARY_INSTRUCTIONS},
                {"role": "user",
                 "content": f"TRANSCRIPT:\n{transcript.as_dialogue()}"},
            ],
        })
        content = (data.get("message") or {}).get("content", "")
        if not content:
            raise RuntimeError(f"Ollama returned no content (model {self.model!r}). "
                               f"Try `ollama pull {self.model}` first.")
        return summary_from_json(json.loads(content))

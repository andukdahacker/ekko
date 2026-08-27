"""Gemini Flash summarizer (v1 default).

Cheap, fast, large context — good for summarizing every meeting. Uses JSON
mode so we get structured output without brittle parsing. Model id is
configurable because Google rev's the Flash line; set the current one in config.
"""
from __future__ import annotations

import json
import os

from ..models import Summary, Transcript
from .base import SUMMARY_INSTRUCTIONS, Summarizer, summary_from_json


class GeminiSummarizer(Summarizer):
    def __init__(self, model: str = "gemini-3.6-flash", api_key: str | None = None):
        self.model = model
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY not set (use a billing-enabled key "
                               "so your data isn't used for training).")
        self._client = None

    def _load(self):
        if self._client is None:
            from google import genai
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def summarize(self, transcript: Transcript) -> Summary:
        client = self._load()
        prompt = f"{SUMMARY_INSTRUCTIONS}\n\nTRANSCRIPT:\n{transcript.as_dialogue()}"
        resp = client.models.generate_content(
            model=self.model,
            contents=prompt,
            config={"response_mime_type": "application/json"},
        )
        return summary_from_json(json.loads(resp.text))

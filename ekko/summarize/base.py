"""Summarizer interface — provider-agnostic on purpose.

Gemini Flash is the v1 default, but Claude and a local 8B model implement this
same interface, so switching providers is a config change. NOTE: cloud
summarizers send transcript TEXT off-device (audio + transcript stay local).
Use a billing-enabled key so the provider doesn't train on your data.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Summary, Transcript

# Shared prompt so every provider is asked for the same structured output.
SUMMARY_INSTRUCTIONS = """You are a meeting-notes assistant. Given a diarized \
transcript where each line is "Speaker: text", produce concise notes.

Return JSON with exactly these keys:
  "tldr": one or two sentence summary,
  "key_points": array of short strings,
  "decisions": array of short strings (explicit decisions made),
  "action_items": array of objects {"text": ..., "owner": speaker name or null}.

Attribute each action item to the speaker who owns it when the transcript makes
it clear; use null when it's genuinely ambiguous. Do not invent action items."""


class Summarizer(ABC):
    @abstractmethod
    def summarize(self, transcript: Transcript) -> Summary:
        ...

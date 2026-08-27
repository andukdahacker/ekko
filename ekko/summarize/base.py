"""Summarizer interface — provider-agnostic on purpose.

Gemini Flash is the v1 default, but Claude and a local 8B model implement this
same interface, so switching providers is a config change. NOTE: cloud
summarizers send transcript TEXT off-device (audio + transcript stay local).
Use a billing-enabled key so the provider doesn't train on your data.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import ActionItem, Summary, Transcript

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

# JSON schema for the same output. Providers that support schema-constrained
# decoding (Gemini JSON mode, Ollama `format`) pass this so parsing never has to
# recover from malformed JSON. Kept in lockstep with SUMMARY_INSTRUCTIONS above.
SUMMARY_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "tldr": {"type": "string"},
        "key_points": {"type": "array", "items": {"type": "string"}},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "action_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "owner": {"type": ["string", "null"]},
                },
                "required": ["text"],
            },
        },
    },
    "required": ["tldr", "key_points", "decisions", "action_items"],
}


def summary_from_json(data: dict) -> Summary:
    """Build a Summary from the provider's decoded JSON.

    Shared so every Summarizer maps the same keys the same way — a new provider
    only has to return this JSON shape, never reimplement the mapping."""
    return Summary(
        tldr=data.get("tldr", ""),
        key_points=data.get("key_points", []),
        decisions=data.get("decisions", []),
        action_items=[
            ActionItem(text=a["text"], owner=a.get("owner"))
            for a in data.get("action_items", [])
        ],
    )


class Summarizer(ABC):
    @abstractmethod
    def summarize(self, transcript: Transcript) -> Summary:
        ...

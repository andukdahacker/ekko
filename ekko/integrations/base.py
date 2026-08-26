"""Integration adapter interface.

Integrations are consumers of the finished Meeting record — decoupled from
capture. v1 ships the Markdown/Obsidian adapter; Notion, Slack, Asana etc.
implement this same one-method interface later.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Meeting


class Integration(ABC):
    @abstractmethod
    def publish(self, meeting: Meeting) -> None:
        ...

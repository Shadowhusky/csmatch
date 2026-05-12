from __future__ import annotations

from abc import ABC, abstractmethod

from csmatch.models import Match, MatchDetail


class SourceError(Exception):
    """Raised when a source fails to fetch or parse data."""


class MatchSource(ABC):
    """Abstract data source for live CS2 pro matches."""

    name: str = "source"

    @abstractmethod
    async def list_live(self) -> list[Match]:
        """Return matches currently in progress."""

    @abstractmethod
    async def get_detail(self, match_id: str) -> MatchDetail:
        """Return a full scoreboard for a single match."""

    async def close(self) -> None:
        """Release any underlying connections. Override if needed."""
        return None

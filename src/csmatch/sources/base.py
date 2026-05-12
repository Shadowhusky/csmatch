from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

from csmatch.models import Match, MatchDetail


# Shared lock around curl_cffi calls. curl_cffi uses BoringSSL via a
# C-level backend that isn't safe for fully concurrent threaded use —
# two `requests.get(..., impersonate=...)` calls firing on different
# `asyncio.to_thread` workers at the same time can race and surface as
# `SSLError: invalid library (0)`. Holding this lock around every
# curl_cffi call effectively serialises them; the event loop is still
# free to run other coroutines while a request is in flight.
curl_lock = asyncio.Lock()


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

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Status = Literal["live", "upcoming", "finished"]
Side = Literal["CT", "T"]
Half = Literal["1st", "2nd", "OT"]


class Team(BaseModel):
    name: str
    logo_url: str | None = None
    country: str | None = None


class Score(BaseModel):
    team_a: int = 0
    team_b: int = 0
    half: Half | None = None
    side_a: Side | None = None

    def display(self) -> str:
        return f"{self.team_a:>2}-{self.team_b:<2}"


class Player(BaseModel):
    nick: str
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    adr: float | None = None
    rating: float | None = None

    @property
    def kd(self) -> str:
        return f"{self.kills}-{self.deaths}"


class Match(BaseModel):
    """Live-list summary of a match.

    Two scores live here on purpose:

    - ``series_score``: map wins per team (e.g. 1-1 in a BO3). Always
      populated for live and finished matches; ``None`` for upcoming.
    - ``score``: the *current map's* running score (e.g. 13-7 with side
      CT/T). Set only while a map is actively in progress; ``None``
      between maps or before kickoff.

    Display rule: lead with series_score; if score is set, append it.
    """

    id: str
    team_a: Team
    team_b: Team
    series_score: Score | None = None
    score: Score | None = None
    map: str | None = None
    map_index: int | None = None
    best_of: int | None = None
    event: str | None = None
    started_at: datetime | None = None
    status: Status = "live"

    @property
    def label(self) -> str:
        return f"{self.team_a.name} vs {self.team_b.name}"


class MatchDetail(Match):
    """Full scoreboard for an expanded view."""

    players_a: list[Player] = Field(default_factory=list)
    players_b: list[Player] = Field(default_factory=list)
    map_scores: list[Score] = Field(default_factory=list)
    round_history: list[str] = Field(default_factory=list)
    fetched_at: datetime | None = None

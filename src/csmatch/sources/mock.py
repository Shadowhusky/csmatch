from __future__ import annotations

import random
from datetime import datetime, timedelta

from csmatch.models import Match, MatchDetail, Player, Score, Team
from csmatch.sources.base import MatchSource, SourceError


_TEAMS = [
    ("NAVI", "ua"),
    ("FaZe", "eu"),
    ("Vitality", "fr"),
    ("MOUZ", "de"),
    ("G2", "eu"),
    ("Liquid", "us"),
    ("Spirit", "ru"),
    ("Heroic", "dk"),
]
_MAPS = ["de_mirage", "de_inferno", "de_anubis", "de_nuke", "de_ancient", "de_dust2"]
_EVENT = "BLAST Open Spring 2026"


def _player(nick: str, hi: bool = False) -> Player:
    k = random.randint(15, 28) if hi else random.randint(10, 22)
    d = random.randint(10, 22)
    return Player(
        nick=nick,
        kills=k,
        deaths=d,
        assists=random.randint(2, 9),
        adr=round(random.uniform(60, 110), 1),
        rating=round(random.uniform(0.8, 1.5), 2),
    )


def _make_match(seed: int) -> MatchDetail:
    rng = random.Random(seed)
    a_idx, b_idx = rng.sample(range(len(_TEAMS)), 2)
    a_name, a_cc = _TEAMS[a_idx]
    b_name, b_cc = _TEAMS[b_idx]
    map_index = rng.randint(1, 3)
    a_score = rng.randint(0, 16)
    b_score = rng.randint(0, 16)
    side_a = rng.choice(["CT", "T"])
    half = "1st" if a_score + b_score < 12 else ("2nd" if a_score + b_score < 24 else "OT")

    detail = MatchDetail(
        id=f"mock-{seed}",
        team_a=Team(name=a_name, country=a_cc),
        team_b=Team(name=b_name, country=b_cc),
        score=Score(team_a=a_score, team_b=b_score, half=half, side_a=side_a),
        series_score=Score(team_a=rng.randint(0, 1), team_b=rng.randint(0, 1)),
        map=rng.choice(_MAPS),
        map_index=map_index,
        best_of=3,
        event=_EVENT,
        started_at=datetime.now() - timedelta(minutes=rng.randint(10, 90)),
        status="live",
        players_a=[_player(f"{a_name[:2].lower()}{i}") for i in range(5)],
        players_b=[_player(f"{b_name[:2].lower()}{i}") for i in range(5)],
        map_scores=[
            Score(team_a=rng.randint(7, 16), team_b=rng.randint(5, 16)) for _ in range(map_index)
        ],
        round_history=[rng.choice(["ct", "t"]) for _ in range(a_score + b_score)],
        fetched_at=datetime.now(),
    )
    return detail


class MockSource(MatchSource):
    """Deterministic fixture source for offline development."""

    name = "mock"

    def __init__(self, count: int = 3, fail: bool = False) -> None:
        self._count = count
        self._fail = fail
        self._details: dict[str, MatchDetail] = {}
        for i in range(count):
            d = _make_match(i + 1)
            self._details[d.id] = d

    async def list_live(self) -> list[Match]:
        if self._fail:
            raise SourceError("mock failure")
        # Re-roll scores a bit so the UI shows motion when polled.
        for d in self._details.values():
            d.score.team_a = min(d.score.team_a + random.randint(0, 1), 16)
            d.score.team_b = min(d.score.team_b + random.randint(0, 1), 16)
            d.fetched_at = datetime.now()
        out = []
        for d in self._details.values():
            data = d.model_dump(exclude={"players_a", "players_b", "map_scores", "round_history", "fetched_at"})
            out.append(Match(**data))
        return out

    async def get_detail(self, match_id: str) -> MatchDetail:
        if self._fail:
            raise SourceError("mock failure")
        if match_id not in self._details:
            raise SourceError(f"unknown match {match_id}")
        d = self._details[match_id]
        # Bump some players' kill counts so the kill-delta feed sees motion.
        for roster in (d.players_a, d.players_b):
            for p in roster:
                if random.random() < 0.35:
                    p.kills += random.randint(1, 2)
        d.fetched_at = datetime.now()
        return d

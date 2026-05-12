"""Live CS2 match source backed by bo3.gg's public JSON API.

Reverse-engineered from the bo3.gg frontend. No auth needed; we use
curl_cffi with Safari TLS impersonation as a safety belt against
ordinary bot-block heuristics.

Endpoints used:
- GET /api/v1/matches?filter[matches.status][eq]=current
- GET /api/v1/matches/<slug>
- GET /api/v1/games?filter[games.match_id][eq]=<id>&sort=number
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from curl_cffi import requests

from csmatch.models import Match, MatchDetail, Score, Team
from csmatch.sources.base import MatchSource, SourceError, curl_lock


API = "https://api.bo3.gg/api/v1"
HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://bo3.gg/",
}

_BO3_SIDE = {"CT": "CT", "TERRORIST": "T"}


def _team_names_from_slug(slug: str) -> tuple[str, str] | None:
    # slug shape: "<a-slug>-vs-<b-slug>-DD-MM-YYYY"
    m = re.match(r"^(?P<a>.+?)-vs-(?P<b>.+?)-\d{2}-\d{2}-\d{4}$", slug)
    if not m:
        return None
    titlecase = lambda s: " ".join(p.capitalize() for p in s.split("-"))
    return titlecase(m.group("a")), titlecase(m.group("b"))


def _team_name(raw: dict[str, Any], side: int, slug: str) -> str:
    """Resolve a team name from match payload. Tries (in order):
    bet_updates.team_N.name → slug parse → #<team_id> fallback.
    """
    bu = raw.get("bet_updates") or {}
    key = "team_1" if side == 1 else "team_2"
    name = (bu.get(key) or {}).get("name")
    if name:
        return name
    parsed = _team_names_from_slug(slug)
    if parsed:
        return parsed[side - 1]
    tid = raw.get(f"team{side}_id")
    return f"#{tid}" if tid else "?"


def _to_match(raw: dict[str, Any]) -> Match:
    slug = raw.get("slug", "")
    a_name = _team_name(raw, 1, slug)
    b_name = _team_name(raw, 2, slug)

    raw_status = (raw.get("status") or "").lower()
    series_score: Score | None
    if raw_status == "upcoming":
        series_score = None
    else:
        series_score = Score(
            team_a=raw.get("team1_score") or 0,
            team_b=raw.get("team2_score") or 0,
        )

    score: Score | None = None
    current_map: str | None = None
    map_index: int | None = None

    lu = raw.get("live_updates") or {}
    if lu:
        current_map = lu.get("map_name")
        map_index = lu.get("game_number")
        a_round = (lu.get("team_1") or {}).get("game_score")
        b_round = (lu.get("team_2") or {}).get("game_score")
        a_side_raw = (lu.get("team_1") or {}).get("side")
        if a_round is not None and b_round is not None:
            score = Score(
                team_a=a_round,
                team_b=b_round,
                side_a=_BO3_SIDE.get(a_side_raw),
            )

    start = raw.get("start_date")
    started_at = None
    if start:
        try:
            started_at = datetime.fromisoformat(start.replace("Z", "+00:00"))
        except ValueError:
            started_at = None

    parsed = raw.get("parsed_status")
    if raw_status == "upcoming":
        status = "upcoming"
    elif parsed in {"done", "finished"} or raw_status == "finished":
        status = "finished"
    else:
        status = "live"

    return Match(
        id=slug or str(raw.get("id")),
        team_a=Team(name=a_name),
        team_b=Team(name=b_name),
        series_score=series_score,
        score=score,
        map=current_map,
        map_index=map_index,
        best_of=raw.get("bo_type"),
        event=None,  # tournament name requires a join we don't do for the list view
        started_at=started_at,
        status=status,
    )


class BO3Source(MatchSource):
    """bo3.gg-backed source for live CS2 pro matches."""

    name = "bo3"

    def __init__(self, page_limit: int = 25, upcoming_hours: int = 24) -> None:
        self._page_limit = page_limit
        self._upcoming_hours = upcoming_hours

    # curl_cffi blocking calls are run in a thread so the TUI's event
    # loop stays responsive, and held under the shared curl_lock so
    # they don't race with HLTV-source calls and trip a BoringSSL state
    # error.
    async def _fetch_json(self, url: str) -> Any:
        def _get() -> Any:
            r = requests.get(url, headers=HEADERS, impersonate="safari17_0", timeout=15)
            if r.status_code >= 400:
                raise SourceError(f"bo3 {r.status_code} for {url}")
            try:
                return r.json()
            except Exception as e:
                raise SourceError(f"bo3 invalid JSON for {url}: {e}") from e

        async with curl_lock:
            return await asyncio.to_thread(_get)

    async def list_live(self) -> list[Match]:
        live_url = (
            f"{API}/matches?filter[matches.status][eq]=current"
            f"&sort=start_date&page[limit]={self._page_limit}"
        )
        upcoming_url = (
            f"{API}/matches?filter[matches.status][eq]=upcoming"
            f"&sort=start_date&page[limit]={self._page_limit}"
        )
        live_data, upcoming_data = await asyncio.gather(
            self._fetch_json(live_url),
            self._fetch_json(upcoming_url),
        )
        live_raw = live_data.get("results") if isinstance(live_data, dict) else None
        upcoming_raw = upcoming_data.get("results") if isinstance(upcoming_data, dict) else None
        if not isinstance(live_raw, list) or not isinstance(upcoming_raw, list):
            raise SourceError("unexpected bo3 list shape")

        # Drop upcoming matches starting more than N hours from now.
        now = datetime.now(tz=timezone.utc)
        horizon = now + timedelta(hours=self._upcoming_hours)
        def _within_horizon(raw: dict[str, Any]) -> bool:
            s = raw.get("start_date")
            if not s:
                return True
            try:
                t = datetime.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                return True
            return t <= horizon
        upcoming_raw = [m for m in upcoming_raw if _within_horizon(m)]

        tier_rank = {"s": 0, "a": 1, "b": 2, "c": 3}
        def _live_key(raw: dict[str, Any]) -> tuple[int, int]:
            return (
                tier_rank.get((raw.get("tier") or "").lower(), 9),
                0 if raw.get("live_updates") else 1,
            )
        def _upcoming_key(raw: dict[str, Any]) -> tuple[str, int]:
            return (raw.get("start_date") or "", tier_rank.get((raw.get("tier") or "").lower(), 9))

        live_raw.sort(key=_live_key)
        upcoming_raw.sort(key=_upcoming_key)

        # Live first, then upcoming. Dedup defensively — bo3 sometimes
        # returns the same match-id in both buckets during transitions.
        seen_ids: set[str] = set()
        out: list[Match] = []
        for raw in (*live_raw, *upcoming_raw):
            m = _to_match(raw)
            if m.id in seen_ids:
                continue
            seen_ids.add(m.id)
            out.append(m)
        return out

    async def get_detail(self, match_id: str) -> MatchDetail:
        # match_id is the slug ('foo-vs-bar-12-05-2026') or numeric id
        match_url = f"{API}/matches/{match_id}"
        raw = await self._fetch_json(match_url)
        base = _to_match(raw)

        games_url = (
            f"{API}/games?filter[games.match_id][eq]={raw['id']}&sort=number"
        )
        gdata = await self._fetch_json(games_url)
        games = gdata.get("results", []) if isinstance(gdata, dict) else []

        # Map scores per game in order. winner_clan_name tells us which side
        # the score belongs to, so we normalize back to team_a/team_b.
        a_name = base.team_a.name
        map_scores: list[Score] = []
        for g in games:
            ws = g.get("winner_clan_score")
            ls = g.get("loser_clan_score")
            if ws is None or ls is None:
                # Not yet started; skip.
                continue
            winner_name = (g.get("winner_clan_name") or "").lower()
            if winner_name and a_name and winner_name == a_name.lower():
                map_scores.append(Score(team_a=ws, team_b=ls))
            else:
                map_scores.append(Score(team_a=ls, team_b=ws))

        return MatchDetail(
            **base.model_dump(),
            map_scores=map_scores,
            round_history=[],  # not exposed by bo3 list endpoints
            fetched_at=datetime.now(),
        )

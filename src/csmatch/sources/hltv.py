"""HLTV.org-backed source.

Scrapes the public HTML at /matches and /matches/<id>/<slug>. HLTV's
match list HTML doesn't carry live scores in the static markup (those
come from the scorebot socket), so list_live() returns metadata only;
get_detail() pulls the full per-map and per-player breakdown from the
match page, which IS server-rendered.

curl_cffi + Safari TLS impersonation passes plain Cloudflare for these
endpoints today. If HLTV tightens this we'll need a headless browser
fallback.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone

from curl_cffi import requests
from selectolax.parser import HTMLParser, Node

from csmatch.models import Match, MatchDetail, Player, Score, Team
from csmatch.sources.base import MatchSource, SourceError


BASE = "https://www.hltv.org"
HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.hltv.org/",
}

# Player rows have the nickname duplicated: "JingYu 'VanceKK' WanVanceKK" → "VanceKK".
_NICK_RE = re.compile(r"'([^']+)'")
_HALF_RE = re.compile(r"\((\d+)\s*:\s*(\d+)\s*;\s*(\d+)\s*:\s*(\d+)\)")


_TEAM_NAME_ALIASES = {
    "natus vincere": "navi",
    "navi": "natus vincere",
}


def _norm_name(name: str | None) -> str:
    return (name or "").strip().lower()


def _enrich(m: Match, bo3_lookup: dict[tuple[str, str], Match]) -> Match:
    """If bo3 has the same matchup, copy over map / map_index / score /
    series_score from bo3 — HLTV's static HTML doesn't carry them."""
    if not bo3_lookup:
        return m
    a, b = _norm_name(m.team_a.name), _norm_name(m.team_b.name)
    bo3_m = bo3_lookup.get((a, b))
    if bo3_m is None:
        # Try common alias swap on either team
        a2 = _TEAM_NAME_ALIASES.get(a, a)
        b2 = _TEAM_NAME_ALIASES.get(b, b)
        bo3_m = bo3_lookup.get((a2, b2)) or bo3_lookup.get((b2, a2))
    if bo3_m is None:
        return m
    return m.model_copy(update={
        "map": bo3_m.map or m.map,
        "map_index": bo3_m.map_index or m.map_index,
        "score": bo3_m.score or m.score,
        "series_score": bo3_m.series_score or m.series_score,
    })


def _parse_match_wrapper(w: Node) -> Match | None:
    mid = w.attributes.get("data-match-id")
    if not mid:
        return None
    classes = w.attributes.get("class") or ""
    is_live = "live-match-container" in classes or w.attributes.get("live") == "true"

    team_names: list[str] = []
    for el in w.css(".match-teamname"):
        name = el.text(strip=True)
        if name:
            team_names.append(name)
        if len(team_names) == 2:
            break
    if len(team_names) < 2:
        return None

    event_el = w.css_first(".match-event .text-ellipsis")
    event = event_el.text(strip=True) if event_el else None

    bo: int | None = None
    for meta in w.css(".match-meta"):
        t = meta.text(strip=True).lower()
        if t.startswith("bo"):
            try:
                bo = int(t[2:])
            except ValueError:
                pass

    started_at: datetime | None = None
    time_el = w.css_first("[data-unix]")
    if time_el:
        try:
            ts_ms = int(time_el.attributes.get("data-unix") or "0")
            if ts_ms:
                started_at = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        except (TypeError, ValueError):
            pass

    # HLTV's /matches page doesn't expose series score in static HTML —
    # those numbers come from the scorebot socket. Leave it None so the UI
    # renders "LIVE" / "vs" instead of a misleading "0-0".
    return Match(
        id=str(mid),
        team_a=Team(name=team_names[0]),
        team_b=Team(name=team_names[1]),
        series_score=None,
        score=None,
        map=None,
        map_index=None,
        best_of=bo,
        event=event,
        started_at=started_at,
        status="live" if is_live else "upcoming",
    )


def _parse_player_row(cells: list[str]) -> Player | None:
    """Each player row has 9 cells: name, K-D, eK-eD, Swing, ADR, eADR, KAST, eKAST, Rating."""
    if len(cells) < 2:
        return None
    name_cell = cells[0]
    m = _NICK_RE.search(name_cell)
    nick = m.group(1) if m else name_cell.split()[0]
    kd_cell = cells[1] if len(cells) > 1 else "0-0"
    try:
        k_str, d_str = kd_cell.split("-", 1)
        kills = int(k_str.strip())
        deaths = int(d_str.strip())
    except (ValueError, AttributeError):
        kills, deaths = 0, 0
    adr: float | None = None
    if len(cells) > 4:
        try:
            adr = float(cells[4])
        except (ValueError, TypeError):
            pass
    rating: float | None = None
    if len(cells) > 8:
        try:
            rating = float(cells[8])
        except (ValueError, TypeError):
            pass
    return Player(nick=nick, kills=kills, deaths=deaths, adr=adr, rating=rating)


def _parse_mapholder(node: Node, team_a_name: str) -> tuple[str | None, Score | None, str]:
    """Returns (map_name, Score_with_halves, status) for one .mapholder block.
    `status` is 'finished', 'live', or 'pending' inferred from whether the map
    has any score yet.
    """
    text = node.text(separator="|", strip=True)
    parts = [p for p in text.split("|") if p]
    # Format observed: ['Ancient', team_a, '13', 'STATS', '(', '11', ':', '1', ';', '2', ':', '6', ')', team_b, '7']
    if not parts:
        return None, None, "pending"
    map_name = parts[0]

    # Match an embedded "(X:Y;A:B)" pattern.
    flat = "".join(parts)
    half = _HALF_RE.search(flat)

    # Pull total scores by scanning text for two integer tokens flanking team names.
    nums = [int(p) for p in parts if p.isdigit()]
    a_total = b_total = None
    if half:
        a1, b1, a2, b2 = (int(g) for g in half.groups())
        a_total = a1 + a2
        b_total = b1 + b2
    elif len(nums) >= 2:
        a_total, b_total = nums[0], nums[1]

    status: str
    if a_total is None or b_total is None:
        status = "pending"
        score = None
    elif a_total >= 13 or b_total >= 13:
        status = "finished"
        score = Score(team_a=a_total, team_b=b_total)
    else:
        status = "live"
        score = Score(team_a=a_total, team_b=b_total)
    return map_name, score, status


class HLTVSource(MatchSource):
    name = "hltv"

    def __init__(self, upcoming_hours: int = 24, max_matches: int = 40) -> None:
        self._upcoming_hours = upcoming_hours
        self._max_matches = max_matches

    async def _get(self, path: str) -> str:
        url = path if path.startswith("http") else f"{BASE}{path}"

        def _do() -> str:
            r = requests.get(url, headers=HEADERS, impersonate="safari17_0", timeout=20)
            if r.status_code >= 400:
                raise SourceError(f"hltv {r.status_code} for {url}")
            return r.text

        return await asyncio.to_thread(_do)

    async def list_live(self) -> list[Match]:
        # HLTV's /matches HTML leaves map name + scores as empty
        # placeholders that its scorebot JS fills at runtime. We can't
        # see them from a static fetch. To still surface map/score on
        # the list we concurrently pull bo3.gg (which exposes the same
        # data in JSON) and merge on team-name match.
        html_task = asyncio.create_task(self._get("/matches"))
        bo3_task = asyncio.create_task(self._fetch_bo3_enrichment())
        html = await html_task
        bo3_lookup = await bo3_task

        tree = HTMLParser(html)
        now = datetime.now(tz=timezone.utc)
        horizon = now + timedelta(hours=self._upcoming_hours)

        live: list[Match] = []
        upcoming: list[Match] = []
        seen: set[str] = set()
        for w in tree.css(".match-wrapper"):
            m = _parse_match_wrapper(w)
            if not m or m.id in seen:
                continue
            seen.add(m.id)
            if m.status == "live":
                live.append(_enrich(m, bo3_lookup))
            elif m.status == "upcoming" and m.started_at and m.started_at <= horizon:
                upcoming.append(m)

        live.sort(key=lambda m: m.started_at or now)
        upcoming.sort(key=lambda m: m.started_at or horizon)
        out = live + upcoming
        return out[: self._max_matches]

    async def _fetch_bo3_enrichment(self) -> dict[tuple[str, str], Match]:
        """Best-effort sidecar fetch of bo3.gg's live list, keyed by
        (lower-cased team_a, lower-cased team_b) — both orientations so
        a HLTV match can match either way around. Empty dict on any
        failure (we never want a bo3 hiccup to break the HLTV view)."""
        try:
            # Local import to keep the module-level dep graph clean and
            # to allow HLTVSource to be used in isolation.
            from csmatch.sources.bo3gg import BO3Source
            bo3 = BO3Source(upcoming_hours=0)  # live-only
            matches = await bo3.list_live()
        except Exception:
            return {}
        lookup: dict[tuple[str, str], Match] = {}
        for m in matches:
            if m.status != "live":
                continue
            a, b = _norm_name(m.team_a.name), _norm_name(m.team_b.name)
            if not a or not b:
                continue
            lookup[(a, b)] = m
            lookup[(b, a)] = m
        return lookup

    async def get_detail(self, match_id: str) -> MatchDetail:
        # HLTV needs a slug suffix; passing any slug works and the server
        # responds with the canonical page.
        html = await self._get(f"/matches/{match_id}/x")
        tree = HTMLParser(html)

        # Team names from teamsBox
        team_name_els = tree.css(".teamsBox .teamName")
        if len(team_name_els) < 2:
            raise SourceError(f"hltv: couldn't find team names for {match_id}")
        a_name = team_name_els[0].text(strip=True)
        b_name = team_name_els[1].text(strip=True)

        # LIVE / countdown status
        cd = tree.css_first(".teamsBox .countdown")
        is_live = cd is not None and (cd.text(strip=True).upper() == "LIVE" or cd.attributes.get("data-time-countdown") == "LIVE")

        # Event name
        event_el = tree.css_first(".timeAndEvent .event a, .teamsBox .event a")
        event = event_el.text(strip=True) if event_el else None

        # Best-of from veto-box or matchInfoBox
        bo: int | None = None
        for sel in [".veto-box", ".match-info"]:
            box = tree.css_first(sel)
            if not box:
                continue
            t = box.text(strip=True).lower()
            m = re.search(r"best of (\d+)", t)
            if m:
                bo = int(m.group(1))
                break

        # Map-by-map scores
        map_scores: list[Score] = []
        maps_played = 0
        current_map: str | None = None
        current_map_idx: int | None = None
        for i, mh in enumerate(tree.css(".mapholder"), start=1):
            name, score, status = _parse_mapholder(mh, a_name)
            if status == "finished" and score:
                map_scores.append(score)
                maps_played += 1
            elif status == "live" and score:
                current_map = name
                current_map_idx = i

        # Series score = number of map wins per side
        a_wins = sum(1 for s in map_scores if s.team_a > s.team_b)
        b_wins = sum(1 for s in map_scores if s.team_b > s.team_a)
        series_score = Score(team_a=a_wins, team_b=b_wins)

        # Current map round-score (only when a map is actively in progress).
        live_round_score: Score | None = None
        if current_map is not None:
            for mh in tree.css(".mapholder"):
                name, score, status = _parse_mapholder(mh, a_name)
                if status == "live" and score and name == current_map:
                    live_round_score = score
                    break

        # Per-player stats. HLTV renders 1 totals table + 1 table per map for
        # each team, so for an N-map series we see (N+1) tables for team A
        # then (N+1) for team B. We want the totals tables (the "running"
        # numbers, including the in-progress map).
        players_a: list[Player] = []
        players_b: list[Player] = []
        sc = tree.css_first(".stats-content")
        if sc:
            for table in sc.css("table"):
                rows = table.css("tr")
                if not rows:
                    continue
                header_cells = [c.text(strip=True) for c in rows[0].css("td, th")]
                if not header_cells:
                    continue
                header_team = header_cells[0]
                if header_team == a_name and not players_a:
                    bucket: list[Player] = players_a
                elif header_team == b_name and not players_b:
                    bucket = players_b
                else:
                    continue
                for row in rows[1:6]:
                    cells = [c.text(strip=True) for c in row.css("td, th")]
                    p = _parse_player_row(cells)
                    if p:
                        bucket.append(p)
                if players_a and players_b:
                    break

        return MatchDetail(
            id=str(match_id),
            team_a=Team(name=a_name),
            team_b=Team(name=b_name),
            series_score=series_score,
            score=live_round_score,
            map=current_map,
            map_index=current_map_idx or (maps_played + 1 if is_live else None),
            best_of=bo,
            event=event,
            started_at=None,
            status="live" if is_live else "upcoming",
            players_a=players_a,
            players_b=players_b,
            map_scores=map_scores,
            round_history=[],
            fetched_at=datetime.now(),
        )

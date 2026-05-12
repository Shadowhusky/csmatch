"""Parse-only tests for BO3Source using captured fixture payloads."""

from __future__ import annotations

from csmatch.sources.bo3gg import _team_name, _team_names_from_slug, _to_match


# Captured 2026-05-12 from api.bo3.gg/api/v1/matches?filter[matches.status][eq]=current
LIVE_MATCH_WITH_UPDATES = {
    "id": 119879,
    "slug": "alter-ego-vs-thunder-downunder-12-05-2026",
    "team1_id": 22191,
    "team2_id": 23286,
    "team1_score": 0,
    "team2_score": 1,
    "status": "current",
    "parsed_status": "partially_done",
    "bo_type": 3,
    "start_date": "2026-05-12T12:00:00.000+00:00",
    "tier": "a",
    "bet_updates": {
        "team_1": {"name": "Alter Ego", "team_id": 22191},
        "team_2": {"name": "THUNDER dOWNUNDER", "team_id": 23286},
    },
    "live_updates": {
        "team_1": {"side": "TERRORIST", "game_score": 0, "match_score": 0},
        "team_2": {"side": "CT", "game_score": 7, "match_score": 1},
        "map_name": "de_dust2",
        "game_ended": False,
        "game_number": 2,
        "round_phase": "IN_PROGRESS",
        "round_number": 8,
    },
}

WAITING_MATCH = {
    "id": 119540,
    "slug": "furia-vs-gentle-mates-12-05-2026",
    "team1_id": 100,
    "team2_id": 101,
    "team1_score": 1,
    "team2_score": 1,
    "status": "current",
    "parsed_status": "waiting",
    "bo_type": 3,
    "start_date": "2026-05-12T13:00:00.000+00:00",
    "tier": "s",
    "bet_updates": {
        "team_1": {"name": "FURIA"},
        "team_2": {"name": "Gentle Mates"},
    },
    "live_updates": None,
}


def test_slug_parse():
    a, b = _team_names_from_slug("furia-vs-gentle-mates-12-05-2026")
    assert a == "Furia"
    assert b == "Gentle Mates"


def test_slug_parse_bad():
    assert _team_names_from_slug("not-a-valid-slug") is None


def test_team_name_prefers_bet_updates():
    assert _team_name(WAITING_MATCH, 1, WAITING_MATCH["slug"]) == "FURIA"


def test_team_name_falls_back_to_slug():
    raw = {"team1_id": 1, "team2_id": 2}  # no bet_updates
    name = _team_name(raw, 1, "furia-vs-gentle-mates-12-05-2026")
    assert name == "Furia"


def test_team_name_falls_back_to_id():
    raw = {"team1_id": 999, "team2_id": 1000}
    assert _team_name(raw, 1, "no-slug-pattern") == "#999"


def test_to_match_live_has_round_and_series_scores():
    m = _to_match(LIVE_MATCH_WITH_UPDATES)
    assert m.team_a.name == "Alter Ego"
    assert m.team_b.name == "THUNDER dOWNUNDER"
    # Current map round score
    assert m.score is not None
    assert (m.score.team_a, m.score.team_b) == (0, 7)
    assert m.score.side_a == "T"
    # Series score
    assert m.series_score is not None
    assert (m.series_score.team_a, m.series_score.team_b) == (0, 1)
    assert m.map == "de_dust2"
    assert m.map_index == 2
    assert m.best_of == 3
    assert m.status == "live"


def test_to_match_waiting_only_has_series_score():
    m = _to_match(WAITING_MATCH)
    # Between maps: no live round score
    assert m.score is None
    # Series score reflects map wins
    assert m.series_score is not None
    assert (m.series_score.team_a, m.series_score.team_b) == (1, 1)
    assert m.map is None
    assert m.status == "live"

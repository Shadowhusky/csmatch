"""Logic tests for the scorebot bridge's socket-payload processing.

These drive the pure event-processing methods (`_handle_log` /
`_handle_scoreboard`) directly with synthetic payloads and drain the
queue — no Playwright, no network.
"""

from __future__ import annotations

from csmatch.scorebot import (
    BombPlantEvent,
    KillEvent,
    PlayerScoreboard,
    RoundOverEvent,
    RoundStartEvent,
    ScoreboardState,
    ScorebotBridge,
    _kill_event,
    _players_from_scoreboard,
    _side,
    _wintype_reason,
)


def _kill(eid: int, killer="A", victim="B", weapon="ak47", hs=False, killer_side="CT", victim_side="TERRORIST"):
    return {"Kill": {
        "eventId": eid, "killerNick": killer, "victimNick": victim, "weapon": weapon,
        "headShot": hs, "killerSide": killer_side, "victimSide": victim_side,
    }}


def _log(*entries):
    # Socket delivers the log newest-first as a JSON string.
    return {"log": list(reversed(list(entries)))}


def _sb(round_, t, ct, mp="de_dust2", bomb=False, ct_players=None, t_players=None):
    return {
        "currentRound": round_, "terroristScore": t, "counterTerroristScore": ct,
        "mapName": mp, "bombPlanted": bomb,
        "CT": ct_players or [], "TERRORIST": t_players or [],
        "ctTeamName": "CTteam", "terroristTeamName": "Tteam",
    }


def _drain(bridge):
    out = []
    while True:
        item = bridge.get_nowait()
        if item is None:
            break
        out.append(item)
    return out


def _kills(items):
    return [i for i in items if isinstance(i, KillEvent)]


def _prime(b):
    """Feed a connect backlog (history) and drain it — sets _primed so
    later frames emit. Uses a high event id that won't collide with the
    low ids the tests use for live events."""
    b._handle_log(_log(_kill(900), _kill(901)))
    _drain(b)


# ── pure helpers ────────────────────────────────────────────────────

def test_side_mapping():
    assert _side("TERRORIST") == "T"
    assert _side("CT") == "CT"
    assert _side(None) is None


def test_kill_event_flash_vs_regular_assist():
    flash = _kill_event({"killerNick": "k", "victimNick": "v", "weapon": "awp",
                         "headShot": True, "flasherNick": "f"}, None)
    assert flash.assist == "f" and flash.flash_assist is True and flash.headshot
    reg = _kill_event({"killerNick": "k", "victimNick": "v", "flasherNick": "f"}, "buddy")
    assert reg.assist == "buddy" and reg.flash_assist is False


def test_wintype_reason():
    assert _wintype_reason("Target_Bombed") == "Target bombed"
    assert _wintype_reason("Bomb_Defused") == "Bomb defused"
    assert _wintype_reason("CTs_Win") == "Enemy eliminated"


def test_players_from_scoreboard():
    ps = _players_from_scoreboard(_sb(5, 2, 3, ct_players=[
        {"nick": "x", "score": 7, "assists": 1, "deaths": 4, "hp": 80, "money": 1500,
         "damagePrRound": 91.2, "alive": True},
    ]))
    assert isinstance(ps, PlayerScoreboard)
    ct = ps.teams[0]
    assert ct.name == "CTteam" and ct.side == "CT"
    p = ct.players[0]
    assert p.kills == 7 and p.assists == 1 and p.deaths == 4 and p.hp == 80 and p.adr == 91.2


# ── log processing: prime, emit, dedup ──────────────────────────────

def test_first_backlog_primes_silently():
    b = ScorebotBridge()
    b._handle_scoreboard(_sb(5, 2, 3))      # establish a live round
    _drain(b)
    b._handle_log(_log(_kill(1), _kill(2)))  # connect backlog → history
    assert _kills(_drain(b)) == []
    assert b._primed is True


def test_live_kill_emitted_with_round_and_weapon():
    b = ScorebotBridge()
    b._handle_scoreboard(_sb(5, 2, 3))
    _prime(b)                     # prime
    _drain(b)
    b._handle_log(_log(_kill(10, killer="s1mple", victim="ZywOo", weapon="awp", hs=True)))
    ks = _kills(_drain(b))
    assert len(ks) == 1
    k = ks[0]
    assert k.killer == "s1mple" and k.victim == "ZywOo" and k.weapon == "awp"
    assert k.headshot and k.round == 5


def test_kill_dedup_by_event_id_across_reconnect():
    b = ScorebotBridge()
    b._handle_scoreboard(_sb(5, 2, 3))
    _prime(b)                     # prime
    _drain(b)
    b._handle_log(_log(_kill(10)))            # live kill
    assert len(_kills(_drain(b))) == 1
    # reconnect replay: old kill 10 again + new gap kill 11. Only 11 is new.
    b._handle_log(_log(_kill(10), _kill(11)))
    ks = _kills(_drain(b))
    assert len(ks) == 1 and ks == [k for k in ks if True]
    # the same backlog again → nothing new
    b._handle_log(_log(_kill(10), _kill(11)))
    assert _kills(_drain(b)) == []


def test_assist_merged_into_kill():
    b = ScorebotBridge()
    b._handle_scoreboard(_sb(5, 2, 3))
    _prime(b)
    _drain(b)
    frame = {"log": [
        {"Assist": {"assisterNick": "helper", "killEventId": 20}},
        {"Kill": {"eventId": 20, "killerNick": "k", "victimNick": "v", "weapon": "m4a1"}},
    ]}
    b._handle_log(frame)
    ks = _kills(_drain(b))
    assert len(ks) == 1 and ks[0].assist == "helper"


def test_warmup_kills_filtered_round_zero():
    b = ScorebotBridge()
    b._handle_scoreboard(_sb(0, 0, 0))        # warmup: round 0
    _prime(b)
    _drain(b)
    b._handle_log(_log(_kill(1), _kill(2)))   # deathmatch kills
    assert _kills(_drain(b)) == []
    # match starts → round 1, kills now emit
    b._handle_scoreboard(_sb(1, 0, 0))
    _drain(b)
    b._handle_log(_log(_kill(3)))
    assert len(_kills(_drain(b))) == 1


# ── round/bomb derivation from the scoreboard ───────────────────────

def test_round_over_and_start_from_scoreboard():
    b = ScorebotBridge()
    b._handle_scoreboard(_sb(5, 2, 3))        # baseline (no emit)
    _drain(b)
    # CT wins round 6: score 2-3 → 2-4 (sum 5→6), round counter 5→6
    b._handle_scoreboard(_sb(6, 2, 4))
    items = _drain(b)
    overs = [i for i in items if isinstance(i, RoundOverEvent)]
    starts = [i for i in items if isinstance(i, RoundStartEvent)]
    assert len(overs) == 1 and overs[0].winner_side == "CT"
    assert overs[0].ct_score == 4 and overs[0].t_score == 2 and overs[0].round == 6
    assert len(starts) == 1 and starts[0].round == 6


def test_round_over_reason_prefers_log_wintype():
    b = ScorebotBridge()
    b._handle_scoreboard(_sb(5, 2, 3))
    _prime(b)                      # prime
    _drain(b)
    # a live RoundEnd increment carrying the authoritative winType
    b._handle_log({"log": [{"RoundEnd": {
        "terroristScore": 3, "counterTerroristScore": 3,
        "winner": "TERRORIST", "winType": "Target_Bombed"}}]})
    b._handle_scoreboard(_sb(7, 3, 3))         # T wins → 3-3 (sum 5→6... )
    overs = [i for i in _drain(b) if isinstance(i, RoundOverEvent)]
    assert len(overs) == 1 and overs[0].reason == "Target bombed"


def test_bomb_plant_from_scoreboard_transition():
    b = ScorebotBridge()
    b._handle_scoreboard(_sb(5, 2, 3, bomb=False))
    _prime(b)
    _drain(b)
    # planter buffered from the log, then the scoreboard flips bombPlanted
    b._handle_log({"log": [{"BombPlanted": {"playerNick": "planterGuy", "tPlayers": 4, "ctPlayers": 3}}]})
    b._handle_scoreboard(_sb(5, 2, 3, bomb=True))
    bombs = [i for i in _drain(b) if isinstance(i, BombPlantEvent)]
    assert len(bombs) == 1 and bombs[0].planter == "planterGuy"
    assert bombs[0].t_alive == 4 and bombs[0].ct_alive == 3


def test_round_over_reason_not_polluted_across_maps():
    b = ScorebotBridge()
    b._handle_scoreboard(_sb(5, 2, 3, mp="de_mirage"))
    _prime(b)
    _drain(b)
    # map 1: a round ends 2-1 via "Target_Saved" (buffered)
    b._handle_log({"log": [{"RoundEnd": {
        "terroristScore": 2, "counterTerroristScore": 1, "winType": "Target_Saved"}}]})
    # new map → buffer cleared; a fresh 2-1 round-over must NOT reuse it
    b._handle_scoreboard(_sb(2, 1, 1, mp="de_inferno"))   # map change baseline
    _drain(b)
    b._handle_scoreboard(_sb(3, 2, 1, mp="de_inferno"))   # T wins → 2-1
    overs = [i for i in _drain(b) if isinstance(i, RoundOverEvent)]
    assert len(overs) == 1
    assert overs[0].reason != "Target saved"

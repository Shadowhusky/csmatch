"""Pure logic tests for the live-analysis layer (no Playwright/network).

Events are constructed directly and fed to a `RoundTracker` in arrival
order; annotations are asserted per event.
"""

from __future__ import annotations

from csmatch.analysis import Annotation, RoundSummary, RoundTracker
from csmatch.scorebot import (
    BombDefuseEvent,
    BombPlantEvent,
    KillEvent,
    LivePlayer,
    PlayerScoreboard,
    RoundOverEvent,
    RoundStartEvent,
    TeamScoreboard,
)


def _scoreboard(ct: list[str], t: list[str]) -> PlayerScoreboard:
    return PlayerScoreboard(teams=[
        TeamScoreboard(name="CTteam", side="CT", players=[LivePlayer(nick=n) for n in ct]),
        TeamScoreboard(name="Tteam", side="T", players=[LivePlayer(nick=n) for n in t]),
    ])


def _roster5(tracker: RoundTracker) -> None:
    tracker.feed_players(_scoreboard(
        ct=["c1", "c2", "c3", "c4", "c5"],
        t=["t1", "t2", "t3", "t4", "t5"],
    ))


def _kill(killer: str, victim: str, *, killer_side="CT", victim_side="T", round=1, **kw) -> KillEvent:
    return KillEvent(
        killer=killer, killer_side=killer_side,
        victim=victim, victim_side=victim_side,
        round=round, **kw,
    )


# ── opening kill ───────────────────────────────────────────────────────

def test_first_kill_is_opening_rest_are_not():
    tr = RoundTracker()
    _roster5(tr)
    tr.feed_event(RoundStartEvent(round=1))

    a1 = tr.feed_event(_kill("c1", "t1"))
    a2 = tr.feed_event(_kill("c2", "t2"))

    assert a1.opening is True
    assert a2.opening is False


def test_opening_works_without_rosters():
    tr = RoundTracker()
    tr.feed_event(RoundStartEvent(round=1))
    a1 = tr.feed_event(_kill("c1", "t1"))
    a2 = tr.feed_event(_kill("c1", "t2"))
    assert a1.opening is True
    assert a2.opening is False


# ── multikill / ace ──────────────────────────────────────────────────────

def test_multikill_counts_per_killer_this_round():
    tr = RoundTracker()
    _roster5(tr)
    tr.feed_event(RoundStartEvent(round=1))

    assert tr.feed_event(_kill("c1", "t1")).multikill == 1
    assert tr.feed_event(_kill("c2", "t2")).multikill == 1  # different killer
    assert tr.feed_event(_kill("c1", "t3")).multikill == 2
    assert tr.feed_event(_kill("c1", "t4")).multikill == 3
    assert tr.feed_event(_kill("c1", "t5")).multikill == 4


def test_ace_reaches_five():
    tr = RoundTracker()
    _roster5(tr)
    tr.feed_event(RoundStartEvent(round=1))
    counts = [
        tr.feed_event(_kill("c1", v, victim_side="T")).multikill
        for v in ["t1", "t2", "t3", "t4", "t5"]
    ]
    assert counts == [1, 2, 3, 4, 5]


def test_multikill_resets_each_round():
    tr = RoundTracker()
    _roster5(tr)
    tr.feed_event(RoundStartEvent(round=1))
    tr.feed_event(_kill("c1", "t1", round=1))
    tr.feed_event(_kill("c1", "t2", round=1))

    tr.feed_event(RoundStartEvent(round=2))
    a = tr.feed_event(_kill("c1", "t1", round=2))
    assert a.multikill == 1


# ── round reset semantics ───────────────────────────────────────────────

def test_reset_on_round_change_without_round_start():
    tr = RoundTracker()
    _roster5(tr)
    tr.feed_event(RoundStartEvent(round=1))
    tr.feed_event(_kill("c1", "t1", round=1))

    # No RoundStartEvent — a kill carrying a new round must reset state.
    a = tr.feed_event(_kill("c1", "t2", round=2))
    assert a.opening is True
    assert a.multikill == 1


def test_round_start_resets_opening_and_kills():
    tr = RoundTracker()
    _roster5(tr)
    tr.feed_event(RoundStartEvent(round=1))
    tr.feed_event(_kill("c1", "t1"))

    tr.feed_event(RoundStartEvent(round=2))
    a = tr.feed_event(_kill("c2", "t1", round=2))
    assert a.opening is True


# ── round summary ────────────────────────────────────────────────────────

def test_round_over_emits_summary():
    tr = RoundTracker()
    _roster5(tr)
    tr.feed_event(RoundStartEvent(round=1))
    a = tr.feed_event(RoundOverEvent(
        round=1, map="de_inferno", winner_side="CT",
        t_score=6, ct_score=8, reason="Bomb defused",
    ))
    s = a.summary
    assert isinstance(s, RoundSummary)
    assert s.round == 1
    assert s.map == "de_inferno"
    assert s.winner_side == "CT"
    assert s.t_score == 6
    assert s.ct_score == 8
    assert s.reason == "Bomb defused"


def test_round_over_summary_when_round_only_known_from_over_event():
    # No prior round-tracking state; the RoundOverEvent carries the round
    # and must still produce a summary for it (no reset that loses it).
    tr = RoundTracker()
    a = tr.feed_event(RoundOverEvent(round=3, winner_side="T", reason="x"))
    assert a.summary is not None
    assert a.summary.round == 3


# ── clutch detection ─────────────────────────────────────────────────────

def test_won_clutch_1v5():
    tr = RoundTracker()
    _roster5(tr)
    tr.feed_event(RoundStartEvent(round=1))
    # Reduce CT to just c5 while T still has >= 2 alive → 1vN starts.
    tr.feed_event(_kill("t1", "c1", killer_side="T", victim_side="CT"))
    tr.feed_event(_kill("t1", "c2", killer_side="T", victim_side="CT"))
    tr.feed_event(_kill("t2", "c3", killer_side="T", victim_side="CT"))
    tr.feed_event(_kill("t2", "c4", killer_side="T", victim_side="CT"))
    # Now CT = {c5}, T = {t1..t5} (5 alive) → clutch start 1v5.
    # c5 wins the round by clearing T.
    for v in ["t1", "t2", "t3", "t4", "t5"]:
        tr.feed_event(_kill("c5", v, killer_side="CT", victim_side="T"))
    a = tr.feed_event(RoundOverEvent(round=1, winner_side="CT", reason="All enemies eliminated"))
    assert a.clutch == "1v5"
    assert a.clutcher == "c5"


def test_clutch_uses_opponent_count_at_start_not_later():
    tr = RoundTracker()
    _roster5(tr)
    tr.feed_event(RoundStartEvent(round=1))
    # First whittle CT down to exactly 2 alive while T still has several,
    # then make t5 the lone survivor → 1v2 recorded at that moment.
    tr.feed_event(_kill("t1", "c1", killer_side="T", victim_side="CT"))
    tr.feed_event(_kill("t1", "c2", killer_side="T", victim_side="CT"))
    tr.feed_event(_kill("t1", "c3", killer_side="T", victim_side="CT"))
    # CT = {c4, c5} (2 alive), T = {t1..t5} (5 alive).
    # Now reduce T to just t5; the kill making t5 lone sees CT == 2.
    tr.feed_event(_kill("c4", "t1"))
    tr.feed_event(_kill("c4", "t2"))
    tr.feed_event(_kill("c5", "t3"))
    tr.feed_event(_kill("c5", "t4"))
    # T = {t5}, CT = {c4, c5} (2) → clutch 1v2 for t5.
    # A later trade drops CT to 1, but the recorded count stays 2.
    tr.feed_event(_kill("t5", "c4", killer_side="T", victim_side="CT"))
    tr.feed_event(_kill("t5", "c5", killer_side="T", victim_side="CT"))
    a = tr.feed_event(RoundOverEvent(round=1, winner_side="T", reason="All enemies eliminated"))
    assert a.clutch == "1v2"  # opponent count frozen at clutch start
    assert a.clutcher == "t5"


def test_1v1_is_not_a_clutch():
    tr = RoundTracker()
    _roster5(tr)
    tr.feed_event(RoundStartEvent(round=1))
    # Down both sides to a single player each, never passing through a 1vN.
    # CT loses 4, T loses 4 interleaved so neither side is ever lone-vs->=2.
    seq = [
        _kill("c1", "t1"),
        _kill("t2", "c1", killer_side="T", victim_side="CT"),
        _kill("c2", "t2"),
        _kill("t3", "c2", killer_side="T", victim_side="CT"),
        _kill("c3", "t3"),
        _kill("t4", "c3", killer_side="T", victim_side="CT"),
        _kill("c4", "t4"),
        _kill("t5", "c4", killer_side="T", victim_side="CT"),
    ]
    for e in seq:
        tr.feed_event(e)
    # Now CT = {c5}, T = {t5} → a 1v1, not a clutch.
    a = tr.feed_event(RoundOverEvent(round=1, winner_side="CT", reason="x"))
    assert a.clutch is None
    assert a.clutcher is None


def test_clutcher_dies_before_round_end_no_clutch():
    tr = RoundTracker()
    _roster5(tr)
    tr.feed_event(RoundStartEvent(round=1))
    # CT down to c5 with T 3 alive → 1v3 start.
    tr.feed_event(_kill("t1", "c1", killer_side="T", victim_side="CT"))
    tr.feed_event(_kill("t1", "c2", killer_side="T", victim_side="CT"))
    tr.feed_event(_kill("t1", "c3", killer_side="T", victim_side="CT"))
    tr.feed_event(_kill("t1", "c4", killer_side="T", victim_side="CT"))
    # c5 trades two then dies → CT loses.
    tr.feed_event(_kill("c5", "t2"))
    tr.feed_event(_kill("c5", "t3"))
    tr.feed_event(_kill("t1", "c5", killer_side="T", victim_side="CT"))
    a = tr.feed_event(RoundOverEvent(round=1, winner_side="T", reason="All enemies eliminated"))
    assert a.clutch is None
    assert a.clutcher is None


def test_clutch_recorded_but_side_loses_no_clutch():
    # A 1vN can start but the clutch attempt fails (the lone survivor's
    # side does not win) → no clutch on the summary.
    tr = RoundTracker()
    _roster5(tr)
    tr.feed_event(RoundStartEvent(round=1))
    tr.feed_event(_kill("t1", "c1", killer_side="T", victim_side="CT"))
    tr.feed_event(_kill("t1", "c2", killer_side="T", victim_side="CT"))
    tr.feed_event(_kill("t1", "c3", killer_side="T", victim_side="CT"))
    tr.feed_event(_kill("t1", "c4", killer_side="T", victim_side="CT"))
    # CT = {c5}, T 5 alive → 1v5 start. Bomb goes down, CT loses.
    a = tr.feed_event(RoundOverEvent(round=1, winner_side="T", reason="Target bombed"))
    assert a.clutch is None
    assert a.clutcher is None


def test_both_sides_reach_one_simultaneously_no_clutch():
    # The kill that creates a lone survivor also leaves the *other* side at
    # exactly 1 → no >= 2 opponents → not a clutch.
    tr = RoundTracker()
    tr.feed_players(_scoreboard(ct=["c1", "c2"], t=["t1", "t2"]))
    tr.feed_event(RoundStartEvent(round=1))
    tr.feed_event(_kill("c1", "t1"))            # CT 2, T 1
    tr.feed_event(_kill("t2", "c1", killer_side="T", victim_side="CT"))  # CT 1, T 1
    a = tr.feed_event(RoundOverEvent(round=1, winner_side="CT", reason="x"))
    assert a.clutch is None


def test_no_clutch_without_rosters_even_when_one_side_wiped():
    # Rosters unknown → alive sets stay empty → clutch detection disabled,
    # but opening/multikill still work.
    tr = RoundTracker()
    tr.feed_event(RoundStartEvent(round=1))
    a_open = tr.feed_event(_kill("c1", "t1"))
    tr.feed_event(_kill("c1", "t2"))
    a = tr.feed_event(RoundOverEvent(round=1, winner_side="CT", reason="x"))
    assert a_open.opening is True
    assert a.clutch is None
    assert a.clutcher is None


def test_only_first_clutch_situation_recorded():
    # Once a 1vN is recorded, a later re-derived lone-survivor flip on the
    # other side must not overwrite it (clutch frozen at first detection).
    tr = RoundTracker()
    _roster5(tr)
    tr.feed_event(RoundStartEvent(round=1))
    # t1 wipes CT down to c5; no T has died yet → c5 is lone vs 5 → 1v5.
    tr.feed_event(_kill("t1", "c1", killer_side="T", victim_side="CT"))
    tr.feed_event(_kill("t1", "c2", killer_side="T", victim_side="CT"))
    tr.feed_event(_kill("t1", "c3", killer_side="T", victim_side="CT"))
    tr.feed_event(_kill("t1", "c4", killer_side="T", victim_side="CT"))
    # c5 then clears T; the recorded clutch count must stay frozen at 5.
    tr.feed_event(_kill("c5", "t1"))
    tr.feed_event(_kill("c5", "t2"))
    tr.feed_event(_kill("c5", "t3"))
    tr.feed_event(_kill("c5", "t4"))
    tr.feed_event(_kill("c5", "t5"))
    a = tr.feed_event(RoundOverEvent(round=1, winner_side="CT", reason="x"))
    assert a.clutch == "1v5"
    assert a.clutcher == "c5"


def test_bomb_events_produce_empty_annotation():
    tr = RoundTracker()
    _roster5(tr)
    tr.feed_event(RoundStartEvent(round=1))
    a_plant = tr.feed_event(BombPlantEvent(round=1, planter="t1"))
    a_defuse = tr.feed_event(BombDefuseEvent(round=1, defuser="c1"))
    assert a_plant == Annotation()
    assert a_defuse == Annotation()

# ── regressions from adversarial review ────────────────────────────────

def test_consecutive_round_overs_do_not_leak_clutch():
    """A round-over for a round we never saw start (reconnect score-jump)
    must not inherit the previous round's clutch/alive state."""
    tr = RoundTracker()
    _roster5(tr)
    tr.feed_event(RoundStartEvent(round=1))
    # T wipes four CTs -> c5 is a lone 1v5, then aces.
    for i, c in enumerate(("c1", "c2", "c3", "c4"), 1):
        tr.feed_event(_kill(f"t{i}", c, killer_side="T", victim_side="CT"))
    for i in range(1, 6):
        tr.feed_event(_kill("c5", f"t{i}"))
    a1 = tr.feed_event(RoundOverEvent(winner_side="CT", round=1))
    assert a1.clutch == "1v5" and a1.clutcher == "c5"
    # Round-over for round 2 with no round-start / kills in between.
    a2 = tr.feed_event(RoundOverEvent(winner_side="CT", round=2))
    assert a2.clutch is None and a2.clutcher is None
    assert a2.summary is not None and a2.summary.round == 2


def test_clutch_works_without_round_start_when_rosters_known():
    """Rosters fed but no round-bearing event yet: alive sets are seeded
    by feed_players, so a clutch in that window still registers."""
    tr = RoundTracker()
    _roster5(tr)
    # No RoundStartEvent; all events carry round=None.
    for i, c in enumerate(("c1", "c2", "c3", "c4"), 1):
        tr.feed_event(_kill(f"t{i}", c, killer_side="T", victim_side="CT", round=None))
    for i in range(1, 6):
        tr.feed_event(_kill("c5", f"t{i}", round=None))
    over = tr.feed_event(RoundOverEvent(winner_side="CT", round=None))
    assert over.clutch == "1v5" and over.clutcher == "c5"


def test_feed_players_mid_round_does_not_resurrect_dead():
    tr = RoundTracker()
    _roster5(tr)
    tr.feed_event(RoundStartEvent(round=1))
    tr.feed_event(_kill("c1", "t1"))
    # A late scoreboard mid-round must not re-seed alive sets.
    _roster5(tr)
    assert "t1" not in tr._alive["T"]


def test_round_summary_carries_winner_team_name():
    tr = RoundTracker()
    _roster5(tr)
    tr.feed_event(RoundStartEvent(round=1))
    over = tr.feed_event(RoundOverEvent(winner_side="CT", round=1, t_score=0, ct_score=1))
    assert over.summary is not None and over.summary.winner_team == "CTteam"
    over_t = tr.feed_event(RoundOverEvent(winner_side="T", round=2, t_score=1, ct_score=1))
    assert over_t.summary is not None and over_t.summary.winner_team == "Tteam"

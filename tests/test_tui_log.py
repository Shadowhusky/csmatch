"""Render-order tests for the live event-log (no Textual app needed —
the render helpers write into a plain rich Text)."""

from __future__ import annotations

from datetime import datetime

from rich.text import Text

from csmatch.scorebot import KillEvent, RoundOverEvent, RoundStartEvent
from csmatch.tui import DetailPane, LogEntry
from csmatch.analysis import Annotation, RoundSummary


def _entry(event, **ann_kw) -> LogEntry:
    return LogEntry(event=event, annotation=Annotation(**ann_kw), location=None)


def _pane_with_two_rounds() -> DetailPane:
    pane = DetailPane()
    ts = datetime.now()
    r1_summary = RoundSummary(
        round=1, map="de_inferno", winner_side="CT",
        t_score=0, ct_score=1, reason="Bomb defused", winner_team="BetBoom",
    )
    log = [
        _entry(RoundStartEvent(round=1, map="de_inferno", ts=ts)),
        _entry(KillEvent(killer="alpha", victim="x1", round=1, map="de_inferno", ts=ts), opening=True),
        _entry(KillEvent(killer="bravo", victim="x2", round=1, map="de_inferno", ts=ts)),
        _entry(
            RoundOverEvent(winner_side="CT", t_score=0, ct_score=1, round=1, map="de_inferno", ts=ts),
            summary=r1_summary, clutch="1v2", clutcher="bravo",
        ),
        _entry(RoundStartEvent(round=2, map="de_inferno", ts=ts)),
        _entry(KillEvent(killer="charlie", victim="x3", round=2, map="de_inferno", ts=ts), opening=True),
    ]
    pane._scorebot_log = log
    return pane


def test_structured_blocks_newest_first_entries_chronological():
    pane = _pane_with_two_rounds()
    text = Text()
    pane._render_structured_log(text)
    out = text.plain

    # Newest round block (R2) renders above R1.
    assert out.index("R2") < out.index("R1")
    # Within R1, the opening kill (alpha) comes before the later kill (bravo).
    r1_block = out[out.index("R1"):]
    assert r1_block.index("alpha") < r1_block.index("x2")
    # The clutch line is the block's closing beat, after the last kill.
    assert r1_block.index("1v2") > r1_block.index("x2")
    # Header carries the result with the winning team's name.
    header_line = next(line for line in out.splitlines() if "R1" in line)
    assert "BetBoom" in header_line and "defused" in header_line and "0-1" in header_line


def test_narrative_blocks_newest_first_round_over_is_closing_line():
    pane = _pane_with_two_rounds()
    text = Text()
    pane._render_narrative_log(text)
    out = text.plain

    assert out.index("round 2") < out.index("round 1")
    r1_block = out[out.index("round 1"):]
    # Prose order: opening kill, second kill, then the round-over sentence.
    assert r1_block.index("alpha opened the round") < r1_block.index("bravo killed x2")
    assert r1_block.index("BetBoom won the round") > r1_block.index("bravo killed x2")
    assert "bravo clutched the 1v2" in r1_block


def test_cross_map_round_numbers_do_not_collide():
    """Map 1 R1 and map 2 R1 must form distinct blocks with their own
    results (round numbers repeat across maps)."""
    pane = DetailPane()
    ts = datetime.now()
    m1 = RoundSummary(round=1, map="de_inferno", winner_side="CT",
                      t_score=0, ct_score=1, reason="Enemy eliminated", winner_team="BetBoom")
    pane._scorebot_log = [
        _entry(KillEvent(killer="alpha", victim="x1", round=1, map="de_inferno", ts=ts), opening=True),
        _entry(RoundOverEvent(winner_side="CT", t_score=0, ct_score=1, round=1, map="de_inferno", ts=ts),
               summary=m1),
        _entry(KillEvent(killer="zulu", victim="x9", round=1, map="de_mirage", ts=ts), opening=True),
    ]
    blocks = pane._round_blocks()
    assert len(blocks) == 2
    # Map-2's block has no round-over → no result leaks from map 1.
    assert pane._block_result(blocks[1]) is None

    text = Text()
    pane._render_structured_log(text)
    out = text.plain
    mirage_header = next(line for line in out.splitlines() if "mirage" in line)
    assert "BetBoom" not in mirage_header


def test_round_start_epoch_splits_same_round_blocks():
    """Warmup kills (reported as round 1) and the real round 1 must not
    merge: the round-start increments the block epoch."""
    pane = DetailPane()
    ts = datetime.now()
    pane.push_scorebot(KillEvent(killer="warm", victim="up", round=1, map="de_anubis", ts=ts))
    pane.push_scorebot(RoundStartEvent(round=1, map="de_anubis", ts=ts))
    pane.push_scorebot(KillEvent(killer="real", victim="deal", round=1, map="de_anubis", ts=ts))
    blocks = pane._round_blocks()
    assert len(blocks) == 2
    assert blocks[0][0].event.killer == "warm"
    assert any(isinstance(e.event, KillEvent) and e.event.killer == "real" for e in blocks[1])

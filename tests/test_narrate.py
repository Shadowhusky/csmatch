"""Tests for the pure narration layer (no Playwright, no network).

`ann` is duck-typed in the real code, so tests build it with
`types.SimpleNamespace` rather than importing `analysis.Annotation`.
"""

from __future__ import annotations

from types import SimpleNamespace

from csmatch.narrate import narrate, weapon_name
from csmatch.scorebot import (
    BombDefuseEvent,
    BombPlantEvent,
    KillEvent,
    OtherEvent,
    RoundOverEvent,
    RoundStartEvent,
)


def _ann(**kw):
    base = {"opening": False, "multikill": 0, "clutch": None, "clutcher": None, "summary": None}
    base.update(kw)
    return SimpleNamespace(**base)


def _summary(winner_side="CT", reason="Enemy eliminated", t_score=6, ct_score=8, round=14):
    return SimpleNamespace(
        winner_side=winner_side, reason=reason, t_score=t_score, ct_score=ct_score, round=round
    )


def test_weapon_name_known():
    assert weapon_name("ak47") == "AK-47"
    assert weapon_name("m4a1") == "M4A4"
    assert weapon_name("m4a1_silencer") == "M4A1-S"
    assert weapon_name("usp_silencer") == "USP-S"
    assert weapon_name("awp") == "AWP"
    assert weapon_name("deagle") == "Desert Eagle"
    assert weapon_name("revolver") == "R8 Revolver"
    assert weapon_name("elite") == "Dual Berettas"
    assert weapon_name("hegrenade") == "HE grenade"
    assert weapon_name("inferno") == "molotov"
    assert weapon_name("molotov") == "molotov"
    assert weapon_name("incgrenade") == "incendiary"
    assert weapon_name("ssg08") == "SSG 08"
    assert weapon_name("sg556") == "SG 553"
    assert weapon_name("galilar") == "Galil AR"
    assert weapon_name("cz75a") == "CZ75-Auto"
    assert weapon_name("fiveseven") == "Five-SeveN"
    assert weapon_name("taser") == "Zeus"
    assert weapon_name("knife") == "knife"


def test_weapon_name_unknown_passthrough():
    assert weapon_name("nuke_launcher") == "nuke_launcher"


def test_weapon_name_none():
    assert weapon_name(None) == ""


def test_kill_no_weapon_omits_phrase():
    ev = KillEvent(killer="rikko", victim="Spinx", weapon=None)
    assert narrate(ev, _ann(), None) == "rikko killed Spinx"
    out = narrate(ev, _ann(opening=True), "mid")
    assert out == "rikko opened the round, killing Spinx at mid"


def test_kill_basic():
    ev = KillEvent(killer="rikko", victim="Spinx", weapon="ak47")
    assert narrate(ev, _ann(), "ramp") == "rikko killed Spinx with an AK-47 at ramp"


def test_kill_no_location():
    ev = KillEvent(killer="rikko", victim="Spinx", weapon="ak47")
    assert narrate(ev, _ann(), None) == "rikko killed Spinx with an AK-47"


def test_kill_opening():
    ev = KillEvent(killer="rikko", victim="torzsi", weapon="ak47")
    out = narrate(ev, _ann(opening=True), "A site")
    assert out == "rikko opened the round, killing torzsi with an AK-47 at A site"


def test_kill_headshot():
    ev = KillEvent(killer="rikko", victim="Spinx", weapon="awp", headshot=True)
    assert narrate(ev, _ann(), None) == "rikko killed Spinx with an AWP (headshot)"


def test_kill_assist():
    ev = KillEvent(killer="rikko", victim="torzsi", weapon="ak47", assist="KRIMZ")
    assert narrate(ev, _ann(), None) == "rikko killed torzsi with an AK-47 (assisted by KRIMZ)"


def test_kill_flash_assist():
    ev = KillEvent(killer="rikko", victim="torzsi", weapon="ak47", assist="KRIMZ", flash_assist=True)
    assert narrate(ev, _ann(), None) == "rikko killed torzsi with an AK-47 (flashed by KRIMZ)"


def test_kill_headshot_and_assist():
    ev = KillEvent(
        killer="rikko", victim="torzsi", weapon="ak47", headshot=True, assist="KRIMZ"
    )
    out = narrate(ev, _ann(opening=True), "A site")
    assert out == (
        "rikko opened the round, killing torzsi with an AK-47 "
        "(headshot, assisted by KRIMZ) at A site"
    )


def test_kill_multikill_3k():
    ev = KillEvent(killer="Jackinho", victim="Brollan", weapon="deagle", headshot=True)
    out = narrate(ev, _ann(multikill=3), "A site")
    assert out == "Jackinho killed Brollan with a Desert Eagle (headshot) — 3K at A site"


def test_kill_multikill_4k():
    ev = KillEvent(killer="Jackinho", victim="Brollan", weapon="awp")
    assert narrate(ev, _ann(multikill=4), None) == "Jackinho killed Brollan with an AWP — 4K"


def test_kill_ace():
    ev = KillEvent(killer="Jackinho", victim="Brollan", weapon="awp")
    assert narrate(ev, _ann(multikill=5), None) == "Jackinho killed Brollan with an AWP — ace"


def test_kill_multikill_below_threshold():
    ev = KillEvent(killer="Jackinho", victim="Brollan", weapon="awp")
    assert narrate(ev, _ann(multikill=2), None) == "Jackinho killed Brollan with an AWP"


def test_bomb_plant_with_alive_counts():
    ev = BombPlantEvent(planter="b1t", t_alive=3, ct_alive=2)
    assert narrate(ev, _ann(), None) == "b1t planted the bomb (3v2)"


def test_bomb_plant_no_counts():
    ev = BombPlantEvent(planter="b1t")
    assert narrate(ev, _ann(), None) == "b1t planted the bomb"


def test_bomb_defuse():
    ev = BombDefuseEvent(defuser="Jackinho")
    assert narrate(ev, _ann(), None) == "Jackinho defused the bomb"


def test_round_over_ct():
    ev = RoundOverEvent(winner_side="CT")
    out = narrate(ev, _ann(summary=_summary(winner_side="CT", reason="Bomb defused")), None)
    assert out == "CT won the round — bomb defused (6-8)."


def test_round_over_t():
    ev = RoundOverEvent(winner_side="T")
    summary = _summary(winner_side="T", reason="Target bombed", t_score=7, ct_score=6)
    assert narrate(ev, _ann(summary=summary), None) == "T won the round — target bombed (7-6)."


def test_round_over_with_clutch():
    ev = RoundOverEvent(winner_side="CT")
    out = narrate(
        ev,
        _ann(summary=_summary(reason="Bomb defused"), clutch="1v2", clutcher="Jackinho"),
        None,
    )
    assert out == "CT won the round — bomb defused (6-8). Jackinho clutched the 1v2."


def test_round_over_clutch_requires_clutcher():
    ev = RoundOverEvent(winner_side="CT")
    out = narrate(ev, _ann(summary=_summary(reason="Time"), clutch="1v2", clutcher=None), None)
    assert out == "CT won the round — time (6-8)."


def test_round_over_no_summary():
    ev = RoundOverEvent(winner_side="CT")
    assert narrate(ev, _ann(), None) == ""


def test_round_start_empty():
    ev = RoundStartEvent()
    assert narrate(ev, _ann(), None) == ""


def test_other_event_text():
    ev = OtherEvent(text="something happened")
    assert narrate(ev, _ann(), None) == "something happened"

def test_round_over_prefers_team_name():
    ev = RoundOverEvent(winner_side="CT")
    summary = _summary(winner_side="CT", reason="Bomb defused")
    summary.winner_team = "BetBoom"
    out = narrate(ev, _ann(summary=summary), None)
    assert out == "BetBoom won the round — bomb defused (6-8)."


def test_article_overrides_for_initialisms():
    ev = KillEvent(killer="a", victim="b", weapon="m4a1_silencer")
    assert "with an M4A1-S" in narrate(ev, _ann(), None)
    ev = KillEvent(killer="a", victim="b", weapon="m4a1")
    assert "with an M4A4" in narrate(ev, _ann(), None)
    ev = KillEvent(killer="a", victim="b", weapon="usp_silencer")
    assert "with a USP-S" in narrate(ev, _ann(), None)
    ev = KillEvent(killer="a", victim="b", weapon="ump45")
    assert "with a UMP-45" in narrate(ev, _ann(), None)
    ev = KillEvent(killer="a", victim="b", weapon="ssg08")
    assert "with an SSG 08" in narrate(ev, _ann(), None)
    ev = KillEvent(killer="a", victim="b", weapon="famas")
    assert "with a FAMAS" in narrate(ev, _ann(), None)

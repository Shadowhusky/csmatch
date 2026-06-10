"""Natural-language narration of enriched scorebot events (pure).

Turns a single `ScorebotEvent` plus its `Annotation` (duck-typed, from
`analysis.RoundTracker`) and an optional kill location into one concise
prose sentence for the narrative display mode. No Playwright, no network.
"""

from __future__ import annotations

from .scorebot import (
    BombDefuseEvent,
    BombPlantEvent,
    KillEvent,
    RoundOverEvent,
    RoundStartEvent,
    ScorebotEvent,
)

_WEAPONS = {
    "ak47": "AK-47",
    "m4a1": "M4A4",
    "m4a1_silencer": "M4A1-S",
    "awp": "AWP",
    "usp_silencer": "USP-S",
    "hkp2000": "P2000",
    "glock": "Glock-18",
    "deagle": "Desert Eagle",
    "revolver": "R8 Revolver",
    "elite": "Dual Berettas",
    "p250": "P250",
    "tec9": "Tec-9",
    "fiveseven": "Five-SeveN",
    "cz75a": "CZ75-Auto",
    "ssg08": "SSG 08",
    "sg556": "SG 553",
    "aug": "AUG",
    "scar20": "SCAR-20",
    "g3sg1": "G3SG1",
    "galilar": "Galil AR",
    "famas": "FAMAS",
    "mac10": "MAC-10",
    "mp9": "MP9",
    "mp7": "MP7",
    "mp5sd": "MP5-SD",
    "ump45": "UMP-45",
    "p90": "P90",
    "bizon": "PP-Bizon",
    "nova": "Nova",
    "xm1014": "XM1014",
    "mag7": "MAG-7",
    "sawedoff": "Sawed-Off",
    "m249": "M249",
    "negev": "Negev",
    "hegrenade": "HE grenade",
    "inferno": "molotov",
    "molotov": "molotov",
    "incgrenade": "incendiary",
    "flashbang": "flashbang",
    "smokegrenade": "smoke grenade",
    "decoy": "decoy",
    "taser": "Zeus",
    "knife": "knife",
    "knife_t": "knife",
}


def weapon_name(w: str | None) -> str:
    """Full display name for a CS2 weapon id; unknown ids pass through raw."""
    if not w:
        return ""
    return _WEAPONS.get(w, w)


# Names whose article doesn't follow the first-letter vowel rule: spoken
# initialisms ("an em-four", "a you-ess-pee") rather than written letters.
_ARTICLE_AN = {"M4A4", "M4A1-S", "M249", "MP9", "MP7", "MP5-SD", "SSG 08", "SG 553", "XM1014", "R8 Revolver", "HE grenade"}
_ARTICLE_A = {"USP-S", "UMP-45"}


def _article(name: str) -> str:
    if name in _ARTICLE_AN:
        return "an"
    if name in _ARTICLE_A:
        return "a"
    return "an" if name[:1].lower() in "aeiou" else "a"


def _kill_sentence(event: KillEvent, ann: object, location: str | None) -> str:
    weapon = weapon_name(event.weapon)
    with_weapon = f" with {_article(weapon)} {weapon}" if weapon else ""
    if getattr(ann, "opening", False):
        base = f"{event.killer} opened the round, killing {event.victim}{with_weapon}"
    else:
        base = f"{event.killer} killed {event.victim}{with_weapon}"

    notes: list[str] = []
    if event.headshot:
        notes.append("headshot")
    if event.assist:
        notes.append(f"flashed by {event.assist}" if event.flash_assist else f"assisted by {event.assist}")
    if notes:
        base += f" ({', '.join(notes)})"

    multikill = getattr(ann, "multikill", 0)
    if multikill >= 5:
        base += " — ace"
    elif multikill >= 3:
        base += f" — {multikill}K"

    if location:
        base += f" at {location}"
    return base


def narrate(event: ScorebotEvent, ann: object, location: str | None) -> str:
    """One concise natural-language sentence for an enriched event.

    RoundStartEvent returns "" (the round header carries it).
    """
    if isinstance(event, KillEvent):
        return _kill_sentence(event, ann, location)

    if isinstance(event, BombPlantEvent):
        sentence = f"{event.planter} planted the bomb"
        if event.t_alive is not None and event.ct_alive is not None:
            sentence += f" ({event.t_alive}v{event.ct_alive})"
        return sentence

    if isinstance(event, BombDefuseEvent):
        return f"{event.defuser} defused the bomb"

    if isinstance(event, RoundOverEvent):
        return _round_over_sentence(ann)

    if isinstance(event, RoundStartEvent):
        return ""

    return getattr(event, "text", "") or ""


def _round_over_sentence(ann: object) -> str:
    summary = getattr(ann, "summary", None)
    if summary is None:
        return ""
    # Prefer the team name ("BetBoom won the round") over the bare side.
    winner = getattr(summary, "winner_team", None) or summary.winner_side
    reason = (summary.reason or "").lower() or "round over"
    sentence = f"{winner} won the round — {reason} ({summary.t_score}-{summary.ct_score})."
    clutch = getattr(ann, "clutch", None)
    clutcher = getattr(ann, "clutcher", None)
    if clutch and clutcher:
        sentence += f" {clutcher} clutched the {clutch}."
    return sentence

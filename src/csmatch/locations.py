"""Approximate bombsite-relative zones for CS2 world coordinates.

Pure helper used by the live log to label a kill with a coarse zone
("A site" / "B site" / "mid" / a few approach/spawn callouts). Each map
carries a small table of landmarks in WORLD coordinates; a coordinate is
classified by its nearest landmark (Euclidean in world units).

Landmark coordinates are APPROXIMATE / best-effort. They are anchored to
verified CT/T spawn positions and to each map's radar calibration
(pos_x/pos_y/scale → world bounds), then placed by map layout. They are
meant to be validated against live captures later; until then a wrong
zone is worse than none, so unknown maps are omitted rather than guessed.

Sources for the anchors:
- Radar calibration (pos_x, pos_y, scale) per map: awpy / CS2Callouts
  ``map-data.json`` — gives each radar's world bounds.
- CT/T spawn world coordinates: community ``setpos`` spawn guides.

de_nuke is intentionally absent: its A (upper) and B (lower) sites are
stacked vertically and share almost the same 2D position, so a 2D
Euclidean classifier cannot tell them apart reliably.
"""

from __future__ import annotations

import math

# Per-map landmarks: (zone, world_x, world_y). Approximate — see module
# docstring. Coordinates are in CS2 world units.
_LANDMARKS: dict[str, list[tuple[str, float, float]]] = {
    "de_mirage": [
        ("A site", -380.0, -2050.0),
        ("B site", -2050.0, 400.0),
        ("mid", -650.0, -600.0),
        ("T spawn", 1250.0, -150.0),
        ("CT spawn", -1750.0, -1900.0),
    ],
    "de_inferno": [
        ("A site", 2050.0, 400.0),
        ("B site", 420.0, 2600.0),
        ("mid", 700.0, 1000.0),
        ("B approach", -400.0, 1700.0),  # banana
        ("T spawn", -1580.0, 540.0),
        ("CT spawn", 2400.0, 2050.0),
    ],
    "de_dust2": [
        ("A site", 1250.0, 2350.0),
        ("B site", -1550.0, 2550.0),
        ("mid", -200.0, 1000.0),
        ("A approach", 1000.0, 250.0),  # long
        ("T spawn", -750.0, -790.0),
        ("CT spawn", 280.0, 2410.0),
    ],
    "de_ancient": [
        ("A site", -1300.0, 1350.0),
        ("B site", 1100.0, -100.0),
        ("mid", -300.0, -450.0),
        ("T spawn", -440.0, -2320.0),
        ("CT spawn", -360.0, 1650.0),
    ],
    "de_anubis": [
        ("A site", 950.0, 1250.0),
        ("B site", -1450.0, 1100.0),
        ("mid", -300.0, 250.0),
        ("T spawn", -280.0, -1600.0),
        ("CT spawn", -400.0, 2200.0),
    ],
    "de_overpass": [
        ("A site", -2050.0, 200.0),
        ("B site", -3550.0, -1900.0),
        ("mid", -2600.0, -1500.0),
        ("T spawn", -1420.0, -3200.0),
        ("CT spawn", -2260.0, 800.0),
    ],
    "de_vertigo": [
        ("A site", -300.0, 260.0),
        ("B site", -2150.0, -450.0),
        ("mid", -1450.0, 180.0),
        ("T spawn", -1400.0, -1300.0),
        ("CT spawn", -980.0, 820.0),
    ],
    "de_train": [
        ("A site", 80.0, -720.0),
        ("B site", -460.0, 830.0),
        ("mid", -300.0, -100.0),
        ("T spawn", -1950.0, 1360.0),
        ("CT spawn", 1500.0, -1330.0),
    ],
}


def _canonical_map(name: str) -> str | None:
    """Normalise a map name to its ``de_*`` key, or None if unknown."""
    key = name.strip().lower()
    if not key.startswith("de_"):
        key = f"de_{key}"
    return key if key in _LANDMARKS else None


# A real on-map position is always within ~1.5k units of some landmark;
# anything farther is bogus data, and a wrong zone is worse than none.
_MAX_LANDMARK_DIST = 3000.0


def zone_for(map_name: str | None, x: float | None, y: float | None) -> str | None:
    """Coarse bombsite-relative zone for a world coordinate on a known map.

    Returns e.g. 'A site', 'B site', 'mid', or None when the map is unknown
    or coords are missing/non-finite/out of range. Classification is by
    nearest landmark.
    """
    if map_name is None or x is None or y is None:
        return None
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    key = _canonical_map(map_name)
    if key is None:
        return None
    landmarks = _LANDMARKS[key]
    nearest = min(landmarks, key=lambda lm: math.dist((x, y), (lm[1], lm[2])))
    if math.dist((x, y), (nearest[1], nearest[2])) > _MAX_LANDMARK_DIST:
        return None
    return nearest[0]

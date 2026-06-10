"""Tests for the approximate map-zone classifier (pure, no network)."""

from __future__ import annotations

import pytest

from csmatch.locations import _LANDMARKS, zone_for


def test_unknown_map_returns_none():
    assert zone_for("de_cache", 0.0, 0.0) is None
    assert zone_for("cache", 0.0, 0.0) is None
    assert zone_for(None, 0.0, 0.0) is None


def test_missing_coords_return_none():
    assert zone_for("de_mirage", None, 100.0) is None
    assert zone_for("de_mirage", 100.0, None) is None
    assert zone_for("de_mirage", None, None) is None


def test_coord_at_landmark_classifies_to_that_zone():
    assert zone_for("de_mirage", -380.0, -2050.0) == "A site"
    assert zone_for("de_mirage", -2050.0, 400.0) == "B site"
    assert zone_for("de_inferno", 2050.0, 400.0) == "A site"
    assert zone_for("de_dust2", -1550.0, 2550.0) == "B site"


def test_coord_near_landmark_classifies_to_that_zone():
    # Small offsets from a known site stay on that site.
    assert zone_for("de_mirage", -350.0, -2000.0) == "A site"
    assert zone_for("de_dust2", 1300.0, 2300.0) == "A site"
    assert zone_for("de_inferno", 650.0, 950.0) == "mid"


def test_map_name_accepts_short_and_cased_forms():
    assert zone_for("mirage", -380.0, -2050.0) == "A site"
    assert zone_for("DE_MIRAGE", -380.0, -2050.0) == "A site"
    assert zone_for(" Inferno ", 2050.0, 400.0) == "A site"


@pytest.mark.parametrize("map_name", sorted(_LANDMARKS))
def test_every_landmark_classifies_to_itself(map_name):
    for zone, x, y in _LANDMARKS[map_name]:
        assert zone_for(map_name, x, y) == zone


@pytest.mark.parametrize("map_name", sorted(_LANDMARKS))
def test_returns_a_known_zone_for_in_bounds_coord(map_name):
    zones = {lm[0] for lm in _LANDMARKS[map_name]}
    result = zone_for(map_name, 0.0, 0.0)
    assert result in zones


def test_non_finite_coords_return_none():
    for bad in (float("nan"), float("inf"), float("-inf")):
        assert zone_for("de_inferno", bad, 100.0) is None
        assert zone_for("de_inferno", 100.0, bad) is None


def test_far_out_of_range_coords_return_none():
    # Way beyond any real map extent -> bogus data, omit rather than guess.
    assert zone_for("de_inferno", 1e9, -1e9) is None
    assert zone_for("de_mirage", 50000.0, 50000.0) is None


def test_zero_origin_still_classifies():
    assert zone_for("de_inferno", 0.0, 0.0) is not None

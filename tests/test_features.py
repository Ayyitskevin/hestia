"""Shoot-type presets → feature flags."""

import pytest

from hestia.features import SHOOT_TYPES, flags_for, normalize_shoot_type


@pytest.mark.parametrize("shoot_type,album,heroes", [
    ("wedding", True, 8),
    ("event", True, 6),
    ("portrait", True, 5),
    ("commercial", False, 5),
    ("food", False, 5),
    ("other", False, 5),
])
def test_flags_table(shoot_type, album, heroes):
    flags = flags_for(shoot_type)
    assert flags.shoot_type == shoot_type
    assert flags.album_offer is album
    assert flags.hero_count == heroes


def test_unknown_shoot_type_defaults_to_other():
    assert normalize_shoot_type("interpretive-dance") == "other"
    assert normalize_shoot_type(None) == "other"
    assert flags_for("nonsense").shoot_type == "other"


def test_case_insensitive():
    assert flags_for("Wedding").album_offer is True


def test_all_shoot_types_resolve():
    for st in SHOOT_TYPES:
        assert flags_for(st).shoot_type == st

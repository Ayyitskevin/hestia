"""Shoot-type presets → in-app tuning (no microservice flags anymore).

Hestia is one horizontal product; shoot type tunes *defaults* — whether offers
emphasize albums, how many hero picks to surface, the storefront tone. Pure and
unit-testable; the single source of truth for "how does this shoot type sell".

| shoot_type | album offer | hero picks |
|------------|-------------|-----------|
| wedding    | yes         | 8         |
| event      | yes         | 6         |
| portrait   | yes         | 5         |
| commercial | no          | 5         |
| food       | no          | 5         |
| other      | no          | 5         |
"""

from __future__ import annotations

from dataclasses import dataclass

SHOOT_TYPES = ("wedding", "event", "portrait", "commercial", "food", "other")
DEFAULT_SHOOT_TYPE = "other"

SHOOT_TYPE_LABELS = {
    "wedding": "Wedding",
    "event": "Event",
    "portrait": "Portrait",
    "commercial": "Commercial",
    "food": "Food",
    "other": "Other / Mixed",
}


@dataclass(frozen=True)
class FeatureFlags:
    shoot_type: str
    album_offer: bool   # include an album bundle in generated offers
    hero_count: int     # how many hero picks to surface

    def as_dict(self) -> dict:
        return {
            "shoot_type": self.shoot_type,
            "album_offer": self.album_offer,
            "hero_count": self.hero_count,
        }


# shoot_type → (album_offer, hero_count)
_PRESETS: dict[str, tuple[bool, int]] = {
    "wedding": (True, 8),
    "event": (True, 6),
    "portrait": (True, 5),
    "commercial": (False, 5),
    "food": (False, 5),
    "other": (False, 5),
}


def normalize_shoot_type(shoot_type: str | None) -> str:
    if shoot_type and shoot_type.lower() in _PRESETS:
        return shoot_type.lower()
    return DEFAULT_SHOOT_TYPE


def flags_for(shoot_type: str | None) -> FeatureFlags:
    st = normalize_shoot_type(shoot_type)
    album_offer, hero_count = _PRESETS[st]
    return FeatureFlags(shoot_type=st, album_offer=album_offer, hero_count=hero_count)

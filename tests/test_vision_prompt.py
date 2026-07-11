"""Vision prompt hardening — culling guidance and demo fixtures."""

from hestia.founder_demo import _SHOWCASE_FRAMES
from hestia.vision import (
    BLINK_THRESHOLD,
    BRIGHT_THRESHOLD,
    DARK_THRESHOLD,
    KEEPER_THRESHOLD,
    SHARP_THRESHOLD,
    MockVisionProvider,
    vision_prompt,
)


def test_vision_prompt_covers_blink_hero_and_technical_scores():
    prompt = vision_prompt()
    assert "eyes_closed" in prompt
    assert "hero_potential" in prompt
    assert "keeper_score" in prompt
    assert "sharpness" in prompt
    assert "exposure" in prompt
    assert "0.85" in prompt  # blink discard guidance


def test_vision_prompt_includes_style_preference():
    styled = vision_prompt("moody documentary")
    assert "moody documentary" in styled
    assert "style preference" in styled


def test_documented_cull_thresholds_are_stable():
    assert BLINK_THRESHOLD == 0.85
    assert KEEPER_THRESHOLD == 0.7
    assert SHARP_THRESHOLD == 0.40
    assert DARK_THRESHOLD == 0.35
    assert BRIGHT_THRESHOLD == 0.90


def test_showcase_fixture_has_blink_and_duplicate_targets():
    """Founder demo frames are named so mock vision flags blink + duplicate reliably."""
    filenames = [name for name, _ in _SHOWCASE_FRAMES]
    assert "frame-03.jpg" in filenames
    assert filenames.count("frame-02.jpg") >= 1 or filenames.count("frame-04.jpg") >= 1
    blink = MockVisionProvider().analyze(filename="frame-03.jpg", data=b"x")
    assert blink.eyes_closed >= BLINK_THRESHOLD


def test_mock_duplicate_bytes_share_dup_key():
    from hestia.vision import content_dup_key

    assert content_dup_key(b"SAME") == content_dup_key(b"SAME")
    assert content_dup_key(b"A") != content_dup_key(b"B")

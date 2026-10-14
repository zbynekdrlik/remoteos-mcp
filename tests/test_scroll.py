"""Scroll units (#29): the pure builder behind every provider's ``scroll()``.

One unit on every OS: ``amount`` = wheel CLICKS (notches) and ``direction`` =
up / down / left / right. The legacy signed ``amount`` + ``horizontal`` is still
accepted and mapped (positive = up/right); an explicit ``direction`` wins.
Windows sends ``clicks × WHEEL_DELTA`` (120) per wheel event.
"""

from __future__ import annotations

import pytest

from remoteos.scroll import chunks, normalize_request, wheel_steps

# (direction, clicks) → (dy, dx, horizontal, Windows mouseData)
_TABLE = [
    ("up", 3, 3, 0, False, 360),
    ("down", 3, -3, 0, False, -360),
    ("left", 2, 0, -2, True, -240),
    ("right", 2, 0, 2, True, 240),
    ("down", 1, -1, 0, False, -120),
]


@pytest.mark.parametrize(("direction", "clicks", "dy", "dx", "horizontal", "delta"), _TABLE)
def test_wheel_steps_table(direction, clicks, dy, dx, horizontal, delta):
    steps = wheel_steps(direction, clicks)
    assert (steps.direction, steps.clicks) == (direction, clicks)
    assert (steps.dy, steps.dx) == (dy, dx)
    assert steps.horizontal is horizontal
    assert steps.delta == delta


def test_clicks_are_clamped_to_1_100():
    assert wheel_steps("down", 500).clicks == 100
    assert wheel_steps("down", 500).delta == -12000  # still a valid signed 16-bit wheel delta
    assert wheel_steps("up", 0).clicks == 1
    assert wheel_steps("up", 100).clicks == 100


def test_direction_is_case_and_space_insensitive():
    assert wheel_steps(" Down ", 2).direction == "down"
    assert wheel_steps("RIGHT", 2).dx == 2


@pytest.mark.parametrize("bad", ["sideways", "", "u p", "north"])
def test_unknown_direction_is_an_error(bad):
    with pytest.raises(ValueError, match="direction must be one of up, down, left, right"):
        wheel_steps(bad, 1)


def test_summary_names_clicks_and_direction():
    assert wheel_steps("down", 3).summary() == "Scrolled 3 clicks down"
    assert wheel_steps("left", 1).summary() == "Scrolled 1 click left"


def test_summary_reports_a_clamp():
    assert wheel_steps("up", 500).summary() == "Scrolled 100 clicks up (requested 500, clamped to 1..100)"
    assert wheel_steps("up", 0).summary() == "Scrolled 1 click up (requested 0, clamped to 1..100)"


# --- the Scroll tool's arguments: new direction form + the legacy signed form ---


@pytest.mark.parametrize(
    ("amount", "horizontal", "direction", "clicks"),
    [
        (-5, False, "down", 5),  # legacy: negative = down
        (5, False, "up", 5),  # legacy: positive = up
        (3, True, "right", 3),  # legacy horizontal: positive = right
        (-3, True, "left", 3),  # legacy horizontal: negative = left
        (0, False, "down", 1),  # zero: down/left (as the Linux provider always did), clamped to 1
        (-500, False, "down", 100),
    ],
)
def test_legacy_signed_amount_is_mapped(amount, horizontal, direction, clicks):
    steps = normalize_request(amount, horizontal=horizontal)
    assert (steps.direction, steps.clicks) == (direction, clicks)


def test_explicit_direction_wins_over_sign_and_horizontal():
    steps = normalize_request(-3, direction="up", horizontal=True)
    assert (steps.direction, steps.clicks, steps.dy) == ("up", 3, 3)
    assert normalize_request(7, direction="left").dx == -7


def test_request_keeps_the_requested_count_for_the_clamp_note():
    assert normalize_request(-500).requested == 500
    assert normalize_request(4, direction="down").requested == 4


def test_request_with_unknown_direction_is_an_error():
    with pytest.raises(ValueError):
        normalize_request(3, direction="diagonal")


# --- macOS splits a scroll into events of at most 10 lines (Apple's range) ---


@pytest.mark.parametrize(
    ("total", "parts"),
    [(3, [3]), (10, [10]), (25, [10, 10, 5]), (-25, [-10, -10, -5]), (-7, [-7]), (100, [10] * 10)],
)
def test_chunks_split_a_signed_total(total, parts):
    assert chunks(total, 10) == parts

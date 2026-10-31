"""Linux drag (#38): a real motion over ``duration``, and the button always released.

Only ``session._run_x`` (the xdotool subprocess) and the clock are faked.
"""

from __future__ import annotations

import time

import pytest

from remoteos.platform.linux import desktop as d
from remoteos.platform.linux import session as linux_session


class _Proc:
    returncode = 0
    stderr = b""

    def __init__(self, stdout: bytes = b"") -> None:
        self.stdout = stdout


@pytest.fixture
def xdotool(monkeypatch):
    """Records the xdotool argv and the sleeps; ``fail_on`` = the argv to fail."""
    monkeypatch.setattr(linux_session, "_SESSION", d.SessionInfo(mode="x11", display=":0", uid=1000, user="u"))
    box = type("Box", (), {"calls": [], "sleeps": [], "fail_on": None})()

    def run_x(cmd, *, input_bytes=None, timeout=15, discard_output=False):
        box.calls.append(cmd[1:])
        if box.fail_on is not None and cmd[1:] == box.fail_on:
            raise RuntimeError("xdotool failed (rc=1): no display")
        if cmd[1:] == ["getmouselocation", "--shell"]:
            return _Proc(b"X=100\nY=200\nSCREEN=0\nWINDOW=1\n")
        return _Proc()

    monkeypatch.setattr(linux_session, "_run_x", run_x)
    monkeypatch.setattr(time, "sleep", box.sleeps.append)
    return box


def _moves(box) -> list[tuple[int, int]]:
    return [(int(c[1]), int(c[2])) for c in box.calls if c[0] == "mousemove"]


def test_a_drag_moves_in_steps_between_press_and_release(xdotool):
    assert d.move(300, 200, drag=True, start_x=0, start_y=0, duration=0.3) == "Dragged to (300,200)"
    assert xdotool.calls[0] == ["mousemove", "0", "0"]
    assert xdotool.calls[1] == ["mousedown", "1"]
    assert xdotool.calls[-1] == ["mouseup", "1"]
    # 0.3 s at 30 steps a second: nine evenly spaced points, the last one the target
    assert _moves(xdotool)[1:] == [
        (33, 22),
        (67, 44),
        (100, 67),
        (133, 89),
        (167, 111),
        (200, 133),
        (233, 156),
        (267, 178),
        (300, 200),
    ]


def test_a_drag_takes_its_duration(xdotool):
    d.move(300, 200, drag=True, start_x=0, start_y=0, duration=1.0)
    assert len(xdotool.sleeps) == 30
    assert sum(xdotool.sleeps) == pytest.approx(1.0)


def test_a_drag_without_a_start_begins_at_the_cursor(xdotool):
    d.move(100, 300, drag=True, duration=0.1)
    assert xdotool.calls[0] == ["getmouselocation", "--shell"]
    assert xdotool.calls[1] == ["mousedown", "1"]
    assert _moves(xdotool) == [(100, 233), (100, 267), (100, 300)]


@pytest.mark.parametrize("duration", [0, -1])
def test_a_drag_with_no_duration_still_reaches_the_target_with_the_button_down(xdotool, duration):
    d.move(300, 200, drag=True, start_x=5, start_y=5, duration=duration)
    assert xdotool.calls == [["mousemove", "5", "5"], ["mousedown", "1"], ["mousemove", "300", "200"], ["mouseup", "1"]]
    assert xdotool.sleeps == []


def test_a_long_drag_stays_bounded(xdotool):
    d.move(300, 200, drag=True, start_x=0, start_y=0, duration=3600)
    assert sum(xdotool.sleeps) == pytest.approx(30.0)  # MAX_HOLD_S, the bound of a held key
    assert len(_moves(xdotool)) - 1 == 900
    assert _moves(xdotool)[-1] == (300, 200)


def test_a_failed_step_releases_the_button_and_reports_the_step(xdotool):
    xdotool.fail_on = ["mousemove", "100", "67"]
    with pytest.raises(RuntimeError, match="no display"):
        d.move(300, 200, drag=True, start_x=0, start_y=0, duration=0.3)
    assert xdotool.calls[-1] == ["mouseup", "1"]


def test_a_failed_release_after_a_failed_step_keeps_the_step_s_error(xdotool, monkeypatch):
    real = linux_session._run_x

    def run_x(cmd, **kwargs):
        if cmd[1:] == ["mouseup", "1"]:
            raise RuntimeError("release failed")
        if cmd[1:] == ["mousemove", "100", "67"]:
            raise RuntimeError("step failed")
        return real(cmd, **kwargs)

    monkeypatch.setattr(linux_session, "_run_x", run_x)
    with pytest.raises(RuntimeError, match="step failed"):
        d.move(300, 200, drag=True, start_x=0, start_y=0, duration=0.3)


def test_a_failed_release_after_a_good_drag_is_an_error(xdotool):
    xdotool.fail_on = ["mouseup", "1"]
    with pytest.raises(RuntimeError, match="no display"):
        d.move(300, 200, drag=True, start_x=0, start_y=0, duration=0.1)

"""The stated Wayland position (#38): the desktop tools exist there and fail with one clear message.

No backend is promised: this only pins that a client on a Wayland box learns what is missing,
and that the headless boxes (cam1..4) keep their contract.
"""

from __future__ import annotations

import pytest
from tool_surface import server
from tool_test_support import tool_error

from remoteos.platform.linux import desktop as linux_desktop
from remoteos.platform.linux import session as linux_session
from remoteos.tiers import (
    LINUX_ALWAYS_EXCLUDED_TOOLS,
    LINUX_DISPLAY_TOOLS,
    LINUX_EXCLUDED_TOOLS,
    linux_platform_excludes,
)


@pytest.fixture
def wayland(monkeypatch):
    monkeypatch.setattr(linux_session, "_SESSION", linux_desktop.SessionInfo(mode="wayland"))
    monkeypatch.setattr(server, "is_linux", lambda: True)
    monkeypatch.setattr(server, "is_windows", lambda: False)
    monkeypatch.setattr(server, "is_macos", lambda: False)
    monkeypatch.setattr("remoteos.platform.get_desktop", lambda: linux_desktop)
    monkeypatch.setattr(server, "get_desktop", lambda: linux_desktop)


def test_the_message_names_what_is_missing_and_what_to_do(wayland):
    message = linux_session._unavailable()
    assert "Wayland" in message
    assert "xdotool" in message and "scrot" in message  # what the tools need
    assert "X11 session" in message  # what to do


def test_wayland_keeps_the_display_tools_so_they_can_say_why():
    assert linux_platform_excludes("wayland") == LINUX_ALWAYS_EXCLUDED_TOOLS
    assert not linux_platform_excludes("wayland") & LINUX_DISPLAY_TOOLS


def test_x11_keeps_them_and_headless_loses_them_as_always():
    assert linux_platform_excludes("x11") == LINUX_ALWAYS_EXCLUDED_TOOLS
    assert linux_platform_excludes("headless") == LINUX_EXCLUDED_TOOLS


def test_the_exclude_set_is_a_copy():
    linux_platform_excludes("x11").add("Shell")
    assert "Shell" not in LINUX_ALWAYS_EXCLUDED_TOOLS


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("Snapshot", ()),
        ("Click", (1, 2)),
        ("Move", (1, 2)),
        ("ElementTree", ()),
        ("ClickElement", ("1.1",)),
        ("TypeElement", ("1.1", "x")),
    ],
)
def test_a_desktop_tool_fails_with_exactly_that_message(wayland, name, args):
    assert tool_error(lambda: getattr(server, name)(*args)) == linux_session._unavailable()

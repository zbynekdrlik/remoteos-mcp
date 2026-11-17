"""Real UI Automation against a live window (#35): the real ``uiautomation`` package, no fakes.

A small WinForms window in a real PowerShell child: a button that writes the text box to a file.
The button stands for what ClickElement must reach and the file proves the click happened.
"""

from __future__ import annotations

import subprocess
import time
import uuid
from pathlib import Path

import pytest
import pywintypes
import win32gui

from remoteos.element_tree import StaleRef
from remoteos.platform.command import kill_process_tree, own_console, powershell_argv
from remoteos.platform.win import desktop, uia
from remoteos.platform.win.psquote import ps_literal

SLOVAK = "Žltý kôň ďakuje – €"

FORM = r"""
Add-Type -AssemblyName System.Windows.Forms
$form = New-Object Windows.Forms.Form
$form.Text = {title}
$form.TopMost = $true
$form.Add_Shown({{ $form.Activate() }})
$form.Width = 400
$form.Height = 200
$field = New-Object Windows.Forms.TextBox
$field.AccessibleName = 'Field'
$field.Left = 10
$field.Top = 10
$field.Width = 300
$button = New-Object Windows.Forms.Button
$button.Text = 'Save'
$button.Left = 10
$button.Top = 50
$button.Width = 120
$button.Add_Click({{ [IO.File]::WriteAllText({marker}, $field.Text, [Text.Encoding]::UTF8) }})
$form.Controls.Add($field)
$form.Controls.Add($button)
$timer = New-Object Windows.Forms.Timer
$timer.Interval = 120000
$timer.Add_Tick({{ $form.Close() }})
$timer.Start()
{stall}
[Windows.Forms.Application]::Run($form)
"""

STALL_S = 25
# The window's UI thread computes for STALL_S seconds from its first second: it answers no message,
# as on a stalled runner (#114). A busy loop, because a CLR wait on an STA thread pumps sent messages.
STALL = f"""
$stall = New-Object Windows.Forms.Timer
$stall.Interval = 1000
$stall.Add_Tick({{ $stall.Stop(); $end = (Get-Date).AddSeconds({STALL_S}); while ((Get-Date) -lt $end) {{}} }})
$stall.Start()
"""


@pytest.fixture
def window(tmp_path):
    """A live window: yields ``(title, marker file)``."""
    yield from live_window(tmp_path, "")


@pytest.fixture
def stalling_window(tmp_path):
    """A live window whose UI thread stops answering after its first second, for STALL_S."""
    yield from live_window(tmp_path, STALL)


def live_window(tmp_path, stall: str):
    title = f"remoteos-uia-{uuid.uuid4().hex[:8]}"
    marker = tmp_path / "saved.txt"
    script = FORM.format(title=ps_literal(title), marker=ps_literal(str(marker)), stall=stall)
    child = subprocess.Popen(powershell_argv(script), stdin=subprocess.DEVNULL, **own_console())
    try:
        deadline = time.monotonic() + 60
        while not any(w.title == title for w in desktop.enumerate_windows()):
            assert time.monotonic() < deadline, "the test window never appeared"
            assert child.poll() is None, f"the test window's PowerShell exited with {child.returncode}"
            time.sleep(0.2)
        yield title, marker
    finally:
        kill_process_tree(child)


def tree_of(title: str):
    """The window's tree, once its controls are up and it answers.

    UI Automation's first start takes seconds, and a window started a moment ago on a stalled
    runner may not answer yet (#114): UI Automation gives up on it inside the budget and the
    worker lives on. An abandoned worker (a plain UiaTimeout) is never retried: that is the bug.
    """
    deadline = time.monotonic() + 60
    while True:
        try:
            tree = desktop.element_tree(title)
        except (uia.UiaNotAsked, uia.UiaUnanswered):
            tree = None
        if tree is not None and len(tree.items) >= 2:
            return tree
        assert time.monotonic() < deadline, f"no controls in the tree: {tree}"
        time.sleep(0.2)


def item(tree, name: str):
    return next(i for i in tree.items if i.name == name)


def saved(marker: Path) -> str:
    deadline = time.monotonic() + 10
    while not marker.exists() or not marker.stat().st_size:
        assert time.monotonic() < deadline, "the button's click never ran"
        time.sleep(0.1)
    return marker.read_text(encoding="utf-8-sig")


def test_a_real_window_lists_its_controls_with_roles_and_bounds(window):
    title, _ = window
    tree = tree_of(title)
    assert tree.title == title
    button, field = item(tree, "Save"), item(tree, "Field")
    assert (button.role, button.enabled) == ("Button", True)
    assert field.role == "Edit"
    assert field.rect[2] > field.rect[0] and field.rect[3] > field.rect[1]
    assert button.rect[1] > field.rect[1], "the button sits below the field"


def not_answering(hwnd: int) -> bool:
    try:
        win32gui.SendMessageTimeout(hwnd, 0, 0, 0, 0x0002, 500)  # WM_NULL, SMTO_ABORTIFHUNG
    except pywintypes.error:
        return True
    return False


def test_a_window_that_does_not_answer_is_a_timeout_and_the_worker_lives_on(stalling_window):
    title, _ = stalling_window
    hwnd = next(w.handle for w in desktop.enumerate_windows() if w.title == title)
    uia._worker.call(lambda session: None, 60)  # a started worker: the timeout below is the window's
    worker, stuck = uia._worker._current, len(uia._worker._stuck)
    deadline = time.monotonic() + 15
    while not not_answering(hwnd):
        assert time.monotonic() < deadline, "the window's UI thread never stalled"
        time.sleep(0.2)
    started = time.monotonic()
    with pytest.raises(uia.UiaUnanswered, match="did not answer"):
        desktop.element_tree(title)
    assert time.monotonic() - started < uia.TREE_TIMEOUT_S, "UI Automation itself must give up inside the budget"
    assert (uia._worker._current, len(uia._worker._stuck)) == (worker, stuck), "the worker was abandoned"
    assert item(tree_of(title), "Save").role == "Button"  # the same worker, once the window answers again
    assert uia._worker._current is worker


def test_type_then_click_reach_the_real_controls(window):
    title, marker = window
    tree = tree_of(title)
    assert "Set the value" in desktop.type_element(item(tree, "Field").ref, SLOVAK)
    assert "Invoked" in desktop.click_element(item(tree, "Save").ref)
    assert saved(marker) == SLOVAK


def test_the_mouse_fallback_finds_the_centre_of_an_uncovered_control(window):
    title, _ = window
    tree = tree_of(title)
    button = item(tree, "Save")

    def center(session):
        return session._center(button.ref, session._live(button.ref))

    x, y = uia._worker.call(center, 10)
    left, top, right, bottom = button.rect
    assert left <= x < right and top <= y < bottom


def test_a_ref_of_an_older_tree_is_refused(window):
    title, _ = window
    old = tree_of(title)
    tree_of(title)
    with pytest.raises(StaleRef, match="call ElementTree again"):
        desktop.click_element(item(old, "Save").ref)


def test_a_snapshot_list_reads_the_window_and_ends_no_ref(window):
    title, marker = window
    tree = tree_of(title)
    hwnd = next(w.handle for w in desktop.enumerate_windows() if w.title == title)
    assert "Save" in [e["text"] for e in uia.read_elements(hwnd, 50)]
    desktop.type_element(item(tree, "Field").ref, "x")
    desktop.click_element(item(tree, "Save").ref)
    assert saved(marker) == "x"


def test_a_closed_window_s_element_is_gone_not_clicked(window):
    title, _ = window
    tree = tree_of(title)
    hwnd = next(w.handle for w in desktop.enumerate_windows() if w.title == title)
    subprocess.run(
        ["taskkill", "/F", "/PID", str(next(w.pid for w in desktop.enumerate_windows() if w.handle == hwnd))],
        check=True,
        capture_output=True,
        **own_console(),
    )
    deadline = time.monotonic() + 15
    while any(w.title == title for w in desktop.enumerate_windows()):
        assert time.monotonic() < deadline, "the window did not close"
        time.sleep(0.1)
    with pytest.raises(StaleRef, match="gone: call ElementTree again"):
        desktop.click_element(item(tree, "Save").ref)

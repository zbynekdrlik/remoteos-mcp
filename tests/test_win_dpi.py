"""Windows DPI awareness (#28).

pyautogui's import calls ``SetProcessDPIAware()`` (system-aware). Once that has
run, the process can no longer become per-monitor aware, so on a mixed-DPI
setup screenshots (physical pixels), window rects and ``SetCursorPos`` disagree.
Per-monitor-v2 awareness must therefore be requested BEFORE pyautogui is
imported — in ``__main__.py`` and in the Windows provider. These checks run on
any OS: the import order is checked on the source AST, and the fallback chain
runs against a fake ``windll`` (the Win32 API is the external boundary).
"""

from __future__ import annotations

import ast
import ctypes
from pathlib import Path

import pytest

from remoteos.platform.win import dpi

_SRC = Path(__file__).resolve().parents[1] / "src" / "remoteos"


def _first_line(tree: ast.AST, pred) -> int:
    lines = [n.lineno for n in ast.walk(tree) if pred(n)]
    assert lines, "expected node not found"
    return min(lines)


def _imports_pyautogui(node: ast.AST) -> bool:
    return isinstance(node, ast.Import) and any(a.name == "pyautogui" for a in node.names)


def _calls_enable_dpi(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
    return name == "enable_per_monitor_dpi_awareness"


@pytest.mark.parametrize("rel", ["__main__.py", "platform/win/desktop.py"])
def test_dpi_awareness_is_set_before_pyautogui_import(rel):
    tree = ast.parse((_SRC / rel).read_text(encoding="utf-8"))
    assert _first_line(tree, _calls_enable_dpi) < _first_line(tree, _imports_pyautogui)


class _Fn:
    def __init__(self, result):
        self.result = result
        self.args: list = []

    def __call__(self, *args):
        self.args.append(args)
        return self.result


class _Lib:
    pass


def _windll(ctx_result=1, shcore_result=0, system_result=1, has_ctx=True):
    lib = _Lib()
    lib.user32 = _Lib()
    if has_ctx:
        lib.user32.SetProcessDpiAwarenessContext = _Fn(ctx_result)
    lib.user32.SetProcessDPIAware = _Fn(system_result)
    lib.shcore = _Lib()
    lib.shcore.SetProcessDpiAwareness = _Fn(shcore_result)
    return lib


def test_per_monitor_v2_is_preferred():
    lib = _windll()
    assert dpi.enable_per_monitor_dpi_awareness(windll=lib) == "per-monitor-v2"
    (args,) = lib.user32.SetProcessDpiAwarenessContext.args
    assert args[0].value & 0xFFFFFFFF == 0xFFFFFFFC  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == -4
    assert lib.shcore.SetProcessDpiAwareness.args == []
    assert lib.user32.SetProcessDPIAware.args == []


def test_falls_back_to_per_monitor_v1():
    lib = _windll(ctx_result=0)
    assert dpi.enable_per_monitor_dpi_awareness(windll=lib) == "per-monitor"
    assert lib.shcore.SetProcessDpiAwareness.args == [(2,)]


def test_falls_back_when_context_api_missing():
    lib = _windll(has_ctx=False)
    assert dpi.enable_per_monitor_dpi_awareness(windll=lib) == "per-monitor"


def test_falls_back_to_system_aware_last():
    lib = _windll(ctx_result=0, shcore_result=-2147024891)  # E_ACCESSDENIED
    assert dpi.enable_per_monitor_dpi_awareness(windll=lib) == "system"


def test_reports_unchanged_when_everything_fails():
    lib = _windll(ctx_result=0, shcore_result=-2147024891, system_result=0)
    assert dpi.enable_per_monitor_dpi_awareness(windll=lib) == "unchanged"


_CTX = 0x7FFF0011  # an opaque DPI_AWARENESS_CONTEXT handle


def _with_read_back(lib, awareness: int, is_v2: int | None = None):
    """Add the Win10 1607+ read-back API, reporting process awareness ``awareness``.

    ``is_v2`` (when given) is what ``AreDpiAwarenessContextsEqual(ctx, V2)`` returns.
    """
    lib.user32.GetThreadDpiAwarenessContext = _Fn(_CTX)
    lib.user32.GetAwarenessFromDpiAwarenessContext = _Fn(awareness)
    if is_v2 is not None:
        lib.user32.AreDpiAwarenessContextsEqual = _Fn(is_v2)
    return lib


def test_reports_actual_awareness_when_already_set():
    # e.g. an application manifest made the process per-monitor aware before we ran:
    # every set call is refused or a no-op, so report what the process really IS.
    lib = _with_read_back(_windll(ctx_result=0, shcore_result=-2147024891, system_result=1), awareness=2)
    assert dpi.enable_per_monitor_dpi_awareness(windll=lib) == "per-monitor"
    # the context is a HANDLE: pointer-sized both ways, and handed on unchanged
    assert lib.user32.GetThreadDpiAwarenessContext.restype is ctypes.c_void_p
    assert lib.user32.GetAwarenessFromDpiAwarenessContext.argtypes == [ctypes.c_void_p]
    assert lib.user32.GetAwarenessFromDpiAwarenessContext.args == [(_CTX,)]


def test_reports_preset_per_monitor_v2():
    # a manifest already made the process per-monitor-v2: the read-back must say v2,
    # not v1 (DPI_AWARENESS alone cannot tell them apart).
    lib = _with_read_back(_windll(ctx_result=0, shcore_result=-2147024891, system_result=1), awareness=2, is_v2=1)
    assert dpi.enable_per_monitor_dpi_awareness(windll=lib) == "per-monitor-v2"
    ((ctx, v2),) = lib.user32.AreDpiAwarenessContextsEqual.args
    assert ctx == _CTX
    # both handles pointer-sized: an int-typed -4 would reach Win64 as a 32-bit value
    assert lib.user32.AreDpiAwarenessContextsEqual.argtypes == [ctypes.c_void_p, ctypes.c_void_p]
    v2 = v2.value if isinstance(v2, ctypes.c_void_p) else ctypes.c_void_p(v2).value
    assert v2 == ctypes.c_void_p(-4).value  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2, sign-extended


def test_reports_per_monitor_v1_when_contexts_differ():
    lib = _with_read_back(_windll(ctx_result=0, shcore_result=0), awareness=2, is_v2=0)
    assert dpi.enable_per_monitor_dpi_awareness(windll=lib) == "per-monitor"


def test_reports_unaware_from_read_back():
    lib = _with_read_back(_windll(ctx_result=0, shcore_result=-2147024891, system_result=0), awareness=0)
    assert dpi.enable_per_monitor_dpi_awareness(windll=lib) == "unaware"


def test_reports_system_aware_from_read_back():
    lib = _with_read_back(_windll(ctx_result=0, shcore_result=-2147024891, system_result=0), awareness=1)
    assert dpi.enable_per_monitor_dpi_awareness(windll=lib) == "system"


def test_v2_success_does_not_need_read_back():
    lib = _with_read_back(_windll(), awareness=2)
    assert dpi.enable_per_monitor_dpi_awareness(windll=lib) == "per-monitor-v2"
    assert lib.user32.GetAwarenessFromDpiAwarenessContext.args == []


def test_unavailable_off_windows(monkeypatch):
    # No ctypes.windll on Linux/macOS → nothing to set, never raises.
    monkeypatch.delattr(ctypes, "windll", raising=False)
    monkeypatch.setattr(dpi, "_MODE", None)
    assert dpi.enable_per_monitor_dpi_awareness() == "unavailable"

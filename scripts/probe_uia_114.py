"""TEMPORARY probe for #114 (removed before merge): where does the first UIA walk of a new window spend its time?

Runs the CI test's sequence (a WinForms window in a PowerShell child, then ElementTree until its
controls are up) with every UIA step timed, and prints one line per step. Also asks the window's
thread with SendMessageTimeout(WM_NULL) whether it pumps messages before each call.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import threading
import time
import uuid
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests_windows"))

from test_uia_windows import FORM  # noqa: E402

from remoteos.platform.command import kill_process_tree, own_console, powershell_argv  # noqa: E402
from remoteos.platform.win import desktop, uia  # noqa: E402
from remoteos.platform.win.psquote import ps_literal  # noqa: E402

T0 = time.monotonic()
SLOW = 0.3


def say(text: str) -> None:
    print(f"[{time.monotonic() - T0:7.3f}] {threading.current_thread().name}: {text}", flush=True)


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.SendMessageTimeoutW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
    wintypes.UINT,
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_size_t),
]
_user32.SendMessageTimeoutW.restype = wintypes.LPARAM


def pumps(hwnd: int, ms: int = 5000) -> str:
    out = ctypes.c_size_t()
    start = time.monotonic()
    ok = _user32.SendMessageTimeoutW(hwnd, 0, 0, 0, 0x0002, ms, ctypes.byref(out))  # WM_NULL, SMTO_ABORTIFHUNG
    return f"{'pumps' if ok else 'NOT pumping'} after {time.monotonic() - start:.3f}s"


def timed(name, fn, slow=SLOW):
    def wrapper(*a, **k):
        start = time.monotonic()
        try:
            return fn(*a, **k)
        finally:
            took = time.monotonic() - start
            if took >= slow:
                say(f"SLOW {name} {took:.3f}s")

    return wrapper


def instrument() -> None:
    real_open = uia.open_session

    def open_session(abandoned):
        start = time.monotonic()
        session = real_open(abandoned)
        say(f"open_session {time.monotonic() - start:.3f}s")
        session.auto.ControlFromHandle = timed("ControlFromHandle", session.auto.ControlFromHandle, 0)
        return session

    uia._worker = uia.UiaWorker(open_session)
    uia.Session._facts = timed("_facts", uia.Session._facts)
    uia.Session._entry = timed("_entry", uia.Session._entry)
    real_children = uia.Session._children

    def children(self, control):
        it = real_children(self, control)
        while True:
            start = time.monotonic()
            try:
                child = next(it)
            except StopIteration:
                took = time.monotonic() - start
                if took >= SLOW:
                    say(f"SLOW children-end {took:.3f}s")
                return
            took = time.monotonic() - start
            if took >= SLOW:
                say(f"SLOW children-next {took:.3f}s")
            yield child

    uia.Session._children = children
    real_tree = uia.Session.tree

    def tree(self, hwnd):
        start = time.monotonic()
        say("job begins")
        try:
            return real_tree(self, hwnd)
        finally:
            say(f"job ends {time.monotonic() - start:.3f}s")

    uia.Session.tree = tree


def run_once() -> None:
    title = f"remoteos-uia-{uuid.uuid4().hex[:8]}"
    marker = Path.cwd() / f"probe-{title}.txt"
    script = FORM.format(title=ps_literal(title), marker=ps_literal(str(marker)))
    say("starting the window")
    child = subprocess.Popen(powershell_argv(script), stdin=subprocess.DEVNULL, **own_console())
    try:
        while True:
            hits = [w for w in desktop.enumerate_windows() if w.title == title]
            if hits:
                break
            time.sleep(0.2)
        hwnd = hits[0].handle
        say(f"window appeared, {pumps(hwnd)}")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            start = time.monotonic()
            say(f"read_tree call, window {pumps(hwnd, 200)}")
            try:
                tree = desktop.element_tree(title)
                say(f"read_tree -> {len(tree.items)} items in {time.monotonic() - start:.3f}s")
                if len(tree.items) >= 2:
                    print("RESULT ok", flush=True)
                    return
            except uia.UiaNotAsked as e:
                say(f"read_tree NotAsked after {time.monotonic() - start:.3f}s: {e}")
            except uia.UiaTimeout as e:
                say(f"read_tree TIMEOUT after {time.monotonic() - start:.3f}s: {e}; window {pumps(hwnd)}")
                print("RESULT timeout", flush=True)
                return
            time.sleep(0.2)
        print("RESULT never", flush=True)
    finally:
        kill_process_tree(child)


def stress(n: int) -> list:
    """``n`` busy processes: a starved runner, as a shared CI host can be."""
    burn = "while True: pass"
    return [subprocess.Popen([sys.executable, "-c", burn], **own_console()) for _ in range(n)]


if __name__ == "__main__":
    load = stress(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
    say(f"{len(load)} busy processes")
    instrument()
    try:
        run_once()
    finally:
        for p in load:
            p.kill()

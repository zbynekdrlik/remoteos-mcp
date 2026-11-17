"""TEMPORARY probe for #114 (removed before merge): does UIA's own TransactionTimeout bound a hung window?

A WinForms window whose UI thread sleeps 40 s right after it is shown. The tree is read once the
thread is asleep, in a fresh process (first contact), with the client's timeouts as given.
"""

from __future__ import annotations

import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests_windows"))

from test_uia_windows import FORM  # noqa: E402

from remoteos.platform.command import kill_process_tree, own_console, powershell_argv  # noqa: E402
from remoteos.platform.win import desktop, uia  # noqa: E402
from remoteos.platform.win.psquote import ps_literal  # noqa: E402

T0 = time.monotonic()
HANG = "$form.Add_Shown({ $form.Activate(); $form.BeginInvoke([Action]{ Start-Sleep -Seconds 40 }) | Out-Null })"


def pumps(hwnd: int) -> bool:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendMessageTimeoutW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    user32.SendMessageTimeoutW.restype = wintypes.LPARAM
    out = ctypes.c_size_t()
    return bool(user32.SendMessageTimeoutW(hwnd, 0, 0, 0, 0x0002, 1000, ctypes.byref(out)))


def say(text: str) -> None:
    print(f"[{time.monotonic() - T0:7.3f}] {text}", flush=True)


def main(transaction_ms: int, connection_ms: int, warm: bool) -> None:
    real_open = uia.open_session

    def open_session(abandoned):
        session = real_open(abandoned)
        client = session.auto._AutomationClient.instance()
        if transaction_ms:
            import comtypes.client

            core = client.UIAutomationCore
            eight = comtypes.client.CreateObject(
                "{e22ad333-b25f-460c-83d0-0581107395c9}", interface=core.IUIAutomation2
            )
            client.IUIAutomation = eight
            client.ViewWalker = eight.RawViewWalker
            two = eight
            say(f"defaults: transaction {two.TransactionTimeout} ms, connection {two.ConnectionTimeout} ms")
            two.TransactionTimeout = transaction_ms
            two.ConnectionTimeout = connection_ms
            say(f"set: transaction {two.TransactionTimeout} ms, connection {two.ConnectionTimeout} ms")
        return session

    uia._worker = uia.UiaWorker(open_session)
    title = f"remoteos-uia-{uuid.uuid4().hex[:8]}"
    script = FORM.format(title=ps_literal(title), marker=ps_literal(str(Path.cwd() / "x.txt")))
    script = script.replace("$form.Add_Shown({ $form.Activate() })", HANG if not warm else "")
    child = subprocess.Popen(powershell_argv(script), stdin=subprocess.DEVNULL, **own_console())
    try:
        while not any(w.title == title for w in desktop.enumerate_windows()):
            time.sleep(0.2)
        say("window appeared")
        if warm:
            tree = desktop.element_tree(title)
            say(f"warm read: {len(tree.items)} items")
            return
        time.sleep(3)
        hwnd = next(w.handle for w in desktop.enumerate_windows() if w.title == title)
        say(f"window thread hung: {not pumps(hwnd)}")
        for attempt in range(3):
            start = time.monotonic()
            try:
                tree = desktop.element_tree(title)
                say(f"attempt {attempt}: {len(tree.items)} items {tree.truncated!r} in {time.monotonic() - start:.3f}s")
            except Exception as e:  # the probe reports every outcome
                say(f"attempt {attempt}: {type(e).__name__}: {e} in {time.monotonic() - start:.3f}s")
            say(f"stuck workers: {len(uia._worker._stuck)}")
    finally:
        kill_process_tree(child)


if __name__ == "__main__":
    main(int(sys.argv[1]), int(sys.argv[2]), False)

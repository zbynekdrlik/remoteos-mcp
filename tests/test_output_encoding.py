"""A Windows child's output reaches the client as exact Unicode (#48).

Live in the #32 verification, ``Shell('"Žltý kôň"')`` came back ``Zlty kôn`` on resolume-pp
(en_150, OEM cp437). PowerShell writes its output through the child console's OEM page, so the
characters that page cannot hold were best-fitted away before Python decoded anything.

Only the OS boundary is faked:
- ``windows_child`` (conftest): a real Python child that writes the given bytes, started through
  the real ``subprocess``;
- Python's Windows-only ``oem`` codec, registered as a named code page.
Expected values are literals, never imported from the code under test.
"""

from __future__ import annotations

import remoteos.__main__ as server

SLOVAK = "Žltý kôň ďakuje – €"


def test_windows_shell_returns_powershell_utf8_output_exactly(monkeypatch, windows_child):
    # the child writes UTF-8 (its console was switched by the prefix); read through the OEM page
    # (cp437 on resolume-pp) the same bytes came back as "┼╜lt├╜ k├┤┼ê …"
    monkeypatch.setattr(server, "is_windows", lambda: True)
    windows_child.stdout = (SLOVAK + "\r\n").encode("utf-8")
    assert server.Shell(f'"{SLOVAK}"') == SLOVAK

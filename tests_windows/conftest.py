"""Real Windows PowerShell 5.1, no fakes (#48 round 2): this directory runs only on Windows.

It is the windows-latest CI job (`pytest tests_windows`). The Linux job never collects it (pytest's
`testpaths` is `tests`), so nothing is skipped anywhere. Started anywhere else it refuses to run.
"""

from __future__ import annotations

import sys

import pytest


def pytest_configure(config):
    if sys.platform != "win32":
        raise pytest.UsageError("tests_windows runs real Windows PowerShell 5.1: only on Windows (the windows CI job)")

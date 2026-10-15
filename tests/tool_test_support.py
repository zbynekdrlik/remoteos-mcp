"""Helpers shared by the tool and provider tests on every OS (#32 review round 8).

- ``tool_error``: the text of the ``ToolError`` a failing tool raises.
- ``purge_module``: forget an imported provider module, so the next import runs
  against the current fakes (the Windows, macOS and Linux provider fixtures).
"""

from __future__ import annotations

import sys

import pytest
from fastmcp.exceptions import ToolError


def tool_error(call) -> str:
    """The text of the ToolError a failing tool raises (#32: a failure is never a success string)."""
    with pytest.raises(ToolError) as exc:
        call()
    return str(exc.value)


def purge_module(name: str) -> None:
    """Forget an imported provider module, so the next import runs against the current fakes."""
    sys.modules.pop(name, None)
    parent, _, child = name.rpartition(".")
    if parent in sys.modules and hasattr(sys.modules[parent], child):
        delattr(sys.modules[parent], child)

"""Linux system stubs — Windows registry operations are not available on Linux.

RegRead/RegWrite are never registered on Linux (``tiers.LINUX_ALWAYS_EXCLUDED_TOOLS``);
a direct call still fails as an error, never as a successful answer (#32).
"""

from __future__ import annotations


def reg_read(key: str, value_name: str) -> str:
    """Not available on Linux."""
    raise NotImplementedError("RegRead is not available on Linux. Use 'Shell' to read config files instead.")


def reg_write(key: str, value_name: str, data: str, reg_type: str = "string") -> str:
    """Not available on Linux."""
    raise NotImplementedError("RegWrite is not available on Linux. Use 'Shell' to edit config files instead.")

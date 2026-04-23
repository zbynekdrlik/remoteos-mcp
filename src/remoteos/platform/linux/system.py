"""Linux system stubs — Windows registry operations are not available on Linux."""

from __future__ import annotations


def reg_read(key: str, value_name: str) -> str:
    """Not available on Linux."""
    return "RegRead is not available on Linux. Use 'Shell' to read config files instead."


def reg_write(key: str, value_name: str, data: str, reg_type: str = "string") -> str:
    """Not available on Linux."""
    return "RegWrite is not available on Linux. Use 'Shell' to edit config files instead."

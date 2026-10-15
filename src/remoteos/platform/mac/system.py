"""macOS system preferences via defaults command (replaces Windows registry)."""

from __future__ import annotations

from remoteos.platform.command import run_command as _run

_TYPE_FLAGS = {
    "reg_sz": "-string",
    "string": "-string",
    "reg_dword": "-int",
    "reg_qword": "-int",
    "int": "-int",
    "float": "-float",
    "bool": "-bool",
}


def reg_read(key: str, value_name: str) -> str:
    """Read a macOS defaults value. Key is the domain (e.g. com.apple.finder)."""
    return _run(["defaults", "read", key, value_name], check=True)  # a missing key is an error, as on Windows


def reg_write(key: str, value_name: str, data: str, reg_type: str = "string") -> str:
    """Write a macOS defaults value."""
    type_flag = _TYPE_FLAGS.get(reg_type.lower())
    if type_flag is None:
        raise ValueError(f"Unknown type '{reg_type}'. Use: {', '.join(_TYPE_FLAGS.keys())}")
    result = _run(["defaults", "write", key, value_name, type_flag, str(data)], check=True)
    return f"Written {value_name} = {data!r} to {key}\n{result}"

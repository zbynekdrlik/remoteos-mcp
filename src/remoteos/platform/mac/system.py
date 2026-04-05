"""macOS system preferences via defaults command (replaces Windows registry)."""

from __future__ import annotations

import subprocess


def _run(cmd: list[str], timeout: int = 30) -> str:
    """Run a command and return stdout+stderr."""
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    output = result.stdout
    if result.stderr:
        output += f"\n[STDERR] {result.stderr}"
    if result.returncode != 0:
        output += f"\n[Exit code: {result.returncode}]"
    return output.strip() or "(no output)"


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
    try:
        return _run(["defaults", "read", key, value_name])
    except Exception as e:
        return f"RegRead error: {e}"


def reg_write(key: str, value_name: str, data: str, reg_type: str = "string") -> str:
    """Write a macOS defaults value."""
    try:
        type_flag = _TYPE_FLAGS.get(reg_type.lower())
        if type_flag is None:
            return f"Error: Unknown type '{reg_type}'. Use: {', '.join(_TYPE_FLAGS.keys())}"
        result = _run(["defaults", "write", key, value_name, type_flag, str(data)])
        return f"Written {value_name} = {data!r} to {key}\n{result}"
    except Exception as e:
        return f"RegWrite error: {e}"

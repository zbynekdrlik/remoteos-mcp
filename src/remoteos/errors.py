"""Tool failures as real MCP errors (#32): the error type and the tool decorator.

A failing tool RAISES and never returns error text as a successful result.
FastMCP 4.0.3 turns a raised ``ToolError`` into ``CallToolResult(isError=True)``
whose only text block is the error message. ``mask_error_details`` never hides
a ``ToolError``: it masks only other exceptions (``fastmcp/server/server.py``
``call_tool``). So the message is what the model reads, and it has to say what
is wrong and what to do.

- :func:`reports_errors` decorates every tool. It maps any exception to
  ``ToolError("<Tool> error: <reason>")``, the text the tools always used, and
  passes a ``ToolError`` through unchanged.
- :class:`DesktopUnavailable` is a diagnosis: the screen cannot be seen or
  driven. Its message is complete and reaches the client verbatim, without the
  tool prefix. The Linux headless/Wayland stub texts (the cam1..4 contract)
  travel this way.

Pure: no OS calls.
"""

from __future__ import annotations

import functools
import logging
import traceback
from typing import Callable, Optional

from fastmcp.exceptions import ToolError

__all__ = ["DesktopUnavailable", "ToolError", "reports_errors", "stack_frames", "tool_error"]

log = logging.getLogger("remoteos.errors")


class DesktopUnavailable(RuntimeError):
    """The desktop cannot be seen or driven right now (no display, locked, no session, no permission).

    The message is the whole diagnosis, actionable on its own, and reaches the
    client as is. A ``RuntimeError``, so a provider's existing ``except
    RuntimeError`` callers keep working.
    """


def stack_frames(exc: BaseException) -> str:
    """``exc``'s traceback for the server log: the frames and the exception TYPE, never its message.

    An error message can echo a tool argument (a path, a window title, a process
    name), and logs never carry tool arguments or replies (#31). The client gets
    the message; the log gets where it broke.
    """
    frames = "".join(traceback.format_tb(exc.__traceback__))
    return f"Traceback (most recent call last):\n{frames}{type(exc).__qualname__} (message not logged)"


def tool_error(tool: str, exc: BaseException) -> ToolError:
    """The ``ToolError`` a failure of ``tool`` reaches the client as.

    A ``ToolError`` is returned unchanged, and a :class:`DesktopUnavailable`
    keeps its whole message. Anything else becomes ``"<tool> error: <exc>"``.
    All of them are expected failures, logged by FastMCP at WARNING, not ERROR.
    """
    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, DesktopUnavailable):
        return ToolError(str(exc), log_level=logging.WARNING)
    return ToolError(f"{tool} error: {exc}", log_level=logging.WARNING)


def reports_errors(fn: Optional[Callable] = None, *, preflight: Optional[Callable[[], None]] = None):
    """Decorate a tool so that every failure reaches the client as an MCP error (``isError``).

    ``@reports_errors`` or ``@reports_errors(preflight=check)``. ``check()``
    runs before the tool body and raises (typically :class:`DesktopUnavailable`)
    when the tool cannot work. The error carries the tool's name
    (``fn.__name__``), which is the MCP tool name. ``functools.wraps`` keeps the
    signature and docstring, so FastMCP builds the same schema as for the bare
    function.
    """

    def decorate(func: Callable) -> Callable:
        name = func.__name__

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                if preflight is not None:
                    preflight()
                return func(*args, **kwargs)
            except ToolError:
                raise
            except DesktopUnavailable as e:
                log.info("%s: desktop unavailable: %s", name, e)
                raise tool_error(name, e) from e
            except Exception as e:
                # a helper's failure, or a bug: where it broke goes to the log, the message to the client
                log.warning("%s failed: %s\n%s", name, type(e).__name__, stack_frames(e))
                raise tool_error(name, e) from e

        return wrapper

    return decorate(fn) if fn is not None else decorate

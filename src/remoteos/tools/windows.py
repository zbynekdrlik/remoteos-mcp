"""Window management tools: FocusWindow, MinimizeAll, App."""

from __future__ import annotations

try:
    from mcp.types import ToolAnnotations
except ImportError:
    from fastmcp.tools import ToolAnnotations

from remoteos.app import _input_tool, _require_win32, mcp
from remoteos.errors import failure, reports_errors
from remoteos.platform import get_desktop


@mcp.tool(
    annotations=ToolAnnotations(
        title="FocusWindow",
        destructiveHint=False,
        openWorldHint=False,
    )
)
@reports_errors
def FocusWindow(title: str = "", handle: int = 0) -> str:
    """Bring a window to the foreground.

    Args:
        title: Window title (fuzzy matched).
        handle: Window handle (exact).
    """
    _require_win32("FocusWindow")
    return get_desktop().focus_window(title=title or None, handle=handle or None)


@mcp.tool(
    annotations=ToolAnnotations(
        title="MinimizeAll",
        destructiveHint=False,
        openWorldHint=False,
    )
)
@_input_tool
def MinimizeAll() -> str:
    """Minimize all windows (Win+D — show desktop)."""
    return get_desktop().minimize_all()


@mcp.tool(
    annotations=ToolAnnotations(
        title="App",
        destructiveHint=False,
        openWorldHint=True,
    )
)
@reports_errors
def App(
    action: str = "launch",
    name: str = "",
    args: str = "",
    handle: int = 0,
    width: int = 0,
    height: int = 0,
) -> str:
    """Launch, switch to, or resize an application.

    Args:
        action: 'launch', 'switch', or 'resize'.
        name: Application name or path (for launch/switch).
        args: Arguments (for launch).
        handle: Window handle (for resize/switch).
        width: New width (for resize), in raw screen units (the coords="screen" space:
            physical pixels on Windows/Linux, points on macOS) — not Snapshot image pixels.
        height: New height (for resize), in the same raw screen units as width.
    """
    if action == "launch":
        return get_desktop().launch_app(name, args)
    elif action == "switch":
        _require_win32("App(switch)")
        return get_desktop().focus_window(title=name or None, handle=handle or None)
    elif action == "resize":
        _require_win32("App(resize)")
        if not handle:
            raise failure("resize requires a window handle")
        return get_desktop().resize_window(handle, width, height)
    raise failure(f"Unknown action: {action}")

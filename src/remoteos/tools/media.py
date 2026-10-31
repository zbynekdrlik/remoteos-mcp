"""Screen reading tools: OCR, ScreenRecord, AnnotatedSnapshot."""

from __future__ import annotations

from mcp.types import ImageContent, TextContent

from remoteos import ocr, recording
from remoteos.app import ToolAnnotations, _capture_snapshot_frame, _capture_tool, mcp
from remoteos.coords import contract_line
from remoteos.platform import get_desktop, is_linux
from remoteos.snapshot import draw_annotations, encode_jpeg

# ============================== OCR ========================================


@mcp.tool(
    annotations=ToolAnnotations(
        title="OCR",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
@_capture_tool
def OCR(
    left: int = 0,
    top: int = 0,
    right: int = 0,
    bottom: int = 0,
    lang: str = "eng",
) -> str:
    """Extract text from screen using OCR. Captures a region or the full screen.

    Uses pytesseract if available, falls back to platform-specific OCR engine.

    The region is in SCREEN coordinates (physical pixels on Windows/Linux, points
    on macOS) — NOT Snapshot image pixels.

    Args:
        left: Left edge of region (0 = full screen).
        top: Top edge of region.
        right: Right edge of region.
        bottom: Bottom edge of region.
        lang: OCR language for pytesseract (default 'eng').
    """
    region = {}
    if left or top or right or bottom:
        region = {"left": left, "top": top, "right": right, "bottom": bottom}
    text = ocr.run_ocr(**region, lang=lang) if region else ocr.run_ocr(lang=lang)
    if not text:
        return "(no text detected)"
    return text


# ========================== SCREEN RECORDING ===============================


@mcp.tool(
    annotations=ToolAnnotations(
        title="ScreenRecord",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
@_capture_tool
def ScreenRecord(
    duration: float = 3.0,
    fps: int = 5,
    left: int = 0,
    top: int = 0,
    right: int = 0,
    bottom: int = 0,
    max_width: int = 800,
) -> list:
    """Record the screen and return an animated GIF.

    The region is in SCREEN coordinates (physical pixels on Windows/Linux, points
    on macOS) — NOT Snapshot image pixels.

    Args:
        duration: Recording length in seconds (default 3, max 10).
        fps: Frames per second (default 5, max 10).
        left: Left edge of capture region (0 = full screen).
        top: Top edge of capture region.
        right: Right edge of capture region.
        bottom: Bottom edge of capture region.
        max_width: Max width of output GIF (default 800).
    """
    region = {}
    if left or top or right or bottom:
        region = {"left": left, "top": top, "right": right, "bottom": bottom}
    if is_linux():
        # Linux path: ffmpeg x11grab in the resolved session (issue #8). Same
        # base64-GIF return shape as recording.record_screen, so the content
        # below is identical to the Windows/macOS path.
        b64 = get_desktop().record_screen(duration=duration, fps=fps, max_width=max_width, **region)
    else:
        b64 = recording.record_screen(duration=duration, fps=fps, max_width=max_width, **region)
    return [
        ImageContent(type="image", data=b64, mimeType="image/gif"),
        TextContent(
            type="text",
            text=f"Recorded {duration}s at {fps}fps ({len(b64) * 3 // 4 // 1024}KB GIF)",
        ),
    ]


# ======================== ANNOTATED SNAPSHOT ===============================


@mcp.tool(
    annotations=ToolAnnotations(
        title="AnnotatedSnapshot",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
@_capture_tool
def AnnotatedSnapshot(
    max_elements: int = 30,
    quality: int = 65,
    max_width: int = 1280,
    monitor: int = 0,
) -> list:
    """Take a screenshot with numbered labels on interactive UI elements.

    Draws red rectangles and white numbered labels on each interactive element,
    making it easy for AI agents to identify click targets visually. Same frame and
    coordinate contract as Snapshot: the text starts with the contract line, element
    centers are IMAGE pixels, and Click / Move / Scroll / Type take them as-is.

    Uses the platform accessibility tree for the foreground window (UI Automation
    on Windows, AT-SPI on Linux X11); returns the plain screenshot with a
    note when the focused app exposes no elements.

    Args:
        max_elements: Maximum number of elements to annotate (default 30).
        quality: JPEG quality 1-100 (default 65).
        max_width: Max image width in pixels. 0=native resolution. Default 1280 (same as Snapshot).
        monitor: Monitor to capture. 0=all monitors (default; on macOS the main display), 1/2/3=specific monitor.
    """
    img, frame = _capture_snapshot_frame(monitor, max_width)
    coordinates = f"**Coordinates:** {contract_line(frame)}"

    elements = get_desktop().get_interactive_elements(limit=max_elements)
    if not elements:
        note = "No interactive elements found"
        if is_linux():
            note += " (AT-SPI exposed none for the focused app)"
        return [
            ImageContent(type="image", data=encode_jpeg(img, quality), mimeType="image/jpeg"),
            TextContent(type="text", text=f"{coordinates}\n\n{note}."),
        ]

    annotated = draw_annotations(img, elements, frame, max_elements)
    text_summary = f"{coordinates}\n\n**Annotated {len(annotated)} elements:**\n" + "\n".join(annotated)
    return [
        ImageContent(type="image", data=encode_jpeg(img, quality), mimeType="image/jpeg"),
        TextContent(type="text", text=text_summary),
    ]

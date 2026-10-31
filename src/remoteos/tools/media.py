"""Screen reading tools: OCR, ScreenRecord, AnnotatedSnapshot."""

from __future__ import annotations

import logging

from mcp.types import ImageContent, TextContent

from remoteos import ocr, recording
from remoteos.app import (
    ToolAnnotations,
    _capture_snapshot_frame,
    _capture_tool,
    _capture_with_retry,
    mcp,
)
from remoteos.coords import contract_line
from remoteos.marks import MAX_MARKS, Word, merge
from remoteos.marks import OCR as OCR_SOURCE
from remoteos.platform import get_desktop, is_linux
from remoteos.snapshot import draw_marks, encode_jpeg

log = logging.getLogger("remoteos.tools.media")

_MAX_REASON = 500  # several engines' errors in a row: the last one is the one that counts

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
    lang: str = ocr.DEFAULT_LANG,
) -> str:
    """Extract text from screen using OCR. Captures a region or the full screen.

    Uses pytesseract if available, falls back to platform-specific OCR engine. A language
    that is not installed is left out and the reply says so.

    The region is in SCREEN coordinates (physical pixels on Windows/Linux, points
    on macOS) — NOT Snapshot image pixels.

    Args:
        left: Left edge of region (0 = full screen).
        top: Top edge of region.
        right: Right edge of region.
        bottom: Bottom edge of region.
        lang: OCR languages joined by + (default 'slk+eng'). The Windows engine reads with the first one installed.
    """
    region = {}
    if left or top or right or bottom:
        region = {"left": left, "top": top, "right": right, "bottom": bottom}
    reading = ocr.read_text(**region, lang=lang)
    text = reading.text or "(no text detected)"
    return f"{text}\n\n[{reading.note}]" if reading.note else text


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
    max_elements: int = MAX_MARKS,
    quality: int = 65,
    max_width: int = 1280,
    monitor: int = 0,
) -> list:
    """Take a screenshot with one numbered set of marks: UI elements plus the text OCR read.

    Finds click targets in custom-drawn apps too (Ableton, Resolume, JUCE), which show
    little to accessibility. Elements of the foreground window (UI Automation on Windows,
    AT-SPI on Linux X11) are red; a line of OCR text (Slovak and English) no element
    covers is blue. One line per mark: [n] source role "name" at (x,y), numbered top-left
    to bottom-right. Same frame and coordinate contract as Snapshot: the text starts with
    the contract line, the points are IMAGE pixels, and Click / Move / Scroll / Type take
    them as-is. On Windows an element's line also gives its ref for ClickElement and
    TypeElement; this starts a new ref tree, as ElementTree does. If OCR fails, the
    reply says so and keeps the image and the elements.

    Args:
        max_elements: Maximum number of marks, 1-300 (default 300). The reply says when more were found.
        quality: JPEG quality 1-100 (default 65).
        max_width: Max image width in pixels. 0=native resolution. Default 1280 (same as Snapshot).
        monitor: Monitor to capture. 0=all monitors (default; on macOS the main display), 1/2/3=specific monitor.
    """
    if not 1 <= max_elements <= MAX_MARKS:
        raise ValueError(f"max_elements must be 1..{MAX_MARKS}, got {max_elements}")
    img, frame = _capture_snapshot_frame(monitor, max_width)
    elements, cut = _marked_elements(max_elements)
    words, notes = _read_words(frame)
    found = merge(elements, words, max_elements)
    lines = draw_marks(img, found.marks, frame)
    log.debug("AnnotatedSnapshot: %d elements, %d words, %d marks", len(elements), len(words), len(lines))

    parts = [f"**Coordinates:** {contract_line(frame)}"]
    if lines:
        from_ocr = sum(mark.source == OCR_SOURCE for mark in found.marks)
        heading = f"**Marks:** {len(lines)} ({len(lines) - from_ocr} elements, {from_ocr} OCR)"
        parts.append("\n".join([heading, *lines]))
    else:
        parts.append("No interactive elements or text found.")
    if found.dropped:
        parts.append(f"**Incomplete:** {found.dropped} more not listed (limit {max_elements}).")
    if cut:
        parts.append(f"**Incomplete:** {cut}.")
    parts += [f"**OCR:** {note}" for note in notes]
    return [
        ImageContent(type="image", data=encode_jpeg(img, quality), mimeType="image/jpeg"),
        TextContent(type="text", text="\n\n".join(parts)),
    ]


def _marked_elements(limit: int) -> tuple[list[dict], str]:
    """The foreground window's elements and why the list is cut: refs where the OS has them (Windows)."""
    desktop = get_desktop()
    if hasattr(desktop, "marked_elements"):
        return desktop.marked_elements(limit=limit)
    elements = desktop.get_interactive_elements(limit=limit)
    return elements, f"the element list reached its limit of {limit}" if len(elements) >= limit else ""


def _read_words(frame) -> tuple[tuple[Word, ...], list[str]]:
    """``(words on the frame, notes)``. OCR failing costs the words, never the image."""
    region = (frame.x, frame.y, frame.x + frame.width, frame.y + frame.height)
    capture = get_desktop().capture_region
    try:
        reading = ocr.read_words(region, lambda bbox: _capture_with_retry(lambda: capture(bbox)))
    except Exception as e:
        log.warning("no OCR marks (%s)", type(e).__name__)
        reason = " ".join(str(e).split())
        return (), ["no text marks: " + (f"…{reason[-_MAX_REASON:]}" if len(reason) > _MAX_REASON else reason)]
    return reading.words, [reading.note] if reading.note else []

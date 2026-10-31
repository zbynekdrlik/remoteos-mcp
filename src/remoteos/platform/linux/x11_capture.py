"""Linux screen capture: scrot/import screenshots, the capture frame, ffmpeg x11grab recording."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Optional

from remoteos.coords import CaptureGeometry, frame_for
from remoteos.errors import DesktopUnavailable
from remoteos.platform.linux import session
from remoteos.platform.linux.display import query_geometry
from remoteos.platform.linux.session import _unavailable, get_session
from remoteos.snapshot import fit_to_frame

log = logging.getLogger("remoteos.platform.linux.desktop")  # one logger for the whole provider


# ---------------------------------------------------------------------------
# Screenshot
# ---------------------------------------------------------------------------


def _capture_tmp(suffix: str) -> Path:
    """Return a PRIVATE temp path for a screen capture (may hold on-screen secrets).

    Captures must NOT be created in world-readable ``/tmp`` (files land 0644 under
    the sticky dir, so a co-located local user can read a screenshot/recording
    before it is unlinked). The session user's ``XDG_RUNTIME_DIR``
    (``/run/user/<uid>``, mode 0700, owned by that user) is private AND writable by
    the session user even when the service runs as root and shells the capture out
    via ``sudo -u <user>`` — a root-owned 0700 temp dir would NOT be. ``_detect``
    always populates ``runtime_dir`` for an X11 session (the only mode these
    capture paths run in), so the ``/tmp`` fallback is a last resort for a
    malformed session only.
    """
    s = get_session()
    runtime_dir = s.runtime_dir
    name = f"remoteos-{uuid.uuid4().hex}{suffix}"
    if runtime_dir and Path(runtime_dir).is_dir():
        return Path(runtime_dir) / name
    return Path("/tmp") / name


def capture_png(bbox: Optional[tuple[int, int, int, int]] = None) -> bytes:
    """Capture the screen (or a region) as PNG bytes via scrot (fallback: import).

    ``bbox`` is ``(left, top, right, bottom)`` in screen coordinates, or ``None``
    for the full screen. Only valid on an X11 session — callers must gate on mode.
    """
    tmp = _capture_tmp(".png")
    scrot_cmd = ["scrot", "--overwrite"]
    if bbox is not None:
        left, top, right, bottom = bbox
        w = max(1, right - left)
        h = max(1, bottom - top)
        scrot_cmd += ["-a", f"{left},{top},{w},{h}"]
    scrot_cmd.append(str(tmp))
    try:
        try:
            session._run_x(scrot_cmd)
        except RuntimeError as scrot_err:
            # Fallback: ImageMagick import to stdout.
            log.warning("scrot failed (%s) — falling back to ImageMagick import", scrot_err)
            import_cmd = ["import", "-window", "root", "-silent"]
            if bbox is not None:
                left, top, right, bottom = bbox
                import_cmd += ["-crop", f"{right - left}x{bottom - top}+{left}+{top}"]
            import_cmd.append("png:-")
            r = session._run_x(import_cmd)
            data = r.stdout or b""
            if not data:
                raise RuntimeError("ImageMagick import produced empty output")
            return data
        data = tmp.read_bytes()
        if not data:
            raise RuntimeError("screenshot produced an empty file")
        return data
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def capture_geometry(monitor: int = 0) -> CaptureGeometry:
    """Capture geometry (#28, see ``platform/linux/display.py``). Raises the stub
    message on a non-X11 session without shelling out (the headless contract)."""
    if get_session().mode != "x11":
        raise DesktopUnavailable(_unavailable())
    return query_geometry(monitor, lambda cmd: (session._run_x(cmd).stdout or b"").decode("utf-8", "replace"))


def capture_frame(monitor: int = 0, max_width: int = 1280):
    """Capture ``monitor`` (scrot) fitted to its frame → ``(PIL image, Frame)``."""
    import io

    from PIL import Image

    geometry = capture_geometry(monitor)
    bbox = None
    if monitor > 0:
        bbox = (geometry.x, geometry.y, geometry.x + geometry.width, geometry.y + geometry.height)
    img = Image.open(io.BytesIO(capture_png(bbox)))
    frame = frame_for(monitor, max_width, geometry)
    return fit_to_frame(img, frame, geometry), frame


# ---------------------------------------------------------------------------
# Screen recording — ffmpeg x11grab (issue #8)
# ---------------------------------------------------------------------------


def _ffmpeg_x11grab_argv(
    display: str,
    out_path: str,
    *,
    duration: float,
    fps: int,
    region: Optional[tuple[int, int, int, int]],
    max_width: int,
) -> list[str]:
    """Build the ``ffmpeg -f x11grab`` argv for a bounded recording (PURE).

    ``region`` is ``(left, top, right, bottom)`` or ``None`` for the whole display
    (x11grab auto-detects the root-window size when no ``-video_size`` is given).
    Output is an animated GIF so the shared ``ScreenRecord`` tool returns it the
    SAME way as Windows/macOS (``ImageContent`` ``image/gif``). Split out from
    :func:`record_screen` so the argv can be unit-tested without a display.
    """
    fps = min(max(int(fps), 1), 10)
    duration = min(max(float(duration), 0.5), 10.0)
    argv = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "x11grab",
        "-framerate",
        str(fps),
    ]
    input_spec = display or ":0"
    if region is not None:
        left, top, right, bottom = region
        width = max(2, right - left)
        height = max(2, bottom - top)
        argv += ["-video_size", f"{width}x{height}"]
        input_spec = f"{input_spec}+{left},{top}"
    argv += ["-i", input_spec, "-t", f"{duration:g}"]
    vf = [f"fps={fps}"]
    if max_width and int(max_width) > 0:
        # ``-2`` keeps the aspect ratio while rounding to an even dimension.
        vf.append(f"scale={int(max_width)}:-2:flags=lanczos")
    argv += ["-vf", ",".join(vf), out_path]
    return argv


def record_screen(
    duration: float = 3.0,
    fps: int = 5,
    left: Optional[int] = None,
    top: Optional[int] = None,
    right: Optional[int] = None,
    bottom: Optional[int] = None,
    max_width: int = 800,
) -> str:
    """Record the X11 screen via ffmpeg x11grab; return a base64 animated GIF.

    Mirrors :func:`remoteos.recording.record_screen`'s signature + return shape
    (a base64 string) so the shared ``ScreenRecord`` tool treats Linux exactly
    like Windows/macOS. Runs inside the session via ``_build_invocation`` (env
    injection as the session user, ``sudo -u`` wrap when running as root), the
    same as every other X command.

    Raises :class:`RuntimeError` with the headless stub message on a non-X11 box
    — the same contract as :func:`capture_frame`, so the headless cam1..4 boxes
    never shell out and never crash (the tool is display-gated off there anyway).
    """
    s = get_session()
    if s.mode != "x11":
        raise DesktopUnavailable(_unavailable())

    region: Optional[tuple[int, int, int, int]] = None
    if None not in (left, top, right, bottom):
        li, ti, ri, bi = int(left), int(top), int(right), int(bottom)  # type: ignore[arg-type]
        if ri > li and bi > ti:
            region = (li, ti, ri, bi)

    dur = min(max(float(duration), 0.5), 10.0)
    tmp = _capture_tmp(".gif")
    argv = _ffmpeg_x11grab_argv(s.display or ":0", str(tmp), duration=dur, fps=fps, region=region, max_width=max_width)
    log.info("record_screen: %.1fs @ %sfps region=%s -> %s", dur, fps, region, tmp)
    try:
        # ffmpeg records for `dur` seconds then encodes — allow generous headroom.
        session._run_x(argv, timeout=int(dur) + 30)
        data = tmp.read_bytes()
        if not data:
            raise RuntimeError("ffmpeg x11grab produced an empty recording")
        import base64

        log.info("record_screen: produced %d bytes of GIF", len(data))
        return base64.b64encode(data).decode()
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - defensive
            pass

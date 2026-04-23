"""OCR support — screenshot a region and extract text."""

from __future__ import annotations

import io
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from PIL import ImageGrab
except ImportError:
    ImageGrab = None  # type: ignore[assignment,misc]


def _screenshot_region(
    left: int | None = None,
    top: int | None = None,
    right: int | None = None,
    bottom: int | None = None,
) -> bytes:
    """Capture a region (or full screen) and return PNG bytes."""
    if left is not None and top is not None and right is not None and bottom is not None:
        img = ImageGrab.grab(bbox=(left, top, right, bottom))
    else:
        img = ImageGrab.grab()
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def ocr_pytesseract(
    left: int | None = None,
    top: int | None = None,
    right: int | None = None,
    bottom: int | None = None,
    lang: str = "eng",
) -> str:
    """Run OCR using pytesseract."""
    try:
        import pytesseract
    except ImportError:
        raise ImportError(
            "pytesseract is not installed. Install it with:\n"
            "  pip install pytesseract\n"
            "You also need Tesseract-OCR installed:\n"
            "  Windows: https://github.com/UB-Mannheim/tesseract/wiki\n"
            "  Then set TESSERACT_CMD or add to PATH."
        )

    if left is not None and top is not None and right is not None and bottom is not None:
        img = ImageGrab.grab(bbox=(left, top, right, bottom))
    else:
        img = ImageGrab.grab()

    text: str = pytesseract.image_to_string(img, lang=lang)
    return text.strip()


def ocr_windows_builtin(
    left: int | None = None,
    top: int | None = None,
    right: int | None = None,
    bottom: int | None = None,
) -> str:
    """Run OCR using Windows built-in OCR engine via PowerShell."""
    png_bytes = _screenshot_region(left, top, right, bottom)

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp.write(png_bytes)
        tmp_path = tmp.name

    ps_script = f"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]

# Helper to await WinRT async
Add-Type -TypeDefinition @'
using System;
using System.Threading.Tasks;
using System.Runtime.CompilerServices;
public static class AsyncHelper {{
    public static T Await<T>(Windows.Foundation.IAsyncOperation<T> op) {{
        return Task.Run(() => {{
            while (op.Status == Windows.Foundation.AsyncStatus.Started) {{
                System.Threading.Thread.Sleep(10);
            }}
            return op.GetResults();
        }}).Result;
    }}
}}
'@ -ReferencedAssemblies "C:\\Windows\\Microsoft.NET\\Framework64\\v4.0.30319\\System.Runtime.WindowsRuntime.dll"

$path = "{tmp_path.replace(chr(92), chr(92) + chr(92))}"
$stream = [System.IO.File]::OpenRead($path)
$ras = [System.IO.WindowsRuntimeStreamExtensions]::AsRandomAccessStream($stream)
$decoder = [AsyncHelper]::Await([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($ras))
$bitmap = [AsyncHelper]::Await($decoder.GetSoftwareBitmapAsync())
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
$result = [AsyncHelper]::Await($engine.RecognizeAsync($bitmap))
$stream.Close()
Write-Output $result.Text
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout.strip()
    except Exception as e:
        return f"Windows OCR error: {e}"
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass


def ocr_macos_shortcuts(
    left: int | None = None,
    top: int | None = None,
    right: int | None = None,
    bottom: int | None = None,
) -> str:
    """Use macOS Shortcuts for basic text recognition."""
    bbox = None
    if all(v is not None for v in (left, top, right, bottom)):
        bbox = (left, top, right, bottom)
    img = ImageGrab.grab(bbox=bbox)
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        img.save(tmp, format="PNG")
        tmp_path = tmp.name
    try:
        result = subprocess.run(
            [
                "osascript", "-e",
                f'do shell script "shortcuts run \\"Extract Text from Image\\" <<< \\"{tmp_path}\\""',
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        return "macOS OCR: No text extracted (Shortcuts OCR not available)"
    except Exception as e:
        return f"macOS OCR error: {e}"
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def run_ocr(
    left: int | None = None,
    top: int | None = None,
    right: int | None = None,
    bottom: int | None = None,
    lang: str = "eng",
) -> str:
    """Run OCR, trying pytesseract first, then platform-specific fallback."""
    errors = []
    # Try pytesseract first
    try:
        return ocr_pytesseract(left, top, right, bottom, lang=lang)
    except ImportError as e:
        errors.append(f"pytesseract: {e}")
    except Exception as e:
        errors.append(f"pytesseract error: {e}")

    # macOS fallback
    if sys.platform == "darwin":
        try:
            return ocr_macos_shortcuts(left, top, right, bottom)
        except Exception as e:
            errors.append(f"macOS OCR: {e}")

    # Fallback to Windows built-in OCR
    if sys.platform == "win32":
        try:
            result = ocr_windows_builtin(left, top, right, bottom)
            if result and "error" not in result.lower()[:20]:
                return result
            if result:
                errors.append(f"Windows OCR: {result}")
        except Exception as e:
            errors.append(f"Windows OCR error: {e}")

    # All failed
    return "OCR failed. Errors:\n" + "\n".join(errors)

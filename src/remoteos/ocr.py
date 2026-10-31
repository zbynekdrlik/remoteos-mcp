"""OCR support: screenshot a region and read its text, or its words with their boxes (#36)."""

from __future__ import annotations

import io
import json
import logging
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from remoteos.coords import Bbox, capture_box_to_screen
from remoteos.marks import Word
from remoteos.platform.command import powershell_argv, run_child
from remoteos.platform.win.psquote import ps_literal

try:
    from PIL import ImageGrab
except ImportError:
    ImageGrab = None  # type: ignore[assignment,misc]

log = logging.getLogger("remoteos.ocr")

DEFAULT_LANG = "slk+eng"
# Sparse text: a custom-drawn UI is labels scattered on a surface, not a page of paragraphs.
_TESSERACT_WORD_MODE = ("--psm", "11")
# Below this, Tesseract's "words" on icons and meters are noise. Its confidence is 0..100.
_MIN_WORD_CONFIDENCE = 40.0
_TSV_COLUMNS = 12  # level page block par line word left top width height conf text
_TSV_WORD_LEVEL = "5"

# WinRT names languages by BCP-47 tag, Tesseract by its own codes. Any other code passes as it is.
_WINRT_TAGS = {
    "ces": "cs",
    "deu": "de",
    "eng": "en",
    "fra": "fr",
    "hun": "hu",
    "ita": "it",
    "pol": "pl",
    "rus": "ru",
    "slk": "sk",
    "spa": "es",
    "ukr": "uk",
}


@dataclass(frozen=True)
class TextReading:
    """What OCR read. ``note`` says when it could not use the languages asked for."""

    text: str
    note: str = ""


@dataclass(frozen=True)
class WordReading:
    """The words OCR read, boxes in screen coordinates (#28), and the language note."""

    words: tuple[Word, ...]
    note: str = ""


def choose_languages(requested: str, installed: Iterable[str]) -> tuple[str, str]:
    """``(languages to use, note)``: the ones asked for that exist, else what exists.

    An empty ``installed`` means the list could not be read. Then Tesseract decides for
    itself, and fails loudly if it must: a probe never refuses a language.
    """
    known = [lang for lang in installed if lang != "osd"]  # osd is orientation data, no language
    if not known:
        return requested, ""
    wanted = requested.split("+")
    usable = [lang for lang in wanted if lang in known]
    missing = [lang for lang in wanted if lang not in known]
    chosen = "+".join(usable or known)
    return chosen, f"OCR languages: {'+'.join(missing)} not installed, used {chosen}" if missing else ""


def _is_word(text: str) -> bool:
    """Whether OCR read something a person would call a label: an edge of a meter reads as ``|``."""
    return any(c.isalnum() for c in text)


def parse_tsv(tsv: str) -> list[tuple[str, tuple[int, int, int, int]]]:
    """``(word, (left, top, right, bottom))`` in capture pixels, from Tesseract's TSV output."""
    words = []
    for line in tsv.splitlines()[1:]:
        columns = line.split("\t", _TSV_COLUMNS - 1)
        if len(columns) < _TSV_COLUMNS or columns[0] != _TSV_WORD_LEVEL:
            continue
        text = columns[11].strip()
        if float(columns[10]) < _MIN_WORD_CONFIDENCE or not _is_word(text):
            continue
        left, top, width, height = (int(v) for v in columns[6:10])
        words.append((text, (left, top, left + width, top + height)))
    return words


def _grab(left: int | None, top: int | None, right: int | None, bottom: int | None):
    """The region (or every screen) as a PIL image.

    all_screens=True: without it the grab is primary-only and a region on another monitor is black.
    """
    bbox = None if None in (left, top, right, bottom) else (left, top, right, bottom)
    return ImageGrab.grab(bbox=bbox, all_screens=True)


def _png(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _screenshot_region(
    left: int | None = None,
    top: int | None = None,
    right: int | None = None,
    bottom: int | None = None,
) -> bytes:
    """Capture a region (or full screen) and return PNG bytes."""
    return _png(_grab(left, top, right, bottom))


def _pytesseract():
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
    return pytesseract


def ocr_pytesseract(
    left: int | None = None,
    top: int | None = None,
    right: int | None = None,
    bottom: int | None = None,
    lang: str = DEFAULT_LANG,
) -> TextReading:
    """Run OCR using pytesseract."""
    pytesseract = _pytesseract()
    chosen, note = choose_languages(lang, pytesseract.get_languages(config=""))
    img = _grab(left, top, right, bottom)
    text: str = pytesseract.image_to_string(img, lang=chosen)
    return TextReading(text.strip(), note)


# Stop: a failing step ends the script with a non-zero exit. Without it a call on a null OcrEngine
# exits 0 with empty stdout, which read as "(no text detected)" (#32 review round 1).
# UTF-8 comes from powershell_argv's console trampoline, which switches the child's OWN console
# before PowerShell starts: through the OEM page PowerShell replaced € and – with "?" (#32 review
# round 5; shared since #48, with no encoding code in the script since round 2).
# The path and the language tags are single-quoted literals (#39): PowerShell never expands them.
# AsTask, not a compiled C# helper: Add-Type does not reference the WinRT interface types (#36).
# One engine reads one language, so the first requested one that is installed reads the screen.
_WINRT_SCRIPT = """
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Foundation.IAsyncOperation`1, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime]

$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1
function Await($operation, $type) {
    $task = $asTask.MakeGenericMethod($type).Invoke($null, @($operation))
    $null = $task.Wait(-1)
    $task.Result
}

$path = @PATH@
$stream = [System.IO.File]::OpenRead($path)
$ras = [System.IO.WindowsRuntimeStreamExtensions]::AsRandomAccessStream($stream)
$decoding = [Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($ras)
$decoder = Await $decoding ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

$installed = @([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages)
$engine = $null
$missing = @()
foreach ($tag in @(@TAGS@)) {
    $match = @($installed | Where-Object { $_.LanguageTag.Split('-')[0] -eq $tag.Split('-')[0] })
    if ($match.Count -eq 0) { $missing += $tag }
    elseif ($null -eq $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($match[0]) }
}
if ($null -eq $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
if ($null -eq $engine) { throw 'no OCR language is installed on this Windows' }

$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$stream.Close()
$words = @(foreach ($line in $result.Lines) {
    foreach ($word in $line.Words) {
        $box = $word.BoundingRect
        @{ text = $word.Text; left = $box.X; top = $box.Y; width = $box.Width; height = $box.Height }
    }
})
@{
    language = $engine.RecognizerLanguage.LanguageTag
    missing = @($missing)
    text = "$($result.Text)"
    words = $words
} | ConvertTo-Json -Depth 4 -Compress
"""


def _winrt_tags(lang: str) -> list[str]:
    """``lang`` in WinRT's language tags, in the order asked for, each once."""
    return list(dict.fromkeys(_WINRT_TAGS.get(code, code) for code in lang.split("+")))


def _winrt_read(png_bytes: bytes, lang: str) -> dict:
    """The Windows OCR engine's reading of a PNG: ``text``, ``words``, ``language``, ``missing``."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp.write(png_bytes)
        tmp_path = tmp.name
    values = {"TAGS": ", ".join(ps_literal(tag) for tag in _winrt_tags(lang)), "PATH": ps_literal(tmp_path)}
    # one pass: a caller's text that spells a placeholder must stay text
    script = re.sub("@(TAGS|PATH)@", lambda match: values[match[1]], _WINRT_SCRIPT)
    try:
        result = run_child(powershell_argv(script), timeout=30, windows=True)
        out, problem = result.stdout.strip(), result.stderr.strip()
        # a failure is a non-zero exit, or no reply at all: never "(no text detected)"
        if result.returncode != 0 or not out:
            raise RuntimeError(f"Windows OCR failed: {problem or f'exit code {result.returncode}'}")
        return json.loads(out)
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except OSError as e:
            log.debug("OCR temp file left behind (%s)", type(e).__name__)


def _winrt_note(reading: dict) -> str:
    missing = reading["missing"]
    return f"OCR languages: {'+'.join(missing)} not installed, used {reading['language']}" if missing else ""


def ocr_windows_builtin(
    left: int | None = None,
    top: int | None = None,
    right: int | None = None,
    bottom: int | None = None,
    lang: str = DEFAULT_LANG,
) -> TextReading:
    """Run OCR using the Windows built-in OCR engine via PowerShell."""
    reading = _winrt_read(_screenshot_region(left, top, right, bottom), lang)
    return TextReading(reading["text"].strip(), _winrt_note(reading))


def _tesseract_languages() -> list[str]:
    """The languages of the ``tesseract`` CLI; empty when it does not say."""
    result = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines()[1:] if line.strip()]


def _tesseract_cli(png_bytes: bytes, lang: str, *options: str) -> tuple[str, str]:
    """``(tesseract's stdout, language note)`` for a PNG."""
    chosen, note = choose_languages(lang, _tesseract_languages())
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp.write(png_bytes)
        tmp_path = tmp.name
    try:
        result = subprocess.run(
            ["tesseract", tmp_path, "stdout", "-l", chosen, *options],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"tesseract failed: {result.stderr.strip()}")
        return result.stdout, note
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def ocr_linux_tesseract(
    left: int | None = None,
    top: int | None = None,
    right: int | None = None,
    bottom: int | None = None,
    lang: str = DEFAULT_LANG,
) -> TextReading:
    """OCR on Linux: screenshot via the X11 provider (scrot) → tesseract CLI.

    Uses the Linux desktop provider's ``capture_png`` so the same session
    detection / X-authority handling used for Snapshot applies here, then runs
    the ``tesseract`` binary directly (no pytesseract/PIL dependency needed).
    Raises when there is no usable X11 session so ``read_text`` surfaces it.
    """
    from remoteos.platform.linux import desktop as linux_desktop

    session = linux_desktop.get_session()
    if session.mode != "x11":
        raise RuntimeError(linux_desktop._unavailable())

    bbox = None
    if all(v is not None for v in (left, top, right, bottom)):
        bbox = (left, top, right, bottom)
    text, note = _tesseract_cli(linux_desktop.capture_png(bbox), lang)
    return TextReading(text.strip(), note)


def ocr_macos_shortcuts(
    left: int | None = None,
    top: int | None = None,
    right: int | None = None,
    bottom: int | None = None,
) -> TextReading:
    """Use macOS Shortcuts for basic text recognition."""
    img = _grab(left, top, right, bottom)
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
        if result.returncode != 0:  # e.g. the "Extract Text from Image" shortcut is not installed
            reason = result.stderr.strip() or f"exit code {result.returncode}"
            raise RuntimeError(f"macOS Shortcuts OCR failed: {reason}")
        return TextReading(result.stdout.strip())
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def read_text(
    left: int | None = None,
    top: int | None = None,
    right: int | None = None,
    bottom: int | None = None,
    lang: str = DEFAULT_LANG,
) -> TextReading:
    """Run OCR, trying pytesseract first, then platform-specific fallback."""
    errors = []

    # Linux: use the X11 provider screenshot + tesseract CLI (no PIL/pytesseract).
    if sys.platform == "linux":
        try:
            return ocr_linux_tesseract(left, top, right, bottom, lang=lang)
        except Exception as e:
            errors.append(f"linux tesseract: {e}")

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
            return ocr_windows_builtin(left, top, right, bottom, lang=lang)
        except Exception as e:
            errors.append(f"Windows OCR error: {e}")

    # All failed: an error, never a successful "text" (#32)
    raise RuntimeError("OCR failed. Errors:\n" + "\n".join(errors))


def run_ocr(
    left: int | None = None,
    top: int | None = None,
    right: int | None = None,
    bottom: int | None = None,
    lang: str = DEFAULT_LANG,
) -> str:
    """The text of :func:`read_text`, for a caller that only matches on it."""
    return read_text(left, top, right, bottom, lang=lang).text


def _words_tesseract_cli(img, lang: str):
    tsv, note = _tesseract_cli(_png(img), lang, *_TESSERACT_WORD_MODE, "tsv")
    return parse_tsv(tsv), note


def _words_pytesseract(img, lang: str):
    pytesseract = _pytesseract()
    chosen, note = choose_languages(lang, pytesseract.get_languages(config=""))
    tsv = pytesseract.image_to_data(img, lang=chosen, config=" ".join(_TESSERACT_WORD_MODE), timeout=30)
    return parse_tsv(tsv), note


def _words_winrt(img, lang: str):
    reading = _winrt_read(_png(img), lang)
    words = []
    for word in reading["words"]:
        left, top = word["left"], word["top"]
        if _is_word(word["text"]):
            words.append((word["text"], (left, top, left + word["width"], top + word["height"])))
    return words, _winrt_note(reading)


def _word_backends() -> list[tuple[str, Callable]]:
    """Every engine that gives boxes, in :func:`read_text`'s order. macOS Shortcuts gives none."""
    backends: list[tuple[str, Callable]] = [("pytesseract", _words_pytesseract)]
    if sys.platform == "linux":
        backends.insert(0, ("tesseract", _words_tesseract_cli))
    if sys.platform == "win32":
        backends.append(("Windows OCR", _words_winrt))
    return backends


def read_words(region: Bbox, capture: Callable[[Bbox], object], lang: str = DEFAULT_LANG) -> WordReading:
    """The words in ``region`` (screen coordinates) with their boxes in screen coordinates.

    ``capture`` grabs ``region`` at native resolution (the provider's ``capture_region``): OCR
    reads the full pixels, not a Snapshot shrunk to ``max_width``. Raises when no engine gives boxes.
    """
    img = capture(region)
    errors = []
    for name, backend in _word_backends():
        try:
            found, note = backend(img, lang)
        except ImportError:
            errors.append(f"{name} is not installed")
        except Exception as e:
            errors.append(f"{name}: {e}")
        else:
            words = (Word(text, capture_box_to_screen(box, img.size, region)) for text, box in found)
            return WordReading(tuple(words), note)
    raise RuntimeError("; ".join(errors))

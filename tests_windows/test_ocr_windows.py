"""The real Windows OCR script on PowerShell 5.1 (#36), no fakes.

It runs the WinRT loading, the async wait and the JSON reply for real. Whether the runner has an
OCR language is the image's choice: with one the word must be read and boxed, without one the
reply must be the script's own named failure. Anything else (a parse error, a type that does
not load, a broken wait) fails either way.
"""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw, ImageFont

from remoteos import ocr


def _picture() -> Image.Image:
    img = Image.new("RGB", (600, 200), "white")
    ImageDraw.Draw(img).text((40, 60), "HELLO WORLD", fill="black", font=ImageFont.truetype("arial.ttf", 48))
    return img


def test_the_real_script_reads_a_word_or_names_what_is_missing():
    try:
        reading = ocr.read_words((100, 50, 700, 250), lambda region: _picture())
    except RuntimeError as e:
        assert "no OCR language is installed on this Windows" in str(e)
        return
    hello = [word for word in reading.words if word.text.upper() == "HELLO"]
    assert hello, reading.words
    left, top, right, bottom = hello[0].rect
    assert 100 < left < right < 700 and 50 < top < bottom < 250
    assert left == pytest.approx(140, abs=15), "the box is offset by the region"

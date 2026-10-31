"""The real Windows OCR script on PowerShell 5.1 (#36), no fakes.

The script loads WinRT and decodes the PNG through its async wait BEFORE it needs an OCR language,
so a parse error, a type that does not load or a broken wait fails on any runner. Whether the
runner has a language is the image's choice: with one, the word must be read and boxed; without
one, the reply must be the script's own named failure, and recognition and the JSON reply are
proven only by the live check.
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
        words, note = ocr._words_winrt(_picture(), ocr.DEFAULT_LANG)
    except RuntimeError as e:
        assert "no OCR language is installed on this Windows" in str(e)
        return
    print(note)
    hello = [box for text, box in words if text.upper() == "HELLO"]
    assert hello, words
    assert hello[0][0] == pytest.approx(40, abs=15)
    assert hello[0][1] == pytest.approx(70, abs=25)

"""OCR word boxes and languages (#36), and AnnotatedSnapshot's merged marks at tool level.

Each backend runs for real; only its boundary is faked: the stdlib ``subprocess``, the
``pytesseract`` module, PowerShell's reply.
"""

from __future__ import annotations

import json
import subprocess
import sys
import types

import pytest
import test_coordinate_contract_tools as coordinate_tools
from fake_console_child import popen_answered_by, run_result
from PIL import Image
from tool_surface import server
from tool_test_support import tool_error

from remoteos import ocr
from remoteos.coords import capture_box_to_screen
from remoteos.marks import Word

fake_x11 = coordinate_tools.fake_x11
dual_x11 = coordinate_tools.dual_x11
_mousemoves = coordinate_tools._mousemoves
_text = coordinate_tools._text

TSV_HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"


def tsv_word(text, left, top, width, height, conf=90.0):
    return f"5\t1\t1\t1\t1\t1\t{left}\t{top}\t{width}\t{height}\t{conf}\t{text}\n"


def capture_of(size):
    return lambda region: Image.new("RGB", size)


# -- languages ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "requested, installed, chosen, note",
    [
        ("slk+eng", ["eng", "osd", "slk"], "slk+eng", ""),
        ("slk+eng", ["eng", "osd"], "eng", "OCR languages: slk not installed, used eng"),
        ("slk", ["deu", "osd"], "deu", "OCR languages: slk not installed, used deu"),
        ("slk+eng", ["deu", "fra"], "deu+fra", "OCR languages: slk+eng not installed, used deu+fra"),
        ("slk+eng", [], "slk+eng", ""),  # the list could not be read: tesseract decides
        ("slk", ["osd"], "slk", ""),  # osd is no language
    ],
)
def test_languages_fall_back_to_what_is_installed_and_say_so(requested, installed, chosen, note):
    assert ocr.choose_languages(requested, installed) == (chosen, note)


def test_the_default_is_slovak_and_english():
    assert ocr.DEFAULT_LANG == "slk+eng"


# -- Tesseract's TSV ------------------------------------------------------------------------------


def test_tsv_gives_word_level_rows_with_right_and_bottom():
    page = "1\t1\t0\t0\t0\t0\t0\t0\t800\t600\t-1\t\n"
    assert ocr.parse_tsv(TSV_HEADER + page + tsv_word("Prehrať", 10, 20, 30, 40)) == [("Prehrať", (10, 20, 40, 60))]


@pytest.mark.parametrize(
    "row, kept",
    [
        (tsv_word("Noise", 0, 0, 9, 9, conf=39.9), False),
        (tsv_word("Sure", 0, 0, 9, 9, conf=40), True),
        (tsv_word("|", 0, 0, 9, 9), False),  # an edge of a meter, no word
        (tsv_word("—", 0, 0, 9, 9), False),
        (tsv_word("   ", 0, 0, 9, 9), False),
        (tsv_word("7", 0, 0, 9, 9), True),
    ],
)
def test_tsv_drops_what_is_no_word(row, kept):
    assert bool(ocr.parse_tsv(TSV_HEADER + row)) is kept


def test_a_header_alone_is_no_words():
    assert ocr.parse_tsv(TSV_HEADER) == []


# -- into the #28 contract ------------------------------------------------------------------------


def test_a_box_of_a_native_capture_is_offset_by_its_region():
    assert capture_box_to_screen((10, 20, 30, 40), (100, 100), (1920, 50, 2020, 150)) == (1930, 70, 1950, 90)


def test_a_retina_capture_in_pixels_maps_to_points():
    assert capture_box_to_screen((100, 40, 201, 80), (2880, 1800), (0, 0, 1440, 900)) == (50, 20, 101, 40)


def test_a_box_rounds_half_up_never_banker_s():
    # 4.5 screen units: round() would give 4, the contract gives 5
    assert capture_box_to_screen((9, 9, 9, 9), (20, 20), (0, 0, 10, 10))[0] == 5


# -- the tesseract CLI ----------------------------------------------------------------------------


class _Tesseract:
    """The ``tesseract`` CLI at ``subprocess.run``."""

    def __init__(self, langs="eng\nosd\nslk\n", tsv=TSV_HEADER, code=0, stderr=""):
        self.langs, self.tsv, self.code, self.stderr = langs, tsv, code, stderr
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        if "--list-langs" in cmd:
            listing = f"List of available languages (3):\n{self.langs}"
            return types.SimpleNamespace(returncode=0, stdout=listing, stderr="")
        return types.SimpleNamespace(returncode=self.code, stdout=self.tsv, stderr=self.stderr)


@pytest.fixture
def tesseract(monkeypatch):
    fake = _Tesseract()
    monkeypatch.setattr(ocr.sys, "platform", "linux")
    monkeypatch.setattr(ocr.subprocess, "run", fake)
    return fake


def test_linux_words_come_from_tsv_in_sparse_mode_with_the_chosen_languages(tesseract):
    tesseract.tsv = TSV_HEADER + tsv_word("Play", 10, 20, 30, 40)
    reading = ocr.read_words((100, 0, 200, 100), capture_of((100, 100)))
    assert reading == ocr.WordReading((Word("Play", (110, 20, 140, 60)),), "")
    [_, command] = tesseract.calls
    assert command[0] == "tesseract" and command[2:] == ["stdout", "-l", "slk+eng", "--psm", "11", "tsv"]


def test_linux_words_say_when_slovak_is_missing(tesseract):
    tesseract.langs = "eng\nosd\n"
    reading = ocr.read_words((0, 0, 10, 10), capture_of((10, 10)))
    assert reading.note == "OCR languages: slk not installed, used eng"
    assert tesseract.calls[1][4] == "eng"


def test_a_failing_tesseract_is_a_failure_never_no_words(tesseract, monkeypatch):
    tesseract.code, tesseract.stderr = 1, "Error opening data file"
    monkeypatch.setitem(sys.modules, "pytesseract", None)
    with pytest.raises(RuntimeError, match="tesseract: tesseract failed: Error opening data file; pytesseract is not"):
        ocr.read_words((0, 0, 10, 10), capture_of((10, 10)))


def test_the_text_path_falls_back_too_so_the_new_default_never_breaks_ocr(tesseract, monkeypatch):
    tesseract.langs, tesseract.tsv = "eng\n", "Ahoj\n"
    png = coordinate_png()
    monkeypatch.setattr("remoteos.platform.linux.desktop.capture_png", lambda bbox=None: png)
    monkeypatch.setattr(
        "remoteos.platform.linux.desktop.get_session", lambda: types.SimpleNamespace(mode="x11")
    )
    assert ocr.read_text() == ocr.TextReading("Ahoj", "OCR languages: slk not installed, used eng")
    assert ocr.run_ocr() == "Ahoj"
    assert tesseract.calls[-1][-2:] == ["-l", "eng"]


def coordinate_png() -> bytes:
    return ocr._png(Image.new("RGB", (8, 8)))


# -- pytesseract ----------------------------------------------------------------------------------


@pytest.fixture
def pytesseract(monkeypatch):
    seen = {}

    def image_to_data(img, lang, config, timeout):
        seen.update(lang=lang, config=config, timeout=timeout)
        return TSV_HEADER + tsv_word("Slovo", 4, 4, 8, 8)

    module = types.SimpleNamespace(get_languages=lambda config: ["eng"], image_to_data=image_to_data, seen=seen)
    monkeypatch.setitem(sys.modules, "pytesseract", module)
    return module


def test_macos_words_come_from_pytesseract_with_retina_scaled_boxes(monkeypatch, pytesseract):
    monkeypatch.setattr(ocr.sys, "platform", "darwin")
    reading = ocr.read_words((0, 0, 50, 50), capture_of((100, 100)))
    assert reading.words == (Word("Slovo", (2, 2, 6, 6)),)
    assert reading.note == "OCR languages: slk not installed, used eng"
    assert pytesseract.seen == {"lang": "eng", "config": "--psm 11", "timeout": 30}


def test_macos_without_pytesseract_has_no_boxes_and_says_why(monkeypatch):
    monkeypatch.setattr(ocr.sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "pytesseract", None)
    with pytest.raises(RuntimeError, match="pytesseract is not installed"):
        ocr.read_words((0, 0, 10, 10), capture_of((10, 10)))


# -- Windows OCR ----------------------------------------------------------------------------------


def winrt_reply(text="", words=(), language="sk-SK", missing=()):
    return json.dumps({"language": language, "missing": list(missing), "text": text, "words": list(words)})


@pytest.fixture
def powershell(monkeypatch, windows_python):
    calls = []
    reply = types.SimpleNamespace(stdout=winrt_reply(), code=0, stderr="", calls=calls)

    def _run(cmd, **kwargs):
        calls.append(list(cmd))
        return run_result(kwargs, reply.code, reply.stdout, reply.stderr)

    monkeypatch.setattr(subprocess, "Popen", popen_answered_by(_run))
    monkeypatch.setattr(ocr.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "pytesseract", None)
    return reply


def test_windows_words_scale_float_boxes_onto_a_negative_monitor(powershell):
    powershell.stdout = winrt_reply(words=[{"text": "Stop", "left": 10.0, "top": 20.5, "width": 30.0, "height": 9.5}])
    reading = ocr.read_words((-1920, 0, 0, 1080), capture_of((1920, 1080)))
    assert reading == ocr.WordReading((Word("Stop", (-1910, 21, -1880, 30)),), "")


def test_windows_asks_for_slovak_then_english_by_their_tags_as_literals(powershell):
    ocr.read_words((0, 0, 10, 10), capture_of((10, 10)))
    [line] = [x for x in powershell.calls[0][-1].splitlines() if x.startswith("foreach ($tag")]
    assert line == "foreach ($tag in @('sk', 'en')) {"


def test_a_caller_s_language_cannot_leave_its_literal(powershell):
    ocr.read_words((0, 0, 10, 10), capture_of((10, 10)), lang="x');calc;('")
    assert "@('x'');calc;(''')" in powershell.calls[0][-1]


def test_a_caller_s_language_cannot_spell_a_placeholder(powershell):
    ocr.read_words((0, 0, 10, 10), capture_of((10, 10)), lang="@PATH@")
    assert "foreach ($tag in @('@PATH@')) {" in powershell.calls[0][-1]


def test_the_decoding_comes_before_the_language_so_any_runner_proves_the_wait():
    script = ocr._WINRT_SCRIPT
    assert script.index("GetSoftwareBitmapAsync") < script.index("no OCR language is installed")


def test_windows_drops_what_is_no_word_as_tesseract_does(powershell):
    box = {"left": 1.0, "top": 1.0, "width": 5.0, "height": 5.0}
    powershell.stdout = winrt_reply(words=[{"text": "|", **box}, {"text": "Ok", **box}])
    assert [w.text for w in ocr.read_words((0, 0, 10, 10), capture_of((10, 10))).words] == ["Ok"]


def test_a_language_windows_lacks_is_said(powershell):
    powershell.stdout = winrt_reply(language="en-US", missing=["sk"])
    assert ocr.read_words((0, 0, 10, 10), capture_of((10, 10))).note == "OCR languages: sk not installed, used en-US"


def test_windows_text_goes_through_the_same_reading(powershell, monkeypatch):
    grab = lambda bbox=None, all_screens=False: Image.new("RGB", (8, 8))  # noqa: E731
    monkeypatch.setattr(ocr, "ImageGrab", types.SimpleNamespace(grab=grab))
    powershell.stdout = winrt_reply(text="Žltý", language="en-US", missing=["sk"])
    assert ocr.read_text() == ocr.TextReading("Žltý", "OCR languages: sk not installed, used en-US")


@pytest.mark.parametrize(
    "code, stdout, stderr, message",
    [
        (1, "", "no OCR language is installed on this Windows", "no OCR language is installed"),
        (0, "", "You cannot call a method on a null-valued expression.", "null-valued"),
        (0, "", "", "exit code 0"),  # the script always answers: silence is a failure, never "no text"
    ],
)
def test_a_failing_script_is_a_failure_never_no_words(powershell, code, stdout, stderr, message):
    powershell.code, powershell.stdout, powershell.stderr = code, stdout, stderr
    with pytest.raises(RuntimeError, match=message):
        ocr.read_words((0, 0, 10, 10), capture_of((10, 10)))


def test_the_script_uses_no_compiled_helper_and_stays_ascii():
    # Add-Type could not reach Windows.Foundation; and -Command text must survive every code page
    assert "Add-Type -TypeDefinition" not in ocr._WINRT_SCRIPT
    assert ocr._WINRT_SCRIPT.isascii()


# -- the OCR tool ---------------------------------------------------------------------------------


def test_the_ocr_tool_says_when_a_language_is_missing(fake_x11, monkeypatch):
    note = "OCR languages: slk not installed, used eng"
    monkeypatch.setattr(ocr, "read_text", lambda **kw: ocr.TextReading("Ahoj", note))
    assert server.OCR() == f"Ahoj\n\n[{note}]"


def test_the_ocr_tool_says_no_text_and_the_language(fake_x11, monkeypatch):
    monkeypatch.setattr(ocr, "read_text", lambda **kw: ocr.TextReading("", "note"))
    assert server.OCR() == "(no text detected)\n\n[note]"


def test_the_ocr_tool_passes_its_region_and_language(fake_x11, monkeypatch):
    seen = {}
    monkeypatch.setattr(ocr, "read_text", lambda **kw: seen.update(kw) or ocr.TextReading("x"))
    assert server.OCR(left=1, top=2, right=3, bottom=4, lang="deu") == "x"
    assert seen == {"left": 1, "top": 2, "right": 3, "bottom": 4, "lang": "deu"}


# -- AnnotatedSnapshot ----------------------------------------------------------------------------


@pytest.fixture
def screen_words(fake_x11, monkeypatch):
    """A 2560x1600 custom-drawn surface where OCR reads ``Play`` at native (1000,400)-(1100,440)."""
    fake = _Tesseract(tsv=TSV_HEADER + tsv_word("Play", 1000, 400, 100, 40))
    monkeypatch.setattr(ocr.subprocess, "run", fake)
    return fake_x11


def test_a_word_on_a_custom_drawn_surface_is_a_mark_and_click_hits_it(screen_words):
    text = _text(server.AnnotatedSnapshot())
    assert '**Marks:** 1 (0 elements, 1 OCR)\n  [1] ocr text "Play" at (525,210)' in text
    server.Click(525, 210)
    x, y = (int(v) for v in _mousemoves(screen_words)[-1])
    assert 1000 <= x < 1100 and 400 <= y < 440


def test_ocr_reads_the_native_capture_not_the_shrunk_image(screen_words):
    server.AnnotatedSnapshot()
    regions = [call[call.index("-a") + 1] for call in screen_words if call[:1] == ["scrot"] and "-a" in call]
    assert regions == ["0,0,2560,1600"]


def test_marks_of_another_monitor_are_in_its_frame(dual_x11, monkeypatch):
    fake = _Tesseract(tsv=TSV_HEADER + tsv_word("Play", 100, 100, 100, 40))
    monkeypatch.setattr(ocr.subprocess, "run", fake)
    text = _text(server.AnnotatedSnapshot(monitor=2))
    assert 'ocr text "Play" at (100,80)' in text  # scale 1.500 on this head
    server.Click(100, 80, monitor=2)
    assert _mousemoves(dual_x11)[-1] == ["2710", "120"]


def test_elements_and_words_share_one_list_and_a_covered_word_adds_nothing(screen_words, monkeypatch):
    rect = {"left": 990, "top": 390, "right": 1110, "bottom": 450}
    element = {"index": 1, "class": "push button", "text": "Play", "rect": rect}
    monkeypatch.setattr("remoteos.platform.linux.desktop.marked_elements", lambda limit=300: ([element], ""))
    text = _text(server.AnnotatedSnapshot())
    assert '**Marks:** 1 (1 elements, 0 OCR)\n  [1] element push button "Play" at (525,210)' in text
    assert "ocr text" not in text


def test_ocr_failing_keeps_the_image_and_says_why(fake_x11, monkeypatch):
    def broken(cmd, **kwargs):
        raise OSError("no tesseract here")

    monkeypatch.setattr(ocr.subprocess, "run", broken)
    monkeypatch.setitem(sys.modules, "pytesseract", None)
    parts = server.AnnotatedSnapshot()
    assert coordinate_tools._image_size(parts) == (1280, 800)
    assert "**OCR:** no text marks: tesseract: no tesseract here; pytesseract is not installed" in _text(parts)


def test_a_long_ocr_failure_keeps_its_real_reason(fake_x11, monkeypatch):
    reason = "pytesseract is not installed; Windows OCR: Windows OCR failed: " + "x" * 60 + " the real reason"
    monkeypatch.setattr(ocr, "read_words", lambda region, capture: (_ for _ in ()).throw(RuntimeError(reason)))
    assert f"**OCR:** no text marks: {reason}" in _text(server.AnnotatedSnapshot())


def test_an_over_long_ocr_failure_keeps_its_last_engine_s_reason(fake_x11, monkeypatch):
    reason = "first engine: " + "y" * 600 + " last engine: the real reason"
    monkeypatch.setattr(ocr, "read_words", lambda region, capture: (_ for _ in ()).throw(RuntimeError(reason)))
    text = _text(server.AnnotatedSnapshot())
    assert text.endswith("last engine: the real reason") and "first engine" not in text


def test_a_screen_with_nothing_on_it_says_so(fake_x11, monkeypatch):
    monkeypatch.setattr(ocr.subprocess, "run", _Tesseract())
    assert "No interactive elements or text found." in _text(server.AnnotatedSnapshot())


def test_the_language_note_reaches_the_reply(screen_words, monkeypatch):
    ocr.subprocess.run.langs = "eng\n"
    assert "**OCR:** OCR languages: slk not installed, used eng" in _text(server.AnnotatedSnapshot())


def test_truncation_is_reported(fake_x11, monkeypatch):
    rows = "".join(tsv_word(f"w{i}", 100, 100 * i, 50, 40) for i in range(1, 6))
    monkeypatch.setattr(ocr.subprocess, "run", _Tesseract(tsv=TSV_HEADER + rows))
    text = _text(server.AnnotatedSnapshot(max_elements=2))
    assert "**Marks:** 2 " in text
    assert "**Incomplete:** 3 more not listed (limit 2)." in text


def test_a_cut_element_list_says_why(fake_x11, monkeypatch):
    rect = {"left": 0, "top": 0, "right": 10, "bottom": 10}
    element = {"index": 1, "class": "button", "text": "a", "rect": rect}
    listing = lambda limit=300: ([element], f"only the first {limit} elements are listed")  # noqa: E731
    monkeypatch.setattr("remoteos.platform.linux.desktop.marked_elements", listing)
    monkeypatch.setattr(ocr.subprocess, "run", _Tesseract())
    text = _text(server.AnnotatedSnapshot(max_elements=1))
    assert "**Incomplete:** only the first 1 elements are listed." in text


@pytest.mark.parametrize("limit", [0, -1, 301])
def test_a_limit_out_of_range_is_refused(fake_x11, limit):
    assert "max_elements must be 1..300" in tool_error(lambda: server.AnnotatedSnapshot(max_elements=limit))


def test_a_word_s_text_is_never_logged(screen_words, caplog):
    caplog.set_level("DEBUG")
    server.AnnotatedSnapshot()
    assert "Play" not in caplog.text

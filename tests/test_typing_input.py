"""Unicode typing plan (#30): the pure splitter behind every provider's ``type_unicode()``.

Text becomes strokes: a Unicode character (its UTF-16 code units — a character
outside the BMP is a surrogate pair) or a special key (``\\n`` / ``\\r\\n`` / ``\\r``
→ Enter, ``\\t`` → Tab). Windows sends one stroke per character; macOS merges
consecutive characters into runs of at most 20 UTF-16 units. The plan never
drops a character, and the tool's reply counts what was really delivered.
"""

from __future__ import annotations

import pytest

from remoteos.typing_input import check_method, plan, typed_reply, utf16_units


def _view(strokes):
    return [(s.source, s.key, s.units) for s in strokes]


def test_slovak_text_with_newline_is_code_units_plus_enter():
    assert _view(plan("Žltý kôň\n")) == [
        ("Ž", None, (0x017D,)),
        ("l", None, (0x006C,)),
        ("t", None, (0x0074,)),
        ("ý", None, (0x00FD,)),
        (" ", None, (0x0020,)),
        ("k", None, (0x006B,)),
        ("ô", None, (0x00F4,)),
        ("ň", None, (0x0148,)),
        ("\n", "enter", ()),
    ]


def test_emoji_is_one_stroke_of_a_surrogate_pair():
    assert _view(plan("😀")) == [("😀", None, (0xD83D, 0xDE00))]
    assert utf16_units("😀") == (0xD83D, 0xDE00)
    assert utf16_units("€") == (0x20AC,)


def test_tab_is_the_tab_key():
    assert _view(plan("a\tb")) == [("a", None, (0x61,)), ("\t", "tab", ()), ("b", None, (0x62,))]


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_every_line_break_form_is_one_enter(newline):
    assert _view(plan(f"a{newline}b")) == [("a", None, (0x61,)), (newline, "enter", ()), ("b", None, (0x62,))]


def test_crlf_pair_stays_one_enter_and_lone_cr_lf_are_two():
    assert [s.key for s in plan("\r\n\r\n")] == ["enter", "enter"]
    assert [s.key for s in plan("\n\r")] == ["enter", "enter"]  # LF then a lone CR: two line breaks


def test_empty_text_is_no_strokes():
    assert plan("") == []


# --- macOS merges characters into runs of at most N UTF-16 units ---


def test_runs_merge_characters_up_to_the_unit_limit():
    assert _view(plan("Žltý kôň\n", max_units=20)) == [
        ("Žltý kôň", None, (0x017D, 0x6C, 0x74, 0xFD, 0x20, 0x6B, 0xF4, 0x148)),
        ("\n", "enter", ()),
    ]


def test_runs_split_at_the_limit():
    assert [s.source for s in plan("a" * 25, max_units=20)] == ["a" * 20, "a" * 5]


def test_runs_never_split_a_surrogate_pair():
    # 19 units + a 2-unit emoji would be 21 > 20: the emoji starts the next run, whole
    assert [s.source for s in plan("a" * 19 + "😀b", max_units=20)] == ["a" * 19, "😀b"]
    assert [s.source for s in plan("a😀", max_units=2)] == ["a", "😀"]
    assert [s.source for s in plan("ab😀c", max_units=3)] == ["ab", "😀c"]


def test_special_keys_break_runs():
    assert [(s.source, s.key) for s in plan("ab\tcd", max_units=20)] == [
        ("ab", None),
        ("\t", "tab"),
        ("cd", None),
    ]


@pytest.mark.parametrize(
    "text",
    ["Žltý kôň ďakuje – €", "a\r\nb\rc\nd\te", "😀👍🏽 ľščťž", "x" * 47, "\r", "\r\r\n\n\t"],
)
@pytest.mark.parametrize("max_units", [1, 2, 20])
def test_plan_never_drops_a_character(text, max_units):
    strokes = plan(text, max_units=max_units)
    assert "".join(s.source for s in strokes) == text
    assert sum(s.chars for s in strokes) == len(text)
    assert all(len(s.units) <= max(max_units, 2) for s in strokes if s.key is None)


# --- the tool's method + the reply counts what was really delivered ---


@pytest.mark.parametrize(("method", "name"), [("keys", "keys"), (" Paste ", "paste"), ("", "keys")])
def test_method_is_keys_or_paste(method, name):
    assert check_method(method) == name


def test_unknown_method_is_an_error():
    with pytest.raises(ValueError, match=r"^method must be 'keys' or 'paste' \(got 'clipboard'\)$"):
        check_method("clipboard")


def test_reply_counts_the_delivered_characters():
    assert typed_reply(15, 15) == "Typed 15 chars"
    assert typed_reply(0, 0) == "Typed 0 chars"


def test_delivered_count_short_of_the_text_is_an_error():
    with pytest.raises(RuntimeError, match=r"^delivered 3 of 15 chars$"):
        typed_reply(3, 15)

"""One numbered set of marks from elements and OCR words (#36): the pure merge."""

from __future__ import annotations

import pytest

from remoteos.marks import ELEMENT, MAX_MARKS, OCR, Word, covered, join_words, merge


def element(left, top, right, bottom, text="Save", role="Button", **extra):
    rect = {"left": left, "top": top, "right": right, "bottom": bottom}
    return {"index": 1, "class": role, "text": text, "rect": rect, **extra}


def word(left, top, right, bottom, text="Play"):
    return Word(text, (left, top, right, bottom))


# -- a word against the elements ---------------------------------------------------------------


@pytest.mark.parametrize(
    "box, expected",
    [
        ((10, 10, 30, 30), True),  # inside
        ((90, 10, 110, 30), False),  # exactly half outside: "more than half" is not met
        ((89, 10, 109, 30), True),  # just over half inside
        ((91, 10, 111, 30), False),
        ((200, 10, 220, 30), False),
    ],
)
def test_a_word_is_covered_by_more_than_half_of_its_own_area(box, expected):
    assert covered(word(*box), [(0, 0, 100, 100)]) is expected


def test_two_elements_each_holding_a_part_do_not_add_up():
    assert not covered(word(0, 0, 100, 20), [(0, 0, 40, 20), (60, 0, 100, 20)])


def test_a_word_on_an_element_adds_no_mark_and_a_free_word_does():
    found = merge([element(0, 0, 100, 40)], [word(10, 10, 50, 30, "Save"), word(200, 10, 250, 30, "Play")])
    assert [(m.source, m.name) for m in found.marks] == [(ELEMENT, "Save"), (OCR, "Play")]


def test_a_mark_keeps_the_element_s_role_and_ref_and_a_word_has_neither():
    found = merge([element(0, 0, 100, 40, ref="7.1")], [word(200, 10, 250, 30)])
    first, second = found.marks
    assert (first.role, first.ref) == ("Button", "7.1")
    assert (second.role, second.ref) == ("text", None)


def test_no_elements_at_all_still_gives_marks():
    assert [m.source for m in merge([], [word(0, 0, 20, 20)]).marks] == [OCR]


def test_no_words_keeps_the_elements_as_they_were():
    assert [m.source for m in merge([element(0, 0, 20, 20)], []).marks] == [ELEMENT]


def test_a_word_with_no_area_is_no_mark():
    assert merge([], [word(10, 10, 10, 30)]).marks == ()


# -- dedupe ---------------------------------------------------------------------------------------


def test_the_same_element_twice_is_one_mark():
    assert len(merge([element(0, 0, 50, 20), element(0, 0, 50, 20)], []).marks) == 1


def test_two_elements_on_one_rectangle_with_other_names_stay_two():
    assert len(merge([element(0, 0, 50, 20), element(0, 0, 50, 20, text="Other")], []).marks) == 2


def test_the_same_word_twice_is_one_mark():
    assert [m.name for m in merge([], [word(0, 0, 30, 20), word(0, 0, 30, 20)]).marks] == ["Play"]


# -- joining words into lines --------------------------------------------------------------------


def test_close_words_on_a_line_join_in_reading_order_whatever_order_they_came_in():
    words = [word(62, 0, 100, 20, "bank"), word(0, 0, 60, 20, "Sample")]
    assert join_words(words) == [Word("Sample bank", (0, 0, 100, 20))]


def test_a_wide_gap_is_two_labels():
    assert len(join_words([word(0, 0, 40, 20), word(60, 0, 100, 20)])) == 2


@pytest.mark.parametrize("gap, lines", [(16, 1), (17, 2)])
def test_the_gap_limit_is_0_8_of_the_word_height(gap, lines):
    assert len(join_words([word(0, 0, 40, 20), word(40 + gap, 0, 80 + gap, 20)])) == lines


def test_two_rows_never_join():
    assert len(join_words([word(0, 0, 40, 20), word(42, 22, 80, 42)])) == 2


def test_a_taller_neighbour_on_the_same_middle_joins():
    assert len(join_words([word(0, 4, 40, 16), word(42, 0, 80, 20, "Ág")])) == 1


def test_a_line_grows_over_its_whole_length():
    words = [word(0, 0, 30, 20, "a"), word(35, 0, 60, 20, "b"), word(65, 0, 90, 20, "c")]
    assert join_words(words) == [Word("a b c", (0, 0, 90, 20))]


def test_a_word_an_element_covers_does_not_stretch_the_line_around_it():
    # "Save" is on the button; "as" and "copy" beside it must not join across the button
    found = merge(
        [element(50, 0, 90, 20)],
        [word(10, 0, 45, 20, "as"), word(55, 0, 85, 20, "Save"), word(95, 0, 130, 20, "copy")],
    )
    assert sorted(m.name for m in found.marks if m.source == OCR) == ["as", "copy"]


# -- numbering ------------------------------------------------------------------------------------


def test_numbers_run_top_left_to_bottom_right_across_both_sources():
    found = merge(
        [element(300, 200, 350, 220, text="third"), element(10, 0, 60, 20, text="first")],
        [word(10, 200, 60, 220, "second"), word(300, 400, 350, 420, "fourth")],
    )
    assert [(m.number, m.name) for m in found.marks] == [(1, "first"), (2, "second"), (3, "third"), (4, "fourth")]


def test_a_row_with_uneven_tops_reads_left_to_right():
    found = merge([], [word(300, 32, 340, 48, "b"), word(100, 34, 140, 50, "a")])
    assert [m.name for m in found.marks] == ["a", "b"]


def test_the_same_screen_gets_the_same_numbers_whatever_order_it_was_read_in():
    words = [word(10 * i * 10, 40 * i, 10 * i * 10 + 40, 40 * i + 20, f"w{i}") for i in range(1, 8)]
    assert merge([], words) == merge([], list(reversed(words)))


# -- the bound ------------------------------------------------------------------------------------


def _row(count):
    return [word(0, 40 * i, 40, 40 * i + 20, f"w{i}") for i in range(count)]


def test_exactly_the_limit_is_complete_and_one_more_is_reported():
    assert merge([], _row(MAX_MARKS)).dropped == 0
    found = merge([], _row(MAX_MARKS + 1))
    assert (len(found.marks), found.dropped) == (MAX_MARKS, 1)


def test_a_higher_limit_never_passes_the_bound():
    assert len(merge([], _row(MAX_MARKS + 5), limit=MAX_MARKS + 5).marks) == MAX_MARKS


def test_elements_outlast_words_at_the_limit_wherever_they_are():
    found = merge([element(0, 900, 50, 920, text="low")], _row(3), limit=2)
    assert [m.name for m in found.marks] == ["w0", "low"]
    assert found.dropped == 2


def test_more_elements_than_the_limit_are_cut_and_counted():
    elements = [element(0, 40 * i, 50, 40 * i + 20, text=f"e{i}") for i in range(4)]
    found = merge(elements, [word(100, 0, 140, 20)], limit=2)
    assert [m.name for m in found.marks] == ["e0", "e1"]
    assert found.dropped == 3

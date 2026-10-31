---
paths:
  - "src/remoteos/marks.py"
  - "src/remoteos/ocr.py"
  - "src/remoteos/tools/media.py"
  - "tests/test_marks.py"
  - "tests/test_ocr_words.py"
  - "tests_windows/test_ocr_windows.py"
---

# Marks and OCR word boxes (#36) — gotchas

- **Split:** `marks.py` is pure (word vs element, joining, numbering, the bound); `ocr.read_words`
  gives `Word`s in screen coordinates; `snapshot.draw_marks`/`mark_line` render in image pixels;
  AnnotatedSnapshot is the only tool surface. YOLO for unlabelled icons stayed out (#27).
- **The word boxes come from their own capture**: `capture_region(frame bbox)` at native size through
  `_capture_with_retry`, never the image fitted to `max_width`. `coords.capture_box_to_screen` scales by
  the capture's own size, because macOS gives pixels for a bbox in points.
- **A word is filtered before joining.** Joined first, a line could swallow the words on a button.
  Free words either side of an element narrower than the gap limit still join.
- **An element over 100 times a word's area is a surface and covers nothing (`_SURFACE`).** `listed()`
  takes any focusable element, so one pane over a custom-drawn app would hide all of its labels.
- **Numbers are by rows from each row's first top (`_reading_order`)**, not a fixed grid: on a grid
  tops 31 and 32 are two rows. A row's edge still exists: tops exactly 16 and 17 below its first top
  fall on either side, so numbers are stable away from an edge, not in every case. A mark is a
  point to click now, never an id to keep.
- **`_SURFACE` scales with the word**, so a very short word on a wide button can pass as free: a
  stray mark whose point is still on that button. Accepted: comparing to the frame misses panes.
- **Linux walks up to the limit with `max_nodes` and no time budget**, and a stop at `max_nodes`
  is not reported. Check its time in the live run on a big AT-SPI app.
- **Elements go in first at the limit**, then numbering sorts all of them. Cutting after the sort
  would lose elements at the bottom of the screen to words at the top.
- **`max_elements` bounds marks** (default 300, was 30 elements) and is the elements' limit too. A
  cut list is reported: Windows gives the walk's own reason, the Linux list only its size at the limit.
- **OCR failing costs the marks, never the image**: `_read_words` catches, logs the type, and the
  reply has an `**OCR:**` line. The word texts are screen content: logs carry counts only (#31).
- **`--psm 11` is two argv items for the CLI** and one config string for pytesseract. As one item
  tesseract refuses it (found on real tesseract; a fake answers anything).
- **Languages:** `choose_languages` serves the text path too, so `slk+eng` works on a box without
  `slk`. `run_ocr` stays the str for WaitFor: a note in the text would be matched as screen text.
  An empty `--list-langs` is "unknown": the request goes through and tesseract decides.
- **The WinRT script answers JSON and waits through `AsTask`.** One engine = one language.
  `AvailableRecognizerLanguages` gives tags (`sk-SK`), compared on the part before the dash.
- **Tests:** a fake `subprocess.run`/`pytesseract`/PowerShell reply per backend in
  `tests/test_ocr_words.py`, the tools on the fake X11 of `test_coordinate_contract_tools`. CI's
  Linux runner may have no tesseract, so a real one is a manual run. `tests_windows/test_ocr_windows.py`
  runs the real script up to the decoded bitmap at least. A runner with no OCR language proves only
  that part: `RecognizeAsync` and the JSON then rest on the live check.
- **Live (supervisor, after deploy):** on win-resolume or win-ableton-foh, `AnnotatedSnapshot` of
  Resolume/Ableton shows a labelled button as an `ocr text` mark, and `Click` at its point hits it;
  on a Slovak label the diacritics are read; `OCR()` on a box without Slovak says so.

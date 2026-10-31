---
paths:
  - "src/remoteos/input_keys.py"
  - "src/remoteos/tools/desktop.py"
  - "src/remoteos/platform/*/desktop.py"
  - "src/remoteos/platform/linux/x11_input.py"
  - "src/remoteos/ocr.py"
  - "tests/test_input_actions.py"
---

# Computer-use actions (#34) — gotchas

- **`Click` goes through the provider on all three OSes:** `desktop.click(x, y, button, action)`.
  `Shortcut`, plain `Move` and drag on Windows/macOS stay in the tool (pyautogui). A test that
  fakes pyautogui for Click patches the provider's `pyautogui` too.
- **Held keys = `input_keys.held_keys(keys, desktop)`, the only way to hold a key.**
  - It calls the provider's `key_down` / `key_up`. Each pressed key is released, in reverse order,
    on a failing body, a failing later press, and when another release fails.
  - After a good body a failed release raises. After a failed body the body's error stays the one
    reported and the release failure is logged.
  - Key names go through `input_keys.canonical` (`win`/`cmd` = `super`). Provider key tables are
    keyed on that name. On Linux `_map_key` maps it to the keysym: keysyms are case-sensitive, so
    `f5` must become `F5`.
- **Arguments are checked before any input:** `check_click`, `check_modifiers`, `check_repeat`,
  `hold_seconds`. `modifiers` takes only ctrl/shift/alt/super, so macOS always has a flag for them.
- **Windows:** `key_down`/`key_up` and `action="down"/"up"` are `SendInput`: 0 inserted raises.
  `pyautogui.keyDown`/`mouseDown` report nothing. SendInput's button is physical, so
  `SM_SWAPBUTTON` (metric 23) swaps left and right first. `super` and the navigation keys need
  `KEYEVENTF_EXTENDEDKEY`. A key outside the table raises `Unknown key`.
- **macOS: a posted mouse or wheel event does not pick up a posted modifier key.**
  - `_held` tracks the keys down; `_apply_held_flags` sets `CGEventSetFlags` on each mouse, wheel
    and key event. pyautogui's events carry no flags, so a click under a modifier and
    `action="down"/"up"` are Quartz (`_post_mouse`). A plain click stays pyautogui's.
  - `kCGMouseEventClickState` (field 1) must count: 1, 1, 2, 2, 3, 3 is a triple click.
  - With a button held, a plain moved event is no drag: `Move` calls `move_held`, which posts the
    button's dragged event (6/7/27).
  - Key codes are the ANSI layout's `kVK_*`.
- **Linux:** `xdotool keydown` / `mousedown` outlive the xdotool process, so a press and its release
  are separate calls. `xdotool key --repeat N` is the repeat.
- **Wait and WaitFor are `ToolCategory.WAIT`:** no desktop lock, 10 slots of their own, so a wait
  starves neither input nor queries. `HoldKey` is DESKTOP: it is input.
- **WaitFor calls `ocr.run_ocr` and `enumerate_windows` directly**, not the wrapped tools. Its
  preflight is `"capture"` for text, `"windows"` for a window, as Snapshot's. An OCR failure ends the
  wait at once; only "not found yet" polls. `run_ocr` has no word boxes, so `text` reports the monitor
  and the line, no pixels. Its `timeout` is in `_RUNTIME_ARGS`, so the stale sweep leaves it alone.
- **OCR grabs with `all_screens=True` (`ocr._grab`).** Without it a non-primary Windows monitor is
  black. A fake of `ImageGrab.grab` for OCR takes `bbox` and `all_screens`.
- **Tests:** `tests/test_input_actions.py` shares the fixtures of `test_coordinate_contract_tools`
  and `test_provider_geometry` by assignment. The Quartz fake records flags as `event["flags"]` and
  the click state as `event["field1"]`. Windows keys are decoded from the raw INPUT bytes.
- **Live smoke (supervisor, after deploy):** on one Windows and one X11 box: Notepad / a text
  editor with words, `Click(action="triple")` selects the line; `Click(modifiers="shift")` extends a
  selection; `HoldKey("x", 1)` repeats x; `Shortcut("backspace", repeat=3)` deletes three;
  `CursorPosition` equals the last Click's image point; `WaitFor(window=...)` on a window opened
  from `Shell`; `Wait(1)` during a running `Snapshot` returns without queueing.

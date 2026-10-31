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
    lower-cased. On Linux `_map_key` maps it to the keysym: `canonical` keeps a non-alias key's case,
    because keysyms are case-sensitive (`Page_Up`), and `f5` must become `F5`.
- **Arguments are checked before any input:** `check_click`, `check_modifiers`, `check_repeat`,
  `hold_seconds`. `modifiers` takes only ctrl/shift/alt/super, so macOS always has a flag for them.
  `held_keys` calls the provider's `check_key` for every key before the first press (Windows, macOS).
  Linux cannot know a keysym beforehand: xdotool prints `No such key name` and EXITS 0, so
  `_key_command` reads stderr; the keys pressed before it are released as usual.
- **Windows:** `key_down`/`key_up` and `action="down"/"up"` are `SendInput`: 0 inserted raises.
  `pyautogui.keyDown`/`mouseDown` report nothing. `left` is the PHYSICAL button in both
  pyautogui's click and SendInput, so `SM_SWAPBUTTON` is deliberately not read: a click and a
  down/up mean the same button. `super` and the navigation keys need `KEYEVENTF_EXTENDEDKEY`. A key
  outside the table (punctuation) raises `Unknown key`.
- **macOS: a posted mouse or wheel event does not pick up a posted modifier key.**
  - `_held` tracks the keys down; `_apply_held_flags` sets `CGEventSetFlags` on each mouse, wheel
    and key event. pyautogui's events carry no flags, so a click under a modifier and
    `action="down"/"up"` are Quartz (`_post_mouse`). A plain single click stays pyautogui's.
  - `kCGMouseEventClickState` (field 1) must count: 1, 1, 2, 2, 3, 3 is a triple click. pyautogui
    never sets it, so a plain double or triple click is Quartz too.
  - With a button held, a plain moved event is no drag: `Move` and hover call `move_held`, and
    `_post_mouse` starts with the dragged event (6/7/27). `move_held` is one event: it ignores
    `Move`'s `duration`.
  - Key codes are the ANSI layout's `kVK_*`.
- **Linux:** `xdotool keydown` / `mousedown` outlive the xdotool process, so a press and its release
  are separate calls. `xdotool key --repeat N` is the repeat.
- **Wait and WaitFor are `ToolCategory.WAIT`:** no desktop lock, 10 slots of their own, so a wait
  starves neither input nor queries. `HoldKey` is DESKTOP: it is input.
- **WaitFor calls `ocr.run_ocr` and the provider's `list_windows` directly**, not the wrapped tools.
  Its preflight is `"capture"` for text, `"windows"` for a window, as Snapshot's. A failure ends the
  wait at once; only "not found yet" polls. That is why it is `list_windows`: the Linux
  `enumerate_windows` swallows a wmctrl failure into `[]`, and each failed poll would re-detect the
  session. Without the desktop lock these tools run next to `_load_atspi`'s temporary `os.environ`
  keys, so `session._session_env` copies through `shell_env._snapshot`. `run_ocr` has no word boxes, so `text` reports the monitor
  and the line, no pixels. Its `timeout` is in `_RUNTIME_ARGS`, so the stale sweep leaves it alone.
- **OCR grabs with `all_screens=True` (`ocr._grab`).** Without it a non-primary Windows monitor is
  black. So a default `OCR()` on Windows reads every monitor, as Snapshot does; the WinRT engine has
  an image-size limit (`OcrEngine.MaxImageDimension`) that a very wide desktop can hit, and that fails
  loudly. A fake of `ImageGrab.grab` for OCR takes `bbox` and `all_screens`.
- **Tests:** `tests/test_input_actions.py` shares the fixtures of `test_coordinate_contract_tools`
  and `test_provider_geometry` by assignment. The Quartz fake records flags as `event["flags"]` and
  the click state as `event["field1"]`. Windows keys are decoded from the raw INPUT bytes.
- **Live smoke (supervisor, after deploy):** on one Windows and one X11 box: Notepad / a text
  editor with words, `Click(action="triple")` selects the line; `Click(modifiers="shift")` extends a
  selection; `HoldKey("x", 1)` repeats x; `Shortcut("backspace", repeat=3)` deletes three;
  `CursorPosition` equals the last Click's image point; `WaitFor(window=...)` on a window opened
  from `Shell`; `Wait(1)` during a running `Snapshot` returns without queueing.

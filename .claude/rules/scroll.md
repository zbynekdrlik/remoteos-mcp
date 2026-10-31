---
paths:
  - "src/remoteos/scroll.py"
  - "src/remoteos/tools/desktop.py"
  - "src/remoteos/platform/*/desktop.py"
  - "src/remoteos/platform/linux/x11_input.py"
  - "tests/test_scroll.py"
  - "tests/test_provider_geometry.py"
---

# Mouse wheel / Scroll (#29) — gotchas

- **One unit everywhere: wheel CLICKS (notches) and a direction** (`up`/`down`/`left`/`right`).
  - `remoteos.scroll` is pure. `normalize_request` handles the tool's arguments, including the
    legacy signed `amount` + `horizontal`, where positive = up/right and an explicit `direction`
    wins. `wheel_steps` validates the direction and clamps to 1..100.
  - Every provider has the same `scroll(direction, clicks, x=None, y=None)`. A point moves the
    cursor only when it `is not None`. Never use `if x and y`: x=0 is the left screen column.
  - The tool passes `steps.requested` to the provider, so the reply can say "clamped".
- **Never route the wheel through pyautogui 0.9.54.**
  - On Windows it sends the raw count as the wheel delta: 3 = 3/120 of a notch, which apps ignore.
  - Its `hscroll` calls the vertical `_scroll`.
  - On macOS its negative horizontal remainder flips sign.
- **Windows: one `SendInput` of ±clicks × `WHEEL_DELTA` (120).**
  - `MOUSEEVENTF_WHEEL` (0x0800): positive = up. `MOUSEEVENTF_HWHEEL` (0x1000): positive = right.
  - 100 clicks = 12000, inside the signed 16-bit delta in `HIWORD(wParam)`. That is why the clamp
    is 100.
  - The ctypes `INPUT` uses fixed-width fields. `mouseData` is declared `c_int32` (the same bits as
    the DWORD), because a wheel delta is signed.
  - The union holds `MOUSEINPUT` and `KEYBDINPUT` (#30). `MOUSEINPUT` is the largest member, so
    `sizeof(INPUT)` is 40 on x64 and 28 on x86. SendInput rejects any other `cbSize`, and a
    test locks it.
  - `SetCursorPos` runs first. The wheel goes to the window under the cursor.
  - SendInput returning 0 (a locked or disconnected session) is raised as an error, never ignored.
  - A UIPI block is SILENT: when a higher-integrity window is under the cursor, SendInput still
    returns 1 (Microsoft docs). Never claim UIPI in an error message.
  - The error text carries no `GetLastError` code. `ctypes.windll`'s last error can be
    overwritten before it is read; `use_last_error` would need `WinDLL`, which the tests do not
    fake.
- **macOS builds every event before posting any.** A `None` from `CGEventCreateMouseEvent` /
  `CGEventCreateScrollWheelEvent` raises, and nothing is posted.
- **macOS: Quartz line events, one line per click.**
  - wheel1 = vertical, positive = up. **wheel2 positive = LEFT.** Hammerspoon passes its offsets
    straight to `CGEventCreateScrollWheelEvent` and documents "positive values scroll up or left".
    So `right` posts a negative wheel2.
  - At most 10 lines per event. Apple: "typically -10..+10, large values may have unexpected
    results".
  - With a point, post a `kCGEventMouseMoved` there first and `CGEventSetLocation` each scroll
    event.
- **Linux:** xdotool buttons 4 = up, 5 = down, 6 = left, 7 = right, with `click --repeat N`.
- **Tests:** they fake only the OS boundary.
  - Windows: the `win` fixture's `ctypes.windll` records `SetCursorPos` and `SendInput` and reads
    the INPUT array (`ctypes.windll.input.log`).
  - macOS: the `mac` fixture's fake `Quartz` records `posted` events as dicts.
  - Expected values (0x0800, 120, 40/28) are literals in the tests, never imported from the code
    under test.
- **Live check on Windows (non-destructive):** in a background PowerShell, start a `WH_MOUSE_LL`
  hook that logs `WM_MOUSEWHEEL` / `WM_MOUSEHWHEEL` deltas. Then run `Scroll(direction="down",
  amount=3)` and expect −360, and `Scroll(direction="right", amount=2)` and expect +240.

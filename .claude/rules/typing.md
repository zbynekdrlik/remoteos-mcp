---
paths:
  - "src/remoteos/typing_input.py"
  - "src/remoteos/paste.py"
  - "src/remoteos/__main__.py"
  - "src/remoteos/platform/*/desktop.py"
  - "tests/test_typing_input.py"
  - "tests/test_provider_geometry.py"
  - "tests/test_linux_desktop.py"
---

# Typing text / Type tool (#30) — gotchas

- **Never type text through pyautogui 0.9.54.** `write` is an alias of `typewrite`. Its key
  map covers only `chr(32..127)` and SILENTLY skips everything else, on Windows and macOS.
  That is how `Type("Žltý kôň")` dropped the diacritics and still replied `Typed 8 chars`.
  pyautogui stays only for click / select-all / Delete / Enter around the text.
- **`remoteos.typing_input` is pure.**
  - `plan(text, max_units)` splits text into `Stroke`s: Unicode text as UTF-16 code units,
    or a special key. `\n`, `\r\n` and a lone `\r` are ONE Enter each; `\t` is Tab.
  - A stroke keeps its `source` text. That is what lets the reply count delivered
    characters and what the never-drops-a-character test checks.
  - A character is never split, so a surrogate pair always stays whole. The Windows default
    is `max_units=1`, one stroke per character.
- **Windows: one `SendInput` per character.**
  - Text: `KEYEVENTF_UNICODE` (0x0004), `wVk` = 0, `wScan` = the code unit, a down then an
    up (`| KEYEVENTF_KEYUP` 0x0002) per code unit. An emoji is 4 events in ONE call.
  - Enter / Tab: `VK_RETURN` 0x0D / `VK_TAB` 0x09 with `wScan` = 0.
  - `KEYBDINPUT` sits in the `INPUT` union next to `MOUSEINPUT`. MOUSEINPUT is still the
    largest member, so `sizeof(INPUT)` stays 40 (x64) / 28 (x86).
  - A short return raises an error that names the characters already delivered. A UIPI
    block is silent here too: SendInput still succeeds.
  - 10 ms between characters.
- **macOS: keyboard events with keycode 0 + `CGEventKeyboardSetUnicodeString`.**
  - The string is set on both the down and the up event.
  - At most 20 UTF-16 units per event: Quartz truncates a longer string.
  - Return / Tab are real keycodes (`kVK_Return` 0x24, `kVK_Tab` 0x30).
  - Every event is built before any is posted, as in the scroll code.
  - The edit-shortcut modifier is **Command**. Each provider has a `SHORTCUT_MODIFIER`
    (win `ctrl`, mac `command`). Ctrl+A in Cocoa moves the caret to the paragraph start, so
    the old `clear` deleted one character.
- **The reply is the delivered count.** Providers return how many characters they handed to
  the OS. The tool runs `typed_reply(delivered, len(text))`, and any shortfall is an error,
  never `Typed N chars`.
- **`method="paste"` = `remoteos.paste.paste_text`, shared by all three OSes.**
  - It goes through the providers' existing `get_clipboard` / `set_clipboard`. Those REPORT a
    failure as an `Error: …` string and do not raise. A failed read means "no text to
    restore"; a failed write raises before the paste keys are pressed.
  - It waits 0.3 s before restoring, because the target app reads the clipboard when it
    handles the paste keys.
  - Only text survives. macOS `pbpaste` cannot tell an empty clipboard from a non-text one.
  - `method` is validated before anything is clicked or typed.
- **Tests fake only the OS boundary.**
  - `_FakeInput.raw` keeps each `SendInput` call's INPUT bytes. `_key_calls` decodes them with
    the Win32 offsets (union at 8 on x64 / 4 on x86; `wVk`, `wScan` WORDs, then `dwFlags`),
    not with our field names.
  - `_FakeClipboard` stands in for `win32clipboard`. The fake Quartz records keyboard events as
    dicts.
  - Expected values are literals (0x0004, 0x017D for Ž, 0xD83D/0xDE00).
- **Live check on Windows (non-destructive):** open Notepad, run `Type("Žltý kôň ďakuje – €")`,
  read the edit control back with `WM_GETTEXT` / UIA, and expect an exact match. Close
  without saving.

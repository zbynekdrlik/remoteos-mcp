---
paths:
  - "src/remoteos/typing_input.py"
  - "src/remoteos/text_entry.py"
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
  - In the win/mac `type_text`, pyautogui only clicks and presses named keys: select-all,
    Delete, the paste keys and Enter.
- **One provider call, like Scroll.**
  - The tool validates `method`, maps the point, and calls
    `get_desktop().type_text(text, x, y, clear, press_enter, method)`.
  - A point is used only when it `is not None`. x=0 or y=0 is a real screen row/column.
  - Every provider also has `type_unicode(text) -> int`, the Unicode-safe primitive
    (#34/#35 build on it).
  - The text step is shared: `remoteos.text_entry.enter_text` runs keys → `type_unicode` +
    `typed_reply`, or paste → `paste_text`.
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
  - The string is set on both the down and the up event; the length is in UTF-16 units, as
    pynput passes it.
  - At most 20 UTF-16 units per event: Quartz truncates a longer string.
  - Return / Tab are real keycodes (`kVK_Return` 0x24, `kVK_Tab` 0x30).
  - `CGEventPost` returns nothing, and without the posting permission macOS drops events
    SILENTLY. pyautogui's click and Command+V post the same way. So `type_text` checks
    `CGPreflightPostEventAccess()` (macOS 10.15+) BEFORE the click, clear or paste, and
    `type_unicode` checks again for direct callers. When it is false they raise and nothing
    is clicked, typed or pasted.
  - A locked console still drops events unseen. Since #32 the Type tool's preflight refuses
    it first: `diagnose()` checks `CGSSessionScreenIsLocked` and the same
    `CGPreflightPostEventAccess`. See `.claude/rules/tool-errors.md`.
  - Every event is built before any is posted, as in the scroll code.
  - The edit-shortcut modifier is **Command** (`_SHORTCUT_MODIFIER`). Ctrl+A in Cocoa moves
    the caret to the paragraph start, so the old `clear` deleted one character.
- **The reply is the delivered count.** `typed_reply(delivered, len(text))`: any shortfall is
  an error, never `Typed N chars`.
- **`method="paste"` = `text_entry.paste_text`.**
  - It goes through the providers' existing `get_clipboard` / `set_clipboard`. Those RAISE on
    failure (#32). Clipboard text that starts with `Error:` is ordinary text. A clipboard
    without text reads as `""` on every OS: Windows checks `IsClipboardFormatAvailable`, and
    Linux maps xclip's "target … not available" to `""`.
  - A failed (raising) or EMPTY read means "no text to restore". macOS `pbpaste` prints `""`
    for an image too, so "restoring" `""` would only wipe it.
  - A failed write raises before the paste keys are pressed. The `finally` restores even when
    the paste keys fail.
  - It waits 0.3 s before restoring, because the target app reads the clipboard when it
    handles the paste keys.
  - Never log clipboard content or the typed text; log only counts and method.
- **Tests fake only the OS boundary.**
  - `_FakeInput.raw` keeps each `SendInput` call's INPUT bytes. `_key_calls` decodes them with
    the Win32 offsets (union at 8 on x64 / 4 on x86; `wVk`, `wScan` WORDs, then `dwFlags`),
    not with our field names.
  - `_FakeClipboard` stands in for `win32clipboard`. The fake Quartz records keyboard events
    and has `CGPreflightPostEventAccess`. `_mac_pasteboard` fakes `pbcopy`/`pbpaste`.
  - The tool-layer fixtures patch BOTH `server.pyautogui` and the provider's `pyautogui`.
  - Expected values are literals (0x0004, 0x017D for Ž, 0xD83D/0xDE00).
- **Live checks (non-destructive):**
  - **Windows:** open Notepad, run `Type("Žltý kôň ďakuje – €")`, read the edit control back
    with `WM_GETTEXT` / UIA, and expect an exact match. Close without saving.
  - **macOS:** TextEdit, `Type("Žltý kôň 😀")`, read the text back with AppleScript.

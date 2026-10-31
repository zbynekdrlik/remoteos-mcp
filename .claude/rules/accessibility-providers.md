---
paths:
  - "src/remoteos/element_tree.py"
  - "src/remoteos/platform/mac/ax.py"
  - "src/remoteos/platform/mac/availability.py"
  - "src/remoteos/platform/linux/windows.py"
  - "tests/test_atspi_tree.py"
  - "tests/test_mac_ax.py"
---

# Element trees on macOS and Linux (#37, #38) — gotchas

- **One shape on three OSes.** Every provider has `element_tree(window)`, `click_element(ref)`,
  `type_element(ref, text)`, `get_interactive_elements(limit=)` and `marked_elements(limit=)`, and
  builds them from `element_tree.py`: `walk`, `RefTable`, `tree_from`, `marked_from`, `listed_elements`,
  the roles of `INTERACTIVE_ROLES` (`_ROLES` maps each OS's names), `gone`, `may_have_happened`.
- **No worker thread, on purpose.** UIA needs one for its COM apartment and uncancellable calls. AX
  objects have no thread and `AXUIElementSetMessagingTimeout` (2 s) bounds a call; AT-SPI calls
  are D-Bus calls with a method timeout. A lock (`_tree_lock`) serializes the refs and the walk.
- **A press that errors is never followed by the mouse**, unless the error is a plain refusal: AX
  `_REFUSED`, AT-SPI `do_action` returning False. A modal dialog blocks the call, a button
  closes its own window, and a second press could do it twice. `may_have_happened` says so.
- **AT-SPI's press is a named action, never index 0.** Qt lists `SetFocus` and `ShowMenu`, and GTK 2's
  `press` is half of `click`. `get_action_name` is not localized, nor is `get_role_name`.
- **AT-SPI root: the X window's process names the application** (`_atspi_root`), then the frame of
  that title, then an ACTIVE or SHOWING frame, then the application. Without a pid only an exact
  title counts: another app's active frame would be the wrong window's list.
- **GTK 3's entry has role `text`**, the role of static text too: `EDITABLE` decides. A read-only
  entry stays `Text` and is listed only when focusable. `EditableText` without `EDITABLE` = read-only.
- **The Linux mouse fallback needs the element's window to be the active one** (`_center`): AT-SPI's
  hit test is not reliable across toolkits. Typing activates the window first, because xdotool types to
  the active window and a FOCUSED state alone can be in the background.
- **A hint is reported when an app shows nothing** (`_enable_hint`), by toolkit. Chromium needs BOTH
  `ACCESSIBILITY_ENABLED=1` and `--force-renderer-accessibility`: with the flag alone real Chrome
  never joined the a11y bus. On the test bus Qt showed its tree without
  `QT_LINUX_ACCESSIBILITY_ALWAYS_ON`; a desktop's bus state may differ.
- **AX never reads `AXValue`:** a field can be a password. `name` = AXTitle, AXDescription, AXPlaceholderValue.
- **`AXValueGetValue`'s return:** the wheel's docstring says `value`, older code unpacked a pair, so
  `_axvalue` takes both. Check it on the first live run on a Mac.
- **`HIServices` is not in Quartz:** `pyobjc-framework-ApplicationServices` (a darwin pin, it brings
  CoreText) has it, and it is imported at call time. Its constants are written as numbers, from `AXError.h`.
- **`AXIsProcessTrusted` shares a pane with posting events and is another check**: `"accessibility"`.
- **Electron on macOS** may need `AXManualAccessibility` set on the app. Not done: nothing to test it on.
- **Tests:** `tests/test_atspi_tree.py` and `tests/test_mac_ax.py` fake only the library, with its real
  call shapes.
- **A real AT-SPI run needs no fleet box:** under `dbus-run-session`, Xvfb, openbox and
  `/usr/libexec/at-spi-bus-launcher --launch-immediately`, then a GTK, Qt or Chrome window and the
  provider functions. Run on dev1 for #38: GTK 3 (click, disabled, read-only, EditableText with
  diacritics, a toggle), Qt 6 (click, EditableText) and Chrome (click in the page; the omnibox fell
  to focus + keys). All passed. wmctrl was absent there, so a shim over xdotool stood in for it.
- **Live (supervisor, after deploy):** strih-pp or stream-pp: `ElementTree` of a GTK app, and of a Qt
  app; `ClickElement` a button and see its effect; `TypeElement` a field and read it back; `Move(drag=True,
  duration=1)` in a drawing app draws a line, not a dot. On a Mac when one is up: `ElementTree` on
  TextEdit, the same two actions, and the refusal text with the permission off.

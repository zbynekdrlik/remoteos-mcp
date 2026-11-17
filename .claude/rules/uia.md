---
paths:
  - "src/remoteos/element_tree.py"
  - "src/remoteos/platform/win/uia.py"
  - "tests/test_element_tree.py"
  - "tests/test_uia_timeouts.py"
  - "tests/uia_fakes.py"
  - "tests_windows/test_uia_windows.py"
---

# UI Automation tree and element refs (#35) — gotchas

- **Split:** `element_tree.py` is pure (what is listed, the walk's bounds, `RefTable`, the shared
  replies `tree_from` / `marked_from` / `listed_elements`); `snapshot.tree_lines` renders;
  `platform/win/uia.py` is the `uiautomation` adapter and the worker. AX and AT-SPI use the same
  module with their own `read` and `children`: `.claude/rules/accessibility-providers.md`.
- **Every UIA object lives on the worker's thread (`UiaWorker`).** UIA objects are apartment-bound.
  `import comtypes` runs `CoInitializeEx` on the IMPORTING thread and the package's
  `_AutomationClient` is a process singleton, so:
  - the import happens in `open_session`, on the worker; importing the package at module level
    would bind COM to the wrong thread;
  - a new worker replaces the singleton in ONE assignment (`client()`, never `_instance = None` then
    `instance()`), under `_client_lock`. The old client goes to `_retired_clients` and is never
    released: its apartment may belong to a hung thread;
  - the package's functions read that singleton, so a late job must end: `Session.abandoned` stops a
    walk at its next element and an action after the call that hung (`_refused`), and a start
    abandoned at `START_TIMEOUT_S` leaves the client alone;
  - an exception leaves the worker `_detached`: its traceback's frames hold controls;
  - `import uiautomation.uiautomation`, the module: the package's `*` leaves out `_AutomationClient`;
  - nothing a job returns may hold a control. Results are plain data, and the ref table is on the
    `Session`, so a restarted worker has no refs: a hung call ends every ref.
- **A hung call cannot be cancelled.** It is abandoned, and its thread ends when the call returns.
  `MAX_STUCK_WORKERS` bounds them. The thread never calls CoUninitialize: a control released
  after it crashes.
- **UI Automation gives up on an app before we do (#114).** The client is `CUIAutomation8` as
  `IUIAutomation2` (installed into the package's client: `IUIAutomation` + `ViewWalker`, the only
  client members the package reads besides `UIAutomationCore`; it derives from `IUIAutomation`).
  Windows 8+ only, no fallback: the fleet is Win10/11 + Server 2019. The default
  `TransactionTimeout` is 20 s, longer than every budget, so before #114 an app that did not answer
  ALWAYS abandoned the worker (measured on windows-latest: a busy UI thread → 15.0 s + a stuck
  thread; bounded → a COMError in about 1 s, worker alive).
  - `UiaWorker(answer_within=Session.answer_within)` gives each job its call's remaining budget
    minus `ANSWER_MARGIN_S`. The timeout applies to EACH call into the app, so every call goes
    through `Session._ask(call)`, which first re-sets it to what the job has left (a put on our own
    client, no call into the app), never under `MIN_ANSWER_MS`. The floor only lets a responsive
    app answer at the end of a job; a hung app late in a job is left to the abandon backstop. A
    new call into the app that skips `_ask` gets a stale limit: the ledger tests catch it. The
    walk's deadline includes the window lookup.
  - **Never the package's `Control` wrappers for navigation, patterns or focus.** Its
    `GetFirstChildControl` / `GetNextSiblingControl` / `GetParentControl` / `ControlFromHandle` /
    `ControlFromPoint` / `GetFocusedControl` are TWO calls into the app (the element, then
    `CurrentControlType` in `CreateControlFromElement`) under one limit, and its `GetPattern` /
    `SetFocus` SWALLOW every COM error (None / False): a timeout there would send ClickElement to
    the mouse and TypeElement to the keys. The session uses its own `automation` (IUIAutomation2:
    `ElementFromHandle`, `ElementFromPoint`, `GetFocusedElement`) and `walker` (`RawViewWalker`),
    `Session._control(find)` = the element and its control, each in its own `_ask`, and the raw
    `Element.GetCurrentPattern` + `auto.CreatePattern` / `Element.SetFocus`. A refused pattern
    lookup is still `None` and a refused SetFocus still goes on to the focus check, as the
    package did; only a timeout is `UiaUnanswered`. `IsWindowVisible` is user32, not UIA.
  - Its timeout HRESULTs (`_TIMED_OUT`: UIA_E_TIMEOUT 0x80131505, ERROR_TIMEOUT 0x800705B4) on a
    READ are `UiaUnanswered` (saying what was done: "nothing was done to it", or for TypeElement's
    focus steps "nothing was typed, the focus may have moved"), never "vanished" (an element skipped
    silently) or "gone" (a ref ended for nothing). On Invoke/SetValue they are
    `UiaActionUnanswered` "may have happened", never a fallback to the mouse or the keys.
    ClickElement/TypeElement pass both through as they are. The abandon stays the backstop.
  - Any other COM error of the window lookup (`Session._window`: `ElementFromHandle`, then its
    control type) is `UiaLookupFailed` "UI Automation failed while <step> (HRESULT 0x…): nothing was
    done to it", raised `from None` (#116): never the raw COMError, whose text names no step.
    ElementTree shows it as `ElementTree error: …`; Snapshot/AnnotatedSnapshot still drop the list.
  - Tests: `tests/test_uia_timeouts.py` (one case per read site; a fake clock and a ledger that
    checks the limit in force at every call). The fakes are `tests/uia_fakes.py`: they behave like
    the package (GetPattern/SetFocus swallow, navigation is element + type, CreatePattern's
    QueryInterface can be refused). Known limit: a `dead` fake element still navigates.
- **Hanging a test window:** a busy loop on its UI thread (`while ((Get-Date) -lt $end) {}` in a
  forms timer tick). `Start-Sleep` does not hang it: a CLR wait on an STA thread pumps sent
  messages, WM_GETOBJECT included. `SendMessageTimeout(WM_NULL)` tells whether it answers.
- **`UiaNotAsked` (starting, busy, stuck) is not a hang: nothing was asked of any app.** The first
  start ever generates comtypes' wrappers, which takes seconds (into `comtypes/gen`, or
  `%APPDATA%\Python\…\comtypes_cache` when that is not writable). It keeps the starting worker. An
  action reports it as it is, never as "may have happened". `timeout` is one budget for the whole
  call (lock, start, job). `needs` is what the job may legitimately take: a call left less than
  that is not begun, because overrunning abandons a healthy worker and ends every ref.
- **The package's `Logger.Write` writes to `sys.stdout`** (the JSON-RPC stream under stdio) and to
  `@AutomationLog.txt` in the cwd. `open_session` replaces it. Its `print`s are in the bitmap DLL
  loader and behind `debug=True`, neither of which we reach.
- **`import uiautomation` sets process-wide `restype`s** on the shared `ctypes.windll` functions
  (GetForegroundWindow, WindowFromPoint, GetAncestor, SendMessageW, GetDC, OpenProcess...). Nothing in
  `src` calls those through `ctypes.windll`. Set your own `restype` if you add one. It also calls
  `SetProcessDpiAwareness`, which fails harmlessly: per-monitor-v2 is already set (`dpi.py`), so
  `BoundingRectangle` is physical pixels, the screen space of the Windows contract.
- **Refs are `<tree>.<n>`, tree numbers unique in a process and the first one random.** Numbering
  from 1 would make an old ref name another element of the new tree. A ref also carries the
  RuntimeId read at the walk: a mismatch, a COM error reading it, or an invisible window
  (`IsWindowVisible`: a closed window's element may still answer) is "gone". An element with an
  empty RuntimeId gets no ref: nothing could prove later that it is the same one.
- **`Snapshot` reads through `Session.elements`, which publishes no refs; `AnnotatedSnapshot`
  through `Session.marked`, which does (#36)**, so it ends an earlier ElementTree's refs. Both have
  a shorter budget and only the `limit` they show (`get_interactive_elements(limit=)`,
  `marked_elements(limit=)`). An element with no RuntimeId stays a mark with no ref. A UIA failure
  there costs the list, never the image; ElementTree reports it. Every name in a reply goes through
  `element_tree.one_line`: a name can be a whole document, and text of an app must not forge a reply line.
- **ClickElement order:** `_live` (ref, identity, enabled), Invoke. A failed HRESULT raises
  `COMError`, so `Invoke()` returning at all means accepted, whatever it returns (`False` is only
  `S_FALSE`): never act a second time on it. A COM refusal falls back to the mouse. A TIMEOUT never
  does (a button opening a modal dialog blocks the Invoke), nor does a `_WENT_AWAY` HRESULT (a
  button that closes its own window): both say "may have happened". The mouse needs `_center`:
  on-screen, and `ControlFromPoint` at the centre is the element or inside it. Never compare
  HWNDs: an owned dialog has the same root owner, and everything in a Chromium page shares one HWND.
- **TypeElement:** Value pattern = the whole new value; read-only is refused, because keystrokes
  would "succeed" into nothing. Otherwise SetFocus and `_is_or_inside` (the focus on the element or
  inside it), or nothing is typed. The keystrokes run on the caller's thread, so a long text
  cannot overrun the worker's timeout. The package's `Invoke`/`SetValue` sleep 0.5 s unless
  `waitTime=0`.
- **Never log a name's or a text's content:** counts and exception types only (#31).
- **Tests:** `tests/test_element_tree.py` runs the real worker threads; only the package's controls
  and patterns are faked. `tests_windows/test_uia_windows.py` runs the real package against a
  WinForms window in the `windows-powershell` CI job; its `tree_of` retries `UiaUnanswered` too (a
  window started a moment ago on a stalled runner may not answer yet: the #114 flake, where the
  first contact of a fresh window took over 12 s while the same run's next test was 4x slow), never
  a plain `UiaTimeout`: that is an abandoned worker, the bug itself.
- **Live (supervisor, after deploy):** on a Windows box, `ElementTree` of a Chromium window lists
  its buttons (the very first walk can be short: Chromium builds its tree when a UIA client
  appears); `ClickElement` one and see its effect; `TypeElement` a field and read it back.

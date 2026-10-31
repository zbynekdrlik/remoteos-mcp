---
paths:
  - "src/remoteos/element_tree.py"
  - "src/remoteos/platform/win/uia.py"
  - "tests/test_element_tree.py"
  - "tests_windows/test_uia_windows.py"
---

# UI Automation tree and element refs (#35) — gotchas

- **Split:** `element_tree.py` is pure (what is listed, the walk's bounds, `RefTable`);
  `snapshot.tree_lines` renders; `platform/win/uia.py` is the `uiautomation` adapter and the worker.
  #37 / #38 reuse `walk` and `RefTable` with their own `read` and `children`. A provider without
  `element_tree` gets "not available on this OS yet" from the tools (`hasattr`).
- **Every UIA object lives on the worker's thread (`UiaWorker`).** UIA objects are apartment-bound.
  `import comtypes` runs `CoInitializeEx` on the IMPORTING thread and the package's
  `_AutomationClient` is a process singleton, so:
  - the import happens in `open_session`, on the worker; importing the package at module level
    would bind COM to the wrong thread;
  - a new worker replaces the singleton. The old client goes to `_retired_clients` and is never
    released: its apartment may belong to a hung thread;
  - `import uiautomation.uiautomation`, the module: the package's `*` leaves out `_AutomationClient`;
  - nothing a job returns may hold a control. Results are plain data, and the ref table is on the
    `Session`, so a restarted worker has no refs: a hung call ends every ref.
- **A hung call cannot be cancelled.** It is abandoned, and its thread ends when the call returns.
  `MAX_STUCK_WORKERS` bounds them. The thread never calls CoUninitialize: a control released
  after it crashes.
- **"Still starting" (`UiaStarting`) is not a hang.** The first start ever generates comtypes'
  wrappers, which takes seconds (into `comtypes/gen`, or `%APPDATA%\Python\…\comtypes_cache` when
  that is not writable). It keeps the starting worker and, for an action, means nothing happened.
- **The package's `Logger.Write` writes to `sys.stdout`** (the JSON-RPC stream under stdio) and to
  `@AutomationLog.txt` in the cwd. `open_session` replaces it. Its `print`s are only in the bitmap
  DLL loader, which we never touch.
- **`import uiautomation` sets process-wide `restype`s** on the shared `ctypes.windll` functions
  (GetForegroundWindow, WindowFromPoint, GetAncestor, SendMessageW, GetDC, OpenProcess...). Nothing in
  `src` calls those through `ctypes.windll`. Set your own `restype` if you add one. It also calls
  `SetProcessDpiAwareness`, which fails harmlessly: per-monitor-v2 is already set (`dpi.py`), so
  `BoundingRectangle` is physical pixels, the screen space of the Windows contract.
- **Refs are `<tree>.<n>`, the first tree number random.** Numbering from 1 would make an old ref
  name another element of the new tree. A ref also carries the RuntimeId read at the walk:
  a mismatch, or a COM error reading it, is "gone".
- **`Snapshot`/`AnnotatedSnapshot` read through `Session.elements`, which publishes no refs**, with
  a shorter budget. A UIA failure there costs the list, never the image; ElementTree reports it.
- **ClickElement order:** `_live` (ref, identity, enabled), Invoke; a COM refusal or `False` falls
  back to the mouse, a TIMEOUT never does (the Invoke may have run: a button opening a modal dialog
  blocks it). The mouse needs `_center`: on-screen, and `WindowFromPoint`'s root owner equal to the
  listed window's, so an owned popup passes and a covering window refuses.
- **TypeElement:** Value pattern = the whole new value; read-only is refused, because keystrokes
  would "succeed" into nothing. Otherwise SetFocus and `_has_focus` (the focus on the element or
  inside it), or nothing is typed. The keystrokes run on the caller's thread, so a long text
  cannot overrun the worker's timeout. The package's `Invoke`/`SetValue` sleep 0.5 s unless
  `waitTime=0`.
- **Never log a name's or a text's content:** counts and exception types only (#31).
- **Tests:** `tests/test_element_tree.py` runs the real worker threads; only the package's controls
  and patterns are faked. `tests_windows/test_uia_windows.py` runs the real package against a
  WinForms window in the `windows-powershell` CI job.
- **Live (supervisor, after deploy):** on a Windows box, `ElementTree` of a Chromium window lists
  its buttons (the very first walk can be short: Chromium builds its tree when a UIA client
  appears); `ClickElement` one and see its effect; `TypeElement` a field and read it back.

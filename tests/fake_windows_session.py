"""A fake Windows logon session for the desktop-availability probes (#32): the external Win32/WTS boundary.

``FakeWindowsSession`` answers what kernel32 ``ProcessIdToSessionId``, user32
``GetProcessWindowStation`` / ``OpenInputDesktop`` / ``GetUserObjectInformationW`` /
``CloseDesktop`` and wtsapi32 ``WTSQuerySessionInformationW`` / ``WTSFreeMemory``
report for ONE session; ``add_to(windll)`` puts those functions on a fake
``ctypes.windll``. ``tscon(cmd, **kwargs)`` plays ``tscon <id> /dest:console`` for a faked
``subprocess.run``. The WTS buffers use the Win32 offsets as literals, never the
provider's own struct. The default is a HEALTHY session (session 1, WinSta0,
active, input desktop "Default", unlocked).

Also shared by the provider tests: ``FakeClipboard`` (pywin32's clipboard).
"""

from __future__ import annotations

import ctypes
import struct
import types

from fake_console_child import run_result

WTS_CONNECT_STATE, WTS_SESSION_INFO_EX = 8, 25  # WTS_INFO_CLASS
WTS_ACTIVE, WTS_DISCONNECTED = 0, 4  # WTS_CONNECTSTATE_CLASS
LOCK, UNLOCK = 0, 1  # WTSINFOEX_LEVEL1_W.SessionFlags
DESKTOP_HANDLE, WINSTA_HANDLE = 0x1A4, 0x2C8


class Fn:
    """A fake DLL function: callable, records its calls, and takes restype/argtypes like a ctypes one."""

    def __init__(self, impl):
        self.impl = impl
        self.calls: list[tuple] = []
        self.restype = None
        self.argtypes = None

    def __call__(self, *args):
        self.calls.append(args)
        return self.impl(*args)


class FakeWindowsSession:
    """One Windows logon session as the Win32 / WTS APIs report it."""

    def __init__(self):
        self.session_id = 1
        self.window_station = "WinSta0"
        self.connect_state = WTS_ACTIVE
        self.input_desktop: str | None = "Default"  # None = OpenInputDesktop fails (lock screen / UAC)
        self.lock_flags: int | None = UNLOCK  # None = WTSSessionInfoEx fails
        self.fail_session_id = False
        self.tscon_attaches = True  # a tscon of a disconnected session attaches it
        self.buffers: list = []  # keeps the WTS buffers alive until freed
        self.freed: list[int] = []
        self.closed: list[int] = []
        self.open_access: list[int] = []

    # kernel32
    def ProcessIdToSessionId(self, pid, sid_ref):
        if self.fail_session_id:
            raise OSError("ProcessIdToSessionId is gone")
        sid_ref._obj.value = self.session_id
        return 1

    # user32
    def OpenInputDesktop(self, flags, inherit, access):
        self.open_access.append(access)
        return DESKTOP_HANDLE if self.input_desktop is not None else None

    def GetUserObjectInformationW(self, handle, index, buf, size, needed_ref):
        assert index == 2  # UOI_NAME
        buf.value = {DESKTOP_HANDLE: self.input_desktop, WINSTA_HANDLE: self.window_station}[handle.value]
        return 1

    def CloseDesktop(self, handle):
        self.closed.append(handle.value)
        return 1

    # wtsapi32
    def WTSQuerySessionInformationW(self, server_handle, sid, info_class, buf_ref, size_ref):
        assert sid == self.session_id
        if info_class == WTS_CONNECT_STATE:
            raw = struct.pack("<i", self.connect_state)
        elif info_class == WTS_SESSION_INFO_EX:
            if self.lock_flags is None:
                return 0
            # WTSINFOEXW: DWORD Level; then an 8-aligned union (it holds LARGE_INTEGERs) whose
            # WTSINFOEX_LEVEL1_W starts with SessionId, SessionState, SessionFlags.
            raw = struct.pack("<IIIii", 1, 0, sid, self.connect_state, self.lock_flags) + bytes(200)
        else:
            return 0
        buf = ctypes.create_string_buffer(raw, len(raw))
        self.buffers.append(buf)
        buf_ref._obj.value = ctypes.addressof(buf)
        size_ref._obj.value = len(raw)
        return 1

    def WTSFreeMemory(self, pointer):
        self.freed.append(pointer.value if hasattr(pointer, "value") else pointer)

    # tscon <id> /dest:console, for a faked subprocess.run(cmd, **kwargs)
    def tscon(self, cmd, **kwargs):
        assert cmd[0] == "tscon" and cmd[2] == "/dest:console"
        if self.tscon_attaches and int(cmd[1]) == self.session_id:
            self.connect_state = WTS_ACTIVE
        return run_result(kwargs)

    def add_to(self, windll: types.SimpleNamespace) -> None:
        """Put this session's kernel32 / user32 / wtsapi32 functions on a fake ``ctypes.windll``."""
        kernel32 = getattr(windll, "kernel32", None) or types.SimpleNamespace()
        kernel32.GetCurrentProcessId = Fn(lambda: 4242)
        kernel32.ProcessIdToSessionId = Fn(self.ProcessIdToSessionId)
        windll.kernel32 = kernel32
        user32 = windll.user32
        user32.GetProcessWindowStation = Fn(lambda: WINSTA_HANDLE)
        user32.OpenInputDesktop = Fn(self.OpenInputDesktop)
        user32.GetUserObjectInformationW = Fn(self.GetUserObjectInformationW)
        user32.CloseDesktop = Fn(self.CloseDesktop)
        windll.wtsapi32 = types.SimpleNamespace(
            WTSQuerySessionInformationW=Fn(self.WTSQuerySessionInformationW),
            WTSFreeMemory=Fn(self.WTSFreeMemory),
        )


class FakeClipboard:
    """pywin32 ``win32clipboard`` with one CF_UNICODETEXT slot (``None`` = no text on it)."""

    def __init__(self, text: str | None = None):
        self.text = text
        self.writes: list[str] = []

    def OpenClipboard(self):
        pass

    def CloseClipboard(self):
        pass

    def IsClipboardFormatAvailable(self, fmt):
        return self.text is not None

    def EmptyClipboard(self):
        self.text = None

    def GetClipboardData(self, fmt):
        if self.text is None:
            raise TypeError("Specified clipboard format is not available")
        return self.text

    def SetClipboardText(self, text, fmt):
        self.text = text
        self.writes.append(text)


"""The Windows locale name on Snapshot's "System Language" line (#48, the ticket's locale comment).

``locale.getdefaultlocale()`` has been deprecated since Python 3.11 and is removed in 3.15. The
provider reads the user's default locale name from kernel32 ``GetUserDefaultLocaleName``, then
``locale.getlocale()``, then ``en_US``. Only the OS boundary is faked: the Win32 call (on the
``conftest.win_session`` fake ``ctypes.windll``) and the C library's locale behind
``locale.getlocale``.
"""

from __future__ import annotations

import ctypes
import locale
from pathlib import Path

import pytest
from fake_windows_session import Fn

import remoteos

_LOCALE_NAME_MAX_LENGTH = 85  # winnls.h, including the terminating null


def _locale_name_api(monkeypatch, impl) -> Fn:
    fn = Fn(impl)
    monkeypatch.setattr(ctypes.windll.kernel32, "GetUserDefaultLocaleName", fn, raising=False)
    return fn


def _writes(name: str):
    """GetUserDefaultLocaleName that succeeds: the name in the buffer, its length + 1 returned."""

    def impl(buf, size):
        buf.value = name
        return len(name) + 1

    return impl


def test_system_language_is_the_windows_user_locale_name(win_session, monkeypatch):
    mod, _ = win_session
    fn = _locale_name_api(monkeypatch, _writes("sk-SK"))
    assert mod._get_system_language() == "sk-SK"
    [(buf, size)] = fn.calls
    assert size == _LOCALE_NAME_MAX_LENGTH
    assert ctypes.sizeof(buf) == _LOCALE_NAME_MAX_LENGTH * ctypes.sizeof(ctypes.c_wchar)


def test_a_failing_locale_name_call_falls_back_to_getlocale(win_session, monkeypatch):
    mod, _ = win_session
    _locale_name_api(monkeypatch, lambda buf, size: 0)  # 0 = the call failed
    monkeypatch.setattr(locale, "getlocale", lambda category=locale.LC_CTYPE: ("Slovak_Slovakia", "1250"))
    assert mod._get_system_language() == "Slovak_Slovakia"


def _raises(buf, size):
    raise OSError("GetUserDefaultLocaleName failed")


@pytest.mark.parametrize("api", ["fails", "raises", "missing"])
@pytest.mark.parametrize("getlocale", ["none", "raises"])
def test_without_any_locale_name_it_is_en_US(win_session, monkeypatch, api, getlocale):
    mod, _ = win_session
    if api == "fails":
        _locale_name_api(monkeypatch, lambda buf, size: 0)
    elif api == "raises":
        _locale_name_api(monkeypatch, _raises)
    else:  # an older Windows: the fake kernel32 has no GetUserDefaultLocaleName
        assert not hasattr(ctypes.windll.kernel32, "GetUserDefaultLocaleName")

    def _getlocale(category=locale.LC_CTYPE):
        if getlocale == "raises":
            raise ValueError("unknown locale: x")
        return (None, None)

    monkeypatch.setattr(locale, "getlocale", _getlocale)
    assert mod._get_system_language() == "en_US"


def test_no_getdefaultlocale_call_is_left():
    # removed in Python 3.15: a call left anywhere would be an AttributeError there
    src = Path(remoteos.__file__).parent
    callers = [p.relative_to(src).as_posix() for p in sorted(src.rglob("*.py")) if "getdefaultlocale" in p.read_text()]
    assert callers == []

"""``server``: the tools and helpers by name, wherever the code lives (#49).

The tool code is split over ``remoteos.app``, ``remoteos.tools.*`` and ``remoteos.__main__``, and
each of those binds its own ``is_windows`` / ``get_desktop`` / ``pyautogui``. Reading
``server.<name>`` finds the real object. ``monkeypatch.setattr(server, name, fake)`` patches the REAL
modules, every one that binds ``name``, so a fake cannot miss a tool module.
"""

from __future__ import annotations

import remoteos.__main__ as _main
from remoteos import app, tools

_MODULES = (_main, app, tools.desktop, tools.windows, tools.shell, tools.files, tools.system, tools.network,
            tools.media, tools.tasks)  # fmt: skip


class _ToolSurface:
    __slots__ = ()  # no instance __dict__: pytest's undo then restores the value it read, not a deletion

    def _binding(self, name: str) -> list:
        return [m for m in _MODULES if name in vars(m)]

    def __getattr__(self, name: str):
        for module in self._binding(name):
            return getattr(module, name)
        raise AttributeError(name)

    def __setattr__(self, name: str, value) -> None:
        # pyautogui is not imported on Linux: a fake of it goes where the input tools read it
        for module in self._binding(name) or [tools.desktop]:
            setattr(module, name, value)

    def __delattr__(self, name: str) -> None:
        for module in self._binding(name):
            delattr(module, name)


server = _ToolSurface()

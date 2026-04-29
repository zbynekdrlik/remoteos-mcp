"""Platform auto-detection and backend dispatch."""

from __future__ import annotations

import sys
from types import ModuleType

_PLATFORM = sys.platform

def get_desktop() -> ModuleType:
    if _PLATFORM == "darwin":
        from remoteos.platform.mac import desktop
        return desktop
    if _PLATFORM == "linux":
        from remoteos.platform.linux import desktop
        return desktop
    from remoteos.platform.win import desktop
    return desktop

def get_services() -> ModuleType:
    if _PLATFORM == "darwin":
        from remoteos.platform.mac import services
        return services
    if _PLATFORM == "linux":
        from remoteos.platform.linux import services
        return services
    from remoteos.platform.win import services
    return services

def get_system() -> ModuleType:
    if _PLATFORM == "darwin":
        from remoteos.platform.mac import system
        return system
    if _PLATFORM == "linux":
        from remoteos.platform.linux import system
        return system
    from remoteos.platform.win import registry
    return registry

def is_windows() -> bool:
    return _PLATFORM == "win32"

def is_macos() -> bool:
    return _PLATFORM == "darwin"

def is_linux() -> bool:
    return _PLATFORM == "linux"

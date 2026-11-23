"""Host backends: everything that depends on the desktop platform the game runs on.

The package is named ``host`` rather than ``platform`` because agent/main.py
puts agent/ first on sys.path, where a ``platform`` package would shadow the
standard library.

Four interfaces, implemented once per backend and chosen from the connected
controller's info:

- ``Target``: identity and focus of the game window (exists, minimized,
  foreground, hung, bring to front, what currently has focus).
- ``Input`` (``InputBackend``): game input with guaranteed release. Task code uses
  navigation.controller.ForegroundInput, which holds the keys and buttons
  it pressed and releases them through the backend on stop, focus loss or
  error. Win32 virtual-key integers stay the key id everywhere; a backend
  translates them if its platform needs other codes.
- ``Geometry``: capture geometry of the target (client size, client rect on
  screen, monitor work area).
- ``Capabilities``: what the backend supports, checked before work starts.

Optional adapters (overlay windows, launching and closing the game,
credential storage) are reached through ``overlay_backend()`` and similar
accessors only where a capability says they exist.

Backends live in subpackages (``host.win32``) and are imported lazily, so
importing task code never loads another platform's backend.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Protocol


@dataclass(frozen=True)
class Capabilities:
    backend: str
    foreground_input: bool = False
    background_input: bool = False
    overlay: bool = False
    tall_window: bool = False
    launch_game: bool = False
    close_game: bool = False


UNSUPPORTED = Capabilities("unsupported")


class Target(Protocol):
    """The connected game window.

    Backends may add platform handles (Win32Target.user32); neutral code must
    not rely on them.
    """

    handle: int
    capture_requires_foreground: bool

    def exists(self) -> bool: ...
    def minimized(self) -> bool: ...
    def foreground(self) -> bool: ...
    def hung(self) -> bool: ...
    def bring_to_foreground(self, controller) -> bool:
        """Restore and activate the window; True when it is in the foreground afterwards."""
        ...
    def focus_details(self) -> dict: ...


class InputBackend(Protocol):
    """Platform input primitives behind navigation.controller.ForegroundInput.

    The neutral input class tracks held keys and buttons and guarantees their
    release; these primitives release without the agent channel or refocusing.
    Keys are Win32 virtual-key integers. Pending: the Win32 implementation
    (send_mouse, keybd_event/mouse_event fallbacks, window-message keys and
    BackgroundInput) still lives in navigation/controller.py.
    """

    def force_key_up(self, key: int) -> None: ...
    def force_mouse_up(self, button: int) -> None: ...
    def press_mouse(self, button: int) -> bool: ...
    def post_window_key(self, key: int, *, down: bool) -> bool: ...
    def relative_move(self, dx: int, dy: int) -> bool: ...


class Geometry(Protocol):
    """Where the target's client area is, in screen pixels.

    Pending: the Win32 implementation is still tall_window.py.
    """

    def client_size(self) -> tuple[int, int] | None: ...
    def client_screen_rect(self) -> tuple[int, int, int, int] | None: ...
    def monitor_work_area(self) -> tuple[int, int, int, int] | None: ...


def platform_backend() -> str | None:
    """Backend for this process's desktop, or None when there is none."""
    return "win32" if os.name == "nt" else None


def controller_backend(info) -> str | None:
    """Backend that can drive a controller described by Controller.info, or None."""
    if platform_backend() == "win32" and isinstance(info, dict) and info.get("type") == "win32":
        return "win32"
    return None


def connect_target(controller) -> Target:
    """Target for the window the client connected, never an independently found one.

    Controller.info is forwarded by MaaAgentServer; Toolkit enumeration is not.
    Raises NavigationError when this host cannot drive that controller.
    """
    from navigation.models import NavigationError
    if platform_backend() != "win32":
        raise NavigationError("前台导航仅支持 Windows 游戏窗口。")
    try:
        info = controller.info
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError) as error:
        raise NavigationError("无法读取当前连接的游戏窗口，请重新连接后再试。") from error
    from host.win32.target import Win32Target
    return Win32Target(info)


def capabilities(backend: str | None) -> Capabilities:
    if backend == "win32":
        from host.win32 import CAPABILITIES
        return CAPABILITIES
    return UNSUPPORTED


def overlay_backend():
    """Module providing the overlay windows on this desktop, or None when overlays are unavailable.

    Chosen by the process's desktop, not the controller: overlays are desktop
    windows, and the only overlay backend is Win32.
    """
    if capabilities(platform_backend()).overlay:
        from host.win32 import overlays
        return overlays
    return None

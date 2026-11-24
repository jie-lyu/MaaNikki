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

Backends live in subpackages (``host.win32``, ``host.macos``) and are imported
lazily, so importing task code never loads another platform's backend.

Keys: Win32 virtual-key integers are the key id everywhere. ``native_key()`` is
the one place that turns one into what the connected controller expects
(unchanged on Win32, host/macos/keys.py on macOS); call it exactly where a key
is handed to the controller or to a pipeline action.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import sys
from typing import Protocol


@dataclass(frozen=True)
class Capabilities:
    backend: str
    foreground_input: bool = False
    background_input: bool = False
    overlay: bool = False
    tall_window: bool = False  # 16:9 healing and the temporary tall scan window
    work_area_bounds: bool = False  # click targets clipped to the monitor work area (taskbar, off-screen)
    pointer_hover: bool = False  # the cursor may rest on a UI control before and after a click
    # Navigation presses Alt once before camera turns to leave the game's cursor mode (Windows). On macOS
    # the key shows the pointer while held, leaves no lasting mode, and makes the game swallow the next
    # relative move (device investigation, 2026-11-24).
    requires_alt_before_camera: bool = False
    launch_game: bool = False
    close_game: bool = False
    clipboard: bool = False
    # Typing a string into a game text field (InputText). macOS posts one character per request (patch 4) but is not
    # yet device-verified there, so text-dependent sub-options refuse before any committing input.
    text_input: bool = False
    credential_store: bool = False


UNSUPPORTED = Capabilities("unsupported")


class UnsupportedInput(Exception):
    """An input this desktop cannot send yet. Deliberately not an OSError/RuntimeError/ValueError: callers that turn
    those into a fallback (for example the realm queue spending the remaining energy on the default realm) must not
    catch it; the task stops instead."""


class Target(Protocol):
    """The connected game window.

    Backends may add platform handles (Win32Target.user32); neutral code must
    not rely on them.
    """

    handle: int
    capture_requires_foreground: bool
    # True where releasing through the controller never refocuses the game, so held input is
    # released through it even after focus loss (macOS). Win32 releases directly instead.
    release_ignores_focus: bool

    def exists(self) -> bool: ...
    def minimized(self) -> bool: ...
    def foreground(self) -> bool: ...
    def hung(self) -> bool: ...
    def bring_to_foreground(self, controller) -> bool:
        """Restore and activate the window; True when it is in the foreground afterwards."""
        ...
    def focus_details(self) -> dict: ...
    def input(self) -> "InputBackend":
        """Input primitives bound to this window."""
        ...
    def geometry(self) -> "Geometry":
        """Capture geometry of this window."""
        ...


class InputBackend(Protocol):
    """Platform input primitives behind navigation.controller.ForegroundInput.

    The neutral input class tracks held keys and buttons and guarantees their
    release; these primitives release without the agent channel or refocusing.
    Keys are Win32 virtual-key integers. The Win32 implementation is
    host/win32/input.py; window-targeted background input is
    host/win32/background_input.py.
    """

    def force_key_up(self, key: int) -> None: ...
    def force_mouse_up(self, button: int) -> None: ...
    def press_mouse(self, button: int) -> bool: ...
    def post_window_key(self, key: int, *, down: bool) -> bool: ...
    def relative_move(self, dx: int, dy: int) -> bool: ...
    def last_error(self) -> int: ...
    def physical_keys(self):
        """Object whose down(key) reports whether the user holds a physical key (route recording)."""
        ...


class Geometry(Protocol):
    """Where the target's client area is, in screen pixels.

    The Win32 implementation is host/win32/geometry.py over window_layout.py,
    which also owns 16:9 healing and the temporary tall window behind the
    neutral tall_window.py entry points.
    """

    def client_size(self) -> tuple[int, int] | None: ...
    def client_screen_rect(self) -> tuple[int, int, int, int] | None: ...
    def monitor_work_area(self) -> tuple[int, int, int, int] | None: ...


def platform_backend() -> str | None:
    """Backend for this process's desktop, or None when there is none."""
    if os.name == "nt":
        return "win32"
    if sys.platform == "darwin":
        return "macos"
    return None


def controller_backend(info) -> str | None:
    """Backend that can drive a controller described by Controller.info, or None."""
    backend = platform_backend()
    kind = info.get("type") if isinstance(info, dict) else None
    if (backend, kind) in {("win32", "win32"), ("macos", "macos")}:
        return backend
    return None


def native_key(key: int) -> int:
    """The connected controller's code for one of the agent's Win32 virtual keys.

    Unchanged on Win32. On macOS the single table in host/macos/keys.py; a key without a macOS
    counterpart raises NavigationError instead of becoming 0 (which is the A key there).
    """
    if platform_backend() != "macos":
        return key
    from host.macos.keys import UnmappedKey, translate
    try:
        return translate(key)
    except UnmappedKey as error:
        from navigation.models import NavigationError
        raise NavigationError(str(error)) from None


def connect_target(controller) -> Target:
    """Target for the window the client connected, never an independently found one.

    Controller.info is forwarded by MaaAgentServer; Toolkit enumeration is not.
    Raises NavigationError when this host cannot drive that controller.
    """
    from navigation.models import NavigationError
    backend = platform_backend()
    if backend not in ("win32", "macos"):
        raise NavigationError("前台导航仅支持 Windows 游戏窗口。")
    try:
        info = controller.info
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError) as error:
        raise NavigationError("无法读取当前连接的游戏窗口，请重新连接后再试。") from error
    if backend == "macos":
        from host.macos.target import MacOSTarget
        return MacOSTarget(info, controller)
    from host.win32.target import Win32Target
    return Win32Target(info)


def controller_supports_foreground_input(controller) -> bool:
    """Whether the interface.json controller definition lets the agent send foreground input itself.

    The rule belongs to the backend for this desktop; without one, never.
    """
    backend = platform_backend()
    if backend == "win32":
        from host.win32 import supports_foreground_inputs
        return supports_foreground_inputs(controller)
    if backend == "macos":
        from host.macos import supports_foreground_inputs
        return supports_foreground_inputs(controller)
    return False


def connection_geometry_problem(get_controller) -> str | None:
    """Why input must pause because of the connection's geometry, or None when it is fine.

    macOS: the patched unit's geometry_changed latch, read live from Controller.info. True means
    the window moved, changed display or scaling since connect and clicks are refused until
    reconnect; a value that cannot be read is treated the same way, never as unchanged.
    Always None on Win32, where the controller is not even looked up (``get_controller`` is called
    only on macOS).
    """
    if platform_backend() != "macos":
        return None
    try:
        info = get_controller().info
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
        info = None
    latch = info.get("geometry_changed") if isinstance(info, dict) else None
    if latch is False:
        return None
    if latch is True:
        return "游戏窗口的位置、所在显示器或缩放在运行中发生了变化，已暂停输入；请重新连接游戏后再运行"
    return "无法读取 Mac 控制模块的窗口几何状态，已暂停输入；请重新连接游戏后再运行"


def task_entry_supported(entry) -> bool:
    """Whether this desktop's backend may run the task whose pipeline entry is ``entry``.

    Win32 runs every task, as before. macOS runs the preview's verified and experimental entries.
    """
    if platform_backend() == "macos":
        from host.macos import dev_extra_task_entries, task_tier
        if entry in dev_extra_task_entries():
            return True  # development switch for supervised device tests; never set by a release client
        try:
            return task_tier(entry) is not None
        except (OSError, ValueError, KeyError, TypeError):
            return False  # no readable list: refuse, like the client
    return True


def task_refusal_reason(entry) -> str:
    """The user-facing reason this desktop refuses ``entry`` (macOS: the list's excluded reason when there is one)."""
    if platform_backend() == "macos":
        from host.macos import refusal_reason
        return refusal_reason(entry)
    return "当前系统不支持这个任务，已跳过。"


def task_start_notice(entry) -> str | None:
    """A warning for the task's own log when ``entry`` starts: macOS experimental tasks only, None elsewhere."""
    if platform_backend() == "macos":
        from host.macos import start_notice
        return start_notice(entry)
    return None


def background_input_class():
    """Window-targeted input class for this desktop; raises NavigationError where there is none."""
    if platform_backend() != "win32":
        from navigation.models import NavigationError
        raise NavigationError("前台导航仅支持 Windows 游戏窗口。")
    from host.win32.background_input import BackgroundInput
    return BackgroundInput


def launch_window_probes():
    """Window liveness and responsiveness probes for the start-game pretask, or None where this
    desktop cannot launch the game."""
    if not capabilities(platform_backend()).launch_game:
        return None
    from host.win32 import launch
    return launch


def game_close_api():
    """Binding that closes the game and launcher windows/processes on this desktop."""
    if not capabilities(platform_backend()).close_game:
        raise RuntimeError("当前系统不支持关闭游戏。")
    from host.win32.close import Win32Api
    return Win32Api()


def clipboard():
    """Module with get_clipboard_text() and set_clipboard_text() for this desktop."""
    backend = platform_backend()
    if not capabilities(backend).clipboard:
        raise RuntimeError("当前系统不支持读写剪贴板。")
    if backend == "macos":
        from host.macos import clipboard as module
        return module
    from host.win32 import clipboard as module
    return module


def credential_store():
    """Module whose credentials(path) reads the PearPal login protected for the current user."""
    if not capabilities(platform_backend()).credential_store:
        raise RuntimeError("当前系统不支持读取已保存的登录信息。")
    from host.win32 import credentials as module
    return module


def capabilities(backend: str | None) -> Capabilities:
    if backend == "win32":
        from host.win32 import CAPABILITIES
        return CAPABILITIES
    if backend == "macos":
        from host.macos import CAPABILITIES
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

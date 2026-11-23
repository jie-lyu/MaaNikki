from __future__ import annotations

from task_control import task_time as time

from host import background_input_class, connect_target
from .models import GameWindowClosed, NavigationError
from game_keys import Binding, GameKeys, MACRO_KEYS


MAX_CAMERA_PIXELS = 8192


class ForegroundInput:
    """Bounded Maa key pulses. Stop/focus loss always releases held keys."""
    window_only = False
    capture_requires_foreground = False

    def __init__(self, controller, stopped, ready=lambda: True, bindings=None):
        self.controller = controller
        self.stopped = stopped
        self.ready = ready
        self.held = set()
        self.buttons = set()
        self.bindings = bindings or GameKeys()
        # The backend verifies the connected window and owns focus queries.
        self.target = connect_target(controller)
        self.hwnd = self.target.handle
        self.capture_requires_foreground = self.target.capture_requires_foreground
        # Platform input primitives that release without the agent channel.
        self.native = self.target.input()
        from task_control import active_session
        session = active_session()
        if session is not None:
            session.register(self)

    def foreground(self):
        return self.target.foreground()

    def capturable(self):
        """Return whether the bound window exists, is unminimized, and satisfies capture requirements."""
        return bool(
            self.target.exists()
            and not self.target.minimized()
            and (not self.capture_requires_foreground or self.foreground())
        )

    def focus_details(self):
        return self.target.focus_details()

    def hung(self):
        return self.target.hung()

    def _wait_without_session(self, ready):
        if not ready():
            self.release()
            while not ready():
                if self.stopped():
                    raise NavigationError("任务已停止。")
                if not self.target.exists():
                    raise GameWindowClosed("连接的游戏窗口已经关闭，请重新连接。")
                time.sleep(.05)

    def check(self, *, require_foreground=True):
        if self.stopped():
            raise NavigationError("已停止导航。")
        from task_control import active_session
        session = active_session()
        if session is not None:
            session.wait(self, require_foreground=require_foreground)
            return
        ready = self.foreground if require_foreground else self.capturable
        self._wait_without_session(ready)

    def _post_window_key(self, key: int, *, down: bool) -> bool:
        return self.native.post_window_key(key, down=down)

    def click_window_key(self, key, pause, seconds=.05):
        """Post a key pulse to the connected window without global input/focus."""
        if type(key) is not int or not 0 < key < 256 or not .01 <= seconds <= 1:
            raise NavigationError("后台按键参数无效。")
        if self.stopped() or not self.target.exists() or self.target.minimized():
            return False
        if not self._post_window_key(key, down=True):
            raise NavigationError(f"无法向游戏窗口发送后台按键（错误 {self.native.last_error()}）。")
        try:
            return pause(seconds)
        finally:
            # Even Stop during the pulse must enqueue the matching window-only up.
            # Never use global key_up here: it could affect the user's active app.
            if self.target.exists():
                if not self._post_window_key(key, down=False):
                    raise NavigationError(f"无法释放游戏窗口的后台按键（错误 {self.native.last_error()}）。")

    @property
    def generation(self):
        from task_control import active_session
        session = active_session()
        return session.generation if session else 0

    @property
    def pause_generation(self):
        from task_control import active_session
        session = active_session()
        return session.pause_generation if session else 0

    def release(self):
        # Maa Seize's key_up can refocus the game. Direct ups on focus loss avoid
        # stealing focus while still preventing globally stuck movement keys.
        for key in list(self.held):
            self.release_key(key)
        for button in list(self.buttons):
            self.release_mouse(button)

    def face_camera(self):
        """Align the character to the camera with a brief Ctrl+forward pulse."""
        forward = self.bindings.get("forward")
        control = Binding("key", 17)
        if forward.kind != "key" or forward.code in {17, 162, 163}:
            raise NavigationError("朝向对齐需要前进键为 Ctrl 以外的键盘按键。")
        self.check()
        if not self.ready():
            raise NavigationError("当前不是可移动的主界面，未调整角色朝向。")
        self.release()
        generation = self.generation
        hold, settle = .08, .4
        try:
            self._down(control)
            if self.generation != generation:
                raise NavigationError("调整朝向时游戏前台发生变化，请重新确认朝向。")
            started = time.monotonic()
            self._down(forward)
            # Count key-down submission time within the short pulse. Never
            # restore a held W after focus loss or perform OCR while holding it.
            self.wait(max(0, hold-(time.monotonic()-started)), restore=False)
        finally:
            # Release W before Ctrl so cleanup cannot leave ordinary forward
            # movement held. These releases also work after Stop/focus loss.
            try:
                self.release_key(forward.code)
                released_at = time.monotonic()
            finally:
                self.release_key(control.code)
        self.wait(settle, restore=False)
        if self.generation != generation:
            raise NavigationError("调整朝向时游戏前台发生变化，请重新确认朝向。")
        return {"hold_seconds": hold, "settle_seconds": settle,
                "forward_input_seconds": released_at-started}

    def release_key(self, key):
        if key not in self.held:
            return
        try:
            if self.foreground():
                try:
                    released = self.controller.post_key_up(key).wait().succeeded
                except (OSError, RuntimeError, ValueError):
                    released = False
                if not released:
                    self.force_key_up(key)
            else:
                self.force_key_up(key)
        finally:
            self.held.discard(key)

    def release_mouse(self, button):
        if button not in self.buttons:
            return
        try:
            if self.foreground():
                try:
                    released = self.controller.post_touch_up(button).wait().succeeded
                except (OSError, RuntimeError, ValueError):
                    released = False
                if not released:
                    self.force_mouse_up(button)
            else:
                self.force_mouse_up(button)
        finally:
            self.buttons.discard(button)

    def click_mouse(self, button, seconds=.05):
        """Pulse a scene button without moving the cursor or releasing movement."""
        if button not in (0, 1, 2, 3, 4) or not .01 <= seconds <= 1:
            raise NavigationError("鼠标点击参数无效。")
        self.check()
        if button in self.buttons:
            raise NavigationError("该鼠标按键正在按住，不能重复点击。")
        try:
            self.mouse_down(button)
            self.wait(seconds, restore=False)
            self.check()
            return True
        finally:
            self.release_mouse(button)

    def force_key_up(self, key):
        # Cleanup only: do not require a working Agent channel or refocus.
        self.native.force_key_up(key)

    def force_mouse_up(self, button):
        self.native.force_mouse_up(button)

    def hold(self, key, seconds, *, restore=True):
        """Press any registered game action, for route-authored key steps."""
        binding = self.bindings.get(key)
        self._down(binding, restore=restore)
        try:
            self.wait(seconds, restore=restore)
        finally:
            self._up(binding)

    def key_down(self, key):
        if key not in MACRO_KEYS:
            raise NavigationError("宏按键未登记。")
        self.check()
        self._down(self.bindings.get(key))

    def _down(self, binding, *, restore=True):
        self.check()
        if binding.kind == "key" and binding.code in self.held:
            return
        if binding.kind == "mouse":
            if binding.code in self.buttons:
                return
            self.mouse_down(binding.code)
            return
        self.held.add(binding.code)
        if not self.controller.post_key_down(binding.code).wait().succeeded:
            raise NavigationError("宏按键输入失败。")

    def key_up(self, key):
        if key not in MACRO_KEYS:
            raise NavigationError("宏按键未登记。")
        self.check()
        binding = self.bindings.get(key)
        if binding.kind == "mouse":
            if binding.code in self.buttons:
                self.mouse_up(binding.code)
            return
        if binding.code not in self.held:
            return
        if not self.controller.post_key_up(binding.code).wait().succeeded:
            raise NavigationError("宏按键释放失败。")
        self.held.discard(binding.code)

    def _up(self, binding):
        self.check()
        if binding.kind == "mouse":
            if binding.code in self.buttons:
                self.mouse_up(binding.code)
        elif binding.code in self.held:
            if not self.controller.post_key_up(binding.code).wait().succeeded:
                raise NavigationError("按键释放失败。")
            self.held.discard(binding.code)

    def mouse_down(self, button=0):
        if button not in (0, 1, 2, 3, 4):
            raise NavigationError("鼠标按键未登记。")
        self.check()
        self.buttons.add(button)
        # Press only; the backend never moves the cursor (that could turn the camera).
        if not self.native.press_mouse(button):
            raise NavigationError("鼠标动作输入失败。")

    def mouse_up(self, button=0):
        if button not in self.buttons:
            raise NavigationError("鼠标按键尚未按下。")
        self.check()
        if not self.controller.post_touch_up(button).wait().succeeded:
            raise NavigationError("鼠标动作释放失败。")
        self.buttons.discard(button)

    def wait(self, seconds, *, restore=True):
        if not 0 <= seconds <= 8:
            raise NavigationError("动作等待时间超出限制。")
        keys, buttons = set(self.held), set(self.buttons)
        generation = self.generation
        deadline = time.monotonic()+seconds
        while time.monotonic() < deadline:
            self.check()
            if restore and self.generation != generation:
                generation = self.generation
                # A bounded macro/ability hold resumes only its remaining
                # active duration. Navigation movement replans separately.
                from game_keys import Binding
                for key in keys:
                    self._down(Binding("key", key))
                for button in buttons:
                    self.mouse_down(button)
            time.sleep(min(.05, max(0, deadline-time.monotonic())))


def BackgroundInput(controller, stopped, ready=lambda: True, bindings=None):
    """Window-targeted input on the connected window, from the host backend (host/win32/background_input.py)."""
    return background_input_class()(controller, stopped, ready=ready, bindings=bindings)


def relative_camera(controller, dx: int, dy: int) -> bool:
    """Prefer native Maa relative motion; bounded fallback for older Seize builds.

    This is part of the application, not a computer-use testing workaround.
    No background or arbitrary-window injection is supported.
    """
    if (type(dx) is not int or type(dy) is not int
            or abs(dx) > MAX_CAMERA_PIXELS or abs(dy) > 300):
        return False
    adapter = ForegroundInput(controller, lambda: False)
    from task_control import active_session
    if active_session() is not None:
        adapter.check()
    if not adapter.foreground():
        return False
    if controller.post_relative_move(dx, dy).wait().succeeded:
        return True
    if active_session() is not None:
        adapter.check()
    if not adapter.foreground():
        return False
    return adapter.native.relative_move(dx, dy)


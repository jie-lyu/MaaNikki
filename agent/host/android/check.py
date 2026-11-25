"""The read-only Android check task (MaaNikki_AndroidCheck): one focus-confirmed frame, classified by the action
table; it reports the screen and what each named action would tap now, and sends no input.

Run by the nikki.gameplay action (kind "android_check"), which imports this module only when that task runs; its
entry node exists only in resource_android/, and the Windows package ships neither.
"""
from __future__ import annotations

def describe(screen, plans) -> str:
    from host.android import actions
    names = {"quit_dialog": "退出确认框", "modal": f"弹窗（{screen.title}）", "hud": "大世界", "page":
             f"页面（{screen.title}）", "unknown": "认不出的画面"}
    lines = [f"当前画面：{names.get(screen.kind, screen.kind)}" + (f"——{screen.reason}" if screen.reason else "")]
    for action in sorted(plans):
        plan = plans[action]
        label = actions.ACTION_NAMES[action]
        if isinstance(plan, str):
            lines.append(f"· {label}：不可用（{plan}）")
        elif plan.target is None:
            lines.append(f"· {label}：不用点")
        else:
            lines.append(f"· {label}：会点 {plan.target}" + ("" if plan.completes else "（先关掉退出确认框）"))
    return "\n".join(lines)


ATTEMPTS = 3


def check(runtime) -> bool:
    """No input. True when a frame was classified (whatever the screen is) with device focus held throughout:
    a focus loss observed while deciding voids the decision and it is made again on a new frame (decision 7)."""
    from host.android import actions
    for _attempt in range(ATTEMPTS):
        frame = runtime.capture()
        if frame is None:
            return False
        recognize = runtime._recognizer_on(frame)
        screen = actions.read_screen(recognize)
        plans = {}
        for action in actions.ACTIONS:
            try:
                plans[action] = actions.plan_tap(action, screen, recognize)
            except actions.ScreenUnrecognised as error:
                plans[action] = str(error)
        if runtime._device_focus_now() and runtime._frame_still_valid(frame):
            runtime.log(describe(screen, plans))
            return True
    runtime.log(f"安卓检查：连续 {ATTEMPTS} 次判断期间游戏焦点都变过，没有得出结论。")
    return False

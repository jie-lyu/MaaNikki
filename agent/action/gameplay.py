from __future__ import annotations

import cv2
from client_config import parse_custom_param
from registration import agent_action
from task_control import TaskStopped
from maa.custom_action import CustomAction
from daily.gameplay import Gameplay
from daily.runtime import Executors


@agent_action("nikki.ui_click")
class UIClickAction(CustomAction):
    """Pipeline clicks and Python workflows use the same input handling."""

    def run(self, context, argv):
        runtime = Executors(context)
        try:
            options = parse_custom_param(argv.custom_action_param)
            target = options.get("target", True)
            resumed = runtime.check_focus()
            if target is True and resumed and not options.get("ready"):
                return runtime.click_template(argv.node_name, wait_seconds=0)
            target = list(argv.box) if target is True else target
            ready = options.get("ready")
            if ready:
                # Retries must recognize a fresh box, not argv's stale box.
                return runtime.click_template(argv.node_name, ready=ready, wait_seconds=0)
            return runtime.action("Click", target=target)
        except TaskStopped:
            raise
        except (ValueError, TypeError, KeyError, RuntimeError) as error:
            runtime.log(f"界面点击失败：{error}。")
            return False


ANDROID_CHECK_ENTRY = "MaaNikki_AndroidCheck"


@agent_action("nikki.gameplay")
class GameplayAction(CustomAction):
    def run(self, context, argv):
        runtime = Executors(context)
        kind = None
        # The read-only Android check is bound to its task entry, not to a parameter: whatever the parameter says,
        # that task runs the check or nothing, and never the recovery below (which can tap).
        read_only = getattr(getattr(argv, "task_detail", None), "entry", None) == ANDROID_CHECK_ENTRY
        try:
            options = parse_custom_param(argv.custom_action_param)
            kind = options["kind"]
            if read_only or kind == "android_check":
                if not (read_only and kind == "android_check"):
                    runtime.log("安卓检查的任务入口和参数不一致，没有执行。")
                    return False
                # Android only (its entry node is in resource_android/): one frame, no input, no recovery.
                from host.android.check import check
                return check(runtime)
            game = Gameplay(runtime)
            if kind == "weekly_shop":
                from host import capabilities, game_backend
                if capabilities(game_backend()).touch_actions_only:
                    # The phone layout has its own flow and taps (Windows does not ship it). It branches before
                    # WeeklyShop is built: that reads the UID (its fallback navigates the PC way) for the weekly
                    # record, which the phone flow does not keep (it only reports, nothing is claimed).
                    from host.android import shop
                    return shop.run(runtime) and not runtime.stopped
                from daily.shop import WeeklyShop
                return WeeklyShop(runtime).run()
            if kind == "monthly_exchange":
                from daily.monthly_exchange import MonthlyExchange
                return MonthlyExchange(runtime).run()
            if kind == "mailbox":
                from daily.mailbox import Mailbox
                return Mailbox(runtime).run()
            if kind == "redeem_codes":
                from daily.redeem_codes import RedeemCodes
                return RedeemCodes(runtime).run()
            if kind == "home":
                from daily.home import Home
                return Home(runtime).run()
            if kind == "fishing":
                from daily.fishing import current_fishing
                return current_fishing(runtime)
            if kind == "main":
                return runtime.main()
            if kind in ("dig", "monthly", "lookbook"):
                return getattr(game, kind)()
            if kind in ("jihua", "bless", "monster"):
                return game.realm(kind, allow_exhausted=True,
                                  return_page=options.get("return_page", "main"))
            return False
        except TaskStopped:
            raise
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, cv2.error) as error:
            from host import android_user_stop
            if android_user_stop():
                return False  # Android: a user stop is no failure (always False on the desktop)
            runtime.log(f"任务未完成：{error}。")
            if not runtime.stopped and not read_only and kind != "android_check":  # the check never sends input
                from host import capabilities, game_backend
                if not capabilities(game_backend()).touch_actions_only:  # Android flows return to the HUD themselves
                    runtime.main()
            return False

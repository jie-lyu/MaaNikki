"""Local replay of the Android big-map teleport (host.android.teleport) and the hub bell checks
(host.android.crystal_route) on the saved device frames; not part of the offline suite (.build/ is not in the
repository). No device, no input. Needs ./maafw.

    python -I -X utf8 scripts/android/teleport_frames.py [--build DIR]

DIR defaults to .build/android. For each frame of DIR/map-survey/frames it prints what the task would recognise and
decide there, through the real nodes (both resource layers):
  - the screen class (actions.read_screen) and the map state (teleport.map_state);
  - on the map: the region card's name and state (teleport.card_state), the zoom state, the map located on the world
    map (score, runner-up), where 无界枢纽 and 星梦群屿·主岛 are, and for each: tap its pillar (template hit, distance
    from where its icon should be, the guards) or the drag that would bring it into TARGET_ZONE (both ends blank);
  - on 地图切换: the 星海 row (and whether it is a province header) or the collapse chevron, with the guards;
  - on a point panel: its title, buttons, map words, whether it passes for 星梦群屿·主岛, the 传送 tap and its guards;
  - on the HUD frames (DIR/map-survey and DIR/survey/walk): the bell button and the walk's screen class.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "scripts" / "android"))

CHECKPOINTS = {"无界枢纽": (834.327, 1019.073), "星梦群屿·主岛": (1433.676, 997.388)}
DEFAULT_SCALE = 0.676  # the survey's registration of the default zoom (MAX_ZOOM_SCALE x 1.452)


class _Rt:
    @staticmethod
    def _frame_still_valid(_frame):
        return True


def judge(world, frame_path):
    import cv2
    from host.android import actions, move, pages, teleport, touch_point
    from host.android.crystal_route import BELL_POINT
    frame = cv2.imread(str(frame_path))

    def run(recognize):
        screen = actions.read_screen(recognize)
        state = teleport.map_state(_Rt, screen, frame, recognize)
        out = {"screen": screen.kind, "title": screen.title, "state": state}
        if state in ("map", "panel"):
            out["region"] = teleport.region_name(recognize)
            card = teleport.card_state(recognize)
            out["card"] = {"kind": card.kind, "crystals": card.crystals, "counters": card.counters}
            title = recognize(teleport.REGION_NAME)
            if actions._hit(title):
                out["card_title_tap"] = {"at": touch_point(actions._box(title)),
                                         "forbidden": pages._forbidden_near(recognize, actions._box(title)),
                                         "map_word": teleport.word_near(recognize, actions._box(title))}
        if state == "map":
            level = teleport.zoom_state(frame)
            out["zoom"] = level
            zoom_in = recognize(teleport.ZOOM_IN)
            out["zoom_in"] = touch_point(actions._box(zoom_in)) if actions._hit(zoom_in) else None
            scale = teleport.MAX_ZOOM_SCALE if level == "max" else DEFAULT_SCALE
            pose = world.locate(frame, scale)
            out["pose"] = pose.log() | {"trusted": pose.trusted, "scale": scale}
            residual = world.residual(frame, pose)
            points = {}
            for name, (x, y) in CHECKPOINTS.items():
                here = [round(v) for v in pose.to_screen(x, y)]
                item = {"screen": here, "in_target_zone": teleport.in_zone(here, teleport.TARGET_ZONE)}
                expected = teleport.pillar_expected(pose, x, y)
                if 0 <= expected[0] < 1280 and 0 <= expected[1] < 720:
                    result = recognize(teleport.PILLAR, {"roi": teleport.pillar_window(expected)})
                    if actions._hit(result):
                        box = actions._box(result)
                        centre = (box[0] + box[2] / 2, box[1] + box[3] / 2)
                        item["pillar"] = {"tap": touch_point(box), "off_px": round(math.dist(centre, expected), 1),
                                          "forbidden": pages._forbidden_near(recognize, box),
                                          "map_word": teleport.word_near(recognize, box)}
                    else:
                        item["pillar"] = None
                if not item["in_target_zone"]:
                    plan = teleport.plan_drag(residual, here)
                    item["drag"] = plan and {"begin": list(plan[0]), "end": list(plan[1]),
                                             "map_words": [teleport.word_near(recognize, [*p, 1, 1]) for p in plan],
                                             "forbidden": [pages._forbidden_near(recognize, [*p, 1, 1]) for p in plan]}
                points[name] = item
            out["points"] = points
        if state == "menu":
            row = recognize(teleport.MENU_STARSEA)
            if actions._hit(row):
                box = actions._box(row)
                out["starsea_row"] = {"tap": touch_point(box), "header": teleport.province_header(box),
                                      "map_word": teleport.word_near(recognize, box),
                                      "forbidden": pages._forbidden_near(recognize, box)}
            chevron = recognize(teleport.MENU_COLLAPSE)
            out["collapse"] = touch_point(actions._box(chevron)) if actions._hit(chevron) else None
        if state == "panel":
            panel = teleport.read_panel(recognize)
            out["panel"] = {"title": panel.title, "buttons": list(panel.buttons), "words": list(panel.words),
                            "ok_for_main_island": teleport.panel_ok(panel, "星梦群屿·主岛"),
                            "ok_for_hub": teleport.panel_ok(panel, "无界枢纽"),
                            "teleport_tap": touch_point(panel.teleport) if panel.teleport else None,
                            "teleport_guards": panel.teleport and [pages._forbidden_near(recognize, panel.teleport),
                                                                   teleport.word_near(recognize, panel.teleport)]}
            close = recognize(teleport.PANEL_CLOSE)
            out["close_tap"] = touch_point(actions._box(close)) if actions._hit(close) else None
        if screen.kind == "hud":
            bell = recognize(teleport.HUD_BELL)
            out["bell"] = touch_point(actions._box(bell)) if actions._hit(bell) else None
            out["bell_in_place"] = actions._hit(bell) and actions._near(touch_point(actions._box(bell)), BELL_POINT)
            out["walk_class"] = move.classify(screen, recognize).kind
        return out
    return run


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", default=str(ROOT / ".build" / "android"))
    args = parser.parse_args()
    build = Path(args.build)
    import probe
    from host.android.teleport import WorldMap
    world = WorldMap(resource=ROOT / "resource")
    frames = sorted((build / "map-survey" / "frames").glob("*.png")) + sorted((build / "survey" / "walk").glob("*.png"))
    keep = []
    for path in frames:
        recognizer = probe.Recognizer(probe.frame_controller(path), android_layer=True)
        keep.append(recognizer)  # several recognizers in one process: keep them alive
        result = recognizer.on_one_frame(judge(world, path))
        print(json.dumps({"frame": f"{path.parent.name}/{path.name}", **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

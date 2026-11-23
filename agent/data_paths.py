"""Dual-layer game data & recognition model resolver (hot-update overlay -> built-in)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re

from app_paths import DATA_ROOT, RESOURCE_ROOT

_SLOT_RE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")

_REQUIRED_OVERLAY_FILES = (
    "resource/data/wardrobe_scoring.json",
    "resource/data/blessing_catalog.json",
    "resource/data/crafting_catalog.json",
    "resource/data/xinghai_rotation.json",
    "resource/navigation/materials_catalog_v1.json",
    "resource/model/wardrobe/labels.json",
    "resource/model/wardrobe/parts.json",
    "resource/model/wardrobe/coverage.json",
    "resource/model/wardrobe/model-contract.json",
    "resource/model/backpack/catalog.json",
    "resource/model/backpack/icons.npz",
    "resource/model/rejuvenate/categories.json",
    "resource/model/rejuvenate/icons.npz",
    "resource/model/blessing_identity.json",
    "resource/model/blessing_identity.onnx",
    "resource/model/blessing_badge.json",
    "resource/model/blessing_badge.onnx",
    "resource/model/blessing_badge.npz",
    "resource/model/blessing_level.json",
    "resource/model/blessing_level.onnx",
)


def _slot_complete(slot_dir: Path) -> bool:
    return slot_dir.is_dir() and all((slot_dir / rel).is_file() for rel in _REQUIRED_OVERLAY_FILES)


def active_overlay_dir(data_root: Path | None = None, resource_root: Path | None = None) -> Path | None:
    """The hot-update slot under the data root, if complete and not older than the built-in data."""
    data_root = DATA_ROOT if data_root is None else data_root
    resource_root = RESOURCE_ROOT if resource_root is None else resource_root
    env_slot = os.environ.get("PI_MAANIKKI_DATA_OVERLAY", "").strip()
    if env_slot:
        candidate = Path(env_slot)
        if _slot_complete(candidate):
            return candidate

    active_file = data_root / "data_overlay" / "active.json"
    if not active_file.is_file():
        return None
    try:
        record = json.loads(active_file.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    slot = str(record.get("slot") or "")
    if record.get("schema_version") != 1 or not _SLOT_RE.fullmatch(slot) or ".." in slot:
        return None
    candidate = data_root / "data_overlay" / "slots" / slot
    if not _slot_complete(candidate):
        return None

    builtin_ws = resource_root / "resource/data/wardrobe_scoring.json"
    if builtin_ws.is_file():
        try:
            ws = json.loads(builtin_ws.read_text(encoding="utf-8-sig"))
            b_ver = str(ws.get("version") or "")
            b_cnt = len(ws.get("game_items") or {})
            o_ver = str(record.get("wardrobe_version") or "")
            o_cnt = int(record.get("game_items") or 0)
            if (b_ver and o_ver and b_ver > o_ver) or b_cnt > o_cnt:
                return None
        except (OSError, ValueError, TypeError):
            pass
    return candidate


def data_path(rel_path: str, data_root: Path | None = None, resource_root: Path | None = None) -> Path:
    """rel_path from the active overlay when present there, otherwise from the resource root."""
    clean = rel_path.replace("\\", "/").lstrip("./")
    overlay = active_overlay_dir(data_root, resource_root)
    if overlay is not None:
        candidate = overlay / clean
        if candidate.exists():
            return candidate
    return (RESOURCE_ROOT if resource_root is None else resource_root) / clean

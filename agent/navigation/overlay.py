"""Route overlay data: terrain sampling, labels and settings. The Win32 window is in host/win32/route_overlay.py."""
from __future__ import annotations

import base64
import json
import math
import threading

import cv2
import numpy as np

from app_paths import DATA_ROOT, RESOURCE_ROOT
from vision import read_image


NAV_ROOT = RESOURCE_ROOT / "resource/navigation"

ACTION_LABELS = {
    "": "途经",
    "teleport": "传送",
    "wait": "等待",
    "collect": "采集",
    "insect": "捕虫",
    "fishing": "钓鱼",
    "fishing_star": "钓星",
    "bottle": "漂流瓶",
    "macro": "连招",
    "key": "按键",
    "photo": "拍照",
    "place": "摆放",
    "music": "音乐",
    "chat": "聊天",
    "delivery": "投递",
    "animal": "动物",
}

ACTION_BGR = {
    "": (245, 245, 245),
    "teleport": (240, 108, 124),
    "wait": (44, 138, 194),
    "collect": (106, 157, 63),
    "insect": (58, 162, 122),
    "fishing": (196, 143, 47),
    "fishing_star": (196, 143, 47),
    "bottle": (168, 168, 74),
    "macro": (122, 86, 208),
    "key": (115, 127, 138),
}


class _TerrainSampler:
    """Multi-layer 3D world surface reader combining terrain heightmaps, 3D solid structures, and spire anchors."""

    _cache: dict[str, "_TerrainSampler"] = {}
    _pillars_by_map: dict[str, list[tuple[float, float, float]]] | None = None
    _lock = threading.Lock()

    def __init__(self, map_id: str):
        self.map_id = map_id
        self.px_scale = 0.5
        self.z_offset = -300.0
        self.z_scale = 64.0
        self.z_grid: np.ndarray | None = None
        self.valid_grid: np.ndarray | None = None
        self.obs_grid: np.ndarray | None = None
        self.h = 0
        self.w = 0
        self._raw_meshes: list = []
        self._mesh_cache: dict[int, tuple[np.ndarray, np.ndarray] | None] = {}
        self._instances: list = []
        self._buckets: dict[tuple[int, int], list[int]] = {}
        self._inst_cache: dict[int, tuple | None] = {}
        self._layer_cache: dict[tuple[int, int], tuple[float, ...]] = {}
        self._query_lock = threading.Lock()
        self.pillars: list[tuple[float, float, float]] = self._load_pillars(map_id)
        try:
            manifest_path = NAV_ROOT / "terrain/manifest.json"
            if manifest_path.is_file():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                info = manifest.get(map_id)
                if info and info.get("image"):
                    self.px_scale = float(info.get("px_scale", 0.5))
                    self.z_offset = float(info.get("z_offset", -500.0))
                    self.z_scale = float(info.get("z_scale", 10.0))
                    img = read_image(NAV_ROOT / info["image"], cv2.IMREAD_COLOR)
                    if img is not None and img.ndim == 3:
                        self.h, self.w = img.shape[:2]
                        b = img[:, :, 0].astype(np.uint16)
                        g = img[:, :, 1].astype(np.uint16)
                        r = img[:, :, 2].astype(np.uint16)
                        hu16 = (r << 8) | g
                        valid = ((b & 1) != 0) & (hu16 > 0)
                        self.valid_grid = valid
                        self.obs_grid = ((b >> 1) & 1) != 0
                        self.z_grid = np.where(
                            valid,
                            hu16.astype(np.float32) / self.z_scale + self.z_offset,
                            np.nan,
                        )
                if info and info.get("structures"):
                    struct_path = NAV_ROOT / info["structures"]
                    if struct_path.is_file():
                        sdata = json.loads(struct_path.read_text(encoding="utf-8"))
                        self._raw_meshes = sdata.get("meshes") or []
                        self._instances = sdata.get("instances") or []
                        buckets: dict[tuple[int, int], list[int]] = {}
                        for idx, row in enumerate(self._instances):
                            bx0 = int(math.floor(float(row[0]))) >> 5
                            bx1 = int(math.floor(float(row[2]))) >> 5
                            by0 = int(math.floor(float(row[1]))) >> 5
                            by1 = int(math.floor(float(row[3]))) >> 5
                            for bx in range(bx0, bx1 + 1):
                                for by in range(by0, by1 + 1):
                                    buckets.setdefault((bx, by), []).append(idx)
                        self._buckets = buckets
        except Exception:
            self.z_grid = None
            self.valid_grid = None
            self.obs_grid = None

    @classmethod
    def _load_pillars(cls, map_id: str) -> list[tuple[float, float, float]]:
        if cls._pillars_by_map is None:
            loaded: dict[str, list[tuple[float, float, float]]] = {}
            try:
                cat_path = NAV_ROOT / "materials_catalog_v1.json"
                if cat_path.is_file():
                    cat = json.loads(cat_path.read_text(encoding="utf-8"))
                    for mid, pts in (cat.get("pillars") or {}).items():
                        loaded[mid] = [(float(p[0]), float(p[1]), float(p[2])) for p in pts if len(p) >= 3]
            except Exception:
                loaded = {}
            cls._pillars_by_map = loaded
        return cls._pillars_by_map.get(map_id, [])

    @classmethod
    def get(cls, map_id: str) -> "_TerrainSampler":
        with cls._lock:
            sampler = cls._cache.get(map_id)
            if sampler is None:
                sampler = cls(map_id)
                cls._cache[map_id] = sampler
            return sampler

    def sample_z(self, wx: float, wy: float, fallback: float = 0.0) -> float:
        if self.z_grid is None:
            return fallback
        gx = wx * self.px_scale
        gy = wy * self.px_scale
        x0 = int(math.floor(gx))
        y0 = int(math.floor(gy))
        if x0 < 0 or x0 >= self.w - 1 or y0 < 0 or y0 >= self.h - 1:
            ix = min(self.w - 1, max(0, int(round(gx))))
            iy = min(self.h - 1, max(0, int(round(gy))))
            v = float(self.z_grid[iy, ix])
            return fallback if math.isnan(v) else v
        fx = gx - x0
        fy = gy - y0
        z00 = float(self.z_grid[y0, x0])
        z10 = float(self.z_grid[y0, x0 + 1])
        z01 = float(self.z_grid[y0 + 1, x0])
        z11 = float(self.z_grid[y0 + 1, x0 + 1])
        if not (math.isnan(z00) or math.isnan(z10) or math.isnan(z01) or math.isnan(z11)):
            return (
                z00 * (1.0 - fx) * (1.0 - fy)
                + z10 * fx * (1.0 - fy)
                + z01 * (1.0 - fx) * fy
                + z11 * fx * fy
            )
        vals = [v for v in (z00, z10, z01, z11) if not math.isnan(v)]
        return sum(vals) / len(vals) if vals else fallback

    def is_base_obstacle(self, wx: float, wy: float) -> bool:
        if self.obs_grid is None:
            return False
        ix = min(self.w - 1, max(0, int(round(wx * self.px_scale))))
        iy = min(self.h - 1, max(0, int(round(wy * self.px_scale))))
        return bool(self.obs_grid[iy, ix])

    def nearest_pillar_z(self, wx: float, wy: float, max_dist_m: float = 14.0) -> float | None:
        best_d = max_dist_m
        best_z: float | None = None
        for px, py, pz in self.pillars:
            d_m = math.hypot(wx - px, wy - py) * 0.90
            if d_m <= best_d:
                best_d = d_m
                best_z = pz
        return best_z

    def _get_mesh(self, mesh_idx: int) -> tuple[np.ndarray, np.ndarray] | None:
        if mesh_idx in self._mesh_cache:
            return self._mesh_cache[mesh_idx]
        if mesh_idx < 0 or mesh_idx >= len(self._raw_meshes):
            self._mesh_cache[mesh_idx] = None
            return None
        try:
            m = self._raw_meshes[mesh_idx]
            bx0, by0, bz0, bx1, by1, bz1 = (float(m[k]) for k in range(6))
            dx, dy, dz = max(1.0, bx1 - bx0), max(1.0, by1 - by0), max(1.0, bz1 - bz0)
            vb = np.frombuffer(base64.b64decode(m[6]), dtype=np.uint8).reshape(-1, 3).astype(np.float32)
            tb = base64.b64decode(m[7]) if len(m) > 7 and m[7] else b""
            if len(vb) < 3 or len(tb) < 3:
                self._mesh_cache[mesh_idx] = None
                return None
            tris = np.frombuffer(tb, dtype=np.uint8).reshape(-1, 3)
            if int(tris.max()) >= len(vb):
                self._mesh_cache[mesh_idx] = None
                return None
            verts = np.column_stack((
                bx0 + vb[:, 0] * (dx / 255.0),
                by0 + vb[:, 1] * (dy / 255.0),
                bz0 + vb[:, 2] * (dz / 255.0),
            ))
            res = (verts, tris)
        except Exception:
            res = None
        self._mesh_cache[mesh_idx] = res
        return res

    def _get_inst_tris(self, idx: int) -> tuple | None:
        if idx in self._inst_cache:
            return self._inst_cache[idx]
        row = self._instances[idx]
        mesh_idx = int(row[8]) if len(row) > 8 else max(0, min(5, int(row[6]) - 1))
        mesh = self._get_mesh(mesh_idx)
        if mesh is None:
            self._inst_cache[idx] = None
            return None
        verts, tris = mesh
        tx = float(row[9]) if len(row) > 9 else (float(row[0]) + float(row[2])) * 0.5
        ty = float(row[10]) if len(row) > 10 else (float(row[1]) + float(row[3])) * 0.5
        tz = float(row[11]) if len(row) > 11 else (float(row[4]) + float(row[5])) * 0.5
        yr = math.radians(float(row[7]) if len(row) > 7 else 0.0)
        pr = math.radians(float(row[12]) if len(row) > 12 else 0.0)
        rr = math.radians(float(row[13]) if len(row) > 13 else 0.0)
        sx = float(row[14]) if len(row) > 14 else 1.0
        sy = float(row[15]) if len(row) > 15 else 1.0
        sz = float(row[16]) if len(row) > 16 else 1.0
        flip = -1.0 if (sx * sy * sz < 0.0) else 1.0
        cp, sp = math.cos(pr), math.sin(pr)
        cy, syr = math.cos(yr), math.sin(yr)
        cr, sr = math.cos(rr), math.sin(rr)
        rot = np.array([
            [cp * cy, cp * syr, sp],
            [sr * sp * cy - cr * syr, sr * sp * syr + cr * cy, -sr * cp],
            [-cr * sp * cy - sr * syr, -cr * sp * syr + sr * cy, cr * cp],
        ], dtype=np.float32)
        wv = (verts * np.array([sx, sy, sz], dtype=np.float32)) @ rot
        vx = tx + wv[:, 0] / 90.0
        vy = ty + wv[:, 1] / 90.0
        vz = tz + wv[:, 2] / 100.0
        i0, i1, i2 = tris[:, 0], tris[:, 1], tris[:, 2]
        x0, y0, z0 = vx[i0], vy[i0], vz[i0]
        x1, y1, z1 = vx[i1], vy[i1], vz[i1]
        x2, y2, z2 = vx[i2], vy[i2], vz[i2]
        e1 = np.column_stack(((x1 - x0) * 0.90, (y1 - y0) * 0.90, z1 - z0))
        e2 = np.column_stack(((x2 - x0) * 0.90, (y2 - y0) * 0.90, z2 - z0))
        n = -np.cross(e1, e2) * flip
        nzu = n[:, 2] / np.maximum(1e-6, np.linalg.norm(n, axis=1))
        denom = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
        keep = (np.abs(denom) > 1e-6) & ((nzu >= 0.36) | (nzu <= -0.30))
        if not np.any(keep):
            self._inst_cache[idx] = None
            return None
        x0, y0, z0 = x0[keep], y0[keep], z0[keep]
        x1, y1, z1 = x1[keep], y1[keep], z1[keep]
        x2, y2, z2 = x2[keep], y2[keep], z2[keep]
        denom = denom[keep]
        nzu = nzu[keep]
        inv_d = 1.0 / denom
        abs_d = np.abs(denom)
        e0_pad = -0.07 * np.hypot(x2 - x1, y2 - y1) / abs_d
        e1_pad = -0.07 * np.hypot(x0 - x2, y0 - y2) / abs_d
        e2_pad = -0.07 * np.hypot(x1 - x0, y1 - y0) / abs_d
        tx0 = np.minimum(np.minimum(x0, x1), x2) - 0.08
        tx1 = np.maximum(np.maximum(x0, x1), x2) + 0.08
        ty0 = np.minimum(np.minimum(y0, y1), y2) - 0.08
        ty1 = np.maximum(np.maximum(y0, y1), y2) + 0.08
        ux = (y1 - y2) * inv_d
        uy = (x2 - x1) * inv_d
        uc = -ux * x2 - uy * y2
        vx_c = (y2 - y0) * inv_d
        vy_c = (x0 - x2) * inv_d
        vc = -vx_c * x2 - vy_c * y2
        res = (tx0, tx1, ty0, ty1, ux, uy, uc, vx_c, vy_c, vc, z0, z1, z2, e0_pad, e1_pad, e2_pad, nzu)
        self._inst_cache[idx] = res
        return res

    def query_layers(self, wx: float, wy: float, fallback: float = 0.0) -> list[float]:
        qkey = (int(round(wx * 20.0)), int(round(wy * 20.0)))
        cached = self._layer_cache.get(qkey)
        if cached is not None:
            return list(cached)
        qwx = qkey[0] * 0.05
        qwy = qkey[1] * 0.05
        gz = self.sample_z(qwx, qwy, math.nan)
        struct_tops: list[float] = []
        buried_spans: list[tuple[float, float]] = []
        cand_indices = self._buckets.get((int(math.floor(qwx)) >> 5, int(math.floor(qwy)) >> 5))
        if cand_indices:
            with self._query_lock:
                for idx in cand_indices:
                    row = self._instances[idx]
                    if not (float(row[0]) - 0.12 <= qwx <= float(row[2]) + 0.12 and float(row[1]) - 0.12 <= qwy <= float(row[3]) + 0.12):
                        continue
                    td = self._get_inst_tris(idx)
                    if td is None:
                        continue
                    tx0, tx1, ty0, ty1, ux, uy, uc, vx_c, vy_c, vc, z0, z1, z2, e0_pad, e1_pad, e2_pad, nzu = td
                    m_bb = (qwx >= tx0) & (qwx <= tx1) & (qwy >= ty0) & (qwy <= ty1)
                    if not np.any(m_bb):
                        continue
                    u = ux[m_bb] * qwx + uy[m_bb] * qwy + uc[m_bb]
                    v = vx_c[m_bb] * qwx + vy_c[m_bb] * qwy + vc[m_bb]
                    w = 1.0 - u - v
                    z0_m, z1_m, z2_m = z0[m_bb], z1[m_bb], z2[m_bb]
                    nzu_m = nzu[m_bb]
                    hit_pad = (u >= e0_pad[m_bb]) & (v >= e1_pad[m_bb]) & (w >= e2_pad[m_bb])
                    up_m = hit_pad & (nzu_m >= 0.38)
                    if np.any(up_m):
                        zh_u = np.sort(u[up_m] * z0_m[up_m] + v[up_m] * z1_m[up_m] + w[up_m] * z2_m[up_m])
                        grp_max = float(zh_u[0])
                        for zv in zh_u[1:]:
                            zf = float(zv)
                            if zf - grp_max <= 0.65:
                                grp_max = zf
                            else:
                                struct_tops.append(grp_max)
                                grp_max = zf
                        struct_tops.append(grp_max)
                    hit_s = (u >= -1e-4) & (v >= -1e-4) & (w >= -1e-4)
                    up_s = hit_s & (nzu_m >= 0.38)
                    dn_s = hit_s & (nzu_m <= -0.30)
                    if np.any(up_s) and np.any(dn_s):
                        z_up_s = float(np.max(u[up_s] * z0_m[up_s] + v[up_s] * z1_m[up_s] + w[up_s] * z2_m[up_s]))
                        z_dn_s = float(np.min(u[dn_s] * z0_m[dn_s] + v[dn_s] * z1_m[dn_s] + w[dn_s] * z2_m[dn_s]))
                        if 0.05 < z_up_s - z_dn_s <= 24.0:
                            buried_spans.append((z_dn_s - 0.80, z_up_s - 0.18))

        raw: list[tuple[float, bool]] = [(z, True) for z in struct_tops]
        if not math.isnan(gz):
            raw.append((gz, False))
        if not raw:
            return [fallback]
        raw.sort(key=lambda it: (it[0], it[1]))
        filtered: list[tuple[float, bool]] = []
        for zc, is_st in raw:
            if any(lo <= zc < hi for lo, hi in buried_spans):
                continue
            filtered.append((zc, is_st))
        if not filtered:
            filtered = [raw[-1]]
        merged: list[tuple[float, bool]] = [filtered[0]]
        for zc, is_st in filtered[1:]:
            pz, pst = merged[-1]
            if zc - pz <= 0.26:
                if is_st or not pst:
                    merged[-1] = (zc, is_st)
            else:
                merged.append((zc, is_st))
        out_tuple = tuple(z for z, _ in merged)
        if len(self._layer_cache) > 24576:
            self._layer_cache.clear()
        self._layer_cache[qkey] = out_tuple
        return list(out_tuple)

    @staticmethod
    def _step_cost(z0: float, z1: float, ds_m: float, is_jump: bool, obs1: bool, multi1: bool) -> float:
        dz = z1 - z0
        pen = 1.6 if (obs1 and multi1) else 0.0
        if is_jump:
            max_r = 0.75 + ds_m * 0.95
            min_r = -1.20 - ds_m * 1.15
            if min_r <= dz <= max_r:
                return pen + abs(dz) * 0.38
            if max_r < dz <= 4.8:
                return pen + 0.35 + abs(dz) * 0.42
            if -18.0 <= dz < min_r:
                return pen + 0.55 + abs(dz) * 0.45
            return pen + 28.0 + abs(dz) * 9.0
        max_w = 0.72 + ds_m * 0.95
        min_w = -0.85 - ds_m * 1.05
        if min_w <= dz <= max_w:
            return pen + abs(dz) * 0.45
        if dz < min_w:
            return pen + 1.20 + abs(dz) * 0.85
        return pen + 16.0 + dz * 6.5

    def resolve_char_surface_z(
        self,
        wx: float,
        wy: float,
        prev_z: float | None,
        upcoming_pts: list[tuple[float, float, bool]],
    ) -> float:
        layers = self.query_layers(wx, wy, prev_z if prev_z is not None else 0.0)
        if prev_z is None:
            pz = self.nearest_pillar_z(wx, wy, 14.0)
            if pz is not None:
                best = min(layers, key=lambda z: abs(z - pz))
                if abs(best - pz) <= 4.5:
                    return best
            if len(layers) > 1 and upcoming_pts:
                chain = [(wx, wy, False)] + upcoming_pts[:4]
                node_layers = [layers] + [self.query_layers(px, py, layers[0]) for px, py, _ in upcoming_pts[:4]]
                obs0 = self.is_base_obstacle(wx, wy)
                best_start = 0
                best_total = math.inf
                for s_idx in range(len(layers)):
                    dp = [math.inf] * len(layers)
                    dp[s_idx] = 1.5 if (obs0 and s_idx == 0) else 0.02 * s_idx
                    for step_i in range(1, len(chain)):
                        x0, y0, _ = chain[step_i - 1]
                        x1, y1, jmp = chain[step_i]
                        ds_m = math.hypot(x1 - x0, y1 - y0) * 0.90
                        cur_ls = node_layers[step_i]
                        obs1 = self.is_base_obstacle(x1, y1)
                        dp = [
                            min(
                                dp[k] + self._step_cost(node_layers[step_i - 1][k], z1, ds_m, jmp, obs1 and j == 0, len(cur_ls) > 1)
                                for k in range(len(dp))
                            )
                            for j, z1 in enumerate(cur_ls)
                        ]
                    tot = min(dp)
                    if tot < best_total:
                        best_total = tot
                        best_start = s_idx
                return layers[best_start]
            return layers[1] if (len(layers) > 1 and self.is_base_obstacle(wx, wy)) else layers[0]

        # Capsule-perimeter support check (0.25m foot radius) to prevent minimap jitter on ledge edges
        if all(abs(z - prev_z) > 0.60 for z in layers):
            z_low = min(layers)
            for ox, oy in ((0.28, 0.0), (-0.28, 0.0), (0.0, 0.28), (0.0, -0.28)):
                for zp in self.query_layers(wx + ox, wy + oy, prev_z):
                    if abs(zp - prev_z) <= 0.48 and zp > z_low + 0.60:
                        layers = sorted(layers + [zp])
                        break
                else:
                    continue
                break

        obs_here = self.is_base_obstacle(wx, wy)
        best_z = layers[0]
        best_c = math.inf
        for idx_l, zc in enumerate(layers):
            dz = zc - prev_z
            if abs(dz) <= 0.85:
                c = abs(dz) * 0.4
            elif 0.85 < dz <= 4.8:
                c = 1.2 + dz * 0.85
            elif dz < -0.85:
                c = 1.0 + abs(dz) * 0.65
            else:
                c = 30.0 + abs(dz) * 8.0
            if obs_here and idx_l == 0 and len(layers) > 1:
                c += 1.5
            if c < best_c:
                best_c = c
                best_z = zc
        return best_z

    def sample_footing_slope(self, wx: float, wy: float, surface_z: float) -> tuple[float, float]:
        """Return continuous footing gradient (dz/dx_m, dz/dy_m), rejecting cliff/wall steps."""
        step_px = 1.2  # ~1.08m around character capsule

        def _same_layer(nx: float, ny: float) -> float | None:
            ls = self.query_layers(nx, ny, surface_z)
            near = min(ls, key=lambda z: abs(z - surface_z))
            return near if abs(near - surface_z) <= 0.68 else None

        zx1, zx0 = _same_layer(wx + step_px, wy), _same_layer(wx - step_px, wy)
        zy1, zy0 = _same_layer(wx, wy + step_px), _same_layer(wx, wy - step_px)
        half_m = step_px * 0.90
        if zx1 is not None and zx0 is not None:
            dz_dx = (zx1 - zx0) / (2.0 * half_m)
        elif zx1 is not None:
            dz_dx = (zx1 - surface_z) / half_m
        elif zx0 is not None:
            dz_dx = (surface_z - zx0) / half_m
        else:
            dz_dx = 0.0

        if zy1 is not None and zy0 is not None:
            dz_dy = (zy1 - zy0) / (2.0 * half_m)
        elif zy1 is not None:
            dz_dy = (zy1 - surface_z) / half_m
        elif zy0 is not None:
            dz_dy = (surface_z - zy0) / half_m
        else:
            dz_dy = 0.0
        return max(-0.40, min(0.40, dz_dx)), max(-0.40, min(0.40, dz_dy))

    def solve_path_elevations(
        self,
        dense_pts: list[tuple[float, float, bool, str]],
        start_z: float,
        tail_wps: list[tuple[float, float, bool]],
    ) -> tuple[list[float], list[float]]:
        if not dense_pts:
            return [], []
        nodes: list[tuple[float, float, bool]] = [(p[0], p[1], p[2]) for p in dense_pts] + list(tail_wps)
        node_layers: list[list[float]] = [[start_z]]
        node_obs: list[bool] = [False]
        for wx, wy, _ in nodes[1:]:
            node_layers.append(self.query_layers(wx, wy, start_z))
            node_obs.append(self.is_base_obstacle(wx, wy))

        n_nodes = len(nodes)
        back_ptrs: list[list[int]] = [[0]]
        dp = [0.0]
        for i in range(1, n_nodes):
            x0, y0, _ = nodes[i - 1]
            x1, y1, is_jmp = nodes[i]
            ds_m = math.hypot(x1 - x0, y1 - y0) * 0.90
            prev_ls = node_layers[i - 1]
            cur_ls = node_layers[i]
            obs1 = node_obs[i]
            multi1 = len(cur_ls) > 1
            cur_dp: list[float] = []
            cur_bp: list[int] = []
            for j, z1 in enumerate(cur_ls):
                best_k = 0
                best_v = math.inf
                o_flag = obs1 and j == 0
                for k, z0 in enumerate(prev_ls):
                    v = dp[k] + self._step_cost(z0, z1, ds_m, is_jmp, o_flag, multi1)
                    if v < best_v:
                        best_v = v
                        best_k = k
                cur_dp.append(best_v)
                cur_bp.append(best_k)
            dp = cur_dp
            back_ptrs.append(cur_bp)

        idx_best = int(min(range(len(dp)), key=dp.__getitem__))
        solved = [0.0] * n_nodes
        for i in range(n_nodes - 1, -1, -1):
            solved[i] = node_layers[i][idx_best]
            idx_best = back_ptrs[i][idx_best]

        m = len(dense_pts)
        zs = solved[:m]
        tail_zs = solved[m:]

        # Bridge narrow (<3.2m) V-shaped crevasses/tile seams where surface drops >0.65m and immediately recovers
        if m >= 3:
            cum = [0.0] * m
            for i in range(1, m):
                cum[i] = cum[i - 1] + math.hypot(dense_pts[i][0] - dense_pts[i - 1][0], dense_pts[i][1] - dense_pts[i - 1][1]) * 0.90
            i = 0
            while i < m - 2:
                bridged = False
                for j in range(min(m - 1, i + 8), i + 1, -1):
                    span_m = cum[j] - cum[i]
                    if span_m <= 0.05 or span_m > 3.2 or abs(zs[j] - zs[i]) > 0.85:
                        continue
                    min_drop = min(zs[k] - (zs[i] + (zs[j] - zs[i]) * ((cum[k] - cum[i]) / span_m)) for k in range(i + 1, j))
                    if min_drop < -0.65:
                        for k in range(i + 1, j):
                            u_t = (cum[k] - cum[i]) / span_m
                            zs[k] = max(zs[k], zs[i] + (zs[j] - zs[i]) * u_t)
                        i = j
                        bridged = True
                        break
                if not bridged:
                    i += 1

            # Suppress narrow (<2.2m) grazed-column/roof spikes that immediately return to the same base elevation
            i = 0
            while i < m - 2:
                bridged = False
                for j in range(min(m - 1, i + 7), i + 1, -1):
                    span_m = cum[j] - cum[i]
                    if span_m <= 0.05 or span_m > 2.2 or abs(zs[j] - zs[i]) > 0.75:
                        continue
                    max_rise = max(zs[k] - (zs[i] + (zs[j] - zs[i]) * ((cum[k] - cum[i]) / span_m)) for k in range(i + 1, j))
                    has_jump = any(dense_pts[k][2] for k in range(i + 1, j + 1))
                    if max_rise > 3.2 or (max_rise > 0.75 and not has_jump):
                        for k in range(i + 1, j):
                            u_t = (cum[k] - cum[i]) / span_m
                            zs[k] = min(zs[k], zs[i] + (zs[j] - zs[i]) * u_t)
                        i = j
                        bridged = True
                        break
                if not bridged:
                    i += 1

            # Edge-preserving micro-smoothing: smooth flat/sloped ground while keeping wall/ledge steps >0.45m razor-sharp
            for _ in range(2):
                nxt = list(zs)
                for k in range(1, m - 1):
                    if abs(zs[k] - zs[k - 1]) <= 0.45 and abs(zs[k + 1] - zs[k]) <= 0.45:
                        nxt[k] = 0.25 * zs[k - 1] + 0.50 * zs[k] + 0.25 * zs[k + 1]
                zs = nxt

        return zs, tail_zs


# Unpacked 3C jump + float vertical profile (DA_PlayerJumpStageConfig_Nikki_DoubleJump_V3 +
# Curve_JumpGravityCurve_Nikki_Jump_V3: JumpZVelocity=10.0m/s, 0.30s hold + 0.10s gap ->
# 2.148m apex at t=0.395s, then Float stage FloatGravity=0.1 capped at MaxZVelocity=-1.0m/s).
_JUMP_V3_ASCENT_M = (0.000, 0.453, 0.816, 1.129, 1.419, 1.694, 1.938, 2.101, 2.148)
_JUMP_V3_APEX_S = 0.40
_JUMP_V3_APEX_M = 2.148
_FLOAT_MAX_AIRTIME_S = 3.05


def _nikki_jump_float_height_m(dt: float) -> float:
    """Evaluate Nikki's unpacked Jump_V3 ascent + Floating descent offset above takeoff (meters)."""
    if dt <= 0.0 or dt >= _FLOAT_MAX_AIRTIME_S:
        return 0.0
    if dt <= _JUMP_V3_APEX_S:
        pos = (dt / _JUMP_V3_APEX_S) * (len(_JUMP_V3_ASCENT_M) - 1)
        idx = min(len(_JUMP_V3_ASCENT_M) - 2, int(pos))
        frac = pos - idx
        return _JUMP_V3_ASCENT_M[idx] + (_JUMP_V3_ASCENT_M[idx + 1] - _JUMP_V3_ASCENT_M[idx]) * frac
    s = dt - _JUMP_V3_APEX_S
    if s <= 0.95:
        return max(0.0, _JUMP_V3_APEX_M - 0.07 * s - 0.49 * s * s)
    return max(0.0, 1.639 - 1.00 * (s - 0.95))


def _premul(bgr: tuple[int, int, int], alpha: float) -> tuple[int, int, int, int]:
    a = max(0.0, min(1.0, float(alpha)))
    return (
        int(round(bgr[0] * a)),
        int(round(bgr[1] * a)),
        int(round(bgr[2] * a)),
        int(round(255.0 * a)),
    )


MXU_CONFIG_FILE = DATA_ROOT / "config/mxu-MaaNikki.json"
_CASE_TO_MODE = {
    "Full": "full",
    "MinimapOnly": "minimap",
    "Off": "off",
    "full": "full",
    "minimap": "minimap",
    "off": "off",
}


def _mxu_mtime_ns() -> int:
    try:
        return MXU_CONFIG_FILE.stat().st_mtime_ns
    except OSError:
        return 0


def _read_live_overlay_mode(fallback: str = "full", *, prefer_global: bool = False) -> str:
    try:
        if not MXU_CONFIG_FILE.is_file():
            return fallback
        cfg = json.loads(MXU_CONFIG_FILE.read_text(encoding="utf-8-sig"))
        if not isinstance(cfg, dict):
            return fallback
        if not prefer_global:
            active_id = cfg.get("lastActiveInstanceId")
            inst = next((it for it in cfg.get("instances", []) if isinstance(it, dict) and it.get("id") == active_id), None)
            if isinstance(inst, dict):
                for task in inst.get("tasks", []):
                    if not isinstance(task, dict) or not task.get("enabled", True):
                        continue
                    if task.get("taskName") in ("open_world_collect", "custom_route"):
                        t_opt = (task.get("optionValues") or {}).get("TaskRouteOverlay")
                        if isinstance(t_opt, dict):
                            t_case = t_opt.get("caseName")
                            if t_case in ("Full", "MinimapOnly", "Off"):
                                return _CASE_TO_MODE[t_case]
        g_opt = (cfg.get("globalOptionValues") or {}).get("RouteOverlayMode")
        if isinstance(g_opt, dict):
            if g_opt.get("type") == "switch":
                return "full" if g_opt.get("value", True) else "off"
            g_case = g_opt.get("caseName")
            if g_case in _CASE_TO_MODE:
                return _CASE_TO_MODE[g_case]
    except Exception:
        pass
    return fallback


def resolve_route_overlay_config(context=None) -> tuple[str, bool]:
    """Return (mode, is_task_override) from pipeline override or live config."""
    if context is not None:
        try:
            from client_config import node_action_params
            params = node_action_params(context, "MaaNikki_RouteOverlay")
            raw = str(params.get("mode") or "").strip()
            if raw in _CASE_TO_MODE:
                return _CASE_TO_MODE[raw], bool(params.get("task_override"))
        except Exception:
            pass
    return _read_live_overlay_mode("full"), False


def resolve_route_overlay_mode(context=None) -> str:
    return resolve_route_overlay_config(context)[0]



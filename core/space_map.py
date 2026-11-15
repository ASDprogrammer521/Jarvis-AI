"""The maths of the Hand Space map (no Qt, no network): a Google-Maps-style "slippy" map of the world.

The world is a square of 256 · 2^z pixels (Web Mercator, as OpenStreetMap / Google use). `MapView` keeps where the view is
looking (`x`, `y` in 0…1 of the world, and `z`, a *fractional* zoom) plus a TARGET the hand moves; every screen frame the
shown view eases toward the target (`step`), so a 10-30 Hz hand still pans and zooms smoothly. `tiles()` lists the 256-px
picture tiles that cover the window; a tile that has not arrived yet is stood in for by its (blurry) parent (`ancestor`).
"""
from __future__ import annotations

import math

TILE = 256
MAX_TILE_Z = 19                 # the highest zoom OpenStreetMap serves
MIN_Z, MAX_Z = 1.0, 19.0
MAX_LAT = 85.05112878           # Web Mercator's limit
MAX_TILES = 80                  # safety net: never ask for more than this many tiles for one window


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def lonlat_to_xy(lat: float, lon: float) -> tuple[float, float]:
    """Latitude / longitude (degrees) → position in the world square, x and y in 0…1 (y grows southwards)."""
    lat = clamp(float(lat), -MAX_LAT, MAX_LAT)
    x = (float(lon) + 180.0) / 360.0
    s = math.sin(math.radians(lat))
    y = 0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)
    return x % 1.0, clamp(y, 0.0, 1.0)


def xy_to_lonlat(x: float, y: float) -> tuple[float, float]:
    lon = x * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * clamp(y, 0.0, 1.0)))))
    return lat, lon


def zoom_for_bbox(south: float, north: float, west: float, east: float, w: float, h: float) -> float:
    """The zoom at which a bounding box (degrees) just fits a w × h window (a city fills the screen, a country shows whole)."""
    x0, y1 = lonlat_to_xy(south, west)
    x1, y0 = lonlat_to_xy(north, east)
    sx = max(1e-9, abs(x1 - x0) if x1 != x0 else 1e-9)
    sy = max(1e-9, abs(y1 - y0))
    if east < west:                                                     # crosses the date line
        sx = max(1e-9, 1.0 - abs(x1 - x0))
    z = min(math.log2(max(1.0, w) / (TILE * sx)), math.log2(max(1.0, h) / (TILE * sy)))
    return clamp(z - 0.2, 3.0, 17.0)


class MapView:
    def __init__(self, lat: float = 25.0, lon: float = 30.0, z: float = 2.2):
        self.x, self.y = lonlat_to_xy(lat, lon)          # what is shown
        self.z = float(z)
        self.tx, self.ty, self.tz = self.x, self.y, self.z    # where the hand is taking it
        self.marker: tuple[float, float, str] | None = None   # (lat, lon, label)
        self.h = 1080.0                                       # window height: the world must fill it (no bands beyond the poles)
        self.min_z = MIN_Z

    def set_window(self, w: float, h: float) -> None:
        """Tell the view how big its window is: the lowest zoom is the one where the world is as tall as the window."""
        self.h = max(64.0, float(h))
        self.min_z = max(MIN_Z, math.log2(self.h / TILE))
        self.tz, self.z = max(self.tz, self.min_z), max(self.z, self.min_z)
        self.ty, self.y = self._ylim(self.ty, self.tz), self._ylim(self.y, self.z)

    def _ylim(self, y: float, z: float) -> float:
        half = (self.h / 2) / (TILE * 2.0 ** z)                       # half the window, in world units
        return clamp(y, half, 1.0 - half) if half < 0.5 else 0.5

    # ── moving the target ────────────────────────────────────────────────────────
    def pan_px(self, dx: float, dy: float) -> None:
        """Drag the map by (dx, dy) screen pixels."""
        s = TILE * 2.0 ** self.tz
        self.tx = (self.tx - dx / s) % 1.0
        self.ty = self._ylim(self.ty - dy / s, self.tz)

    def zoom_by(self, dz: float, ax: float = 0.0, ay: float = 0.0) -> None:
        """Zoom the target by dz, keeping the spot at (ax, ay) pixels from the window centre where it is."""
        self.set_zoom(self.tz + dz, ax, ay)

    def set_zoom(self, z: float, ax: float = 0.0, ay: float = 0.0) -> None:
        new = clamp(z, self.min_z, MAX_Z)
        s0, s1 = TILE * 2.0 ** self.tz, TILE * 2.0 ** new
        wx, wy = self.tx + ax / s0, self.ty + ay / s0                 # the world point under the pointer
        self.tx = (wx - ax / s1) % 1.0
        self.tz = new
        self.ty = self._ylim(wy - ay / s1, new)

    def goto(self, lat: float, lon: float, z: float | None = None) -> None:
        self.tx, self.ty = lonlat_to_xy(lat, lon)
        if z is not None:
            self.tz = clamp(z, self.min_z, MAX_Z)
        self.ty = self._ylim(self.ty, self.tz)

    def jump(self, lat: float, lon: float, z: float) -> None:
        """Go there at once (no easing): the home position when the map is first shown."""
        self.goto(lat, lon, z)
        self.x, self.y, self.z = self.tx, self.ty, self.tz

    def set_marker(self, lat: float, lon: float, label: str) -> None:
        self.marker = (float(lat), float(lon), str(label)[:140])

    # ── easing (every screen frame) ──────────────────────────────────────────────
    def step(self, dt: float, tau_pan: float = 0.06, tau_zoom: float = 0.09) -> bool:
        """Moves the shown view toward the target; True while it is still moving (the window keeps repainting)."""
        ap = 1.0 - math.exp(-dt / max(1e-3, tau_pan))
        az = 1.0 - math.exp(-dt / max(1e-3, tau_zoom))
        dx = (self.tx - self.x + 0.5) % 1.0 - 0.5                      # shortest way round the globe
        self.x = (self.x + dx * ap) % 1.0
        self.z += (self.tz - self.z) * az
        self.y = self._ylim(self.y + (self.ty - self.y) * ap, self.z)
        moving = abs(dx) > 1e-7 or abs(self.ty - self.y) > 1e-7 or abs(self.tz - self.z) > 1e-4
        if not moving:
            self.x, self.y, self.z = self.tx, self.ty, self.tz
        elif abs(dx) < 1e-8 and abs(self.ty - self.y) < 1e-8 and abs(self.tz - self.z) < 1e-5:
            self.x, self.y, self.z = self.tx, self.ty, self.tz
            moving = False
        return moving

    # ── what to draw ─────────────────────────────────────────────────────────────
    def level(self) -> int:
        return int(clamp(math.floor(self.z + 0.5), 0, MAX_TILE_Z))

    def tiles(self, w: float, h: float):
        """→ (Z, scale, [(tile_x, tile_y, screen_x, screen_y), …]) for a w × h window centred on the view.
        Tiles are 256 px at level Z; they are drawn at `scale` (= 2^(z - Z), 0.7…1.4) so the zoom is continuous."""
        Z = self.level()
        n = 2 ** Z
        s = 2.0 ** (self.z - Z)
        cx, cy = self.x * n * TILE, self.y * n * TILE
        hw, hh = w / 2 / s, h / 2 / s
        x0, x1 = math.floor((cx - hw) / TILE), math.floor((cx + hw) / TILE)
        y0, y1 = max(0, math.floor((cy - hh) / TILE)), min(n - 1, math.floor((cy + hh) / TILE))
        out = []
        for ty in range(y0, y1 + 1):
            for tx in range(x0, x1 + 1):
                out.append((tx % n, ty, w / 2 + (tx * TILE - cx) * s, h / 2 + (ty * TILE - cy) * s))
                if len(out) >= MAX_TILES:
                    return Z, s, out
        return Z, s, out

    def project(self, lat: float, lon: float, w: float, h: float) -> tuple[float, float]:
        """Window position (pixels) of a place, for the marker."""
        px, py = lonlat_to_xy(lat, lon)
        s = TILE * 2.0 ** self.z
        dx = (px - self.x + 0.5) % 1.0 - 0.5
        return w / 2 + dx * s, h / 2 + (py - self.y) * s


def ancestor(z: int, x: int, y: int, k: int):
    """The tile k levels up that contains tile (z, x, y), and which part of it (in its 256-px picture) is ours:
    → (z - k, x >> k, y >> k, (sx, sy, size)) or None when k goes above the world."""
    if k < 1 or z - k < 0:
        return None
    size = TILE / (1 << k)
    return z - k, x >> k, y >> k, ((x & ((1 << k) - 1)) * size, (y & ((1 << k) - 1)) * size, size)

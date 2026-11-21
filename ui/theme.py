from __future__ import annotations

import colorsys
import json
from pathlib import Path

from PyQt6.QtGui import QColor

DEFAULT_ACCENT = "#3d9bff"


class C:
    BG       = "#06070f"
    BG2      = "#0b0d1c"
    PANEL    = "#10132a"
    PANEL2   = "#161a38"
    BORDER   = "#262b52"
    BORDER_B = "#3a4180"
    PRI      = "#6ee7ff"
    PRI_DIM  = "#3fb6d6"
    PRI_GHO  = "#12303f"
    ACC      = "#b388ff"
    ACC2     = "#ff7ad9"
    GREEN    = "#5cffb0"
    AMBER    = "#ffc857"
    RED      = "#ff5c7a"
    TEXT     = "#eef2ff"
    TEXT_MED = "#aab3d9"
    TEXT_DIM = "#6b74a6"


_FIXED = {n: getattr(C, n) for n in ("TEXT", "GREEN", "AMBER", "RED")}      # colours a theme may override but the accent never touches


class T:
    """Design tokens: the one place for corner radii, spacing, type sizes and animation timings.

    New widgets take their numbers from here (and their colours from C), so everything stays consistent and a change
    is made once. Pair it with `card_css()` / `button_css()` below for QSS-styled widgets."""
    # corner radii
    R_XS, R_SM, R_MD, R_LG, R_XL, R_PILL = 6, 10, 14, 22, 28, 999
    # spacing scale (px)
    S1, S2, S3, S4, S5, S6 = 4, 8, 12, 16, 24, 32
    # type scale (points)
    F_CAPTION, F_BODY, F_LABEL, F_TITLE, F_HEADING, F_DISPLAY = 8, 10, 11, 13, 18, 28
    # animation (ms): quick feedback, normal transitions, larger moves
    D_FAST, D_BASE, D_SLOW = 150, 200, 250
    # elevation: how opaque glass cards are (0-255)
    TINT_LOW, TINT_MID, TINT_HIGH = 140, 190, 225


def card_css(selector: str = "QFrame", radius: int | None = None, accent: bool = False) -> str:
    """A glass-card look as QSS (for plain widgets that are not GlassCard)."""
    r = T.R_LG if radius is None else radius
    edge = rgba(C.PRI, .38) if accent else rgba("#ffffff", .10)
    return (f"{selector} {{ background: {rgba(C.PANEL, .82)}; border: 1px solid {edge}; border-radius: {r}px; }}")


def button_css(selector: str = "QPushButton", primary: bool = False) -> str:
    base = rgba(C.PRI, .22) if primary else rgba("#ffffff", .07)
    hov = rgba(C.PRI, .38) if primary else rgba("#ffffff", .13)
    return (f"{selector} {{ background: {base}; color: {C.TEXT}; border: 1px solid {rgba(C.PRI, .45) if primary else rgba('#ffffff', .12)};"
            f" border-radius: {T.R_MD}px; padding: {T.S2}px {T.S4}px; font-weight: 600; }}"
            f"{selector}:hover {{ background: {hov}; }}")


def qcol(h: str, a: int = 255) -> QColor:
    c = QColor(h)
    c.setAlpha(max(0, min(255, int(a))))
    return c


def rgba(h: str, a: float) -> str:
    c = QColor(h)
    return f"rgba({c.red()},{c.green()},{c.blue()},{max(0.0, min(1.0, a)):.3f})"


def _hex(r: float, g: float, b: float) -> str:
    return "#{:02x}{:02x}{:02x}".format(int(r * 255 + .5), int(g * 255 + .5), int(b * 255 + .5))


def _hsv(h: str):
    c = QColor(h)
    return colorsys.rgb_to_hsv(c.red() / 255, c.green() / 255, c.blue() / 255)


def apply_accent(hex_color: str) -> bool:
    hex_color = (hex_color or "").strip().lower()
    c = QColor(hex_color)
    if not c.isValid() or not hex_color.startswith("#") or len(hex_color) != 7:
        return False
    h, s, v = _hsv(hex_color)
    v = max(v, 0.75)
    s = max(s, 0.28) if s > 0.08 else s
    C.PRI = _hex(*colorsys.hsv_to_rgb(h, s, v))
    C.PRI_DIM = _hex(*colorsys.hsv_to_rgb(h, min(1, s + .1), v * .72))
    C.PRI_GHO = _hex(*colorsys.hsv_to_rgb(h, .7, .22))
    C.ACC = _hex(*colorsys.hsv_to_rgb((h + .17) % 1, max(.45, s * .8), 1.0))
    C.ACC2 = _hex(*colorsys.hsv_to_rgb((h - .13) % 1, max(.45, s * .8), 1.0))
    sat = 0.0 if s < 0.08 else 1.0
    C.BG = _hex(*colorsys.hsv_to_rgb(h, .35 * sat, .06))
    C.BG2 = _hex(*colorsys.hsv_to_rgb(h, .38 * sat, .10))
    C.PANEL = _hex(*colorsys.hsv_to_rgb(h, .40 * sat, .17))
    C.PANEL2 = _hex(*colorsys.hsv_to_rgb(h, .40 * sat, .23))
    C.BORDER = _hex(*colorsys.hsv_to_rgb(h, .38 * sat, .32))
    C.BORDER_B = _hex(*colorsys.hsv_to_rgb(h, .38 * sat, .48))
    C.TEXT_MED = _hex(*colorsys.hsv_to_rgb(h, .14 * sat, .86))
    C.TEXT_DIM = _hex(*colorsys.hsv_to_rgb(h, .24 * sat, .62))
    return True


# ── ready-made themes ──────────────────────────────────────────────────────────────────────────────────────────────────────
# Each theme is an accent colour (everything else — backgrounds, panels, borders, text — is derived from it exactly like the
# accent picker does) plus optional explicit overrides for the palette colours that should not follow the accent.
THEMES: dict[str, dict] = {
    "jarvis":   {"label": "Jarvis",   "accent": DEFAULT_ACCENT, "over": {}},
    "ironman":  {"label": "Iron Man", "accent": "#ff4a3d",
                 "over": {"ACC": "#ffc857", "ACC2": "#ff8a3d", "PRI": "#ffb23d", "PRI_DIM": "#c8791c", "PRI_GHO": "#3a1d0c",
                           "BG": "#0a0505", "BG2": "#140a08", "PANEL": "#1f100d", "PANEL2": "#2a1612",
                           "BORDER": "#4a2a22", "BORDER_B": "#7a4636"}},
    "neon":     {"label": "Neon",     "accent": "#00f0ff",
                 "over": {"ACC": "#ff2bd6", "ACC2": "#b6ff3b", "PRI": "#00f0ff", "PRI_DIM": "#00a9b5", "PRI_GHO": "#06303a",
                           "BG": "#05020d", "BG2": "#0a0618", "PANEL": "#120a2a", "PANEL2": "#1b1040",
                           "BORDER": "#35206b", "BORDER_B": "#5b34b8"}},
    "minimal":  {"label": "Minimal",  "accent": "#dcdcdc",
                 "over": {"ACC": "#9aa0a6", "ACC2": "#ffffff", "PRI": "#f2f2f2", "PRI_DIM": "#a8a8a8", "PRI_GHO": "#262626",
                           "BG": "#050505", "BG2": "#0b0b0b", "PANEL": "#141414", "PANEL2": "#1c1c1c",
                           "BORDER": "#2b2b2b", "BORDER_B": "#444444",
                           "TEXT": "#f4f4f4", "TEXT_MED": "#b5b5b5", "TEXT_DIM": "#7a7a7a"}},
    "emerald":  {"label": "Emerald",  "accent": "#2ee6a6", "over": {}},
    "sunset":   {"label": "Sunset", "accent": "#ff7a45",
                 "over": {"ACC": "#b36bff", "ACC2": "#ff4fa3", "PRI": "#ff8a4c", "PRI_DIM": "#c4562a", "PRI_GHO": "#3a1a22",
                           "BG": "#0b0612", "BG2": "#140a1f", "PANEL": "#1e1030", "PANEL2": "#2a1642",
                           "BORDER": "#4a2a6b", "BORDER_B": "#7a46a8"}},
    "arc":      {"label": "Arc Reactor", "accent": "#bfefff",
                 "over": {"ACC": "#6ecbff", "ACC2": "#ffffff", "PRI": "#d6f4ff", "PRI_DIM": "#7fb7cf", "PRI_GHO": "#10303d",
                           "BG": "#04090f", "BG2": "#09131d", "PANEL": "#0f2230", "PANEL2": "#15303f",
                           "BORDER": "#26495e", "BORDER_B": "#3f7894"}},
    "matrix":   {"label": "Matrix", "accent": "#2cff6a",
                 "over": {"ACC": "#9dff8a", "ACC2": "#00d4a0", "PRI": "#3dff7a", "PRI_DIM": "#1fa84d", "PRI_GHO": "#08301a",
                           "BG": "#010503", "BG2": "#04100a", "PANEL": "#071a0f", "PANEL2": "#0b2415",
                           "BORDER": "#124a28", "BORDER_B": "#1f8048",
                           "TEXT": "#d9ffe6", "TEXT_MED": "#8fd9a8", "TEXT_DIM": "#4f8a63"}},
    "sakura":   {"label": "Sakura", "accent": "#ff9ccf",
                 "over": {"ACC": "#d9a6ff", "ACC2": "#ffd1e6", "PRI": "#ffa8d2", "PRI_DIM": "#c46e9c", "PRI_GHO": "#3a1830",
                           "BG": "#0d0609", "BG2": "#170a12", "PANEL": "#241220", "PANEL2": "#311a2c",
                           "BORDER": "#58304a", "BORDER_B": "#8c4f77"}},
    # "Daylight" is a soft, lighter slate theme (not pure white: the glass cards and the 3D stage are built for dark surfaces).
    "daylight": {"label": "Daylight", "accent": "#2f7bff",
                 "over": {"ACC": "#7a5cff", "ACC2": "#ff6aa8", "PRI": "#7fb2ff", "PRI_DIM": "#4a86e0", "PRI_GHO": "#243a63",
                           "BG": "#2a3550", "BG2": "#323f5e", "PANEL": "#3b4a6e", "PANEL2": "#455682",
                           "BORDER": "#5a6c9a", "BORDER_B": "#7d92c4",
                           "TEXT": "#f6f8ff", "TEXT_MED": "#d0d9f2", "TEXT_DIM": "#a2b0d6"}},
}
THEME_ORDER = ("jarvis", "ironman", "neon", "minimal", "emerald", "sunset", "arc", "matrix", "sakura", "daylight")
_THEME = {"key": "jarvis"}

# Automatic day / night: with "ui_theme_auto": true in the config, Jarvis uses DAY_THEME from DAY_START to NIGHT_START
# (local time) and the theme you picked (or Jarvis) the rest of the day.
DAY_THEME = "daylight"
DAY_START, NIGHT_START = 7, 19


def auto_theme_key(base: str, hour: int | None = None) -> str:
    """The theme to show right now: DAY_THEME during the day, `base` at night."""
    import datetime
    h = datetime.datetime.now().hour if hour is None else hour
    return DAY_THEME if DAY_START <= h < NIGHT_START else (base if base in THEMES else "jarvis")


def apply_theme(key: str) -> bool:
    """Switch the whole palette to a ready-made theme. Returns False for an unknown name."""
    key = (key or "").strip().lower()
    th = THEMES.get(key)
    if th is None:
        return False
    for name, value in _FIXED.items():
        setattr(C, name, value)
    apply_accent(th["accent"])
    for name, value in th["over"].items():
        setattr(C, name, value)
    _THEME["key"] = key
    return True


def current_theme() -> str:
    return _THEME["key"]


def build_qss() -> str:
    return f"""
    QToolTip {{ background: {C.PANEL2}; color: {C.TEXT}; border: 1px solid {C.BORDER_B};
        border-radius: {T.R_SM - 2}px; padding: 5px 9px; }}
    QLabel {{ background: transparent; color: {C.TEXT}; }}
    QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {rgba(C.TEXT_MED, .25)}; border-radius: 4px; min-height: 40px; }}
    QScrollBar::handle:vertical:hover {{ background: {rgba(C.PRI, .65)}; }}
    QTextEdit, QPlainTextEdit {{ selection-background-color: {C.PRI_DIM}; }}
    QPushButton:focus {{ outline: none; }}
    QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ background: none; height: 0; }}
    QLineEdit {{ background: {rgba('#ffffff', .05)}; color: {C.TEXT}; border: 1px solid {rgba('#ffffff', .10)};
        border-radius: {T.R_MD - 2}px; padding: 9px 14px; selection-background-color: {C.PRI_DIM}; }}
    QLineEdit:focus {{ border: 1px solid {C.PRI}; background: {rgba('#ffffff', .09)}; }}
    QComboBox {{ background: {rgba('#ffffff', .06)}; color: {C.TEXT}; border: 1px solid {rgba('#ffffff', .10)};
        border-radius: 10px; padding: 6px 12px; }}
    QComboBox:hover {{ border-color: {C.PRI}; }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox QAbstractItemView {{ background: {C.PANEL}; color: {C.TEXT}; border: 1px solid {C.BORDER_B};
        selection-background-color: {C.PRI_GHO}; outline: none; padding: 4px; }}
    QSlider::groove:horizontal {{ height: 6px; background: {rgba('#ffffff', .10)}; border-radius: 3px; }}
    QSlider::sub-page:horizontal {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {C.PRI}, stop:1 {C.ACC});
        border-radius: 3px; }}
    QSlider::handle:horizontal {{ width: 16px; height: 16px; margin: -5px 0; border-radius: 8px; background: {C.TEXT}; }}
    """


import sys

BUNDLE = Path(__file__).resolve().parent.parent
ROOT = Path(sys.executable).parent if getattr(sys, "frozen", False) else BUNDLE
ASSETS = BUNDLE / "assets"


def config_path() -> Path:
    return ROOT / "config" / "api_keys.json"


_CFG = {"key": None, "data": {}}


def read_cfg() -> dict:
    """Config file contents; parsed again only when the file changed (called from timers)."""
    try:
        path = config_path()
        st = path.stat()
        key = (st.st_mtime_ns, st.st_size)
        if _CFG["key"] != key:
            _CFG["data"] = json.loads(path.read_text(encoding="utf-8"))
            _CFG["key"] = key
        return json.loads(json.dumps(_CFG["data"]))
    except Exception:
        return {}


HOTKEY_DEFAULTS = {"mute": "F4", "fullscreen": "F11", "compact": "F12"}
HOTKEY_LABELS = {"mute": "Mute / unmute", "fullscreen": "Fullscreen", "compact": "Compact mode"}


def hotkeys() -> dict:
    """Window shortcuts: the defaults plus whatever the user changed in Settings."""
    saved = read_cfg().get("hotkeys")
    out = dict(HOTKEY_DEFAULTS)
    if isinstance(saved, dict):
        for k in HOTKEY_DEFAULTS:
            v = saved.get(k)
            if isinstance(v, str) and v.strip():
                out[k] = v.strip()
    return out


def patch_cfg(**fields) -> None:
    try:
        from memory.config_manager import _patch_config
        _patch_config(**fields)
        return
    except Exception:
        pass
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    d = read_cfg()
    d.update(fields)
    p.write_text(json.dumps(d, indent=2), encoding="utf-8")


apply_accent(DEFAULT_ACCENT)

# by ASDcuber521

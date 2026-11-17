"""Dynamic Island — a floating glass pill with the time, the weather and a music player, above everything (Hand Space too).

This is the Qt Quick edition: the pill is drawn by the Qt Quick scene graph on the GPU — Direct3D 11 on Windows — and every
movement is a spring animation inside QML (ui/qml/DynamicIsland.qml), so it follows the monitor's refresh rate instead of a
Python timer. The OS window never changes size (it is a fixed, transparent canvas, click-through everywhere except the pill);
only the pill inside it grows and shrinks.

What it does (same as before, plus the event bubbles of Windhawk's Dynamic Island):
  · resting pill: time | weather, or cover + equaliser while music plays
  · hover / click / a new song: it springs open into the player (cover, title / artist / album, progress, ⏮ ⏯ ⏭), tinted with the cover
  · pages: player (while music is loaded) · calendar · clock + weather · hardware (CPU / RAM / GPU / temp / network / battery)
  · flip pages with: the mouse wheel or two-finger touchpad scroll · the ‹ › buttons on both sides · the dots at the top
    · a sideways swipe (drag) on the open card   (Shift+drag moves the island; a sideways drag on the closed pill moves it too)
  · pull it down to open it, up to shut it; sideways or Shift+drag moves it; right-click for the menu
  · short event bubbles: charger plugged / unplugged, low battery, "Copied" when something goes to the clipboard

Settings (config/api_keys.json — everything optional):
  "dynamic_island": false                  turn it off          "dynamic_island_renderer": "widgets"   the old QPainter version
  "island_graphics": "d3d11"               d3d11 (default on Windows) | d3d12 | opengl | vulkan | software | auto
  "dynamic_island_battery": false          no battery bubbles   "dynamic_island_clipboard": false     no "Copied" bubble
Environment: JARVIS_ISLAND_API overrides island_graphics, JARVIS_ISLAND_LEGACY=1 forces the old version.

If the Qt Quick scene cannot start (no QtQuick, a QML error, no usable graphics driver) the old widget version
(ui/dynamic_island_legacy.py) takes over by itself, so the island never goes missing.

Music comes from core/media_session.py (any player on Windows through the `winrt-*` packages), the weather from Open-Meteo.
"""
from __future__ import annotations

import calendar as _cal
import colorsys
import math
import os
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QObject, QPointF, QRect, QRectF, QSize, QTimer, QUrl, Qt, pyqtProperty, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QCursor, QGuiApplication, QImage, QPainter, QRegion, QSurfaceFormat

from core import media_session, space_feeds
from ui.overlay_utils import force_topmost
from ui.theme import C, patch_cfg, read_cfg

QML_DIR = Path(__file__).resolve().parent / "qml"
M = 16                                   # transparent margin around the pill (room for its shadow)
WIN_W, WIN_H = 420, 230                  # the fixed canvas: wide enough for the open card (388) + margin, tall enough for 186 + shadow
DEFAULT_TOP = 34                         # distance from the top edge — a little lower than the very edge
_APIS = ("d3d11", "d3d12", "opengl", "vulkan", "metal", "software")


# ═════════════════════════════════════════════════════════════════════════════════
#  helpers
# ═════════════════════════════════════════════════════════════════════════════════
def _configure_graphics() -> str:
    """Pick the graphics API of every Qt Quick window. Must run before the first QQuickWindow exists."""
    from PyQt6.QtQuick import QQuickWindow, QSGRendererInterface
    cfg = read_cfg()
    want = str(os.environ.get("JARVIS_ISLAND_API") or cfg.get("island_graphics") or ("d3d11" if sys.platform == "win32" else "auto")).lower()
    G = QSGRendererInterface.GraphicsApi
    table = {"d3d11": G.Direct3D11, "d3d12": G.Direct3D12, "opengl": G.OpenGL, "vulkan": G.Vulkan, "metal": G.Metal, "software": G.Software}
    if want in table:
        QQuickWindow.setGraphicsApi(table[want])
    return want


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    return QColor(int(a.red() + (b.red() - a.red()) * t), int(a.green() + (b.green() - a.green()) * t), int(a.blue() + (b.blue() - a.blue()) * t))


def _accent_of(img: QImage) -> QColor:
    """The cover's strongest colour, lifted so it reads on black."""
    small = img.scaled(16, 16, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    px = []
    for y in range(16):
        for x in range(16):
            c = QColor(small.pixel(x, y))
            px.append((c.saturationF() * c.valueF(), c.red(), c.green(), c.blue()))
    px.sort(reverse=True)
    top = px[:40]
    r, g, b = (sum(p[i] for p in top) / len(top) for i in (1, 2, 3))
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    if s > 0.15:
        s = max(s, 0.6)
    v = max(v, 0.88)
    rr, gg, bb = colorsys.hsv_to_rgb(h, s, v)
    return QColor(int(rr * 255), int(gg * 255), int(bb * 255))


def _render_wx(kind: str, n: int) -> QImage:
    """A weather icon (the same drawing Hand Space uses) as an image the QML can show."""
    from ui.hs_widgets import _wx_icon
    img = QImage(n, n, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    try:
        _wx_icon(p, kind, QRectF(n * 0.06, n * 0.06, n * 0.88, n * 0.88), 0.0)
    finally:
        p.end()
    return img


def _icon_dir() -> Path:
    import tempfile
    d = Path(tempfile.gettempdir()) / "jarvis_island"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _file_url(p: Path) -> str:
    return QUrl.fromLocalFile(str(p)).toString()


def _wx_url(kind: str, n: int) -> str:
    """A weather icon as a small PNG in the temp folder (drawn once per kind and size); the QML shows it by file URL."""
    p = _icon_dir() / f"wx_{kind}_{n}.png"
    try:
        if not p.exists():
            _render_wx(kind, n * 2).save(str(p), "PNG")
        return _file_url(p)
    except Exception:                                                  # noqa: BLE001 - no icon is better than no island
        return ""


# ═════════════════════════════════════════════════════════════════════════════════
#  the backend the QML talks to
# ═════════════════════════════════════════════════════════════════════════════════
class IslandBackend(QObject):
    mediaChanged = pyqtSignal()
    weatherChanged = pyqtSignal()
    noticeChanged = pyqtSignal()
    accentChanged = pyqtSignal()
    todayChanged = pyqtSignal()
    systemChanged = pyqtSignal()
    calendarChanged = pyqtSignal()
    trackChanged = pyqtSignal()           # a new song started: the QML opens the card for a moment
    togglePin = pyqtSignal()              # menu → open / close
    hideRequested = pyqtSignal()          # menu → hide the island for good

    def __init__(self):
        super().__init__()
        cfg = read_cfg()
        self.view = None                                               # set by QmlIsland
        self._dx = int(cfg.get("dynamic_island_dx", 0) or 0)           # offset from the screen's horizontal centre
        self._dy = int(cfg.get("dynamic_island_dy", DEFAULT_TOP) or DEFAULT_TOP)
        self._use_battery = bool(cfg.get("dynamic_island_battery", True))
        self._use_clip = bool(cfg.get("dynamic_island_clipboard", True))
        self._media: dict = {"music": False, "playing": False, "title": "", "artist": "", "album": "", "pos": 0.0, "end": 0.0, "stamp": 0.0, "art": ""}
        self._weather: dict = {}
        self._notice: dict = {"active": False, "icon": "", "text": ""}
        self._notice_id = 0
        self._today: dict = {}
        self._calendar: list = []
        self._system: dict = {}
        self._page = ""
        self._t_sys = 0.0
        self._accent = QColor(C.PRI)
        self._accent_default = QColor(C.PRI)
        self.art_image: QImage | None = None
        self._art_rev = 0
        self._art_url = ""
        self._art_key = None
        self._last_title = ""
        self._expanded = False
        self._min = -1
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="island")
        self._jobs: dict[str, Future] = {}
        self._t_np = 0.0
        self._t_wx = 0.0
        self._t_bat = 0.0
        self._t_top = 0.0
        self._bat: tuple | None = None                                 # (plugged, low_notified)
        self._clip_t = 0.0
        self._wx_raw: dict | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(400)
        self._timer.timeout.connect(self._tick)
        self._date_tick(time.time())

    # ── properties (read by the QML) ─────────────────────────────────────────────
    @pyqtProperty("QVariantMap", notify=mediaChanged)
    def media(self):
        return self._media

    @pyqtProperty("QVariantMap", notify=weatherChanged)
    def weather(self):
        return self._weather

    @pyqtProperty("QVariantMap", notify=noticeChanged)
    def notice(self):
        return self._notice

    @pyqtProperty("QVariantMap", notify=todayChanged)
    def today(self):
        return self._today

    @pyqtProperty("QVariantMap", notify=systemChanged)
    def system(self):
        return self._system

    @pyqtProperty("QVariantList", notify=calendarChanged)
    def calendar(self):
        return self._calendar

    @pyqtProperty(QColor, notify=accentChanged)
    def accent(self):
        return self._accent

    # ── lifecycle ────────────────────────────────────────────────────────────────
    def start(self):
        self._timer.start()
        self._pool.submit(self._read_system)                            # warm up the metrics thread (its first CPU reading is 0)
        if self._use_clip:
            try:
                QGuiApplication.clipboard().dataChanged.connect(self._on_clip)
            except Exception:                                          # noqa: BLE001
                pass
        self._tick()

    def stop(self):
        self._timer.stop()
        try:
            QGuiApplication.clipboard().dataChanged.disconnect(self._on_clip)
        except Exception:                                              # noqa: BLE001
            pass
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ── slots (called by the QML) ────────────────────────────────────────────────
    @pyqtSlot(bool)
    def setExpanded(self, on: bool):
        self._expanded = bool(on)
        if on:
            self._t_np = 0.0                                            # fresh progress as it opens

    @pyqtSlot(str)
    def setPage(self, name: str):
        """The QML tells which page is showing; the hardware numbers are only polled while that page is open."""
        self._page = str(name)
        if self._page == "system":
            self._t_sys = 0.0

    @pyqtSlot(result=QPointF)
    def cursor(self):
        p = QCursor.pos()
        return QPointF(p.x(), p.y())

    @pyqtSlot(result=QPointF)
    def offset(self):
        return QPointF(self._dx, self._dy)

    @pyqtSlot(float, float)
    def moveTo(self, dx: float, dy: float):
        self._dx, self._dy = int(dx), max(0, int(dy))
        self.place()

    @pyqtSlot()
    def saveOffset(self):
        try:
            patch_cfg(dynamic_island_dx=self._dx, dynamic_island_dy=self._dy)
        except Exception:                                              # noqa: BLE001
            pass

    @pyqtSlot(float, float, float, float)
    def setInputRect(self, x: float, y: float, w: float, h: float):
        """Only the pill takes the mouse; the rest of the transparent canvas lets clicks through to the desktop."""
        v = self.view
        if v is None:
            return
        x0, y0 = max(0, int(x)), max(0, int(y))
        v.setMask(QRegion(QRect(x0, y0, min(WIN_W - x0, int(w)), min(WIN_H - y0, int(h)))))

    @pyqtSlot(str)
    def control(self, action: str):
        now = time.time()
        if action == "toggle":                                         # show the change at once; the next poll corrects it
            m = dict(self._media)
            m.update(pos=self._pos(now), stamp=now * 1000.0, playing=not m.get("playing"))
            self._media = m
            self.mediaChanged.emit()
        self._t_np = 0.0
        self._pool.submit(media_session.control, action, 0.0)

    @pyqtSlot(float)
    def seek(self, secs: float):
        now = time.time()
        m = dict(self._media)
        m.update(pos=float(secs), stamp=now * 1000.0)
        self._media = m
        self.mediaChanged.emit()
        self._t_np = 0.0
        self._pool.submit(media_session.control, "seek", float(secs))

    @pyqtSlot(bool)
    def showMenu(self, pinned: bool):
        QTimer.singleShot(0, lambda: self._menu(pinned))               # not inside the mouse handler

    # ── placement ────────────────────────────────────────────────────────────────
    def place(self):
        v = self.view
        if v is None:
            return
        scr = QGuiApplication.primaryScreen()
        g = scr.geometry() if scr is not None else None
        cx = (g.x() + g.width() / 2) if g is not None else 960
        top = (g.y() if g is not None else 0) + self._dy
        v.setPosition(int(cx + self._dx - WIN_W / 2), int(top - M))

    def lift(self):
        if self.view is not None and self.view.isVisible():          # a hidden island (lock screen) must stay hidden
            force_topmost(self.view, click_through=False)

    def _menu(self, pinned: bool):
        from PyQt6.QtWidgets import QMenu
        m = QMenu()
        a_pin = m.addAction("Close (or pull up)" if pinned else "Open (or pull down)")
        a_reset = m.addAction("Reset position")
        a_hide = m.addAction("Hide Dynamic Island")
        act = m.exec(QCursor.pos())
        if act is a_pin:
            self.togglePin.emit()
        elif act is a_reset:
            self._dx, self._dy = 0, DEFAULT_TOP
            self.saveOffset()
            self.place()
        elif act is a_hide:
            self.hideRequested.emit()

    # ── data ─────────────────────────────────────────────────────────────────────
    def _tick(self):
        now = time.time()
        self._harvest(now)
        busy_np = self._media.get("music") or self._expanded
        if "np" not in self._jobs and now - self._t_np >= (1.0 if busy_np else 2.5):
            self._t_np = now
            self._jobs["np"] = self._pool.submit(media_session.snapshot)
        if "wx" not in self._jobs and now - self._t_wx >= (600.0 if self._weather else 60.0):
            self._t_wx = now
            self._jobs["wx"] = self._pool.submit(space_feeds.weather_json)
        if self._expanded and self._page == "system" and "sys" not in self._jobs and now - self._t_sys >= 1.0:
            self._t_sys = now
            self._jobs["sys"] = self._pool.submit(self._read_system)
        if self._use_battery and "bat" not in self._jobs and now - self._t_bat >= 5.0:
            self._t_bat = now
            self._jobs["bat"] = self._pool.submit(self._read_battery)
        if int(now // 60) != self._min:
            self._date_tick(now)
        if now - self._t_top > 1.5:                                     # stay above Hand Space and games
            self._t_top = now
            self.lift()

    def _harvest(self, now: float):
        for k, fut in list(self._jobs.items()):
            if not fut.done():
                continue
            del self._jobs[k]
            try:
                res = fut.result()
            except Exception:                                          # noqa: BLE001
                continue
            if k == "wx":
                data = space_feeds.parse_json(res)
                if data is not None and not data.get("error"):
                    self._set_weather(data)
            elif k == "np" and isinstance(res, dict):
                self._take_media(res, now)
            elif k == "bat" and res:
                self._take_battery(res)
            elif k == "sys" and isinstance(res, dict):
                if res != self._system:
                    self._system = res
                    self.systemChanged.emit()

    @staticmethod
    def _read_battery():
        try:
            import psutil
            b = psutil.sensors_battery()
            return (int(b.percent), bool(b.power_plugged)) if b is not None else None
        except Exception:                                              # noqa: BLE001
            return None

    @staticmethod
    def _read_system() -> dict:
        """CPU / RAM / GPU / temperature / network from ui.metrics, plus the battery. Missing sensors stay -1."""
        out = {"cpu": 0.0, "mem": 0.0, "gpu": -1.0, "tmp": -1.0, "net": 0.0, "batt": -1, "plugged": False}
        try:
            from ui.metrics import _metrics
            out.update(_metrics.snapshot())
        except Exception:                                              # noqa: BLE001
            pass
        b = IslandBackend._read_battery()
        if b:
            out["batt"], out["plugged"] = b
        return out

    def _take_battery(self, res):
        pct, plugged = res
        if self._bat is None:                                          # the first reading is the baseline, not news
            self._bat = (plugged, False)
            return
        was_plugged, low_told = self._bat
        if plugged != was_plugged:
            self.show_notice("⚡" if plugged else "\U0001F50B", f"{'Charging' if plugged else 'Unplugged'} · {pct}%", 3200)
            low_told = False
        elif not plugged and pct <= 20 and not low_told:
            self.show_notice("⚠", f"Battery low · {pct}%", 4500)
            low_told = True
        elif plugged or pct > 25:
            low_told = False
        self._bat = (plugged, low_told)

    def _on_clip(self):
        now = time.time()
        if now - self._clip_t < 1.5:
            return
        self._clip_t = now
        try:
            text = QGuiApplication.clipboard().text()
        except Exception:                                              # noqa: BLE001
            return
        if text:
            n = len(text)
            self.show_notice("⧉", f"Copied · {n} char{'s' if n != 1 else ''}", 1800)   # never the text itself

    def show_notice(self, icon: str, text: str, ms: int = 2500):
        """A short event bubble: the pill widens for a moment, then goes back."""
        self._notice_id += 1
        nid = self._notice_id
        self._notice = {"active": True, "icon": icon, "text": text}
        self.noticeChanged.emit()
        QTimer.singleShot(ms, lambda: self._end_notice(nid))

    def _end_notice(self, nid: int):
        if nid == self._notice_id:
            self._notice = {"active": False, "icon": "", "text": ""}
            self.noticeChanged.emit()

    def _date_tick(self, now: float):
        self._min = int(now // 60)
        d = datetime.fromtimestamp(now)
        today = {"month": d.strftime("%B"), "year": str(d.year), "day": str(d.day), "weekday": d.strftime("%A"), "date": d.strftime("%A, %d %B")}
        if today != self._today:
            self._today = today
            self.todayChanged.emit()
        cells = []
        for week in _cal.Calendar(0).monthdatescalendar(d.year, d.month):
            for i, day in enumerate(week):
                kind = "today" if day == d.date() else "other" if day.month != d.month else "weekend" if i >= 5 else "day"
                cells.append({"d": day.day, "k": kind})
        if cells != self._calendar:
            self._calendar = cells
            self.calendarChanged.emit()

    def _set_weather(self, wx: dict):
        words, icon = space_feeds.wx_kind(wx.get("code"), bool(wx.get("day", True)))
        days = []
        for dd in (wx.get("days") or [])[1:5]:
            try:
                name = datetime.strptime(dd["d"], "%Y-%m-%d").strftime("%a")
            except (ValueError, KeyError):
                name = "—"
            days.append({"name": name, "icon": _wx_url(space_feeds.wx_kind(dd.get("code"), True)[1], 24),
                         "hi": dd.get("hi", ""), "lo": dd.get("lo", "")})
        today = (wx.get("days") or [{}])[0]
        out = {"ok": True, "t": wx.get("t", ""), "unit": wx.get("unit", "°C"), "words": words, "city": wx.get("city", ""),
               "icon": _wx_url(icon, 58), "days": days,
               "hasHiLo": today.get("hi") is not None, "hi": today.get("hi", ""), "lo": today.get("lo", "")}
        if out != self._weather:
            self._weather = out
            self.weatherChanged.emit()

    def _pos(self, now: float) -> float:
        m = self._media
        pos = float(m.get("pos", 0.0))
        if m.get("playing"):
            pos += now - float(m.get("stamp", now * 1000.0)) / 1000.0
        end = float(m.get("end", 0.0))
        return min(pos, end) if end > 0 else pos

    def _take_media(self, d: dict, now: float):
        thumb = d.pop("thumb", None)
        state = d.get("state")
        music = state in ("playing", "paused")
        playing = state == "playing"
        title = d.get("title", "") or ""
        key = (d.get("title"), d.get("artist"), len(thumb) if thumb else 0)
        if key != self._art_key:                                        # a new cover: decode it once, tint the card with it
            self._art_key = key
            self.art_image = None
            accent = QColor(self._accent_default)
            if thumb:
                img = QImage.fromData(thumb)
                if not img.isNull():
                    s = min(img.width(), img.height())
                    img = img.copy((img.width() - s) // 2, (img.height() - s) // 2, s, s)
                    accent = _accent_of(img)
                    self.art_image = img.scaled(200, 200, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
            self._art_rev += 1
            self._art_url = ""
            if self.art_image is not None:
                try:
                    icon_dir = _icon_dir()
                    for old_f in icon_dir.glob("art_*.png"):
                        try:
                            old_f.unlink()
                        except OSError:
                            pass
                    p = icon_dir / f"art_{self._art_rev}.png"
                    if self.art_image.save(str(p), "PNG"):
                        self._art_url = _file_url(p)
                except Exception:                                      # noqa: BLE001
                    pass
            if accent.rgb() != self._accent.rgb():
                self._accent = accent
                self.accentChanged.emit()
        new = {"music": music, "playing": playing, "title": title, "artist": d.get("artist", "") or "", "album": d.get("album", "") or "",
               "pos": float(d.get("pos", 0.0)), "end": float(d.get("end", 0.0)), "stamp": now * 1000.0,
               "art": self._art_url}
        old = self._media
        same = all(old.get(k) == new[k] for k in ("music", "playing", "title", "artist", "album", "end", "art"))
        if same and abs(self._pos(now) - new["pos"]) < 1.2:             # nothing the QML can't already interpolate: stay quiet
            return
        self._media = new
        self.mediaChanged.emit()
        new_song = playing and title and title != self._last_title
        self._last_title = title if playing else ""
        if new_song:
            self.trackChanged.emit()


# ═════════════════════════════════════════════════════════════════════════════════
#  the window
# ═════════════════════════════════════════════════════════════════════════════════
class QmlIsland:
    """Same small API the main window uses: start() · stop() · lift() · isVisible()."""

    def __init__(self):
        self._fallback = None
        self._held = False                                             # hide() was called: stay hidden until show()
        self.api = _configure_graphics()
        from PyQt6.QtQuick import QQuickView
        self.backend = IslandBackend()
        view = QQuickView()
        self.view = view
        self.backend.view = view
        view.setFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
                      | Qt.WindowType.WindowDoesNotAcceptFocus)
        view.setColor(QColor(0, 0, 0, 0))
        fmt = QSurfaceFormat()
        fmt.setAlphaBufferSize(8)
        view.setFormat(fmt)
        view.setResizeMode(QQuickView.ResizeMode.SizeViewToRootObject)
        view.rootContext().setContextProperty("backend", self.backend)
        view.sceneGraphError.connect(self._gfx_error)
        view.setSource(QUrl.fromLocalFile(str(QML_DIR / "DynamicIsland.qml")))
        if view.status() == QQuickView.Status.Error or view.rootObject() is None:
            raise RuntimeError("; ".join(e.toString() for e in view.errors()) or "DynamicIsland.qml did not load")
        self.backend.hideRequested.connect(self._hide_forever)
        self.backend.place()

    # ── lifecycle ────────────────────────────────────────────────────────────────
    def start(self):
        if self._fallback is not None:
            return self._fallback.start()
        self.view.show()
        self.lift()
        self.backend.start()

    def stop(self):
        if self._fallback is not None:
            return self._fallback.stop()
        self.backend.stop()
        self.view.hide()
        self.view.close()

    def lift(self):
        if self._fallback is not None:
            return self._fallback.lift()
        if not self._held:
            self.backend.lift()

    def isVisible(self) -> bool:
        return self._fallback.isVisible() if self._fallback is not None else self.view.isVisible()

    def hide(self):
        """Hidden for now (the lock screen does this); show() brings it back."""
        if self._fallback is not None:
            return self._fallback.hide()
        self._held = True
        self.view.hide()

    def show(self):
        if self._fallback is not None:
            return self._fallback.show()
        self._held = False
        self.view.show()
        self.backend.place()
        self.backend.lift()

    # ── trouble ──────────────────────────────────────────────────────────────────
    def _gfx_error(self, *_):
        """The GPU scene could not run: swap in the old widget version, on the next turn of the event loop."""
        if self._fallback is None:
            QTimer.singleShot(0, self._to_legacy)

    def _to_legacy(self):
        if self._fallback is not None:
            return
        print("[JARVIS] Dynamic Island: Qt Quick scene failed - using the widget version.")
        try:
            self.backend.stop()
            self.view.hide()
        except Exception:                                              # noqa: BLE001
            pass
        from ui.dynamic_island_legacy import DynamicIsland as Legacy
        self._fallback = Legacy()
        self._fallback.start()

    def _hide_forever(self):
        try:
            patch_cfg(dynamic_island=False)
        except Exception:                                              # noqa: BLE001
            pass
        self.stop()


def DynamicIsland():
    """The island the main window starts: Qt Quick on the GPU, or the old widget version if that is not possible."""
    cfg = read_cfg()
    if os.environ.get("JARVIS_ISLAND_LEGACY") or str(cfg.get("dynamic_island_renderer", "")).lower() == "widgets":
        from ui.dynamic_island_legacy import DynamicIsland as Legacy
        return Legacy()
    try:
        return QmlIsland()
    except Exception as e:                                             # noqa: BLE001 - no QtQuick / a QML error / no driver
        print(f"[JARVIS] Dynamic Island: Qt Quick unavailable ({e}) - using the widget version.")
        from ui.dynamic_island_legacy import DynamicIsland as Legacy
        return Legacy()
#by ASDcuber

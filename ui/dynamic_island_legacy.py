"""Dynamic Island — a floating glass pill with the time, the weather and a music player, above everything (Hand Space too).

Resting: a small dark pill — time | weather and the orange dot; round cover, equaliser and the dot while music plays.
Hover it, click it, or change the song: it opens into the player card (cover, title / artist / album, progress, ⏮ ⏯ ⏭), tinted
with the colour of the cover. The orange dot on the right flips through the pages: player (while music is loaded) · calendar · clock + weather. Pull it down with the mouse to open it, up to shut it; Shift+drag moves it.
It never takes focus and shrinks back by itself.

Cheap by design: the animation is time-based (same speed at any frame rate), a resting pill repaints once a minute, an equaliser
pill ~20 times a second, and the timer only runs fast while the card is actually moving or playing music.

Music comes from core/media_session.py (any player on Windows through the `winrt-*` packages — they work on Python 3.14; Spotify's window title otherwise),
the weather from Open-Meteo (`hs_weather_city`, `hs_weather_units`). Turn it off: `"dynamic_island": false` or right-click → Hide.
"""
from __future__ import annotations

import colorsys
import math
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath, QPen,
                         QPixmap, QPolygonF, QRadialGradient)
from PyQt6.QtWidgets import QMenu, QWidget

from core import media_session, space_feeds
from ui.hs_widgets import _F, _disc, _equalizer, _mix, _q, _txt, _wx_icon
from ui.overlay_utils import force_topmost
from ui.theme import C, patch_cfg, read_cfg

_AL = Qt.AlignmentFlag
M = 16                                   # transparent margin around the card (room for its shadow)
CW, CH = 158.0, 34.0                     # resting pill: small and dark
CW_PLAY = 158.0                          # … the same while music plays
EW, EH_PLAY, EH_CAL, EH_INFO = 388.0, 186.0, 168.0, 126.0
DOT = "#ff9f0a"                          # the orange dot on the right: click it for the next page
DEFAULT_TOP = 34                         # distance from the top edge — a little lower than the very edge


def _fmt(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


class DynamicIsland(QWidget):

    def __init__(self):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
                         | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        cfg = read_cfg()
        self._dx = int(cfg.get("dynamic_island_dx", 0) or 0)           # offset from the screen's horizontal centre
        self._dy = int(cfg.get("dynamic_island_dy", DEFAULT_TOP) or DEFAULT_TOP)
        self.cw, self.ch = CW, CH
        self._hover = False
        self._pinned = False
        self._page = 0                                                 # index into _pages(): player · calendar · clock + weather
        self._boost = 0.0                                              # open until this time (a new song, a button press)
        self._leave = 0.0
        self._wx: dict | None = None
        self._np = media_session._blank()
        self._art: QPixmap | None = None
        self._art_key = None
        self._accent = QColor(C.PRI)
        self._accent_to = QColor(C.PRI)
        self._last_title = ""
        self._btns: dict[str, QRectF] = {}
        self._press = None
        self._dragged = False
        self._mode = None                                              # None · "pull" (open / close by dragging) · "move" (reposition)
        self._pulling = False
        self._pull_t = 0.0                                             # 0 resting … 1 fully pulled open
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="island")
        self._jobs: dict[str, Future] = {}
        self._t_np = 0.0
        self._t_wx = 0.0
        self._t_top = 0.0
        self._t_last = time.time()                                       # for the time-based animation
        self._min = -1                                                   # the minute last painted (a resting pill needs no more)
        self._sig = None                                                 # what the pill showed last — repaint only when it changes
        self._hl = QColor(255, 255, 255)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._step)
        self._place()

    # ── lifecycle ────────────────────────────────────────────────────────────────
    def start(self):
        self.show()
        force_topmost(self, click_through=False)
        self._timer.start(33)

    def stop(self):
        self._timer.stop()
        self.hide()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def lift(self):
        """Above everything again — also above Hand Space, which asserts its own topmost state."""
        force_topmost(self, click_through=False)

    # ── data ─────────────────────────────────────────────────────────────────────
    def _poll(self, now: float):
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
                    self._wx = data
            elif k == "np" and isinstance(res, dict):
                self._take_media(res, now)
        if "np" not in self._jobs and now - self._t_np >= (1.0 if (self._music or self._expanded(now)) else 2.5):
            self._t_np = now
            self._jobs["np"] = self._pool.submit(media_session.snapshot)
        if "wx" not in self._jobs and now - self._t_wx >= (600.0 if self._wx else 60.0):
            self._t_wx = now
            self._jobs["wx"] = self._pool.submit(space_feeds.weather_json)

    def _take_media(self, d: dict, now: float):
        thumb = d.pop("thumb", None)
        d["t"] = now
        self._np = d
        title = d.get("title", "") if d.get("state") == "playing" else ""
        if title and title != self._last_title:                        # a new song: open for a few seconds
            self._boost = now + 4.0
            self._page = 0
        self._last_title = title
        key = (d.get("title"), d.get("artist"), len(thumb) if thumb else 0)
        if key != self._art_key:
            self._art_key = key
            self._art = None
            self._accent_to = QColor(C.PRI)
            if thumb:
                img = QImage.fromData(thumb)
                if not img.isNull():
                    s = min(img.width(), img.height())
                    img = img.copy((img.width() - s) // 2, (img.height() - s) // 2, s, s)
                    self._accent_to = self._accent_of(img)
                    self._art = QPixmap.fromImage(img.scaled(200, 200, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation))

    @staticmethod
    def _accent_of(img: QImage) -> QColor:
        """The cover's strongest colour, lifted so it reads on black."""
        small = img.scaled(16, 16, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        px = []
        for y in range(16):
            for x in range(16):
                c = QColor(small.pixel(x, y))
                h, s, v = c.hsvHueF(), c.hsvSaturationF(), c.valueF()
                px.append((s * v, c.red(), c.green(), c.blue()))
        px.sort(reverse=True)
        top = px[:40]
        r, g, b = (sum(p[i] for p in top) / len(top) for i in (1, 2, 3))
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if s > 0.15:
            s = max(s, 0.6)
        v = max(v, 0.88)
        rr, gg, bb = colorsys.hsv_to_rgb(h, s, v)
        return QColor(int(rr * 255), int(gg * 255), int(bb * 255))

    def _act(self, action: str, value: float = 0.0):
        if action == "toggle":                                         # show the change at once; the next poll corrects it
            self._np = dict(self._np, pos=self._pos(time.time()), t=time.time(),
                            state="paused" if self._np.get("state") == "playing" else "playing")
        elif action == "seek":
            self._np = dict(self._np, pos=value, t=time.time())
        self._t_np = time.time() - 1.0
        self._pool.submit(media_session.control, action, value)

    def _pos(self, now: float) -> float:
        n = self._np
        pos = float(n.get("pos", 0.0))
        if n.get("state") == "playing":
            pos += now - float(n.get("t", now))
        end = float(n.get("end", 0.0))
        return min(pos, end) if end > 0 else pos

    # ── geometry ─────────────────────────────────────────────────────────────────
    def _pages(self) -> list:
        """The pages the dot cycles through: the player (only while something is loaded), the calendar, the clock + weather."""
        return (["player"] if self._music else []) + ["calendar", "info"]

    def _cur(self) -> str:
        pg = self._pages()
        return pg[self._page % len(pg)]

    def _eh(self) -> float:
        return {"player": EH_PLAY, "calendar": EH_CAL}.get(self._cur(), EH_INFO)

    @property
    def _music(self) -> bool:
        return self._np.get("state") in ("playing", "paused")

    def _expanded(self, now: float) -> bool:
        return self._pinned or self._hover or now < self._boost or now - self._leave < 0.45

    def _targets(self, now: float):
        if self._pulling:                                              # the card follows the mouse while it is pulled down
            t = self._pull_t
            eh = self._eh()
            rw = CW_PLAY if self._music else CW
            return rw + (EW - rw) * t, CH + (eh - CH) * t
        if self._expanded(now):
            return EW, self._eh()
        return (CW_PLAY if self._music else CW), CH

    def _place(self):
        scr = QGuiApplication.primaryScreen()
        g = scr.geometry() if scr is not None else None
        cx = (g.x() + g.width() / 2) if g is not None else 960
        top = (g.y() if g is not None else 0) + self._dy
        self.setGeometry(int(cx + self._dx - self.cw / 2 - M), int(top - M), int(math.ceil(self.cw + 2 * M)), int(math.ceil(self.ch + 2 * M)))

    def _step(self):
        now = time.time()
        dt = max(0.001, min(0.1, now - self._t_last))
        self._t_last = now
        self._poll(now)
        tw, th = self._targets(now)
        k = 1.0 - math.exp(-dt * 16.0)                                 # critically-damped glide, the same at 20 or 120 fps
        moving = abs(tw - self.cw) > 0.35 or abs(th - self.ch) > 0.35
        if moving:
            self.cw += (tw - self.cw) * k
            self.ch += (th - self.ch) * k
            if abs(tw - self.cw) <= 0.35 and abs(th - self.ch) <= 0.35:
                self.cw, self.ch = tw, th
            self._place()
        a, b = self._accent, self._accent_to
        tinting = a.rgb() != b.rgb()
        if tinting:                                                    # the tint glides to the new cover's colour
            self._accent = _mix(a, b, 1.0 - math.exp(-dt * 6.0))
            if abs(self._accent.red() - b.red()) + abs(self._accent.green() - b.green()) + abs(self._accent.blue() - b.blue()) < 6:
                self._accent = QColor(b)
        if now - self._t_top > 1.5:                                     # stay above Hand Space and games
            self._t_top = now
            self.lift()
        playing = self._np.get("state") == "playing"
        expanded = self._expanded(now)
        if moving:
            want = 16
        elif expanded:
            want = 33 if playing else 250                              # open + playing: progress and equaliser; open + paused: just the clock
        elif playing:
            want = 50                                                  # equaliser only
        else:
            want = 1000                                                # resting: the time and nothing else
        if self._timer.interval() != want:
            self._timer.setInterval(want)
        # repaint only when something visible can have changed
        minute = int(now // 60)
        sig = (round(self.cw, 1), round(self.ch, 1), self._np.get("state"), self._np.get("title"), self._page, self._art_key, minute,
               self._wx is not None and self._wx.get("t"), self._accent.rgb())
        if moving or tinting or playing or expanded or sig != self._sig:
            self._sig = sig
            self.update()

    # ── input ────────────────────────────────────────────────────────────────────
    def enterEvent(self, e):
        self._hover = True
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self._leave = time.time()
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.RightButton:
            m = QMenu(self)
            m.addAction("Close (or pull up)" if self._pinned else "Open (or pull down)").triggered.connect(lambda: setattr(self, "_pinned", not self._pinned))
            m.addAction("Reset position").triggered.connect(self._reset_pos)
            m.addAction("Hide Dynamic Island").triggered.connect(self._hide_forever)
            m.exec(e.globalPosition().toPoint())
            return
        self._press = (e.globalPosition(), self._dx, self._dy, e.position() - QPointF(M, M))
        self._dragged = False
        self._mode = None

    def mouseMoveEvent(self, e):
        if self._press is None:
            return
        g0, dx0, dy0, _ = self._press
        d = e.globalPosition() - g0
        if self._mode is None and abs(d.x()) + abs(d.y()) > 6:
            shift = bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            sideways = abs(d.x()) > abs(d.y()) * 1.6
            self._mode = "move" if (shift or sideways) else "pull"       # sideways or Shift: move it · up / down: pull it open / shut
            self._dragged = True
            if self._mode == "pull" and not self._pinned:
                self._pulling = True
        if self._mode == "move":
            self._dx, self._dy = int(dx0 + d.x()), max(0, int(dy0 + d.y()))
            self._place()
        elif self._mode == "pull":
            if self._pinned:                                             # open: pull it up to shut it
                if d.y() < -28:
                    self._pinned, self._boost = False, 0.0
                    self._leave = 0.0
                    self._hover = False
                    self._mode = "done"
            else:                                                        # shut: the further down, the more open (with a little resistance)
                self._pull_t = max(0.0, min(1.0, (d.y() / 120.0) ** 0.9)) if d.y() > 0 else 0.0
                self._pulling = True

    def mouseReleaseEvent(self, e):
        press, self._press = self._press, None
        mode, self._mode = self._mode, None
        if press is None:
            return
        if mode == "pull":
            if self._pulling:
                self._pulling = False
                self._pinned = self._pull_t > 0.4                        # pulled far enough: it stays open
                self._leave = time.time()
            self._pull_t = 0.0
            return
        if mode == "done":
            return
        if mode == "move":
            try:
                patch_cfg(dynamic_island_dx=self._dx, dynamic_island_dy=self._dy)
            except Exception:                                          # noqa: BLE001
                pass
            return
        pos = press[3]
        for name, r in self._btns.items():
            if r.contains(pos):
                self._click(name, pos)
                return
        self._pinned = not self._pinned
        if not self._pinned:
            self._leave = time.time()

    def _click(self, name: str, pos):
        self._boost = max(self._boost, time.time() + 3.0)
        if name == "page":
            self._page = (self._page + 1) % len(self._pages())
        elif name == "seek":
            r, end = self._btns["seek"], float(self._np.get("end", 0.0))
            if end > 0:
                self._act("seek", max(0.0, min(1.0, (pos.x() - r.left()) / r.width())) * end)
        else:
            self._act({"prev": "previous", "play": "toggle", "next": "next"}[name])

    def _reset_pos(self):
        self._dx, self._dy = 0, DEFAULT_TOP
        try:
            patch_cfg(dynamic_island_dx=0, dynamic_island_dy=DEFAULT_TOP)
        except Exception:                                              # noqa: BLE001
            pass
        self._place()

    def _hide_forever(self):
        try:
            patch_cfg(dynamic_island=False)
        except Exception:                                              # noqa: BLE001
            pass
        self.stop()

    # ── painting ─────────────────────────────────────────────────────────────────
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        now = time.time()
        w, h = self.cw, self.ch
        playing = self._np.get("state") == "playing"
        prog = max(0.0, min(1.0, (w - CW_PLAY) / (EW - CW_PLAY)))      # 0 = resting pill, 1 = fully open
        rad = 19.0 + 11.0 * prog if h < 60 else 30.0
        rad = min(rad, h / 2)
        acc = QColor(self._accent) if self._music else QColor(C.PRI)
        # soft shadow
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(4):
            p.setBrush(QColor(0, 0, 0, 30 - i * 7))
            p.drawRoundedRect(QRectF(M - i * 3, M - i * 3 + 5, w + i * 6, h + i * 6), rad + i * 3, rad + i * 3)
        p.translate(M, M)
        body = QPainterPath()
        body.addRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), rad, rad)
        tint = acc if self._music else QColor("#2a2f55")
        g = QLinearGradient(0, 0, w, h)
        g.setColorAt(0, _mix("#05050a", tint, 0.30 if prog > 0.5 else 0.12))
        g.setColorAt(1, _mix("#000000", tint, 0.10 if prog > 0.5 else 0.03))
        p.setPen(QPen(_q(_mix("#ffffff", tint, 0.5), 52), 1.0))
        p.setBrush(QBrush(g))
        p.drawPath(body)
        p.setClipPath(body)
        if self._music:                                                # a soft glow of the cover's colour from the lower left
            gl = QRadialGradient(QPointF(w * 0.22, h * 1.05), max(w, h) * 0.75)
            gl.setColorAt(0, _q(acc, 70 if prog > 0.5 else 38))
            gl.setColorAt(1, _q(acc, 0))
            p.fillRect(QRectF(0, 0, w, h), QBrush(gl))
        sh = QLinearGradient(0, 0, 0, min(h, 26.0))                     # glass sheen along the top edge
        sh.setColorAt(0, QColor(255, 255, 255, 30))
        sh.setColorAt(1, QColor(255, 255, 255, 0))
        p.fillRect(QRectF(0, 0, w, min(h, 26.0)), QBrush(sh))
        d = datetime.now()
        self._btns = {}
        if prog < 0.98:
            p.setOpacity(1.0 - min(1.0, prog * 2.4))
            self._compact(p, w, h, d, now, playing, acc)
        if prog > 0.3:
            p.setOpacity(((prog - 0.3) / 0.7) ** 2)
            ox = (w - EW) / 2
            cur = self._cur()
            if cur == "player":
                self._player(p, ox, now, playing, acc, prog > 0.9)
            elif cur == "calendar":
                self._calendar(p, ox, h, d, prog > 0.9)
            else:
                self._info(p, ox, h, d, now, prog > 0.9, acc)
            p.setPen(Qt.PenStyle.NoPen)                                # the orange dot: next page
            p.setBrush(QColor(DOT))
            p.drawEllipse(QPointF(ox + EW - 12, h / 2), 3.4, 3.4)
            if prog > 0.9:
                self._btns["page"] = QRectF(ox + EW - 32, h / 2 - 22, 32, 44)
        p.end()

    def _cover(self, p: QPainter, r: QRectF, acc: QColor, now: float, playing: bool, rad: float):
        path = QPainterPath()
        path.addRoundedRect(r, rad, rad)
        p.save()
        p.setClipPath(path, Qt.ClipOperation.IntersectClip)
        if self._art is not None:
            p.drawPixmap(r, self._art, QRectF(self._art.rect()))
        else:
            g = QLinearGradient(r.topLeft(), r.bottomRight())
            g.setColorAt(0, _mix(acc, "#ffffff", 0.1))
            g.setColorAt(1, _mix(acc, "#000000", 0.6))
            p.fillRect(r, QBrush(g))
            _disc(p, r.center(), r.width() * 0.34, now, playing)
        p.restore()
        p.setPen(QPen(QColor(255, 255, 255, 40), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(r, rad, rad)

    def _compact(self, p: QPainter, w: float, h: float, d: datetime, now: float, playing: bool, acc: QColor):
        cy = h / 2
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(DOT))
        p.drawEllipse(QPointF(w - 14, cy), 3.2, 3.2)
        if self._music:                                                # round cover on the left, equaliser on the right
            self._cover(p, QRectF(8, cy - 12, 24, 24), acc, now, playing, 12)
            _equalizer(p, QRectF(w - 56, cy - 7, 30, 14), acc, playing, now, 6)
            end = float(self._np.get("end", 0.0))
            if end > 0:                                                # a hairline of progress along the bottom
                f = max(0.0, min(1.0, self._pos(now) / end))
                p.setBrush(QColor(255, 255, 255, 22))
                p.drawRect(QRectF(16, h - 2.5, w - 32, 1.5))
                p.setBrush(_q(acc, 210))
                p.drawRect(QRectF(16, h - 2.5, (w - 32) * f, 1.5))
            return
        hm = d.strftime("%H:%M")
        if self._wx:                                                   # time | weather icon + temperature
            _txt(p, QRectF(13, 0, 48, h), hm, 9, "#ffffff", QFont.Weight.DemiBold)
            p.setPen(QPen(QColor(255, 255, 255, 40), 1))
            p.drawLine(QPointF(63, cy - 7), QPointF(63, cy + 7))
            _wx_icon(p, space_feeds.wx_kind(self._wx.get("code"), bool(self._wx.get("day", True)))[1], QRectF(71, cy - 9, 18, 18), now)
            _txt(p, QRectF(93, 0, 36, h), f'{self._wx.get("t", "")}°', 9, C.TEXT_MED, QFont.Weight.DemiBold)
        else:
            _txt(p, QRectF(0, 0, w - 20, h), hm, 11, "#ffffff", QFont.Weight.Bold, _AL.AlignHCenter)

    def _marquee(self, p: QPainter, r: QRectF, text: str, size: int, color, weight, now: float, live: bool):
        """A title that is too long for its box scrolls slowly (with a pause at both ends); otherwise it is drawn still."""
        f = _F(size, weight)
        fm = QFontMetrics(f)
        tw = fm.horizontalAdvance(text)
        if tw <= r.width() or not live:
            _txt(p, r, text, size, color, weight)
            return
        over = tw - r.width() + 24
        t = (now * 28.0) % (over * 2 + 2 * 28.0 * 1.2)                  # forth, pause, back, pause
        pause = 28.0 * 1.2
        off = 0.0 if t < pause else min(over, t - pause) if t < pause + over else over if t < 2 * pause + over else max(0.0, over - (t - 2 * pause - over))
        p.save()
        p.setClipRect(r)
        p.setFont(f)
        p.setPen(QColor(color))
        p.drawText(QRectF(r.left() - off, r.top(), tw + 8, r.height()), _AL.AlignLeft | _AL.AlignVCenter, text)
        p.restore()

    # the player page — cover, titles, progress, buttons
    def _player(self, p: QPainter, ox: float, now: float, playing: bool, acc: QColor, live: bool):
        n = self._np
        self._cover(p, QRectF(ox + 22, 22, 68, 68), acc, now, playing, 14)
        tx = ox + 106
        tw = EW - 106 - 64
        self._marquee(p, QRectF(tx, 24, tw, 22), n.get("title") or "—", 11, "#ffffff", QFont.Weight.Bold, now, live)
        _txt(p, QRectF(tx, 47, tw, 16), n.get("artist") or "", 9, _mix(C.TEXT_MED, acc, 0.25))
        _txt(p, QRectF(tx, 64, tw, 15), n.get("album") or "", 8, C.TEXT_DIM)
        _equalizer(p, QRectF(ox + EW - 72, 28, 36, 12), acc, playing, now, 8)
        end = float(n.get("end", 0.0))
        pos = self._pos(now)
        by = 152.0
        if end > 0:
            _txt(p, QRectF(ox + 18, 99, 42, 16), _fmt(pos), 7, C.TEXT_MED, None, _AL.AlignRight)
            _txt(p, QRectF(ox + EW - 60, 99, 44, 16), "-" + _fmt(end - pos), 7, C.TEXT_MED)
            bar = QRectF(ox + 68, 105, EW - 68 - 66, 4)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 34))
            p.drawRoundedRect(bar, 2, 2)
            f = max(0.0, min(1.0, pos / end))
            p.setBrush(acc)
            p.drawRoundedRect(QRectF(bar.left(), bar.top(), max(4.0, bar.width() * f), bar.height()), 2, 2)
            p.setBrush(QColor("#ffffff"))
            p.drawEllipse(QPointF(bar.left() + bar.width() * f, bar.center().y()), 4.2, 4.2)
            if live:
                self._btns["seek"] = bar.adjusted(0, -9, 0, 9)
        else:
            by = 142.0
        icon = _mix(acc, "#ffffff", 0.15)
        for name, cx, rad in (("prev", ox + EW / 2 - 66, 17), ("play", ox + EW / 2, 24), ("next", ox + EW / 2 + 66, 17)):
            r = QRectF(cx - rad, by - rad, rad * 2, rad * 2)
            if live:
                self._btns[name] = r
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_q(_mix("#000000", acc, 0.28), 235) if name == "play" else _q(_mix("#000000", acc, 0.16), 220))
            p.drawEllipse(r)
            p.setBrush(icon)
            c, s = r.center(), rad * 0.36
            if name == "play":
                if playing:
                    p.drawRoundedRect(QRectF(c.x() - s * 0.95, c.y() - s, s * 0.75, s * 2), 1.5, 1.5)
                    p.drawRoundedRect(QRectF(c.x() + s * 0.2, c.y() - s, s * 0.75, s * 2), 1.5, 1.5)
                else:
                    p.drawPolygon(QPolygonF([QPointF(c.x() - s * 0.7, c.y() - s), QPointF(c.x() - s * 0.7, c.y() + s), QPointF(c.x() + s * 1.1, c.y())]))
            else:
                sg = -1 if name == "prev" else 1
                p.drawPolygon(QPolygonF([QPointF(c.x() - sg * s * 0.8, c.y() - s * 0.9), QPointF(c.x() - sg * s * 0.8, c.y() + s * 0.9),
                                         QPointF(c.x() + sg * s * 0.5, c.y())]))
                p.drawRoundedRect(QRectF(c.x() + sg * s * 0.55 - (0 if sg > 0 else s * 0.3), c.y() - s * 0.9, s * 0.3, s * 1.8), 1, 1)

    # the calendar page: the day big on the left, the month on the right with today in red
    def _calendar(self, p: QPainter, ox: float, h: float, d: datetime, live: bool):
        import calendar as _cal
        red = QColor("#ff453a")
        _txt(p, QRectF(ox + 24, 18, 120, 14), d.strftime("%B").upper(), 8, red, QFont.Weight.Bold)
        _txt(p, QRectF(ox + 24, 32, 120, 14), str(d.year), 8, C.TEXT_DIM)
        _txt(p, QRectF(ox + 24, 50, 120, 56), str(d.day), 40, "#ffffff", QFont.Weight.Bold)
        _txt(p, QRectF(ox + 24, h - 38, 120, 16), d.strftime("%A"), 9, C.TEXT_DIM)
        gx, gw = ox + 150, EW - 150 - 34
        cw = gw / 7
        for i, ch in enumerate("MTWTFSS"):
            _txt(p, QRectF(gx + i * cw, 16, cw, 14), ch, 7, _q(red, 190) if i >= 5 else C.TEXT_DIM, QFont.Weight.DemiBold, _AL.AlignHCenter)
        today = d.date()
        for r, week in enumerate(_cal.Calendar(0).monthdatescalendar(d.year, d.month)):
            y = 34 + r * 17
            for i, day in enumerate(week):
                cx = gx + i * cw
                if day == today:
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(red)
                    p.drawEllipse(QPointF(cx + cw / 2, y + 8), 8.5, 8.5)
                    col, wt = "#ffffff", QFont.Weight.Bold
                elif day.month != d.month:
                    col, wt = _q(C.TEXT_DIM, 110), None
                else:
                    col, wt = (_q(red, 170) if i >= 5 else "#ffffff"), None
                _txt(p, QRectF(cx, y, cw, 16), str(day.day), 7, col, wt, _AL.AlignHCenter)

    # the clock + weather page
    def _info(self, p: QPainter, ox: float, h: float, d: datetime, now: float, live: bool, acc: QColor):
        W = EW
        tf = _F(30, QFont.Weight.Bold)
        hm = d.strftime("%H:%M")
        _txt(p, QRectF(ox + 24, 12, 150, 50), hm, 30, "#ffffff", QFont.Weight.Bold)
        tw = QFontMetrics(tf).horizontalAdvance(hm)
        _txt(p, QRectF(ox + 24 + tw + 4, 26, 40, 24), d.strftime(":%S"), 12, C.ACC2, QFont.Weight.DemiBold)
        _txt(p, QRectF(ox + 24, 62, 200, 20), d.strftime("%A, %d %B"), 10, C.TEXT_MED)
        wx = self._wx
        if wx:
            words, icon = space_feeds.wx_kind(wx.get("code"), bool(wx.get("day", True)))
            unit = wx.get("unit", "°C")
            _wx_icon(p, icon, QRectF(ox + W - 170, 8, 58, 58), now)
            _txt(p, QRectF(ox + W - 106, 12, 84, 34), f'{wx.get("t", "")}{unit}', 20, "#ffffff", QFont.Weight.Bold, _AL.AlignRight)
            _txt(p, QRectF(ox + W - 136, 46, 114, 16), words, 8, C.TEXT_MED, None, _AL.AlignRight)
            day = (wx.get("days") or [{}])[0]
            hi_lo = f'  ↑{day["hi"]}° ↓{day["lo"]}°' if day.get("hi") is not None else ""
            _txt(p, QRectF(ox + W - 232, 64, 210, 18), f'{wx.get("city", "")}{hi_lo}', 8, C.TEXT_DIM, None, _AL.AlignRight)
            days = (wx.get("days") or [])[1:5]
            if h > 150 and days:                                       # room for the next days
                tw_ = (W - 44) / len(days)
                for i, dd in enumerate(days):
                    r = QRectF(ox + 22 + i * tw_, 104, tw_ - 6, 66)
                    p.setPen(QPen(QColor(255, 255, 255, 20), 1))
                    p.setBrush(QColor(255, 255, 255, 12))
                    p.drawRoundedRect(r, 14, 14)
                    try:
                        nm = datetime.strptime(dd["d"], "%Y-%m-%d").strftime("%a")
                    except ValueError:
                        nm = "—"
                    _txt(p, QRectF(r.left(), r.top() + 4, r.width(), 14), nm, 8, C.TEXT_MED, None, _AL.AlignHCenter)
                    _wx_icon(p, space_feeds.wx_kind(dd["code"], True)[1], QRectF(r.center().x() - 12, r.top() + 18, 24, 24), now)
                    _txt(p, QRectF(r.left(), r.top() + 46, r.width(), 16), f'{dd["hi"]}° / {dd["lo"]}°', 8, "#ffffff", QFont.Weight.DemiBold, _AL.AlignHCenter)
        else:
            _txt(p, QRectF(ox + W - 200, 20, 180, 30), "Weather…", 10, C.TEXT_DIM, None, _AL.AlignRight)
#by ASDcuber

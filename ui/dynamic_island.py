"""Dynamic Island — a floating glass pill with the time, the weather and a music player, above everything (Hand Space too).

Resting: a slim pill — time in the middle; cover picture and equaliser while music plays, the weather otherwise.
Hover it, click it, or change the song: it opens into the player card (cover, title / artist / album, progress, ⏮ ⏯ ⏭), tinted
with the colour of the cover. The ⋮ button flips the card to the clock + weather page. Drag it anywhere — it remembers the spot.
It never takes focus and shrinks back by itself.

Music comes from core/media_session.py (any player on Windows with `pip install winsdk`; Spotify's window title otherwise),
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
                         QPixmap, QPolygonF)
from PyQt6.QtWidgets import QMenu, QWidget

from core import media_session, space_feeds
from ui.hs_widgets import _F, _disc, _equalizer, _mix, _q, _txt, _wx_icon
from ui.overlay_utils import force_topmost
from ui.theme import C, patch_cfg, read_cfg

_AL = Qt.AlignmentFlag
M = 16                                   # transparent margin around the card (room for its shadow)
CW, CH = 196.0, 38.0                     # resting pill
CW_PLAY = 236.0                          # resting pill while music plays
EW, EH_PLAY, EH_INFO = 388.0, 186.0, 126.0
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
        self._page = 0                                                 # 0 player · 1 clock + weather
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
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="island")
        self._jobs: dict[str, Future] = {}
        self._t_np = 0.0
        self._t_wx = 0.0
        self._t_top = 0.0
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
        if "np" not in self._jobs and now - self._t_np >= 1.5:
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
    @property
    def _music(self) -> bool:
        return self._np.get("state") in ("playing", "paused")

    def _expanded(self, now: float) -> bool:
        return self._pinned or self._hover or now < self._boost or now - self._leave < 0.45

    def _targets(self, now: float):
        if self._expanded(now):
            return EW, (EH_PLAY if self._music else EH_INFO)
        return (CW_PLAY if self._music else CW), CH

    def _place(self):
        scr = QGuiApplication.primaryScreen()
        g = scr.geometry() if scr is not None else None
        cx = (g.x() + g.width() / 2) if g is not None else 960
        top = (g.y() if g is not None else 0) + self._dy
        self.setGeometry(int(cx + self._dx - self.cw / 2 - M), int(top - M), int(math.ceil(self.cw + 2 * M)), int(math.ceil(self.ch + 2 * M)))

    def _step(self):
        now = time.time()
        self._poll(now)
        tw, th = self._targets(now)
        moving = abs(tw - self.cw) > 0.4 or abs(th - self.ch) > 0.4
        if moving:
            self.cw += (tw - self.cw) * 0.26                           # springy ease-out
            self.ch += (th - self.ch) * 0.26
            if abs(tw - self.cw) <= 0.4 and abs(th - self.ch) <= 0.4:
                self.cw, self.ch = tw, th
            self._place()
        a, b = self._accent, self._accent_to
        if a.rgb() != b.rgb():                                         # the tint glides to the new cover's colour
            self._accent = _mix(a, b, 0.14)
            if abs(self._accent.red() - b.red()) + abs(self._accent.green() - b.green()) + abs(self._accent.blue() - b.blue()) < 6:
                self._accent = QColor(b)
        if now - self._t_top > 0.25:                                    # stay above Hand Space and games
            self._t_top = now
            self.lift()
        playing = self._np.get("state") == "playing"
        want = 16 if moving else (33 if playing or self._expanded(now) else 500)
        if self._timer.interval() != want:
            self._timer.setInterval(want)
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
            m.addAction("Unpin" if self._pinned else "Pin open").triggered.connect(lambda: setattr(self, "_pinned", not self._pinned))
            m.addAction("Reset position").triggered.connect(self._reset_pos)
            m.addAction("Hide Dynamic Island").triggered.connect(self._hide_forever)
            m.exec(e.globalPosition().toPoint())
            return
        self._press = (e.globalPosition(), self._dx, self._dy, e.position() - QPointF(M, M))
        self._dragged = False

    def mouseMoveEvent(self, e):
        if self._press is None:
            return
        g0, dx0, dy0, _ = self._press
        d = e.globalPosition() - g0
        if self._dragged or abs(d.x()) + abs(d.y()) > 6:
            self._dragged = True
            self._dx, self._dy = int(dx0 + d.x()), max(0, int(dy0 + d.y()))
            self._place()

    def mouseReleaseEvent(self, e):
        press, self._press = self._press, None
        if press is None:
            return
        if self._dragged:
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
            self._page ^= 1
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
        for i in range(7):
            p.setBrush(QColor(0, 0, 0, 20 - i * 2))
            p.drawRoundedRect(QRectF(M - i * 2, M - i * 2 + 5, w + i * 4, h + i * 4), rad + i * 2, rad + i * 2)
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
        d = datetime.now()
        self._btns = {}
        if prog < 0.98:
            p.setOpacity(1.0 - min(1.0, prog * 2.4))
            self._compact(p, w, h, d, now, playing, acc)
        if prog > 0.3:
            p.setOpacity(((prog - 0.3) / 0.7) ** 2)
            ox = (w - EW) / 2
            if self._music and self._page == 0:
                self._player(p, ox, now, playing, acc, prog > 0.9)
            else:
                self._info(p, ox, h, d, now, prog > 0.9, acc)
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
        if self._music:
            self._cover(p, QRectF(9, cy - 13, 26, 26), acc, now, playing, 7)
            _equalizer(p, QRectF(w - 42, cy - 8, 28, 16), acc, playing, now, 5)
        elif self._wx:
            _wx_icon(p, space_feeds.wx_kind(self._wx.get("code"), bool(self._wx.get("day", True)))[1], QRectF(10, cy - 12, 24, 24), now)
            _txt(p, QRectF(w - 52, 0, 42, h), f'{self._wx.get("t", "")}°', 10, C.TEXT_MED, QFont.Weight.DemiBold, _AL.AlignRight)
        else:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_q(C.PRI, 150))
            p.drawEllipse(QPointF(22, cy), 3.5, 3.5)
        _txt(p, QRectF(48, 0, w - 96, h), d.strftime("%H:%M"), 12, "#ffffff", QFont.Weight.Bold, _AL.AlignHCenter)

    # the player page — cover, titles, progress, buttons
    def _player(self, p: QPainter, ox: float, now: float, playing: bool, acc: QColor, live: bool):
        n = self._np
        self._cover(p, QRectF(ox + 22, 22, 68, 68), acc, now, playing, 14)
        tx = ox + 106
        tw = EW - 106 - 64
        _txt(p, QRectF(tx, 24, tw, 22), n.get("title") or "—", 11, "#ffffff", QFont.Weight.Bold)
        _txt(p, QRectF(tx, 47, tw, 16), n.get("artist") or "", 9, _mix(C.TEXT_MED, acc, 0.25))
        _txt(p, QRectF(tx, 64, tw, 15), n.get("album") or "", 8, C.TEXT_DIM)
        _equalizer(p, QRectF(ox + EW - 72, 28, 36, 12), acc, playing, now, 8)
        p.setPen(Qt.PenStyle.NoPen)                                    # ⋮ — flips to the clock page
        p.setBrush(QColor(255, 255, 255, 150))
        for i in range(3):
            p.drawEllipse(QPointF(ox + EW - 24, 32 + i * 9), 1.7, 1.7)
        if live:
            self._btns["page"] = QRectF(ox + EW - 40, 20, 34, 44)
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
        if self._music:                                                # a way back to the player
            if live:
                self._btns["page"] = QRectF(ox + W - 80, 0, 80, 20)
            _txt(p, QRectF(ox + W - 90, 2, 76, 12), "♪ player", 7, _mix(C.TEXT_DIM, acc, 0.4), None, _AL.AlignRight)

#by ASDcuber

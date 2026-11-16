"""Dynamic Island — a small black pill at the top of the screen that shows the time, the weather and what Spotify plays.

Resting: a slim pill (time in the middle; a spinning disc and equaliser while music plays, the weather otherwise).
Hover it, click it, or change the song: it grows into a card with the big time, the date, the weather and the player
(previous / play-pause / next). It never takes focus, stays above other windows and shrinks back by itself.

Turn it off with `"dynamic_island": false` in the settings file (or right-click the pill → Hide). Weather comes from Open-Meteo
(city: `hs_weather_city`, units: `hs_weather_units`), the song from Spotify's window title — see core/space_feeds.py.
"""
from __future__ import annotations

import math
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QBrush, QColor, QFont, QFontMetrics, QGuiApplication, QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF
from PyQt6.QtWidgets import QMenu, QWidget

from core import space_feeds
from ui.hs_widgets import _F, _disc, _equalizer, _mix, _q, _txt, _wx_icon
from ui.overlay_utils import force_topmost
from ui.theme import C, patch_cfg

_AL = Qt.AlignmentFlag
CW, CH = 196.0, 38.0                     # resting pill
CW_PLAY = 232.0                          # resting pill while music plays (room for the disc and the equaliser)
EW = 440.0                               # expanded card
EH_BASE, EH_PLAY = 126.0, 206.0
TOP = 8                                  # gap to the top edge of the screen


class DynamicIsland(QWidget):

    def __init__(self):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
                         | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.cw, self.ch = CW, CH
        self._hover = False
        self._pinned = False
        self._boost = 0.0                                  # expanded until this time (a new song, a click on a button)
        self._leave = 0.0
        self._wx: dict | None = None
        self._np = {"state": "none", "title": "", "artist": ""}
        self._last_title = ""
        self._btns: dict[str, QRectF] = {}
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

    # ── data ─────────────────────────────────────────────────────────────────────
    def _poll(self, now: float):
        for k, fut in list(self._jobs.items()):
            if fut.done():
                del self._jobs[k]
                try:
                    data = space_feeds.parse_json(fut.result())
                except Exception:                                  # noqa: BLE001
                    data = None
                if k == "wx" and data is not None and not data.get("error"):
                    self._wx = data
                elif k == "np" and data is not None:
                    self._np = data
                    title = data.get("title", "") if data.get("state") == "playing" else ""
                    if title and title != self._last_title:        # a new song: show it for a few seconds
                        self._boost = now + 4.0
                    self._last_title = title
        if "np" not in self._jobs and now - self._t_np >= 3.0:
            self._t_np = now
            self._jobs["np"] = self._pool.submit(space_feeds.nowplaying_json)
        if "wx" not in self._jobs and now - self._t_wx >= (600.0 if self._wx else 60.0):
            self._t_wx = now
            self._jobs["wx"] = self._pool.submit(space_feeds.weather_json)

    def _transport(self, action: str):
        def run():
            try:
                from actions import spotify_controller as sc
                sc._transport(action)
            except Exception:                                      # noqa: BLE001
                pass
        if action == "toggle":                                     # show the change at once, the next poll corrects it
            self._np = dict(self._np, state="paused" if self._np.get("state") == "playing" else "playing")
        self._t_np = time.time() - 2.0                              # re-read the song soon
        self._pool.submit(run)

    # ── geometry ─────────────────────────────────────────────────────────────────
    @property
    def _music(self) -> bool:
        return self._np.get("state") in ("playing", "paused")

    def _expanded(self, now: float) -> bool:
        return self._pinned or self._hover or now < self._boost or now - self._leave < 0.45

    def _targets(self, now: float):
        if self._expanded(now):
            return EW, (EH_PLAY if self._music else EH_BASE)
        return (CW_PLAY if self._np.get("state") == "playing" else CW), CH

    def _place(self):
        scr = QGuiApplication.primaryScreen()
        g = scr.geometry() if scr is not None else None
        cx = (g.x() + g.width() / 2) if g is not None else 960
        self.setGeometry(int(cx - self.cw / 2), (g.y() if g is not None else 0) + TOP, int(math.ceil(self.cw)), int(math.ceil(self.ch)))

    def _step(self):
        now = time.time()
        self._poll(now)
        tw, th = self._targets(now)
        moving = abs(tw - self.cw) > 0.4 or abs(th - self.ch) > 0.4
        if moving:
            k = 0.26                                               # springy ease-out
            self.cw += (tw - self.cw) * k
            self.ch += (th - self.ch) * k
            if abs(tw - self.cw) <= 0.4 and abs(th - self.ch) <= 0.4:
                self.cw, self.ch = tw, th
            self._place()
        if now - self._t_top > 2.0:                                # games and other windows steal the top spot — take it back
            self._t_top = now
            force_topmost(self, click_through=False)
        want = 16 if moving else (33 if self._np.get("state") == "playing" or self._expanded(now) else 500)
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
            m.addAction("Pin open" if not self._pinned else "Unpin").triggered.connect(lambda: setattr(self, "_pinned", not self._pinned))
            m.addAction("Hide Dynamic Island").triggered.connect(self._hide_forever)
            m.exec(e.globalPosition().toPoint())
            return
        pos = e.position()
        for name, r in self._btns.items():
            if r.contains(pos):
                self._transport({"prev": "previous", "play": "toggle", "next": "next"}[name])
                self._boost = max(self._boost, time.time() + 3.0)
                return
        self._pinned = not self._pinned
        if not self._pinned:
            self._leave = time.time()

    def _hide_forever(self):
        try:
            patch_cfg(dynamic_island=False)
        except Exception:                                          # noqa: BLE001
            pass
        self.stop()

    # ── painting ─────────────────────────────────────────────────────────────────
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        now = time.time()
        w, h = self.cw, self.ch
        playing = self._np.get("state") == "playing"
        prog = max(0.0, min(1.0, (w - CW) / (EW - CW)))            # 0 = resting pill, 1 = fully open
        rad = min(h / 2, 30.0 + 0 * prog)
        body = QPainterPath()
        body.addRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), rad, rad)
        g = QLinearGradient(0, 0, 0, h)
        g.setColorAt(0, QColor("#0b0b10"))
        g.setColorAt(1, QColor("#000000"))
        p.setPen(QPen(_q(C.GREEN, 70) if playing else QColor(255, 255, 255, 26), 1.0))
        p.setBrush(QBrush(g))
        p.drawPath(body)
        p.setClipPath(body)
        d = datetime.now()
        self._btns = {}
        if prog < 0.98:
            p.setOpacity(1.0 - min(1.0, prog * 2.2))
            self._compact(p, w, h, d, now, playing)
        if prog > 0.3:
            p.setOpacity(((prog - 0.3) / 0.7) ** 2)
            self._card(p, (w - EW) / 2, d, now, playing, prog > 0.9)
        p.end()

    def _compact(self, p: QPainter, w: float, h: float, d: datetime, now: float, playing: bool):
        cy = h / 2
        if self._music:
            _disc(p, QPointF(22, cy), 10.5, now, playing)
            _equalizer(p, QRectF(w - 42, cy - 8, 28, 16), C.GREEN, playing, now, 5)
        else:
            if self._wx:
                _wx_icon(p, space_feeds.wx_kind(self._wx.get("code"), bool(self._wx.get("day", True)))[1], QRectF(10, cy - 12, 24, 24), now)
                _txt(p, QRectF(w - 52, 0, 42, h), f'{self._wx.get("t", "")}°', 10, C.TEXT_MED, QFont.Weight.DemiBold, _AL.AlignRight)
            else:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(_q(C.PRI, 150))
                p.drawEllipse(QPointF(22, cy), 3.5, 3.5)
        _txt(p, QRectF(48, 0, w - 96, h), d.strftime("%H:%M"), 12, "#ffffff", QFont.Weight.Bold, _AL.AlignHCenter)

    def _card(self, p: QPainter, ox: float, d: datetime, now: float, playing: bool, live: bool):
        W = EW
        # time and date
        tf = _F(30, QFont.Weight.Bold)
        hm = d.strftime("%H:%M")
        _txt(p, QRectF(ox + 22, 12, 150, 50), hm, 30, "#ffffff", QFont.Weight.Bold)
        tw = QFontMetrics(tf).horizontalAdvance(hm)
        _txt(p, QRectF(ox + 22 + tw + 4, 26, 40, 24), d.strftime(":%S"), 12, C.ACC2, QFont.Weight.DemiBold)
        _txt(p, QRectF(ox + 22, 62, 210, 20), d.strftime("%A, %d %B"), 10, C.TEXT_MED)
        # weather
        wx = self._wx
        if wx:
            words, icon = space_feeds.wx_kind(wx.get("code"), bool(wx.get("day", True)))
            unit = wx.get("unit", "°C")
            _wx_icon(p, icon, QRectF(ox + W - 168, 8, 58, 58), now)
            _txt(p, QRectF(ox + W - 104, 12, 84, 34), f'{wx.get("t", "")}{unit}', 20, "#ffffff", QFont.Weight.Bold, _AL.AlignRight)
            _txt(p, QRectF(ox + W - 134, 46, 114, 16), words, 8, C.TEXT_MED, None, _AL.AlignRight)
            day = (wx.get("days") or [{}])[0]
            hi_lo = f'  ↑{day["hi"]}° ↓{day["lo"]}°' if day.get("hi") is not None else ""
            _txt(p, QRectF(ox + W - 230, 64, 210, 18), f'{wx.get("city", "")}{hi_lo}', 8, C.TEXT_DIM, None, _AL.AlignRight)
        else:
            _txt(p, QRectF(ox + W - 200, 20, 180, 30), "Weather…", 10, C.TEXT_DIM, None, _AL.AlignRight)
        if not self._music:
            return
        # player
        card = QRectF(ox + 14, 98, W - 28, 92)
        p.setPen(QPen(QColor(255, 255, 255, 22), 1))
        p.setBrush(QColor(255, 255, 255, 14))
        p.drawRoundedRect(card, 20, 20)
        _disc(p, QPointF(card.left() + 46, card.center().y()), 32, now, playing)
        tx = card.left() + 96
        _txt(p, QRectF(tx, card.top() + 12, 200, 14), "NOW PLAYING" if playing else "PAUSED", 7, C.GREEN if playing else C.AMBER, QFont.Weight.Bold)
        _txt(p, QRectF(tx, card.top() + 28, card.right() - tx - 126, 24), self._np.get("title") or "—", 12, "#ffffff", QFont.Weight.Bold)
        _txt(p, QRectF(tx, card.top() + 52, card.right() - tx - 126, 18), self._np.get("artist") or "", 9, C.TEXT_MED)
        # buttons
        by = card.center().y()
        for k, (name, cx) in enumerate((("prev", card.right() - 100), ("play", card.right() - 62), ("next", card.right() - 24))):
            big = name == "play"
            r = QRectF(cx - (17 if big else 13), by - (17 if big else 13), 34 if big else 26, 34 if big else 26)
            self._btns[name] = r if live else QRectF()
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#ffffff") if big else QColor(255, 255, 255, 30))
            p.drawEllipse(r)
            col = QColor("#000000") if big else QColor("#ffffff")
            p.setBrush(col)
            c = r.center()
            s = r.width() * 0.22
            if name == "play" and playing:
                p.drawRoundedRect(QRectF(c.x() - s * 0.9, c.y() - s, s * 0.7, s * 2), 1.5, 1.5)
                p.drawRoundedRect(QRectF(c.x() + s * 0.2, c.y() - s, s * 0.7, s * 2), 1.5, 1.5)
            elif name == "play":
                p.drawPolygon(QPolygonF([QPointF(c.x() - s * 0.7, c.y() - s), QPointF(c.x() - s * 0.7, c.y() + s), QPointF(c.x() + s * 1.1, c.y())]))
            else:
                sg = -1 if name == "prev" else 1
                p.drawPolygon(QPolygonF([QPointF(c.x() - sg * s * 0.9, c.y() - s * 0.9), QPointF(c.x() - sg * s * 0.9, c.y() + s * 0.9),
                                         QPointF(c.x() + sg * s * 0.5, c.y())]))
                p.drawRoundedRect(QRectF(c.x() + sg * s * 0.5 - (0 if sg > 0 else s * 0.3), c.y() - s * 0.9, s * 0.3, s * 1.8), 1, 1)

#by ASDcuber

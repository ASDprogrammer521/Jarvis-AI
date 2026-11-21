"""Boot intro: a short cinematic start-up sequence.

Like the lock screen it is its own top-level window laid exactly over the Jarvis window (a child widget can be hidden behind the
GPU-rendered 3D stage, a top-level one cannot). It plays for about three and a half seconds, fades out by itself, and any click
or key skips it. Switch it off with "boot_intro": false in the config (or Settings → Appearance).

Layout is computed, never hand-placed: the window is split into a reactor zone and one text block (name, subtitle, status line,
progress bar), and every element gets its own row, so nothing can overlap at any window size.
"""
from __future__ import annotations

import math
import time
from pathlib import Path

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QBrush, QColor, QConicalGradient, QFont, QLinearGradient, QPainter, QPen, QPixmap, QPolygonF, QRadialGradient,
)
from PyQt6.QtWidgets import QWidget

from ui.theme import C

_ROOT = Path(__file__).resolve().parent.parent
_MONO = "Consolas"


def _module_count() -> int:
    """How many tool modules Jarvis ships with (shown in the 'Loading modules' step); cheap and never raises."""
    try:
        return sum(1 for f in (_ROOT / "actions").glob("*.py") if not f.name.startswith("_"))
    except Exception:
        return 0


def _version() -> str:
    try:
        v = (_ROOT / "VERSION").read_text(encoding="utf-8").strip()
        return v if v else ""
    except Exception:
        return ""


def _rgba(hex_or_color, a: float) -> QColor:
    c = QColor(hex_or_color)
    c.setAlphaF(max(0.0, min(1.0, a)))
    return c


def _smooth(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


class BootScreen(QWidget):
    finished = pyqtSignal()

    RUN_MS = 3200                    # the sequence itself
    FADE_MS = 500                    # fade-out after "ONLINE"
    DURATION_MS = RUN_MS + FADE_MS

    def __init__(self, anchor: QWidget | None, name: str = "JARVIS"):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.anchor = anchor
        self.name = (name or "JARVIS").upper()
        self.version = _version()
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        mods = _module_count()
        self._steps = [                                           # (shown from progress, text)
            (0.00, "INITIALIZING CORE"),
            (0.22, f"LOADING MODULES{f'  ·  {mods}' if mods else ''}"),
            (0.45, "CALIBRATING HOLOGRAM"),
            (0.68, "LINKING VOICE & VISION"),
            (0.88, "ONLINE"),
        ]
        self._t0 = time.monotonic()
        self._done = False
        self._bg: QPixmap | None = None
        self._bg_key = None
        self._tm = QTimer(self)
        self._tm.timeout.connect(self._tick)

    # ── life cycle ───────────────────────────────────────────────────────────────────
    def start(self):
        self._follow(force_show=True)
        self._t0 = time.monotonic()
        self._tm.start(16)

    def _follow(self, force_show: bool = False):
        w = self.anchor
        if w is None:
            return
        if w.isMinimized():
            if self.isVisible():
                self.hide()
            return
        g = w.frameGeometry()
        if self.geometry() != g:
            self.setGeometry(g)
        if force_show or not self.isVisible():
            self.show()
            self.raise_()
            self.activateWindow()
            self.setFocus()

    def _elapsed_ms(self) -> float:
        return (time.monotonic() - self._t0) * 1000.0

    def _tick(self):
        ms = self._elapsed_ms()
        if ms >= self.DURATION_MS:
            self.finish()
            return
        self._follow()
        if ms > self.RUN_MS:
            self.setWindowOpacity(max(0.0, 1.0 - _smooth((ms - self.RUN_MS) / self.FADE_MS)))
        self.update()

    def finish(self):
        if self._done:
            return
        self._done = True
        self._tm.stop()
        self.hide()
        self.finished.emit()
        self.deleteLater()

    def mousePressEvent(self, e):
        self.finish()

    def keyPressEvent(self, e):
        self.finish()

    # ── cached background: vignette + faint grid ─────────────────────────────────────
    def _background(self, w: int, h: int) -> QPixmap:
        dpr = self.devicePixelRatioF()
        key = (w, h, round(dpr, 2), C.BG, C.PRI)
        if self._bg is not None and self._bg_key == key:
            return self._bg
        pm = QPixmap(max(1, int(w * dpr)), max(1, int(h * dpr)))
        pm.setDevicePixelRatio(dpr)
        p = QPainter(pm)
        p.fillRect(0, 0, w, h, QColor(C.BG))
        glow = QRadialGradient(QPointF(w / 2, h * 0.42), max(w, h) * 0.62)
        glow.setColorAt(0.0, _rgba(C.PRI, 0.13))
        glow.setColorAt(1.0, _rgba(C.PRI, 0.0))
        p.fillRect(0, 0, w, h, QBrush(glow))
        step = 52
        p.setPen(QPen(_rgba(C.PRI, 0.045), 1))
        for x in range(0, w, step):
            p.drawLine(x, 0, x, h)
        for y in range(0, h, step):
            p.drawLine(0, y, w, y)
        vig = QRadialGradient(QPointF(w / 2, h / 2), math.hypot(w, h) * 0.55)
        vig.setColorAt(0.55, QColor(0, 0, 0, 0))
        vig.setColorAt(1.0, QColor(0, 0, 0, 175))
        p.fillRect(0, 0, w, h, QBrush(vig))
        p.end()
        self._bg, self._bg_key = pm, key
        return pm

    # ── painting ─────────────────────────────────────────────────────────────────────
    def paintEvent(self, _):
        ms = self._elapsed_ms()
        prog = min(1.0, ms / self.RUN_MS)
        ease = _smooth(prog)
        t = ms / 1000.0
        w, h = self.width(), self.height()
        if w < 50 or h < 50:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.drawPixmap(0, 0, self._background(w, h))

        pri, acc = QColor(C.PRI), QColor(C.ACC)
        intro = _smooth(ms / 600.0)

        # ── layout: a reactor zone above one text block, each element on its own row ──
        mt, mb = 56, 64
        px = max(28, min(46, int(min(w, h) * 0.058)))
        name_h, sub_h, status_h, bar_h = int(px * 1.5), 20, 24, 4
        gap_name, gap_status, gap_bar, gap_zone = 10, 30, 18, 24
        block = name_h + gap_name + sub_h + gap_status + status_h + gap_bar + bar_h
        zone = max(120.0, h - mt - mb - block - gap_zone)
        base = max(26.0, min(zone / 2 / 2.3, min(w, h) * 0.115, 104.0))
        c = QPointF(w / 2, mt + zone / 2)
        name_y = mt + zone + 12
        sub_y = name_y + name_h + gap_name
        status_y = sub_y + sub_h + gap_status
        bar_y = status_y + status_h + gap_bar

        self._particles(p, w, h, t, intro)
        self._sweep(p, w, h, prog)

        # ── reactor ──
        p.save()
        p.setOpacity(intro)
        self._reactor(p, c, base, t, prog, ease, pri, acc)
        p.restore()

        # ── text block ──
        self._title(p, w, name_y, name_h, prog, t)
        self._subtitle(p, w, sub_y, sub_h, prog)
        self._status(p, w, status_y, status_h, prog, ms)
        self._bar(p, w, bar_y, bar_h, ease, pri, acc)
        self._corners(p, w, h, ms, intro)
        p.end()

    def _particles(self, p: QPainter, w: int, h: int, t: float, intro: float):
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(46):
            s = (i * 0.7548776662) % 1.0
            x = ((i * 0.6180339887) % 1.0) * w
            speed = 12.0 + s * 28.0
            y = h + 10 - ((t * speed + s * h) % (h + 20))
            tw = 0.5 + 0.5 * math.sin(t * 1.8 + i)
            p.setBrush(_rgba(C.PRI, (0.10 + 0.22 * tw) * intro))
            r = 0.9 + s * 1.3
            p.drawEllipse(QPointF(x, y), r, r)

    def _sweep(self, p: QPainter, w: int, h: int, prog: float):
        """One soft horizontal scan line travelling down the screen."""
        y = (prog * 1.25 - 0.1) * h
        if y < -60 or y > h + 60:
            return
        g = QLinearGradient(0, y - 60, 0, y + 60)
        g.setColorAt(0.0, _rgba(C.PRI, 0.0))
        g.setColorAt(0.5, _rgba(C.PRI, 0.07))
        g.setColorAt(1.0, _rgba(C.PRI, 0.0))
        p.fillRect(QRectF(0, y - 60, w, 120), QBrush(g))

    def _reactor(self, p: QPainter, c: QPointF, base: float, t: float, prog: float, ease: float, pri: QColor, acc: QColor):
        pulse = 0.5 + 0.5 * math.sin(t * 3.0)
        # core glow
        g = QRadialGradient(c, base * (1.9 + 0.08 * pulse))
        g.setColorAt(0.0, _rgba(pri, 0.30 + 0.12 * pulse))
        g.setColorAt(0.45, _rgba(acc, 0.10))
        g.setColorAt(1.0, _rgba(pri, 0.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawEllipse(c, base * 2.0, base * 2.0)

        # radar sweep inside the main ring
        sweep = QConicalGradient(c, -(t * 140.0) % 360.0)
        sweep.setColorAt(0.0, _rgba(pri, 0.34))
        sweep.setColorAt(0.20, _rgba(pri, 0.0))
        sweep.setColorAt(1.0, _rgba(pri, 0.0))
        p.setBrush(QBrush(sweep))
        p.drawEllipse(c, base * 0.97, base * 0.97)
        p.setBrush(Qt.BrushStyle.NoBrush)

        # faint base ring + gradient progress arc with a bright head
        p.setPen(QPen(_rgba(pri, 0.20), 1.4))
        p.drawEllipse(c, base, base)
        if ease > 0.002:
            cg = QConicalGradient(c, 90.0)
            cg.setColorAt(1.0, pri)
            cg.setColorAt(max(0.0, 1.0 - ease), acc)
            if ease < 1.0:
                cg.setColorAt(0.0, acc)
            p.setPen(QPen(QBrush(cg), 3.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawArc(QRectF(c.x() - base, c.y() - base, base * 2, base * 2), 90 * 16, int(-360 * 16 * ease))
            a = math.radians(90 - 360 * ease)
            head = QPointF(c.x() + math.cos(a) * base, c.y() - math.sin(a) * base)
            hg = QRadialGradient(head, 11)
            hg.setColorAt(0.0, QColor(255, 255, 255, 235))
            hg.setColorAt(0.35, _rgba(acc, 0.65))
            hg.setColorAt(1.0, _rgba(acc, 0.0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(hg))
            p.drawEllipse(head, 11, 11)
            p.setBrush(Qt.BrushStyle.NoBrush)

        # inner hexagon, slowly turning
        hexr = base * 0.80
        poly = QPolygonF()
        for k in range(6):
            a = math.radians(60 * k + t * 24.0)
            poly.append(QPointF(c.x() + math.cos(a) * hexr, c.y() + math.sin(a) * hexr))
        p.setPen(QPen(_rgba(pri, 0.34), 1.2))
        p.drawPolygon(poly)

        # segmented ring (24 arcs), turning clockwise
        p.setPen(QPen(_rgba(pri, 0.82), 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        r1 = base * 1.24
        rect1 = QRectF(c.x() - r1, c.y() - r1, r1 * 2, r1 * 2)
        off = -t * 38.0
        for k in range(24):
            p.drawArc(rect1, int((k * 15.0 + off) * 16), int(9.0 * 16))

        # dashed counter-rotating ring and three orbiting dots
        r2 = base * 1.52
        p.save()
        p.translate(c)
        p.rotate(-t * 22.0)
        p.setPen(QPen(_rgba(acc, 0.50), 1.1, Qt.PenStyle.DashLine))
        p.drawEllipse(QPointF(0, 0), r2, r2)
        p.restore()
        p.setPen(Qt.PenStyle.NoPen)
        for k in range(3):
            a = math.radians(t * 70.0 + k * 120.0)
            d = QPointF(c.x() + math.cos(a) * r2, c.y() + math.sin(a) * r2)
            dg = QRadialGradient(d, 8)
            dg.setColorAt(0.0, QColor(255, 255, 255, 230))
            dg.setColorAt(0.4, _rgba(acc, 0.7))
            dg.setColorAt(1.0, _rgba(acc, 0.0))
            p.setBrush(QBrush(dg))
            p.drawEllipse(d, 8, 8)

        # outer tick ring
        p.setPen(QPen(_rgba(pri, 0.55), 1.2))
        for k in range(72):
            a = math.radians(k * 5.0 + t * 9.0)
            r_in = base * 1.86
            r_out = base * (2.12 if k % 6 == 0 else 1.98)
            p.drawLine(QPointF(c.x() + math.cos(a) * r_in, c.y() + math.sin(a) * r_in),
                       QPointF(c.x() + math.cos(a) * r_out, c.y() + math.sin(a) * r_out))

        # shockwave when the system comes online
        if prog > 0.88:
            s = _smooth((prog - 0.88) / 0.12)
            p.setPen(QPen(_rgba(pri, 0.65 * (1 - s)), 2.0))
            rr = base * (1.0 + s * 1.15)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(c, rr, rr)

        # percentage in the middle
        pct = int(round(min(1.0, prog) * 100))
        f = QFont("Segoe UI", 10, QFont.Weight.Light)
        f.setPixelSize(int(base * 0.62))
        p.setFont(f)
        p.setPen(QColor(C.TEXT))
        p.drawText(QRectF(c.x() - base * 0.7, c.y() - base * 0.45, base * 1.4, base * 0.8), Qt.AlignmentFlag.AlignCenter, str(pct))
        f2 = QFont(_MONO, 8)
        f2.setPixelSize(max(9, int(base * 0.15)))
        f2.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2)
        p.setFont(f2)
        p.setPen(_rgba(C.TEXT_DIM, 1.0))
        p.drawText(QRectF(c.x() - base * 0.5, c.y() + base * 0.30, base, base * 0.3), Qt.AlignmentFlag.AlignCenter, "PERCENT")

    def _title(self, p: QPainter, w: int, y: float, h: int, prog: float, t: float):
        a = _smooth((prog - 0.04) / 0.18)
        if a <= 0:
            return
        px = max(28, min(46, int(min(w, self.height()) * 0.058)))
        f = QFont("Segoe UI", 10, QFont.Weight.DemiBold)
        f.setPixelSize(px)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 12.0 + 12.0 * (1 - a))      # letters glide together as it appears
        p.setFont(f)
        rect = QRectF(0, y, w, h)
        band = _smooth((prog - 0.18) / 0.62)                                              # one light sweep across the name
        cx = w / 2 + (band * 1.6 - 0.8) * w * 0.28
        g = QLinearGradient(cx - w * 0.10, 0, cx + w * 0.10, 0)
        g.setColorAt(0.0, QColor(C.TEXT))
        g.setColorAt(0.5, QColor(255, 255, 255))
        g.setColorAt(1.0, QColor(C.TEXT))
        p.save()
        p.setOpacity(a)
        p.setPen(QPen(QBrush(g), 1))
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.name)
        p.restore()

    def _subtitle(self, p: QPainter, w: int, y: float, h: int, prog: float):
        a = _smooth((prog - 0.14) / 0.2)
        if a <= 0:
            return
        f = QFont("Segoe UI", 8, QFont.Weight.DemiBold)
        f.setPixelSize(12)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 7.0)
        p.setFont(f)
        p.setPen(_rgba(C.TEXT_DIM, a))
        p.drawText(QRectF(0, y, w, h), Qt.AlignmentFlag.AlignCenter, "YOUR AI ASSISTANT")

    def _status(self, p: QPainter, w: int, y: float, h: int, prog: float, ms: float):
        """Only the current step is shown (one row, crossfading from the previous one), so lines can never pile up."""
        idx = 0
        for i, (t0, _) in enumerate(self._steps):
            if prog >= t0:
                idx = i
        t0, text = self._steps[idx]
        since = (prog - t0) * self.RUN_MS
        a = _smooth(since / 260.0)
        online = idx == len(self._steps) - 1
        f = QFont(_MONO, 10)
        f.setPixelSize(14)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 3.0)
        p.setFont(f)
        rise = (1 - a) * 9.0
        col = QColor(C.GREEN if online else C.PRI)
        if idx > 0 and a < 1.0:                                   # the step that is leaving fades out upwards
            prev = self._steps[idx - 1][1]
            p.setPen(_rgba(C.TEXT_DIM, 0.8 * max(0.0, 1.0 - a * 2.5)))          # gone before the new line is readable
            p.drawText(QRectF(0, y - a * 9.0, w, h), Qt.AlignmentFlag.AlignCenter, prev)
        tag = f"{idx + 1:02d}/{len(self._steps):02d}   "
        txt = ("●  " if online else tag) + text
        if not online and int(ms / 300) % 2 == 0 and a >= 1.0:
            txt += " _"
        col.setAlphaF(_smooth((a - 0.35) / 0.65) if idx > 0 else a)
        p.setPen(col)
        p.drawText(QRectF(0, y + rise, w, h), Qt.AlignmentFlag.AlignCenter, txt)

    def _bar(self, p: QPainter, w: int, y: float, h: int, ease: float, pri: QColor, acc: QColor):
        bw = min(460.0, w * 0.46)
        x = (w - bw) / 2
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 24))
        p.drawRoundedRect(QRectF(x, y, bw, h), h / 2, h / 2)
        fw = max(0.0, bw * ease)
        if fw > 0.5:
            g = QLinearGradient(x, 0, x + bw, 0)
            g.setColorAt(0.0, pri)
            g.setColorAt(1.0, acc)
            p.setBrush(QBrush(g))
            p.drawRoundedRect(QRectF(x, y, fw, h), h / 2, h / 2)
            hg = QRadialGradient(QPointF(x + fw, y + h / 2), 14)
            hg.setColorAt(0.0, _rgba(acc, 0.85))
            hg.setColorAt(1.0, _rgba(acc, 0.0))
            p.setBrush(QBrush(hg))
            p.drawEllipse(QPointF(x + fw, y + h / 2), 14, 14)

    def _corners(self, p: QPainter, w: int, h: int, ms: float, intro: float):
        """HUD frame: four corner brackets and small captions at the very edges (clear of the centre block)."""
        m, L = 26, 24
        p.save()
        p.setOpacity(intro)
        p.setPen(QPen(_rgba(C.PRI, 0.55), 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.SquareCap))
        for sx, sy in ((m, m), (w - m, m), (m, h - m), (w - m, h - m)):
            dx = L if sx < w / 2 else -L
            dy = L if sy < h / 2 else -L
            p.drawLine(QPointF(sx, sy), QPointF(sx + dx, sy))
            p.drawLine(QPointF(sx, sy), QPointF(sx, sy + dy))
        f = QFont(_MONO, 8)
        f.setPixelSize(11)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2.0)
        p.setFont(f)
        p.setPen(_rgba(C.TEXT_DIM, 0.9))
        blink = "●" if int(ms / 500) % 2 == 0 else "○"
        p.drawText(QRectF(m + 12, m + 6, 260, 16), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, f"{blink} SYSTEM BOOT")
        if self.version:
            p.drawText(QRectF(w - m - 272, m + 6, 260, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"v{self.version}")
        p.drawText(QRectF(w - m - 372, h - m - 22, 360, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   "CLICK OR PRESS ANY KEY TO SKIP")
        p.restore()

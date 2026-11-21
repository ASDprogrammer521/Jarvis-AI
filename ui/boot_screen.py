"""Boot intro: a short cinematic start-up sequence ("Initializing core… Loading modules… Online").

Like the lock screen it is its own top-level window laid exactly over the Jarvis window (a child widget can be hidden behind the
GPU-rendered 3D stage, a top-level one cannot). It plays for about three seconds, fades out by itself, and any click or key
skips it. Switch it off with "boot_intro": false in the config (or Settings → Appearance).
"""
from __future__ import annotations

import math
import time
from pathlib import Path

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen, QRadialGradient
from PyQt6.QtWidgets import QWidget

from ui.theme import C

_ROOT = Path(__file__).resolve().parent.parent


def _module_count() -> int:
    """How many tool modules Jarvis ships with (shown in the 'Loading modules' line); cheap and never raises."""
    try:
        return sum(1 for f in (_ROOT / "actions").glob("*.py") if not f.name.startswith("_"))
    except Exception:
        return 0


class BootScreen(QWidget):
    finished = pyqtSignal()

    RUN_MS = 2600                    # the sequence itself
    FADE_MS = 450                    # fade-out after "ONLINE"
    DURATION_MS = RUN_MS + FADE_MS

    def __init__(self, anchor: QWidget | None, name: str = "JARVIS"):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.anchor = anchor
        self.name = (name or "JARVIS").upper()
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        mods = _module_count()
        self._steps = [                                           # (shown from, text)
            (0.04, "Initializing core"),
            (0.26, f"Loading modules{f' ({mods})' if mods else ''}"),
            (0.50, "Calibrating hologram"),
            (0.72, "Linking voice and vision"),
            (0.90, "Online"),
        ]
        self._t0 = time.monotonic()
        self._done = False
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
            self.setWindowOpacity(max(0.0, 1.0 - (ms - self.RUN_MS) / self.FADE_MS))
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

    # ── painting ─────────────────────────────────────────────────────────────────────
    def paintEvent(self, _):
        ms = self._elapsed_ms()
        prog = min(1.0, ms / self.RUN_MS)
        ease = 1 - (1 - prog) ** 3
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(C.BG))
        w, h = self.width(), self.height()
        c = QPointF(w / 2, h * 0.42)
        pri = QColor(C.PRI)
        acc = QColor(C.ACC)

        # soft glow that grows as the system comes up
        g = QRadialGradient(c, min(w, h) * (0.25 + 0.30 * ease))
        g.setColorAt(0, QColor(pri.red(), pri.green(), pri.blue(), int(70 * ease)))
        g.setColorAt(1, QColor(pri.red(), pri.green(), pri.blue(), 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(g)
        p.drawEllipse(c, min(w, h) * 0.55, min(w, h) * 0.55)

        # three counter-rotating rings; the first one is a progress arc
        p.setBrush(Qt.BrushStyle.NoBrush)
        spin = ms / 1000.0
        base = max(54.0, min(w, h) * 0.13)
        p.setPen(QPen(QColor(pri.red(), pri.green(), pri.blue(), 70), 1.4))
        p.drawEllipse(c, base, base)
        p.setPen(QPen(pri, 3.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(QRectF(c.x() - base, c.y() - base, base * 2, base * 2), 90 * 16, int(-360 * 16 * ease))
        for k, (rad, wid, alpha, speed, dash) in enumerate(((1.30, 1.8, 200, 70, False), (1.62, 1.1, 120, -45, True), (1.95, 0.8, 70, 28, True))):
            col = QColor(acc if k == 1 else pri)
            col.setAlpha(int(alpha * min(1.0, ease * 1.6)))
            p.setPen(QPen(col, wid, Qt.PenStyle.DashLine if dash else Qt.PenStyle.SolidLine))
            p.save()
            p.translate(c)
            p.rotate(spin * speed)
            p.drawEllipse(QPointF(0, 0), base * rad, base * rad)
            p.restore()
        # tick marks on the outer ring
        p.setPen(QPen(QColor(pri.red(), pri.green(), pri.blue(), int(150 * ease)), 1.2))
        for i in range(36):
            a = math.radians(i * 10 + spin * 18)
            r1, r2 = base * 2.10, base * (2.20 if i % 3 else 2.30)
            p.drawLine(QPointF(c.x() + math.cos(a) * r1, c.y() + math.sin(a) * r1),
                       QPointF(c.x() + math.cos(a) * r2, c.y() + math.sin(a) * r2))

        # name, letter by letter
        f = QFont("Segoe UI", 26, QFont.Weight.Bold)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 10)
        p.setFont(f)
        shown = int(len(self.name) * min(1.0, max(0.0, (prog - 0.05) / 0.35)) + 0.999)
        p.setPen(QColor(C.TEXT))
        ny = c.y() + base * 2.35 + 46
        p.drawText(QRectF(0, ny, w, 50), Qt.AlignmentFlag.AlignCenter, self.name[:shown])

        # status lines: previous ones dim, the latest one bright, with a blinking cursor
        done = [s for t, s in self._steps if prog >= t]
        sf = QFont("Consolas", 11)
        p.setFont(sf)
        ly = ny + 64
        for i, line in enumerate(done[-3:]):
            last = i == len(done[-3:]) - 1
            online = line == "Online"
            col = QColor(C.GREEN if online else (C.PRI if last else C.TEXT_DIM))
            txt = f"▸ {line}" + ("" if online else " …" if last else "  ✓")
            if last and not online and int(ms / 260) % 2 == 0:
                txt += "▌"
            p.setPen(col)
            p.drawText(QRectF(0, ly + i * 24, w, 22), Qt.AlignmentFlag.AlignCenter, txt)

        # thin progress bar at the bottom
        bw = min(420.0, w * 0.5)
        bx, by = (w - bw) / 2, h - 70
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 22))
        p.drawRoundedRect(QRectF(bx, by, bw, 3), 1.5, 1.5)
        gr = QLinearGradient(bx, 0, bx + bw, 0)
        gr.setColorAt(0, QColor(C.PRI))
        gr.setColorAt(1, QColor(C.ACC))
        p.setBrush(gr)
        p.drawRoundedRect(QRectF(bx, by, bw * ease, 3), 1.5, 1.5)
        p.setPen(QColor(C.TEXT_DIM))
        p.setFont(QFont("Segoe UI", 8))
        p.drawText(QRectF(0, by + 10, w, 16), Qt.AlignmentFlag.AlignCenter, "click or press any key to skip")

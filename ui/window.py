from __future__ import annotations

import base64
import math
import os
import platform
import re
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from PyQt6.QtCore import QCoreApplication, QEasingCurve, QEvent, QObject, QPoint, QPointF, QPropertyAnimation, QRect, QRectF, Qt, QTimer, QUrl, QVariantAnimation, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QCursor, QDesktopServices, QFont, QImage, QKeySequence, QLinearGradient, QPainter, QPen, QPixmap, QPolygonF, QRadialGradient, QSurfaceFormat
from PyQt6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QPushButton, QSizePolicy, QSlider, QStackedWidget, QStyle, QTextEdit, QVBoxLayout, QWidget,
)

import ui.theme as ui_theme
from ui.fx import GL_ENABLED, Backdrop, GlassCard, GlowButton, ModalLayer, Stage3D, Toggle, fade_in, slide_to
from ui.metrics import _metrics
from ui.panels import APP_VERSION, ConfirmPanel, RemotePanel, SettingsPanel, SetupPanel
from ui.theme import C, DAY_THEME, DEFAULT_ACCENT, THEMES, T, apply_accent, apply_theme, auto_theme_key, build_qss, current_theme, patch_cfg, qcol, read_cfg, rgba
from ui.video_extras import PlayerButton, PlayButton, PlayerSettings, VideosOverlay
from ui.toasts import ToastCenter
from ui.pages import DashboardPage, InputBar, LogoMark, NavItem, PhonePage, Sidebar
from ui.widgets import ChatView, IconButton, StatePill, font

_OS = platform.system()
_WIN_HIDE: dict = {"creationflags": subprocess.CREATE_NO_WINDOW} if _OS == "Windows" else {}


BASE_DIR = ui_theme.ROOT
APP_ID = "jarvis-ai"


class CompactMic(QWidget):
    clicked = pyqtSignal()
    restore = pyqtSignal()

    def __init__(self):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(72, 72)
        self.muted = False
        self.amp = 0.0
        self._drag: QPoint | None = None
        self._moved = False
        self._t = QTimer(self)
        self._t.timeout.connect(self.update)
        self._t.start(50)

    def paintEvent(self, _):
        from ui.widgets import draw_icon
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        col = qcol(C.RED if self.muted else C.PRI)
        g = QRadialGradient(36, 36, 36)
        g.setColorAt(0.55, QColor(col.red(), col.green(), col.blue(), int(60 + 120 * self.amp)))
        g.setColorAt(1, QColor(col.red(), col.green(), col.blue(), 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawEllipse(0, 0, 72, 72)
        p.setBrush(qcol(C.PANEL, 235))
        p.setPen(QPen(col, 1.6))
        p.drawEllipse(10, 10, 52, 52)
        draw_icon(p, "mic_off" if self.muted else "mic", QRectF(21, 21, 30, 30), col, 2.0)

    def mousePressEvent(self, e):
        self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
        self._moved = False

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self._moved = True
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e):
        if not self._moved:
            self.clicked.emit()
        self._drag = None

    def mouseDoubleClickEvent(self, e):
        self.restore.emit()


def _mmss(sec) -> str:
    sec = int(max(0, sec))
    return f"{sec // 60}:{sec % 60:02d}"


WORKOUT_TITLES = {"PUSH-UPS", "SQUATS", "SIT-UPS", "PULL-UPS", "PLANK"}


class PushupHUD(QWidget):
    """The big workout counter in the top-left corner of the full-screen view (push-ups, squats, sit-ups, pull-ups, plank).

    Three looks: reps (a big number), time (plank: a big m:ss clock) and rest (a countdown between sets, in green).
    Painted by hand (no pixmaps, no timer): it redraws only when new numbers arrive (~14 times a second) and for the short
    "pop" animation that plays every time a rep is counted."""

    BW, BH = 300, 276

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.label = "PUSH-UPS"
        self.mode = "reps"                                # reps | time | rest
        self.reps = 0                                     # this set's number (plank: seconds held)
        self.target = None
        self.good = 0
        self.elapsed = 0.0
        self.ok = False
        self.set_no, self.sets = 1, 1
        self.rest_left, self.rest_total = 0.0, 60
        self.record = False
        self._k = 1.0
        self._pop = 1.0                                   # 0 → 1 after each rep (plank: each second)
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setDuration(420)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_pop)
        self.hide()
        self.place()

    def _on_pop(self, v):
        self._pop = float(v)
        self.update()

    def reset(self):
        self.mode, self.reps, self.target, self.good, self.elapsed, self.ok = "reps", 0, None, 0, 0.0, False
        self.set_no, self.sets, self.rest_left, self.record = 1, 1, 0.0, False
        self._anim.stop()
        self._pop = 1.0

    def set_stats(self, d: dict):
        n = int(d.get("value", d.get("reps", 0)))
        mode = str(d.get("mode", "reps"))
        if self.isVisible() and mode != "rest" and mode == self.mode and n > self.reps:
            self._anim.stop()
            self._anim.start()
        self.mode = mode
        self.label = str(d.get("label") or "PUSH-UPS")
        self.reps = n
        self.target = d.get("target") or None
        self.good = int(d.get("good", 0))
        self.elapsed = float(d.get("elapsed", 0.0))
        self.ok = bool(d.get("ok", False))
        self.set_no, self.sets = int(d.get("set", 1)), int(d.get("sets", 1))
        self.rest_left, self.rest_total = float(d.get("rest_left", 0.0)), max(1.0, float(d.get("rest_total", 60)))
        self.record = bool(d.get("record", False))
        self.update()

    def place(self):
        p = self.parentWidget()
        if p is None:
            return
        self._k = max(0.7, min(1.5, p.height() / 1080.0))      # scales with the screen: the same look on 1080p and 4K
        self.setFixedSize(int(self.BW * self._k), int(self.BH * self._k))
        self.move(int(22 * self._k), int(58 * self._k))

    @staticmethod
    def _outlined(p, rect, flags, text, color, shadow=170, d=2.0):
        """Text with a dark outline: readable on any camera picture without a background box."""
        p.setPen(QColor(0, 0, 0, shadow))
        for dx, dy in ((-d, 0), (d, 0), (0, -d), (0, d), (d, d), (-d, d), (d, -d), (-d, -d)):
            p.drawText(rect.translated(dx, dy), flags, text)
        p.setPen(color)
        p.drawText(rect, flags, text)

    def paintEvent(self, _):
        """No card, no background: just the big number, a small caption and the clock, outlined so they read on the camera picture."""
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.scale(self._k, self._k)
        W = self.BW
        R = Qt.AlignmentFlag
        resting = self.mode == "rest"
        gold = QColor(92, 255, 176) if resting else QColor(255, 214, 102)
        pop = 1.0 if resting else self._pop
        # small title line (+ set n/N), left-aligned
        f = font(10, QFont.Weight.Bold)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 3)
        p.setFont(f)
        title = self.label + (f"  ·  SET {self.set_no}/{self.sets}" if self.sets > 1 else "")
        if self.record:
            title = "🏆 " + title
        self._outlined(p, QRectF(4, 0, W, 24), R.AlignLeft | R.AlignVCenter, title, QColor(255, 255, 255, 235), d=1.5)
        # the big number: reps / m:ss / rest seconds
        if resting:
            text, px = str(int(math.ceil(self.rest_left))), 190
        elif self.mode == "time":
            text, px = _mmss(self.reps), 124
        else:
            text, px = str(self.reps), (190 if len(str(self.reps)) <= 2 else 140)
        nf = font(10, QFont.Weight.Black)
        nf.setPixelSize(px)
        p.save()
        p.translate(4, 120)
        sc = 1.0 + 0.30 * (1 - pop) ** 2                  # pops on every rep, then settles
        p.scale(sc, sc)                                   # (grows from the left edge so it stays in the corner)
        p.translate(0, 0)
        p.setFont(nf)
        mix = (1 - pop) * 0.85
        col = QColor(int(gold.red() + (255 - gold.red()) * mix), int(gold.green() + (255 - gold.green()) * mix),
                     int(gold.blue() + (255 - gold.blue()) * mix))
        self._outlined(p, QRectF(0, -100, W, 200), R.AlignLeft | R.AlignVCenter, text, col, shadow=190, d=3.0)
        p.restore()
        # "of 15" / "rest · next set" next to the number's baseline
        cap = None
        if resting:
            cap = f"REST · next: set {min(self.sets, self.set_no + 1)}"
        elif self.target:
            cap = f"of {_mmss(self.target)}" if self.mode == "time" else f"of {self.target}"
        if cap:
            p.setFont(font(14, QFont.Weight.Bold))
            self._outlined(p, QRectF(4, 200, W, 26), R.AlignLeft | R.AlignVCenter, cap, QColor(235, 242, 255), d=1.6)
        # the clock, a bit bigger than a caption
        cf = font(18, QFont.Weight.Black)
        cf.setPixelSize(40)
        p.setFont(cf)
        self._outlined(p, QRectF(4, 204 if not cap else 224, W, 44), R.AlignLeft | R.AlignVCenter,
                       "⏱ " + _mmss(self.elapsed), QColor(255, 255, 255), d=2.0)


class WorkoutPanel(QWidget):
    """Side panels for the workout view, drawn in the free strips beside the camera picture.

    side="left":  today's ring (progress to the daily goal), streak, the sets done so far and the record to beat.
    side="right": technique — a live angle gauge, the form verdict and one bar per rep (green = clean, amber = flawed,
                  height = how long the rep took).
    Painted by hand and repainted only when numbers arrive (~14 times a second)."""

    BW, BH = 210, 330

    def __init__(self, parent, side: str):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.side = side
        self.d: dict = {}
        self._k = 1.0
        self.hide()

    def set_stats(self, d: dict):
        self.d = d
        self.update()

    def scale_to(self, k: float):
        self._k = k
        self.setFixedSize(int(self.BW * k), int(self.BH * k))

    # ── drawing helpers ──
    @staticmethod
    def _t(p, x, y, w, h, text, size=11, weight=QFont.Weight.DemiBold, color=(225, 235, 250, 230), align=Qt.AlignmentFlag.AlignLeft, spacing=0):
        f = font(size, weight)
        if spacing:
            f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
        p.setFont(f)
        p.setPen(QColor(*color))
        p.drawText(QRectF(x, y, w, h), align | Qt.AlignmentFlag.AlignVCenter, text)

    def paintEvent(self, _):
        d = self.d
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.scale(self._k, self._k)
        W, H = self.BW, self.BH
        acc = qcol(C.PRI)
        bg = QLinearGradient(0, 0, 0, H)
        bg.setColorAt(0.0, QColor(16, 22, 38, 235))
        bg.setColorAt(1.0, QColor(8, 11, 20, 235))
        p.setPen(QPen(QColor(acc.red(), acc.green(), acc.blue(), 120), 1.4))
        p.setBrush(QBrush(bg))
        p.drawRoundedRect(QRectF(1, 1, W - 2, H - 2), 20, 20)
        if not d:
            return
        (self._left if self.side == "left" else self._right)(p, d, W, H, acc)

    def _left(self, p, d, W, H, acc):
        A = Qt.AlignmentFlag
        time_mode = d.get("unit") == "seconds"
        self._t(p, 18, 12, W - 36, 18, "TODAY", 8, QFont.Weight.Bold, (acc.red(), acc.green(), acc.blue(), 235), spacing=3)
        # daily-goal ring
        goal = max(1, int(d.get("goal", 100)))
        today = int(d.get("today", 0))
        frac = max(0.0, min(1.0, today / goal))
        cx, cy, r = W / 2, 84.0, 44.0
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 34), 9))
        p.drawEllipse(QPointF(cx, cy), r, r)
        done = today >= goal
        col = QColor(92, 255, 176) if done else QColor(255, 214, 102)
        pen = QPen(col, 9)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        if frac > 0:
            p.drawArc(QRectF(cx - r, cy - r, 2 * r, 2 * r), 90 * 16, -int(360 * 16 * frac))
        self._t(p, cx - 40, cy - 19, 80, 30, _mmss(today) if time_mode else str(today), 20, QFont.Weight.Black, (255, 255, 255, 255), A.AlignCenter)
        self._t(p, cx - 40, cy + 8, 80, 16, f"/ {_mmss(goal) if time_mode else goal}", 9, QFont.Weight.DemiBold, (170, 185, 215, 230), A.AlignCenter)
        streak = int(d.get("streak", 0))
        self._t(p, 14, 138, W - 28, 20, f"🔥 {streak}-day streak" if streak else "Start your streak today", 10, QFont.Weight.DemiBold,
                (255, 190, 110, 240) if streak else (170, 185, 215, 220), A.AlignCenter)
        p.setPen(QPen(QColor(255, 255, 255, 26), 1))
        p.drawLine(QPointF(16, 166), QPointF(W - 16, 166))
        # sets
        self._t(p, 18, 172, W - 36, 16, "SETS", 8, QFont.Weight.Bold, (150, 165, 200, 230), spacing=3)
        y = 192
        for n, v, g in (d.get("sets_done") or [])[-4:]:
            self._t(p, 18, y, 70, 20, f"Set {n}", 10, color=(200, 212, 235, 230))
            self._t(p, W - 100, y, 82, 20, (_mmss(v) if time_mode else str(v)) + "  ✓", 11, QFont.Weight.Bold, (92, 255, 176, 240), A.AlignRight)
            y += 21
        if d.get("mode") != "rest" and int(d.get("sets", 1)) >= int(d.get("set", 1)):
            self._t(p, 18, y, 70, 20, f"Set {int(d.get('set', 1))}", 10, QFont.Weight.Bold, (255, 214, 102, 245))
            self._t(p, W - 100, y, 82, 20, (_mmss(d.get("value", 0)) if time_mode else str(d.get("value", 0))) + "  ▸", 11, QFont.Weight.Bold,
                    (255, 214, 102, 245), A.AlignRight)
        # record to beat
        best, total = int(d.get("best", 0)), int(d.get("total", 0))
        p.setPen(QPen(QColor(255, 255, 255, 26), 1))
        p.drawLine(QPointF(16, 276), QPointF(W - 16, 276))
        if best > 0:
            self._t(p, 18, 280, 100, 16, "RECORD", 8, QFont.Weight.Bold, (150, 165, 200, 230), spacing=3)
            self._t(p, W - 100, 280, 82, 16, f"🏆 {_mmss(best) if time_mode else best}", 10, QFont.Weight.Bold, (255, 214, 102, 245), A.AlignRight)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 34))
            p.drawRoundedRect(QRectF(18, 304, W - 36, 8), 4, 4)
            fr = max(0.0, min(1.0, total / float(best)))
            p.setBrush(QColor(92, 255, 176) if total > best else QColor(255, 214, 102))
            if fr > 0:
                p.drawRoundedRect(QRectF(18, 304, max(8.0, (W - 36) * fr), 8), 4, 4)
        else:
            self._t(p, 14, 282, W - 28, 36, "First time — set your record!", 10, QFont.Weight.DemiBold, (170, 185, 215, 230), A.AlignCenter)

    def _right(self, p, d, W, H, acc):
        A = Qt.AlignmentFlag
        self._t(p, 18, 12, W - 36, 18, "TECHNIQUE", 8, QFont.Weight.Bold, (acc.red(), acc.green(), acc.blue(), 235), spacing=3)
        # angle gauge: a half-circle, the needle follows the joint angle (0° left … 180° right)
        ang = max(0, min(180, int(d.get("angle", 0))))
        cx, cy, r = W / 2, 112.0, 58.0
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 34), 9))
        p.drawArc(QRectF(cx - r, cy - r, 2 * r, 2 * r), 0, 180 * 16)
        ok = bool(d.get("ok"))
        col = QColor(92, 255, 176) if ok else QColor(255, 200, 87)
        pen = QPen(col, 9)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        if ang > 0:
            p.drawArc(QRectF(cx - r, cy - r, 2 * r, 2 * r), 180 * 16, -int(ang * 16))
        a = math.radians(180 - ang)
        p.setPen(QPen(QColor(255, 255, 255, 235), 2.4))
        p.drawLine(QPointF(cx, cy), QPointF(cx + math.cos(a) * (r - 14), cy - math.sin(a) * (r - 14)))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255))
        p.drawEllipse(QPointF(cx, cy), 4.5, 4.5)
        self._t(p, cx - 50, cy + 6, 100, 30, f"{ang}°", 19, QFont.Weight.Black, (255, 255, 255, 255), A.AlignCenter)
        self._t(p, 10, cy + 34, W - 20, 16, "body line" if d.get("unit") == "seconds" else "joint angle", 8, color=(150, 165, 200, 220), align=A.AlignCenter, spacing=1)
        # verdict
        log = d.get("rep_log") or []
        if d.get("unit") == "seconds":
            verdict, vcol = (f"Longest hold {_mmss(d.get('best_streak', 0))}", (92, 255, 176, 240))
            bad = int(d.get("breaks", 0))
            if bad:
                verdict, vcol = (f"Form broke ×{bad}", (255, 200, 87, 240))
        elif log:
            last = log[-1]
            verdict, vcol = (("Clean rep ✓", (92, 255, 176, 240)) if last[0] else ((last[2] or "Check form"), (255, 200, 87, 240)))
        else:
            verdict, vcol = ("Waiting for the first rep", (170, 185, 215, 230))
        self._t(p, 12, 166, W - 24, 22, str(verdict)[:26], 11, QFont.Weight.Bold, vcol, A.AlignCenter)
        p.setPen(QPen(QColor(255, 255, 255, 26), 1))
        p.drawLine(QPointF(16, 196), QPointF(W - 16, 196))
        # one bar per rep
        self._t(p, 18, 202, W - 36, 16, "EACH REP", 8, QFont.Weight.Bold, (150, 165, 200, 230), spacing=3)
        if log:
            secs = [x[1] for x in log]
            top = max(max(secs), 1.0)
            n = len(log)
            bw = min(10.0, (W - 36) / max(n, 1) - 3)
            x0 = 18
            p.setPen(Qt.PenStyle.NoPen)
            for i, (good, sec, _issue) in enumerate(log):
                h = 12 + 50 * (sec / top)
                p.setBrush(QColor(92, 255, 176, 230) if good else QColor(255, 200, 87, 230))
                p.drawRoundedRect(QRectF(x0 + i * (bw + 3), 286 - h, bw, h), 3, 3)
            avg = sum(secs) / len(secs)
            self._t(p, 14, 292, W - 28, 30, f"last {secs[-1]:.1f}s  ·  avg {avg:.1f}s", 9, QFont.Weight.DemiBold, (200, 212, 235, 230), A.AlignCenter)
        else:
            self._t(p, 14, 240, W - 28, 40, "Bars appear after your first rep", 9, color=(170, 185, 215, 220), align=A.AlignCenter)


class CelebrationFX(QWidget):
    """A new personal record: gold confetti, expanding rings and a big "NEW RECORD · 32" that pops in and fades out (~5 s).

    One QVariantAnimation drives everything and every particle position is a pure function of time, so there is no per-particle
    state to update — it is a single repaint per frame. Mouse-transparent; hides itself when finished."""

    DURATION = 5200
    COLORS = ((255, 214, 102), (255, 160, 80), (92, 255, 176), (120, 200, 255), (255, 120, 170), (255, 245, 200))

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.title, self.big, self.sub = "NEW RECORD", "", ""
        self._t = 1.0
        self._parts = []
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setDuration(self.DURATION)
        self._anim.valueChanged.connect(self._tick)
        self._anim.finished.connect(self.hide)
        self.hide()

    def _tick(self, v):
        self._t = float(v)
        self.update()

    def play(self, title: str, big: str, sub: str = ""):
        import random
        rnd = random.Random(int(time.time() * 1000) & 0xFFFF)
        self.title, self.big, self.sub = str(title), str(big), str(sub)
        # (x 0..1, start delay, fall speed, sway, phase, size, spin, colour)
        self._parts = [(rnd.random(), rnd.random() * 0.35, 0.55 + rnd.random() * 0.7, 0.02 + rnd.random() * 0.05,
                        rnd.random() * 6.28, 7 + rnd.random() * 9, (rnd.random() - 0.5) * 14,
                        self.COLORS[rnd.randrange(len(self.COLORS))]) for _ in range(110)]
        p = self.parentWidget()
        if p is not None:
            self.setGeometry(0, 0, p.width(), p.height())
        self._anim.stop()
        self._t = 0.0
        self.show()
        self.raise_()
        self._anim.start()

    def paintEvent(self, _):
        t = self._t
        if t >= 1.0 or not self.isVisible():
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        W, H = self.width(), self.height()
        k = max(0.7, min(1.6, H / 1080.0))
        fade = 1.0 if t < 0.8 else max(0.0, (1.0 - t) / 0.2)
        enter = min(1.0, t / 0.12)                                     # 0 → 1 over the first ~0.6 s
        back = 1.0 + 2.7 * (enter - 1) ** 3 + 1.7 * (enter - 1) ** 2   # OutBack: overshoots a little, then settles
        # dim the picture a little so the text reads
        p.fillRect(self.rect(), QColor(0, 0, 0, int(95 * fade * min(1.0, t / 0.08))))
        cx, cy = W / 2, H * 0.42
        # glow + rings
        gr = 330 * k
        g = QRadialGradient(cx, cy, gr)
        g.setColorAt(0.0, QColor(255, 214, 102, int(120 * fade)))
        g.setColorAt(1.0, QColor(255, 214, 102, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawEllipse(QPointF(cx, cy), gr, gr)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for i in range(3):
            tt = (t * 1.6 - i * 0.18)
            if 0 < tt < 1:
                p.setPen(QPen(QColor(255, 226, 140, int(210 * (1 - tt) * fade)), 4 * k))
                r = (120 + 520 * tt) * k
                p.drawEllipse(QPointF(cx, cy), r, r)
        # confetti
        p.setPen(Qt.PenStyle.NoPen)
        for x0, delay, speed, sway, ph, size, spin, col in self._parts:
            tt = (t - delay * 0.5) / 0.85
            if tt <= 0 or tt >= 1:
                continue
            x = (x0 + math.sin(ph + tt * 9) * sway) * W
            y = (-0.06 + tt * speed * 1.25) * H
            if y > H + 20:
                continue
            p.save()
            p.translate(x, y)
            p.rotate(spin * tt * 60 + ph * 20)
            flip = abs(math.cos(ph + tt * 10))                         # a spinning chip: its width follows a cosine
            p.setBrush(QColor(col[0], col[1], col[2], int(235 * fade)))
            s = size * k
            p.drawRect(QRectF(-s / 2, -s / 4, s * (0.25 + 0.75 * flip), s / 2))
            p.restore()
        # the text
        p.save()
        p.translate(cx, cy)
        p.scale(back, back)
        tf = font(16, QFont.Weight.Black)
        tf.setPixelSize(int(46 * k))
        tf.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 10 * k)
        p.setFont(tf)
        p.setPen(QColor(255, 255, 255, int(235 * fade)))
        p.drawText(QRectF(-W / 2, -250 * k, W, 70 * k), Qt.AlignmentFlag.AlignCenter, "🏆  " + self.title)
        bf = font(10, QFont.Weight.Black)
        bf.setPixelSize(int(260 * k))
        p.setFont(bf)
        p.setPen(QColor(0, 0, 0, int(140 * fade)))
        p.drawText(QRectF(-W / 2 + 6 * k, -170 * k + 8 * k, W, 300 * k), Qt.AlignmentFlag.AlignCenter, self.big)
        shimmer = 0.5 + 0.5 * math.sin(t * 22)                         # a slow gold shimmer on the number
        p.setPen(QColor(255, int(205 + 40 * shimmer), int(90 + 90 * shimmer), int(255 * fade)))
        p.drawText(QRectF(-W / 2, -170 * k, W, 300 * k), Qt.AlignmentFlag.AlignCenter, self.big)
        if self.sub:
            sf = font(13, QFont.Weight.DemiBold)
            sf.setPixelSize(int(26 * k))
            sf.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 4 * k)
            p.setFont(sf)
            p.setPen(QColor(235, 240, 250, int(225 * fade)))
            p.drawText(QRectF(-W / 2, 150 * k, W, 50 * k), Qt.AlignmentFlag.AlignCenter, self.sub)
        p.restore()


class MiniJarvis(QWidget):
    """A very light Jarvis for the corner of the full-screen camera: no OpenGL, no pixmaps — a handful of arcs and lines.

    The timer only runs while the widget is visible (full-screen camera) and slows down when Jarvis is idle, so it costs
    next to nothing next to the camera picture itself. The animation follows Jarvis's state and the loudness of his voice."""

    W, H = 212, 245                                   # a 170 x 196 canvas scaled by W / 170
    _COL = {"SPEAKING": "ACC2", "LISTENING": "GREEN", "THINKING": "AMBER", "PROCESSING": "AMBER",
            "SLEEPING": "TEXT_DIM", "MUTED": "RED"}

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFixedSize(self.W, self.H)
        self.state = "LISTENING"
        self._target = 0.0
        self._lvl = 0.0
        self._t0 = time.monotonic()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self.hide()

    def push(self, level: float):
        if level > self._target:
            self._target = level

    def activate(self, on: bool):
        if on:
            self.place()
            self.show()
            self.raise_()
            self._timer.start(33)
        else:
            self._timer.stop()
            self.hide()

    def place(self):
        p = self.parentWidget()
        if p is not None:
            self.move(p.width() - self.W - 18, p.height() - self.H - 16)

    def _tick(self):
        self._lvl += (self._target - self._lvl) * 0.4
        self._target *= 0.78
        busy = self.state in ("SPEAKING", "THINKING", "PROCESSING") or self._lvl > 0.02
        self._timer.setInterval(33 if busy else 80)      # ~30 fps while talking, ~12 fps while idle
        self.update()

    def paintEvent(self, _):
        t = time.monotonic() - self._t0
        st = self.state
        lvl = min(1.0, self._lvl * 1.6) if st == "SPEAKING" else self._lvl * 0.5
        col = qcol(getattr(C, self._COL.get(st, "PRI")))
        r_, g_, b_ = col.red(), col.green(), col.blue()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        k = self.W / 170.0
        p.scale(k, k)                                        # everything below is drawn on a 170 x 196 canvas
        cx, cy = 85.0, 84.0
        # dark glass disc (slightly lighter towards the rim) so the HUD stays readable on any camera picture
        dg = QRadialGradient(cx, cy, 76)
        dg.setColorAt(0.0, QColor(8, 14, 26, 175))
        dg.setColorAt(0.85, QColor(8, 14, 26, 150))
        dg.setColorAt(1.0, QColor(r_, g_, b_, 70))
        p.setPen(QPen(QColor(r_, g_, b_, 90), 1.2))
        p.setBrush(QBrush(dg))
        p.drawEllipse(QPointF(cx, cy), 74, 74)
        # sound ripples travelling outwards while Jarvis talks
        if st == "SPEAKING" or lvl > 0.05:
            p.setBrush(Qt.BrushStyle.NoBrush)
            for i in range(2):
                ph = (t * 0.9 + i * 0.5) % 1.0
                p.setPen(QPen(QColor(r_, g_, b_, int(120 * (1 - ph) * min(1.0, 0.4 + lvl))), 1.6))
                rr = 62 + 22 * ph
                p.drawEllipse(QPointF(cx, cy), rr, rr)
        # glow that breathes with the voice
        g = QRadialGradient(cx, cy, 70)
        g.setColorAt(0.0, QColor(r_, g_, b_, int(70 + 150 * lvl)))
        g.setColorAt(0.6, QColor(r_, g_, b_, int(18 + 40 * lvl)))
        g.setColorAt(1.0, QColor(r_, g_, b_, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawEllipse(QPointF(cx, cy), 70, 70)
        # fine tick ring (slowly turning), like a dial
        spin = 220 if st in ("THINKING", "PROCESSING") else (90 if st == "SPEAKING" else 34)
        p.save()
        p.translate(cx, cy)
        p.rotate(t * spin * 0.25)
        for i in range(48):
            a = i * math.tau / 48
            long_ = i % 4 == 0
            r0, r1 = (63.5, 68.5) if long_ else (65.5, 68.0)
            p.setPen(QPen(QColor(r_, g_, b_, 190 if long_ else 90), 1.4 if long_ else 1.0))
            p.drawLine(QPointF(math.cos(a) * r0, math.sin(a) * r0), QPointF(math.cos(a) * r1, math.sin(a) * r1))
        p.restore()
        # core
        rc = 15 + 13 * lvl + (1.5 * (1 + math.sin(t * 2.4)) if st != "SPEAKING" else 0)
        cg = QRadialGradient(cx, cy, rc)
        cg.setColorAt(0.0, QColor(255, 255, 255, 240))
        cg.setColorAt(0.45, QColor(r_, g_, b_, 235))
        cg.setColorAt(1.0, QColor(r_, g_, b_, 40))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(cg))
        p.drawEllipse(QPointF(cx, cy), rc, rc)
        # rings
        p.setBrush(Qt.BrushStyle.NoBrush)
        pen = QPen(QColor(r_, g_, b_, 215), 2.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        a0 = int((t * spin) % 360 * 16)
        rect = QRectF(cx - 40, cy - 40, 80, 80)
        for q in range(3):
            p.drawArc(rect, a0 + q * 120 * 16, 62 * 16)
        pen.setWidthF(1.6)
        pen.setColor(QColor(r_, g_, b_, 150))
        p.setPen(pen)
        a1 = int((-t * spin * 0.6) % 360 * 16)
        rect2 = QRectF(cx - 52, cy - 52, 104, 104)
        for q in range(2):
            p.drawArc(rect2, a1 + q * 180 * 16, 100 * 16)
        # two small satellites on the outer ring
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 235))
        for q in range(2):
            a = math.radians(-t * spin * 0.45 + q * 180)
            p.drawEllipse(QPointF(cx + math.cos(a) * 52, cy + math.sin(a) * 52), 2.3, 2.3)
        # voice bars
        if st == "SPEAKING" or lvl > 0.04:
            pen = QPen(QColor(r_, g_, b_, 235), 2.2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            n = 32
            for i in range(n):
                a = i * 2 * math.pi / n
                wob = 0.35 + 0.65 * abs(math.sin(i * 1.9 + t * 11) * math.cos(i * 0.7 - t * 6))
                ln = 3 + 17 * lvl * wob
                c, s_ = math.cos(a), math.sin(a)
                p.drawLine(QPointF(cx + c * 58, cy + s_ * 58), QPointF(cx + c * (58 + ln), cy + s_ * (58 + ln)))
        # status pill: a pulsing dot + the state
        f = font(8, QFont.Weight.Bold)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2)
        p.setFont(f)
        txt = f"JARVIS · {st}"
        tw = p.fontMetrics().horizontalAdvance(txt) + 34
        pill = QRectF((170 - tw) / 2, 166, tw, 22)
        p.setPen(QPen(QColor(r_, g_, b_, 110), 1.0))
        p.setBrush(QColor(6, 10, 18, 190))
        p.drawRoundedRect(pill, 11, 11)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(r_, g_, b_, int(150 + 100 * (0.5 + 0.5 * math.sin(t * 4)))))
        p.drawEllipse(QPointF(pill.left() + 12, pill.center().y()), 3.2, 3.2)
        p.setPen(QColor(r_, g_, b_, 235))
        p.drawText(QRectF(pill.left() + 20, pill.top(), pill.width() - 24, pill.height()), Qt.AlignmentFlag.AlignCenter, txt)


class StageHost(QWidget):

    def __init__(self, stage: Stage3D, parent=None):
        super().__init__(parent)
        self.stage = stage
        stage.setParent(self)
        self.floats: dict[str, QWidget] = {}
        self.media_full = False
        self.cam_full = False                           # the live camera covers the whole window
        self.float_parent: QWidget | None = None      # the floating panels (toast, alert, video…) are children of this widget …
        self.pages_ref: QStackedWidget | None = None   # … so they show on EVERY page, not only on Home
        self.on_layout = None

    def resizeEvent(self, e):
        self.stage.setGeometry(0, 0, self.width(), self.height())
        self.layout_floats()
        super().resizeEvent(e)

    def area(self) -> QRect:
        """Where the floating panels live, in the float parent's coordinates: the stage on Home, the whole page area elsewhere."""
        fp, pg = self.float_parent, self.pages_ref
        if fp is None:
            return QRect(0, 0, self.width(), self.height())
        if pg is None or pg.currentIndex() == 0:
            return QRect(self.mapTo(fp, QPoint(0, 0)), self.size())
        return QRect(pg.mapTo(fp, QPoint(0, 0)), pg.size())

    def layout_floats(self):
        a = self.area()
        X, Y, W, H = a.x(), a.y(), a.width(), a.height()
        c = self.floats.get("content")
        if c is not None and c.isVisible():
            h = int(min(320, H * 0.5))
            c.setGeometry(X + 18, Y + H - h - 10, W - 36, h)
        m = self.floats.get("media")
        if m is not None and m.isVisible():
            if self.cam_full and self.float_parent is not None:
                m.setGeometry(0, 0, self.float_parent.width(), self.float_parent.height())
            elif self.media_full:
                m.setGeometry(X, Y, W, H)
            else:
                media_w = min(960, max(360, min(int(W * 0.66), W - 28)))
                extra = getattr(self, "media_extra", 0)
                if int(media_w * 9 / 16) + 58 + extra > H - 24:      # tall panel (suggestions row): shrink the video, keep 16:9
                    media_w = max(360, min(media_w, int((H - 24 - 58 - extra) * 16 / 9)))
                media_h = max(220, min(H - 24, int(media_w * 9 / 16) + 58 + extra))
                m.setGeometry(X + max(12, W - media_w - 12), Y + 12, media_w, media_h)
        k = self.floats.get("clip")
        if k is not None and k.isVisible():
            k.setGeometry(X + int((W - min(W - 40, 620)) / 2), Y + 12, min(W - 40, 620), 96)
        t = self.floats.get("toast")
        if t is not None and t.isVisible():
            t.adjustSize()
            t.move(X + int((W - t.width()) / 2), Y + 14)
        al = self.floats.get("alert")
        if al is not None and al.isVisible():
            al.adjustSize()
            al.move(X + int((W - al.width()) / 2), Y + int(H * 0.36))
        cb = self.on_layout
        if cb is not None:
            cb()                                           # the mpv window follows the panel at once, not on the next timer tick


class _PageVeil(QWidget):
    """A page change fades in: a dark veil in the background colour over the page area that dissolves in ~200 ms.
    (No QGraphicsEffect: the page is not re-rendered into a buffer, only this one rectangle is painted.)"""

    def __init__(self, page_area: QWidget):
        super().__init__(page_area)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._a = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        self._anim.setDuration(T.D_BASE)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on)
        self._anim.finished.connect(self.hide)
        self.hide()

    def _on(self, v):
        self._a = float(v)
        self.update()

    def play(self):
        p = self.parentWidget()
        if p is None or not p.isVisible():
            return
        self.setGeometry(0, 0, p.width(), p.height())
        self._anim.stop()
        self._a = 0.55
        self.show()
        self.raise_()
        self._anim.setStartValue(0.55)
        self._anim.start()

    def paintEvent(self, _):
        if self._a <= 0.01:
            return
        p = QPainter(self)
        c = QColor(C.BG)
        c.setAlpha(int(255 * self._a))
        p.fillRect(self.rect(), c)


class _PageWatch(QObject):
    """Tells the floating panels when the page area moves or resizes (Home's stage is hidden on other pages and gets no resize)."""

    def __init__(self, host: StageHost):
        super().__init__(host)
        self.host = host

    def eventFilter(self, obj, ev):
        if ev.type() in (QEvent.Type.Resize, QEvent.Type.Move):
            self.host.layout_floats()
        return False


class FrameLabel(QLabel):
    """The HUD picture (camera, phone screen, video frames), painted straight from the QImage.

    Before, every frame was turned into a QPixmap and scaled into a second pixmap, and — because the label was see-through —
    the glass card behind it was repainted too. Now the frame is drawn once, scaled by the painter, onto an opaque black
    background, so only this label repaints."""

    def __init__(self):
        super().__init__()
        self._img: QImage | None = None
        self._smooth = True
        self.cover = False                       # True: fill the whole label (crop the overflow) instead of letterboxing
        self.whole = False                       # True (workouts): always show the WHOLE picture, the sides filled with a blurred copy
        self.shade = False                       # True (full-screen camera): soft shading at the top and bottom, viewfinder corners, LIVE badge
        self._fps, self._fn, self._ft = 0.0, 0, time.monotonic()
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

    def set_frame(self, img: QImage, smooth: bool = True, whole: bool = False):
        self._img, self._smooth, self.whole = img, smooth, whole
        self._fn += 1
        now = time.monotonic()
        if now - self._ft >= 0.6:
            self._fps, self._fn, self._ft = self._fn / (now - self._ft), 0, now
        self.update()

    def _chrome(self, p: QPainter, W: int, H: int, iw: int, ih: int):
        """The full-screen camera dressing: soft shade top and bottom (so the HUD and captions read on any picture), thin
        viewfinder corners, and a small LIVE badge with the real resolution and frame rate. A few cheap strokes per frame."""
        k = max(0.75, min(1.6, H / 1080.0))
        top = QLinearGradient(0, 0, 0, 120 * k)
        top.setColorAt(0.0, QColor(0, 0, 0, 150))
        top.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(QRectF(0, 0, W, 120 * k), QBrush(top))
        bot = QLinearGradient(0, H - 190 * k, 0, H)
        bot.setColorAt(0.0, QColor(0, 0, 0, 0))
        bot.setColorAt(1.0, QColor(0, 0, 0, 175))
        p.fillRect(QRectF(0, H - 190 * k, W, 190 * k), QBrush(bot))
        # viewfinder corners
        col = qcol(C.PRI, 170)
        pen = QPen(col, 2.2 * k)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        m, L = 16 * k, 34 * k
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for x, y, dx, dy in ((m, m, 1, 1), (W - m, m, -1, 1), (m, H - m, 1, -1), (W - m, H - m, -1, -1)):
            p.drawLine(QPointF(x, y), QPointF(x + dx * L, y))
            p.drawLine(QPointF(x, y), QPointF(x, y + dy * L))
        if self.whole:
            return                                # workouts: the HUD owns the top-left corner
        # LIVE badge (top-left): red dot, resolution, fps
        f = font(9, QFont.Weight.DemiBold)
        p.setFont(f)
        txt = f"LIVE  ·  {iw}×{ih}" + (f"  ·  {self._fps:.0f} fps" if self._fps > 0 else "")
        tw = p.fontMetrics().horizontalAdvance(txt) + 40 * k
        r = QRectF(32 * k, 30 * k, tw, 28 * k)
        p.setPen(QPen(QColor(255, 255, 255, 40), 1))
        p.setBrush(QColor(8, 12, 22, 190))
        p.drawRoundedRect(r, 14 * k, 14 * k)
        pulse = 0.5 + 0.5 * math.sin(time.monotonic() * 4)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 80, 90, int(150 + 105 * pulse)))
        p.drawEllipse(QPointF(r.left() + 15 * k, r.center().y()), 4.5 * k, 4.5 * k)
        p.setPen(QColor(235, 240, 250, 235))
        p.drawText(QRectF(r.left() + 26 * k, r.top(), r.width() - 30 * k, r.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, txt)

    def clear(self):
        self._img = None
        super().clear()
        self.update()

    def setPixmap(self, pm):
        self._img = None
        super().setPixmap(pm)

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0))
        img = self._img
        if img is None or img.isNull():
            super().paintEvent(e)
            return
        W, H = self.width(), self.height()
        iw, ih = img.width(), img.height()
        if self.whole:
            # workouts need the whole body in view, so nothing is cropped: the picture is shown complete and the space left
            # at the sides is filled with a dark, blurred copy of the same frame (a tiny copy scaled up = a cheap blur)
            tiny = img.scaled(48, max(1, int(48 * ih / iw)), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
            k0 = max(W / tiny.width(), H / tiny.height())
            tw, th = tiny.width() * k0, tiny.height() * k0
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            p.drawImage(QRectF((W - tw) / 2, (H - th) / 2, tw, th), tiny)
            p.fillRect(self.rect(), QColor(0, 0, 0, 140))
            k = min(W / iw, H / ih)
            w, h = iw * k, ih * k
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, self._smooth and k < 1.6)
            p.drawImage(QRectF((W - w) / 2, (H - h) / 2, w, h), img)
            if self.shade:
                self._chrome(p, W, H, iw, ih)
            return
        if self.cover:
            k = max(W / iw, H / ih)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, self._smooth and k < 1.6)
            sw, sh = W / k, H / k                # the part of the picture that fits the label
            p.drawImage(QRectF(0, 0, W, H), img, QRectF((iw - sw) / 2, (ih - sh) / 2, sw, sh))
            if self.shade:
                self._chrome(p, W, H, iw, ih)
            return
        k = min(W / iw, H / ih)
        w, h = iw * k, ih * k
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, self._smooth and k < 1.6)
        p.drawImage(QRectF((W - w) / 2, (H - h) / 2, w, h), img)


class SeekSlider(QSlider):
    """Seek bar for the HUD video: a click or drag jumps straight to that position (a plain QSlider only pages).
    Painted by hand: a slim rounded track with a cyan→violet fill and a glowing knob that grows on hover."""
    seek_to = pyqtSignal(int)
    PAD = 8

    def __init__(self):
        super().__init__(Qt.Orientation.Horizontal)
        self.setRange(0, 1000)
        self.setFixedHeight(22)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.dragging = False

    def _val(self, x) -> int:
        span = max(1, self.width() - 2 * self.PAD)
        x = max(0, min(int(x) - self.PAD, span))
        return QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), x, span)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        span = max(1, w - 2 * self.PAD)
        hot = self.dragging or self.underMouse()
        th = 7.0 if hot else 5.0
        cy = h / 2
        frac = (self.value() - self.minimum()) / max(1, self.maximum() - self.minimum())
        x_knob = self.PAD + frac * span
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 40))
        p.drawRoundedRect(QRectF(self.PAD, cy - th / 2, span, th), th / 2, th / 2)
        if frac > 0:
            g = QLinearGradient(self.PAD, 0, self.PAD + span, 0)
            g.setColorAt(0, QColor(C.PRI))
            g.setColorAt(1, QColor(C.ACC))
            p.setBrush(g)
            p.drawRoundedRect(QRectF(self.PAD, cy - th / 2, max(th, x_knob - self.PAD), th), th / 2, th / 2)
        r = 8.0 if hot else 6.0
        halo = QColor(C.PRI)
        halo.setAlpha(70 if hot else 0)
        p.setBrush(halo)
        p.drawEllipse(QPointF(x_knob, cy), r + 4, r + 4)
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QPointF(x_knob, cy), r, r)
        p.end()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.dragging = True
            self.setValue(self._val(e.position().x()))
            e.accept()
        else:
            super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self.dragging:
            self.setValue(self._val(e.position().x()))
            e.accept()
        else:
            super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self.dragging and e.button() == Qt.MouseButton.LeftButton:
            self.dragging = False
            self.seek_to.emit(self._val(e.position().x()))
            e.accept()
        else:
            super().mouseReleaseEvent(e)


def _fmt_clock(sec: float) -> str:
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _video_icon_shapes(kind: str, cx: float, cy: float, s: float) -> list:
    """Vector shapes (button pixels) of the YouTube-style player icons:
    ("poly", [(x, y), ...]) or ("rect", x, y, w, h, radius). Drawn white by VideoIconButton."""
    if kind == "play":
        return [("poly", [(cx - .38 * s, cy - .52 * s), (cx - .38 * s, cy + .52 * s), (cx + .56 * s, cy)])]
    if kind == "pause":
        w, g = .30 * s, .22 * s
        return [("rect", cx - g / 2 - w, cy - .5 * s, w, s, w * .3), ("rect", cx + g / 2, cy - .5 * s, w, s, w * .3)]
    if kind == "stop":
        return [("rect", cx - .45 * s, cy - .45 * s, .9 * s, .9 * s, .18 * s)]
    tw, th = .52 * s, .5 * s  # "back" / "fwd": two triangles side by side
    out = []
    for x0 in (cx - tw, cx):
        if kind == "fwd":
            out.append(("poly", [(x0, cy - th), (x0, cy + th), (x0 + tw, cy)]))
        else:
            out.append(("poly", [(x0 + tw, cy - th), (x0 + tw, cy + th), (x0, cy)]))
    return out


class VideoIconButton(QPushButton):
    """YouTube-style player button: dark rounded rectangle with a white play / pause / stop / rewind / forward icon
    (rewind / forward also show a small label such as "5m"). Never takes keyboard focus."""

    def __init__(self, kind: str, label: str = "", tip: str = "", width: int = 46):
        super().__init__()
        self.kind = kind
        self.label = label
        self.setFixedSize(width, 30)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # never steal Space / arrows from the video
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        if tip:
            self.setToolTip(tip)

    def set_kind(self, kind: str, tip: str = ""):
        if kind != self.kind:
            self.kind = kind
            self.update()
        if tip and tip != self.toolTip():
            self.setToolTip(tip)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(.5, .5, -.5, -.5)
        if self.isDown():
            bg = QColor(122, 122, 130)
        elif self.underMouse():
            bg = QColor(98, 98, 106)
        else:
            bg = QColor(66, 66, 72)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(r, 9, 9)
        white = QColor(255, 255, 255)
        w, h = r.width(), r.height()
        icx, cy = w / 2, h / 2
        if self.label:
            icx = w * (.30 if self.kind == "back" else .70)
            f = QFont(self.font())
            f.setPixelSize(11)
            f.setBold(True)
            p.setFont(f)
            p.setPen(white)
            tr = QRectF(w * .50, 0, w * .44, h) if self.kind == "back" else QRectF(w * .06, 0, w * .44, h)
            p.drawText(tr, Qt.AlignmentFlag.AlignCenter, self.label)
        p.setBrush(white)
        p.setPen(QPen(white, 1.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        for sh in _video_icon_shapes(self.kind, icx, cy, 12.0):
            if sh[0] == "poly":
                p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in sh[1]]))
            else:
                _, x, y, rw, rh, rad = sh
                p.drawRoundedRect(QRectF(x, y, rw, rh), rad, rad)
        p.end()


class VideoBar(QWidget):
    """The player's own control bar: seek bar on top; below it time · rewind / play / forward / stop · 👍 👎 🔔 ·
    Videos · settings. Used under the video in the media panel and, in full screen, inside the floating FsOverlay."""
    ctl = pyqtSignal(str, object)     # ("pause", None) | ("seek", seconds)
    seek_fraction = pyqtSignal(int)   # 0..1000 of the whole video
    stop = pyqtSignal()
    action = pyqtSignal(str)          # like | dislike | sub | videos | settings
    HEIGHT = 76

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(self.HEIGHT)
        self.setStyleSheet("background: transparent;")
        self._hold_until = 0.0
        bl = QVBoxLayout(self)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(2)
        self.slider = SeekSlider()
        self.slider.seek_to.connect(self._on_seek)
        bl.addWidget(self.slider)

        row = QHBoxLayout()
        row.setContentsMargins(4, 0, 4, 0)
        row.setSpacing(4)
        self.time_lbl = QLabel("0:00 / --:--")
        self.time_lbl.setMinimumWidth(96)
        self.time_lbl.setStyleSheet(f"color: {C.TEXT}; font-size: 12px; font-weight: 600; background: transparent;")
        row.addWidget(self.time_lbl)
        row.addStretch()

        self.back_btn = PlayerButton("rewind", "Back 5 minutes (Shift+Left)", text="5m", size=32, icon_px=16)
        self.back_btn.clicked.connect(lambda: self.ctl.emit("seek", -300))
        self.play_btn = PlayButton(44)
        self.play_btn.setToolTip("Pause (Space)")
        self.play_btn.clicked.connect(lambda: self.ctl.emit("pause", None))
        self.fwd_btn = PlayerButton("forward", "Forward 5 minutes (Shift+Right)", text="5m", size=32, icon_px=16)
        self.fwd_btn.clicked.connect(lambda: self.ctl.emit("seek", 300))
        self.stop_btn = PlayerButton("stop", "Stop and close the video", size=32, icon_px=14, color=C.RED)
        self.stop_btn.clicked.connect(lambda: self.stop.emit())
        for b_ in (self.back_btn, self.play_btn, self.fwd_btn, self.stop_btn):
            row.addWidget(b_)
        row.addStretch()

        self._chips: dict[str, PlayerButton] = {}
        self._sep = QFrame()
        self._sep.setFixedSize(1, 18)
        self._sep.setStyleSheet("background: rgba(255,255,255,0.14);")
        for key, icon, tip, text, col in (
                ("like", "thumb_up", "Like on YouTube", "", C.PRI),
                ("dislike", "thumb_down", "Dislike on YouTube", "", C.RED),
                ("sub", "bell", "Subscribe to the channel", "", C.GREEN),
                ("videos", "grid", "Suggested videos — pick the next one", "Videos", C.PRI),
                ("settings", "settings", "Player settings: quality, speed, volume…", "", C.PRI)):
            b = PlayerButton(icon, tip, text=text, size=32, icon_px=17, color=col)
            b.clicked.connect(lambda _=False, k=key: self.action.emit(k))
            if key == "videos":
                row.addWidget(self._sep, 0, Qt.AlignmentFlag.AlignVCenter)
            row.addWidget(b)
            self._chips[key] = b
        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet(f"color: {C.ACC}; font-size: 10px; font-weight: 700; background: transparent;")
        row.addWidget(self.status_lbl)
        bl.addLayout(row)

    def set_youtube(self, on: bool):
        """👍 👎 🔔 and Videos only make sense for YouTube videos; settings is always there."""
        for k in ("like", "dislike", "sub", "videos"):
            self._chips[k].setVisible(bool(on))
        self._sep.setVisible(bool(on))

    def set_state(self, kind: str, on: bool):
        b = self._chips.get(kind)
        if b is None:
            return
        b.set_active(on)
        if kind == "sub":
            b.set_text("Subscribed" if on else "")
        if kind == "like" and on:
            self.set_state("dislike", False)
        if kind == "dislike" and on:
            self.set_state("like", False)

    def settings_anchor(self) -> QPoint:
        b = self._chips["settings"]
        return b.mapToGlobal(QPoint(b.width(), 0))

    def _on_seek(self, v: int):
        self._hold_until = time.monotonic() + 1.5  # keep the bar where the user dropped it until mpv has sought
        self.seek_fraction.emit(v)

    def reset(self):
        self.slider.setValue(0)
        self.time_lbl.setText("0:00 / --:--")
        self.status_lbl.setText("")
        self.play_btn.set_kind("pause", "Pause (Space)")

    def refresh(self, st: dict):
        pos, dur = st["pos"], st["dur"]
        sl = self.slider
        if sl.dragging and dur > 0:
            pos = dur * sl.value() / 1000.0
        elif dur > 0 and time.monotonic() >= self._hold_until:
            sl.setValue(int(max(0.0, min(1.0, pos / dur)) * 1000))
        self.time_lbl.setText(f"{_fmt_clock(pos)} / {_fmt_clock(dur) if dur > 0 else '--:--'}")
        if st["paused"] and not st["buffering"]:
            self.play_btn.set_kind("play", "Play (Space)")
        else:
            self.play_btn.set_kind("pause", "Pause (Space)")
        if st["buffering"]:
            status = "BUFFERING…"
        else:
            parts = []
            if st["cache"] >= 1:
                parts.append(f"buffer {st['cache']:.0f}s")
            if st["drops"] > 0:
                parts.append(f"dropped {st['drops']}")
            status = "  ·  ".join(parts)
        self.status_lbl.setText(status)


class FsOverlay(QWidget):
    """Floating control bar over the full-screen mpv window. Shown while the mouse moves (or the video is
    paused / buffering) and hidden a few seconds later; it never takes keyboard focus."""

    def __init__(self, owner):
        super().__init__(owner, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setObjectName("fsOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowOpacity(0.97)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 10, 24, 8)
        self.bar = VideoBar()
        lay.addWidget(self.bar)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        g = QLinearGradient(0, r.top(), 0, r.bottom())
        g.setColorAt(0, QColor(16, 22, 42, 238))
        g.setColorAt(1, QColor(7, 10, 20, 244))
        p.setBrush(g)
        edge = QColor(C.PRI)
        edge.setAlpha(80)
        p.setPen(QPen(edge, 1))
        p.drawRoundedRect(r, 22, 22)
        p.end()


class MpvWindow(QWidget):
    """Frameless native window that mpv renders into. It floats over the HUD media panel
    (the same window goes full screen, so playback never restarts)."""
    want_toggle = pyqtSignal()
    key_handler = None  # set by the HUD: handles Space / arrows (see MainWindow._video_key)

    def __init__(self, owner):
        super().__init__(owner, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setStyleSheet("background: #000;")
        self.setWindowTitle("Jarvis video")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def keyPressEvent(self, e):
        k = e.key()
        if k in (Qt.Key.Key_F, Qt.Key.Key_F11) or (k == Qt.Key.Key_Escape and self.isFullScreen()):
            self.want_toggle.emit()
        elif self.key_handler is not None and self.key_handler(e):
            pass
        else:
            super().keyPressEvent(e)

    def mouseDoubleClickEvent(self, e):
        self.want_toggle.emit()


class MainWindow(QMainWindow):
    _log_sig = pyqtSignal(str)
    _state_sig = pyqtSignal(str)
    _content_sig = pyqtSignal(str, str)
    _reconfig_sig = pyqtSignal()
    _cam_stream_sig = pyqtSignal(bool)
    _cam_frame_sig = pyqtSignal(bytes)
    _cam_img_sig = pyqtSignal(object)
    _cam_full_sig = pyqtSignal(bool)
    _pushup_sig = pyqtSignal(object)
    _pushup_full_sig = pyqtSignal(bool)
    _celebrate_sig = pyqtSignal(str, str, str)
    _phone_frame_sig = pyqtSignal(str)
    _media_frame_sig = pyqtSignal(bytes, str)
    _mpv_open_sig = pyqtSignal(object)
    _mpv_close_sig = pyqtSignal()
    _mpv_cmd_sig = pyqtSignal(str)
    _confirm_sig = pyqtSignal(str, str)
    _confirm_hide_sig = pyqtSignal()
    _wake_dl_sig = pyqtSignal(bool, str)
    _update_sig = pyqtSignal(object)
    _toast_sig = pyqtSignal(str)
    _phone_event_sig = pyqtSignal(object)
    _profile_sig = pyqtSignal(str)
    _tasks_sig = pyqtSignal()
    _update_done_sig = pyqtSignal(object)
    _restart_sig = pyqtSignal()
    _quit_sig = pyqtSignal()
    _gesture_sig = pyqtSignal(str, str)
    _gesture_status_sig = pyqtSignal(str)
    _face_sig = pyqtSignal(str, str)
    _media_info_sig = pyqtSignal(object)
    _media_sugg_sig = pyqtSignal(object)
    _vx_state_sig = pyqtSignal(str, bool)
    _cursor_sig = pyqtSignal(int, int, str)
    _cursor_panic_sig = pyqtSignal()
    _hand_sig = pyqtSignal(object, float)
    _space_sig = pyqtSignal(object)
    _module_sig = pyqtSignal(str, str)
    _face_seen_sig = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        cfg = read_cfg()
        self.assistant_name = (cfg.get("assistant_name") or "JARVIS").strip() or "JARVIS"
        self.user_name = cfg.get("user_name") or ""
        col = (cfg.get("ui_color") or "").strip()
        theme = (cfg.get("ui_theme") or "").strip().lower()
        self._auto_period = None                              # "day" / "night" last applied by the automatic theme
        if cfg.get("ui_theme_auto") and auto_theme_key(theme or "jarvis") == DAY_THEME:
            apply_theme(DAY_THEME)                            # automatic day / night: it is daytime
            self._auto_period = "day"
        elif theme and apply_theme(theme):
            if cfg.get("ui_theme_auto"):
                self._auto_period = "night"                   # a ready-made theme (Settings → Appearance)
        elif col and col.lower() != DEFAULT_ACCENT:
            apply_accent(col)
            if cfg.get("ui_theme_auto"):
                self._auto_period = "night"
        elif cfg.get("ui_theme_auto"):
            self._auto_period = "night"
        self.setStyleSheet(build_qss())
        self.setWindowTitle(self.assistant_name.upper())
        self.setMinimumSize(960, 640)
        self.resize(1200, 780)
        self.setAcceptDrops(True)
        scr = QApplication.primaryScreen().availableGeometry()
        self.move((scr.width() - 1200) // 2, max(0, (scr.height() - 780) // 2))

        self.on_text_command = None
        self.on_remote_clicked = None
        self.on_interrupt = None
        self.on_voice_change = None
        self.on_audio_device_change = None
        self.get_plugins = None
        self.get_plugin_settings = None
        self.on_wake_toggle = None
        self.on_wake_manual = None
        self.on_wake_install = None
        self.on_push_to_talk = None
        self.ptt_hold = None
        self.wake_get_state = None
        self._muted = False
        self._ready = False
        self._current_file: str | None = None
        self._state = "INITIALISING"
        self._ptt_on = False
        self._ptt_down = False
        self._cam_run = False
        self._cam_full = False
        self._pushup_on = False                    # push-up frames are arriving in the media panel
        self._pushup_want_full = False             # open the push-up view full screen as soon as its first frame arrives
        self._cam_max = (1920, 1080)
        self._cam_was_fs = False
        self._media_lock = threading.Lock()
        self._media_pending = None
        self._media_queued = False
        self._media_scene_paused = False
        self._mpv_win = None
        self._compact: CompactMic | None = None
        self._t0 = time.time()
        self._confirm_panel = None
        self._settings_open = False
        self._page = "home"
        self._settings_page = "general"
        self._last_clip = ""

        self.backdrop = Backdrop()
        self.setCentralWidget(self.backdrop)
        self.backdrop.aurora = bool(cfg.get("fx_aurora", True))
        self.backdrop.stars = bool(cfg.get("fx_stars", True))
        self.backdrop.speed = float(cfg.get("fx_speed", 1.0))
        self.stage = Stage3D()
        self.backdrop.stage = self.stage
        self.stage.particles = bool(cfg.get("fx_particles", True))
        self.stage.speed = float(cfg.get("fx_speed", 1.0))
        self.stage.set_model(str(cfg.get("stage_model") or "holo"))
        self.stage.show_status = False
        self.stage.on_gpu_event = self._on_gpu_event
        self.stage.quality = str(cfg.get("quality") or "auto")
        self._build()
        self.modal = ModalLayer(self.backdrop)
        self.modal.closed.connect(self._on_modal_closed)
        self.modal.opened.connect(lambda: self._pause_scene(True))

        self._log_sig.connect(self._on_log)
        self._state_sig.connect(self._apply_state)
        self._content_sig.connect(self._show_content)
        self._reconfig_sig.connect(self._show_setup)
        self._cam_stream_sig.connect(self._camera_toggle)
        self._cam_frame_sig.connect(self._camera_frame)
        self._cam_img_sig.connect(self._camera_image)
        self._cam_full_sig.connect(self._cam_full_request)
        self._pushup_sig.connect(self._pushup_stats)
        self._pushup_full_sig.connect(self._pushup_full_request)
        self._celebrate_sig.connect(self._celebrate)
        self._cam_pending = False
        self._cam_px = (640, 480)
        self._phone_frame_sig.connect(self._phone_frame)
        self._media_frame_sig.connect(self._on_media_frame)
        self._mpv_open_sig.connect(self._mpv_open)
        self._mpv_close_sig.connect(self._hide_media)
        self._mpv_cmd_sig.connect(self._mpv_cmd)
        self._mpv_timer = QTimer(self)
        self._mpv_timer.timeout.connect(self._mpv_tick)
        self._confirm_sig.connect(self._show_confirm)
        self._confirm_hide_sig.connect(self._hide_confirm)
        self._wake_dl_sig.connect(self._on_wake_dl)
        self._update_sig.connect(self._on_update)
        self._toast_sig.connect(self._toast)
        self._phone_event_sig.connect(self._on_phone_event)
        self._profile_sig.connect(self.apply_profile)
        self._tasks_sig.connect(lambda: self.dash.tasks.reload())
        self._update_done_sig.connect(self._on_update_done)
        self._restart_sig.connect(self.restart_app)
        self._quit_sig.connect(QApplication.quit)
        self._gesture_sig.connect(self._run_gesture_action)
        self._gesture_status_sig.connect(self._on_gesture_status)
        self._face_sig.connect(self._on_face_event)
        self._media_info_sig.connect(self._on_media_info)
        self._media_sugg_sig.connect(self._on_media_suggestions)
        self._vx_state_sig.connect(self._vx_apply_state)
        self._cursor_sig.connect(self._on_cursor_state)
        self._cursor_panic_sig.connect(self._on_cursor_panic)
        self._hand_sig.connect(self._on_hand_points)
        self._space_sig.connect(self._on_space_hands)
        self._module_sig.connect(self._run_module_cmd)
        self._hand_overlay = None
        self._modules: dict = {}
        self._locked = False
        self._lock_screen = None
        self._lock_prev_muted = False
        from core.face_lock import LockLogic
        self._lock_logic = LockLogic()
        self._face_seen_sig.connect(self._on_face_seen)
        self._lock_timer = QTimer(self)
        self._lock_timer.timeout.connect(self._lock_tick)
        self._lock_timer.start(1000)
        self._boot_screen = None
        boot_on = bool(read_cfg().get("boot_intro", True))
        if boot_on:
            from ui.boot_screen import BootScreen
            QTimer.singleShot(60, self._show_boot_intro)
        # the lock screen waits for the boot intro, so the intro plays first and the lock appears right after it
        QTimer.singleShot(450 + (BootScreen.DURATION_MS if boot_on else 0), self._lock_on_start)
        self.gesture_status = "off"
        self.camera = None
        QTimer.singleShot(2500, self._camera_autostart)
        QTimer.singleShot(3000, self._start_dev_hotkey)

        self._clock = QTimer(self)
        self._clock.timeout.connect(self._tick)
        self._clock.start(1000)
        self._tick()
        try:
            from memory.config_manager import get_push_to_talk_enabled
            self._ptt_on = bool(get_push_to_talk_enabled())
        except Exception:
            pass
        try:
            QApplication.clipboard().dataChanged.connect(self._on_clip)
        except Exception:
            pass
        self._theme_timer = QTimer(self)
        self._theme_timer.timeout.connect(self._auto_theme_tick)
        self._theme_timer.start(60_000)
        QTimer.singleShot(250, self._boot)
        QTimer.singleShot(2500, self._check_gpu)
        QTimer.singleShot(9000, self._auto_update_check)
        QTimer.singleShot(3500, self._start_island)

    def _show_boot_intro(self):
        """Cinematic start-up sequence over the window; never lets a problem here get in the way of starting."""
        try:
            from ui.boot_screen import BootScreen
            self._boot_screen = BootScreen(self, self.assistant_name)
            self._boot_screen.finished.connect(lambda: setattr(self, "_boot_screen", None))
            self._boot_screen.start()
        except Exception as e:
            self._boot_screen = None
            self.write_log(f"ERR: boot intro — {e}")

    def set_voice_ring(self, on: bool):
        self.stage.voice_ring = bool(on)
        patch_cfg(voice_ring=bool(on))

    def set_boot_intro(self, on: bool):
        patch_cfg(boot_intro=bool(on))
        self._toast("Boot intro: on" if on else "Boot intro: off")

    def _boot(self):
        try:
            from memory.config_manager import is_configured
            ok = is_configured()
        except Exception:
            ok = True
        if ok:
            self._ready = True
        else:
            self._show_setup()

    def _build(self):
        root = QHBoxLayout(self.backdrop)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(14)

        # Left rail: icons only at rest, slides open (with labels) while the mouse is over it.
        # It floats over the page area; this spacer keeps the pages clear of the slim rail.
        self.side = side = Sidebar(self.backdrop)
        reserve = QWidget()
        reserve.setFixedWidth(Sidebar.COLLAPSED)
        root.addWidget(reserve)

        sl = QVBoxLayout(side)
        sl.setContentsMargins(12, 16, 12, 12)
        sl.setSpacing(4)
        brand = QHBoxLayout()
        brand.setContentsMargins(3, 0, 0, 0)
        brand.setSpacing(10)
        brand.addWidget(LogoMark())
        self.brand_txt = QWidget()
        bcol = QVBoxLayout(self.brand_txt)
        bcol.setContentsMargins(0, 0, 0, 0)
        bcol.setSpacing(0)
        self.title = QLabel(self.assistant_name.upper())
        tf = font(15, QFont.Weight.Bold)
        tf.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 4)
        self.title.setFont(tf)
        bcol.addWidget(self.title)
        sub_l = QLabel("Your AI Assistant")
        sub_l.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 10px;")
        bcol.addWidget(sub_l)
        brand.addWidget(self.brand_txt, 1)
        sl.addLayout(brand)
        sl.addSpacing(14)
        self.nav: dict = {}
        for key, icon, label in (("home", "home", "Home"), ("chat", "chat", "Chat"), ("dashboard", "dashboard", "Dashboard"),
                                 ("phone", "phone", "Phone"), ("smarthome", "house", "Smart Home"), ("space", "hand", "Hand Space"), ("voice", "mic", "Voice & AI"), ("settings", "gear", "Settings")):
            b = NavItem(icon, label)
            b.setFixedHeight(46)
            b.clicked.connect(lambda _=False, k=key: self._nav(k))
            side.progress.connect(b.set_k)
            self.nav[key] = b
            sl.addWidget(b)
        sl.addStretch()
        self.perf = QLabel("")
        self.perf.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 10px;")
        self.perf.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._perf_pref = bool(read_cfg().get("show_perf", False))
        sl.addWidget(self.perf)

        # status block: mic state · clock · date · fullscreen
        self.pill = StatePill()
        sl.addWidget(self.pill, 0, Qt.AlignmentFlag.AlignHCenter)
        self.clock = QLabel("--:--")
        self.clock.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.date = QLabel("")
        self.date.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.date.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 10px;")
        sl.addWidget(self.clock)
        sl.addWidget(self.date)
        fs = IconButton("full", 34, tip=f"Fullscreen  {__import__('ui.theme', fromlist=['hotkeys']).hotkeys()['fullscreen']}")
        fs.clicked.connect(self.toggle_fullscreen)
        sl.addWidget(fs, 0, Qt.AlignmentFlag.AlignHCenter)
        sl.addSpacing(4)

        crow = QHBoxLayout()
        self.compact_lbl = QLabel("Compact Mode")
        self.compact_lbl.setStyleSheet(f"color: {C.TEXT_MED};")
        crow.addWidget(self.compact_lbl)
        crow.addStretch()
        self.compact_toggle = Toggle(False)
        self.compact_toggle.toggled.connect(self._compact_toggled)
        crow.addWidget(self.compact_toggle)
        sl.addLayout(crow)
        sl.addSpacing(4)
        self.ver_lbl = QLabel(f"v{APP_VERSION}")
        self.ver_lbl.setStyleSheet(f"color: {C.TEXT_MED}; font-size: 10px;")
        self.by_lbl = QLabel("by ASDcuber521")
        self.by_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 10px; letter-spacing: 1px;")
        sl.addWidget(self.ver_lbl)
        sl.addWidget(self.by_lbl)
        self._side_txt = None
        side.progress.connect(self._side_progress)
        self._side_progress(0.0)

        col = QVBoxLayout()
        col.setSpacing(12)

        self.pages = QStackedWidget()
        home = QWidget()
        hv = QVBoxLayout(home)
        hv.setContentsMargins(0, 0, 0, 0)
        hv.setSpacing(6)
        self.host = StageHost(self.stage)
        hv.addWidget(self.host, 1)
        self.hero_title = QLabel(self.assistant_name.upper())
        hf = font(30, QFont.Weight.Bold)
        hf.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 14)
        self.hero_title.setFont(hf)
        self.hero_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hv.addWidget(self.hero_title)
        tag = QLabel("Always here. Always listening.")
        tag.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tag.setStyleSheet(f"color: {C.PRI}; font-size: 14px; letter-spacing: 1px;")
        hv.addWidget(tag)
        hv.addSpacing(6)
        self.home_bar = InputBar(attach=False)
        self.home_bar.setMinimumWidth(460)
        self.home_bar.setMaximumWidth(900)
        bar_row = QHBoxLayout()               # stretch on both sides: the bar fills the width up to 900px
        bar_row.setContentsMargins(0, 0, 0, 0)
        bar_row.addStretch(1)
        bar_row.addWidget(self.home_bar, 12)
        bar_row.addStretch(1)
        hv.addLayout(bar_row)
        chips = QHBoxLayout()
        chips.setSpacing(10)
        chips.addStretch()
        for label, kind in (("Open apps", "apps"), ("Search web", "web"), ("System info", "sys"), ("Create task", "task")):
            c = GlowButton(label, "ghost", pill=True)
            c.setMinimumHeight(38)
            c.clicked.connect(lambda _=False, k=kind: self._chip(k))
            chips.addWidget(c)
        chips.addStretch()
        hv.addLayout(chips)
        hv.addSpacing(4)
        self.pages.addWidget(home)

        chat = GlassCard(radius=26, tint=130)
        cv = QVBoxLayout(chat)
        cv.setContentsMargins(26, 18, 26, 18)
        cv.setSpacing(12)
        ch = QHBoxLayout()
        ct = QLabel("Conversation")
        ct.setFont(font(14, QFont.Weight.Bold))
        ch.addWidget(ct)
        ch.addStretch()
        clr = IconButton("close", 32, tip="Clear conversation", flat=True)
        ch.addWidget(clr)
        cv.addLayout(ch)
        self.chat = ChatView()
        clr.clicked.connect(self.chat.clear)
        self.chat.copied.connect(lambda t: setattr(self, "_last_clip", t))            # our own Copy must not trigger the clipboard hint
        self.chat.reacted.connect(lambda e, _t, on: self._toast(f"Reaction {e} saved") if on else None)
        cv.addWidget(self.chat, 1)
        self.chat_bar = InputBar()
        cv.addWidget(self.chat_bar)
        self.pages.addWidget(chat)

        self.dash = DashboardPage()
        self.dash.quick.connect(self._quick)
        self.dash.toast.connect(self._toast)
        self.pages.addWidget(self.dash)
        self.phone_page = PhonePage()
        self.phone_page.command.connect(self._phone_command)
        self.phone_page.toast.connect(self._toast)
        self.pages.addWidget(self.phone_page)
        from ui.home_page import SmartHomePage
        self.home_page = SmartHomePage()
        self.home_page.toast.connect(self._toast)
        self.pages.addWidget(self.home_page)
        from ui.space_page import SpacePage
        self.space_page = SpacePage(status_fn=lambda: (getattr(self, "gesture_status", "off"), bool(self.camera is not None and self.camera.space)))
        self.space_page.toast.connect(self._toast)
        self.space_page.open_requested.connect(lambda: self.module_command("hand_space", "show"))
        self.pages.addWidget(self.space_page)
        col.addWidget(self.pages, 1)
        root.addLayout(col, 1)

        self._bars = [self.home_bar, self.chat_bar]
        for bar in self._bars:
            bar.submitted.connect(self._send)
            bar.mic_clicked.connect(self.toggle_mute)
            bar.attach_clicked.connect(self._browse)
            bar.detach_clicked.connect(self.clear_file)
        self._sync_nav()
        side.reposition()
        side.raise_()

        self._content = self._make_content()
        self._media = self._make_media()
        self.mini = MiniJarvis(self.backdrop)
        self.pushup_hud = PushupHUD(self.backdrop)
        self._pu_fb = QLabel(self.backdrop)                    # push-up form tip, bottom-left of the full-screen view
        self._pu_fb.setWordWrap(True)
        self._pu_fb.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._pu_fb_good = None
        self._pu_fb.hide()
        self.wk_left = WorkoutPanel(self.backdrop, "left")           # side panels in the free strips beside the picture
        self.wk_right = WorkoutPanel(self.backdrop, "right")
        self._wk_aspect = 16 / 9
        self.celebration = CelebrationFX(self.backdrop)         # new personal record: confetti + big number
        self._cam_caption_w = QLabel(self.backdrop)
        self._cam_caption_w.setWordWrap(True)
        self._cam_caption_w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._cam_caption_w.setStyleSheet("QLabel { background: rgba(6,10,18,175); color: #e8f1ff; border-radius: 14px; "
                                          "padding: 10px 14px; font-size: 15px; }")
        self._cam_caption_w.hide()
        self._cam_caption_timer = QTimer(self)
        self._cam_caption_timer.setSingleShot(True)
        self._cam_caption_timer.timeout.connect(self._cam_caption_w.hide)
        self._clip = self._make_clip()
        self._alert_w = GlassCard(self.backdrop, radius=26, tint=225, accent=C.RED)
        al = QVBoxLayout(self._alert_w)
        al.setContentsMargins(46, 26, 46, 26)
        al.setSpacing(4)
        self._alert_title = QLabel("⚠  UNKNOWN PERSON")
        self._alert_title.setFont(font(24, QFont.Weight.Bold))
        self._alert_title.setStyleSheet(f"color: {C.RED};")
        self._alert_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._alert_sub = QLabel("An unrecognised face is in front of the camera")
        self._alert_sub.setStyleSheet(f"color: {C.TEXT_MED};")
        self._alert_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        al.addWidget(self._alert_title)
        al.addWidget(self._alert_sub)
        self._alert_w.hide()
        self._alert_timer = QTimer(self)
        self._alert_timer.setSingleShot(True)
        self._alert_timer.timeout.connect(self._alert_done)
        self._alert_blink = QTimer(self)
        self._alert_blink.timeout.connect(self._alert_pulse)
        self._alert_on = False
        self.host.float_parent = self.backdrop
        self.host.pages_ref = self.pages
        self.host.floats = {"content": self._content, "media": self._media,
                            "clip": self._clip, "alert": self._alert_w}
        self.host.on_layout = self._on_host_layout
        self.pages.currentChanged.connect(lambda _i: self.host.layout_floats())
        _w = _PageWatch(self.host)
        self.pages.installEventFilter(_w)
        self.toasts = ToastCenter(self.backdrop, self.host.area, self._front)      # the one notification system
        self._veil = _PageVeil(self.pages)                                         # short fade-in when the page changes
        self.pages.currentChanged.connect(lambda _i: self._veil.play())
        self._media_timer = QTimer(self)
        self._media_timer.setSingleShot(True)
        self._media_timer.timeout.connect(self._close_media)

    def _make_content(self) -> GlassCard:
        c = GlassCard(self.backdrop, radius=22, tint=190)
        c.hide()
        l = QVBoxLayout(c)
        l.setContentsMargins(20, 14, 14, 16)
        h = QHBoxLayout()
        self._content_title = QLabel()
        self._content_title.setFont(font(11, QFont.Weight.Bold))
        self._content_title.setStyleSheet(f"color: {C.PRI};")
        h.addWidget(self._content_title)
        h.addStretch()
        x = IconButton("close", 30, flat=True)
        x.clicked.connect(lambda: c.hide())
        h.addWidget(x)
        l.addLayout(h)
        self._content_text = QTextEdit()
        self._content_text.setReadOnly(True)
        self._content_text.setFrameShape(QFrame.Shape.NoFrame)
        self._content_text.viewport().setAutoFillBackground(False)
        self._content_text.setStyleSheet(f"QTextEdit {{ background: transparent; border: none; color: {C.TEXT_MED}; font-size: 13px; }}")
        l.addWidget(self._content_text)
        return c

    def _make_media(self) -> GlassCard:
        c = GlassCard(self.backdrop, radius=20, tint=200, accent=C.ACC)
        c.hide()
        l = QVBoxLayout(c)
        l.setContentsMargins(12, 10, 10, 12)
        h = QHBoxLayout()
        self._media_title = QLabel("CAMERA")
        self._media_title.setStyleSheet(f"color: {C.TEXT}; font-weight: 700; letter-spacing: 1px; font-size: 12px;")
        h.addWidget(self._media_title)
        h.addStretch()
        self._media_full_btn = IconButton("full", 26, flat=True, tip="Full screen (Esc to exit)")
        self._media_full_btn.clicked.connect(self._toggle_media_full)
        h.addWidget(self._media_full_btn)
        x = IconButton("close", 26, flat=True)
        x.clicked.connect(self._close_media_user)
        h.addWidget(x)
        l.addLayout(h)
        self._media_lbl = FrameLabel()
        self._media_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._media_lbl.setMinimumSize(1, 1)
        self._media_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self._media_lbl.mouseDoubleClickEvent = lambda e: self._toggle_media_full()
        l.addWidget(self._media_lbl, 1)
        l.addWidget(self._make_video_bar())
        self._vx_rating = "none"
        self._vx_subscribed = False
        self._vx_settings = None
        self._vx_overlay = None
        self._vx_items = None
        self._vx_meta = {}
        return c

    def _make_video_bar(self) -> QWidget:
        """Control bar shown under the video while mpv plays."""
        bar = VideoBar()
        bar.ctl.connect(self._video_ctl)
        bar.seek_fraction.connect(self._mpv_seek_fraction)
        bar.stop.connect(self._close_media_user)
        bar.action.connect(self._vx_action)
        bar.hide()
        self._mpv_bar = bar
        return bar

    def _make_clip(self) -> GlassCard:
        c = GlassCard(self.backdrop, radius=20, tint=210, accent=C.PRI)
        c.hide()
        l = QVBoxLayout(c)
        l.setContentsMargins(16, 10, 10, 10)
        l.setSpacing(6)
        top = QHBoxLayout()
        lab = QLabel("CLIPBOARD")
        lab.setStyleSheet(f"color: {C.PRI}; font-weight: 700; letter-spacing: 2px; font-size: 10px;")
        top.addWidget(lab)
        self._clip_prev = QLabel()
        self._clip_prev.setStyleSheet(f"color: {C.TEXT_MED};")
        top.addWidget(self._clip_prev, 1)
        x = IconButton("close", 24, flat=True)
        x.clicked.connect(c.hide)
        top.addWidget(x)
        l.addLayout(top)
        row = QHBoxLayout()
        for label, fmt in (("Translate", "Translate this text to English: {t}"), ("Summarise", "Summarise this: {t}"),
                           ("Explain", "Explain this: {t}"), ("Fix", "Fix grammar and spelling: {t}")):
            b = GlowButton(label)
            b.setMinimumHeight(28)
            b.clicked.connect(lambda _=False, f=fmt: self._clip_act(f))
            row.addWidget(b)
        l.addLayout(row)
        return c

    def _on_clip(self):
        try:
            if self.modal.is_open():
                return
            t = QApplication.clipboard().text() or ""
            if len(t.strip()) < 24 or t == self._last_clip or not self._ready:
                return
            self._last_clip = t
            pv = " ".join(t.split())
            self._clip_prev.setText(f"“{pv[:70]}{'…' if len(pv) > 70 else ''}”")
            self._clip.show()
            self.host.layout_floats()
            self._front(self._clip)
            fade_in(self._clip, 240)
            QTimer.singleShot(12000, self._clip.hide)
        except Exception:
            pass

    def _clip_act(self, fmt: str):
        self._clip.hide()
        self._send(fmt.format(t=self._last_clip[:800]))

    def set_perf(self, on: bool):
        self._perf_pref = bool(on)
        self.perf.setVisible(bool(on) and bool(self._side_txt))
        patch_cfg(show_perf=on)

    def _side_progress(self, k: float):
        """Show or hide the side bar's text as it opens and closes (icons are always visible)."""
        wide = k > 0.72
        if wide == self._side_txt:
            return
        self._side_txt = wide
        self.brand_txt.setVisible(wide)
        self.date.setVisible(wide)
        self.compact_lbl.setVisible(wide)
        self.ver_lbl.setVisible(wide)
        self.by_lbl.setVisible(wide)
        self.perf.setVisible(wide and self._perf_pref)
        self.clock.setFont(font(15 if wide else 11, QFont.Weight.Bold))

    def _tick(self):
        if self._perf_pref:
            st = self.stage
            mode = "GPU" if GL_ENABLED else "CPU"
            self.perf.setText(f"{mode} · {st.fps:.0f} fps · {st._ms:.1f} ms")
        self.clock.setText(time.strftime("%H:%M"))
        self.date.setText(time.strftime("%a %d %b %Y"))
        if self._page == "dashboard" and self.dash.isVisible():
            self.dash.tick(_metrics.snapshot())

    def _nav(self, key: str):
        if key in ("home", "chat", "dashboard", "phone", "smarthome", "space"):
            self._goto(key)
        elif key == "voice":
            self.open_settings("voice")
        elif key == "settings":
            self.open_settings("general")
        self._sync_nav()

    def _goto(self, key: str):
        self._page = key
        self.pages.setCurrentIndex({"home": 0, "chat": 1, "dashboard": 2, "phone": 3, "smarthome": 4, "space": 5}[key])
        self._sync_nav()

    def _sync_nav(self):
        for k, b in self.nav.items():
            if self._settings_open:
                on = (k == "voice" and self._settings_page == "voice") or (k == "settings" and self._settings_page != "voice")
            else:
                on = k == self._page
            b.setChecked(on)

    def _chip(self, kind: str):
        if kind == "apps":
            self.home_bar.set_text("Open ")
        elif kind == "web":
            self.home_bar.set_text("Search the web for ")
        elif kind == "sys":
            self._send("Show me my system status: CPU, memory and battery.")
        elif kind == "task":
            self._goto("dashboard")
            self.dash.tasks.focus_input()

    def add_activity(self, text: str):
        self.dash.activity.add(text)

    def _compact_toggled(self, on: bool):
        if on:
            QTimer.singleShot(150, self._enter_compact_from_toggle)

    def _enter_compact_from_toggle(self):
        self.compact_toggle.setChecked(False)
        self.enter_compact()

    def _quick(self, key: str):
        try:
            if key == "folder":
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path.home() / "Documents")))
                self.add_activity("Opened the Documents folder")
            elif key == "screenshot":
                self.take_screenshot()
            elif key == "music":
                self._spotify("resume", "Playing music")
            elif key == "calc":
                self._launch({"Windows": [["calc.exe"]], "Darwin": [["open", "-a", "Calculator"]],
                              "Linux": [["gnome-calculator"], ["kcalc"], ["xcalc"]]}, "Calculator")
            elif key == "notepad":
                self._launch({"Windows": [["notepad.exe"]], "Darwin": [["open", "-a", "TextEdit"]],
                              "Linux": [["gedit"], ["kate"], ["mousepad"]]}, "Notepad")
            elif key == "web":
                webbrowser.open("https://www.google.com")
                self.add_activity("Opened the web browser")
        except Exception as e:
            self._toast(f"⚠ {e}")

    def _launch(self, table: dict, name: str):
        for cmd in table.get(_OS, []):
            try:
                subprocess.Popen(cmd, **_WIN_HIDE)
                self.add_activity(f"Opened {name}")
                return
            except Exception:
                continue
        self._toast(f"Couldn't open {name}")

    def _spotify(self, action: str, note: str):
        def work():
            try:
                from actions import spotify_controller as sc
                sc.spotify_controller({"action": action})
            except Exception as e:
                self._log_sig.emit(f"ERR: Spotify — {e}")
        threading.Thread(target=work, daemon=True).start()
        self.add_activity(note)

    def take_screenshot(self):
        self.showMinimized()
        QTimer.singleShot(500, self._grab_screen)

    def _grab_screen(self):
        try:
            out = Path.home() / "Pictures" / "Jarvis"
            out.mkdir(parents=True, exist_ok=True)
            path = out / time.strftime("screenshot_%Y%m%d_%H%M%S.png")
            QApplication.primaryScreen().grabWindow(0).save(str(path))
            self.add_activity("Screenshot saved")
            self._toast(f"Screenshot saved to Pictures/Jarvis")
        except Exception as e:
            self._toast(f"⚠ Screenshot failed: {e}")
        finally:
            self.showNormal()
            self.activateWindow()

    def _check_gpu(self):
        if GL_ENABLED and hasattr(self.backdrop, "isValid") and not self.backdrop.isValid():
            patch_cfg(hw_accel=False)
            self.write_log("SYS: GPU rendering is unavailable on this system — switched to software mode. Restart Jarvis.")

    def _on_gpu_event(self, why: str):
        patch_cfg(gpu_holo=False)
        self.write_log(f"SYS: GPU hologram renderer unavailable ({why}) — using the standard renderer.")

    def set_gpu_holo(self, on: bool):
        self.stage.gpu_enabled = bool(on and GL_ENABLED)
        patch_cfg(gpu_holo=bool(on))
        self._toast("GPU hologram renderer " + ("on" if on else "off"))

    _HUD_MAP = {"sphere": "globe", "core": "crystal", "armor": "holo"}

    def profile_summary(self, name: str) -> str:
        from memory.config_manager import get_profile
        pr = get_profile(name)
        hud = self._HUD_MAP.get(str(pr.get("hud")), str(pr.get("hud")))
        talk = "push-to-talk" if pr.get("talk_mode") == "push" else "open microphone"
        return f"3D model: {hud.title()} · {talk} · graphics quality: {pr.get('quality', 'auto')}"

    def apply_profile(self, name: str, announce: bool = True):
        from memory.config_manager import get_profile, set_active_profile
        name = str(name or "home").lower()
        if name not in ("home", "work", "game"):
            return
        set_active_profile(name)
        pr = get_profile(name)
        hud = self._HUD_MAP.get(str(pr.get("hud") or "holo").lower(), str(pr.get("hud") or "holo").lower())
        self.set_model(hud)
        self.set_quality(str(pr.get("quality") or "auto"))
        if pr.get("talk_mode") in ("push", "open"):
            self.toggle_ptt(pr.get("talk_mode") == "push")
        self.add_activity(f"Profile: {name.title()}")
        self.write_log(f"SYS: Profile → {name.title()} ({self.profile_summary(name)})")
        if announce:
            self._toast(f"Profile: {name.title()}")

    def set_quality(self, q: str):
        self.stage.quality = q
        patch_cfg(quality=q)

    def set_hw_accel(self, on: bool):
        patch_cfg(hw_accel=on)
        self._toast("GPU acceleration " + ("on" if on else "off") + " — restart to apply")

    def _cycle_model(self):
        m = self.stage.cycle_model()
        patch_cfg(stage_model=m)
        self._toast(f"3D model: {m.title()}")

    def open_settings(self, page: str = "general"):
        if self.modal.is_open():
            return
        self._settings_open = True
        self._settings_page = page
        panel = SettingsPanel(self)
        panel.show_page(page)
        self.modal.present(panel, 900, 620)
        self._sync_nav()

    def close_modal(self):
        self.modal.dismiss()

    def _pause_scene(self, on: bool):
        on = bool(on) or getattr(self, "_media_scene_paused", False) or getattr(self, "_space_scene_paused", False)
        self.backdrop.paused = on
        self.stage.paused = on
        if not on:
            self.stage._tm.setInterval(24)
        if not on:
            self.backdrop._dirty = True

    def _on_modal_closed(self):
        self._pause_scene(False)
        self._settings_open = False
        self._sync_nav()

    def set_accent(self, hx: str, save: bool = True):
        if not apply_accent(hx):
            return
        self.setStyleSheet(build_qss())
        for w in self.findChildren(QWidget):
            w.update()
        self.title.update()
        if save:
            patch_cfg(ui_color=C.PRI, ui_theme="")            # a custom accent replaces the ready-made theme

    def _repaint_theme(self):
        self.setStyleSheet(build_qss())
        for w in self.findChildren(QWidget):
            w.update()
        self.title.update()
        self.backdrop._dirty = True

    def set_theme(self, key: str):
        """Switch to a ready-made theme (Jarvis, Iron Man, Neon, Minimal, Emerald, Sunset, Arc, Matrix, Sakura, Daylight) — instantly."""
        if not apply_theme(key):
            return
        self._repaint_theme()
        patch_cfg(ui_theme=current_theme(), ui_color=C.PRI)
        self._toast(f"Theme: {THEMES.get(current_theme(), {}).get('label', key.title())}")

    # ── automatic day / night ──────────────────────────────────────────────────────────
    def set_theme_auto(self, on: bool):
        patch_cfg(ui_theme_auto=bool(on))
        if on:
            self._auto_period = None
            self._auto_theme_tick()
            self._toast("Automatic day / night: on")
        else:
            self._auto_period = None
            self._restore_night_look()
            self._toast("Automatic day / night: off")

    def _restore_night_look(self):
        """Back to the look the user chose: their ready-made theme, else their custom accent, else the default."""
        cfg = read_cfg()
        theme = (cfg.get("ui_theme") or "").strip().lower()
        col = (cfg.get("ui_color") or "").strip()
        if not (theme and apply_theme(theme)):
            apply_theme("jarvis")
            if col and col.lower() != DEFAULT_ACCENT:
                apply_accent(col)
        self._repaint_theme()

    def _auto_theme_tick(self):
        """Once a minute: switch only when day turns into night or back, so a theme picked by hand is not overridden at once."""
        cfg = read_cfg()
        if not cfg.get("ui_theme_auto"):
            return
        theme = (cfg.get("ui_theme") or "").strip().lower()
        period = "day" if auto_theme_key(theme or "jarvis") == DAY_THEME else "night"
        if period == self._auto_period:
            return
        first = self._auto_period is None
        self._auto_period = period
        if period == "day":
            if apply_theme(DAY_THEME):
                self._repaint_theme()
        elif not first or current_theme() == DAY_THEME:
            self._restore_night_look()

    def set_model(self, name: str):
        self.stage.set_model(name)
        patch_cfg(stage_model=self.stage.model)

    def set_speed(self, v: float):
        self.stage.speed = v
        self.backdrop.speed = v
        patch_cfg(fx_speed=v)

    def metrics_visible(self) -> bool:
        return False

    def set_effect(self, key: str, on: bool):
        if key == "aurora":
            self.backdrop.aurora = on
            self.backdrop._dirty = True
            patch_cfg(fx_aurora=on)
        elif key == "stars":
            self.backdrop.stars = on
            self.backdrop._dirty = True
            patch_cfg(fx_stars=on)
        elif key == "particles":
            self.stage.particles = on
            patch_cfg(fx_particles=on)

    def apply_names(self, name: str, user: str):
        name = (name or "").strip() or "JARVIS"
        self.assistant_name = name
        self.user_name = (user or "").strip()
        self.title.setText(name.upper())
        self.hero_title.setText(name.upper())
        self.setWindowTitle(name.upper())
        try:
            from memory.config_manager import save_assistant_config
            save_assistant_config(name, self.user_name)
        except Exception:
            patch_cfg(assistant_name=name, user_name=self.user_name)
        self._toast("Names saved")

    def apply_voice(self, name: str):
        try:
            from memory.config_manager import save_voice
            save_voice(name)
        except Exception:
            patch_cfg(voice_name=name)
        self.write_log(f"SYS: Voice → {name}")
        if self.on_voice_change:
            try:
                self.on_voice_change()
            except Exception:
                pass

    def toggle_wake(self, on: bool, toggle=None):
        if not self.on_wake_toggle:
            return
        try:
            res = self.on_wake_toggle(on)
        except Exception as e:
            self.write_log(f"ERR: wake word — {e}")
            return
        if res == "need_download":
            if toggle is not None:
                toggle.setChecked(False)
            self.write_log("SYS: Downloading wake-word model…")
            self._wake_install()
        else:
            self.write_log(f"SYS: Wake word {'enabled' if on else 'disabled'}.")

    def _wake_install(self):
        if not self.on_wake_install:
            return

        def work():
            try:
                ok, msg = self.on_wake_install()
            except Exception as e:
                ok, msg = False, str(e)
            self._wake_dl_sig.emit(bool(ok), str(msg))
        threading.Thread(target=work, daemon=True).start()

    def _on_wake_dl(self, ok: bool, msg: str):
        self.write_log(f"SYS: {msg}")
        if ok and self.on_wake_toggle:
            try:
                self.on_wake_toggle(True)
            except Exception:
                pass

    def toggle_ptt(self, on: bool):
        self._ptt_on = on
        scope = ""
        if self.on_push_to_talk:
            try:
                scope = self.on_push_to_talk(on) or ""
            except Exception:
                pass
        self.write_log(f"SYS: Push-to-talk {'on' if on else 'off'}{f' ({scope})' if scope else ''}.")

    def wake_manual(self):
        if self.on_wake_manual:
            try:
                self.on_wake_manual()
            except Exception:
                pass

    def _launch_cmd(self) -> list[str]:
        if getattr(sys, "frozen", False):
            return [sys.executable]
        exe = sys.executable
        if _OS == "Windows" and exe.lower().endswith("python.exe"):
            w = exe[:-10] + "pythonw.exe"
            exe = w if os.path.exists(w) else exe
        return [exe, str(BASE_DIR / "main.py")]

    def _autostart_file(self) -> Path:
        if _OS == "Darwin":
            return Path.home() / "Library/LaunchAgents/com.asdcuber.jarvis.plist"
        return Path.home() / ".config/autostart/jarvis.desktop"

    def autostart_enabled(self) -> bool:
        if _OS == "Windows":
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
                    winreg.QueryValueEx(k, "Jarvis")
                return True
            except Exception:
                return False
        return self._autostart_file().exists()

    def set_autostart(self, on: bool):
        cmd = self._launch_cmd()
        try:
            if _OS == "Windows":
                import winreg
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run",
                                    0, winreg.KEY_SET_VALUE) as k:
                    if on:
                        winreg.SetValueEx(k, "Jarvis", 0, winreg.REG_SZ, " ".join(f'"{c}"' for c in cmd))
                    else:
                        try:
                            winreg.DeleteValue(k, "Jarvis")
                        except FileNotFoundError:
                            pass
            else:
                f = self._autostart_file()
                if on:
                    f.parent.mkdir(parents=True, exist_ok=True)
                    if _OS == "Darwin":
                        args = "".join(f"<string>{c}</string>" for c in cmd)
                        f.write_text('<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>'
                                     '<key>Label</key><string>com.asdcuber.jarvis</string>'
                                     f'<key>ProgramArguments</key><array>{args}</array>'
                                     '<key>RunAtLoad</key><true/></dict></plist>', encoding="utf-8")
                    else:
                        f.write_text("[Desktop Entry]\nType=Application\nName=Jarvis\n"
                                     f"Exec={' '.join(chr(34) + c + chr(34) for c in cmd)}\n"
                                     f"Path={BASE_DIR}\nX-GNOME-Autostart-enabled=true\n", encoding="utf-8")
                elif f.exists():
                    f.unlink()
            self._toast("Auto-start " + ("enabled" if on else "disabled"))
        except Exception as e:
            self.write_log(f"ERR: auto-start — {e}")

    def create_shortcut(self):
        cmd = self._launch_cmd()
        desk = Path.home() / "Desktop"
        try:
            desk.mkdir(exist_ok=True)
            if _OS == "Windows":
                lnk = desk / "Jarvis.lnk"
                args = " ".join(f'"{c}"' for c in cmd[1:])
                ico = BASE_DIR / "config" / "jarvis.ico"
                ps = ("$s=(New-Object -COM WScript.Shell).CreateShortcut('%s');$s.TargetPath='%s';"
                      "$s.Arguments='%s';$s.WorkingDirectory='%s';$s.IconLocation='%s';$s.Save()"
                      % (lnk, cmd[0], args.replace("'", "''"), BASE_DIR, ico))
                subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, **_WIN_HIDE)
            elif _OS == "Darwin":
                f = desk / "Jarvis.command"
                f.write_text(f"#!/bin/bash\ncd \"{BASE_DIR}\"\n{' '.join(chr(34) + c + chr(34) for c in cmd)}\n")
                f.chmod(0o755)
            else:
                f = desk / "Jarvis.desktop"
                f.write_text("[Desktop Entry]\nType=Application\nName=Jarvis\n"
                             f"Exec={' '.join(chr(34) + c + chr(34) for c in cmd)}\nPath={BASE_DIR}\nTerminal=false\n")
                f.chmod(0o755)
            self._toast("Desktop shortcut created")
        except Exception as e:
            self.write_log(f"ERR: shortcut — {e}")

    def check_updates(self):
        self._toast("Checking for updates…")

        def work():
            try:
                from core.updater import check_for_updates
                self._update_sig.emit(check_for_updates())
            except Exception as e:
                self._update_sig.emit({"ok": False, "message": str(e)})
        threading.Thread(target=work, daemon=True).start()

    def _on_update(self, info):
        if not isinstance(info, dict) or not info.get("ok"):
            self.write_log(f"ERR: update check — {(info or {}).get('message', 'failed')}")
            return
        if not info.get("update_available"):
            self._toast(f"You're up to date ({info.get('local_version', '')})")
            return
        notes = (info.get("notes") or "").strip().replace("\n", " ")[:170]
        if info.get("frozen"):
            title = f"Install version {info.get('remote_version')}?"
            detail = "Jarvis downloads the latest release, closes, installs it and starts again. Your settings and memory are kept."
        else:
            title = f"Update to v{info.get('remote_version')}?"
            detail = (notes + " — " if notes else "") + "Your settings, memory and plugins are kept, and a backup lets you roll back."
        p = ConfirmPanel(title, detail)
        p.answered.connect(lambda ok: self._do_update(ok))
        self.modal.present(p, 480, 250, dismissable=False)

    def _do_update(self, ok: bool):
        self.modal.dismiss()
        if not ok:
            return
        self.write_log("SYS: Installing update…")

        def work():
            try:
                from core.updater import apply_update
                self._update_done_sig.emit(apply_update())
            except Exception as e:
                self._update_done_sig.emit({"ok": False, "message": str(e)})
        threading.Thread(target=work, daemon=True).start()

    def _on_update_done(self, r):
        r = r if isinstance(r, dict) else {}
        if r.get("open_url"):
            webbrowser.open(r["open_url"])
        self.write_log(f"{'SYS' if r.get('ok') else 'ERR'}: {r.get('message', 'Update finished.')}")
        if r.get("quit_now"):
            QTimer.singleShot(1500, QApplication.quit)
        elif r.get("ok") and not r.get("manual"):
            QTimer.singleShot(400, self._ask_restart)

    def _ask_restart(self):
        if self.modal.is_open():
            self.modal.dismiss()
        p = ConfirmPanel("Update installed", "Restart Jarvis now to use the new version?")
        p.answered.connect(self._restart_answer)
        QTimer.singleShot(330, lambda: self.modal.present(p, 460, 220, dismissable=False))

    def _restart_answer(self, ok: bool):
        self.modal.dismiss()
        if ok:
            QTimer.singleShot(350, self.restart_app)

    def restart_app(self):
        cmd = self._launch_cmd()
        try:
            if _OS == "Windows":
                subprocess.Popen(["cmd", "/c", "timeout /t 2 /nobreak >nul & start \"\" " + subprocess.list2cmdline(cmd)],
                                 cwd=str(BASE_DIR), **_WIN_HIDE)
            else:
                subprocess.Popen(["sh", "-c", 'sleep 2; exec "$@"', "sh", *cmd], cwd=str(BASE_DIR), start_new_session=True)
        except Exception as e:
            self.write_log(f"ERR: restart — {e}")
            return
        QApplication.quit()

    def _auto_update_check(self):
        cfg = read_cfg()
        if not cfg.get("auto_update_check", True) or time.time() - float(cfg.get("last_update_check", 0) or 0) < 12 * 3600:
            return

        def work():
            try:
                from core.updater import check_for_updates
                info = check_for_updates()
                patch_cfg(last_update_check=time.time())
                if info.get("ok") and info.get("update_available"):
                    self._toast_sig.emit(f"⬆ Update {info.get('remote_version')} is available — Settings → About")
                    self._log_sig.emit(f"SYS: {info.get('message')}. Open Settings → About to install it.")
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    def open_store(self):
        info = self._remote_info()
        if info:
            webbrowser.open(f"{info[0]}/plugin-store")
        else:
            self.write_log("SYS: Plugin store needs the remote dashboard (pip install fastapi uvicorn cryptography).")

    def _remote_info(self):
        if not self.on_remote_clicked:
            return None
        try:
            return self.on_remote_clicked()
        except Exception:
            return None

    def open_remote(self):
        info = self._remote_info()
        if not info:
            self.write_log("SYS: Remote dashboard unavailable.")
            return
        self.modal.present(RemotePanel(info, self._remote_info), 440, 610)

    # ── camera features: hand gestures + face recognition ────────────────────────
    def _camera_hub(self):
        if self.camera is None:
            from core.camera_hub import CameraHub
            self.camera = CameraHub(
                on_gesture=lambda a, g: self._gesture_sig.emit(a, g),
                on_face=lambda k, n: self._face_sig.emit(k, n),
                on_status=lambda s: self._gesture_status_sig.emit(s),
                on_cursor=lambda x, y, st: self._cursor_sig.emit(int(x), int(y), st),
                on_cursor_panic=lambda: self._cursor_panic_sig.emit(),
                on_hand=lambda pts, k: self._hand_sig.emit(pts, float(k)),
                on_space=lambda hands: self._space_sig.emit(hands),
                on_seen=lambda names: self._face_seen_sig.emit(list(names)))
        return self.camera

    def _camera_autostart(self):
        try:
            self._camera_hub().refresh()
        except Exception as e:
            self.write_log(f"ERR: camera — {e}")

    def _camera_refresh(self):
        threading.Thread(target=self._camera_hub().refresh, daemon=True).start()

    def set_gestures_enabled(self, on: bool):
        from core import gesture
        gesture.set_enabled(on)
        self._camera_refresh()
        self.write_log("SYS: Gesture control on — the camera is in use." if on else "SYS: Gesture control off.")

    # ── hand-controlled cursor: the hand becomes the mouse ──────────────────────────
    def set_hand_cursor(self, on: bool):
        from core import hand_cursor
        hand_cursor.set_enabled(on)
        self._camera_refresh()
        if not on and self._hand_overlay is not None:
            self._hand_overlay.hide()
        self.write_log("SYS: Hand cursor on — index finger moves, pinch clicks, ✌ scrolls, hold a fist to stop."
                       if on else "SYS: Hand cursor off.")

    def _on_cursor_state(self, x: int, y: int, state: str):
        if self._hand_overlay is None:
            from ui.hand_overlay import HandCursorOverlay
            self._hand_overlay = HandCursorOverlay()
        self._hand_overlay.update_state(x, y, state)

    def _on_hand_points(self, pts, pinch: float):
        if self._hand_overlay is None:
            from ui.hand_overlay import HandCursorOverlay
            self._hand_overlay = HandCursorOverlay()
        self._hand_overlay.set_hand(pts, pinch)

    # ── Hand Space: a hand-controlled file workspace (drives → folders → pictures / notes) ──────
    def _freeze_animations(self, on: bool):
        """While Hand Space is open the Jarvis scene behind it is invisible, so all its animation timers are stopped
        (backdrop, orb, logo, teasers…) and restarted afterwards. Functional timers (video, clock, alerts) keep running."""
        from PyQt6.QtCore import QTimer
        if on:
            keep = {id(getattr(self, "_mpv_timer", None))}
            frozen = []
            for t in self.findChildren(QTimer):
                if id(t) in keep or t.isSingleShot() or not t.isActive() or t.interval() > 130:
                    continue
                t.stop()
                frozen.append(t)
            self._frozen_timers = frozen
            self._space_scene_paused = True
            self._pause_scene(True)
        else:
            self._space_scene_paused = False
            for t in getattr(self, "_frozen_timers", []):
                try:
                    t.start()
                except RuntimeError:
                    pass
            self._frozen_timers = []
            self._pause_scene(False)

    def _start_island(self):
        """The Dynamic Island: a small pill at the top of the screen with the time, the weather and Spotify."""
        if not read_cfg().get("dynamic_island", True) or self._modules.get("island") is not None:
            return
        try:
            from ui.dynamic_island import DynamicIsland
            isl = DynamicIsland()
            self._modules["island"] = isl
            isl.start()
        except Exception as e:                                           # noqa: BLE001 - a decoration must never stop Jarvis
            self.write_log(f"SYS: Dynamic Island could not start ({e}).")

    def _open_hand_space(self):
        ov = self._modules.get("hand_space")
        if ov is None:
            from ui.hand_space import HandSpaceOverlay
            ov = HandSpaceOverlay()
            ov.closed.connect(self._on_space_closed)
            self._modules["hand_space"] = ov
        pinned = read_cfg().get("hand_space_folder") or None
        if self._hand_overlay is not None:
            self._hand_overlay.hide()
        hub = self._camera_hub()
        hub.space = True                       # the camera loop switches to two-hand tracking for the overlay
        hub.refresh()
        if not getattr(self, "_space_scene_paused", False):
            self._freeze_animations(True)      # Jarvis' animations stop while Hand Space is open
        ov.show_overlay(pinned)
        isl = self._modules.get("island")
        if isl is not None:
            isl.lift()                         # the Dynamic Island stays above Hand Space
        self.write_log("SYS: Hand Space open — your drives are floating: pinch-tap a folder to go in, a picture to enlarge it, hold a fist to go back. Esc to leave.")

    def _on_space_closed(self):
        if getattr(self, "_space_scene_paused", False):
            self._freeze_animations(False)
        if self.camera is not None and self.camera.space:
            self.camera.space = False
            self.camera.refresh()

    def _on_space_hands(self, hands):
        ov = self._modules.get("hand_space")
        if ov is not None and ov.isVisible():
            ov.set_hands(hands)

    def _on_cursor_panic(self):
        self.set_hand_cursor(False)
        self._toast("✊ Hand cursor stopped")

    def apply_dev_hotkey(self):
        old = getattr(self, "_dev_hotkey", None)
        if old is not None:
            old.stop()
        sc = getattr(self, "_dev_shortcut", None)
        if sc is not None:
            sc.setEnabled(False)
            sc.deleteLater()
            self._dev_shortcut = None
        self._start_dev_hotkey()

    def _start_dev_hotkey(self):
        """Alt+Space (configurable: dev_overlay_hotkey) opens the developer overlay from anywhere."""
        try:
            from core.global_hotkey import GlobalHotkey, qt_name
            chord = str(read_cfg().get("dev_overlay_hotkey") or "alt+space")
            self._dev_hotkey = GlobalHotkey(chord, lambda: self._module_sig.emit("dev_overlay", "toggle"))
            if self._dev_hotkey.start():
                return
            from PyQt6.QtGui import QShortcut
            sc = QShortcut(QKeySequence(qt_name(chord)), self)
            sc.setContext(Qt.ShortcutContext.ApplicationShortcut)
            sc.activated.connect(lambda: self._module_sig.emit("dev_overlay", "toggle"))
            self._dev_shortcut = sc
            if self._dev_hotkey.error and "already used" in self._dev_hotkey.error:
                self.write_log(f"ERR: dev overlay hotkey — {self._dev_hotkey.error}")
        except Exception as e:
            self.write_log(f"ERR: dev overlay hotkey — {e}")

    # ── face lock ────────────────────────────────────────────────────────────────────
    @property
    def locked(self) -> bool:
        return self._locked

    def _lock_on_start(self):
        from core import face_lock
        if face_lock.is_enabled():
            self.lock_jarvis("startup")

    def set_face_lock(self, on: bool):
        from core import face_lock, face_id
        face_lock.set_enabled(on)
        if on and not face_id.is_enabled():
            self.set_face_enabled(True)            # the lock needs the camera + face recognition
        self.write_log("SYS: Face lock on." if on else "SYS: Face lock off.")
        if not on and self._locked:
            self.unlock_jarvis("settings")

    def lock_jarvis(self, reason: str = "manual", force: bool = False):
        """Black-out Jarvis behind the lock screen. `force` = the user asked for it (Lock now / voice), so it works even
        when automatic locking is switched off; start-up and auto-lock still need the lock enabled in Settings."""
        from core import face_lock, face_id
        if self._locked:
            return
        if not force and not face_lock.is_enabled():
            return
        if not face_lock.pin_set():                      # never lock without a way back in if the camera fails
            self._toast("Set a PIN first — Settings → Face ID")
            self.open_settings("face")
            return
        self._locked = True
        self._lock_logic.lock()
        if self._lock_screen is None:
            from ui.lock_screen import LockScreen
            self._lock_screen = LockScreen(self, self.assistant_name)
            self._lock_screen.pin_submitted.connect(self._lock_pin)
        self.close_modal()
        for m in self._modules.values():
            m.hide()
        if self._hand_overlay is not None:
            self._hand_overlay.hide()
        self._lock_prev_muted = self._muted
        if not self._muted:
            self.toggle_mute()                      # nobody can talk to a locked assistant
        self._lock_screen.set_error("")
        self._lock_screen.set_progress(0, False)
        self._lock_screen.cover()
        self._lock_screen.show()
        self._lock_screen.focus_pin()
        if face_id.names() and not face_id.is_enabled():
            self.set_face_enabled(True)             # unlocking by face needs the camera + recognition running
        self.write_log(f"SYS: Locked ({reason}). Show a saved face or enter the PIN.")
        try:
            self._camera_hub().refresh()
        except Exception:
            pass

    def unlock_jarvis(self, how: str = "face"):
        if not self._locked:
            return
        self._locked = False
        self._lock_logic.unlock()
        self.backdrop.setEnabled(True)
        if self._lock_screen is not None:
            self._lock_screen.hide()
        isl = self._modules.get("island")                # lock_jarvis hid it; bring the Dynamic Island back
        if isl is not None:
            try:
                isl.show()
                isl.lift()
            except Exception:                            # noqa: BLE001 - a decoration must never break unlocking
                pass
        if not self._lock_prev_muted and self._muted:
            self.toggle_mute()
        self._toast("🔓 Unlocked" + (" by face" if how == "face" else ""))
        self.write_log(f"SYS: Unlocked ({how}).")

    def _on_face_seen(self, names):
        res = self._lock_logic.on_seen(names)
        if self._locked and self._lock_screen is not None:
            from core.face_lock import UNLOCK_STREAK
            self._lock_screen.set_progress(self._lock_logic.streak / UNLOCK_STREAK, bool(names))
        if res == "unlock":
            self.unlock_jarvis("face")

    def _lock_tick(self):
        from core import face_lock
        if self._locked:
            if self._lock_screen is not None:
                self._lock_screen.cover()
                st = getattr(self, "gesture_status", "off")
                if st.startswith(("error", "missing")):
                    self._lock_screen.set_camera_note("Camera unavailable — use the password")
            return
        if face_lock.is_enabled() and self._lock_logic.should_autolock(face_lock.timeout_minutes()):
            self.lock_jarvis("no saved face for a while")

    def _lock_pin(self, pin: str):
        ok, msg = self._lock_logic.try_pin(pin)
        if ok:
            self.unlock_jarvis("PIN")
        elif self._lock_screen is not None:
            self._lock_screen.set_error(msg)

    # ── overlay modules (Game HUD, Developer overlay): any thread → Qt thread ─────────
    def module_command(self, name: str, cmd: str = "toggle"):
        self._module_sig.emit(str(name), str(cmd))

    def _run_module_cmd(self, name: str, cmd: str):
        try:
            if name == "hand_cursor":
                self.set_hand_cursor(cmd == "on")
                return
            if name == "face_lock":
                self.lock_jarvis("manual", force=True)
                return
            if self._locked:
                return
            if name == "hand_space":
                ov = self._modules.get("hand_space")
                if cmd.startswith("do:"):                      # a spoken command for the open Hand Space (allow-listed inside it)
                    if ov is not None and ov.isVisible():
                        ov.voice(cmd[3:])
                    return
                if cmd == "show" or (cmd == "toggle" and not (ov and ov.isVisible())):
                    self._open_hand_space()
                elif ov is not None:
                    ov.hide()
                return
            if name == "smart_home_page":
                self._goto("smarthome")
                return
            if name == "hand_space_page":
                self._goto("space")
                return
            mod = self._modules.get(name)
            if mod is None:
                if name == "game_hud":
                    from ui.game_hud import GameHudOverlay
                    mod = GameHudOverlay()
                elif name == "dev_overlay":
                    from ui.dev_overlay import DevOverlay
                    mod = DevOverlay(self)
                else:
                    return
                self._modules[name] = mod
            if cmd == "show" or (cmd == "toggle" and not mod.isVisible()):
                mod.show_overlay()
            elif cmd in ("hide", "toggle"):
                mod.hide()
            elif cmd == "refresh" and mod.isVisible():
                mod.update()
        except Exception as e:
            self.write_log(f"ERR: {name} overlay — {e}")

    def set_face_enabled(self, on: bool):
        from core import face_id
        face_id.set_enabled(on)
        self._camera_refresh()
        self.write_log("SYS: Face recognition on — the camera is in use. (First start downloads ~37 MB of models.)"
                       if on else "SYS: Face recognition off.")

    def _on_gesture_status(self, s: str):
        self.gesture_status = s
        if s.startswith(("error", "missing")):
            self.write_log(f"ERR: camera — {s}")
            self._toast(f"Camera: {s[:60]}")

    def _on_face_event(self, kind: str, name: str):
        from core import face_id
        if kind == "known":
            self._toast(f"👋 {name} is here")
            self.write_log(f"SYS: Recognised {name}.")
            if face_id.greet_enabled() and self.on_text_command:
                try:
                    self.on_text_command(f"[FaceID] {name} just appeared in front of the camera. Greet them by name in one short friendly sentence.")
                except Exception:
                    pass
        elif kind == "unknown":
            self.write_log("SYS: An unrecognised face is in front of the camera.")
            self._show_unknown_alert()
        elif kind == "enrolled":
            self._toast(f"✅ Saved {name}'s face")
            self.write_log(f"SYS: Face saved for {name}.")
        elif kind == "enroll_failed":
            self._toast("Face not saved — try again")
            self.write_log(f"ERR: face — {name}")

    def _show_unknown_alert(self):
        """Red card in the middle of the HUD for ~4 s, then Jarvis says it out loud."""
        w = self._alert_w
        w.show()
        self.host.layout_floats()                       # centred over the page you are on (Home, Chat, Dashboard…)
        self._front(w)
        fade_in(w, 220)
        self._alert_on = True
        self._alert_blink.start(450)
        self._alert_timer.start(4000)

    def _alert_pulse(self):
        self._alert_title.setStyleSheet(f"color: {C.RED if int(time.time() * 2) % 2 else C.TEXT};")

    def _alert_done(self):
        self._alert_blink.stop()
        self._alert_w.hide()
        self._alert_on = False
        if self.on_text_command:
            try:
                self.on_text_command("[FaceID] An unknown person has been in front of the camera. "
                                     "Warn the user out loud in one short sentence, calmly, like a security system.")
            except Exception:
                pass

    def enroll_face(self, name: str) -> str:
        name = (name or "").strip()
        if not name:
            return "Whose face is it? Type a name first."
        self._camera_hub().enroll(name)
        self._toast(f"Look at the camera, {name}…")
        return f"Look straight at the camera for a few seconds to save {name}."

    def face_command(self, action: str, name: str = "") -> str:
        """Used by the voice tool (actions/face_id.py)."""
        from core import face_id
        hub = self._camera_hub()
        if action == "enroll":
            return self.enroll_face(name)
        if action == "list":
            n = face_id.names()
            return ("Saved faces: " + ", ".join(n)) if n else "No faces saved yet."
        if action == "forget":
            return f"Forgot {name}." if face_id.forget(name) else f"I don't have a face saved for {name}."
        if action == "who":
            if not hub.running or not face_id.is_enabled():
                return "Face recognition is off. Turn it on in Settings → Face ID."
            if time.time() - hub.seen_at > 3 or not hub.visible:
                return "I can't see anyone right now."
            return "I can see: " + ", ".join(hub.visible).replace("unknown", "someone I don't know") + "."
        return "Unknown face action."

    def _run_gesture_action(self, action: str, gesture_id: str):
        from core.gesture import ACTIONS, GESTURES
        try:
            if action == "toggle_mic":
                self.toggle_mute()
            elif action == "wake_toggle":
                self.wake_manual()
            elif action == "open_settings":
                self.open_settings("general")
            elif action == "fullscreen":
                self.toggle_fullscreen()
            elif action == "screenshot":
                import pyautogui
                from actions._common import output_dir, unique_path
                path = unique_path(output_dir("Screenshots"), "gesture", ".png")
                pyautogui.screenshot().save(str(path))
                self._toast(f"📸 Saved {path.name}")
                return
            else:
                import pyautogui
                key = {"play_pause": "playpause", "next_track": "nexttrack", "prev_track": "prevtrack",
                       "volume_up": "volumeup", "volume_down": "volumedown", "volume_mute": "volumemute"}.get(action)
                if key:
                    pyautogui.press(key)
            self._toast(f"{GESTURES.get(gesture_id, ('', ''))[0]} → {ACTIONS.get(action, action)}")
        except Exception as e:
            self.write_log(f"ERR: gesture action {action} — {e}")

    def toggle_fullscreen(self):
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def enter_compact(self):
        if self._compact is None:
            self._compact = CompactMic()
            self._compact.clicked.connect(self.toggle_mute)
            self._compact.restore.connect(self.exit_compact)
        self._compact.muted = self._muted
        scr = QApplication.primaryScreen().availableGeometry()
        self._compact.move(scr.right() - 100, scr.bottom() - 120)
        self._compact.show()
        self.hide()

    def exit_compact(self):
        if self._compact is not None:
            self._compact.hide()
        self.showNormal()
        self.raise_()
        self.activateWindow()

    @property
    def muted(self) -> bool:
        return self._muted

    def toggle_mute(self):
        self._muted = not self._muted
        self.stage.muted = self._muted
        for bar in self._bars:
            bar.set_live(not self._muted)
        if self._compact is not None:
            self._compact.muted = self._muted
        self._apply_state("MUTED" if self._muted else self._state)
        self.write_log(f"SYS: Microphone {'muted' if self._muted else 'live'}.")

    def _browse(self):
        p, _ = QFileDialog.getOpenFileName(self, "Attach a file")
        if p:
            self.set_file(p)

    def set_file(self, path: str):
        self._current_file = path
        for bar in self._bars:
            bar.set_file(Path(path).name)
        self._toast(f"Attached {Path(path).name}")

    def clear_file(self):
        self._current_file = None
        for bar in self._bars:
            bar.set_file(None)

    @property
    def current_file(self):
        return self._current_file

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        for u in e.mimeData().urls():
            if u.isLocalFile():
                self.set_file(u.toLocalFile())
                break

    def _send(self, text: str):
        self.write_log(f"You: {text}")
        if self.on_text_command:
            try:
                self.on_text_command(text)
            except Exception as e:
                self.write_log(f"ERR: {e}")

    _STATE_COL = {"SPEAKING": "ACC2", "LISTENING": "GREEN", "THINKING": "AMBER", "PROCESSING": "AMBER",
                  "SLEEPING": "TEXT_DIM", "MUTED": "RED"}

    def set_state(self, s: str):
        self._state_sig.emit(str(s))

    def _apply_state(self, s: str):
        s = s.upper()
        if s != "MUTED":
            self._state = s
        self.stage.state = s
        self.stage.speaking = s == "SPEAKING"
        if getattr(self, "chat", None) is not None:
            self.chat.set_typing(s in ("THINKING", "PROCESSING") and not self._muted)        # the "typing…" dots in the conversation
        self.mini.state = "MUTED" if self._muted else s
        label = "MUTED" if self._muted else s
        col = getattr(C, self._STATE_COL.get(label, "PRI"))
        self.pill.set(label, col)

    def write_log(self, text: str):
        self._log_sig.emit(str(text))

    def _on_log(self, text: str):
        text = text.strip()
        if not text:
            return
        if text.startswith("You:"):
            self.add_activity("You asked: " + text[4:].strip())
        elif text.startswith("SYS:"):
            self.add_activity(text[4:].strip())
        if text.startswith("SYS:"):
            self.chat.add("sys", "", text[4:].strip())
        elif text.startswith("ERR:"):
            self.chat.add("err", "", text[4:].strip())
            self._toast("⚠ " + text[4:].strip()[:90])
        elif text.startswith("[Phone]"):
            self.chat.add("sys", "", text)
        else:
            m = re.match(r"^\[?([^:\]]{1,24})\]?:\s*(.*)$", text, re.S)
            if m and m.group(1).strip().lower() in ("you", "web"):
                self.chat.add("user", "", m.group(2))
            elif m:
                self.chat.add("ai", m.group(1).strip(), m.group(2))
                self._cam_caption(m.group(2))
            else:
                self.chat.add("sys", "", text)

    def show_content(self, title: str, text: str):
        self._content_sig.emit(title, text)

    def _show_content(self, title: str, text: str):
        self._content_title.setText(str(title).upper())
        self._content_text.setPlainText(str(text))
        self._content.show()
        self.host.layout_floats()
        self._front(self._content)
        end = self._content.pos()
        self._content.move(end.x(), end.y() + 40)
        slide_to(self._content, end, 360)
        fade_in(self._content, 300)

    def _front(self, w):
        w.raise_()
        self.side.raise_()

    def _toast(self, msg: str):
        self.toasts.push(msg)

    def phone_event(self, event):
        if isinstance(event, dict):
            self._phone_event_sig.emit(dict(event))

    def _on_phone_event(self, event):
        if not isinstance(event, dict):
            return
        if event.get("type") == "phone_devices":
            self.phone_page.set_devices(event.get("devices") or [])
        elif event.get("type") == "phone_notification":
            self.phone_page.add_message(event)
        elif event.get("type") == "phone_call":
            self.phone_page.on_call(event)
            who = event.get("name") or event.get("number") or "unknown number"
            if event.get("event") == "incoming_call":
                self._toast_sig.emit(f"📞 Incoming call — {who}")
            elif event.get("event") == "missed_call":
                self._toast_sig.emit(f"❌ Missed call — {who}")

    on_phone_command = None          # set by main: fn(action, value)

    def _phone_command(self, action, value):
        if self.on_phone_command:
            self.on_phone_command(str(action), str(value))
        else:
            self._toast_sig.emit("Phone control is not ready yet")

    def notify_phone_connected(self):
        self._toast_sig.emit("📱 Phone connected")

    def _camera_toggle(self, on: bool):
        if on and not self._cam_run:
            self._cam_run = True
            self._cam_pending = False
            try:                                           # the biggest picture the monitor can show: the camera is asked for that size
                scr = self.screen() or QApplication.primaryScreen()
                dpr = scr.devicePixelRatio()
                self._cam_max = (int(scr.size().width() * dpr), int(scr.size().height() * dpr))
            except Exception:
                self._cam_max = (1920, 1080)
            self._media_title.setText("CAMERA")
            self._media.show()
            self.host.layout_floats()
            fade_in(self._media, 260)
            threading.Thread(target=self._cam_worker, daemon=True).start()
        elif not on:
            self._cam_run = False
            self._hide_media()

    # ── full-screen camera ──
    def _on_host_layout(self):
        if self._mpv_win is not None:
            self._sync_mpv_geometry()
        self.toasts.relayout()
        self._place_cam_overlays()

    def _place_cam_overlays(self):
        if not self._cam_full:
            return
        W, H = self.backdrop.width(), self.backdrop.height()
        self.mini.place()
        if self._pushup_on:
            self.pushup_hud.place()
            fb = self._pu_fb
            fb.setFixedWidth(max(260, min(820, W - MiniJarvis.W - 70)))
            fb.adjustSize()
            fb.move(22, H - fb.height() - 22)
            self.pushup_hud.raise_()
            fb.raise_()
            self._place_wk_panels()
        cap = self._cam_caption_w
        cap.setFixedWidth(max(240, min(760, W - MiniJarvis.W - 60)))
        cap.adjustSize()
        cap.move(18, H - cap.height() - 18)
        self.mini.raise_()
        cap.raise_()

    def _place_wk_panels(self):
        """Show the side panels only when the picture leaves free strips beside it (a 4:3 camera on a wide screen)."""
        lbl, bd = self._media_lbl, self.backdrop
        k = self.pushup_hud._k
        pw = int(WorkoutPanel.BW * k)
        top = lbl.mapTo(bd, QPoint(0, 0))
        strip = (lbl.width() - lbl.height() * self._wk_aspect) / 2
        room = strip >= pw + 14
        hud_b = self.pushup_hud.y() + self.pushup_hud.height()
        for panel in (self.wk_left, self.wk_right):
            panel.scale_to(k)
        ph = self.wk_left.height()
        fits_l = room and hud_b + int(10 * k) + ph < bd.height() - int(80 * k)
        fits_r = room and self.pushup_hud.y() + ph < bd.height() - int(MiniJarvis.H + 24)
        if fits_l:
            self.wk_left.move(int(top.x() + (strip - pw) / 2), hud_b + int(10 * k))
        if fits_r:
            self.wk_right.move(int(top.x() + lbl.width() - strip + (strip - pw) / 2), self.pushup_hud.y())
        self.wk_left.setVisible(bool(fits_l))
        self.wk_right.setVisible(bool(fits_r))
        for w in (self.wk_left, self.wk_right):
            if w.isVisible():
                w.raise_()

    def _cam_caption(self, text: str):
        """While the camera is full screen, show what Jarvis just said (a solved task, its steps) next to the mini Jarvis."""
        if not self._cam_full or self._pushup_on:            # (during push-ups the form tip label is used instead)
            return
        text = " ".join(str(text).split())
        if not text:
            return
        if len(text) > 520:
            text = text[:517] + "…"
        self._cam_caption_w.setText(text)
        self._cam_caption_w.show()
        self._place_cam_overlays()
        self._cam_caption_timer.start(30000)

    def camera_fullscreen(self, on: bool):
        self._cam_full_sig.emit(bool(on))

    def _cam_full_request(self, on: bool):
        if on and not self._cam_run and not self._pushup_on:
            self._camera_toggle(True)                    # voice "fullscreen" with the camera still closed: open it first
        self._set_cam_full(on)

    def _set_cam_full(self, on: bool):
        on = bool(on)
        if on == self._cam_full or (on and not (self._cam_run or self._pushup_on)):
            return
        self._cam_full = on
        self.host.cam_full = on
        lay = self._media.layout()
        if on:
            self._cam_was_fs = self.isFullScreen()
            self._media_lbl.cover = True                 # fill the whole screen, no black bars
            self._media_lbl.shade = True
            if not self._cam_was_fs:
                self.showFullScreen()
            self.side.hide()
            lay.setContentsMargins(6, 4, 6, 6)
            self._media_full_btn.setToolTip("Exit full screen (Esc)")
            self._set_media_scene(True)                  # the animated HUD behind the camera is hidden anyway: stop drawing it
            self.host.layout_floats()
            self._media.raise_()
            self.mini.activate(True)
            if self._pushup_on:
                self.pushup_hud.show()
                self._pu_fb.show()
            self._place_cam_overlays()
        else:
            self._media_lbl.cover = False
            self._media_lbl.shade = False
            self.pushup_hud.hide()
            self.wk_left.hide()
            self.wk_right.hide()
            self._pu_fb.hide()
            self.mini.activate(False)
            self._cam_caption_timer.stop()
            self._cam_caption_w.hide()
            lay.setContentsMargins(12, 10, 10, 12)
            self._media_full_btn.setToolTip("Full screen (Esc to exit)")
            self.side.show()
            self.side.raise_()
            if not self._cam_was_fs and self.isFullScreen():
                self.showNormal()
            self._set_media_scene(False)
            self.host.layout_floats()

    def camera_is_open(self) -> bool:
        return bool(self._cam_run)

    # ── push-up counter view ──
    def pushup_stats(self, d: dict):
        self._pushup_sig.emit(dict(d))

    def pushup_fullscreen(self, on: bool):
        self._pushup_full_sig.emit(bool(on))

    def celebrate(self, title: str, big: str, sub: str = ""):
        self._celebrate_sig.emit(str(title), str(big), str(sub))

    def _celebrate(self, title: str, big: str, sub: str):
        self.celebration.setGeometry(0, 0, self.backdrop.width(), self.backdrop.height())
        self.celebration.play(title, big, sub)

    def pushup_hud_active(self) -> bool:
        """True while the big HUD counter is on screen: the session then leaves the numbers out of the picture itself."""
        return bool(self._cam_full and self._pushup_on)

    def _pushup_full_request(self, on: bool):
        if self._pushup_on:
            self._set_cam_full(on)
        else:
            self._pushup_want_full = on                # the first frame has not arrived yet

    def _pushup_stats(self, d: dict):
        if not self._pushup_on:
            return
        self.pushup_hud.set_stats(d)
        self.wk_left.set_stats(d)
        self.wk_right.set_stats(d)
        fb = str(d.get("feedback", ""))[:90]
        good = (bool(d.get("ok", False)) or d.get("mode") == "rest") and any(k in fb for k in ("clean", "Good", "Ready", "Strong", "Hold", "Chin over", "Rest"))
        if fb != self._pu_fb.text() or good != self._pu_fb_good:
            self._pu_fb_good = good
            col = "92,255,176" if good else "255,200,87"
            self._pu_fb.setStyleSheet(f"QLabel {{ background: rgba(6,10,18,185); color: rgb({col}); border-radius: 14px; "
                                      f"border: 1px solid rgba({col},120); padding: 10px 16px; font-size: 20px; font-weight: 600; }}")
            self._pu_fb.setText(fb)
            if self._cam_full:
                self._place_cam_overlays()

    def shared_camera(self):
        """The one CameraHub (hands, Face ID, preview) — other features borrow its frames instead of opening the camera again."""
        try:
            hub = self._camera_hub()
            hub.max_size = self._cam_max
            return hub
        except Exception:                                  # noqa: BLE001
            return None

    def grab_camera_jpeg(self):
        hub = self.camera
        return hub.snapshot_jpeg() if (hub is not None and self._cam_run) else None

    def _cam_worker(self):
        """Camera preview: the frame is converted and shrunk to the panel's size here, in the worker, and handed over as a QImage
        (no JPEG encode + decode, no scaling on the GUI thread). A frame is only sent when the previous one has been shown."""
        hub = None
        try:
            import cv2
            try:
                hub = self._camera_hub()                       # one camera for everything: hand tracking, face lock and this preview
                hub.max_size = self._cam_max
                hub.preview = True
                hub.refresh()
            except Exception:                                  # noqa: BLE001
                hub = None
            if hub is not None:
                t0, last, started = time.time(), None, False
                while self._cam_run:
                    if self.isMinimized():                    # nobody can see the picture: don't convert frames for it
                        time.sleep(0.25)
                        continue
                    frame = hub.latest
                    if frame is None or frame is last:
                        if not started and not hub.running and time.time() - t0 > 3.0:
                            raise RuntimeError(hub.status if str(hub.status).startswith(("error", "missing")) else "the camera did not start")
                        time.sleep(0.02)
                        continue
                    last, started = frame, True
                    if not self._cam_pending:
                        tw, th = self._cam_px
                        h, w = frame.shape[:2]
                        k = min(max(tw / w, th / h) if self._cam_full else min(tw / w, th / h), 1.0)   # full screen: size for the crop that fills it
                        if k < 0.98:
                            frame = cv2.resize(frame, (max(1, int(w * k)), max(1, int(h * k))), interpolation=cv2.INTER_AREA)
                            h, w = frame.shape[:2]
                        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        self._cam_pending = True
                        self._cam_img_sig.emit(QImage(rgb.data, w, h, w * 3, QImage.Format.Format_RGB888).copy())
                    time.sleep(0.03)
                return
            cap = cv2.VideoCapture(0)
            while self._cam_run and cap.isOpened():
                ok, frame = cap.read()
                if ok and not self._cam_pending:
                    tw, th = self._cam_px
                    h, w = frame.shape[:2]
                    k = min(tw / w, th / h, 1.0)
                    if k < 0.98:
                        frame = cv2.resize(frame, (max(1, int(w * k)), max(1, int(h * k))), interpolation=cv2.INTER_AREA)
                        h, w = frame.shape[:2]
                    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    self._cam_pending = True
                    self._cam_img_sig.emit(QImage(rgb.data, w, h, w * 3, QImage.Format.Format_RGB888).copy())
                time.sleep(0.04)
            cap.release()
        except Exception as e:
            self._log_sig.emit(f"ERR: camera — {e}")
            self._cam_run = False
        finally:
            if hub is not None:
                hub.preview = False
                hub.latest = None
                try:
                    hub.refresh()                              # releases the camera when nothing else needs it
                except Exception:                              # noqa: BLE001
                    pass

    def _camera_image(self, img):
        self._cam_pending = False
        lbl = self._media_lbl
        self._cam_px = (max(160, lbl.width()), max(120, lbl.height()))
        if self._media.isVisible() and isinstance(img, QImage):
            lbl.set_frame(img, smooth=img.width() < lbl.width() - 2 and img.height() < lbl.height() - 2)   # same size: straight copy; bigger label: smooth upscale

    def _camera_frame(self, data: bytes):
        img = QImage.fromData(data)
        if not img.isNull() and self._media.isVisible():
            self._media_lbl.set_frame(img)

    def start_camera_stream(self):
        self._cam_stream_sig.emit(True)

    def stop_camera_stream(self):
        self._cam_run = False
        self._cam_stream_sig.emit(False)

    def show_phone_screen_frame(self, b64: str):
        self._phone_frame_sig.emit(b64)

    def _phone_frame(self, b64: str):
        try:
            img = QImage.fromData(base64.b64decode(b64))
        except Exception:
            return
        if img.isNull() or self._cam_run:
            return
        if not self._media.isVisible():
            self._media_title.setText("PHONE SCREEN")
            self._media.show()
            self.host.layout_floats()
            fade_in(self._media, 260)
        self._media_lbl.set_frame(img)
        self._media_timer.start(6000)

    def show_media_frame(self, data: bytes, title: str = ""):
        with self._media_lock:
            self._media_pending = (bytes(data or b""), str(title or ""))
            if self._media_queued:
                return
            self._media_queued = True
        self._media_frame_sig.emit(b"", "")

    def _on_media_frame(self, _d: bytes = b"", _t: str = ""):
        with self._media_lock:
            pending, self._media_pending = self._media_pending, None
            self._media_queued = False
        if pending is None:
            return
        data, title = pending
        if not data:
            if not self._cam_run:
                self._pushup_want_full = False
                self._hide_media()
            return
        if self._cam_run:
            return
        img = QImage.fromData(data)
        if img.isNull():
            return
        self._show_video_image(img, title)

    def _show_video_image(self, img, title: str):
        if not self._media.isVisible() or self._media_title.text() != (title or "VIDEO"):
            self._media_title.setText(title or "VIDEO")
            self._media.show()
            self.host.layout_floats()
        pushups = title in WORKOUT_TITLES
        if pushups and not self._pushup_on:
            self._pushup_on = True
            self.pushup_hud.reset()
            if self._pushup_want_full:
                self._pushup_want_full = False
                self._set_cam_full(True)
        self._set_media_scene(True)
        if not self._cam_full:
            self._front(self._media)
        if img.height():
            asp = img.width() / img.height()
            if abs(asp - self._wk_aspect) > 0.01:
                self._wk_aspect = asp
                if self._cam_full and self._pushup_on:
                    self._place_cam_overlays()
        self._media_lbl.set_frame(img, smooth=pushups, whole=pushups)
        self._media_timer.stop()  # video panel is closed by the player (empty frame) or the user, not by a timeout

    # ── mpv video: a native window floats over the media panel and mpv draws into it ──
    def start_mpv_window(self, title, result):
        self._mpv_open_sig.emit((title, result))

    def stop_mpv_window(self):
        self._mpv_close_sig.emit()

    def mpv_command(self, cmd: str):
        self._mpv_cmd_sig.emit(str(cmd))

    def _mpv_cmd(self, cmd: str):
        if cmd == "fs":
            self._toggle_media_full()
        elif cmd == "esc":
            self._exit_media_full()

    def _mpv_open(self, payload):
        title, result = payload
        try:
            self._mpv_close()
            vw = MpvWindow(self)
            vw.want_toggle.connect(self._toggle_media_full)
            vw.key_handler = self._video_key
            self._mpv_win = vw
            self._mpv_bar.reset()
            self._mpv_bar.show()
            ov = FsOverlay(vw)  # full-screen control bar (owned by the video window, so it stays on top of it)
            ov.bar.ctl.connect(self._video_ctl)
            ov.bar.seek_fraction.connect(self._mpv_seek_fraction)
            ov.bar.stop.connect(self._close_media_user)
            ov.bar.action.connect(self._vx_action)
            self._fs_overlay = ov
            self._fs_cursor = None
            self._fs_until = 0.0
            self.host.media_extra = 88
            fw = QApplication.focusWidget()
            if isinstance(fw, (QLineEdit, QTextEdit)):
                fw.clearFocus()  # so Space / arrows reach the video instead of a text box
            self.setFocus()
            self._media_title.setText(title or "VIDEO")
            self._media_lbl.clear()
            self._media_timer.stop()
            if not self._media.isVisible():
                self._media.show()
            self.host.layout_floats()
            self._front(self._media)
            self._set_media_scene(True)
            self._sync_mpv_geometry()
            vw.show()
            result["wid"] = int(vw.winId())
            self._mpv_timer.start(250)
        except Exception as exc:
            result["error"] = str(exc)
        finally:
            result["ready"].set()

    def _sync_mpv_geometry(self):
        vw = self._mpv_win
        if vw is None or vw.isFullScreen():
            return
        if self.isMinimized() or not self._media.isVisible():
            vw.hide()
            return
        lbl = self._media_lbl
        rect = QRect(lbl.mapToGlobal(QPoint(0, 0)), lbl.size())
        if vw.geometry() != rect:
            vw.setGeometry(rect)
        if not vw.isVisible():
            vw.show()

    def _mpv_close(self):
        self._mpv_timer.stop()
        if getattr(self, "_mpv_bar", None) is not None:
            self._mpv_bar.hide()
        ov, self._fs_overlay = getattr(self, "_fs_overlay", None), None
        if ov is not None:
            try:
                ov.hide()
            except Exception:
                pass
        self.host.media_extra = 0
        self._vx_overlay = self._vx_settings = None      # owned by the video window — closed with it
        self._vx_items = None
        vw, self._mpv_win = self._mpv_win, None
        if vw is not None:
            try:
                vw.close()
                vw.deleteLater()
            except Exception:
                pass

    # ── HUD video: info, "up next", like / dislike / subscribe, settings ──
    def media_info(self, meta: dict):
        self._media_info_sig.emit(dict(meta or {}))

    def media_suggestions(self, items: list):
        self._media_sugg_sig.emit(list(items or []))

    def _vx_bars(self):
        bars = [self._mpv_bar]
        ov = getattr(self, "_fs_overlay", None)
        if ov is not None:
            bars.append(ov.bar)
        return bars

    def _vx_apply_state(self, kind: str, on: bool):
        for bar in self._vx_bars():
            bar.set_state(kind, on)

    def _on_media_info(self, meta: dict):
        if self._mpv_win is None:
            return
        if meta.get("title"):
            self._media_title.setText(str(meta["title"])[:90])
        self._vx_meta = meta
        self._vx_rating = "none"
        self._vx_items = None                                   # new video → new suggestions are on their way
        for bar in self._vx_bars():
            bar.set_youtube(bool(meta.get("youtube")))
            for k in ("like", "dislike", "sub"):
                bar.set_state(k, False)
        if self._vx_overlay is not None:
            self._vx_overlay.set_items(None)
            if not meta.get("youtube"):
                self._vx_overlay.hide()

    def _on_media_suggestions(self, items: list):
        if self._mpv_win is None:
            return
        self._vx_items = items
        if self._vx_overlay is not None:
            self._vx_overlay.set_items(items)

    def _vx_action(self, kind: str):
        bar = self.sender()
        if kind in ("like", "dislike"):
            self._vx_rate(kind)
        elif kind == "sub":
            self._vx_subscribe()
        elif kind == "videos":
            self._vx_toggle_videos()
        elif kind == "settings":
            self._vx_open_settings(bar.settings_anchor() if hasattr(bar, "settings_anchor") else QCursor.pos())

    def _vx_toggle_videos(self):
        vw = self._mpv_win
        if vw is None:
            return
        if self._vx_overlay is None:
            self._vx_overlay = VideosOverlay(vw)
            self._vx_overlay.pick.connect(lambda url, title: self._video_ctl("play_url", (url, title)))
            self._vx_overlay.set_items(self._vx_items)
        ov = self._vx_overlay
        if ov.isVisible():
            ov.hide()
            return
        ov.setGeometry(vw.geometry())
        ov.show()
        ov.raise_()

    def _vx_sync_overlay(self):
        ov, vw = self._vx_overlay, self._mpv_win
        if ov is not None and vw is not None and ov.isVisible() and ov.geometry() != vw.geometry():
            ov.setGeometry(vw.geometry())

    def _vx_open_settings(self, anchor):
        vw = self._mpv_win
        if vw is None:
            return
        try:
            from core import youtube_actions as ya
            from actions.hud_video import get_prefs
            if self._vx_settings is None:
                self._vx_settings = PlayerSettings(vw)
                self._vx_settings.changed.connect(self._vx_setting)
            self._vx_settings.open_at(anchor, self._vx_meta or {}, get_prefs(), ya.is_connected())
        except Exception as e:
            self.write_log(f"ERR: player settings — {e}")

    def _vx_setting(self, cmd: str, value=None):
        if cmd == "yt_connect":
            self._vx_connect_youtube()
        else:
            self._video_ctl(cmd, value)

    def _vx_connect_youtube(self):
        def work():
            from core import youtube_actions as ya
            self._toast_sig.emit("A browser window opened — sign in to Google there…")
            self._toast_sig.emit(ya.connect())
        threading.Thread(target=work, daemon=True).start()

    def _vx_rate(self, kind: str):
        want = "none" if self._vx_rating == kind else kind          # pressing again removes the rating

        def work():
            try:
                from core import youtube_actions as ya
                from actions.hud_video import get_video_meta
                msg = ya.rate(get_video_meta().get("id", ""), want)
                if not msg.startswith("✗"):
                    self._vx_rating = want
                    self._vx_state_sig.emit(kind, want == kind)
                self._toast_sig.emit(msg)
            except Exception as e:
                self._toast_sig.emit(f"✗ {e}")
        self._toast_sig.emit("Talking to YouTube… (the first time a browser opens so you can sign in)")
        threading.Thread(target=work, daemon=True).start()

    def _vx_subscribe(self):
        def work():
            try:
                from core import youtube_actions as ya
                from actions.hud_video import get_video_meta
                m = get_video_meta()
                msg = ya.subscribe(m.get("id", ""), m.get("channel", ""))
                if not msg.startswith("✗"):
                    self._vx_state_sig.emit("sub", True)
                self._toast_sig.emit(msg)
            except Exception as e:
                self._toast_sig.emit(f"✗ {e}")
        self._toast_sig.emit("Talking to YouTube… (the first time a browser opens so you can sign in)")
        threading.Thread(target=work, daemon=True).start()

    # ── HUD video controls ──
    def _video_ctl(self, cmd: str, value=None):
        try:
            from actions.hud_video import video_control
            video_control(cmd, value)
        except Exception:
            pass

    def _mpv_seek_fraction(self, v: int):
        try:
            from actions.hud_video import get_video_state
            dur = get_video_state()["dur"]
        except Exception:
            return
        if dur > 0:
            self._video_ctl("seek_abs", dur * max(0, min(1000, v)) / 1000.0)

    def _video_key(self, e) -> bool:
        """Space = pause, ←/→ = 5 s, Shift+←/→ = 5 min, ↑/↓ = volume. True when the key was used."""
        mods = e.modifiers()
        if mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier):
            return False
        k = e.key()
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        if k == Qt.Key.Key_Space:
            if not e.isAutoRepeat():
                self._video_ctl("pause")
        elif k == Qt.Key.Key_Left:
            self._video_ctl("seek", -300 if shift else -5)
        elif k == Qt.Key.Key_Right:
            self._video_ctl("seek", 300 if shift else 5)
        elif k == Qt.Key.Key_Up:
            self._video_ctl("volume", 5)
        elif k == Qt.Key.Key_Down:
            self._video_ctl("volume", -5)
        else:
            return False
        return True

    def _mpv_tick(self):
        vw = self._mpv_win
        want = 100 if (vw is not None and vw.isFullScreen()) else 250       # the full-screen bar follows the mouse; otherwise 4 Hz is plenty
        if self._mpv_timer.interval() != want:                              # (the video window follows its panel at once, see layout_floats)
            self._mpv_timer.setInterval(want)
        self._sync_mpv_geometry()
        self._vx_sync_overlay()
        self._update_mpv_bar()

    def _update_mpv_bar(self):
        if self._mpv_win is None:
            return
        try:
            from actions.hud_video import get_video_state
            st = get_video_state()
        except Exception:
            return
        if self._mpv_bar.isVisible():
            self._mpv_bar.refresh(st)
        ov = getattr(self, "_fs_overlay", None)
        if ov is not None:
            self._update_fs_overlay(ov, st)

    def _update_fs_overlay(self, ov, st: dict):
        """Full screen: show the control bar while the mouse moves (or paused/buffering), hide it after 3 s."""
        vw = self._mpv_win
        if vw is None or not vw.isFullScreen() or not vw.isVisible():
            if ov.isVisible():
                ov.hide()
            return
        now = time.monotonic()
        pos = QCursor.pos()
        if pos != self._fs_cursor or (ov.isVisible() and ov.geometry().contains(pos)):
            self._fs_cursor = pos
            self._fs_until = now + 3.0
        if now < self._fs_until or st["paused"] or st["buffering"]:
            g = vw.geometry()
            w, h = max(520, min(g.width() - 80, 1180)), 104
            rect = QRect(g.x() + (g.width() - w) // 2, g.y() + g.height() - h - 40, w, h)
            if ov.geometry() != rect:
                ov.setGeometry(rect)
            if not ov.isVisible():
                ov.show()
                ov.raise_()
            ov.bar.refresh(st)
        elif ov.isVisible():
            ov.hide()

    def _close_media(self):
        if not self._cam_run:
            self._hide_media()

    def _hide_media(self):
        if self._cam_full:
            self._set_cam_full(False)
        self._pushup_on = False
        self._media_lbl.whole = False
        self.pushup_hud.hide()
        self.wk_left.hide()
        self.wk_right.hide()
        self._pu_fb.hide()
        self._mpv_close()
        self._set_media_scene(False)
        self._media.hide()

    def _toggle_media_full(self):
        vw = self._mpv_win
        if vw is None:
            if self._cam_run or self._pushup_on:
                self._set_cam_full(not self._cam_full)      # the live camera / push-ups: full window with the mini Jarvis in the corner
            return
        if vw.isFullScreen():
            vw.showNormal()
            self._sync_mpv_geometry()
        else:
            try:
                scr = self.screen() or QApplication.primaryScreen()
                if scr is not None:
                    vw.setGeometry(scr.geometry())
            except Exception:
                pass
            self._fs_until = time.monotonic() + 3.5
            vw.showFullScreen()
            vw.activateWindow()
            vw.setFocus()

    def _exit_media_full(self):
        vw = self._mpv_win
        if vw is not None and vw.isFullScreen():
            self._toggle_media_full()

    def _set_media_scene(self, on: bool):
        """Freeze the animated HUD background while a video plays; resume afterwards."""
        on = bool(on)
        if on == self._media_scene_paused:
            return
        self._media_scene_paused = on
        self._pause_scene(self.modal.is_open() or on)

    def _close_media_user(self):
        self._cam_run = False
        if self._pushup_on:                                    # closing the push-up view ends the workout (the camera must not stay on unseen)
            try:
                from actions.pushup_counter import stop_session
                stop_session()
            except Exception:
                pass
        try:
            from actions.hud_video import stop_hud_video
            stop_hud_video()
        except Exception:
            pass
        if self._mpv_win is not None:
            self._mpv_win.hide()
            self._set_media_scene(False)
            self._media.hide()
        else:
            self._hide_media()

    def show_confirm(self, title: str, detail: str = ""):
        self._confirm_sig.emit(title, detail)

    def hide_confirm(self):
        self._confirm_hide_sig.emit()

    def _show_confirm(self, title: str, detail: str):
        if self.modal.is_open():
            self.modal.dismiss()
        p = ConfirmPanel(title, detail)
        p.answered.connect(self._confirm_answer)
        self._confirm_panel = p
        self.modal.present(p, 460, 240, dismissable=False)

    def _confirm_answer(self, ok: bool):
        try:
            from core import confirm
            confirm.resolve(ok)
        except Exception as e:
            self.write_log(f"ERR: confirm — {e}")
        self._hide_confirm()

    def _hide_confirm(self):
        if self._confirm_panel is not None:
            self._confirm_panel = None
            self.modal.dismiss()

    def prompt_reconfig(self):
        self._reconfig_sig.emit()

    def _show_setup(self):
        self._ready = False
        detected = {"Darwin": "mac", "Windows": "windows"}.get(_OS, "linux")
        p = SetupPanel(detected)
        p.done.connect(self._setup_done)
        if self.modal.is_open():
            self.modal.dismiss()
            QTimer.singleShot(320, lambda: self.modal.present(p, 480, 420, dismissable=False))
        else:
            self.modal.present(p, 480, 420, dismissable=False)

    def _setup_done(self, key: str, os_key: str):
        try:
            from memory.config_manager import load_api_keys, save_api_keys
            save_api_keys(key)
            patch_cfg(os_system=os_key)
            try:
                from core.installer import install_for_config
                cfg = load_api_keys()
                threading.Thread(target=lambda: install_for_config(cfg, self.write_log), daemon=True).start()
            except Exception:
                pass
        except Exception as e:
            self.write_log(f"ERR: could not save key — {e}")
            return
        self.modal.dismiss()
        self._ready = True
        self.write_log("SYS: API key saved.")

    def reload_hotkeys(self):
        """Re-read the shortcuts after Settings changed them."""
        from ui.theme import hotkeys
        self._hk_map = {seq: act for act, seq in hotkeys().items()}

    def _hotkey_action(self, e):
        if not hasattr(self, "_hk_map"):
            self.reload_hotkeys()
        try:
            return self._hk_map.get(QKeySequence(e.keyCombination()).toString())
        except Exception:
            return None

    def keyPressEvent(self, e):
        k = e.key()
        hk = self._hotkey_action(e)
        if hk == "mute":
            self.toggle_mute()
        elif hk == "fullscreen":
            self.toggle_fullscreen()
        elif hk == "compact":
            self.enter_compact()
        elif self._mpv_win is not None and not self.modal.is_open() and self._video_key(e):
            pass
        elif k == Qt.Key.Key_Escape and self._cam_full:
            self._set_cam_full(False)
        elif k == Qt.Key.Key_Escape and self.host.media_full:
            self._exit_media_full()
        elif k == Qt.Key.Key_Escape and self.isFullScreen() and not self.modal.is_open():
            self.showNormal()
        elif (self._ptt_on and k == Qt.Key.Key_Space and e.modifiers() & Qt.KeyboardModifier.ControlModifier
              and not e.isAutoRepeat() and not self._ptt_down):
            self._ptt_down = True
            if self.ptt_hold:
                self.ptt_hold(True)
        else:
            super().keyPressEvent(e)

    def keyReleaseEvent(self, e):
        if self._ptt_down and not e.isAutoRepeat() and e.key() in (Qt.Key.Key_Space, Qt.Key.Key_Control):
            self._ptt_down = False
            if self.ptt_hold:
                self.ptt_hold(False)
        super().keyReleaseEvent(e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "modal"):
            self.modal.setGeometry(self.backdrop.rect())
        if getattr(self, "_lock_screen", None) is not None and self._locked:
            self._lock_screen.cover()
        if getattr(self, "_mpv_win", None) is not None:
            self._sync_mpv_geometry()
        if getattr(self, "_cam_full", False):
            self.host.layout_floats()

    def moveEvent(self, e):
        super().moveEvent(e)
        if getattr(self, "_lock_screen", None) is not None and self._locked:
            self._lock_screen.cover()
        if getattr(self, "_mpv_win", None) is not None:
            self._sync_mpv_geometry()

    def closeEvent(self, e):
        if getattr(self, "_lock_screen", None) is not None:
            self._lock_screen.close()
        if self.camera is not None:
            self.camera.stop()
        self._close_media_user()
        self._mpv_close()
        if self._modules.get("island") is not None:
            self._modules["island"].stop()
        if self._compact is not None:
            self._compact.close()
        super().closeEvent(e)
        QApplication.quit()


class _Root:

    def __init__(self, app: QApplication):
        self._app = app

    def mainloop(self):
        sys.exit(self._app.exec())


class JarvisUI:
    _FORWARD = {
        "on_text_command", "on_remote_clicked", "on_interrupt", "on_voice_change", "on_audio_device_change",
        "get_plugins", "get_plugin_settings", "on_wake_toggle", "on_wake_manual", "on_wake_install",
        "on_push_to_talk", "ptt_hold", "wake_get_state", "on_phone_command", "face_command",
    }

    def __init__(self):
        if QApplication.instance() is None and GL_ENABLED:
            QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
            try:
                QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_UseDesktopOpenGL)
            except Exception:
                pass
        object.__setattr__(self, "_app", QApplication.instance() or QApplication(sys.argv))
        self._app.setApplicationName("Jarvis")
        self._app.setFont(font(10))
        object.__setattr__(self, "_win", MainWindow())
        object.__setattr__(self, "root", _Root(self._app))
        self._extra: dict = {}
        icon = BASE_DIR / "config" / "jarvis.ico"
        if icon.exists():
            from PyQt6.QtGui import QIcon
            self._win.setWindowIcon(QIcon(str(icon)))
        self._win.show()

    def __setattr__(self, name, value):
        if name in self._FORWARD:
            setattr(self._win, name, value)
        else:
            object.__setattr__(self, name, value)

    def __getattr__(self, name):
        if name in ("_win", "_app", "root", "_extra"):
            raise AttributeError(name)
        if name in self._FORWARD:
            return getattr(self._win, name)
        if name in self._extra:
            return self._extra[name]
        raise AttributeError(name)

    @property
    def muted(self) -> bool:
        return self._win.muted

    @property
    def locked(self) -> bool:
        return self._win.locked

    @property
    def current_file(self):
        return self._win.current_file

    def set_phone_link_provider(self, fn):
        self._win.phone_page.set_link_provider(fn)

    def wait_for_api_key(self):
        while not self._win._ready:
            time.sleep(0.3)

    def write_log(self, text: str):
        self._win.write_log(text)

    def set_state(self, s: str):
        self._win.set_state(s)

    def set_audio_level(self, level: float):
        self._win.stage.set_audio_level(level)
        if self._win._cam_full:
            self._win.mini.push(float(level))
        if self._win._compact is not None:
            self._win._compact.amp = max(0.0, min(1.0, float(level)))

    def push_visemes(self, frames, hop: float, at: float):
        self._win.stage.push_visemes(frames, hop, at)

    def show_content(self, title: str, text: str):
        self._win.show_content(title, text)

    def start_camera_stream(self):
        self._win.start_camera_stream()

    def stop_camera_stream(self):
        self._win.stop_camera_stream()

    def camera_fullscreen(self, on: bool):
        self._win.camera_fullscreen(on)

    def notify(self, text: str):
        """A short on-screen message (toast) from any thread."""
        self._win._toast_sig.emit(str(text))

    def show_pushup_stats(self, d: dict):
        self._win.pushup_stats(d)

    def pushup_fullscreen(self, on: bool):
        self._win.pushup_fullscreen(on)

    def pushup_hud_active(self) -> bool:
        return self._win.pushup_hud_active()

    def celebrate(self, title: str, big: str, sub: str = ""):
        self._win.celebrate(title, big, sub)

    def camera_is_open(self) -> bool:
        return self._win.camera_is_open()

    def grab_camera_jpeg(self):
        return self._win.grab_camera_jpeg()

    def shared_camera(self):
        return self._win.shared_camera()

    def show_phone_screen_frame(self, b64: str):
        self._win.show_phone_screen_frame(b64)

    def show_media_frame(self, data: bytes, title: str = ""):
        self._win.show_media_frame(data, title)

    def media_info(self, meta):
        self._win.media_info(meta)

    def media_suggestions(self, items):
        self._win.media_suggestions(items)

    def start_mpv_window(self, title, result):
        self._win.start_mpv_window(title, result)

    def stop_mpv_window(self):
        self._win.stop_mpv_window()

    def mpv_command(self, cmd):
        self._win.mpv_command(cmd)

    def notify_phone_connected(self):
        self._win.notify_phone_connected()

    def phone_event(self, event):
        self._win.phone_event(event)

    def show_confirm(self, title: str, detail: str = ""):
        self._win.show_confirm(title, detail)

    def hide_confirm(self):
        self._win.hide_confirm()

    def prompt_reconfig(self):
        self._win.prompt_reconfig()

    def apply_profile(self, name: str):
        self._win._profile_sig.emit(str(name))

    def refresh_tasks(self):
        self._win._tasks_sig.emit()

    def restart_app(self):
        self._win._restart_sig.emit()

    def module_command(self, name: str, cmd: str = "toggle"):
        self._win.module_command(name, cmd)

    def module_visible(self, name: str) -> bool:
        ov = self._win._modules.get(str(name))
        return bool(ov is not None and ov.isVisible())

    def set_hand_cursor(self, on: bool):
        self._win._module_sig.emit("hand_cursor", "on" if on else "off")

    def quit_app(self):
        self._win._quit_sig.emit()

#by ASDcuber

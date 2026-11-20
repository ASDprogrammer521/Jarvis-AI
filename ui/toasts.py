"""One notification system for the whole app.

Every message goes through `ToastCenter.push(text)`: it slides up from the bottom of the page area, stacks above the
previous ones (at most four at a time — the oldest fades out), an identical message that is still on screen is not added a
second time (its timer restarts instead), and each one has a colour by meaning (success / warning / error / info).

Painted by hand, no QGraphicsEffect and no QSS per toast, so it stays cheap next to the 3D scene behind it."""
from __future__ import annotations

from PyQt6.QtCore import QEasingCurve, QObject, QPoint, QPointF, QPropertyAnimation, QRect, QRectF, Qt, QTimer, QVariantAnimation
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PyQt6.QtWidgets import QWidget

from ui.theme import C, T, qcol
from ui.widgets import font

MAX_VISIBLE = 4
GAP = 8
BOTTOM_MARGIN = 92          # keeps the stack clear of the input bar
MAX_W = 520

_OK = ("✅", "📸", "🔓", "👋", "✔", "✓", "🎉", "🏆")
_WARN = ("⚠", "❌", "✖", "Couldn't", "Could not", "failed", "Failed", "error", "Error")
_INFO = ("⬆", "ℹ", "🔄", "Checking")


def kind_of(text: str) -> str:
    t = str(text)
    if any(t.startswith(p) for p in _OK):
        return "ok"
    if any(t.startswith(p) or p in t[:40] for p in _WARN):
        return "warn"
    if any(t.startswith(p) for p in _INFO):
        return "info"
    return "plain"


def _accent(kind: str) -> str:
    return {"ok": C.GREEN, "warn": C.AMBER, "error": C.RED, "info": C.ACC}.get(kind, C.PRI)


class _Toast(QWidget):
    def __init__(self, parent, text: str, kind: str):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.text, self.kind = text, kind
        self._a = 0.0                                   # 0 → 1 while it appears, 1 → 0 while it leaves
        self._f = font(T.F_LABEL, QFont.Weight.DemiBold)
        self._pulse = 0.0
        self._measure()
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.anim = QVariantAnimation(self)
        self.anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.anim.valueChanged.connect(self._on_a)
        self.pulse_anim = QVariantAnimation(self)
        self.pulse_anim.setStartValue(1.0)
        self.pulse_anim.setEndValue(0.0)
        self.pulse_anim.setDuration(420)
        self.pulse_anim.valueChanged.connect(self._on_pulse)
        self.leaving = False
        self.hide()

    def _measure(self):
        fm = QFontMetrics(self._f)
        inner = MAX_W - 2 * T.S4 - 10
        r = fm.boundingRect(QRect(0, 0, inner, 400), int(Qt.TextFlag.TextWordWrap), self.text)
        w = min(MAX_W, r.width() + 2 * T.S4 + 10 + 4)
        h = max(38, r.height() + 2 * T.S3 + 2)
        self.setFixedSize(w, h)

    def _on_a(self, v):
        self._a = float(v)
        self.update()

    def _on_pulse(self, v):
        self._pulse = float(v)
        self.update()

    def flash(self):
        self.pulse_anim.stop()
        self.pulse_anim.start()

    def fade(self, to: float, ms: int, done=None):
        self.anim.stop()
        self.anim.setStartValue(self._a)
        self.anim.setEndValue(to)
        self.anim.setDuration(ms)
        try:
            self.anim.finished.disconnect()
        except TypeError:
            pass
        if done is not None:
            self.anim.finished.connect(done)
        self.anim.start()

    def paintEvent(self, _):
        if self._a <= 0.01:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setOpacity(min(1.0, self._a))
        W, H = self.width(), self.height()
        col = qcol(_accent(self.kind))
        # dark glass card with a thin tinted edge; the left bar carries the meaning
        p.setPen(QPen(QColor(col.red(), col.green(), col.blue(), int(90 + 110 * self._pulse)), 1.2))
        p.setBrush(QColor(10, 14, 26, 232))
        p.drawRoundedRect(QRectF(0.6, 0.6, W - 1.2, H - 1.2), T.R_MD, T.R_MD)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(QRectF(T.S3, T.S3, 3.5, H - 2 * T.S3), 1.75, 1.75)
        p.setFont(self._f)
        p.setPen(qcol(C.TEXT))
        p.drawText(QRectF(T.S3 + 12, 0, W - 2 * T.S4 - 8, H), int(Qt.AlignmentFlag.AlignVCenter.value) | int(Qt.AlignmentFlag.AlignLeft.value) | int(Qt.TextFlag.TextWordWrap.value), self.text)


class ToastCenter(QObject):
    def __init__(self, parent_widget: QWidget, area_fn, raise_fn=None):
        """parent_widget: where toasts live (the window's backdrop); area_fn() -> QRect of the page area, in its coordinates;
        raise_fn(w): brings a toast above the pages (and below the sidebar)."""
        super().__init__(parent_widget)
        self._parent = parent_widget
        self._area = area_fn
        self._raise = raise_fn
        self._items: list[_Toast] = []                  # oldest first; the last one is at the bottom of the stack

    # ── public ──
    def push(self, text: str, kind: str | None = None, ms: int | None = None):
        text = " ".join(str(text).split())
        if not text:
            return
        for t in self._items:                           # same message still showing: don't stack it again
            if t.text == text and not t.leaving:
                t.timer.start(self._life(text, ms))
                t.flash()
                return
        t = _Toast(self._parent, text, kind or kind_of(text))
        t.timer.timeout.connect(lambda tt=t: self._dismiss(tt))
        self._items.append(t)
        live = [x for x in self._items if not x.leaving]
        while len(live) > MAX_VISIBLE:                  # too many: the oldest leaves at once
            self._dismiss(live.pop(0), fast=True)
        t.show()
        self._lift(t)
        self.relayout(new=t)
        t.fade(1.0, T.D_BASE)
        t.timer.start(self._life(text, ms))

    def relayout(self, new: _Toast | None = None):
        a = self._area()
        if a is None:
            return
        live = [x for x in self._items if not x.leaving]
        y = a.y() + a.height() - BOTTOM_MARGIN
        for t in reversed(live):                        # newest at the bottom, older ones above it
            y -= t.height()
            target = QPoint(a.x() + (a.width() - t.width()) // 2, y)
            if t is new:
                t.move(target.x(), target.y() + 18)     # slides up into place
            self._glide(t, target)
            y -= GAP

    # ── internals ──
    @staticmethod
    def _life(text: str, ms: int | None) -> int:
        return ms if ms else min(6500, 2800 + 40 * len(text))

    def _lift(self, t: _Toast):
        if self._raise is not None:
            self._raise(t)
        else:
            t.raise_()

    @staticmethod
    def _glide(t: _Toast, target: QPoint):
        if t.pos() == target:
            return
        a = QPropertyAnimation(t, b"pos", t)
        a.setDuration(T.D_BASE)
        a.setEndValue(target)
        a.setEasingCurve(QEasingCurve.Type.OutCubic)
        a.start()
        t._glide_anim = a

    def _dismiss(self, t: _Toast, fast: bool = False):
        if t.leaving:
            return
        t.leaving = True
        t.timer.stop()

        def gone():
            t.hide()
            t.deleteLater()
            if t in self._items:
                self._items.remove(t)
            self.relayout()

        t.fade(0.0, 90 if fast else T.D_FAST, gone)
        self.relayout()

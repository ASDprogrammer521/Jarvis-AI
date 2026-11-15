"""Hand Space page — the home of the Iron-Man style workspace: launch it, see what each gesture does, choose the folder
the glass cards come from, design a new card (colour + icon + note), and decide whether a hand outline is drawn."""
from __future__ import annotations

import math
import os
import random
import time
from pathlib import Path

from PyQt6.QtCore import QEasingCurve, QPoint, QPointF, QRectF, QSize, Qt, QTimer, QVariantAnimation, pyqtSignal
from PyQt6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QImageReader, QLinearGradient, QPainter, QPainterPath, QPen,
                         QPixmap, QRadialGradient, QTextOption)
from PyQt6.QtWidgets import (QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
                             QScrollArea, QVBoxLayout, QWidget)

from ui.fx import GlassCard, GlowButton, Toggle
from ui.pages import PageHeader
from ui.theme import C, patch_cfg, qcol, read_cfg, rgba
from ui.widgets import draw_icon, font

GESTURES = (
    ("pinch", "Pinch", "Touch thumb and index finger on a card to grab it and carry it."),
    ("tap", "Tap", "A quick pinch goes into a folder or drive, enlarges a picture, opens a note. Pinch again to close it."),
    ("throw", "Throw", "Let go while your hand moves — the card flies, spins and bounces off the edges."),
    ("claw", "Claw", "Bend your fingers like a claw: every card is pulled to your palm. Open the hand to release."),
    ("two", "Two hands", "Pinch an open picture with both hands to zoom it; one hand drags it around."),
    ("fist", "Back", "Hold a fist for a second: closes the open card, or goes up one folder."),
)
_ICON = {"pinch": "hand", "tap": "bolt", "throw": "send", "claw": "hand", "two": "cube", "fist": "stats"}
_NOTE_COLORS = ("#6ee7ff", "#4d8dff", "#b388ff", "#ff7ad9", "#ff5c7a", "#ff9e4a", "#ffc857", "#5cffb0")
_NOTE_ICONS = ("📝", "⭐", "💡", "🎯", "📌", "🔥", "🎬", "🎵", "💼", "🛒", "🏠", "🚀")
_AL = Qt.AlignmentFlag


def _gesture_color(key: str) -> str:
    return {"pinch": C.PRI, "tap": C.ACC, "throw": C.ACC2, "claw": C.AMBER, "two": C.GREEN, "fist": C.RED}.get(key, C.PRI)


def _mixc(a, b, t: float) -> QColor:
    a, b = QColor(a), QColor(b)
    t = max(0.0, min(1.0, t))
    return QColor(int(a.red() + (b.red() - a.red()) * t), int(a.green() + (b.green() - a.green()) * t),
                  int(a.blue() + (b.blue() - a.blue()) * t), int(a.alpha() + (b.alpha() - a.alpha()) * t))


def _meta(e: dict) -> tuple[str, str, str]:
    """(label, accent colour, glyph) of a card — the same colours the overlay uses, so the page previews it faithfully."""
    kind = e.get("kind", "file")
    if kind == "text":
        return "NOTE", e.get("accent") or C.ACC, "doc"
    return {"drive": ("DRIVE", C.PRI, "drive"), "folder": ("FOLDER", C.AMBER, "folder"), "image": ("PICTURE", C.PRI, "image"),
            "model": ("3D MODEL", C.ACC, "cube"), "video": ("VIDEO", C.ACC2, "film")}.get(kind, ("FILE", C.TEXT_MED, "doc"))


def _glyph(p: QPainter, kind: str, r: QRectF, col: QColor, wd: float = 1.7):
    p.setPen(QPen(col, wd, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.setBrush(Qt.BrushStyle.NoBrush)
    x, y, w, h = r.x(), r.y(), r.width(), r.height()
    cx, cy = r.center().x(), r.center().y()
    if kind == "folder":
        path = QPainterPath()
        path.moveTo(x, y + h * 0.34)
        path.lineTo(x, y + h * 0.12)
        path.lineTo(x + w * 0.38, y + h * 0.12)
        path.lineTo(x + w * 0.52, y + h * 0.3)
        p.drawPath(path)
        p.drawRoundedRect(QRectF(x, y + h * 0.3, w, h * 0.64), 3, 3)
    elif kind == "drive":
        p.drawRoundedRect(QRectF(x, y + h * 0.22, w, h * 0.56), 4, 4)
        p.drawLine(QPointF(x + w * 0.18, cy + h * 0.1), QPointF(x + w * 0.5, cy + h * 0.1))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawEllipse(QPointF(x + w * 0.8, cy + h * 0.1), 1.6, 1.6)
    elif kind == "image":
        p.drawRoundedRect(QRectF(x, y + h * 0.1, w, h * 0.8), 3, 3)
        path = QPainterPath()
        path.moveTo(x + 2, y + h * 0.82)
        path.lineTo(x + w * 0.38, cy)
        path.lineTo(x + w * 0.58, cy + h * 0.2)
        path.lineTo(x + w * 0.74, cy - h * 0.06)
        path.lineTo(x + w - 2, y + h * 0.82)
        p.drawPath(path)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawEllipse(QPointF(x + w * 0.28, y + h * 0.32), 1.6, 1.6)
    elif kind == "cube":
        from PyQt6.QtGui import QPolygonF
        p.drawPolygon(QPolygonF([QPointF(cx, y), QPointF(x + w, y + h * 0.25), QPointF(x + w, y + h * 0.75),
                                 QPointF(cx, y + h), QPointF(x, y + h * 0.75), QPointF(x, y + h * 0.25)]))
        p.drawLine(QPointF(cx, cy), QPointF(x, y + h * 0.25))
        p.drawLine(QPointF(cx, cy), QPointF(x + w, y + h * 0.25))
        p.drawLine(QPointF(cx, cy), QPointF(cx, y + h))
    elif kind == "film":
        from PyQt6.QtGui import QPolygonF
        p.drawRoundedRect(QRectF(x, y + h * 0.1, w, h * 0.8), 3, 3)
        p.setBrush(col)
        p.drawPolygon(QPolygonF([QPointF(cx - w * 0.12, cy - h * 0.2), QPointF(cx - w * 0.12, cy + h * 0.2), QPointF(cx + w * 0.2, cy)]))
    else:                                                               # "doc"
        path = QPainterPath()
        path.moveTo(x + w * 0.12, y)
        path.lineTo(x + w * 0.62, y)
        path.lineTo(x + w * 0.88, y + h * 0.26)
        path.lineTo(x + w * 0.88, y + h)
        path.lineTo(x + w * 0.12, y + h)
        path.closeSubpath()
        p.drawPath(path)
        p.drawLine(QPointF(x + w * 0.3, y + h * 0.5), QPointF(x + w * 0.7, y + h * 0.5))
        p.drawLine(QPointF(x + w * 0.3, y + h * 0.7), QPointF(x + w * 0.6, y + h * 0.7))


def _chip(p: QPainter, r: QRectF, accent, glyph: str, emoji: str = ""):
    """A rounded badge with the card's icon (a line glyph, or an emoji the user picked) in the card's colour."""
    acc = QColor(accent)
    g = QLinearGradient(r.topLeft(), r.bottomRight())
    g.setColorAt(0, qcol(acc.name(), 96))
    g.setColorAt(1, qcol(acc.name(), 22))
    rad = r.width() * 0.3
    p.setBrush(QBrush(g))
    p.setPen(QPen(qcol(acc.name(), 150), 1.1))
    p.drawRoundedRect(r, rad, rad)
    if emoji:
        f = QFont("Segoe UI Emoji")
        f.setPixelSize(max(10, int(r.width() * 0.48)))
        p.setFont(f)
        p.setPen(QColor("#ffffff"))
        p.drawText(r, _AL.AlignCenter, emoji)
    else:
        pad = r.width() * 0.27
        _glyph(p, glyph, r.adjusted(pad, pad, -pad, -pad), _mixc(acc, "#ffffff", 0.45), max(1.4, r.width() * 0.05))


def _thumb(path: str, w: int = 190, h: int = 200) -> QPixmap | None:
    """A small picture for an image card (decoded at the target size, so a big photo folder stays cheap)."""
    try:
        if os.path.getsize(path) > 60 * 1024 * 1024:
            return None
        rd = QImageReader(path)
        rd.setAutoTransform(True)
        sz = rd.size()
        if not sz.isValid() or sz.isEmpty():
            return None
        k = min(1.0, max(w / sz.width(), h / sz.height()))
        rd.setScaledSize(QSize(max(1, int(sz.width() * k)), max(1, int(sz.height() * k))))
        img = rd.read()
        return None if img.isNull() else QPixmap.fromImage(img)
    except Exception:                                                  # noqa: BLE001 - a broken picture just shows no thumbnail
        return None


# ── hero ─────────────────────────────────────────────────────────────────────────────
class Hero(QWidget):
    """Animated hologram: a wireframe orb in a starfield with three glass cards orbiting it, and the headline beside it."""

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(250)
        self._t0 = time.time()
        self._tm = QTimer(self)
        self._tm.timeout.connect(self.update)
        rnd = random.Random(7)
        self._stars = [(rnd.random(), rnd.random(), rnd.uniform(0.6, 1.8), rnd.uniform(0, 6.28)) for _ in range(70)]

    def showEvent(self, e):
        super().showEvent(e)
        self._tm.start(33)

    def hideEvent(self, e):
        self._tm.stop()
        super().hideEvent(e)

    @staticmethod
    def _card(p: QPainter, x: float, y: float, depth: float, col: str, glyph: str):
        p.save()
        p.translate(x, y)
        s = 0.78 + 0.3 * depth
        p.scale(s, s)
        p.setOpacity(0.5 + 0.5 * depth)
        r = QRectF(-56, -36, 112, 72)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 50))
        p.drawRoundedRect(r.adjusted(-3, 5, 3, 8), 15, 15)
        g = QLinearGradient(r.topLeft(), r.bottomLeft())
        g.setColorAt(0, qcol(C.PANEL2, 228))
        g.setColorAt(1, qcol(C.PANEL, 240))
        p.setBrush(QBrush(g))
        p.drawRoundedRect(r, 14, 14)
        t = QLinearGradient(r.topLeft(), r.bottomRight())
        t.setColorAt(0, qcol(col, 56))
        t.setColorAt(1, qcol(col, 0))
        p.setBrush(QBrush(t))
        p.drawRoundedRect(r, 14, 14)
        rim = QLinearGradient(r.topLeft(), r.bottomRight())
        rim.setColorAt(0, QColor(255, 255, 255, 120))
        rim.setColorAt(1, qcol(col, 150))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QBrush(rim), 1.2))
        p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 14, 14)
        _chip(p, QRectF(-46, -26, 28, 28), col, glyph)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 120))
        p.drawRoundedRect(QRectF(-10, -20, 44, 5), 2.5, 2.5)
        p.setBrush(QColor(255, 255, 255, 60))
        p.drawRoundedRect(QRectF(-10, -9, 30, 4), 2, 2)
        p.drawRoundedRect(QRectF(-46, 10, 84, 4), 2, 2)
        p.drawRoundedRect(QRectF(-46, 20, 52, 4), 2, 2)
        p.restore()

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        t = time.time() - self._t0
        W, H = self.width(), self.height()
        p.setPen(Qt.PenStyle.NoPen)
        for sx, sy, sz, ph in self._stars:                                    # twinkling starfield
            a = 40 + 80 * (0.5 + 0.5 * math.sin(t * 1.2 + ph))
            p.setBrush(qcol(C.PRI if sz > 1.4 else "#ffffff", int(a)))
            p.drawEllipse(QPointF(sx * W, sy * H), sz, sz)
        cx, cy = W * 0.70, H * 0.5
        r = max(30.0, min(H * 0.27, W * 0.11))
        halo = QRadialGradient(QPointF(cx, cy), r * 2.8)
        halo.setColorAt(0, qcol(C.PRI, 74))
        halo.setColorAt(0.5, qcol(C.ACC, 24))
        halo.setColorAt(1, qcol(C.PRI, 0))
        p.setBrush(QBrush(halo))
        p.drawEllipse(QPointF(cx, cy), r * 2.8, r * 2.8)
        items = []
        for i, (col, g) in enumerate(((C.AMBER, "folder"), (C.PRI, "image"), (C.ACC, "doc"))):
            a = t * 0.55 + i * 2.094
            items.append(((math.sin(a) + 1) / 2, cx + math.cos(a) * r * 2.3, cy + math.sin(a) * r * 0.7 + math.sin(t * 1.4 + i) * 4, col, g))
        items.sort()
        for it in items:
            if it[0] < 0.5:                                                    # cards behind the orb
                self._card(p, it[1], it[2], it[0], it[3], it[4])
        sph = QRadialGradient(QPointF(cx - r * 0.3, cy - r * 0.35), r * 1.5)
        sph.setColorAt(0, qcol(C.PRI, 96))
        sph.setColorAt(0.6, qcol(C.PRI_DIM, 34))
        sph.setColorAt(1, qcol(C.BG, 150))
        p.setBrush(QBrush(sph))
        p.setPen(QPen(qcol(C.PRI, 180), 1.3))
        p.drawEllipse(QPointF(cx, cy), r, r)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for k in range(5):                                                     # meridians: ellipses squeezed by the rotation angle
            ang = t * 0.7 + k * math.pi / 5
            p.setPen(QPen(qcol(C.PRI, 140), 1.2))
            p.drawEllipse(QPointF(cx, cy), max(2.0, abs(math.cos(ang)) * r), r)
        for lat in (-0.6, -0.3, 0.0, 0.3, 0.6):                               # parallels
            half = math.sqrt(max(0.0, 1 - lat * lat)) * r
            p.setPen(QPen(qcol(C.PRI, 100), 1.0))
            p.drawEllipse(QPointF(cx, cy + lat * r), half, half * 0.22)
        k = (t * 0.45) % 1.0                                                   # a pulse ring leaving the orb
        p.setPen(QPen(qcol(C.PRI, int(120 * (1 - k))), 1.4))
        p.drawEllipse(QPointF(cx, cy), r * (1 + 0.9 * k), r * (1 + 0.9 * k))
        p.setPen(QPen(qcol(C.ACC, 64), 1.0, Qt.PenStyle.DashLine))
        p.drawEllipse(QPointF(cx, cy), r * 2.3, r * 0.7)
        for it in items:
            if it[0] >= 0.5:                                                   # cards in front of it
                self._card(p, it[1], it[2], it[0], it[3], it[4])
        tx, tw = 30.0, max(180.0, W * 0.42)                                    # headline, line and three chips
        p.setFont(font(21, QFont.Weight.Bold))
        p.setPen(qcol(C.TEXT))
        p.drawText(QRectF(tx, H * 0.5 - 70, tw, 38), _AL.AlignLeft | _AL.AlignVCenter, "Your files, in the air.")
        opt = QTextOption(_AL.AlignLeft | _AL.AlignTop)
        opt.setWrapMode(QTextOption.WrapMode.WordWrap)
        p.setFont(font(10))
        p.setPen(qcol(C.TEXT_MED))
        p.drawText(QRectF(tx, H * 0.5 - 26, tw, 50), "Pinch, throw and pull glass cards with bare hands — webcam only, nothing leaves this computer.", opt)
        x = tx
        p.setFont(font(8, QFont.Weight.Bold))
        fm = QFontMetrics(p.font())
        for label, col in (("WEBCAM ONLY", C.PRI), ("ALL LOCAL", C.GREEN), ("6 GESTURES", C.ACC)):
            w = fm.horizontalAdvance(label) + 26
            pill = QRectF(x, H * 0.5 + 38, w, 22)
            p.setPen(QPen(qcol(col, 130), 1))
            p.setBrush(qcol(col, 30))
            p.drawRoundedRect(pill, 11, 11)
            p.setPen(qcol(col))
            p.drawText(pill, _AL.AlignCenter, label)
            x += w + 8


# ── status pill ──────────────────────────────────────────────────────────────────────
class StatusPill(QWidget):
    """`● Hand Space is open` — a small capsule whose dot pulses while the workspace is live."""

    def __init__(self):
        super().__init__()
        self.setFixedHeight(30)
        self._text, self._col, self._live = "", C.TEXT_DIM, False
        self._tm = QTimer(self)
        self._tm.timeout.connect(self.update)

    def set(self, text: str, col: str, live: bool = False):
        self._text, self._col, self._live = text, col, live
        self.setFixedWidth(QFontMetrics(font(10, QFont.Weight.DemiBold)).horizontalAdvance(text) + 52)
        if live and not self._tm.isActive():
            self._tm.start(60)
        elif not live:
            self._tm.stop()
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(qcol(self._col, 110), 1))
        p.setBrush(qcol(self._col, 26))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        dot = QPointF(r.left() + 17, r.center().y())
        pulse = 0.5 + 0.5 * math.sin(time.time() * 4) if self._live else 0.0
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qcol(self._col, int(40 + 60 * pulse)))
        p.drawEllipse(dot, 7 + 2 * pulse, 7 + 2 * pulse)
        p.setBrush(qcol(self._col))
        p.drawEllipse(dot, 3.5, 3.5)
        p.setFont(font(10, QFont.Weight.DemiBold))
        p.setPen(qcol(self._col))
        p.drawText(QRectF(r.left() + 32, r.top(), r.width() - 38, r.height()), _AL.AlignLeft | _AL.AlignVCenter, self._text)


# ── a card tile: how a card will look in Hand Space ──────────────────────────────────
class CardTile(QWidget):
    """A glass tile that previews one Hand Space card: its own colour and icon, kind tag, a peek at the content
    (note text, drive usage, picture thumbnail). It lifts and glows when the pointer is over it."""

    def __init__(self, e: dict, thumb: QPixmap | None = None, parent=None, interactive: bool = True):
        super().__init__(parent)
        self.e, self.thumb = e, thumb
        self.setFixedHeight(120)
        self._h = 0.0
        self._interactive = interactive
        self._an = QVariantAnimation(self)
        self._an.setDuration(170)
        self._an.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._an.valueChanged.connect(self._set_h)
        if interactive:
            self.setToolTip(str(e.get("path") or e.get("title") or ""))

    def set_entry(self, e: dict):
        self.e = e
        self.update()

    def _set_h(self, v):
        self._h = float(v)
        self.update()

    def _go(self, v: float):
        if not self._interactive:
            return
        self._an.stop()
        self._an.setStartValue(self._h)
        self._an.setEndValue(v)
        self._an.start()

    def enterEvent(self, ev):
        self._go(1.0)
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self._go(0.0)
        super().leaveEvent(ev)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        e, h = self.e, self._h
        label, col, glyph = _meta(e)
        acc = QColor(col)
        rad = 17.0
        r = QRectF(self.rect()).adjusted(3, 6 - 3 * h, -3, -4 - 3 * h)
        p.setPen(Qt.PenStyle.NoPen)
        for i, al in enumerate((30, 16)):                                    # shadow, deeper while hovered
            g = 3 + i * 5
            p.setBrush(QColor(0, 0, 0, int(al * (0.8 + 0.6 * h))))
            p.drawRoundedRect(r.adjusted(-g, -g + 7, g, g + 7), rad + g, rad + g)
        fill = QLinearGradient(r.topLeft(), r.bottomLeft())
        fill.setColorAt(0, qcol(C.PANEL2, 238))
        fill.setColorAt(1, qcol(C.PANEL, 245))
        p.setBrush(QBrush(fill))
        p.drawRoundedRect(r, rad, rad)
        tint = QLinearGradient(r.topLeft(), r.bottomRight())
        tint.setColorAt(0, qcol(acc.name(), int(40 + 36 * h)))
        tint.setColorAt(0.7, qcol(acc.name(), 6))
        tint.setColorAt(1, qcol(acc.name(), 0))
        p.setBrush(QBrush(tint))
        p.drawRoundedRect(r, rad, rad)
        rim = QLinearGradient(r.topLeft(), r.bottomRight())
        rim.setColorAt(0, QColor(255, 255, 255, int(86 + 80 * h)))
        rim.setColorAt(0.5, QColor(255, 255, 255, 22))
        rim.setColorAt(1, qcol(acc.name(), int(90 + 130 * h)))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QBrush(rim), 1.2))
        p.drawRoundedRect(r.adjusted(0.6, 0.6, -0.6, -0.6), rad - 0.6, rad - 0.6)
        ln = QLinearGradient(r.left() + rad, 0, r.right() - rad, 0)
        ln.setColorAt(0, qcol(acc.name(), 0))
        ln.setColorAt(0.5, qcol(acc.name(), 230))
        ln.setColorAt(1, qcol(acc.name(), 0))
        p.setPen(QPen(QBrush(ln), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(r.left() + rad, r.top() + 0.9), QPointF(r.right() - rad, r.top() + 0.9))

        right = r.right() - 14
        if self.thumb is not None and not self.thumb.isNull():              # picture cards: the picture itself on the right
            tr = QRectF(r.right() - 96, r.top() + 12, 84, r.height() - 24)
            clip = QPainterPath()
            clip.addRoundedRect(tr, 12, 12)
            p.save()
            p.setClipPath(clip)
            iw, ih = self.thumb.width(), self.thumb.height()
            k = max(tr.width() / iw, tr.height() / ih)
            sw, sh = tr.width() / k, tr.height() / k
            p.drawPixmap(tr, self.thumb, QRectF((iw - sw) / 2, (ih - sh) / 2, sw, sh))
            p.restore()
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(255, 255, 255, 60), 1))
            p.drawRoundedRect(tr, 12, 12)
            right = tr.left() - 10
        _chip(p, QRectF(r.left() + 14, r.top() + 14, 38, 38), acc, glyph, str(e.get("icon") or ""))
        tx = r.left() + 62
        title = str(e.get("title") or "Untitled")
        p.setFont(font(11, QFont.Weight.DemiBold))
        p.setPen(qcol(C.TEXT))
        p.drawText(QRectF(tx, r.top() + 12, right - tx, 22), _AL.AlignLeft | _AL.AlignVCenter,
                   QFontMetrics(p.font()).elidedText(title, Qt.TextElideMode.ElideMiddle, int(right - tx)))
        p.setFont(font(7, QFont.Weight.Bold))                                # kind tag
        fm = QFontMetrics(p.font())
        tw = fm.horizontalAdvance(label) + 16
        tag = QRectF(tx, r.top() + 36, tw, 16)
        p.setPen(QPen(qcol(acc.name(), 130), 1))
        p.setBrush(qcol(acc.name(), 30))
        p.drawRoundedRect(tag, 8, 8)
        p.setPen(_mixc(acc, "#ffffff", 0.3))
        p.drawText(tag, _AL.AlignCenter, label)
        body = QRectF(r.left() + 16, r.top() + 62, right - r.left() - 16, r.height() - 72)
        if e.get("kind") == "drive":                                         # usage bar
            used = max(0.0, min(1.0, float(e.get("used", 0.0) or 0.0)))
            warn = QColor(C.RED if used > 0.9 else C.AMBER if used > 0.75 else C.PRI)
            bar = QRectF(body.left(), body.top() + 4, body.width(), 7)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 26))
            p.drawRoundedRect(bar, 3.5, 3.5)
            fw = QRectF(bar.left(), bar.top(), max(7.0, bar.width() * used), bar.height())
            fg = QLinearGradient(fw.left(), 0, fw.right() + 1, 0)
            fg.setColorAt(0, _mixc(warn, "#000000", 0.25))
            fg.setColorAt(1, _mixc(warn, "#ffffff", 0.18))
            p.setBrush(QBrush(fg))
            p.drawRoundedRect(fw, 3.5, 3.5)
            p.setFont(font(9))
            p.setPen(qcol(C.TEXT_DIM))
            p.drawText(QRectF(body.left(), body.top() + 14, body.width(), 18), _AL.AlignLeft | _AL.AlignVCenter, str(e.get("sub", "")))
            return
        text = str(e.get("text") or "") if e.get("kind") == "text" else ""
        text = text.strip() or str(e.get("sub") or "")
        opt = QTextOption(_AL.AlignLeft | _AL.AlignTop)
        opt.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        p.setFont(font(9))
        p.setPen(qcol(C.TEXT_MED if e.get("kind") == "text" else C.TEXT_DIM))
        p.save()
        p.setClipRect(body)
        p.drawText(body, text, opt)
        p.restore()


# ── new-card dialog ──────────────────────────────────────────────────────────────────
class _Pick(QWidget):
    """One round colour swatch or one emoji button of the new-card dialog."""
    clicked = pyqtSignal(str)

    def __init__(self, kind: str, value: str):
        super().__init__()
        self.kind, self.value, self.on = kind, value, False
        self.setFixedSize(36, 36)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_on(self, on: bool):
        self.on = on
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.value)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QPointF(self.width() / 2, self.height() / 2)
        if self.kind == "color":
            col = QColor(self.value)
            if self.on:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(qcol(self.value, 60))
                p.drawEllipse(c, 17, 17)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QColor("#ffffff"), 2))
                p.drawEllipse(c, 14, 14)
            g = QRadialGradient(QPointF(c.x() - 4, c.y() - 5), 18)
            g.setColorAt(0, _mixc(col, "#ffffff", 0.35))
            g.setColorAt(1, col)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(g))
            p.drawEllipse(c, 10.5, 10.5)
            return
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        p.setPen(QPen(qcol(C.PRI, 200) if self.on else QColor(255, 255, 255, 30), 1.4 if self.on else 1))
        p.setBrush(qcol(C.PRI, 36) if self.on else QColor(255, 255, 255, 10))
        p.drawRoundedRect(r, 11, 11)
        f = QFont("Segoe UI Emoji")
        f.setPixelSize(18)
        p.setFont(f)
        p.setPen(QColor("#ffffff"))
        p.drawText(r, _AL.AlignCenter, self.value)


class NoteDialog(QDialog):
    """Design a new card: a title, a note, a colour and an icon, with a live preview of the glass card."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New card")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(580)
        self.color, self.icon = _NOTE_COLORS[0], _NOTE_ICONS[0]
        self._drag: QPoint | None = None
        self.setStyleSheet(
            f"QLabel {{ background: transparent; color: {C.TEXT}; }}"
            f"QLineEdit, QPlainTextEdit {{ background: {rgba('#ffffff', .05)}; color: {C.TEXT}; border: 1px solid {rgba('#ffffff', .12)};"
            f" border-radius: 12px; padding: 8px 12px; selection-background-color: {C.PRI_DIM}; }}"
            f"QLineEdit:focus, QPlainTextEdit:focus {{ border: 1px solid {C.PRI}; background: {rgba('#ffffff', .08)}; }}")
        root = QVBoxLayout(self)
        root.setContentsMargins(38, 34, 38, 32)
        root.setSpacing(12)

        head = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(1)
        t = QLabel("New card")
        t.setFont(font(17, QFont.Weight.Bold))
        s = QLabel("A glass card that floats in Hand Space")
        s.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 11px; background: transparent;")
        col.addWidget(t)
        col.addWidget(s)
        head.addLayout(col, 1)
        x = GlowButton("✕", "ghost")
        x.setFixedSize(38, 38)
        x.clicked.connect(self.reject)
        head.addWidget(x, 0, _AL.AlignTop)
        root.addLayout(head)

        self.preview = CardTile({"kind": "text", "title": "Untitled", "text": "", "sub": "Your note appears here",
                                 "accent": self.color, "icon": self.icon}, interactive=False)
        self.preview.setFixedHeight(126)
        root.addWidget(self.preview)

        root.addWidget(self._cap("TITLE"))
        self.title = QLineEdit()
        self.title.setPlaceholderText("Give the card a name")
        self.title.textChanged.connect(self._refresh)
        root.addWidget(self.title)
        root.addWidget(self._cap("NOTE"))
        self.body = QPlainTextEdit()
        self.body.setPlaceholderText("Write the note…")
        self.body.setMinimumHeight(110)
        self.body.setMaximumHeight(150)
        self.body.textChanged.connect(self._refresh)
        root.addWidget(self.body)

        root.addWidget(self._cap("COLOUR"))
        row = QHBoxLayout()
        row.setSpacing(6)
        self._colors = []
        for c in _NOTE_COLORS:
            pk = _Pick("color", c)
            pk.clicked.connect(self._set_color)
            self._colors.append(pk)
            row.addWidget(pk)
        row.addStretch(1)
        root.addLayout(row)
        root.addWidget(self._cap("ICON"))
        row = QHBoxLayout()
        row.setSpacing(6)
        self._icons = []
        for ic in _NOTE_ICONS:
            pk = _Pick("emoji", ic)
            pk.clicked.connect(self._set_icon)
            self._icons.append(pk)
            row.addWidget(pk)
        row.addStretch(1)
        root.addLayout(row)

        btns = QHBoxLayout()
        btns.setContentsMargins(0, 8, 0, 0)
        btns.addStretch(1)
        c = GlowButton("Cancel", "ghost")
        c.clicked.connect(self.reject)
        ok = GlowButton("Save card", "primary")
        ok.setMinimumWidth(140)
        ok.clicked.connect(self.accept)
        btns.addWidget(c)
        btns.addWidget(ok)
        root.addLayout(btns)
        self._set_color(self.color)
        self._set_icon(self.icon)

    @staticmethod
    def _cap(text: str) -> QLabel:
        lb = QLabel(text)
        f = font(8, QFont.Weight.Bold)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.4)
        lb.setFont(f)
        lb.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        return lb

    def _set_color(self, v: str):
        self.color = v
        for pk in self._colors:
            pk.set_on(pk.value == v)
        self._refresh()

    def _set_icon(self, v: str):
        self.icon = v
        for pk in self._icons:
            pk.set_on(pk.value == v)
        self._refresh()

    def _refresh(self, *_):
        text = self.body.toPlainText().strip()
        self.preview.set_entry({"kind": "text", "title": self.title.text().strip() or "Untitled", "text": text,
                                "sub": "Your note appears here", "accent": self.color, "icon": self.icon})

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(12, 12, -12, -12)
        p.setPen(Qt.PenStyle.NoPen)
        for i, al in enumerate((34, 22, 12)):                                # soft shadow around the panel
            g = 3 + i * 4
            p.setBrush(QColor(0, 0, 0, al))
            p.drawRoundedRect(r.adjusted(-g, -g + 6, g, g + 6), 26 + g, 26 + g)
        fill = QLinearGradient(r.topLeft(), r.bottomLeft())
        fill.setColorAt(0, qcol(C.PANEL2, 250))
        fill.setColorAt(1, qcol(C.BG2, 252))
        p.setBrush(QBrush(fill))
        p.drawRoundedRect(r, 26, 26)
        glow = QRadialGradient(QPointF(r.left() + r.width() * 0.2, r.top()), r.width() * 0.7)
        glow.setColorAt(0, qcol(self.color, 44))
        glow.setColorAt(1, qcol(self.color, 0))
        p.setBrush(QBrush(glow))
        p.drawRoundedRect(r, 26, 26)
        rim = QLinearGradient(r.topLeft(), r.bottomRight())
        rim.setColorAt(0, QColor(255, 255, 255, 110))
        rim.setColorAt(0.5, QColor(255, 255, 255, 20))
        rim.setColorAt(1, qcol(self.color, 130))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QBrush(rim), 1.2))
        p.drawRoundedRect(r.adjusted(0.6, 0.6, -0.6, -0.6), 25.4, 25.4)

    def mousePressEvent(self, e):                                            # the frameless window is dragged by its panel
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self.move(e.globalPosition().toPoint() - self._drag)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._drag = None
        super().mouseReleaseEvent(e)


# ── gesture guide ────────────────────────────────────────────────────────────────────
class _Icon(QWidget):
    def __init__(self, kind: str, color: str | None = None):
        super().__init__()
        self.kind = kind
        self.color = color
        self.setFixedSize(38, 38)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        col = QColor(self.color or C.PRI)
        c = QPointF(19, 19)
        g = QRadialGradient(QPointF(14, 12), 28)
        g.setColorAt(0, qcol(col.name(), 90))
        g.setColorAt(1, qcol(col.name(), 18))
        p.setBrush(QBrush(g))
        p.setPen(QPen(qcol(col.name(), 150), 1.2))
        p.drawEllipse(c, 17, 17)
        draw_icon(p, self.kind, QRectF(8, 8, 22, 22), _mixc(col, "#ffffff", 0.4), 1.7)


class GestureRow(QWidget):
    """One gesture: a coloured icon, its name and what it does. A soft highlight follows the pointer."""

    def __init__(self, key: str, name: str, desc: str):
        super().__init__()
        self._hot = False
        self._col = _gesture_color(key)
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 8, 10, 8)
        row.setSpacing(12)
        row.addWidget(_Icon(_ICON[key], self._col), 0, _AL.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(1)
        a = QLabel(name)
        a.setFont(font(11, QFont.Weight.DemiBold))
        a.setStyleSheet(f"color: {self._col}; background: transparent;")
        b = QLabel(desc)
        b.setWordWrap(True)
        b.setStyleSheet(f"color: {C.TEXT_MED}; font-size: 10px; background: transparent;")
        col.addWidget(a)
        col.addWidget(b)
        row.addLayout(col, 1)

    def enterEvent(self, e):
        self._hot = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hot = False
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _):
        if not self._hot:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        g = QLinearGradient(r.topLeft(), r.topRight())
        g.setColorAt(0, qcol(self._col, 34))
        g.setColorAt(1, qcol(self._col, 4))
        p.setPen(QPen(qcol(self._col, 60), 1))
        p.setBrush(QBrush(g))
        p.drawRoundedRect(r, 14, 14)


class SpacePage(QWidget):
    toast = pyqtSignal(str)
    open_requested = pyqtSignal()

    def __init__(self, status_fn=None, parent=None):
        super().__init__(parent)
        self._status_fn = status_fn or (lambda: ("off", False))
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)
        root.addWidget(PageHeader("Hand Space", "Browse your drives, folders and pictures with your bare hands — webcam only, all local"))

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 8, 8)
        body.setSpacing(14)
        root.addLayout(body, 1)

        # ── left: hero + launch + settings + cards ──
        left = QVBoxLayout()
        left.setSpacing(12)
        body.addLayout(left, 1)

        hero = GlassCard(radius=26, tint=170, accent=C.PRI)
        hv = QVBoxLayout(hero)
        hv.setContentsMargins(18, 10, 18, 16)
        hv.setSpacing(8)
        hv.addWidget(Hero())
        row = QHBoxLayout()
        self.status = StatusPill()
        row.addWidget(self.status)
        row.addStretch(1)
        self.open_b = GlowButton("✋  Open Hand Space", "primary")
        self.open_b.setMinimumHeight(46)
        self.open_b.setMinimumWidth(220)
        self.open_b.clicked.connect(self.open_requested.emit)
        row.addWidget(self.open_b)
        hv.addLayout(row)
        left.addWidget(hero)

        opts = GlassCard(radius=22, tint=160)
        ov = QVBoxLayout(opts)
        ov.setContentsMargins(20, 14, 20, 14)
        ov.setSpacing(10)
        ov.addLayout(self._opt_row("Show hand outline", "Draw thin bones of your hand. Off = only the fingertip and pinch rings.",
                                   self._toggle_outline()))
        rule = QFrame()
        rule.setFixedHeight(1)
        rule.setStyleSheet(f"background: {rgba('#ffffff', .07)}; border: none;")
        ov.addWidget(rule)
        self.folder_lbl = QLabel("")
        self.folder_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 11px; background: transparent;")
        self.folder_lbl.setWordWrap(True)
        pick = GlowButton("Choose folder…", "ghost")
        pick.clicked.connect(self._pick_folder)
        clear = GlowButton("Use built-in cards", "ghost")
        clear.clicked.connect(self._clear_folder)
        fr = QHBoxLayout()
        fr.setSpacing(8)
        fr.addWidget(self.folder_lbl, 1)
        for b in (clear, pick):
            fr.addWidget(b)
        ov.addLayout(fr)
        left.addWidget(opts)

        hr = QHBoxLayout()
        hr.setSpacing(10)
        head = QLabel("Your cards")
        head.setFont(font(13, QFont.Weight.Bold))
        head.setStyleSheet("background: transparent;")
        self.count = QLabel("0")
        self.count.setStyleSheet(f"background: {rgba(C.PRI, .16)}; color: {C.PRI}; border: 1px solid {rgba(C.PRI, .4)};"
                                 f" border-radius: 10px; padding: 1px 10px; font-size: 10px; font-weight: 600;")
        hr.addWidget(head)
        hr.addWidget(self.count)
        hr.addStretch(1)
        new = GlowButton("＋  New card", "primary")
        new.setMinimumWidth(150)
        new.clicked.connect(self._new_note)
        hr.addWidget(new)
        left.addLayout(hr)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setStyleSheet("QScrollArea{background:transparent;}QScrollArea>QWidget>QWidget{background:transparent;}")
        self.inner = QWidget()
        self.grid = QGridLayout(self.inner)
        self.grid.setContentsMargins(0, 0, 8, 8)
        self.grid.setSpacing(8)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.scroll.setWidget(self.inner)
        left.addWidget(self.scroll, 1)

        # ── right: gesture guide ──
        guide = GlassCard(radius=26, tint=170, accent=C.ACC)
        guide.setFixedWidth(360)
        gv = QVBoxLayout(guide)
        gv.setContentsMargins(16, 18, 16, 16)
        gv.setSpacing(2)
        t = QLabel("Gestures")
        t.setFont(font(15, QFont.Weight.Bold))
        t.setStyleSheet("background: transparent; padding-left: 8px;")
        gv.addWidget(t)
        sub = QLabel("Six ways to move things — no controller needed")
        sub.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 10px; background: transparent; padding-left: 8px; padding-bottom: 6px;")
        gv.addWidget(sub)
        for key, name, desc in GESTURES:
            gv.addWidget(GestureRow(key, name, desc))
        gv.addStretch()
        esc = QLabel("Esc or the ✕ in the corner leaves Hand Space. A held fist also stops hand mouse control.")
        esc.setWordWrap(True)
        esc.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 10px; background: transparent; padding: 6px 8px 0 8px;")
        gv.addWidget(esc)
        body.addWidget(guide)

        self._tick = QTimer(self)
        self._tick.timeout.connect(self._refresh_status)

    # ── pieces ───────────────────────────────────────────────────────────────────
    def _opt_row(self, title: str, sub: str, widget: QWidget) -> QHBoxLayout:
        row = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(1)
        a = QLabel(title)
        a.setFont(font(11, QFont.Weight.DemiBold))
        a.setStyleSheet("background: transparent;")
        b = QLabel(sub)
        b.setWordWrap(True)
        b.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 10px; background: transparent;")
        col.addWidget(a)
        col.addWidget(b)
        row.addLayout(col, 1)
        row.addWidget(widget)
        return row

    def _toggle_outline(self) -> Toggle:
        from core import hand_cursor
        tg = Toggle(hand_cursor.show_skeleton())
        tg.toggled.connect(hand_cursor.set_show_skeleton)
        return tg

    # ── lifecycle ────────────────────────────────────────────────────────────────
    def showEvent(self, e):
        super().showEvent(e)
        self._reload()
        self._refresh_status()
        self._tick.start(1500)

    def hideEvent(self, e):
        self._tick.stop()
        super().hideEvent(e)

    def _refresh_status(self):
        st, active = self._status_fn()
        if active:
            self.status.set("Hand Space is open", C.GREEN, True)
        else:
            bad = str(st).startswith(("error", "missing"))
            self.status.set("Camera problem — check Settings → Gestures" if bad else "Needs your webcam · nothing leaves this computer",
                            C.RED if bad else C.TEXT_DIM, False)

    # ── cards ────────────────────────────────────────────────────────────────────
    def _folder(self) -> str:
        return str(read_cfg().get("hand_space_folder") or "")

    def _reload(self):
        from core import hand_space
        folder = self._folder()
        self.folder_lbl.setText(f"Pinned next to your drives: {folder}" if folder else "Starts on your drives (C: first). Pin a folder to show it next to them.")
        while self.grid.count():
            it = self.grid.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        cards = hand_space.load_cards(folder or None)
        self.count.setText(str(len(cards)))
        cols = 3
        for i, c in enumerate(cards[:18]):
            th = _thumb(c["image"]) if c.get("image") else None
            self.grid.addWidget(CardTile(c, th), i // cols, i % cols)
        if not cards:
            empty = QLabel("No cards yet — press “New card” to make the first one.")
            empty.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; padding: 18px;")
            self.grid.addWidget(empty, 0, 0, 1, cols)
        for k in range(cols):
            self.grid.setColumnStretch(k, 1)

    def _pick_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Folder to pin next to your drives in Hand Space", self._folder())
        if path:
            patch_cfg(hand_space_folder=path)
            self._reload()

    def _clear_folder(self):
        patch_cfg(hand_space_folder="")
        self._reload()

    def _new_note(self):
        dlg = NoteDialog(self)
        if not dlg.exec():
            return
        title = dlg.title.text().strip() or "Untitled"
        text = dlg.body.toPlainText().strip()
        folder = self._folder()
        if not folder:                                             # first note: give the cards a home of their own
            from actions._common import DATA_DIR
            folder = str(DATA_DIR / "hand_space_notes")
            patch_cfg(hand_space_folder=folder)
        root = Path(folder)
        root.mkdir(parents=True, exist_ok=True)
        safe = "".join(ch for ch in title if ch.isalnum() or ch in " -_")[:50].strip() or "note"
        path, n = root / f"{safe}.md", 2
        while path.exists():
            path, n = root / f"{safe} {n}.md", n + 1
        # the colour and icon live in the note's front matter: Hand Space reads them back and draws the card in its own look
        path.write_text(f'---\ncolor: "{dlg.color}"\nicon: {dlg.icon}\n---\n\n# {title}\n\n{text}\n', encoding="utf-8")
        self.toast.emit(f"Card saved: {path.name}")
        self._reload()


#by ASDcuber

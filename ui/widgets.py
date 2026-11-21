from __future__ import annotations

import math
import os
import re
import time

from PyQt6.QtCore import QPointF, QRectF, QSize, QUrl, Qt, QTimer, QVariantAnimation, QEasingCurve, pyqtSignal
from PyQt6.QtGui import (
    QBrush, QColor, QDesktopServices, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QConicalGradient, QPixmap,
)
from PyQt6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from ui.fx import GlassCard, Toggle, fade_in, mix
from ui.theme import C, qcol, rgba


def font(size: int = 10, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    return QFont("Segoe UI", size, weight)


def draw_icon(p: QPainter, kind: str, r: QRectF, color: QColor, w: float = 1.8) -> None:
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(color, w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.setBrush(Qt.BrushStyle.NoBrush)
    c = r.center()
    s = min(r.width(), r.height())
    u = s / 24.0
    p.translate(c)
    p.scale(u, u)
    path = QPainterPath()
    if kind == "home":
        p.drawEllipse(QPointF(0, 0), 8, 8)
        p.setBrush(color)
        p.drawEllipse(QPointF(0, 0), 2.6, 2.6)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(0, 0), 11, 4.2)
    elif kind == "chat":
        path.addRoundedRect(QRectF(-9, -8, 18, 13), 4, 4)
        path.moveTo(-3, 5)
        path.lineTo(-5, 10)
        path.lineTo(2, 5)
        p.drawPath(path)
        for dx in (-4, 0, 4):
            p.drawPoint(QPointF(dx, -1.5))
    elif kind == "gear":
        p.drawEllipse(QPointF(0, 0), 4, 4)
        for i in range(8):
            a = i * math.pi / 4
            p.drawLine(QPointF(7 * math.cos(a), 7 * math.sin(a)), QPointF(10 * math.cos(a), 10 * math.sin(a)))
        p.drawEllipse(QPointF(0, 0), 7, 7)
    elif kind in ("mic", "mic_off"):
        path.addRoundedRect(QRectF(-3.5, -10, 7, 12), 3.5, 3.5)
        p.drawPath(path)
        p.drawArc(QRectF(-7, -6, 14, 14), 200 * 16, 140 * 16)
        p.drawLine(QPointF(0, 8), QPointF(0, 11))
        p.drawLine(QPointF(-3, 11), QPointF(3, 11))
        if kind == "mic_off":
            p.setPen(QPen(qcol(C.RED), w + .4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(-9, -9), QPointF(9, 10))
    elif kind == "send":
        path.moveTo(-10, -8)
        path.lineTo(10, 0)
        path.lineTo(-10, 8)
        path.lineTo(-6, 0)
        path.closeSubpath()
        p.drawPath(path)
        p.drawLine(QPointF(-6, 0), QPointF(3, 0))
    elif kind == "close":
        p.drawLine(QPointF(-7, -7), QPointF(7, 7))
        p.drawLine(QPointF(-7, 7), QPointF(7, -7))
    elif kind == "full":
        for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            p.drawLine(QPointF(sx * 9, sy * 9), QPointF(sx * 9, sy * 4))
            p.drawLine(QPointF(sx * 9, sy * 9), QPointF(sx * 4, sy * 9))
    elif kind == "compact":
        path.addRoundedRect(QRectF(-10, -7, 20, 14), 3, 3)
        p.drawPath(path)
        p.setBrush(color)
        p.drawRoundedRect(QRectF(2, 0, 6, 4), 1.2, 1.2)
    elif kind == "stats":
        for i, h in enumerate((8, 14, 10)):
            p.drawLine(QPointF(-7 + i * 7, 9), QPointF(-7 + i * 7, 9 - h))
    elif kind == "cube":
        pts = [QPointF(0, -10), QPointF(9, -5), QPointF(9, 5), QPointF(0, 10), QPointF(-9, 5), QPointF(-9, -5)]
        path.moveTo(pts[0])
        for q in pts[1:]:
            path.lineTo(q)
        path.closeSubpath()
        p.drawPath(path)
        p.drawLine(QPointF(0, 0), QPointF(0, 10))
        p.drawLine(QPointF(0, 0), QPointF(9, -5))
        p.drawLine(QPointF(0, 0), QPointF(-9, -5))
    elif kind == "clip":
        p.drawLine(QPointF(0, -8), QPointF(0, 8))
        p.drawLine(QPointF(-8, 0), QPointF(8, 0))
    elif kind == "back":
        p.drawLine(QPointF(8, 0), QPointF(-8, 0))
        p.drawLine(QPointF(-8, 0), QPointF(-2, -6))
        p.drawLine(QPointF(-8, 0), QPointF(-2, 6))
    elif kind == "dashboard":
        for dx in (-9, 1):
            for dy in (-9, 1):
                p.drawRoundedRect(QRectF(dx, dy, 8, 8), 2, 2)
    elif kind == "phone":
        path.addRoundedRect(QRectF(-7, -11, 14, 22), 3, 3)
        p.drawPath(path)
        p.drawLine(QPointF(-3, 7), QPointF(3, 7))
        p.drawEllipse(QPointF(0, -7), 1.1, 1.1)
    elif kind == "hand":
        p.drawRoundedRect(QRectF(-6, 0, 12, 10), 4, 4)
        for i, (x, top) in enumerate(((-4.5, -7), (-1.5, -10), (1.5, -10), (4.5, -7))):
            p.drawLine(QPointF(x, 0), QPointF(x, top))
        p.drawLine(QPointF(-6, 5), QPointF(-10, 0))
    elif kind == "house":
        path.moveTo(-10, 0)
        path.lineTo(0, -9)
        path.lineTo(10, 0)
        p.drawPath(path)
        p.drawRoundedRect(QRectF(-7, 0, 14, 10), 1.5, 1.5)
        p.drawLine(QPointF(0, 10), QPointF(0, 4))
    elif kind == "bulb":
        p.drawEllipse(QPointF(0, -3), 7.5, 7.5)
        p.drawLine(QPointF(-3.5, 7), QPointF(3.5, 7))
        p.drawLine(QPointF(-2.5, 10), QPointF(2.5, 10))
        p.drawLine(QPointF(-2.5, 3), QPointF(-2.5, 7))
        p.drawLine(QPointF(2.5, 3), QPointF(2.5, 7))
    elif kind == "plug":
        p.drawRoundedRect(QRectF(-6.5, -3, 13, 10), 3, 3)
        p.drawLine(QPointF(-3, -3), QPointF(-3, -9))
        p.drawLine(QPointF(3, -3), QPointF(3, -9))
        p.drawLine(QPointF(0, 7), QPointF(0, 11))
    elif kind == "strip":
        path.moveTo(-11, 4)
        path.cubicTo(-6, -8, -2, 10, 3, -1)
        path.cubicTo(5, -5, 8, -6, 11, -3)
        p.drawPath(path)
        for dx in (-9, -1, 8):
            p.drawEllipse(QPointF(dx, 8), 1.2, 1.2)
    elif kind == "camera":
        p.drawRoundedRect(QRectF(-11, -6, 15, 12), 3, 3)
        path.moveTo(4, -1.5)
        path.lineTo(11, -5.5)
        path.lineTo(11, 5.5)
        path.lineTo(4, 1.5)
        p.drawPath(path)
    elif kind == "monitor":
        p.drawRoundedRect(QRectF(-11, -8, 22, 14), 2.5, 2.5)
        p.drawLine(QPointF(-4, 10), QPointF(4, 10))
        p.drawLine(QPointF(0, 6), QPointF(0, 10))
    elif kind == "bolt":
        path.moveTo(2, -11)
        path.lineTo(-6, 2)
        path.lineTo(0, 2)
        path.lineTo(-2, 11)
        path.lineTo(7, -3)
        path.lineTo(1, -3)
        path.closeSubpath()
        p.drawPath(path)
    elif kind == "lock":
        p.drawRoundedRect(QRectF(-7, -1, 14, 11), 2.5, 2.5)
        path.moveTo(-4.5, -1)
        path.lineTo(-4.5, -5)
        path.arcTo(QRectF(-4.5, -10, 9, 9), 180, -180)
        path.lineTo(4.5, -1)
        p.drawPath(path)
        p.drawPoint(QPointF(0, 4.5))
    elif kind == "folder":
        path.moveTo(-10, -5)
        path.lineTo(-10, 8)
        path.lineTo(10, 8)
        path.lineTo(10, -3)
        path.lineTo(1, -3)
        path.lineTo(-2, -7)
        path.lineTo(-10, -7)
        path.closeSubpath()
        p.drawPath(path)
    elif kind == "screenshot":
        for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            p.drawLine(QPointF(sx * 10, sy * 10), QPointF(sx * 10, sy * 5))
            p.drawLine(QPointF(sx * 10, sy * 10), QPointF(sx * 5, sy * 10))
        p.drawEllipse(QPointF(0, 0), 3.2, 3.2)
    elif kind == "music":
        p.drawLine(QPointF(4, -9), QPointF(4, 5))
        p.drawLine(QPointF(4, -9), QPointF(-6, -6))
        p.drawLine(QPointF(-6, -6), QPointF(-6, 7))
        p.drawEllipse(QPointF(-8, 7), 3, 2.4)
        p.drawEllipse(QPointF(2, 5), 3, 2.4)
    elif kind == "calc":
        path.addRoundedRect(QRectF(-8, -10, 16, 20), 3, 3)
        p.drawPath(path)
        p.drawLine(QPointF(-5, -6), QPointF(5, -6))
        for dx in (-4, 0, 4):
            for dy in (-1, 3, 7):
                p.drawPoint(QPointF(dx, dy))
    elif kind == "note":
        path.addRoundedRect(QRectF(-8, -10, 16, 20), 3, 3)
        p.drawPath(path)
        for dy in (-4, 0, 4):
            p.drawLine(QPointF(-4, dy), QPointF(4, dy))
    elif kind == "search":
        p.drawEllipse(QPointF(-2, -2), 7, 7)
        p.drawLine(QPointF(3, 3), QPointF(9, 9))
    elif kind == "play":
        path.moveTo(-5, -8)
        path.lineTo(7, 0)
        path.lineTo(-5, 8)
        path.closeSubpath()
        p.setBrush(color)
        p.drawPath(path)
    elif kind == "pause":
        p.setBrush(color)
        p.drawRoundedRect(QRectF(-6, -8, 4, 16), 1.5, 1.5)
        p.drawRoundedRect(QRectF(2, -8, 4, 16), 1.5, 1.5)
    elif kind == "prev":
        p.drawLine(QPointF(-8, -7), QPointF(-8, 7))
        path.moveTo(8, -7)
        path.lineTo(-4, 0)
        path.lineTo(8, 7)
        path.closeSubpath()
        p.setBrush(color)
        p.drawPath(path)
    elif kind == "next":
        p.drawLine(QPointF(8, -7), QPointF(8, 7))
        path.moveTo(-8, -7)
        path.lineTo(4, 0)
        path.lineTo(-8, 7)
        path.closeSubpath()
        p.setBrush(color)
        p.drawPath(path)
    elif kind == "check":
        p.drawLine(QPointF(-7, 0), QPointF(-2, 6))
        p.drawLine(QPointF(-2, 6), QPointF(8, -6))
    p.restore()


from ui.hover import HoverAnimationMixin
class IconButton(HoverAnimationMixin, QPushButton):

    def __init__(self, icon: str, size: int = 42, checkable: bool = False, tip: str = "",
                 parent=None, flat: bool = False):
        super().__init__(parent)
        self.icon_kind = icon
        self.force_on = False
        self.flat = flat
        self.setFixedSize(size, size)
        self.setCheckable(checkable)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        if tip:
            self.setToolTip(tip)
        self._h = 0.0
        self._an = QVariantAnimation(self)
        self._an.setDuration(160)
        self._an.valueChanged.connect(self._set_h)





    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        on = self.force_on or (self.isCheckable() and self.isChecked())
        h = self._h
        if on:
            g = QLinearGradient(r.topLeft(), r.bottomRight())
            g.setColorAt(0, qcol(C.PRI, 70))
            g.setColorAt(1, qcol(C.ACC, 70))
            p.setBrush(QBrush(g))
            p.setPen(QPen(qcol(C.PRI, 190), 1.1))
            p.drawRoundedRect(r, r.height() * .34, r.height() * .34)
        elif not self.flat or h > 0.01:
            p.setBrush(QColor(255, 255, 255, int((8 if not self.flat else 0) + 28 * h)))
            p.setPen(QPen(QColor(255, 255, 255, int((22 if not self.flat else 0) + 60 * h)), 1))
            p.drawRoundedRect(r, r.height() * .34, r.height() * .34)
        col = mix(qcol(C.TEXT_MED), qcol(C.PRI if on else C.TEXT), max(h, 1.0 if on else 0.0))
        draw_icon(p, self.icon_kind, r.adjusted(r.width() * .26, r.height() * .26, -r.width() * .26, -r.height() * .26), col)

    def set_icon(self, kind: str):
        self.icon_kind = kind
        self.update()


class StatePill(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(32)
        self._text = "INITIALISING"
        self._color = QColor(C.PRI)
        self._t = 0.0
        self._tm = QTimer(self)
        self._tm.timeout.connect(self._tick)
        self._tm.start(120)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def sizeHint(self):
        return QSize(150, 32)

    def minimumSizeHint(self):
        return QSize(0, 32)

    def set(self, text: str, color: str):
        self._text = text
        self._color = QColor(color)
        self.setToolTip(text.title())
        self.update()

    def _tick(self):
        if self.isVisible() and not self.window().isMinimized():
            self._t += 0.12                                   # 120 ms steps at the same animation speed as before
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.setBrush(QColor(self._color.red(), self._color.green(), self._color.blue(), 28))
        p.setPen(QPen(QColor(self._color.red(), self._color.green(), self._color.blue(), 110), 1))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        pulse = 0.5 + 0.5 * math.sin(self._t * 4)
        icon_only = r.width() < 110          # narrow (collapsed side bar): just the status dot
        c = QPointF(r.center().x() if icon_only else r.left() + 16, r.center().y())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(self._color.red(), self._color.green(), self._color.blue(), int(50 + 60 * pulse)))
        p.drawEllipse(c, 5 + 3 * pulse, 5 + 3 * pulse)
        p.setBrush(self._color)
        p.drawEllipse(c, 3.6, 3.6)
        if icon_only:
            return
        f = font(9, QFont.Weight.DemiBold)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.6)
        p.setFont(f)
        p.setPen(self._color)
        p.drawText(QRectF(r.left() + 30, r.top(), r.width() - 36, r.height()),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self._text)


class MetricTile(QWidget):

    def __init__(self, label: str, color_attr: str = "PRI", parent=None, ring: int = 68):
        super().__init__(parent)
        self.label = label
        self.color_attr = color_attr
        self.ring = ring
        self.setFixedSize(ring + 64, ring + 44)
        self._pct = 0.0
        self._disp = 0.0
        self._text = "--"
        self._tm = QTimer(self)
        self._tm.timeout.connect(self._tick)
        self._tm.start(60)

    def set_value(self, pct: float, text: str):
        self._pct = max(0.0, min(100.0, pct))
        self._text = text

    def _tick(self):
        if abs(self._pct - self._disp) > 0.1 and self.isVisible():
            self._disp += (self._pct - self._disp) * 0.18
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        col = QColor(getattr(C, self.color_attr))
        ring = QRectF(self.width() / 2 - self.ring / 2, 6, self.ring, self.ring)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 22), 6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(ring, 0, 360 * 16)
        g = QConicalGradient(ring.center(), 90)
        g.setColorAt(0, col)
        g.setColorAt(1, qcol(C.ACC))
        p.setPen(QPen(QBrush(g), 6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(ring, 90 * 16, int(-self._disp / 100 * 360 * 16))
        p.setPen(qcol(C.TEXT))
        p.setFont(font(10, QFont.Weight.Bold))
        p.drawText(ring, Qt.AlignmentFlag.AlignCenter, self._text)
        p.setPen(qcol(C.TEXT_DIM))
        f = font(8, QFont.Weight.DemiBold)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2)
        p.setFont(f)
        p.drawText(QRectF(0, self.ring + 14, self.width(), 20), Qt.AlignmentFlag.AlignCenter, self.label)


# ── chat: markdown, code blocks with Copy, typing indicator, file / image previews, reactions ─────────────────────────

_FENCE = re.compile(r"```[ \t]*([\w+#.\-]*)[^\n]*\n?(.*?)(?:```|\Z)", re.S)
_IMG_EXT = {"png", "jpg", "jpeg", "gif", "bmp", "webp"}
# never offer a one-click "open" for anything that can run code
_RISKY_EXT = {"exe", "bat", "cmd", "com", "msi", "scr", "ps1", "vbs", "vbe", "js", "jse", "jar", "lnk", "sh", "app", "dll",
              "reg", "hta", "cpl", "pif", "wsf", "py", "pyw", "appx", "msix", "iso"}
_PATH_RE = re.compile(r"""(?:(?<![\w])[A-Za-z]:[\\/][^\s"'<>|*?`]+|(?<![\w:/.])(?:~|/)[^\s"'<>|*?`]+)""")


def _split_blocks(text: str) -> list[tuple]:
    """Text → [("text", str) | ("code", lang, code)], so ``` fences become code blocks."""
    out: list[tuple] = []
    pos = 0
    for m in _FENCE.finditer(text):
        if m.start() > pos and text[pos:m.start()].strip():
            out.append(("text", text[pos:m.start()].strip("\n")))
        out.append(("code", (m.group(1) or "").strip(), m.group(2).rstrip("\n")))
        pos = m.end()
    if pos < len(text) and text[pos:].strip():
        out.append(("text", text[pos:].strip("\n")))
    return out


def _md(seg: str) -> str:
    """Prepare a text segment for Qt's Markdown renderer: raw '<' is escaped (no HTML injection) and single line breaks stay."""
    seg = seg.replace("<", "&lt;")
    return re.sub(r"(?<!\n)\n(?!\n)", "  \n", seg)


def _find_files(text: str, limit: int = 3) -> list[str]:
    """Existing local files mentioned in a message (not executables), for a preview card under the bubble."""
    found: list[str] = []
    for m in _PATH_RE.finditer(text or ""):
        cand = m.group(0).rstrip(".,;:!?)]}»”’")
        if len(cand) > 300:
            continue
        ext = os.path.splitext(cand)[1].lstrip(".").lower()
        if not ext or ext in _RISKY_EXT:
            continue
        try:
            path = os.path.expanduser(cand)
            if os.path.isfile(path) and path not in found:
                found.append(path)
        except Exception:
            continue
        if len(found) >= limit:
            break
    return found


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


class _ClickLabel(QLabel):
    clicked = pyqtSignal()

    def __init__(self, *a):
        super().__init__(*a)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(e)


def _open_file(path: str) -> None:
    try:
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))
    except Exception:
        pass


def _file_preview(path: str) -> QWidget | None:
    """A clickable thumbnail for an image, or a small file card for anything else."""
    try:
        name = os.path.basename(path)
        size = os.path.getsize(path)
        ext = os.path.splitext(name)[1].lstrip(".").lower()
        if ext in _IMG_EXT and size <= 25 * 1024 * 1024:
            pm = QPixmap(path)
            if not pm.isNull():
                pm = pm.scaled(300, 220, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                lb = _ClickLabel()
                lb.setPixmap(pm)
                lb.setToolTip(f"{name} · click to open")
                lb.setStyleSheet(f"border: 1px solid {rgba('#ffffff', .14)}; border-radius: 10px; padding: 2px; background: transparent;")
                lb.clicked.connect(lambda p=path: _open_file(p))
                return lb
        card = _ClickLabel(f"📄  {name}\n      {_human_size(size)}")
        card.setFont(font(10))
        card.setToolTip(f"{path}\nclick to open")
        card.setStyleSheet(
            f"background: {rgba('#000000', .25)}; border: 1px solid {rgba('#ffffff', .12)}; border-radius: 10px;"
            f"padding: 8px 12px; color: {C.TEXT};")
        card.clicked.connect(lambda p=path: _open_file(p))
        return card
    except Exception:
        return None


class CodeBlock(QFrame):
    """A fenced code block: language tag, monospace text and a Copy button that confirms with 'Copied ✓'."""

    def __init__(self, lang: str, code: str, copy_cb):
        super().__init__()
        self._code = code
        self._copy_cb = copy_cb
        self.setObjectName("codeblock")
        self.setStyleSheet(
            f"QFrame#codeblock {{ background: {rgba('#000000', .38)}; border: 1px solid {rgba('#ffffff', .10)}; border-radius: 10px; }}")
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 6, 12, 10)
        v.setSpacing(4)
        top = QHBoxLayout()
        tag = QLabel((lang or "code").upper())
        tag.setFont(font(8, QFont.Weight.DemiBold))
        tag.setStyleSheet(f"color: {C.TEXT_DIM}; letter-spacing: 2px; background: transparent;")
        top.addWidget(tag)
        top.addStretch()
        self._btn = QPushButton("Copy")
        self._btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {C.PRI}; border: none; font-size: 11px; padding: 2px 6px; }}"
            f"QPushButton:hover {{ color: {C.TEXT}; }}")
        self._btn.clicked.connect(self._copy)
        top.addWidget(self._btn)
        v.addLayout(top)
        body = QLabel(code)
        body.setTextFormat(Qt.TextFormat.PlainText)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        f = QFont("Consolas", 10)
        f.setStyleHint(QFont.StyleHint.Monospace)
        body.setFont(f)
        body.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
        v.addWidget(body)

    def _copy(self):
        self._copy_cb(self._code)
        self._btn.setText("Copied ✓")
        QTimer.singleShot(1500, self._reset)

    def _reset(self):
        try:
            self._btn.setText("Copy")
        except RuntimeError:                                       # the row was deleted meanwhile
            pass


class _TypingDots(QWidget):
    """Three bouncing dots, shown while Jarvis is thinking."""

    def __init__(self):
        super().__init__()
        self.setFixedSize(48, 20)
        self._ph = 0.0
        self._tm = QTimer(self)
        self._tm.timeout.connect(self._tick)
        self._tm.start(70)

    def _tick(self):
        if self.isVisible():
            self._ph += 0.30
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(3):
            k = 0.5 + 0.5 * math.sin(self._ph - i * 0.9)
            p.setBrush(qcol(C.PRI, int(90 + 150 * k)))
            r = 3.0 + 1.7 * k
            p.drawEllipse(QPointF(10 + i * 14, 10 - 3.0 * k), r, r)


class _ReactionBar(QWidget):
    """👍 👎 ❤️ and Copy under an assistant message: they appear while the mouse is over the row, a chosen reaction stays."""

    def __init__(self, on_react, on_copy):
        super().__init__()
        self.setFixedHeight(26)
        self._on_react = on_react
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 0, 0, 0)
        lay.setSpacing(4)
        self._btns: list[QPushButton] = []
        for emoji in ("👍", "👎", "❤️"):
            b = QPushButton(emoji)
            b.setCheckable(True)
            b.setFixedSize(32, 24)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(
                f"QPushButton {{ background: {rgba('#ffffff', .05)}; border: 1px solid {rgba('#ffffff', .08)}; border-radius: 12px; font-size: 12px; }}"
                f"QPushButton:hover {{ background: {rgba('#ffffff', .12)}; }}"
                f"QPushButton:checked {{ background: {rgba(C.PRI, .24)}; border: 1px solid {rgba(C.PRI, .55)}; }}")
            b.toggled.connect(lambda on, e=emoji, bt=b: self._react(e, bt, on))
            lay.addWidget(b)
            self._btns.append(b)
        self._copy = QPushButton("Copy")
        self._copy.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._copy.setCursor(Qt.CursorShape.PointingHandCursor)
        self._copy.setFixedHeight(24)
        self._copy.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {C.TEXT_DIM}; border: none; font-size: 11px; padding: 0 8px; }}"
            f"QPushButton:hover {{ color: {C.PRI}; }}")

        def do_copy():
            on_copy()
            self._copy.setText("Copied ✓")
            QTimer.singleShot(1500, self._reset_copy)
        self._copy.clicked.connect(do_copy)
        lay.addWidget(self._copy)
        lay.addStretch()
        self.set_hover(False)

    def _reset_copy(self):
        try:
            self._copy.setText("Copy")
        except RuntimeError:
            pass

    def _react(self, emoji: str, btn: QPushButton, on: bool):
        if on:                                                     # one reaction per message
            for o in self._btns:
                if o is not btn and o.isChecked():
                    o.blockSignals(True)
                    o.setChecked(False)
                    o.blockSignals(False)
        self._on_react(emoji, on)

    def set_hover(self, hover: bool):
        for b in self._btns:
            b.setVisible(hover or b.isChecked())
        self._copy.setVisible(hover)


class _MsgRow(QWidget):
    """One chat row; shows its reaction bar (if any) while the mouse is over it."""

    def __init__(self):
        super().__init__()
        self.bar: _ReactionBar | None = None

    def enterEvent(self, e):
        if self.bar is not None:
            self.bar.set_hover(True)
        super().enterEvent(e)

    def leaveEvent(self, e):
        if self.bar is not None:
            self.bar.set_hover(False)
        super().leaveEvent(e)


class ChatView(QScrollArea):
    copied = pyqtSignal(str)                  # emitted just before something is put on the clipboard
    reacted = pyqtSignal(str, str, bool)      # emoji, message text, on / off

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.viewport().setAutoFillBackground(False)
        self._body = QWidget()
        self._body.setAutoFillBackground(False)
        self._lay = QVBoxLayout(self._body)
        self._lay.setContentsMargins(6, 6, 12, 6)
        self._lay.setSpacing(12)
        self._lay.addStretch()
        self.setWidget(self._body)
        self._count = 0
        self._typing: QWidget | None = None
        self.verticalScrollBar().rangeChanged.connect(self._stick)

    def _bw(self) -> int:
        return max(260, int(self.viewport().width() * 0.92))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        w = self._bw()
        for fr in self._body.findChildren(QFrame):
            if fr.property("bub"):
                fr.setMaximumWidth(w)

    def _stick(self, _lo, hi):
        self.verticalScrollBar().setValue(hi)

    def clear(self):
        self._typing = None
        while self._lay.count() > 1:
            it = self._lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self._count = 0

    # ── clipboard / reactions ────────────────────────────────────────────────────────
    def copy_text(self, text: str):
        self.copied.emit(text)                                     # the window learns the text first, so its clipboard hint skips it
        QApplication.clipboard().setText(text)

    # ── typing indicator ─────────────────────────────────────────────────────────────
    def set_typing(self, on: bool):
        if on and self._typing is None:
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            bub = QFrame()
            bub.setObjectName("chatbub")
            bub.setStyleSheet(
                f"QFrame#chatbub {{ background: {rgba('#ffffff', .07)}; border: 1px solid {rgba('#ffffff', .10)}; border-radius: 18px; }}")
            bl = QHBoxLayout(bub)
            bl.setContentsMargins(16, 8, 16, 8)
            bl.addWidget(_TypingDots())
            h.addWidget(bub)
            h.addStretch()
            self._lay.insertWidget(self._lay.count() - 1, row)
            self._typing = row
            fade_in(row, 200)
        elif not on and self._typing is not None:
            row, self._typing = self._typing, None
            self._lay.removeWidget(row)
            row.deleteLater()

    # ── messages ─────────────────────────────────────────────────────────────────────
    def _bubble(self, kind: str, who: str, text: str) -> QFrame:
        bub = QFrame()
        bub.setObjectName("chatbub")
        bub.setProperty("bub", True)
        bub.setMaximumWidth(self._bw())
        if kind == "user":
            bub.setStyleSheet(
                f"QFrame#chatbub {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 {rgba(C.PRI, .30)}, stop:1 {rgba(C.ACC, .30)});"
                f"border: 1px solid {rgba(C.PRI, .45)}; border-radius: 18px; }}")
        else:
            bub.setStyleSheet(
                f"QFrame#chatbub {{ background: {rgba('#ffffff', .07)}; border: 1px solid {rgba('#ffffff', .10)}; border-radius: 18px; }}")
        v = QVBoxLayout(bub)
        v.setContentsMargins(17, 12, 17, 12)
        v.setSpacing(8)
        if who and kind != "user":
            wl = QLabel(who)
            wl.setStyleSheet(f"color: {C.PRI}; font-weight: 600; background: transparent;")
            wl.setFont(font(11, QFont.Weight.DemiBold))
            v.addWidget(wl)
        blocks = _split_blocks(text) or [("text", text)]
        for blk in blocks:
            if blk[0] == "code":
                v.addWidget(CodeBlock(blk[1], blk[2], self.copy_text))
                continue
            lb = QLabel()
            lb.setWordWrap(True)
            lb.setFont(font(12))
            lb.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
            if kind == "user":
                lb.setTextFormat(Qt.TextFormat.PlainText)
                lb.setText(blk[1])
                lb.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            else:
                lb.setTextFormat(Qt.TextFormat.MarkdownText)
                lb.setText(_md(blk[1]))
                lb.setOpenExternalLinks(True)
                lb.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.LinksAccessibleByMouse)
            v.addWidget(lb)
        for path in _find_files(text):
            pv = _file_preview(path)
            if pv is not None:
                v.addWidget(pv, 0, Qt.AlignmentFlag.AlignLeft)
        return bub

    def add(self, kind: str, who: str, text: str):
        if kind == "ai":
            self.set_typing(False)                                 # the answer has arrived
        if self._count > 150:
            it = self._lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
            self._count -= 1
        row = _MsgRow()
        outer = QVBoxLayout(row)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)
        h = QHBoxLayout()
        h.setContentsMargins(0, 0, 0, 0)
        if kind in ("sys", "err"):
            bub = QLabel(text)
            bub.setWordWrap(True)
            bub.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            col = C.RED if kind == "err" else C.TEXT_DIM
            bub.setFont(font(9))
            bub.setStyleSheet(f"color: {col}; padding: 2px 6px; background: transparent;")
            bub.setProperty("bub", True)
            h.addWidget(bub)
            h.addStretch()
        elif kind == "user":
            h.addStretch()
            h.addWidget(self._bubble(kind, who, text))
        else:
            h.addWidget(self._bubble(kind, who, text))
            h.addStretch()
        outer.addLayout(h)
        if kind == "ai":
            bar = _ReactionBar(lambda emoji, on, t=text: self.reacted.emit(emoji, t, on), lambda t=text: self.copy_text(t))
            row.bar = bar
            outer.addWidget(bar, 0, Qt.AlignmentFlag.AlignLeft)
        self._lay.insertWidget(self._lay.count() - 1 - (1 if self._typing is not None else 0), row)
        self._count += 1
        fade_in(row, 260)



class HueSlider(QWidget):
    picked = pyqtSignal(str)
    committed = pyqtSignal(str)

    def __init__(self, color: str, parent=None):
        super().__init__(parent)
        self.setFixedHeight(30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._h = QColor(color).hsvHueF() if QColor(color).hsvHueF() >= 0 else 0.5

    def color(self) -> str:
        return QColor.fromHsvF(self._h, 0.62, 1.0).name()

    def set_color(self, hx: str):
        c = QColor(hx)
        if c.isValid() and c.hsvHueF() >= 0:
            self._h = c.hsvHueF()
            self.update()

    def _from(self, x: float):
        self._h = max(0.0, min(0.999, (x - 12) / max(1, self.width() - 24)))
        self.update()
        self.picked.emit(self.color())

    def mousePressEvent(self, e):
        self._from(e.position().x())

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.MouseButton.LeftButton:
            self._from(e.position().x())

    def mouseReleaseEvent(self, e):
        self.committed.emit(self.color())

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        bar = QRectF(12, self.height() / 2 - 5, self.width() - 24, 10)
        g = QLinearGradient(bar.topLeft(), bar.topRight())
        for i in range(0, 11):
            g.setColorAt(i / 10, QColor.fromHsvF(min(i / 10, .999), .62, 1.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawRoundedRect(bar, 5, 5)
        x = bar.left() + bar.width() * self._h
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QPointF(x, bar.center().y()), 10, 10)
        p.setBrush(QColor.fromHsvF(self._h, .62, 1.0))
        p.drawEllipse(QPointF(x, bar.center().y()), 7, 7)


class Segmented(QWidget):
    changed = pyqtSignal(str)

    def __init__(self, options: list[tuple[str, str]], current: str = "", parent=None):
        super().__init__(parent)
        self._btns: dict[str, QPushButton] = {}
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        for key, label in options:
            b = QPushButton(label)
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFixedHeight(34)
            b.clicked.connect(lambda _=False, k=key: self.select(k, True))
            self._btns[key] = b
            lay.addWidget(b)
        self._cur = current
        self._style()

    def select(self, key: str, emit: bool = False):
        self._cur = key
        self._style()
        if emit:
            self.changed.emit(key)

    def current(self) -> str:
        return self._cur

    def _style(self):
        for k, b in self._btns.items():
            on = k == self._cur
            b.setChecked(on)
            b.setStyleSheet(
                f"QPushButton {{ padding: 0 14px; border-radius: 17px; font-weight: 600;"
                f"color: {C.PRI if on else C.TEXT_MED};"
                f"background: {rgba(C.PRI, .18) if on else rgba('#ffffff', .05)};"
                f"border: 1px solid {rgba(C.PRI, .6) if on else rgba('#ffffff', .10)}; }}"
                f"QPushButton:hover {{ border-color: {C.PRI}; color: {C.TEXT}; }}")


def setting_row(title: str, sub: str, widget: QWidget | None) -> QWidget:
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 8, 0, 8)
    h.setSpacing(14)
    col = QVBoxLayout()
    col.setSpacing(2)
    t = QLabel(title)
    t.setFont(font(11, QFont.Weight.DemiBold))
    col.addWidget(t)
    if sub:
        s = QLabel(sub)
        s.setFont(font(9))
        s.setWordWrap(True)
        s.setStyleSheet(f"color: {C.TEXT_DIM};")
        col.addWidget(s)
    h.addLayout(col, 1)
    if widget is not None:
        h.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)
    return w


def section_label(text: str) -> QLabel:
    l = QLabel(text.upper())
    f = font(8, QFont.Weight.Bold)
    f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2.4)
    l.setFont(f)
    l.setStyleSheet(f"color: {C.PRI}; margin-top: 14px;")
    return l


def divider() -> QFrame:
    d = QFrame()
    d.setFixedHeight(1)
    d.setStyleSheet(f"background: {rgba('#ffffff', .07)}; border: none;")
    return d


def make_page() -> tuple[QScrollArea, QVBoxLayout]:
    sc = QScrollArea()
    sc.setWidgetResizable(True)
    sc.setFrameShape(QFrame.Shape.NoFrame)
    sc.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    sc.viewport().setAutoFillBackground(False)
    body = QWidget()
    body.setAutoFillBackground(False)
    lay = QVBoxLayout(body)
    lay.setContentsMargins(4, 0, 14, 20)
    lay.setSpacing(0)
    sc.setWidget(body)
    return sc, lay


def ghost_btn(text: str, slot=None, variant: str = "ghost"):
    from ui.fx import GlowButton
    b = GlowButton(text, variant)
    if slot:
        b.clicked.connect(lambda _=False: slot())
    return b

#by ASDcuber

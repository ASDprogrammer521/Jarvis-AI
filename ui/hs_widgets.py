"""Hand Space — live widget cards and the rich Calendar / Mail / Expenses cards.

The card header (badge, title, underline) is drawn by HandSpaceOverlay._card_text; this module paints what goes under it:
  calendar · mail · expenses · weather · nowplaying · system · clock
Every painter is pure drawing: the data comes from core.space_feeds (parsed text / JSON) and from SysSampler.
`paint(...)` returns False when there is nothing to draw yet (e.g. "Loading…"), so the card falls back to its plain text.
"""
from __future__ import annotations

import math
import time
from datetime import datetime

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QBrush, QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF, QRadialGradient

from core import space_feeds
from ui.theme import C

LIVE = ("calendar", "mail", "expenses", "weather", "nowplaying", "system", "clock")
_AL = Qt.AlignmentFlag
PAL = [C.PRI, C.ACC, C.ACC2, C.GREEN, C.AMBER, "#7aa2ff"]
_PARSED: dict = {}
_FONTS: dict = {}


# ── small helpers ──────────────────────────────────────────────────────────────────────────────────────────────────
def _F(size: int, weight=None, family: str = "Segoe UI") -> QFont:
    k = (family, size, weight)
    f = _FONTS.get(k)
    if f is None:
        f = QFont(family, size) if weight is None else QFont(family, size, weight)
        _FONTS[k] = f
    return f


def _q(c, a: int = 255) -> QColor:
    col = QColor(c)
    col.setAlpha(max(0, min(255, int(a))))
    return col


def _mix(a, b, t: float) -> QColor:
    a, b = QColor(a), QColor(b)
    t = max(0.0, min(1.0, t))
    return QColor(int(a.red() + (b.red() - a.red()) * t), int(a.green() + (b.green() - a.green()) * t),
                  int(a.blue() + (b.blue() - a.blue()) * t), int(a.alpha() + (b.alpha() - a.alpha()) * t))


def _txt(p: QPainter, r: QRectF, s, size: int = 10, color=None, weight=None, align=_AL.AlignLeft, mono: bool = False):
    """One line of text, clipped with an ellipsis."""
    f = _F(size, weight, "Cascadia Mono" if mono else "Segoe UI")
    p.setFont(f)
    p.setPen(QColor(color or C.TEXT))
    s = str(s)
    if r.width() > 4:
        s = QFontMetrics(f).elidedText(s, Qt.TextElideMode.ElideRight, int(r.width()))
    p.drawText(r, align | _AL.AlignVCenter, s)


def _bar(p: QPainter, r: QRectF, frac: float, col, track: int = 28):
    frac = max(0.0, min(1.0, frac))
    rad = r.height() / 2
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(255, 255, 255, track))
    p.drawRoundedRect(r, rad, rad)
    if frac > 0.003:
        w = max(r.height(), r.width() * frac)
        g = QLinearGradient(r.left(), 0, r.left() + w, 0)
        g.setColorAt(0, _mix(col, "#ffffff", 0.0))
        g.setColorAt(1, _mix(col, "#ffffff", 0.35))
        p.setBrush(QBrush(g))
        p.drawRoundedRect(QRectF(r.left(), r.top(), w, r.height()), rad, rad)


def _ring(p: QPainter, r: QRectF, frac: float, col, width: float, label: str = "", sub: str = "", lsize: int = 14):
    """A gauge: dim track, glowing arc from the top, number in the middle."""
    frac = max(0.0, min(1.0, frac))
    inset = width / 2 + 1
    rr = r.adjusted(inset, inset, -inset, -inset)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor(255, 255, 255, 30), width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawArc(rr, 0, 360 * 16)
    if frac > 0.004:
        for w, a in ((width + 5, 40), (width, 255)):
            p.setPen(QPen(_q(col, a), w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawArc(rr, 90 * 16, int(-frac * 360 * 16))
    if label:
        h = r.height()
        _txt(p, QRectF(r.left(), r.top() + h * (0.24 if sub else 0.3), r.width(), h * 0.3), label, lsize, "#ffffff", QFont.Weight.Bold, _AL.AlignHCenter)
        if sub:
            _txt(p, QRectF(r.left(), r.top() + h * 0.54, r.width(), h * 0.2), sub, max(7, lsize - 6), C.TEXT_DIM, None, _AL.AlignHCenter)


def _area(p: QPainter, r: QRectF, vals, col, top: float = 100.0):
    """A filled sparkline of 0..top readings."""
    p.setPen(QPen(QColor(255, 255, 255, 18), 1))
    for k in (0.25, 0.5, 0.75):
        y = r.bottom() - r.height() * k
        p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
    vals = list(vals)
    if len(vals) < 2:
        return
    n = len(vals)
    pts = [QPointF(r.left() + r.width() * i / max(1, 59), r.bottom() - r.height() * max(0.0, min(1.0, v / top)))
           for i, v in enumerate(vals)]
    shift = r.width() * (60 - n) / 59.0 if n < 60 else 0.0
    pts = [QPointF(q.x() + shift, q.y()) for q in pts]
    line = QPainterPath(pts[0])
    for q in pts[1:]:
        line.lineTo(q)
    fill = QPainterPath(line)
    fill.lineTo(QPointF(pts[-1].x(), r.bottom()))
    fill.lineTo(QPointF(pts[0].x(), r.bottom()))
    fill.closeSubpath()
    g = QLinearGradient(0, r.top(), 0, r.bottom())
    g.setColorAt(0, _q(col, 110))
    g.setColorAt(1, _q(col, 0))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(g))
    p.drawPath(fill)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(_q(col, 240), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.drawPath(line)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(_q(col, 70))
    p.drawEllipse(pts[-1], 5, 5)
    p.setBrush(QColor("#ffffff"))
    p.drawEllipse(pts[-1], 2.2, 2.2)


def _hb(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else (f"{n:.1f} {u}" if n < 100 else f"{n:.0f} {u}")
        n /= 1024.0
    return "?"


def _card(p: QPainter, r: QRectF, col=None, rad: float = 10):
    p.setPen(QPen(_q(col or "#ffffff", 34 if col else 16), 1))
    p.setBrush(_q(col or "#ffffff", 14 if col else 10))
    p.drawRoundedRect(r, rad, rad)


def _scrolled(p: QPainter, c, view: QRectF, content_h: float, draw):
    """Clipped, scrollable body (the same wheel / drag scroll the plain text cards use) with the gradient scroll bar."""
    c.scroll = max(0.0, min(c.scroll, max(0.0, content_h - view.height() + 8)))
    p.save()
    p.setClipRect(view)
    draw(view.top() - c.scroll)
    p.restore()
    if content_h > view.height():
        tr = QRectF(view.right() - 6, view.top(), 4, view.height())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 26))
        p.drawRoundedRect(tr, 2, 2)
        th = max(30.0, view.height() * view.height() / content_h)
        ty = tr.top() + (tr.height() - th) * (c.scroll / max(1.0, content_h - view.height()))
        tg = QLinearGradient(0, ty, 0, ty + th)
        tg.setColorAt(0, QColor(C.PRI))
        tg.setColorAt(1, QColor(C.ACC))
        p.setBrush(QBrush(tg))
        p.drawRoundedRect(QRectF(tr.left(), ty, 4, th), 2, 2)


def _message(p: QPainter, view: QRectF, text: str, col=None):
    _txt(p, view.adjusted(4, 0, -4, 0), text, 10, col or C.TEXT_MED, None, _AL.AlignHCenter)


# ── data ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def _data(ov, d: dict, kind: str):
    if kind == "system":
        return ov.sysdata()
    if kind == "clock":
        return datetime.now()
    text = str(d.get("full") or "")
    key = (kind, text)
    if key in _PARSED:
        return _PARSED[key]
    out = None
    if kind == "calendar":
        out = space_feeds.parse_calendar(text)
    elif kind == "mail":
        out = space_feeds.parse_mail(text)
    elif kind == "expenses":
        out = space_feeds.parse_expenses(text)
    elif kind in ("weather", "nowplaying"):
        out = space_feeds.parse_json(text)
    if len(_PARSED) > 60:
        _PARSED.clear()
    _PARSED[key] = out
    return out


def paint(ov, p: QPainter, c, kind: str, view: QRectF, opened: bool) -> bool:
    """Draw the body of a rich / live card inside `view`. False → nothing to show yet, draw the plain text instead."""
    d = c.data
    data = _data(ov, d, kind)
    if data is None:
        return False
    fn = {"calendar": _calendar, "mail": _mail, "expenses": _expenses, "weather": _weather, "nowplaying": _playing,
          "system": _system, "clock": _clock}[kind]
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    fn(ov, p, c, view, data, opened)
    return True


# ── Calendar ───────────────────────────────────────────────────────────────────────────────────────────────────────
def _calendar(ov, p, c, view, days, opened):
    tint = lambda i: PAL[i % len(PAL)]                          # noqa: E731
    if not opened:
        rows = [(dy["label"], ev, i) for dy in days for i, ev in enumerate(dy["events"])]
        if not rows:
            _message(p, view, "Nothing planned — enjoy the quiet", C.GREEN)
            return
        rh = 38.0
        fit = max(1, int((view.height() + 4) // rh))
        for n, (lab, ev, i) in enumerate(rows[:fit]):
            y = view.top() + n * rh
            col = C.AMBER if ev["allday"] else tint(n)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(col and QColor(col))
            p.drawRoundedRect(QRectF(view.left(), y + 4, 3, rh - 12), 1.5, 1.5)
            _txt(p, QRectF(view.left() + 10, y + 1, view.width() - 10, 18), ev["title"] or "(no title)", 9, "#ffffff", QFont.Weight.DemiBold)
            when = "All day" if ev["allday"] else f'{ev["start"]}–{ev["end"]}'
            _txt(p, QRectF(view.left() + 10, y + 18, view.width() - 10, 14), f"{lab} · {when}", 8, C.TEXT_DIM)
        if len(rows) > fit:
            _txt(p, QRectF(view.left(), view.bottom() - 14, view.width(), 14), f"+{len(rows) - fit} more", 8, C.PRI, None, _AL.AlignRight)
        return

    DAY, EV, EMPTY = 34.0, 56.0, 28.0
    total = sum(DAY + (len(dy["events"]) * EV if dy["events"] else EMPTY) + 6 for dy in days)

    def draw(y0: float):
        y, n = y0, 0
        w = view.width() - 14
        for dy in days:
            _txt(p, QRectF(view.left(), y, w, 28), dy["label"], 13, "#ffffff", QFont.Weight.Bold)
            lw = QFontMetrics(_F(13, QFont.Weight.Bold)).horizontalAdvance(dy["label"]) + 12
            _txt(p, QRectF(view.left() + lw, y + 2, w - lw, 26), dy["date"], 10, C.TEXT_DIM)
            y += DAY
            if not dy["events"]:
                _txt(p, QRectF(view.left() + 4, y, w, EMPTY), "Nothing planned", 10, C.TEXT_DIM)
                y += EMPTY + 6
                continue
            for ev in dy["events"]:
                col = C.AMBER if ev["allday"] else tint(n)
                n += 1
                r = QRectF(view.left(), y, w, EV - 6)
                _card(p, r, col)
                g = QLinearGradient(0, r.top(), 0, r.bottom())
                g.setColorAt(0, _q(col, 255))
                g.setColorAt(1, _q(col, 110))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(g))
                p.drawRoundedRect(QRectF(r.left(), r.top() + 6, 4, r.height() - 12), 2, 2)
                if ev["allday"]:
                    _txt(p, QRectF(r.left() + 16, r.top(), 76, r.height()), "All day", 10, col, QFont.Weight.DemiBold)
                else:
                    _txt(p, QRectF(r.left() + 16, r.top() + 6, 76, 20), ev["start"], 12, "#ffffff", QFont.Weight.Bold)
                    _txt(p, QRectF(r.left() + 16, r.top() + 24, 76, 16), ev["end"], 9, C.TEXT_DIM)
                tx = r.left() + 100
                _txt(p, QRectF(tx, r.top() + (6 if ev["loc"] else 0), r.right() - tx - 8, 22 if ev["loc"] else r.height()),
                     ev["title"] or "(no title)", 11, "#ffffff", QFont.Weight.DemiBold)
                if ev["loc"]:
                    _txt(p, QRectF(tx, r.top() + 26, r.right() - tx - 8, 16), "📍 " + ev["loc"], 9, C.TEXT_DIM)
                y += EV
            y += 6
    _scrolled(p, c, view, total, draw)


# ── Mail ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def _mail(ov, p, c, view, mail, opened):
    items = mail["items"]
    if not items:
        _message(p, view, "Inbox is empty", C.GREEN)
        return
    if not opened:
        _txt(p, QRectF(view.left(), view.top(), 70, 30), str(mail["unread"]), 22, C.AMBER if mail["unread"] else C.GREEN, QFont.Weight.Bold)
        _txt(p, QRectF(view.left() + 44, view.top() + 8, view.width() - 44, 16), "unread" if mail["unread"] else "all read", 9, C.TEXT_DIM)
        y0 = view.top() + 36
        rh = 36.0
        for n, it in enumerate(items[: max(1, int((view.bottom() - y0 + 2) // rh))]):
            y = y0 + n * rh
            if it["unread"]:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(_q(C.PRI, 60))
                p.drawEllipse(QPointF(view.left() + 5, y + 10), 6, 6)
                p.setBrush(QColor(C.PRI))
                p.drawEllipse(QPointF(view.left() + 5, y + 10), 3, 3)
            _txt(p, QRectF(view.left() + 16, y, view.width() - 16, 17), it["sender"], 9, "#ffffff" if it["unread"] else C.TEXT_MED,
                 QFont.Weight.Bold if it["unread"] else None)
            _txt(p, QRectF(view.left() + 16, y + 17, view.width() - 16, 15), it["subject"], 8, C.TEXT_DIM)
        return

    RH = 74.0
    total = 56 + len(items) * RH

    def draw(y0: float):
        w = view.width() - 14
        for k, (num, lab, col) in enumerate(((mail["unread"], "unread", C.AMBER), (mail["count"], "in the list", C.PRI))):
            r = QRectF(view.left() + k * (w / 2 + 4), y0, w / 2 - 4, 46)
            _card(p, r, col)
            _txt(p, QRectF(r.left() + 14, r.top(), 56, r.height()), str(num), 20, col, QFont.Weight.Bold)
            _txt(p, QRectF(r.left() + 14 + 40, r.top(), r.width() - 60, r.height()), lab, 10, C.TEXT_MED)
        y = y0 + 56
        for it in items:
            r = QRectF(view.left(), y, w, RH - 8)
            _card(p, r, C.PRI if it["unread"] else None)
            cx, cy = r.left() + 20, r.top() + 22
            if it["unread"]:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(_q(C.PRI, 55))
                p.drawEllipse(QPointF(cx, cy), 8, 8)
                p.setBrush(QColor(C.PRI))
                p.drawEllipse(QPointF(cx, cy), 4, 4)
            else:
                p.setPen(QPen(QColor(255, 255, 255, 50), 1.2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(QPointF(cx, cy), 4, 4)
            tx = r.left() + 38
            _txt(p, QRectF(r.right() - 110, r.top() + 6, 100, 18), it["when"], 9, C.TEXT_DIM, None, _AL.AlignRight)
            _txt(p, QRectF(tx, r.top() + 6, r.right() - tx - 116, 18), it["sender"], 11, "#ffffff" if it["unread"] else C.TEXT_MED,
                 QFont.Weight.Bold if it["unread"] else None)
            _txt(p, QRectF(tx, r.top() + 24, r.right() - tx - 10, 18), it["subject"], 10, "#ffffff" if it["unread"] else C.TEXT_MED)
            _txt(p, QRectF(tx, r.top() + 42, r.right() - tx - 10, 16), it["snippet"], 9, C.TEXT_DIM)
            _txt(p, QRectF(r.left() + 6, r.top() + 36, 28, 14), str(it["n"]), 8, C.TEXT_DIM, None, _AL.AlignHCenter)
            y += RH
    _scrolled(p, c, view, total, draw)


# ── Expenses ───────────────────────────────────────────────────────────────────────────────────────────────────────
def _expenses(ov, p, c, view, ex, opened):
    cats = ex["cats"]
    if not opened:
        _txt(p, QRectF(view.left(), view.top(), view.width(), 28), ex["spent"], 17, "#ffffff", QFont.Weight.Bold)
        _txt(p, QRectF(view.left(), view.top() + 26, view.width(), 14), f'spent · balance {ex["balance"]}', 8, C.TEXT_DIM)
        y = view.top() + 50
        if ex["empty"] or not cats:
            _txt(p, QRectF(view.left(), y, view.width(), 18), "Nothing recorded this month", 9, C.TEXT_MED)
            return
        for n, ct in enumerate(cats[: max(1, int((view.bottom() - y) // 26))]):
            col = C.RED if ct["over"] else PAL[n % len(PAL)]
            _txt(p, QRectF(view.left(), y, view.width() * 0.62, 14), ct["name"], 8, C.TEXT_MED)
            _txt(p, QRectF(view.left(), y, view.width(), 14), f'{ct["share"]}%', 8, C.TEXT_DIM, None, _AL.AlignRight)
            _bar(p, QRectF(view.left(), y + 15, view.width(), 5), ct["share"] / 100.0, col)
            y += 26
        return

    BAR, ROW = 44.0, 30.0
    n_lat = len(ex["latest"])
    total = 70 + (170 + len(cats) * BAR if cats else 40) + (34 + n_lat * ROW if n_lat else 0)

    def draw(y0: float):
        w = view.width() - 14
        stats = (("Spent", ex["spent"], C.ACC2), ("Income", ex["income"], C.GREEN), ("Balance", ex["balance"], C.PRI))
        tw = (w - 12) / 3
        for k, (lab, val, col) in enumerate(stats):
            r = QRectF(view.left() + k * (tw + 6), y0, tw, 58)
            _card(p, r, col)
            _txt(p, QRectF(r.left() + 12, r.top() + 7, r.width() - 16, 16), lab.upper(), 8, C.TEXT_DIM)
            _txt(p, QRectF(r.left() + 12, r.top() + 24, r.width() - 16, 26), val, 14, "#ffffff", QFont.Weight.Bold)
        y = y0 + 70
        if not cats:
            _txt(p, QRectF(view.left(), y, w, 24), "Nothing recorded this month", 11, C.TEXT_MED)
            return
        # donut + legend
        dr = QRectF(view.left() + 4, y + 4, 150, 150)
        p.setBrush(Qt.BrushStyle.NoBrush)
        start = 90.0
        for n, ct in enumerate(cats):
            span = ct["share"] * 3.6
            if span <= 0:
                continue
            p.setPen(QPen(QColor(C.RED) if ct["over"] else QColor(PAL[n % len(PAL)]), 16, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
            p.drawArc(dr.adjusted(10, 10, -10, -10), int(start * 16), int(-max(0.5, span - 1.2) * 16))
            start -= span
        _txt(p, QRectF(dr.left(), dr.top() + 54, dr.width(), 26), ex["spent"], 12, "#ffffff", QFont.Weight.Bold, _AL.AlignHCenter)
        _txt(p, QRectF(dr.left(), dr.top() + 78, dr.width(), 16), ex["month"][:22], 8, C.TEXT_DIM, None, _AL.AlignHCenter)
        lx = dr.right() + 22
        for n, ct in enumerate(cats[:7]):
            ly = y + 12 + n * 20
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(C.RED) if ct["over"] else QColor(PAL[n % len(PAL)]))
            p.drawRoundedRect(QRectF(lx, ly + 5, 9, 9), 3, 3)
            _txt(p, QRectF(lx + 16, ly, w - lx - 16 - 44, 18), ct["name"], 9, C.TEXT_MED)
            _txt(p, QRectF(view.left() + w - 44, ly, 44, 18), f'{ct["share"]}%', 9, C.TEXT_DIM, None, _AL.AlignRight)
        y += 170
        for n, ct in enumerate(cats):                                  # bars, with the budget status when there is one
            col = QColor(C.RED) if ct["over"] else QColor(PAL[n % len(PAL)])
            _txt(p, QRectF(view.left(), y, w * 0.5, 18), ct["name"], 10, "#ffffff", QFont.Weight.DemiBold)
            _txt(p, QRectF(view.left() + w * 0.4, y, w * 0.6, 18), ct["amount"], 10, C.TEXT_MED, None, _AL.AlignRight)
            _bar(p, QRectF(view.left(), y + 21, w, 7), ct["share"] / 100.0, col)
            if ct["status"]:
                _txt(p, QRectF(view.left(), y + 28, w, 14), f'budget {ct["budget"]} — {ct["status"]}', 8, C.RED if ct["over"] else C.TEXT_DIM)
            y += BAR
        if n_lat:
            y += 4
            _txt(p, QRectF(view.left(), y, w, 22), "LATEST", 9, C.TEXT_DIM, QFont.Weight.Bold)
            y += 28
            for e in ex["latest"]:
                _txt(p, QRectF(view.left(), y, 48, ROW), e["date"], 9, C.TEXT_DIM)
                _txt(p, QRectF(view.left() + 54, y, w * 0.45, ROW), e["cat"] + ("  🧾" if e["receipt"] else ""), 10, "#ffffff")
                _txt(p, QRectF(view.left() + w * 0.5, y, w * 0.5, ROW), ("+" if e["income"] else "−") + e["amount"], 10,
                     C.GREEN if e["income"] else C.TEXT, QFont.Weight.DemiBold, _AL.AlignRight)
                y += ROW
    _scrolled(p, c, view, total, draw)


# ── Weather ────────────────────────────────────────────────────────────────────────────────────────────────────────
def _cloud(p: QPainter, r: QRectF, col):
    x, y, w, h = r.x(), r.y(), r.width(), r.height()
    path = QPainterPath()
    path.addRoundedRect(QRectF(x + w * 0.06, y + h * 0.46, w * 0.88, h * 0.34), h * 0.17, h * 0.17)
    path.addEllipse(QRectF(x + w * 0.16, y + h * 0.26, w * 0.34, h * 0.4))
    path.addEllipse(QRectF(x + w * 0.38, y + h * 0.12, w * 0.4, h * 0.5))
    g = QLinearGradient(0, y + h * 0.12, 0, y + h * 0.8)
    g.setColorAt(0, _mix(col, "#ffffff", 0.35))
    g.setColorAt(1, QColor(col))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(g))
    p.drawPath(path.simplified())


def _wx_icon(p: QPainter, kind: str, r: QRectF, t: float):
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    x, y, w, h = r.x(), r.y(), r.width(), r.height()
    cx, cy = r.center().x(), r.center().y()

    def sun(c: QPointF, rad: float):
        g = QRadialGradient(c, rad * 2.2)
        g.setColorAt(0, _q(C.AMBER, 120))
        g.setColorAt(1, _q(C.AMBER, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawEllipse(c, rad * 2.2, rad * 2.2)
        p.setPen(QPen(_q(C.AMBER, 230), max(1.6, rad * 0.16), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        for i in range(8):
            a = math.radians(i * 45 + t * 18)
            p.drawLine(QPointF(c.x() + math.cos(a) * rad * 1.35, c.y() + math.sin(a) * rad * 1.35),
                       QPointF(c.x() + math.cos(a) * rad * 1.75, c.y() + math.sin(a) * rad * 1.75))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#ffd978"))
        p.drawEllipse(c, rad, rad)

    cloud_col = "#c9d3f0"
    if kind == "clear":
        sun(QPointF(cx, cy), w * 0.2)
    elif kind == "night":
        moon = QPainterPath()
        moon.addEllipse(QPointF(cx, cy), w * 0.26, w * 0.26)
        cut = QPainterPath()
        cut.addEllipse(QPointF(cx + w * 0.14, cy - w * 0.08), w * 0.23, w * 0.23)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#dfe6ff"))
        p.drawPath(moon.subtracted(cut))
        p.setBrush(_q("#ffffff", 200))
        for dx, dy_, s in ((0.3, -0.28, 1.6), (0.38, 0.0, 1.2), (0.18, -0.38, 1.0)):
            p.drawEllipse(QPointF(cx + w * dx, cy + h * dy_), s, s)
    elif kind == "partly":
        sun(QPointF(x + w * 0.36, y + h * 0.36), w * 0.15)
        _cloud(p, QRectF(x + w * 0.1, y + h * 0.24, w * 0.86, h * 0.8), cloud_col)
    else:
        _cloud(p, QRectF(x, y - h * 0.08 if kind in ("rain", "snow", "storm") else y, w, h * 0.9), cloud_col if kind != "storm" else "#8a93b8")
        if kind == "rain":
            p.setPen(QPen(QColor(C.PRI), max(1.6, w * 0.03), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            for i in range(3):
                ph = (t * 1.1 + i * 0.33) % 1.0
                bx, by = x + w * (0.3 + i * 0.2) - ph * w * 0.05, y + h * (0.66 + ph * 0.22)
                p.setPen(QPen(_q(C.PRI, int(255 * (1 - ph * 0.7))), max(1.6, w * 0.03), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawLine(QPointF(bx, by), QPointF(bx - w * 0.04, by + h * 0.12))
        elif kind == "snow":
            p.setPen(Qt.PenStyle.NoPen)
            for i in range(3):
                ph = (t * 0.5 + i * 0.33) % 1.0
                p.setBrush(_q("#ffffff", int(255 * (1 - ph * 0.6))))
                p.drawEllipse(QPointF(x + w * (0.3 + i * 0.2) + math.sin(t * 2 + i) * w * 0.03, y + h * (0.66 + ph * 0.22)), w * 0.028, w * 0.028)
        elif kind == "storm":
            bolt = QPolygonF([QPointF(x + w * 0.52, y + h * 0.56), QPointF(x + w * 0.4, y + h * 0.8), QPointF(x + w * 0.5, y + h * 0.8),
                              QPointF(x + w * 0.44, y + h * 1.0), QPointF(x + w * 0.64, y + h * 0.72), QPointF(x + w * 0.53, y + h * 0.72)])
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(C.AMBER if int(t * 2) % 3 else "#ffffff"))
            p.drawPolygon(bolt)
        elif kind == "fog":
            p.setPen(QPen(_q("#c9d3f0", 150), max(1.6, w * 0.035), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            for i in range(3):
                off = math.sin(t * 0.8 + i) * w * 0.04
                p.drawLine(QPointF(x + w * 0.16 + off, y + h * (0.74 + i * 0.1)), QPointF(x + w * 0.84 + off, y + h * (0.74 + i * 0.1)))
    p.restore()


def _weather(ov, p, c, view, wx, opened):
    if wx.get("error"):
        _message(p, view, wx["error"], C.AMBER)
        return
    words, icon = space_feeds.wx_kind(wx.get("code"), bool(wx.get("day", True)))
    t, unit = wx.get("t", 0), wx.get("unit", "°C")
    now = time.time()
    days = wx.get("days") or []
    if not opened:
        s = min(view.height() * 0.62, view.width() * 0.4)
        _wx_icon(p, icon, QRectF(view.left(), view.top() + 2, s, s), now)
        _txt(p, QRectF(view.left() + s + 6, view.top() + 2, view.width() - s - 6, s * 0.62), f"{t}{unit}", 26, "#ffffff", QFont.Weight.Bold)
        _txt(p, QRectF(view.left() + s + 6, view.top() + s * 0.6, view.width() - s - 6, s * 0.4), words, 9, C.TEXT_MED)
        y = view.top() + s + 10
        _txt(p, QRectF(view.left(), y, view.width(), 16), "📍 " + str(wx.get("city", "")), 9, C.TEXT_MED)
        if days and view.bottom() - y > 34:
            _txt(p, QRectF(view.left(), y + 17, view.width(), 15), f'↑ {days[0]["hi"]}°  ↓ {days[0]["lo"]}°  ·  💧 {wx.get("hum", 0)}%', 8, C.TEXT_DIM)
        return

    y = view.top() + 4
    _wx_icon(p, icon, QRectF(view.left() + 6, y, 130, 130), now)
    _txt(p, QRectF(view.left() + 150, y + 4, view.width() - 150, 70), f"{t}{unit}", 46, "#ffffff", QFont.Weight.Bold)
    _txt(p, QRectF(view.left() + 152, y + 74, view.width() - 150, 24), words, 14, C.TEXT_MED)
    _txt(p, QRectF(view.left() + 152, y + 98, view.width() - 150, 22), "📍 " + str(wx.get("city", "")), 11, C.TEXT_DIM)
    y += 146
    w = view.width() - 14
    tw = (w - 12) / 3
    for k, (lab, val, col) in enumerate((("FEELS LIKE", f'{wx.get("feels", t)}{unit}', C.AMBER), ("HUMIDITY", f'{wx.get("hum", 0)}%', C.PRI),
                                         ("WIND", f'{wx.get("wind", 0)} {wx.get("wunit", "km/h")}', C.ACC))):
        r = QRectF(view.left() + k * (tw + 6), y, tw, 56)
        _card(p, r, col)
        _txt(p, QRectF(r.left() + 12, r.top() + 7, r.width() - 12, 16), lab, 8, C.TEXT_DIM)
        _txt(p, QRectF(r.left() + 12, r.top() + 24, r.width() - 12, 26), val, 14, "#ffffff", QFont.Weight.Bold)
    y += 70
    if days:
        _txt(p, QRectF(view.left(), y, w, 20), "NEXT DAYS", 9, C.TEXT_DIM, QFont.Weight.Bold)
        y += 24
        dw = w / len(days)
        lo = min(d["lo"] for d in days)
        hi = max(d["hi"] for d in days)
        span = max(1, hi - lo)
        for i, dd in enumerate(days):
            r = QRectF(view.left() + i * dw + 2, y, dw - 4, 156)
            _card(p, r, C.PRI if i == 0 else None)
            try:
                name = datetime.strptime(dd["d"], "%Y-%m-%d").strftime("%a")
            except ValueError:
                name = "—"
            _txt(p, QRectF(r.left(), r.top() + 6, r.width(), 18), "Today" if i == 0 else name, 9, C.TEXT_MED, QFont.Weight.DemiBold, _AL.AlignHCenter)
            _wx_icon(p, space_feeds.wx_kind(dd["code"], True)[1], QRectF(r.left() + r.width() * 0.2, r.top() + 26, r.width() * 0.6, r.width() * 0.6), now)
            _txt(p, QRectF(r.left(), r.top() + 26 + r.width() * 0.62, r.width(), 18), f'{dd["hi"]}°', 11, "#ffffff", QFont.Weight.Bold, _AL.AlignHCenter)
            tr = QRectF(r.center().x() - 3, r.top() + 26 + r.width() * 0.62 + 22, 6, 36)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 26))
            p.drawRoundedRect(tr, 3, 3)
            a, b = (dd["hi"] - lo) / span, (dd["lo"] - lo) / span
            g = QLinearGradient(0, tr.top(), 0, tr.bottom())
            g.setColorAt(0, QColor(C.AMBER))
            g.setColorAt(1, QColor(C.PRI))
            p.setBrush(QBrush(g))
            p.drawRoundedRect(QRectF(tr.left(), tr.bottom() - tr.height() * max(a, 0.18), 6, tr.height() * max(a - b, 0.18)), 3, 3)
            _txt(p, QRectF(r.left(), tr.bottom() + 4, r.width(), 16), f'{dd["lo"]}°', 9, C.TEXT_DIM, None, _AL.AlignHCenter)


# ── Now playing ────────────────────────────────────────────────────────────────────────────────────────────────────
def _equalizer(p: QPainter, r: QRectF, col, playing: bool, t: float, bars: int = 14):
    gap = r.width() / bars
    bw = gap * 0.58
    for i in range(bars):
        f = 0.35 + 0.65 * abs(math.sin(t * (2.2 + (i % 5) * 0.7) + i * 1.3)) if playing else 0.14 + 0.05 * (i % 3)
        h = max(3.0, r.height() * f)
        g = QLinearGradient(0, r.bottom() - h, 0, r.bottom())
        g.setColorAt(0, _mix(col, "#ffffff", 0.3))
        g.setColorAt(1, _q(col, 120))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawRoundedRect(QRectF(r.left() + i * gap, r.bottom() - h, bw, h), bw / 2, bw / 2)


def _disc(p: QPainter, c: QPointF, rad: float, t: float, spin: bool):
    p.save()
    p.translate(c)
    p.rotate((t * 40) % 360 if spin else 0)
    g = QRadialGradient(QPointF(0, 0), rad)
    g.setColorAt(0, QColor("#1c1f3a"))
    g.setColorAt(1, QColor("#07080f"))
    p.setPen(QPen(_q(C.GREEN, 90), 1.4))
    p.setBrush(QBrush(g))
    p.drawEllipse(QPointF(0, 0), rad, rad)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor(255, 255, 255, 14), 1))
    for k in (0.55, 0.7, 0.85):
        p.drawEllipse(QPointF(0, 0), rad * k, rad * k)
    cg = QLinearGradient(-rad * 0.3, -rad * 0.3, rad * 0.3, rad * 0.3)
    cg.setColorAt(0, QColor(C.GREEN))
    cg.setColorAt(1, QColor(C.PRI))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(cg))
    p.drawEllipse(QPointF(0, 0), rad * 0.3, rad * 0.3)
    p.setBrush(QColor("#07080f"))
    p.drawEllipse(QPointF(0, 0), rad * 0.07, rad * 0.07)
    p.setBrush(_q("#ffffff", 40))
    p.drawPie(QRectF(-rad, -rad, rad * 2, rad * 2), 40 * 16, 30 * 16)
    p.restore()


def _playing(ov, p, c, view, np_, opened):
    state = np_.get("state", "none")
    now = time.time()
    if state == "none":
        _disc(p, QPointF(view.center().x(), view.top() + min(view.height() * 0.34, 70)), min(view.height() * 0.24, 46), now, False)
        _txt(p, QRectF(view.left(), view.bottom() - view.height() * 0.38, view.width(), 22), "Nothing playing", 11, C.TEXT_MED, QFont.Weight.DemiBold, _AL.AlignHCenter)
        _txt(p, QRectF(view.left(), view.bottom() - view.height() * 0.38 + 22, view.width(), 18), "Start Spotify and it shows up here", 8, C.TEXT_DIM, None, _AL.AlignHCenter)
        return
    playing = state == "playing"
    rad = min(view.height() * (0.27 if opened else 0.25), 90 if opened else 44)
    cx = view.left() + rad + 4
    _disc(p, QPointF(cx, view.top() + rad + 6), rad, now, playing)
    tx = cx + rad + 16
    tw = view.right() - tx
    big = opened
    _txt(p, QRectF(tx, view.top() + 2, tw, 14), "NOW PLAYING" if playing else "PAUSED", 8, C.GREEN if playing else C.AMBER, QFont.Weight.Bold)
    _txt(p, QRectF(tx, view.top() + 18, tw, 30 if big else 22), np_.get("title") or "—", 16 if big else 11, "#ffffff", QFont.Weight.Bold)
    _txt(p, QRectF(tx, view.top() + (50 if big else 40), tw, 20), np_.get("artist") or "", 11 if big else 9, C.TEXT_MED)
    eq_h = max(26.0, min(view.height() * 0.3, 70.0))
    _equalizer(p, QRectF(view.left(), view.bottom() - eq_h, view.width(), eq_h), C.GREEN, playing, now, 22 if opened else 14)


# ── This computer ──────────────────────────────────────────────────────────────────────────────────────────────────
def _system(ov, p, c, view, s, opened):
    if s.get("error") or "cpu" not in s:
        _message(p, view, s.get("error", "Reading the system…"), C.AMBER)
        return
    cpu, mem, disk = s["cpu"], s["mem"], s["disk"]
    hot = lambda v: C.RED if v > 90 else (C.AMBER if v > 75 else None)       # noqa: E731
    cc, mc, dc = hot(cpu) or C.PRI, hot(mem) or C.ACC, hot(disk) or C.GREEN
    if not opened:
        d = min(view.height() * 0.5, view.width() * 0.42)
        _ring(p, QRectF(view.left(), view.top(), d, d), cpu / 100.0, cc, 6, f"{cpu:.0f}%", "CPU", 11)
        _ring(p, QRectF(view.right() - d, view.top(), d, d), mem / 100.0, mc, 6, f"{mem:.0f}%", "RAM", 11)
        y = view.top() + d + 10
        _txt(p, QRectF(view.left(), y, view.width(), 14), f'Disk {disk:.0f}% · {_hb(s["disk_free"])} free', 8, C.TEXT_DIM)
        _bar(p, QRectF(view.left(), y + 16, view.width(), 5), disk / 100.0, dc)
        if view.bottom() - y > 44:
            _txt(p, QRectF(view.left(), y + 24, view.width(), 14), f'↓ {_hb(s["down"])}/s  ↑ {_hb(s["up"])}/s', 8, C.TEXT_DIM)
        return
    w = view.width() - 6
    y = view.top() + 4
    half = (w - 12) / 2
    for k, (lab, val, col, hist, sub) in enumerate((("CPU", cpu, cc, ov.sys.cpu_hist, f"{cpu:.0f}%"),
                                                    ("MEMORY", mem, mc, ov.sys.mem_hist, f'{_hb(s["mem_used"])} of {_hb(s["mem_total"])}'))):
        r = QRectF(view.left() + k * (half + 12), y, half, 190)
        _card(p, r, col, 12)
        _txt(p, QRectF(r.left() + 14, r.top() + 8, r.width() - 20, 18), lab, 9, C.TEXT_DIM, QFont.Weight.Bold)
        _txt(p, QRectF(r.left() + 14, r.top() + 26, r.width() - 20, 38), f"{val:.0f}%", 26, "#ffffff", QFont.Weight.Bold)
        if k:
            _txt(p, QRectF(r.left() + 14, r.top() + 62, r.width() - 20, 16), sub, 9, C.TEXT_MED)
        _area(p, QRectF(r.left() + 12, r.top() + 84, r.width() - 24, 90), hist, col)
    y += 206
    _txt(p, QRectF(view.left(), y, w * 0.6, 20), "Disk", 11, "#ffffff", QFont.Weight.DemiBold)
    _txt(p, QRectF(view.left(), y, w, 20), f'{_hb(s["disk_free"])} free of {_hb(s["disk_total"])}', 10, C.TEXT_MED, None, _AL.AlignRight)
    _bar(p, QRectF(view.left(), y + 26, w, 8), disk / 100.0, dc)
    y += 52
    secs = max(0, int(time.time() - s["boot"]))
    up = f"{secs // 86400}d {secs % 86400 // 3600}h" if secs >= 86400 else f"{secs // 3600}h {secs % 3600 // 60}m"
    tiles = [("DOWNLOAD", f'{_hb(s["down"])}/s', C.PRI), ("UPLOAD", f'{_hb(s["up"])}/s', C.ACC2), ("UPTIME", up, C.AMBER)]
    if s.get("bat") is not None:
        tiles.append(("BATTERY", f'{s["bat"]:.0f}%' + (" ⚡" if s.get("plugged") else ""), C.GREEN))
    tw = (w - 6 * (len(tiles) - 1)) / len(tiles)
    for k, (lab, val, col) in enumerate(tiles):
        r = QRectF(view.left() + k * (tw + 6), y, tw, 56)
        _card(p, r, col)
        _txt(p, QRectF(r.left() + 10, r.top() + 7, r.width() - 12, 16), lab, 8, C.TEXT_DIM)
        _txt(p, QRectF(r.left() + 10, r.top() + 24, r.width() - 12, 26), val, 12, "#ffffff", QFont.Weight.Bold)


# ── Clock ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def _analog(p: QPainter, r: QRectF, now: datetime, col):
    cx, cy, rad = r.center().x(), r.center().y(), min(r.width(), r.height()) / 2 - 3
    g = QRadialGradient(QPointF(cx, cy), rad)
    g.setColorAt(0, _q(col, 24))
    g.setColorAt(1, _q(col, 5))
    p.setPen(QPen(_q(col, 150), 1.6))
    p.setBrush(QBrush(g))
    p.drawEllipse(QPointF(cx, cy), rad, rad)
    for i in range(60):
        a = math.radians(i * 6 - 90)
        big = i % 5 == 0
        r1, r2 = rad * (0.84 if big else 0.91), rad * 0.96
        p.setPen(QPen(QColor(255, 255, 255, 190 if big else 60), 1.6 if big else 1.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(cx + math.cos(a) * r1, cy + math.sin(a) * r1), QPointF(cx + math.cos(a) * r2, cy + math.sin(a) * r2))
    ms = time.time() % 1.0
    sec = now.second + ms
    for ang, ln, wd, cl in (((now.hour % 12 + now.minute / 60) * 30, 0.5, 4.2, "#ffffff"), ((now.minute + sec / 60) * 6, 0.74, 3.0, "#ffffff"),
                            (sec * 6, 0.82, 1.6, C.ACC2)):
        a = math.radians(ang - 90)
        p.setPen(QPen(QColor(cl), wd, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(cx - math.cos(a) * rad * 0.1, cy - math.sin(a) * rad * 0.1), QPointF(cx + math.cos(a) * rad * ln, cy + math.sin(a) * rad * ln))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(C.ACC2))
    p.drawEllipse(QPointF(cx, cy), 4, 4)


def _clock(ov, p, c, view, now: datetime, opened):
    col = C.PRI
    if not opened:
        _txt(p, QRectF(view.left(), view.top(), view.width(), view.height() * 0.5), now.strftime("%H:%M"), 30, "#ffffff", QFont.Weight.Bold, _AL.AlignHCenter)
        sec = (now.second + (time.time() % 1.0)) / 60.0
        _bar(p, QRectF(view.left() + 6, view.top() + view.height() * 0.52, view.width() - 12, 4), sec, col)
        _txt(p, QRectF(view.left(), view.top() + view.height() * 0.6, view.width(), 18), now.strftime("%A"), 10, C.TEXT_MED, QFont.Weight.DemiBold, _AL.AlignHCenter)
        _txt(p, QRectF(view.left(), view.top() + view.height() * 0.6 + 18, view.width(), 16), now.strftime("%d %B %Y"), 9, C.TEXT_DIM, None, _AL.AlignHCenter)
        return
    s = min(view.height() * 0.62, view.width() * 0.5, 280)
    _analog(p, QRectF(view.left() + 4, view.top() + 8, s, s), now, col)
    tx = view.left() + s + 30
    tw = view.right() - tx
    _txt(p, QRectF(tx, view.top() + 10, tw, 70), now.strftime("%H:%M"), 52, "#ffffff", QFont.Weight.Bold)
    _txt(p, QRectF(tx, view.top() + 78, tw, 28), now.strftime(":%S"), 18, C.ACC2, QFont.Weight.DemiBold)
    _txt(p, QRectF(tx, view.top() + 112, tw, 26), now.strftime("%A"), 16, C.TEXT, QFont.Weight.DemiBold)
    _txt(p, QRectF(tx, view.top() + 138, tw, 22), now.strftime("%d %B %Y"), 12, C.TEXT_MED)
    y = view.top() + s + 34
    day = (now.hour * 3600 + now.minute * 60 + now.second) / 86400.0
    yr = (now.timetuple().tm_yday - 1 + day) / (366.0 if now.year % 4 == 0 else 365.0)
    wk = now.isocalendar()[1]
    w = view.width() - 14
    for lab, frac, val, cl in (("Today", day, f"{day * 100:.0f}%", C.PRI), ("This year", yr, f"{yr * 100:.0f}% · week {wk}", C.ACC)):
        _txt(p, QRectF(view.left(), y, w * 0.5, 18), lab, 10, C.TEXT_MED)
        _txt(p, QRectF(view.left(), y, w, 18), val, 10, C.TEXT_DIM, None, _AL.AlignRight)
        _bar(p, QRectF(view.left(), y + 22, w, 6), frac, cl)
        y += 42

#by ASDcuber

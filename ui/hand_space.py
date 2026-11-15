"""Hand Space window: a full-screen, hand-controlled file workspace (drives → folders → pictures and notes).

Logic and physics live in core/hand_space.py; this file only draws them. The mouse works too (handy without a camera).
Pictures are decoded in background threads, so browsing a big photo folder never stalls the screen.

GPU: when OpenGL is on (Settings → hw_accel, default) the whole overlay is drawn by the GPU through an OpenGL canvas;
3D models (.obj / .stl) are shaded by their own shader and videos are decoded by the GPU (mpv hwdec) and rendered
into a texture (ui/hs_gpu.py). Set "hand_space_gpu": false in the config to fall back to the CPU painter.
"""
from __future__ import annotations

import math
import os
import time
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath,
                         QPen, QPixmap, QPolygonF, QRadialGradient, QSurfaceFormat, QTextOption)
from PyQt6.QtWidgets import QWidget

from core.hand_space import Card, Space, human_size
from core import map_service, space_feeds
from core.space_map import TILE as MAP_TILE, ancestor as map_ancestor
from ui import hs_gpu, mesh_thumb
from ui.fx import GL_ENABLED, samples_for
from ui.hand_overlay import BONES
from ui.mesh_formats import HEAVY_EXT
from ui.overlay_utils import force_topmost
from ui.theme import C, read_cfg

try:
    from PyQt6.QtOpenGLWidgets import QOpenGLWidget as _GLW
except Exception:                                     # pragma: no cover - PyQt build without OpenGL widgets
    _GLW = None

THUMB_SIDE, LARGE_SIDE = 480, 3200
_AL = Qt.AlignmentFlag
PROFILE = bool(os.environ.get("JARVIS_PROFILE"))               # set JARVIS_PROFILE=1 to print timings every 3 s

_FONTS: dict = {}
_ELIDE: dict = {}
_FM: dict = {}


def _F(size, weight=None, family: str = "Segoe UI", spacing=None) -> QFont:
    """Shared QFont objects: building a font (and its metrics) for every card on every frame was pure CPU waste."""
    k = (family, size, weight, spacing)
    f = _FONTS.get(k)
    if f is None:
        f = QFont(family, size) if weight is None else QFont(family, size, weight)
        if spacing:
            f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
        _FONTS[k] = f
    return f


def _q(c, a: int = 255) -> QColor:
    """A colour (hex string or QColor) with its own alpha."""
    col = QColor(c)
    col.setAlpha(max(0, min(255, int(a))))
    return col


def _mix(a, b, t: float) -> QColor:
    """Linear blend of two colours (alpha included): t = 0 → a, t = 1 → b."""
    a, b = QColor(a), QColor(b)
    t = max(0.0, min(1.0, t))
    return QColor(int(a.red() + (b.red() - a.red()) * t), int(a.green() + (b.green() - a.green()) * t),
                  int(a.blue() + (b.blue() - a.blue()) * t), int(a.alpha() + (b.alpha() - a.alpha()) * t))


def _accent_for(d: dict) -> str:
    """Every kind of card has its own accent colour, so a screen full of cards reads at a glance."""
    kind = d.get("kind", "file")
    if kind == "text":
        if d.get("accent"):                                       # a card the user designed keeps its own colour
            return d["accent"]
        return {"Calendar": C.PRI, "Mail": C.AMBER, "Expenses": C.GREEN}.get(d.get("title"), C.ACC)
    return {"drive": C.PRI, "folder": C.AMBER, "image": C.PRI, "model": C.ACC, "video": C.ACC2, "map": C.PRI}.get(kind, C.TEXT_MED)


def _emoji_for(d: dict) -> str:
    """The icon a user picked for a note card (empty for every other card)."""
    return str(d.get("icon") or "") if d.get("kind") == "text" else ""


def _emoji_font(px: int) -> QFont:
    f = _FONTS.get(("emoji", px))
    if f is None:
        f = QFont("Segoe UI Emoji")
        f.setPixelSize(px)
        _FONTS[("emoji", px)] = f
    return f


def _glyph_for(d: dict) -> str:
    kind = d.get("kind", "file")
    if kind == "text":
        return {"Calendar": "calendar", "Mail": "mail", "Expenses": "wallet"}.get(d.get("title"), "code" if d.get("mono") else "doc")
    return {"folder": "folder", "image": "image", "model": "cube", "video": "film", "map": "globe"}.get(kind, "doc")


def _elided(text: str, size: int, width: int) -> str:
    k = (text, size, width)
    r = _ELIDE.get(k)
    if r is None:
        fm = _FM.get(size)
        if fm is None:
            fm = _FM[size] = QFontMetrics(_F(size, QFont.Weight.DemiBold))
        r = fm.elidedText(text, Qt.TextElideMode.ElideMiddle, width)
        if len(_ELIDE) > 800:
            _ELIDE.clear()
        _ELIDE[k] = r
    return r


def _decode(path: str, side: int):
    """Runs in a worker thread: returns (QImage, width, height). PIL is fast (JPEG draft mode) and honours EXIF rotation."""
    try:
        from PIL import Image, ImageOps
        with Image.open(path) as im:
            ow, oh = im.size
            try:
                im.draft("RGB", (side, side))
            except Exception:
                pass
            im = ImageOps.exif_transpose(im)
            im.thumbnail((side, side), Image.BILINEAR)
            im = im.convert("RGBA")
            w, h = im.size
            qi = QImage(im.tobytes("raw", "RGBA"), w, h, w * 4, QImage.Format.Format_RGBA8888).copy()
            real = (max(ow, oh), min(ow, oh)) if w >= h else (min(ow, oh), max(ow, oh))
            return qi, w, h, real
    except Exception:
        qi = QImage(path)
        if qi.isNull():
            return None
        if max(qi.width(), qi.height()) > side:
            qi = qi.scaled(side, side, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        return qi, qi.width(), qi.height(), (qi.width(), qi.height())


if _GLW is not None:
    class _Canvas(_GLW):
        """Full-size OpenGL surface: every QPainter call of the overlay runs on the GPU, and 3D / video draw into it natively."""

        def __init__(self, owner):
            super().__init__(owner)
            self._owner = owner
            fmt = QSurfaceFormat()
            fmt.setAlphaBufferSize(8)
            fmt.setDepthBufferSize(24)
            fmt.setStencilBufferSize(8)
            fmt.setSamples(samples_for(str(read_cfg().get("quality") or "auto")))
            self.setFormat(fmt)
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)     # the overlay keeps the mouse / keyboard

        def paintGL(self):
            self._owner._paint_gl(self)


class HandSpaceOverlay(QWidget):
    closed = pyqtSignal()

    def __init__(self):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.space = Space(1920, 1080)
        self.space.on_release = self._release_files
        self._thumbs: OrderedDict[str, QPixmap] = OrderedDict()
        self._large: OrderedDict[str, QPixmap] = OrderedDict()
        self._jobs: dict[tuple, Future] = {}
        self._failed: set[str] = set()
        self._pool = ThreadPoolExecutor(max_workers=2)
        self._bg: QPixmap | None = None
        self._idle = 0
        self._fs = False                       # True while the open item is drawn full screen (no card chrome)
        self._chromes: dict = {}               # cached card bodies: one texture per size instead of gradients every frame
        self._state = None
        self._last = time.time()
        # GPU: 3D models, video, OpenGL canvas
        self._mesh = hs_gpu.GlMesh()
        self._video = hs_gpu.GlVideo()
        self._mpool = ThreadPoolExecutor(max_workers=1)
        self._tpool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hs-thumb")    # card pictures of 3D models, one at a time
        self._mthumbs: OrderedDict = OrderedDict()                     # path → QPixmap (the picture a model card shows)
        self._mthumb_jobs: dict[str, Future] = {}
        self._mthumb_failed: set[str] = set()
        self._npool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hs-net")      # calendar / mail / expenses / place search
        self._njobs: list[Future] = []
        self._tiles = None                                           # map_service.TileStore, created when the map is first opened
        self._geo = None
        self._tilepx: OrderedDict = OrderedDict()                    # (z, x, y) → QPixmap (one GPU texture each)
        self._tile_req = ()
        self._tile_req_t = 0.0
        self._mjobs: dict[str, Future] = {}
        self._mdata: dict[str, tuple] = {}
        self._mfail: dict[str, str] = {}
        self._tris: dict[str, int] = {}
        self._vfail: dict[str, str] = {}
        self._gl_tried = False
        self._pre = False
        self._fn = 0
        self._pt = [0.0, 0]
        self._live = 0
        self._anim = False
        self._cm = QColor(C.PRI).getRgb()[:3]
        self._ca = QColor(C.ACC).getRgb()[:3]
        self._cv = None
        self._pms = 0.0                        # recent paint time (ms): decides whether ~60 fps is affordable
        self._why = ""                        # cached reason the OpenGL features are off (see _gl_why)
        if _GLW is not None and GL_ENABLED and read_cfg().get("hand_space_gpu", True):
            try:
                self._cv = _Canvas(self)
                self._cv.setGeometry(self.rect())
            except Exception:
                self._cv = None
        self._t = QTimer(self)
        self._t.timeout.connect(self._frame)

    # ── lifecycle ────────────────────────────────────────────────────────────────
    def show_overlay(self, pinned: str | None = None):
        scr = QGuiApplication.primaryScreen().geometry()
        self.setGeometry(scr)
        self.space.resize(scr.width(), scr.height())
        cfg = read_cfg()
        self.space.virtual = bool(cfg.get("hs_extra_cards", True))     # Map / Calendar / Mail / Expenses beside the drives
        self.space.start(pinned)                       # always begins on "This PC": the drives, C: first
                               # each open-hand gesture can be switched off in the config
        self.space.g_swipe = bool(cfg.get("hs_swipe", True))
        self.space.g_palm = bool(cfg.get("hs_palm_pause", True))
        self.space.g_twist = bool(cfg.get("hs_twist_volume", True))
        home = cfg.get("hs_map_home")                  # [latitude, longitude, zoom] the map starts at (default: the whole world)
        try:
            if isinstance(home, (list, tuple)) and len(home) == 3:
                self.space.mapview.jump(float(home[0]), float(home[1]), float(home[2]))
        except (TypeError, ValueError):
            pass
        self.space.close_requested = False
        self._bg = None
        self._why = ""
        self.show()
        force_topmost(self, click_through=False)
        self.activateWindow()
        self.setFocus()
        self._last = time.time()
        self._t.start(28)
        if self._cv is not None:
            self._cv.setGeometry(self.rect())
            QTimer.singleShot(700, self._check_gl)

    def _net_job(self, req: tuple):
        """Runs in a worker thread: one calendar / mail / expenses / receipt job, or a place search."""
        if req[0] == "geocode":
            if self._geo is None:
                self._geo = map_service.Geocoder(lang=str(read_cfg().get("hs_map_lang") or "en"))
            return "geocode", self._geo.search(req[1])
        return space_feeds.handle(req)

    def _tile_store(self):
        if self._tiles is None:
            self._tiles = map_service.TileStore(str(read_cfg().get("hs_map_tiles") or map_service.OSM_TEMPLATE))
        return self._tiles

    def voice(self, cmd: str):
        """A spoken command from the hand_control tool, \"verb\" or \"verb:argument\" — the Space only runs allow-listed verbs."""
        verb, _, arg = str(cmd).partition(":")
        self.space.voice(verb, arg)

    def _gl_why(self) -> str:
        """The real reason the 3D models / GPU video are off, instead of a one-size-fits-all guess."""
        if not self._why:
            if _GLW is None:
                self._why = "PyQt6 OpenGL widgets are missing (pip install PyQt6)"
            elif not GL_ENABLED:
                self._why = "GPU acceleration was off when Jarvis started (turn it on in Settings, then restart Jarvis)"
            elif not read_cfg().get("hand_space_gpu", True):
                self._why = '"hand_space_gpu" is false in config/api_keys.json (set it to true or delete it)'
            elif self._cv is None:
                self._why = "the OpenGL canvas could not start on this GPU / driver"
            else:
                self._why = "3D shader failed: " + (self._mesh.err or "unknown error")[:100]
        return self._why

    def _check_gl(self):
        """If the OpenGL canvas could not start on this machine, drop it and paint on the CPU as before."""
        cv = self._cv
        if cv is not None and self.isVisible() and not cv.isValid():
            self._cv = None
            cv.hide()
            cv.deleteLater()
            self._bg = None
            self._why = ""
            QWidget.update(self)

    def update(self, *a):                      # noqa: A003 - repaint the GL canvas when there is one
        cv = self._cv
        if cv is not None:
            cv.update()
        else:
            super().update(*a)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._cv is not None:
            self._cv.setGeometry(self.rect())

    def _gpu_release(self):
        """Free the video player and the 3D buffers (needs the canvas' context)."""
        cv = self._cv
        for f in self._mjobs.values():
            f.cancel()
        self._mjobs.clear()
        self._tilepx.clear()                                         # the map's tiles are GPU textures: let them go with the window
        self._tile_req = ()
        self._mdata.clear()
        self._mfail.clear()
        self._vfail.clear()
        if cv is None:
            self._video.stop()
            return
        try:
            cv.makeCurrent()
            self._video.stop()
            self._mesh.drop_all()
            cv.doneCurrent()
        except Exception:
            pass

    def hide(self):                           # noqa: A003
        self._t.stop()
        super().hide()

    def hideEvent(self, e):
        self._t.stop()
        self._gpu_release()
        for f in self._jobs.values():
            f.cancel()
        self._jobs.clear()
        self._thumbs.clear()                  # give the memory back
        self._large.clear()
        self._failed.clear()
        super().hideEvent(e)
        self.closed.emit()

    def set_hands(self, all_lm):
        if not self.isVisible():
            return
        if PROFILE:                                                      # how often does the hand data really arrive?
            self._hn = getattr(self, "_hn", 0) + 1
            t = time.time()
            t0 = getattr(self, "_hn_t", t)
            self._hn_t = t0
            if t - t0 >= 3.0:
                print(f"[hand space] hand input {self._hn / (t - t0):.1f} Hz")
                self._hn, self._hn_t = 0, t
        self.space.update_hands(all_lm)
        if self.space.close_requested:
            self.hide()

    def _frame(self):
        now = time.time()
        self.space.step(now - self._last, now)
        self._last = now
        self._poll_images()
        self._poll_models()
        sp = self.space
        op = sp.opened
        if op is not None and op.cmds:                       # buttons / drags aimed at the video player
            cmds, op.cmds = op.cmds, []
            if op.data.get("kind") == "video":
                self._video.apply(cmds)
        if self._video.active and (op is None or op.data.get("kind") != "video" or not op.open):
            self._video.park()                               # film closed: keep the player ready for the next one
        self._fn += 1
        if not self._pre and self._fn % 15 == 0 and any(e.get("kind") == "video" for e in sp.entries):
            self._pre = True                                 # this folder has films: build the player in the background now
            hs_gpu.prewarm_player()
        anim, self._anim = self._anim, False
        state = (sp.toast, sp.hover_btn, id(sp.hover_card), sp.version, round(sp.ui_a, 2), round(sp.fk, 2), sp.page,
                 sp.select, len(sp.sel), sp.prompt is not None, sp.menu)
        changed, self._state = state != self._state, state
        busy = (changed or any(sp.visible) or op is not None or sp.prompt is not None or self._jobs or self._mjobs or anim
                or any(c.a < 0.99 or c.homing or c.k > 0.01 or c.hov > 0.01 or abs(c.vx) + abs(c.vy) > 2.0 for c in sp.cards))
        self._idle += 0 if busy else 1
        fast = self._cv is not None and 0.0 < self._pms < 9.0 and any(sp.visible)             # a hand in view and cheap frames: 60 fps ring
        want = 16 if ((self._video.active and op is not None and not op.paused) or fast) else 28
        if self._t.interval() != want:
            self._t.setInterval(want)
        if busy:                                   # an idle screen (nothing moving, nobody there) is not repainted at all
            self.update()

    def _gpu_stop_video(self):
        cv = self._cv
        if cv is None:
            self._video.stop()
            return
        try:
            cv.makeCurrent()
            self._video.stop()
            cv.doneCurrent()
        except Exception:
            pass

    # ── 3D models (background parse → GPU upload) ───────────────────────────────────
    def _poll_models(self):
        for path, fut in list(self._mjobs.items()):
            if not fut.done():
                continue
            del self._mjobs[path]
            try:
                self._mdata[path] = fut.result()
            except Exception as e:
                self._mfail[path] = str(e) or "unreadable"
        sp = self.space
        while sp.requests:                                               # jobs the Space asked for → worker threads
            req = sp.requests.pop(0)
            if len(self._njobs) >= 8:
                sp.deliver("toast", "Still busy — try again in a moment")
                break
            self._njobs.append(self._npool.submit(self._net_job, req))
        for fut in [f for f in self._njobs if f.done()]:
            self._njobs.remove(fut)
            try:
                what, payload = fut.result()
            except map_service.MapError as e:
                what, payload = "geocode", str(e)
            except Exception:
                what, payload = "toast", "That didn't work."
            sp.deliver(what, payload)
        if self._tiles is not None:                                      # downloaded tiles → GPU textures, a few per frame
            for key, data in self._tiles.poll(3):
                img = QImage.fromData(data)
                if not img.isNull():
                    self._tilepx[key] = QPixmap.fromImage(img)
                    self._tilepx.move_to_end(key)
            while len(self._tilepx) > 360:
                self._tilepx.popitem(last=False)
        for path, fut in list(self._mthumb_jobs.items()):                # finished card pictures → pixmaps (GUI thread)
            if not fut.done():
                continue
            del self._mthumb_jobs[path]
            try:
                arr = fut.result()
            except Exception:
                arr = None
            if arr is None or arr.ndim != 3 or arr.shape[2] != 4:
                self._mthumb_failed.add(path)
                continue
            h, w = arr.shape[:2]
            img = QImage(arr.tobytes(), w, h, w * 4, QImage.Format.Format_RGBA8888).copy()
            self._mthumbs[path] = QPixmap.fromImage(img)
            while len(self._mthumbs) > 200:
                self._mthumbs.popitem(last=False)

    def _upload_models(self):
        """Runs inside paintGL (the context is current): one upload per frame so a big model never freezes the screen."""
        for path in list(self._mdata):
            data, tris, meta = self._mdata.pop(path)
            if self._mesh.ok and self._mesh.upload(path, data, meta):
                self._tris[path] = tris
                if path not in self._mthumbs and path not in self._mthumb_jobs and path not in self._mthumb_failed:
                    self._mthumb_jobs[path] = self._tpool.submit(mesh_thumb.store_thumb, path, data, meta)   # opened once → card picture for next time
            else:
                self._mfail[path] = self._mesh.err or "GPU upload failed"
            break

    def _mesh_state(self, c: Card, opened: bool) -> str:
        path = c.data["path"]
        if self._mesh.has(path):
            return "ready"
        if path in self._mfail:
            return "failed"
        if path in self._mjobs or path in self._mdata:
            return "loading"
        if opened and len(self._mjobs) < 2:                              # models are read only when opened; cards just show "3D"
            self._mjobs[path] = self._mpool.submit(hs_gpu.load_mesh, path)
            return "loading"
        return "idle"

    def _model_thumb(self, c: Card):
        """The picture of a model card (QPixmap) or None: it is made once, in a worker thread, and cached on disk, so the card
        shows it at once from then on. Big / slow formats (STEP, USD …) get theirs the first time they are opened."""
        path = c.data["path"]
        px = self._mthumbs.get(path)
        if px is not None:
            self._mthumbs.move_to_end(path)
            return px
        if (path in self._mthumb_failed or path in self._mthumb_jobs or len(self._mthumb_jobs) >= 3
                or c.data.get("_sz", 0) > hs_gpu.SMALL_MODEL_BYTES or os.path.splitext(path)[1].lower() in HEAVY_EXT):
            return None
        self._mthumb_jobs[path] = self._tpool.submit(mesh_thumb.thumb_for, path, hs_gpu.load_mesh)
        return None

    # ── pictures (background decode) ────────────────────────────────────────────────
    def _want(self, path: str, side: int, cache: OrderedDict, limit: int):
        if path in cache or path in self._failed:
            if path in cache:
                cache.move_to_end(path)
            return
        key = (path, side)
        if key not in self._jobs and len(self._jobs) < 12:
            self._jobs[key] = self._pool.submit(_decode, path, side)

    def _poll_images(self):
        sp = self.space
        op = sp.opened
        for c in sp.cards:
            if c.data.get("image") and c.a > 0.02:
                self._want(c.data["image"], THUMB_SIDE, self._thumbs, 150)
        if op is not None and op.data.get("image") and op.k > 0.3:
            self._want(op.data["image"], LARGE_SIDE, self._large, 3)
        for key, fut in list(self._jobs.items()):
            if not fut.done():
                continue
            del self._jobs[key]
            path, side = key
            try:
                res = fut.result()
            except Exception:
                res = None
            if res is None:
                self._failed.add(path)
                continue
            qi, w, h, real = res
            for c in sp.cards:
                if c.data.get("image") == path and w > 0 and h > 0:
                    c.data["aspect"], c.data["dims"] = w / h, real
            if side == THUMB_SIDE:
                self._thumbs[path] = QPixmap.fromImage(qi)
                while len(self._thumbs) > 150:
                    self._thumbs.popitem(last=False)
            else:
                self._large[path] = QPixmap.fromImage(qi)
                while len(self._large) > 3:
                    self._large.popitem(last=False)

    def _release_files(self, paths):
        """Space is about to rename / move / delete files: let go of a video that may hold one open."""
        try:
            if self._video.active:
                self._video.park()
        except Exception:
            pass

    # ── input fallback: mouse / keyboard ─────────────────────────────────────────────
    def keyPressEvent(self, e):
        k, sp = e.key(), self.space
        mods = e.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        if sp.prompt is not None:                                    # the rename box takes all typing
            names = {Qt.Key.Key_Return: "enter", Qt.Key.Key_Enter: "enter", Qt.Key.Key_Escape: "esc", Qt.Key.Key_Backspace: "backspace",
                     Qt.Key.Key_Delete: "delete", Qt.Key.Key_Left: "left", Qt.Key.Key_Right: "right", Qt.Key.Key_Home: "home", Qt.Key.Key_End: "end"}
            if ctrl and k == Qt.Key.Key_A:
                sp.prompt_key("", "all")
            elif k in names:
                sp.prompt_key("", names[k])
            elif not ctrl:
                sp.prompt_key(e.text())
            return
        if ctrl and k in (Qt.Key.Key_C, Qt.Key.Key_X, Qt.Key.Key_V, Qt.Key.Key_A):
            if k == Qt.Key.Key_A:
                sp.select_all()
            else:
                sp._press({Qt.Key.Key_C: "a_copy", Qt.Key.Key_X: "a_cut", Qt.Key.Key_V: "a_paste"}[k], time.time())
            return
        if k == Qt.Key.Key_F2:
            sp._press("a_rename", time.time())
            return
        if k == Qt.Key.Key_Delete:
            sp._press("a_delete", time.time())
            return
        if k in (Qt.Key.Key_F, Qt.Key.Key_F11):
            sp._press("v_full", time.time())
        elif k == Qt.Key.Key_Escape:
            if sp.opened is not None or sp.menu or sp.full or sp.select:
                sp.back()
            else:
                self.hide()
        elif k == Qt.Key.Key_Backspace:
            sp.back()
        elif k == Qt.Key.Key_Home:
            sp.go_root()
        elif k in (Qt.Key.Key_Left, Qt.Key.Key_PageUp):
            sp.key_arrow(-1)
        elif k in (Qt.Key.Key_Right, Qt.Key.Key_PageDown):
            sp.key_arrow(1)
        elif k in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            sp._press("v_plus", time.time())
        elif k == Qt.Key.Key_Minus:
            sp._press("v_minus", time.time())
        elif k == Qt.Key.Key_Space:
            sp._press("v_play", time.time())
        else:
            super().keyPressEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.space.mouse(e.position().x(), e.position().y(), True)
            if self.space.close_requested:
                self.hide()
        elif e.button() == Qt.MouseButton.RightButton:
            self.space.back()

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.MouseButton.LeftButton:
            self.space.mouse(e.position().x(), e.position().y(), True)
        else:
            self.space.hover_at(e.position().x(), e.position().y())

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.space.mouse(e.position().x(), e.position().y(), False)
            self.space.visible[0] = False

    def wheelEvent(self, e):
        self.space.wheel(e.angleDelta().y(), e.position().x(), e.position().y())

    def leaveEvent(self, e):
        self.space.mouse_hover = None
        super().leaveEvent(e)

    def _background(self) -> QPixmap:
        """Deep-space backdrop: vignette, two soft aurora glows, a dot grid that fades towards the edges. Drawn once per size."""
        W, H = self.width(), self.height()
        if self._bg is None or self._bg.width() != W or self._bg.height() != H:
            bg = QPixmap(W, H)
            bg.fill(Qt.GlobalColor.transparent)
            p = QPainter(bg)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            g = QRadialGradient(QPointF(W * 0.5, H * 0.58), max(W, H) * 0.85)
            g.setColorAt(0.0, QColor(13, 30, 60, 224))
            g.setColorAt(0.55, QColor(6, 15, 34, 236))
            g.setColorAt(1.0, QColor(1, 3, 10, 248))
            p.fillRect(QRectF(0, 0, W, H), QBrush(g))
            for cx, cy, rad, col in ((W * 0.14, H * 0.08, W * 0.46, C.PRI), (W * 0.9, H * 0.94, W * 0.5, C.ACC),
                                     (W * 0.62, H * 0.02, W * 0.3, C.ACC2)):                 # aurora glows
                a = QRadialGradient(QPointF(cx, cy), rad)
                a.setColorAt(0.0, _q(col, 40))
                a.setColorAt(0.5, _q(col, 12))
                a.setColorAt(1.0, _q(col, 0))
                p.fillRect(QRectF(0, 0, W, H), QBrush(a))
            p.setPen(Qt.PenStyle.NoPen)                                                       # dot grid, brightest in the middle
            mx, my, md = W / 2, H * 0.56, math.hypot(W, H) * 0.5
            step = 44
            for ix, x in enumerate(range(step, W, step)):
                for iy, y in enumerate(range(step, H, step)):
                    t = max(0.0, 1.0 - math.hypot(x - mx, y - my) / md)
                    p.setBrush(_q(C.PRI, 6 + 40 * t * t))
                    p.drawEllipse(QPointF(x, y), 1.0 + 0.5 * t, 1.0 + 0.5 * t)
            p.setBrush(Qt.BrushStyle.NoBrush)                                                 # a few "+" registration marks
            p.setPen(QPen(_q(C.PRI, 34), 1.0))
            for x in range(step * 4, W, step * 8):
                for y in range(step * 4, H, step * 8):
                    p.drawLine(QPointF(x - 4, y), QPointF(x + 4, y))
                    p.drawLine(QPointF(x, y - 4), QPointF(x, y + 4))
            vg = QRadialGradient(QPointF(W / 2, H / 2), math.hypot(W, H) * 0.52)             # vignette
            vg.setColorAt(0.55, QColor(0, 0, 0, 0))
            vg.setColorAt(1.0, QColor(0, 0, 0, 150))
            p.fillRect(QRectF(0, 0, W, H), QBrush(vg))
            top = QLinearGradient(0, 0, 0, 140)                                               # darker bands under the top / bottom bars
            top.setColorAt(0, QColor(0, 6, 16, 200))
            top.setColorAt(1, QColor(0, 6, 16, 0))
            p.fillRect(QRectF(0, 0, W, 140), QBrush(top))
            bot = QLinearGradient(0, H - 160, 0, H)
            bot.setColorAt(0, QColor(0, 6, 16, 0))
            bot.setColorAt(1, QColor(0, 6, 16, 205))
            p.fillRect(QRectF(0, H - 160, W, 160), QBrush(bot))
            ln = QLinearGradient(0, 0, W, 0)                                                  # hairline of light along the top edge
            ln.setColorAt(0, _q(C.PRI, 0))
            ln.setColorAt(0.5, _q(C.PRI, 150))
            ln.setColorAt(1, _q(C.ACC, 0))
            p.fillRect(QRectF(0, 0, W, 1.6), QBrush(ln))
            p.end()
            self._bg = bg
        return self._bg

    def paintEvent(self, _):
        if self._cv is not None:                      # the GL canvas covers this widget and paints everything
            return
        p = QPainter(self)
        self._draw(p)
        p.end()

    def _paint_gl(self, cv):
        t0 = time.perf_counter()
        try:
            self._paint_gl_inner(cv)
        finally:
            ms = (time.perf_counter() - t0) * 1000
            self._pms = self._pms * 0.9 + ms * 0.1 if self._pms else ms
            if PROFILE:
                self._pt[0] += ms / 1000
                self._pt[1] += 1
                if self._pt[1] >= 90:
                    print(f"[hand space] paint {self._pt[0] / self._pt[1] * 1000:.1f} ms/frame · {len(self.space.cards)} cards")
                    self._pt = [0.0, 0]

    def _paint_gl_inner(self, cv):
        p = QPainter(cv)
        if not p.isActive():
            return
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        p.fillRect(cv.rect(), Qt.GlobalColor.transparent)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        try:
            self._draw(p)
        finally:
            p.end()

    def _draw(self, p: QPainter):
        sp = self.space
        if self._cv is not None:
            if not self._gl_tried:
                self._gl_tried = True
                self._mesh.init()
            self._upload_models()
        self._live = 0
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        W, H = self.width(), self.height()
        p.drawPixmap(0, 0, self._background())

        op = sp.opened
        if op is not None and sp.fk > 0.55 and op.k > 0.5:           # FULL SCREEN: only the item itself is drawn
            p.fillRect(QRectF(0, 0, W, H), QColor(0, 0, 0, 255))
            self._fs = True
            try:
                self._draw_card(p, op)
            finally:
                self._fs = False
            self._draw_full_ui(p, op, W, H)
        else:
            dim = max((c.k for c in sp.cards), default=0.0)
            for c in sorted(sp.cards, key=lambda c: (c.open, c.k, c.held, c.lift)):
                if c.open or c.k > 0.02:
                    p.fillRect(QRectF(0, 0, W, H), QColor(0, 3, 10, int(176 * dim + 80 * sp.fk)))      # dark scrim behind the open card
                    dim = 0.0
                self._draw_card(p, c, faded=op is not None and not c.open and c.k < 0.02)
            if sp.empty:
                self._draw_empty(p, W, H)
            self._draw_topbar(p, W)
            if sp.prompt is None:
                if sp.menu_box is not None:
                    self._draw_menu_panel(p, sp.menu_box)
                if op is not None:
                    self._draw_dock(p)
                for b in sp.buttons:
                    self._draw_button(p, b)
            if sp.pages > 1 and op is None:
                self._draw_pager(p, W, H)
            self._draw_hint(p, W, H)
        if sp.toast:
            self._draw_toast(p, W, H, sp.toast)
        if sp.prompt is not None:
            self._draw_prompt(p, W, H)
            for b in sp.buttons:
                self._draw_button(p, b)
        for i, h in enumerate(sp.hands):
            if sp.visible[i] and h.pts:
                self._draw_hand(p, h)

    # full screen: the item alone; a thin title and one control bar that fade away when the hand rests
    def _draw_full_ui(self, p: QPainter, c: Card, W: int, H: int):
        sp = self.space
        a = sp.ui_a
        if a < 0.02:
            return
        d = c.data
        p.save()
        p.setOpacity(a)
        top = QLinearGradient(0, 0, 0, 130)
        top.setColorAt(0, QColor(0, 0, 0, 185))
        top.setColorAt(1, QColor(0, 0, 0, 0))
        p.fillRect(QRectF(0, 0, W, 130), QBrush(top))
        bot = QLinearGradient(0, H - 210, 0, H)
        bot.setColorAt(0, QColor(0, 0, 0, 0))
        bot.setColorAt(1, QColor(0, 0, 0, 205))
        p.fillRect(QRectF(0, H - 210, W, 210), QBrush(bot))
        info = d["title"]
        kind = d.get("kind")
        if kind == "image" and d.get("dims"):
            info += f"   ·   {d['dims'][0]}×{d['dims'][1]}"
        elif kind == "model" and self._tris.get(d["path"]):
            info += f"   ·   {self._tris[d['path']]:,} triangles"
        if kind in ("image", "model") and abs(c.zoom - 1.0) > 0.01:
            info += f"   ·   {int(c.zoom * 100)}%"
        acc = QColor(_accent_for(d))
        self._chip(p, QRectF(40, 28, 36, 36), acc, _glyph_for(d), _emoji_for(d))
        self._title(p, QRectF(90, 28, W - 170, 36), info, 14, "#ffffff")
        if kind == "video" and self._video.active:
            pos, dur, _asp, _end = self._video.info()
            bw = min(W * 0.6, 900.0)
            bar = QRectF((W - bw) / 2, H - 156, bw, 6)
            self._scrub(p, bar, (pos / dur) if dur > 0 else 0.0)
            p.setFont(_F(10, QFont.Weight.DemiBold))
            p.setPen(QColor(255, 255, 255, 235))
            tm = lambda x: f"{int(x) // 60}:{int(x) % 60:02d}"                  # noqa: E731
            p.drawText(QRectF(bar.left(), bar.top() - 26, 120, 22), _AL.AlignLeft | _AL.AlignVCenter, tm(pos))
            p.setPen(QColor(255, 255, 255, 170))
            p.drawText(QRectF(bar.right() - 120, bar.top() - 26, 120, 22), _AL.AlignRight | _AL.AlignVCenter, tm(dur))
        self._draw_dock(p)
        for b in sp.buttons:
            self._draw_button(p, b)
        p.restore()

    def _draw_menu_panel(self, p: QPainter, box: tuple):
        r = QRectF(*box)
        self._glass(p, r, 20, C.PRI, 0.0, dark=True, shadow=True)
        for b in self.space.buttons:                                    # a thin rule above Delete
            if b["id"] == "a_delete":
                ln = QLinearGradient(b["x"], 0, b["x"] + b["w"], 0)
                ln.setColorAt(0, QColor(255, 255, 255, 0))
                ln.setColorAt(0.5, QColor(255, 255, 255, 56))
                ln.setColorAt(1, QColor(255, 255, 255, 0))
                p.setPen(QPen(QBrush(ln), 1))
                p.drawLine(QPointF(b["x"] + 8, b["y"] - 3), QPointF(b["x"] + b["w"] - 8, b["y"] - 3))

    def _draw_prompt(self, p: QPainter, W: int, H: int):
        pr = self.space.prompt
        sc = QRadialGradient(QPointF(W / 2, H / 2), max(W, H) * 0.7)
        sc.setColorAt(0, QColor(2, 8, 20, 190))
        sc.setColorAt(1, QColor(0, 2, 8, 232))
        p.fillRect(QRectF(0, 0, W, H), QBrush(sc))
        box = QRectF(W / 2 - 330, H / 2 - 100, 660, 252)
        self._glass(p, box, 26, C.PRI, 0.0, dark=True, shadow=True)
        self._chip(p, QRectF(box.x() + 28, box.y() + 22, 34, 34), QColor(C.PRI), "doc")
        p.setFont(_F(15, QFont.Weight.DemiBold))
        p.setPen(QColor(C.TEXT))
        p.drawText(QRectF(box.x() + 76, box.y() + 22, box.width() - 104, 34), _AL.AlignLeft | _AL.AlignVCenter, pr["title"])
        fld = QRectF(box.x() + 28, box.y() + 74, box.width() - 56, 48)
        bad = bool(pr["error"])
        ecol = QColor(255, 110, 130) if bad else QColor(C.PRI)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 4, 14, 170))
        p.drawRoundedRect(fld, 12, 12)
        self._glow(p, fld, 12, ecol, 0.9, 1.3)
        p.setFont(_F(15))
        fm = QFontMetrics(p.font())
        txt, cur, s0 = pr["text"], pr["cur"], pr["sel0"]
        x0 = fld.x() + 16
        lo, hi = min(cur, s0), max(cur, s0)
        if lo != hi:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_q(C.PRI, 90))
            p.drawRoundedRect(QRectF(x0 + fm.horizontalAdvance(txt[:lo]), fld.y() + 9, fm.horizontalAdvance(txt[lo:hi]), fld.height() - 18), 4, 4)
        p.setPen(QColor(C.TEXT))
        p.drawText(QRectF(x0, fld.y(), fld.width() - 32, fld.height()), _AL.AlignLeft | _AL.AlignVCenter, txt)
        if int(time.time() * 2) % 2 == 0:
            cx = x0 + fm.horizontalAdvance(txt[:cur])
            p.setPen(QPen(_q(C.PRI, 255), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(cx, fld.y() + 11), QPointF(cx, fld.bottom() - 11))
        if bad:
            p.setFont(_F(10))
            p.setPen(QColor(255, 140, 156))
            p.drawText(QRectF(fld.x() + 2, fld.bottom() + 6, fld.width(), 20), _AL.AlignLeft | _AL.AlignVCenter, pr["error"])

    # ── top bar ──────────────────────────────────────────────────────────────────────
    def _draw_topbar(self, p: QPainter, W: int):
        sp = self.space
        left = 40 + 2 * (76 + 12) + 14
        right = min((b["x"] for b in sp.buttons if b["id"] in ("arrange", "sort", "select", "close")), default=W - 40) - 18
        bar = QLinearGradient(0, 30, 0, 88)                                  # a slim accent bar in front of the title
        bar.setColorAt(0, _q(C.PRI, 255))
        bar.setColorAt(1, _q(C.ACC, 255))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(bar))
        p.drawRoundedRect(QRectF(left - 6, 32, 3.5, 54), 1.75, 1.75)
        r = QRectF(left + 12, 28, max(100, right - left - 12), 34)
        p.setFont(_F(19, QFont.Weight.DemiBold))
        p.setPen(QColor(C.TEXT))
        fm = QFontMetrics(p.font())
        p.drawText(r, _AL.AlignLeft | _AL.AlignVCenter, fm.elidedText(sp.crumb, Qt.TextElideMode.ElideLeft, int(r.width())))
        sel = bool(sp.select)
        col = QColor(C.GREEN if sel else C.TEXT_DIM)
        p.setBrush(col)
        p.drawEllipse(QPointF(r.left() + 3, 76), 3.0, 3.0)
        p.setFont(_F(10, spacing=0.4))
        p.setPen(col)
        p.drawText(QRectF(r.left() + 14, 64, r.width() - 14, 24), _AL.AlignLeft | _AL.AlignVCenter,
                   f"{len(sp.sel)} selected" if sel else sp.summary)

    # ── buttons ──────────────────────────────────────────────────────────────────────
    def _draw_button(self, p: QPainter, b: dict):
        sp = self.space
        bid = b["id"]
        r = QRectF(b["x"], b["y"], b["w"], b["h"])
        on = bool(b["on"])
        hot = sp.hover_btn == bid and on
        age = time.time() - sp.flash.get(bid, 0.0)
        pulse = max(0.0, 1.0 - age / 0.3)
        a = 1.0 if on else 0.36
        if b.get("menu"):                                              # a row of the ⋯ menu: flat, left-aligned text
            kill = bid == "a_delete"
            rr = r.adjusted(2, 1, -2, -1)
            if hot or (kill and b["danger"]):
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(255, 92, 122, 64 if hot else 36) if kill else _q(C.PRI, 42))
                p.drawRoundedRect(rr, 12, 12)
                if hot:
                    p.setBrush(QColor(255, 92, 122) if kill else QColor(C.PRI))
                    p.drawRoundedRect(QRectF(rr.left() + 6, rr.center().y() - 9, 3, 18), 1.5, 1.5)
            col = QColor(255, 138, 156) if kill else QColor("#ffffff" if hot else (C.TEXT if on else C.TEXT_DIM))
            col.setAlpha(int(255 * a))
            p.setFont(_F(11, QFont.Weight.DemiBold))
            p.setPen(col)
            p.drawText(QRectF(r.left() + 24, r.top(), r.width() - 32, r.height()), _AL.AlignLeft | _AL.AlignVCenter, b["label"])
            return
        p.save()
        p.setOpacity(p.opacity() * a)
        circle = bid == "close"
        danger = bool(b.get("danger")) or (circle and hot)
        active = bool(b.get("active")) and not danger
        tint = QColor(255, 92, 122) if danger else QColor(C.GREEN) if active else None
        accent = tint if tint is not None else QColor(C.PRI)
        rr = r.adjusted(-3 * pulse, -3 * pulse, 3 * pulse, 3 * pulse)
        rad = rr.height() / 2 if circle else 17.0
        self._glass(p, rr, rad, accent, 1.0 if hot else 0.0, dark=False, tint=tint)
        if pulse > 0.02:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, int(64 * pulse)))
            p.drawRoundedRect(rr, rad, rad)
        if hot or active or danger:
            self._glow(p, rr.adjusted(0.5, 0.5, -0.5, -0.5), rad, accent, 0.85 if hot else 0.55, 1.2)
        col = QColor("#ffffff") if (hot or danger) else QColor("#d6ffe9") if active else QColor(C.TEXT if on else C.TEXT_DIM)
        ico = {"v_prev": "left", "v_next": "right", "pg_prev": "left", "pg_next": "right", "v_plus": "plus",
               "v_play": "play" if (sp.opened is not None and sp.opened.paused) else "pause",
               "v_minus": "minus", "v_open": "ext", "v_close": "close", "close": "close", "home": "home", "up": "up",
               "explorer": "folder", "sort": "sort", "arrange": "grid", "a_more": "more",
               "v_full": "shrink" if sp.full else "expand"}.get(bid)
        label = b["label"]
        if ico and label:                                              # icon on the left, text after it
            self._icon(p, ico, QRectF(r.left() + 18, r.center().y() - 11, 22, 22), col)
            p.setFont(_F(11, QFont.Weight.DemiBold))
            p.setPen(col)
            p.drawText(QRectF(r.left() + 50, r.top(), r.width() - 58, r.height()), _AL.AlignLeft | _AL.AlignVCenter, label)
        elif ico:
            self._icon(p, ico, QRectF(r.center().x() - 11, r.center().y() - 11, 22, 22), col)
        elif label:
            p.setFont(_F(11, QFont.Weight.DemiBold))
            p.setPen(col)
            p.drawText(r, _AL.AlignCenter, label)
        p.restore()

    @staticmethod
    def _icon(p: QPainter, kind: str, r: QRectF, col: QColor):
        pen = QPen(col, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        x, y, w, h = r.x(), r.y(), r.width(), r.height()
        cx, cy = r.center().x(), r.center().y()
        L = lambda a, b, c, d: p.drawLine(QPointF(a, b), QPointF(c, d))       # noqa: E731
        if kind == "close":
            L(x + 3, y + 3, x + w - 3, y + h - 3)
            L(x + 3, y + h - 3, x + w - 3, y + 3)
        elif kind in ("left", "right"):
            s = -1 if kind == "left" else 1
            L(cx - 4 * s, y + 2, cx + 4 * s, cy)
            L(cx + 4 * s, cy, cx - 4 * s, y + h - 2)
        elif kind == "plus":
            L(x + 2, cy, x + w - 2, cy)
            L(cx, y + 2, cx, y + h - 2)
        elif kind == "minus":
            L(x + 2, cy, x + w - 2, cy)
        elif kind == "play":
            p.setBrush(col)
            p.drawPolygon(QPolygonF([QPointF(x + 4, y + 2), QPointF(x + 4, y + h - 2), QPointF(x + w - 3, cy)]))
        elif kind == "pause":
            L(x + 6, y + 2, x + 6, y + h - 2)
            L(x + w - 6, y + 2, x + w - 6, y + h - 2)
        elif kind == "up":
            L(cx, y + h - 2, cx, y + 3)
            L(cx - 6, y + 9, cx, y + 3)
            L(cx + 6, y + 9, cx, y + 3)
        elif kind == "home":
            path = QPainterPath()
            path.moveTo(x + 1, cy)
            path.lineTo(cx, y + 2)
            path.lineTo(x + w - 1, cy)
            p.drawPath(path)
            p.drawRect(QRectF(x + 4, cy, w - 8, h / 2 - 2))
        elif kind == "ext":
            p.drawRect(QRectF(x + 2, y + 6, w - 8, h - 8))
            L(cx - 1, cy + 1, x + w - 2, y + 2)
            L(x + w - 9, y + 2, x + w - 2, y + 2)
            L(x + w - 2, y + 2, x + w - 2, y + 9)
        elif kind == "folder":
            p.drawRoundedRect(QRectF(x + 1, y + 5, w - 2, h - 8), 3, 3)
            L(x + 2, y + 5, x + 6, y + 2)
            L(x + 6, y + 2, x + 11, y + 2)
        elif kind == "sort":
            for i, ln in enumerate((w - 2, w - 8, w - 14)):
                L(x + 1, y + 4 + i * 7, x + 1 + ln, y + 4 + i * 7)
        elif kind == "more":
            p.setBrush(col)
            p.setPen(Qt.PenStyle.NoPen)
            for i in range(3):
                p.drawEllipse(QPointF(x + 3 + i * (w - 6) / 2, cy), 2.1, 2.1)
        elif kind in ("expand", "shrink"):
            s2 = 7 if kind == "expand" else -7
            for sx, sy in ((x + 2, y + 2), (x + w - 2, y + 2), (x + 2, y + h - 2), (x + w - 2, y + h - 2)):
                dx = 1 if sx < cx else -1
                dy = 1 if sy < cy else -1
                if kind == "shrink":
                    sx, sy = sx + dx * 7, sy + dy * 7
                    dx, dy = -dx, -dy
                L(sx, sy, sx + dx * 7, sy)
                L(sx, sy, sx, sy + dy * 7)
        elif kind == "grid":
            for i in range(2):
                for j in range(2):
                    p.drawRoundedRect(QRectF(x + 2 + i * 10, y + 2 + j * 10, 8, 8), 2, 2)

    # ── cards ────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _body(q: QPainter, rect: QRectF, accent: QColor, rad: float = 18.0, strong: float = 0.0):
        """The glass body of a card: layered shadow, tinted frosted fill, gradient rim, sheen and a lit top edge."""
        k = 1.0 + 0.38 * strong
        q.setPen(Qt.PenStyle.NoPen)
        for i, al in enumerate((24, 15, 8)):
            g = 3 + i * 5
            q.setBrush(QColor(0, 0, 0, al))
            q.drawRoundedRect(rect.adjusted(-g, -g + 9, g, g + 9), rad + g, rad + g)
        fill = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        fill.setColorAt(0, QColor(38, 66, 118, min(255, int(150 * k))))
        fill.setColorAt(1, QColor(10, 21, 48, min(255, int(176 * k))))
        q.setBrush(fill)
        q.drawRoundedRect(rect, rad, rad)
        tint = QLinearGradient(rect.topLeft(), rect.bottomRight())
        tint.setColorAt(0, _q(accent, 40))
        tint.setColorAt(0.65, _q(accent, 6))
        tint.setColorAt(1, _q(accent, 0))
        q.setBrush(tint)
        q.drawRoundedRect(rect, rad, rad)
        sheen = QPainterPath()
        sheen.addRoundedRect(rect.adjusted(2, 2, -2, -rect.height() * 0.55), rad - 2, rad - 2)
        q.setBrush(QColor(255, 255, 255, 13))
        q.drawPath(sheen)
        rim = QLinearGradient(rect.topLeft(), rect.bottomRight())
        rim.setColorAt(0, QColor(255, 255, 255, 120))
        rim.setColorAt(0.45, QColor(255, 255, 255, 26))
        rim.setColorAt(1, _q(accent, 120))
        q.setBrush(Qt.BrushStyle.NoBrush)
        q.setPen(QPen(QBrush(rim), 1.2))
        q.drawRoundedRect(rect.adjusted(0.6, 0.6, -0.6, -0.6), rad - 0.6, rad - 0.6)
        ln = QLinearGradient(rect.left() + rad, 0, rect.right() - rad, 0)
        ln.setColorAt(0, _q(accent, 0))
        ln.setColorAt(0.5, _q(accent, 230))
        ln.setColorAt(1, _q(accent, 0))
        q.setPen(QPen(QBrush(ln), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        q.drawLine(QPointF(rect.left() + rad, rect.top() + 0.9), QPointF(rect.right() - rad, rect.top() + 0.9))

    def _chrome(self, w: float, h: float, accent: QColor) -> QPixmap:
        """The glass body of a resting card drawn once and reused: no gradients per card per frame."""
        dpr = float(self.devicePixelRatioF())
        key = (int(w), int(h), accent.rgb(), dpr)
        pm = self._chromes.get(key)
        if pm is None:
            if len(self._chromes) > 60:
                self._chromes.clear()
            pad = 22
            pm = QPixmap(int((w + 2 * pad) * dpr), int((h + 2 * pad) * dpr))
            pm.setDevicePixelRatio(dpr)
            pm.fill(Qt.GlobalColor.transparent)
            q = QPainter(pm)
            q.setRenderHint(QPainter.RenderHint.Antialiasing)
            self._body(q, QRectF(pad, pad, w, h), accent)
            q.end()
            self._chromes[key] = pm
        return pm

    def _draw_card(self, p: QPainter, c: Card, faded: bool = False):
        if c.a < 0.01:
            return
        fs = self._fs and c.open
        w, h = c.w, c.h
        appear = 1.0 if fs else c.a
        sc = 1.0 if fs else (0.9 + 0.1 * appear) * (1.0 + 0.05 * c.lift + 0.03 * c.hov)
        kind = c.data.get("kind", "file")
        accent = QColor(_accent_for(c.data))
        p.save()
        p.setOpacity(appear * (0.35 if faded else 1.0))
        p.translate(c.x, c.y)
        if not fs:
            p.rotate(math.degrees(c.angle))
        p.scale(sc, sc)
        rect = QRectF(-w / 2, -h / 2, w, h)
        cpath = c.data.get("path")
        if not fs:
            p.setPen(Qt.PenStyle.NoPen)
            if c.k < 0.02 and not c.open:                               # resting card: the cached body
                if c.lift > 0.03:                                       # picked up: a deeper shadow beneath it
                    for i, al in enumerate((34, 20, 10)):
                        g = 4 + i * 7
                        p.setBrush(QColor(0, 0, 0, int(al * c.lift)))
                        p.drawRoundedRect(rect.adjusted(-g, -g + 18, g, g + 18), 18 + g, 18 + g)
                p.drawPixmap(QPointF(-w / 2 - 22, -h / 2 - 22), self._chrome(w, h, accent))
                if c.hov > 0.02 or c.held:
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(_q(accent, 30 * c.hov))
                    p.drawRoundedRect(rect, 18, 18)
                    self._glow(p, rect, 18, QColor(C.GREEN) if c.held else accent, 1.0 if c.held else c.hov, 1.6)
            else:                                                       # opening / open: its size changes every frame, drawn directly
                self._body(p, rect, accent, 18.0, 1.0)
            if cpath and cpath in self.space.sel:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(_q(C.GREEN, 34))
                p.drawRoundedRect(rect, 18, 18)
                self._glow(p, rect, 18, QColor(C.GREEN), 1.0, 2.2)
                cr = QRectF(rect.right() - 38, rect.top() + 10, 28, 28)
                gg = QLinearGradient(cr.topLeft(), cr.bottomLeft())
                gg.setColorAt(0, QColor("#8dffc9"))
                gg.setColorAt(1, QColor(C.GREEN).darker(125))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(gg))
                p.drawEllipse(cr)
                p.setPen(QPen(QColor(4, 22, 14), 2.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
                p.drawLine(QPointF(cr.x() + 7.5, cr.center().y() + 0.5), QPointF(cr.center().x() - 1, cr.bottom() - 8.5))
                p.drawLine(QPointF(cr.center().x() - 1, cr.bottom() - 8.5), QPointF(cr.right() - 7, cr.y() + 8.5))
            elif self.space.select and kind != "drive":
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QColor(255, 255, 255, 64), 1.2, Qt.PenStyle.DashLine))
                p.drawRoundedRect(rect.adjusted(6, 6, -6, -6), 13, 13)
                p.setPen(QPen(QColor(255, 255, 255, 120), 1.6))
                p.drawEllipse(QRectF(rect.right() - 38, rect.top() + 10, 28, 28))
            if cpath and cpath in self.space.clip_paths:
                p.setOpacity(appear * 0.55)                               # cut items look faded until pasted
        if kind == "image":
            self._card_image(p, c, rect)
        elif kind == "model":
            self._card_model(p, c, rect)
        elif kind == "video":
            self._card_video(p, c, rect)
        elif kind == "text":
            self._card_text(p, c, rect)
        elif kind == "map":
            self._card_map(p, c, rect)
        elif kind in ("drive", "folder"):
            self._card_place(p, c, rect, accent)
        else:
            self._card_file(p, c, rect)
        p.restore()

    @staticmethod
    def _title(p: QPainter, rect: QRectF, text: str, size: int = 11, color: str | None = None, align=_AL.AlignLeft):
        p.setFont(_F(size, QFont.Weight.DemiBold))
        p.setPen(QColor(color or C.TEXT))
        p.drawText(rect, align | _AL.AlignVCenter, _elided(text, size, int(rect.width())))

    # drives and folders
    def _card_place(self, p: QPainter, c: Card, rect: QRectF, accent: QColor):
        d = c.data
        inner = rect.adjusted(18, 14, -18, -14)
        fx, fy = inner.left(), inner.top() + 2
        if d["kind"] == "folder":
            glow = QRadialGradient(QPointF(fx + 31, fy + 38), 58)
            glow.setColorAt(0, _q(accent, 70))
            glow.setColorAt(1, _q(accent, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(glow))
            p.drawEllipse(QPointF(fx + 31, fy + 38), 58, 40)
            p.setBrush(_mix(accent, "#000000", 0.42))
            p.drawRoundedRect(QRectF(fx, fy, 27, 14), 5, 5)                                     # tab
            back = QLinearGradient(fx, fy + 6, fx, fy + 44)
            back.setColorAt(0, _mix(accent, "#000000", 0.30))
            back.setColorAt(1, _mix(accent, "#000000", 0.50))
            p.setBrush(QBrush(back))
            p.drawRoundedRect(QRectF(fx, fy + 7, 62, 38), 7, 7)                                 # back panel
            front = QLinearGradient(fx, fy + 15, fx, fy + 47)
            front.setColorAt(0, _mix(accent, "#ffffff", 0.22))
            front.setColorAt(1, _mix(accent, "#000000", 0.14))
            p.setBrush(QBrush(front))
            p.drawRoundedRect(QRectF(fx, fy + 15, 62, 32), 7, 7)                                # front panel
            p.setPen(QPen(QColor(255, 255, 255, 110), 1.1, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(fx + 7, fy + 16.2), QPointF(fx + 55, fy + 16.2))
            self._title(p, QRectF(inner.left(), inner.bottom() - 52, inner.width(), 26), d["title"], 12)
            p.setFont(_F(9))
            p.setPen(QColor(C.TEXT_DIM))
            p.drawText(QRectF(inner.left(), inner.bottom() - 26, inner.width(), 20), _AL.AlignLeft | _AL.AlignVCenter, d.get("sub", ""))
            return
        used = max(0.0, min(1.0, d.get("used", 0.0)))
        warn = QColor(C.RED if used > 0.9 else C.AMBER if used > 0.75 else C.PRI)
        body = QRectF(fx, fy + 4, 60, 36)                                                       # the drive itself
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 60))
        p.drawRoundedRect(body.adjusted(-2, 5, 2, 5), 11, 11)
        g = QLinearGradient(body.topLeft(), body.bottomLeft())
        g.setColorAt(0, QColor(214, 238, 255, 240))
        g.setColorAt(1, QColor(84, 138, 205, 240))
        p.setBrush(QBrush(g))
        p.drawRoundedRect(body, 10, 10)
        p.setBrush(QColor(20, 50, 96, 120))
        p.drawRoundedRect(QRectF(body.left() + 8, body.bottom() - 13, 28, 4), 2, 2)
        p.setBrush(QColor(20, 50, 96, 70))
        p.drawRoundedRect(QRectF(body.left() + 8, body.bottom() - 20, 18, 3), 1.5, 1.5)
        led = QColor(C.GREEN if d.get("sub") != "Not ready" and used <= 0.9 else C.RED)
        p.setBrush(_q(led, 60))
        p.drawEllipse(QPointF(body.right() - 11, body.bottom() - 11), 6.5, 6.5)
        p.setBrush(led)
        p.drawEllipse(QPointF(body.right() - 11, body.bottom() - 11), 3.2, 3.2)
        p.setPen(QPen(QColor(255, 255, 255, 150), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(body.adjusted(0.5, 0.5, -0.5, -0.5), 10, 10)
        tx = inner.left() + 74
        self._title(p, QRectF(tx, inner.top() + 2, inner.right() - tx, 22), d["title"], 11)
        p.setFont(_F(9, QFont.Weight.DemiBold))
        p.setPen(warn)
        p.drawText(QRectF(tx, inner.top() + 26, inner.right() - tx, 18), _AL.AlignLeft | _AL.AlignVCenter, f"{int(round(used * 100))}% used")
        bar = QRectF(inner.left(), inner.bottom() - 34, inner.width(), 8)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 26))
        p.drawRoundedRect(bar, 4, 4)
        fw = max(8.0, bar.width() * used)
        fill = QRectF(bar.left(), bar.top(), fw, bar.height())
        p.setBrush(_q(warn, 46))
        p.drawRoundedRect(fill.adjusted(-2, -3, 2, 3), 6, 6)                                    # soft glow under the bar
        fg = QLinearGradient(fill.left(), 0, fill.right(), 0)
        fg.setColorAt(0, _mix(warn, "#000000", 0.25))
        fg.setColorAt(1, _mix(warn, "#ffffff", 0.18))
        p.setBrush(QBrush(fg))
        p.drawRoundedRect(fill, 4, 4)
        p.setFont(_F(9))
        p.setPen(QColor(C.TEXT_DIM))
        p.drawText(QRectF(inner.left(), inner.bottom() - 22, inner.width(), 20), _AL.AlignLeft | _AL.AlignVCenter, d.get("sub", ""))

    # generic files (zip, pdf, exe …)
    def _card_file(self, p: QPainter, c: Card, rect: QRectF):
        d = c.data
        if c.k > 0.5:
            self._open_file(p, c, rect)
            return
        inner = rect.adjusted(18, 14, -18, -14)
        pg = QRectF(inner.left() + 6, inner.top() + 2, 40, 52)
        path = QPainterPath()
        path.moveTo(pg.left() + 5, pg.top())
        path.lineTo(pg.right() - 12, pg.top())
        path.lineTo(pg.right(), pg.top() + 12)
        path.lineTo(pg.right(), pg.bottom() - 5)
        path.quadTo(pg.right(), pg.bottom(), pg.right() - 5, pg.bottom())
        path.lineTo(pg.left() + 5, pg.bottom())
        path.quadTo(pg.left(), pg.bottom(), pg.left(), pg.bottom() - 5)
        path.lineTo(pg.left(), pg.top() + 5)
        path.quadTo(pg.left(), pg.top(), pg.left() + 5, pg.top())
        path.closeSubpath()
        fg = QLinearGradient(pg.topLeft(), pg.bottomLeft())
        fg.setColorAt(0, QColor(255, 255, 255, 64))
        fg.setColorAt(1, QColor(255, 255, 255, 16))
        p.setPen(QPen(QColor(255, 255, 255, 120), 1.2))
        p.setBrush(QBrush(fg))
        p.drawPath(path)
        fold = QPolygonF([QPointF(pg.right() - 12, pg.top()), QPointF(pg.right() - 12, pg.top() + 12), QPointF(pg.right(), pg.top() + 12)])
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 70))
        p.drawPolygon(fold)
        p.setBrush(QColor(255, 255, 255, 30))
        for i in range(3):
            p.drawRoundedRect(QRectF(pg.left() + 7, pg.top() + 18 + i * 6, 26 - i * 5, 2.2), 1.1, 1.1)
        ext = (d.get("_ext") or "").lstrip(".").upper()[:4] or "FILE"
        p.setFont(_F(8, QFont.Weight.Bold, spacing=0.6))
        tw = QFontMetrics(p.font()).horizontalAdvance(ext) + 14
        badge = QRectF(pg.left() - 8, pg.bottom() - 14, tw, 16)
        bg = QLinearGradient(badge.topLeft(), badge.topRight())
        bg.setColorAt(0, QColor(C.PRI))
        bg.setColorAt(1, QColor(C.ACC))
        p.setBrush(QBrush(bg))
        p.drawRoundedRect(badge, 8, 8)
        p.setPen(QColor(4, 14, 28))
        p.drawText(badge, _AL.AlignCenter, ext)
        self._title(p, QRectF(inner.left(), inner.bottom() - 52, inner.width(), 26), d["title"], 11)
        p.setFont(_F(9))
        p.setPen(QColor(C.TEXT_DIM))
        p.drawText(QRectF(inner.left(), inner.bottom() - 26, inner.width(), 20), _AL.AlignLeft | _AL.AlignVCenter, d.get("sub", ""))

    def _open_file(self, p: QPainter, c: Card, rect: QRectF):
        d = c.data
        inner = rect.adjusted(34, 28, -34, -28)
        self._chip(p, QRectF(inner.left(), inner.top() + 2, 40, 40), QColor(_accent_for(d)), _glyph_for(d), _emoji_for(d))
        self._title(p, QRectF(inner.left() + 54, inner.top(), inner.width() - 54, 44), d["title"], 18, C.PRI)
        ln = QLinearGradient(inner.left(), 0, inner.right(), 0)
        ln.setColorAt(0, _q(C.PRI, 200))
        ln.setColorAt(1, _q(C.PRI, 0))
        p.setPen(QPen(QBrush(ln), 1.4))
        p.drawLine(QPointF(inner.left(), inner.top() + 54), QPointF(inner.right(), inner.top() + 54))
        rows = [("TYPE", (d.get("_ext") or "").lstrip(".").upper() or "File"), ("SIZE", human_size(d.get("_sz", 0))),
                ("MODIFIED", d.get("date", "")), ("LOCATION", d.get("path", ""))]
        for i, (k, v) in enumerate(rows):
            y = inner.top() + 70 + i * 38
            p.setFont(_F(9, QFont.Weight.DemiBold, spacing=1.2))
            p.setPen(QColor(C.TEXT_DIM))
            p.drawText(QRectF(inner.left(), y, 110, 30), _AL.AlignLeft | _AL.AlignVCenter, k)
            p.setFont(_F(12))
            p.setPen(QColor(C.TEXT))
            fm = QFontMetrics(p.font())
            p.drawText(QRectF(inner.left() + 118, y, inner.width() - 118, 30), _AL.AlignLeft | _AL.AlignVCenter,
                       fm.elidedText(v, Qt.TextElideMode.ElideMiddle, int(inner.width() - 118)))
            p.setPen(QPen(QColor(255, 255, 255, 16), 1))
            p.drawLine(QPointF(inner.left(), y + 34), QPointF(inner.right(), y + 34))
        from core.hand_space import can_launch
        if not can_launch(d):
            p.setFont(_F(10))
            msg = "Programs and scripts are never started from a gesture."
            wm = QFontMetrics(p.font()).horizontalAdvance(msg) + 42
            pill = QRectF(inner.left(), inner.bottom() - 34, min(inner.width(), wm), 30)
            p.setPen(QPen(_q(C.AMBER, 120), 1))
            p.setBrush(_q(C.AMBER, 26))
            p.drawRoundedRect(pill, 15, 15)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(C.AMBER))
            p.drawEllipse(QPointF(pill.left() + 16, pill.center().y()), 3.2, 3.2)
            p.setPen(QColor(C.AMBER))
            p.drawText(QRectF(pill.left() + 28, pill.top(), pill.width() - 34, pill.height()), _AL.AlignLeft | _AL.AlignVCenter, msg)

    # notes
    def _card_map(self, p: QPainter, c: Card, rect: QRectF):
        d = c.data
        if c.k <= 0.5:                                                  # resting card: a little globe
            inner = rect.adjusted(16, 14, -16, -14)
            self._chip(p, QRectF(inner.left(), inner.top(), 32, 32), QColor(C.PRI), "globe")
            tx = inner.left() + 42
            self._title(p, QRectF(tx, inner.top() - 1, inner.right() - tx, 18), d["title"], 11, "#ffffff")
            p.setFont(_F(8))
            p.setPen(QColor(C.TEXT_DIM))
            p.drawText(QRectF(tx, inner.top() + 17, inner.right() - tx, 16), _AL.AlignLeft | _AL.AlignVCenter, "OpenStreetMap")
            ctr = QPointF(rect.right() - 52, rect.bottom() - 46)                  # a little globe in the corner
            r = 31.0
            halo = QRadialGradient(ctr, r * 1.9)
            halo.setColorAt(0, _q(C.PRI, 62))
            halo.setColorAt(1, _q(C.PRI, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(halo))
            p.drawEllipse(ctr, r * 1.9, r * 1.9)
            sph = QRadialGradient(QPointF(ctr.x() - r * 0.35, ctr.y() - r * 0.4), r * 1.5)
            sph.setColorAt(0, QColor(130, 220, 255, 200))
            sph.setColorAt(0.6, QColor(34, 110, 190, 190))
            sph.setColorAt(1, QColor(8, 30, 70, 220))
            p.setBrush(QBrush(sph))
            p.setPen(QPen(_q(C.PRI, 190), 1.3))
            p.drawEllipse(ctr, r, r)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(_q("#ffffff", 70), 0.9))
            p.drawEllipse(ctr, r * 0.42, r)
            p.drawEllipse(ctr, r * 0.8, r)
            p.drawLine(QPointF(ctr.x() - r, ctr.y()), QPointF(ctr.x() + r, ctr.y()))
            p.drawLine(QPointF(ctr.x(), ctr.y() - r), QPointF(ctr.x(), ctr.y() + r))
            for k in (-0.55, 0.55):
                w = r * math.sqrt(1 - k * k)
                p.drawLine(QPointF(ctr.x() - w, ctr.y() + k * r), QPointF(ctr.x() + w, ctr.y() + k * r))
            opt = QTextOption(_AL.AlignLeft | _AL.AlignBottom)
            opt.setWrapMode(QTextOption.WrapMode.WordWrap)
            p.setFont(_F(9))
            p.setPen(QColor(C.TEXT_MED))
            p.drawText(QRectF(inner.left(), inner.bottom() - 52, inner.width() - 96, 52), d.get("sub", ""), opt)
            return
        fs = self._fs and c.open
        view = rect if fs else rect.adjusted(14, 14, -14, -14)
        w, h = view.width(), view.height()
        mv = self.space.mapview
        Z, sc, tiles = mv.tiles(w, h)
        store = self._tile_store()
        p.save()
        if not fs:
            clip = QPainterPath()
            clip.addRoundedRect(view, 14, 14)
            p.setClipPath(clip)
        else:
            p.setClipRect(view)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        p.fillRect(view, QColor(212, 218, 220))                          # shown until a tile arrives
        size = MAP_TILE * sc + 0.6                                       # +0.6: no hairline gaps between tiles
        missing = []
        for tx, ty, sx, sy in tiles:
            dest = QRectF(view.left() + sx, view.top() + sy, size, size)
            if dest.right() < view.left() or dest.left() > view.right() or dest.bottom() < view.top() or dest.top() > view.bottom():
                continue
            pm = self._tilepx.get((Z, tx, ty))
            if pm is not None:
                p.drawPixmap(dest, pm, QRectF(0, 0, MAP_TILE, MAP_TILE))
                continue
            missing.append(((Z, tx, ty), (sx + size / 2 - w / 2) ** 2 + (sy + size / 2 - h / 2) ** 2))
            for k in (1, 2, 3):                                          # until it arrives: the blurry parent tile
                a = map_ancestor(Z, tx, ty, k)
                pm2 = self._tilepx.get(a[:3]) if a else None
                if pm2 is not None:
                    ux, uy, usz = a[3]
                    p.drawPixmap(dest, pm2, QRectF(ux, uy, usz, usz))
                    break
        now = time.time()
        want = tuple(k for k, _ in sorted(missing, key=lambda t: t[1]))
        if want != self._tile_req and (now - self._tile_req_t > 0.12 or not want):
            self._tile_req, self._tile_req_t = want, now
            store.request(list(want))                                     # only what is in view and missing; old requests are dropped
        if missing and store.online_error and not any(self._tilepx.get((Z, t[0], t[1])) for t in tiles):
            p.setFont(_F(11))
            p.setPen(QColor(60, 70, 80))
            p.drawText(view.adjusted(0, 24, 0, 0), _AL.AlignHCenter | _AL.AlignTop, "The map is offline — showing what is saved")
        if mv.marker is not None:                                        # the place that was searched for
            lat, lon, label = mv.marker
            mx, my = mv.project(lat, lon, w, h)
            if -40 < mx < w + 40 and -40 < my < h + 40:
                pt = QPointF(view.left() + mx, view.top() + my)
                p.setPen(QPen(QColor(255, 255, 255), 2.4))
                p.setBrush(QColor(226, 54, 54))
                p.drawEllipse(QPointF(pt.x(), pt.y() - 20), 11, 11)
                pin = QPolygonF([QPointF(pt.x() - 8, pt.y() - 14), QPointF(pt.x() + 8, pt.y() - 14), pt])
                p.setPen(Qt.PenStyle.NoPen)
                p.drawPolygon(pin)
                p.setBrush(QColor(255, 255, 255))
                p.drawEllipse(QPointF(pt.x(), pt.y() - 20), 4, 4)
                p.setFont(_F(11, QFont.Weight.DemiBold))
                tw = QFontMetrics(p.font()).horizontalAdvance(label[:60]) + 16
                box = QRectF(pt.x() - tw / 2, pt.y() - 62, tw, 24)
                p.setBrush(QColor(255, 255, 255, 235))
                p.drawRoundedRect(box, 8, 8)
                p.setPen(QColor(30, 40, 50))
                p.drawText(box, _AL.AlignCenter, label[:60])
        p.setFont(_F(8))                                                 # OpenStreetMap's licence asks for this credit
        cr = "© OpenStreetMap contributors"
        cw = QFontMetrics(p.font()).horizontalAdvance(cr) + 12
        box = QRectF(view.right() - cw - 8, view.bottom() - 24 - (66 if fs else 0), cw, 18)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 215))
        p.drawRoundedRect(box, 6, 6)
        p.setPen(QColor(50, 60, 70))
        p.drawText(box, _AL.AlignCenter, cr)
        p.restore()
        if not fs:                                                       # a fine rim around the map window
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(_q("#ffffff", 70), 1.2))
            p.drawRoundedRect(view.adjusted(0.6, 0.6, -0.6, -0.6), 14, 14)
        p.setPen(Qt.PenStyle.NoPen)

    def _card_text(self, p: QPainter, c: Card, rect: QRectF):
        d = c.data
        mono = bool(d.get("mono"))
        if c.k > 0.5:
            inner = rect.adjusted(34, 26, -34, -26)
            acc = QColor(_accent_for(d))
            self._chip(p, QRectF(inner.left(), inner.top() + 2, 40, 40), acc, _glyph_for(d), _emoji_for(d))
            self._title(p, QRectF(inner.left() + 54, inner.top(), inner.width() - 54, 44), d["title"], 18, "#ffffff")
            ln = QLinearGradient(inner.left(), 0, inner.right(), 0)
            ln.setColorAt(0, _q(acc, 210))
            ln.setColorAt(1, _q(acc, 0))
            p.setPen(QPen(QBrush(ln), 1.4))
            p.drawLine(QPointF(inner.left(), inner.top() + 52), QPointF(inner.right(), inner.top() + 52))
            view = QRectF(inner.left(), inner.top() + 60, inner.width(), inner.height() - 60)
            f = QFont("Cascadia Mono" if mono else "Segoe UI", 11 if mono else 13)
            p.setFont(f)
            p.setPen(QColor(C.TEXT))
            opt = QTextOption()
            opt.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere if mono else QTextOption.WrapMode.WordWrap)
            text = d.get("full") or d.get("text") or ""
            full_h = QFontMetrics(f).boundingRect(QRectF(0, 0, view.width() - 14, 10 ** 6).toRect(),
                                                  int(Qt.TextFlag.TextWordWrap), text).height()
            c.scroll = max(0.0, min(c.scroll, max(0.0, full_h - view.height() + 10)))
            p.save()
            p.setClipRect(view)
            p.drawText(QRectF(view.left(), view.top() - c.scroll, view.width() - 14, full_h + 40), text, opt)
            p.restore()
            if full_h > view.height():                                    # scroll bar
                tr = QRectF(view.right() - 6, view.top(), 4, view.height())
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(255, 255, 255, 26))
                p.drawRoundedRect(tr, 2, 2)
                th = max(30.0, view.height() * view.height() / full_h)
                ty = tr.top() + (tr.height() - th) * (c.scroll / max(1.0, full_h - view.height()))
                tg = QLinearGradient(0, ty, 0, ty + th)
                tg.setColorAt(0, QColor(C.PRI))
                tg.setColorAt(1, QColor(C.ACC))
                p.setBrush(QBrush(tg))
                p.drawRoundedRect(QRectF(tr.left(), ty, 4, th), 2, 2)
            return
        inner = rect.adjusted(16, 14, -16, -14)                          # resting card: badge, title, subtitle, a peek of the text
        acc = QColor(_accent_for(d))
        self._chip(p, QRectF(inner.left(), inner.top(), 32, 32), acc, _glyph_for(d), _emoji_for(d))
        tx = inner.left() + 42
        self._title(p, QRectF(tx, inner.top() - 1, inner.right() - tx, 18), d["title"], 11, "#ffffff")
        p.setFont(_F(8))
        p.setPen(QColor(C.TEXT_DIM))
        p.drawText(QRectF(tx, inner.top() + 17, inner.right() - tx, 16), _AL.AlignLeft | _AL.AlignVCenter,
                   _elided(d.get("sub", ""), 8, int(inner.right() - tx)))
        ln = QLinearGradient(inner.left(), 0, inner.right(), 0)
        ln.setColorAt(0, _q(acc, 120))
        ln.setColorAt(1, _q(acc, 0))
        p.setPen(QPen(QBrush(ln), 1.0))
        p.drawLine(QPointF(inner.left(), inner.top() + 42), QPointF(inner.right(), inner.top() + 42))
        p.setFont(QFont("Cascadia Mono" if mono else "Segoe UI", 8 if mono else 9))
        p.setPen(QColor(C.TEXT_MED))
        opt = QTextOption()
        opt.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        p.save()
        p.setClipRect(QRectF(inner.left(), inner.top() + 48, inner.width(), inner.height() - 48))
        p.drawText(QRectF(inner.left(), inner.top() + 48, inner.width(), inner.height() - 48), d.get("text", "") or "(empty)", opt)
        p.restore()

    # pictures
    def _card_image(self, p: QPainter, c: Card, rect: QRectF):
        d = c.data
        path = d["image"]
        big = self._large.get(path) if c.k > 0.3 else None
        px = big or self._thumbs.get(path)
        fs = self._fs and c.open
        pad, rad = (0, 0) if fs else (4, 12)
        tr = rect.adjusted(pad, pad, -pad, -pad)
        clip = QPainterPath()
        clip.addRoundedRect(tr, rad, rad)
        p.save()
        p.setClipPath(clip)
        if px is None:
            p.setBrush(QColor(255, 255, 255, 14 + int(10 * math.sin(time.time() * 4))))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRect(tr)
            if path in self._failed:
                p.setPen(QColor(C.TEXT_DIM))
                p.setFont(_F(10))
                p.drawText(tr, _AL.AlignCenter, "Can't show this picture")
        else:
            iw, ih = px.width(), px.height()
            if c.k > 0.3:                                                     # opened: the whole picture, zoomable
                z = max(1.0, c.zoom)
                sw, sh = iw / z, ih / z
                sx = max(0.0, min(iw - sw, c.vcx * iw - sw / 2))
                sy = max(0.0, min(ih - sh, c.vcy * ih - sh / 2))
                p.drawPixmap(tr, px, QRectF(sx, sy, sw, sh))
            else:                                                             # small card: fill it, crop the overflow
                k = max(tr.width() / iw, tr.height() / ih)
                sw, sh = tr.width() / k, tr.height() / k
                p.drawPixmap(tr, px, QRectF((iw - sw) / 2, (ih - sh) / 2, sw, sh))
        if not fs:
            strip = QLinearGradient(0, tr.bottom() - 62, 0, tr.bottom())
            strip.setColorAt(0, QColor(0, 0, 0, 0))
            strip.setColorAt(1, QColor(0, 4, 12, 215))
            p.fillRect(QRectF(tr.left(), tr.bottom() - 62, tr.width(), 62), QBrush(strip))
        p.restore()
        if fs:
            return
        p.setBrush(Qt.BrushStyle.NoBrush)                                  # a hairline rim so the picture sits in the glass
        p.setPen(QPen(_q("#ffffff", 56), 1.0))
        p.drawRoundedRect(tr.adjusted(0.5, 0.5, -0.5, -0.5), rad, rad)
        title = d["title"] if c.k < 0.5 else d["title"] + (f"   ·   {d['dims'][0]}×{d['dims'][1]}" if d.get("dims") else "")
        self._title(p, QRectF(rect.left() + 16, rect.bottom() - 34, rect.width() - 100, 26), title, 10 if c.k < 0.5 else 12, "#ffffff")
        if c.k > 0.5 and c.zoom > 1.01:
            p.setFont(_F(11, QFont.Weight.DemiBold))
            p.setPen(QColor(255, 255, 255, 230))
            p.drawText(QRectF(rect.right() - 100, rect.bottom() - 34, 84, 26), _AL.AlignRight | _AL.AlignVCenter, f"{int(c.zoom * 100)}%")

    # 3D models: shaded by the GPU
    def _card_model(self, p: QPainter, c: Card, rect: QRectF):
        d = c.data
        path = d["path"]
        fs = self._fs and c.open
        pad, rad = (0, 0) if fs else (4, 12)
        tr = rect.adjusted(pad, pad, -pad, -pad)
        g = QLinearGradient(tr.topLeft(), tr.bottomRight())
        g.setColorAt(0, QColor(30, 22, 72, 255 if fs else 225))
        g.setColorAt(0.55, QColor(10, 20, 52, 255 if fs else 232))
        g.setColorAt(1, QColor(3, 8, 20, 255 if fs else 240))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(g)
        p.drawRoundedRect(tr, rad, rad)
        if not fs:                                                        # a faint spotlight behind the model
            spot = QRadialGradient(QPointF(tr.center().x(), tr.center().y() - 14), tr.width() * 0.55)
            spot.setColorAt(0, _q(C.ACC, 46))
            spot.setColorAt(1, _q(C.ACC, 0))
            p.setBrush(QBrush(spot))
            p.drawRoundedRect(tr, rad, rad)
        opened = c.k > 0.3
        drawn = False
        state = "off"
        if self._cv is not None and self._mesh.ok:
            state = self._mesh_state(c, opened)
            if state == "ready" and opened:                      # the card only says "3D"; the model itself is drawn once opened
                area = tr if fs else tr.adjusted(0, 4, 0, -54)
                box = p.worldTransform().mapRect(area)
                drawn = self._mesh.draw(p, self._cv, path, box, c.yaw, c.pitch, c.zoom, p.opacity(), self._cm, self._ca)
        if not drawn:
            msg = {"loading": "Loading 3D model…", "failed": self._mfail.get(path, "Can't show this model"),
                   "off": self._gl_why(), "idle": ""}.get(state, "")
            px = self._model_thumb(c)
            if px is not None:                                           # the model's picture instead of a bare "3D"
                area = tr if fs else tr.adjusted(8, 8, -8, -52)
                side = max(8.0, min(area.width(), area.height()))
                p.save()
                p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
                p.setOpacity(p.opacity() * (0.55 if opened and state == "loading" else 1.0))
                p.drawPixmap(QRectF(area.center().x() - side / 2, area.center().y() - side / 2, side, side), px,
                             QRectF(0, 0, px.width(), px.height()))
                p.restore()
                tag = QRectF(tr.left() + 12, tr.top() + 12, 34, 20)                # small "3D" pill in the corner
                p.setPen(QPen(_q(C.ACC, 150), 1))
                p.setBrush(_q(C.ACC, 40))
                p.drawRoundedRect(tag, 10, 10)
                p.setFont(_F(8, QFont.Weight.Bold, spacing=0.8))
                p.setPen(_mix(C.ACC, "#ffffff", 0.35))
                p.drawText(tag, _AL.AlignCenter, "3D")
            else:
                self._chip(p, QRectF(tr.center().x() - 28, tr.center().y() - 52, 56, 56), QColor(C.ACC), "cube")
                p.setFont(_F(10, QFont.Weight.Bold, spacing=1.6))
                p.setPen(_mix(C.ACC, "#ffffff", 0.35))
                p.drawText(QRectF(tr.left(), tr.center().y() + 10, tr.width(), 22), _AL.AlignCenter, "3D MODEL")
            if msg and (px is None or opened):
                p.setFont(_F(9))
                p.setPen(QColor(C.TEXT_DIM))
                p.drawText(QRectF(tr.left() + 10, tr.bottom() - 62, tr.width() - 20, 22), _AL.AlignCenter,
                           QFontMetrics(p.font()).elidedText(msg, Qt.TextElideMode.ElideRight, int(tr.width() - 24)))
        if fs:
            return
        strip = QLinearGradient(0, tr.bottom() - 62, 0, tr.bottom())
        strip.setColorAt(0, QColor(0, 0, 0, 0))
        strip.setColorAt(1, QColor(0, 4, 12, 215))
        p.fillRect(QRectF(tr.left() + 1, tr.bottom() - 62, tr.width() - 2, 61), QBrush(strip))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(_q("#ffffff", 56), 1.0))
        p.drawRoundedRect(tr.adjusted(0.5, 0.5, -0.5, -0.5), rad, rad)
        tris = self._tris.get(path)
        title = d["title"] + (f"   ·   {tris:,} triangles" if opened and tris else "")
        self._title(p, QRectF(rect.left() + 16, rect.bottom() - 34, rect.width() - 100, 26), title, 10 if not opened else 12, "#ffffff")
        if c.k > 0.5 and abs(c.zoom - 1.0) > 0.01:
            p.setFont(_F(11, QFont.Weight.DemiBold))
            p.setPen(QColor(255, 255, 255, 230))
            p.drawText(QRectF(rect.right() - 100, rect.bottom() - 34, 84, 26), _AL.AlignRight | _AL.AlignVCenter, f"{int(c.zoom * 100)}%")

    # videos: decoded and rendered by the GPU (mpv)
    def _card_video(self, p: QPainter, c: Card, rect: QRectF):
        d = c.data
        path = d["path"]
        fs = self._fs and c.open
        pad, rad = (0, 0) if fs else (4, 12)
        tr = rect.adjusted(pad, pad, -pad, -pad)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 255 if fs else 215))
        p.drawRoundedRect(tr, rad, rad)
        playing = False
        err = self._vfail.get(path, "")
        if c.open and self._cv is not None and not err:
            v = self._video
            if not v.active or v.path != path:
                if not v.start(path):
                    self._vfail[path] = v.err or "player error"
                    err = self._vfail[path]
            if v.active and not err:
                box = p.worldTransform().mapRect(tr)
                playing = v.draw(p, self._cv, box, p.opacity(), (c.ow, c.oh) if fs else (c.ow - 8, c.oh - 8))
                pos, dur, asp, _ended = v.info()
                if asp and abs(asp - (d.get("aspect") or 0.0)) > 0.01:
                    d["aspect"] = asp
                if not playing and v.err:
                    self._vfail[path] = v.err
                    err = v.err
                if playing:
                    if not fs:                                       # full screen draws this bar itself, above the controls
                        bar = QRectF(tr.left() + 18, tr.bottom() - 58, tr.width() - 36, 5)
                        self._scrub(p, bar, (pos / dur) if dur > 0 else 0.0)
                        p.setFont(_F(9, QFont.Weight.DemiBold))
                        p.setPen(QColor(255, 255, 255, 230))
                        fm = lambda s: f"{int(s) // 60}:{int(s) % 60:02d}"         # noqa: E731
                        p.drawText(QRectF(bar.left(), bar.top() - 24, bar.width(), 20), _AL.AlignRight | _AL.AlignVCenter,
                                   f"{fm(pos)} / {fm(dur)}")
                    if c.paused:
                        ctr = tr.center()
                        p.setPen(QPen(QColor(255, 255, 255, 90), 1.4))
                        p.setBrush(QColor(4, 10, 24, 150))
                        p.drawEllipse(ctr, 40, 40)
                        p.setPen(Qt.PenStyle.NoPen)
                        p.setBrush(QColor(255, 255, 255, 240))
                        cx, cy = ctr.x(), ctr.y()
                        p.drawPolygon(QPolygonF([QPointF(cx - 10, cy - 17), QPointF(cx - 10, cy + 17), QPointF(cx + 19, cy)]))
                    self._anim = True
        if not playing:
            cx, cy = tr.center().x(), tr.center().y() - 12
            halo = QRadialGradient(QPointF(cx, cy), 62)                      # a glowing play button
            halo.setColorAt(0, _q(C.ACC2, 70))
            halo.setColorAt(1, _q(C.ACC2, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(halo))
            p.drawEllipse(QPointF(cx, cy), 62, 62)
            pg = QLinearGradient(cx, cy - 30, cx, cy + 30)
            pg.setColorAt(0, _q(C.ACC2, 120))
            pg.setColorAt(1, _q(C.ACC, 70))
            p.setPen(QPen(_q("#ffffff", 150), 1.6))
            p.setBrush(QBrush(pg))
            p.drawEllipse(QPointF(cx, cy), 29, 29)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 240))
            p.drawPolygon(QPolygonF([QPointF(cx - 8, cy - 13), QPointF(cx - 8, cy + 13), QPointF(cx + 14, cy)]))
            msg = err and f"GPU video unavailable: {err}"[:120]
            if not msg and c.open and self._cv is None:
                msg = "GPU video: " + self._gl_why()
            elif not msg and c.open:
                msg = "Loading video…"
            if msg:
                p.setFont(_F(9))
                p.setPen(QColor(C.AMBER))
                p.drawText(QRectF(tr.left() + 14, tr.bottom() - 96, tr.width() - 28, 40), _AL.AlignCenter | Qt.TextFlag.TextWordWrap,
                           msg + ("  ·  ⧉ opens it in your default player" if err or self._cv is None else ""))
        if fs:
            return
        strip = QLinearGradient(0, tr.bottom() - 62, 0, tr.bottom())
        strip.setColorAt(0, QColor(0, 0, 0, 0))
        strip.setColorAt(1, QColor(0, 4, 12, 215))
        p.fillRect(QRectF(tr.left() + 1, tr.bottom() - 62, tr.width() - 2, 61), QBrush(strip))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(_q("#ffffff", 56), 1.0))
        p.drawRoundedRect(tr.adjusted(0.5, 0.5, -0.5, -0.5), rad, rad)
        self._title(p, QRectF(rect.left() + 16, rect.bottom() - 34, rect.width() - 100, 26), d["title"], 10 if c.k < 0.5 else 12, "#ffffff")

    # ── hands ────────────────────────────────────────────────────────────────────────
    def _skeleton(self) -> bool:
        if time.time() - getattr(self, "_skel_t", 0.0) > 1.0:
            self._skel_t = time.time()
            try:
                from core import hand_cursor
                self._skel = hand_cursor.show_skeleton()
            except Exception:
                self._skel = False
        return getattr(self, "_skel", False)

    def _draw_hand(self, p: QPainter, h):
        ox, oy = h.dpos[0] - h.pos[0], h.dpos[1] - h.pos[1]          # drawn position glides; logic keeps using h.pos
        pts = [QPointF(x + ox, y + oy) for x, y in h.pts]
        col = QColor(C.GREEN if h.pinching else C.AMBER if h.claw else C.RED if h.fist else C.PRI)
        if self._skeleton():                                   # optional thin outline, never joint dots
            path = QPainterPath()
            for a, b in BONES:
                path.moveTo(pts[a])
                path.lineTo(pts[b])
            p.setPen(QPen(_q(col, 80), 1.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
        mid = QPointF(h.dpos[0], h.dpos[1])
        rad = 11.0 if h.pinching else 21.0
        glow = QRadialGradient(mid, rad * 2.8)                  # a soft halo around the pointer
        glow.setColorAt(0, _q(col, 96 if h.pinching else 58))
        glow.setColorAt(1, _q(col, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(glow))
        p.drawEllipse(mid, rad * 2.8, rad * 2.8)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(_q(col, 60), 5.0))
        p.drawEllipse(mid, rad, rad)
        p.setPen(QPen(_q(col, 240), 2.0))
        p.drawEllipse(mid, rad, rad)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_q("#ffffff", 235 if h.pinching else 190))
        p.drawEllipse(mid, 3.6 if h.pinching else 2.6, 3.6 if h.pinching else 2.6)
        if h.claw:
            pc = QPointF(*h.dpalm)
            rg = QRadialGradient(pc, 120)
            rg.setColorAt(0, _q(col, 0))
            rg.setColorAt(0.8, _q(col, 14))
            rg.setColorAt(1, _q(col, 46))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(rg))
            p.drawEllipse(pc, 120, 120)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(_q(col, 170), 1.6, Qt.PenStyle.DashLine))
            p.drawEllipse(pc, 120, 120)
        if h.fist_prog > 0.05:                                 # fist held: a ring fills up, then "back"
            self._ring(p, QPointF(*h.dpalm), h.fist_prog, QColor(C.AMBER), "Back")
        if 0.05 < h.palm_prog < 1.0:                           # flat palm held still over a film: a ring fills up, then pause / play
            op = self.space.opened
            self._ring(p, QPointF(*h.dpalm), h.palm_prog, QColor(C.ACC), "Play" if (op is not None and op.paused) else "Pause")

    # ── shared building blocks: glass, glow, chips, glyphs, scrubber, rings ───────────
    @staticmethod
    def _glass(p: QPainter, r: QRectF, rad: float, accent=None, hot: float = 0.0, dark: bool = False,
               shadow: bool = False, tint=None):
        """A frosted-glass plate: optional drop shadow, vertical fill, gradient rim that lights up with `hot`, top sheen."""
        acc = QColor(accent if accent is not None else C.PRI)
        p.setPen(Qt.PenStyle.NoPen)
        if shadow:
            for i, al in enumerate((28, 17, 9)):
                g = 5 + i * 7
                p.setBrush(QColor(0, 0, 0, al))
                p.drawRoundedRect(r.adjusted(-g, -g + 10, g, g + 10), rad + g, rad + g)
        top = QColor(32, 58, 104, 238 if dark else 132)
        bot = QColor(9, 18, 40, 248 if dark else 116)
        if tint is not None:
            top, bot = _mix(top, tint, 0.40), _mix(bot, tint, 0.30)
        if hot > 0.01:
            top, bot = _mix(top, acc, 0.24 * hot), _mix(bot, acc, 0.16 * hot)
        fill = QLinearGradient(r.topLeft(), r.bottomLeft())
        fill.setColorAt(0, top)
        fill.setColorAt(1, bot)
        p.setBrush(QBrush(fill))
        p.drawRoundedRect(r, rad, rad)
        rim = QLinearGradient(r.topLeft(), r.bottomLeft())
        rim.setColorAt(0, QColor(255, 255, 255, int(96 + 110 * hot)))
        rim.setColorAt(0.5, QColor(255, 255, 255, int(26 + 30 * hot)))
        rim.setColorAt(1, _q(acc, 70 + 140 * hot))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QBrush(rim), 1.2))
        p.drawRoundedRect(r.adjusted(0.6, 0.6, -0.6, -0.6), rad - 0.6, rad - 0.6)
        sheen = QPainterPath()
        sheen.addRoundedRect(r.adjusted(2, 2, -2, -r.height() * 0.52), max(2.0, rad - 2), max(2.0, rad - 2))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 14))
        p.drawPath(sheen)

    @staticmethod
    def _glow(p: QPainter, rect: QRectF, rad: float, col, a: float = 1.0, width: float = 1.6):
        """A neon outline: two wide faint strokes under a crisp one."""
        p.setBrush(Qt.BrushStyle.NoBrush)
        for w, al in ((width + 9, 20), (width + 4.5, 44), (width, 215)):
            p.setPen(QPen(_q(col, al * a), w))
            p.drawRoundedRect(rect, rad, rad)

    def _chip(self, p: QPainter, r: QRectF, accent, glyph: str, emoji: str = ""):
        """A small rounded badge with a line icon, tinted with the card's accent."""
        acc = QColor(accent)
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        g.setColorAt(0, _q(acc, 84))
        g.setColorAt(1, _q(acc, 20))
        rad = r.width() * 0.3
        p.setBrush(QBrush(g))
        p.setPen(QPen(_q(acc, 140), 1.1))
        p.drawRoundedRect(r, rad, rad)
        if emoji:                                                          # the icon the user picked for this card
            p.setFont(_emoji_font(max(10, int(r.width() * 0.48))))
            p.setPen(QColor("#ffffff"))
            p.drawText(r, _AL.AlignCenter, emoji)
            return
        pad = r.width() * 0.27
        self._glyph(p, glyph, r.adjusted(pad, pad, -pad, -pad), _mix(acc, "#ffffff", 0.45), max(1.4, r.width() * 0.05))

    @staticmethod
    def _glyph(p: QPainter, kind: str, r: QRectF, col: QColor, wd: float = 1.7):
        p.setPen(QPen(col, wd, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        x, y, w, h = r.x(), r.y(), r.width(), r.height()
        cx, cy = r.center().x(), r.center().y()
        L = lambda a, b, c, d: p.drawLine(QPointF(a, b), QPointF(c, d))       # noqa: E731
        if kind == "folder":
            path = QPainterPath()
            path.moveTo(x, y + h * 0.34)
            path.lineTo(x, y + h * 0.12)
            path.lineTo(x + w * 0.38, y + h * 0.12)
            path.lineTo(x + w * 0.52, y + h * 0.3)
            p.drawPath(path)
            p.drawRoundedRect(QRectF(x, y + h * 0.3, w, h * 0.64), 3, 3)
        elif kind == "calendar":
            p.drawRoundedRect(QRectF(x, y + h * 0.14, w, h * 0.82), 3, 3)
            L(x, y + h * 0.42, x + w, y + h * 0.42)
            L(x + w * 0.3, y, x + w * 0.3, y + h * 0.26)
            L(x + w * 0.7, y, x + w * 0.7, y + h * 0.26)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(col)
            for dx in (-0.2, 0.2):
                p.drawEllipse(QPointF(cx + w * dx, cy + h * 0.22), 1.3, 1.3)
        elif kind == "mail":
            p.drawRoundedRect(QRectF(x, y + h * 0.16, w, h * 0.68), 3, 3)
            path = QPainterPath()
            path.moveTo(x + 1.5, y + h * 0.26)
            path.lineTo(cx, cy + h * 0.1)
            path.lineTo(x + w - 1.5, y + h * 0.26)
            p.drawPath(path)
        elif kind == "wallet":
            p.drawRoundedRect(QRectF(x, y + h * 0.18, w, h * 0.7), 3.5, 3.5)
            L(x + w * 0.1, y + h * 0.18, x + w * 0.62, y + h * 0.04)
            p.drawRoundedRect(QRectF(x + w * 0.6, cy - h * 0.04, w * 0.4, h * 0.3), 2.5, 2.5)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(col)
            p.drawEllipse(QPointF(x + w * 0.78, cy + h * 0.11), 1.3, 1.3)
        elif kind == "globe":
            p.drawEllipse(QPointF(cx, cy), w / 2, h / 2)
            p.drawEllipse(QPointF(cx, cy), w * 0.2, h / 2)
            L(x, cy, x + w, cy)
        elif kind == "cube":
            p.drawPolygon(QPolygonF([QPointF(cx, y), QPointF(x + w, y + h * 0.25), QPointF(x + w, y + h * 0.75),
                                     QPointF(cx, y + h), QPointF(x, y + h * 0.75), QPointF(x, y + h * 0.25)]))
            L(cx, cy, x, y + h * 0.25)
            L(cx, cy, x + w, y + h * 0.25)
            L(cx, cy, cx, y + h)
        elif kind == "film":
            p.drawRoundedRect(QRectF(x, y + h * 0.1, w, h * 0.8), 3, 3)
            p.setBrush(col)
            p.drawPolygon(QPolygonF([QPointF(cx - w * 0.12, cy - h * 0.2), QPointF(cx - w * 0.12, cy + h * 0.2), QPointF(cx + w * 0.2, cy)]))
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
        elif kind == "code":
            L(x + w * 0.3, y + h * 0.2, x, cy)
            L(x, cy, x + w * 0.3, y + h * 0.8)
            L(x + w * 0.7, y + h * 0.2, x + w, cy)
            L(x + w, cy, x + w * 0.7, y + h * 0.8)
            L(x + w * 0.58, y + h * 0.1, x + w * 0.42, y + h * 0.9)
        else:                                                           # "doc"
            path = QPainterPath()
            path.moveTo(x + w * 0.12, y)
            path.lineTo(x + w * 0.62, y)
            path.lineTo(x + w * 0.88, y + h * 0.26)
            path.lineTo(x + w * 0.88, y + h)
            path.lineTo(x + w * 0.12, y + h)
            path.closeSubpath()
            p.drawPath(path)
            L(x + w * 0.3, y + h * 0.5, x + w * 0.7, y + h * 0.5)
            L(x + w * 0.3, y + h * 0.7, x + w * 0.6, y + h * 0.7)

    @staticmethod
    def _scrub(p: QPainter, bar: QRectF, frac: float):
        """The video progress bar: glassy track, gradient fill and a glowing knob."""
        frac = max(0.0, min(1.0, frac))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 54))
        p.drawRoundedRect(bar, bar.height() / 2, bar.height() / 2)
        fw = max(bar.height(), bar.width() * frac)
        fill = QRectF(bar.left(), bar.top(), fw, bar.height())
        g = QLinearGradient(fill.left(), 0, fill.right() + 1, 0)
        g.setColorAt(0, QColor(C.PRI))
        g.setColorAt(1, QColor(C.ACC))
        p.setBrush(QBrush(g))
        p.drawRoundedRect(fill, bar.height() / 2, bar.height() / 2)
        kx, ky = fill.right(), bar.center().y()
        p.setBrush(_q(C.PRI, 60))
        p.drawEllipse(QPointF(kx, ky), bar.height() * 2.0, bar.height() * 2.0)
        p.setBrush(QColor(255, 255, 255))
        p.drawEllipse(QPointF(kx, ky), bar.height() * 1.05, bar.height() * 1.05)

    @staticmethod
    def _ring(p: QPainter, pc: QPointF, prog: float, col: QColor, label: str):
        """A hold-to-confirm ring around the palm, with a small caption beneath it."""
        rect = QRectF(pc.x() - 46, pc.y() - 46, 92, 92)
        p.setBrush(QColor(0, 6, 16, 110))
        p.setPen(QPen(QColor(255, 255, 255, 36), 6))
        p.drawEllipse(rect)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(_q(col, 70), 10, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(rect, 90 * 16, int(-360 * 16 * prog))
        p.setPen(QPen(col, 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(rect, 90 * 16, int(-360 * 16 * prog))
        p.setFont(_F(10, QFont.Weight.DemiBold))
        tw = QFontMetrics(p.font()).horizontalAdvance(label) + 24
        cap = QRectF(pc.x() - tw / 2, pc.y() + 56, tw, 24)
        p.setPen(QPen(_q(col, 130), 1))
        p.setBrush(QColor(4, 12, 28, 215))
        p.drawRoundedRect(cap, 12, 12)
        p.setPen(col)
        p.drawText(cap, _AL.AlignCenter, label)

    def _draw_dock(self, p: QPainter):
        """A frosted capsule behind the viewer's control buttons, so they read as one bar."""
        bs = [b for b in self.space.buttons if not b.get("menu") and b["id"].startswith(("v_", "a_more"))]
        if len(bs) < 2:
            return
        x0 = min(b["x"] for b in bs)
        x1 = max(b["x"] + b["w"] for b in bs)
        y0 = min(b["y"] for b in bs)
        y1 = max(b["y"] + b["h"] for b in bs)
        r = QRectF(x0 - 18, y0 - 12, x1 - x0 + 36, y1 - y0 + 24)
        self._glass(p, r, min(32.0, r.height() / 2), C.PRI, 0.0, dark=True, shadow=True)

    def _draw_pager(self, p: QPainter, W: int, H: int):
        sp = self.space
        r = QRectF(W / 2 - 66, H - 112 + 8, 132, 36)
        self._glass(p, r, 18, C.PRI, 0.0, dark=True)
        p.setFont(_F(11, QFont.Weight.DemiBold, spacing=0.5))
        p.setPen(QColor(C.TEXT))
        p.drawText(r, _AL.AlignCenter, f"{sp.page + 1}  /  {sp.pages}")

    def _draw_hint(self, p: QPainter, W: int, H: int):
        """The gesture cheat-sheet at the bottom: `Gesture  does this` pairs in a capsule, divided by dots."""
        parts = [s.strip() for s in str(self.space.hint or "").split("·") if s.strip()] + ["Esc / ✕ to leave"]
        fk, fv = _F(9, QFont.Weight.DemiBold), _F(9)
        mk, mv = QFontMetrics(fk), QFontMetrics(fv)
        items = []
        for s in parts:
            if "=" in s:
                k, v = (t.strip() for t in s.split("=", 1))
            else:
                k, v = "", s
            wk = mk.horizontalAdvance(k) if k else 0
            wv = mv.horizontalAdvance(v)
            items.append((k, v, wk, wv, wk + wv + (8 if k else 0)))
        sep = 26
        total = sum(it[4] for it in items) + sep * (len(items) - 1)
        h = 32
        y = H - 58
        if total > W - 110:                                            # too long for one line: just elide it
            p.setFont(fv)
            p.setPen(QColor(C.TEXT_DIM))
            p.drawText(QRectF(40, y, W - 80, h), _AL.AlignCenter,
                       QFontMetrics(fv).elidedText("   ·   ".join(parts), Qt.TextElideMode.ElideRight, W - 100))
            return
        r = QRectF((W - total) / 2 - 24, y, total + 48, h)
        self._glass(p, r, h / 2, C.PRI, 0.0, dark=True)
        x = r.left() + 24
        for i, (k, v, wk, wv, wt) in enumerate(items):
            if k:
                p.setFont(fk)
                p.setPen(QColor(C.PRI))
                p.drawText(QRectF(x, y, wk + 2, h), _AL.AlignLeft | _AL.AlignVCenter, k)
            p.setFont(fv)
            p.setPen(QColor(C.TEXT_MED))
            p.drawText(QRectF(x + wk + (8 if k else 0), y, wv + 2, h), _AL.AlignLeft | _AL.AlignVCenter, v)
            x += wt
            if i < len(items) - 1:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(_q(C.TEXT_DIM, 190))
                p.drawEllipse(QPointF(x + sep / 2, y + h / 2), 1.7, 1.7)
                x += sep

    def _draw_empty(self, p: QPainter, W: int, H: int):
        r = QRectF(W / 2 - 46, H / 2 - 92, 92, 92)
        self._glass(p, r, 46, C.PRI, 0.0, dark=True, shadow=True)
        self._glyph(p, "folder", r.adjusted(27, 28, -27, -28), _q(C.PRI, 235), 2.2)
        p.setFont(_F(17, QFont.Weight.DemiBold))
        p.setPen(QColor(C.TEXT))
        p.drawText(QRectF(0, H / 2 + 14, W, 32), _AL.AlignCenter, "This folder is empty")
        p.setFont(_F(10))
        p.setPen(QColor(C.TEXT_DIM))
        p.drawText(QRectF(0, H / 2 + 46, W, 22), _AL.AlignCenter, "Nothing to show here yet")

    def _draw_toast(self, p: QPainter, W: int, H: int, text: str):
        p.setFont(_F(11, QFont.Weight.DemiBold))
        tw = QFontMetrics(p.font()).horizontalAdvance(text) + 70
        r = QRectF((W - tw) / 2, H - 178, tw, 42)
        self._glass(p, r, 21, C.GREEN, 0.0, dark=True, shadow=True)
        dot = QPointF(r.left() + 24, r.center().y())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_q(C.GREEN, 56))
        p.drawEllipse(dot, 8, 8)
        p.setBrush(QColor(C.GREEN))
        p.drawEllipse(dot, 3.6, 3.6)
        p.setPen(QColor(C.TEXT))
        p.drawText(QRectF(r.left() + 40, r.top(), r.width() - 54, r.height()), _AL.AlignLeft | _AL.AlignVCenter, text)


#by ASDcuber

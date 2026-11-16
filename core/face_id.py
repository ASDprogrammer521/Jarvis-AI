"""Face recognition (OpenCV YuNet detector + SFace recognizer, all local). Off by default.

Only 128-number face "fingerprints" are stored (memory/faces.json) — never photos — and they never leave the PC.
Models (~37 MB) download once to config/models/ the first time it is switched on.
"""
from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path

from core.common_utils import project_root

MODELS = project_root() / "config" / "models"
STORE = project_root() / "memory" / "faces.json"
DET_PATH = MODELS / "face_detection_yunet_2023mar.onnx"
REC_PATH = MODELS / "face_recognition_sface_2021dec.onnx"
_BASE = "https://github.com/opencv/opencv_zoo/raw/main/models/"
DET_URL = _BASE + "face_detection_yunet/face_detection_yunet_2023mar.onnx"
REC_URL = _BASE + "face_recognition_sface/face_recognition_sface_2021dec.onnx"

DETECT_W = 320              # faces are searched for in a picture this wide (YuNet is nearly as accurate there, 3-4x faster)
RECHECK = 4.0               # a known face that stays put is not recognised again for this many seconds
MOVED = 0.35                # …unless its box moved / grew by more than this share of its width: then it is checked at once
MATCH_THRESHOLD = 0.42      # cosine similarity; SFace's own "same person" cut-off is 0.363 — a bit stricter here
_LOCK = threading.Lock()


# ── config ─────────────────────────────────────────────────────────────────────
def _cfg() -> dict:
    from memory.config_manager import load_api_keys
    return load_api_keys()


def _set(**kw) -> None:
    from memory.config_manager import _patch_config
    _patch_config(**kw)


def is_enabled() -> bool:
    return bool(_cfg().get("face_enabled", False))


def set_enabled(on: bool) -> None:
    _set(face_enabled=bool(on))


def greet_enabled() -> bool:
    return bool(_cfg().get("face_greet", False))


def set_greet(on: bool) -> None:
    _set(face_greet=bool(on))


def alert_unknown() -> bool:
    return bool(_cfg().get("face_alert_unknown", False))


def set_alert_unknown(on: bool) -> None:
    _set(face_alert_unknown=bool(on))


# ── storage ────────────────────────────────────────────────────────────────────
def _load() -> dict:
    try:
        d = json.loads(STORE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(d: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d), encoding="utf-8")
    tmp.replace(STORE)


def people() -> dict[str, list]:
    with _LOCK:
        return _load()


def names() -> list[str]:
    return sorted(people())


def add_person(name: str, embeddings: list) -> None:
    name = name.strip()[:40]
    with _LOCK:
        d = _load()
        d.setdefault(name, [])
        d[name] = (d[name] + [list(map(float, e)) for e in embeddings])[-25:]
        _save(d)


def forget(name: str) -> bool:
    with _LOCK:
        d = _load()
        key = next((k for k in d if k.lower() == name.strip().lower()), None)
        if key is None:
            return False
        del d[key]
        _save(d)
        return True


def forget_all() -> None:
    with _LOCK:
        _save({})


# ── models ─────────────────────────────────────────────────────────────────────
def _fetch(url: str, path: Path, min_bytes: int) -> None:
    if path.exists() and path.stat().st_size > min_bytes:
        return
    import requests
    path.parent.mkdir(parents=True, exist_ok=True)
    r = requests.get(url, stream=True, timeout=60)
    r.raise_for_status()
    tmp = path.with_suffix(".part")
    with open(tmp, "wb") as fh:
        for chunk in r.iter_content(1 << 16):
            fh.write(chunk)
    if tmp.stat().st_size <= min_bytes:        # a Git-LFS pointer or an error page, not the model
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"download of {path.name} was incomplete")
    tmp.replace(path)


_MATRIX: dict = {"key": None, "names": [], "mat": None}


class FaceEngine:
    """YuNet finds faces on a small picture; SFace (loaded only when a face is actually there) tells who it is."""

    def __init__(self):
        import cv2
        import numpy as np
        self.cv2, self.np = cv2, np
        if not (hasattr(cv2, "FaceDetectorYN") and hasattr(cv2, "FaceRecognizerSF")):
            raise RuntimeError("OpenCV 4.5.4+ is needed for face recognition: pip install -U opencv-python")
        _fetch(DET_URL, DET_PATH, 100_000)
        self.det = cv2.FaceDetectorYN.create(str(DET_PATH), "", (DETECT_W, 240), 0.85, 0.3, 20)
        self._rec = None
        self._size = (DETECT_W, 240)

    @property
    def rec_ready(self) -> bool:
        return self._rec is not None

    @property
    def rec(self):
        """The recogniser (~37 MB) starts on first use — an empty room never pays for it."""
        if self._rec is None:
            _fetch(REC_URL, REC_PATH, 10_000_000)
            self._rec = self.cv2.FaceRecognizerSF.create(str(REC_PATH), "")
        return self._rec

    def detect(self, bgr):
        """Faces as YuNet rows (x, y, w, h, 5 landmarks, score) in the coordinates of the FULL picture, found on a small copy."""
        h, w = bgr.shape[:2]
        scale = DETECT_W / float(w) if w > DETECT_W else 1.0
        small = self.cv2.resize(bgr, (DETECT_W, max(1, int(round(h * scale)))), interpolation=self.cv2.INTER_AREA) if scale < 1.0 else bgr
        sh, sw = small.shape[:2]
        if (sw, sh) != self._size:
            self.det.setInputSize((sw, sh))
            self._size = (sw, sh)
        _, faces = self.det.detect(small)
        if faces is None:
            return []
        if scale < 1.0:
            faces = faces.copy()
            faces[:, :14] /= scale                      # box and landmarks back to full size (the score stays)
        return list(faces)

    def embed(self, bgr, face):
        feat = self.rec.feature(self.rec.alignCrop(bgr, face))
        v = self.np.asarray(feat, dtype="float32").reshape(-1)
        n = float(self.np.linalg.norm(v))
        return v / n if n else v

    def _matrix(self):
        """Every saved fingerprint as one normalised matrix; rebuilt only when memory/faces.json changes (not on every check)."""
        try:
            st = STORE.stat()
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            key = None
        if _MATRIX["key"] != key or (_MATRIX["mat"] is None and key is not None):
            names, rows = [], []
            for name, embs in people().items():
                for e in embs:
                    v = self.np.asarray(e, dtype="float32")
                    n = float(self.np.linalg.norm(v))
                    if n and v.shape == (128,):
                        names.append(name)
                        rows.append(v / n)
            _MATRIX.update(key=key, names=names, mat=self.np.stack(rows) if rows else None)
        return _MATRIX["names"], _MATRIX["mat"]

    def identify(self, vec):
        """(name, score) of the best match, or (None, score) when nobody known is similar enough."""
        names, mat = self._matrix()
        if mat is None:
            return None, -1.0
        sims = mat @ vec
        i = int(sims.argmax())
        score = float(sims[i])
        return (names[i], score) if score >= MATCH_THRESHOLD else (None, score)


class FaceTracker:
    """Remembers who each face in view is, so someone sitting still is not run through the recogniser again and again.

    A KNOWN face that stays where it was is trusted for RECHECK seconds. A new face, a face that moved or grew noticeably,
    and every unknown face are recognised straight away."""

    def __init__(self):
        self.tracks: list[dict] = []

    @staticmethod
    def _box(f):
        return float(f[0]), float(f[1]), float(f[2]), float(f[3])

    def resolve(self, faces, now: float, identify) -> list[str | None]:
        """Name (or None = unknown) for each face, in order. `identify(face)` is only called when it has to be."""
        out: list = []
        used: set[int] = set()
        for f in faces:
            x, y, w, h = self._box(f)
            cx, cy = x + w / 2, y + h / 2
            best, bd = None, 1e9
            for i, t in enumerate(self.tracks):
                if i in used:
                    continue
                tx, ty, tw, th = t["box"]
                ratio = w / tw if tw else 0
                d = math.hypot(cx - (tx + tw / 2), cy - (ty + th / 2)) / max(w, tw, 1.0)
                if 0.6 < ratio < 1.67 and d < 0.7 and d < bd:
                    best, bd = i, d
            t = self.tracks[best] if best is not None else None
            if t is not None:
                used.add(best)
                rx, ry, rw, rh = t["ref"]
                shift = math.hypot(cx - (rx + rw / 2), cy - (ry + rh / 2)) / max(rw, 1.0)
                grew = abs(w / rw - 1.0) if rw else 1.0
                steady = t["name"] is not None and now - t["t_id"] < RECHECK and shift < MOVED and grew < MOVED
                t["box"], t["seen"] = (x, y, w, h), now
                if steady:
                    out.append(t["name"])
                    continue
            else:
                t = {"name": None}
                self.tracks.append(t)
                used.add(len(self.tracks) - 1)
            t.update(name=identify(f), box=(x, y, w, h), ref=(x, y, w, h), t_id=now, seen=now)
            out.append(t["name"])
        self.tracks = [t for t in self.tracks if now - t.get("seen", 0.0) < 1.5]      # forget faces gone for 1.5 s
        return out

#by ASDcuber

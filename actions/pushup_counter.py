from __future__ import annotations

import math
import os
import threading
import time
import uuid
from collections import deque
from datetime import datetime

from actions._common import BASE_DIR, DATA_DIR, json_load, json_save, log
from core import workout_stats as ws
from core.workout_counters import COUNTERS, SPECS, normalize

STORE = DATA_DIR / "workouts.json"
MODEL_PATH = BASE_DIR / "config" / "models" / "pose_landmarker_lite.task"
MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
             "pose_landmarker_lite/float16/latest/pose_landmarker_lite.task")

L_SH, R_SH, L_EL, R_EL, L_WR, R_WR, L_HIP, R_HIP, L_AN, R_AN = 11, 12, 13, 14, 15, 16, 23, 24, 27, 28
SIDES = {"L": (L_SH, L_EL, L_WR, L_HIP, L_AN), "R": (R_SH, R_EL, R_WR, R_HIP, R_AN)}

DOWN_ANGLE = 105.0
UP_ANGLE = 150.0
DEEP_ANGLE = 92.0
MIN_ALIGN = 155.0
MIN_REP_SECONDS = 0.6
MIN_VIS = 0.45

_SESSION: "Session | None" = None
_LOCK = threading.Lock()


def _angle(a, b, c) -> float:
    v1 = (a[0] - b[0], a[1] - b[1])
    v2 = (c[0] - b[0], c[1] - b[1])
    n = math.hypot(*v1) * math.hypot(*v2)
    if n < 1e-9:
        return 180.0
    cos = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / n))
    return math.degrees(math.acos(cos))


class RepCounter:
    """Push-ups. (Squat / sit-up / pull-up / plank live in core/workout_counters.py and share this interface.)"""
    unit = "reps"
    key = "pushup"

    def reset_state(self):
        self.state = "idle"
        self._smooth.clear()
        self.feedback = "Get into push-up position (side view)"

    def miss(self, now: float):
        pass

    def __init__(self):
        self.reps = 0
        self.good = 0
        self.state = "idle"
        self.position_ok = False
        self.angle = 180.0
        self.align = 180.0
        self.feedback = "Get into push-up position (side view)"
        self.rep_log: list[dict] = []
        self.issues = {"shallow": 0, "sagging hips": 0, "piked hips": 0}
        self._smooth: deque = deque(maxlen=3)
        self._min_angle = 180.0
        self._min_align = 180.0
        self._t_down = 0.0
        self._last_rep_t = 0.0
        self.last_event = ""

    def _pick_side(self, lm):
        best, score = None, 0.0
        for name, idx in SIDES.items():
            vis = sum(lm[i][2] for i in idx) / len(idx)
            if vis > score:
                best, score = name, vis
        return (best, score) if score >= MIN_VIS else (None, score)

    def update(self, lm, aspect: float = 1.0, now: float | None = None) -> str | None:
        now = time.time() if now is None else now
        side, _ = self._pick_side(lm)
        if side is None:
            self.position_ok = False
            self.feedback = "I can't see you clearly — step back so your whole body is in view"
            return None
        sh, el, wr, hip, an = [(lm[i][0] * aspect, lm[i][1]) for i in SIDES[side]]
        self._smooth.append(_angle(sh, el, wr))
        self.angle = sum(self._smooth) / len(self._smooth)
        self.align = _angle(sh, hip, an)
        span = an[0] - sh[0]
        t_line = (hip[0] - sh[0]) / span if abs(span) > 1e-6 else 0.5
        hip_off = hip[1] - (sh[1] + (an[1] - sh[1]) * t_line)
        dx, dy = abs(an[0] - sh[0]), abs(an[1] - sh[1])
        self.position_ok = dx > 0.12 and dy / (dx + 1e-6) < 0.75
        if not self.position_ok:
            if self.state == "down":
                self.state = "up"
            self.feedback = "Get into push-up position (side view)"
            return None

        event = None
        if self.state in ("idle", "up"):
            if self.angle >= UP_ANGLE:
                self.state = "up"
                self.feedback = "Ready — lower down"
            if self.state == "up" and self.angle < DOWN_ANGLE:
                self.state = "down"
                self._t_down = now
                self._min_angle = self.angle
                self._min_align = self.align
                self._hip_below = hip_off > 0
                self.feedback = "Good — now push up"
        elif self.state == "down":
            self._min_angle = min(self._min_angle, self.angle)
            if self.align < self._min_align:
                self._min_align = self.align
                self._hip_below = hip_off > 0
            if self.angle >= UP_ANGLE:
                dur = now - self._t_down
                if dur >= MIN_REP_SECONDS and now - self._last_rep_t >= MIN_REP_SECONDS:
                    event = self._finish_rep(now, dur)
                self.state = "up"
        return event

    def _finish_rep(self, now: float, dur: float) -> str:
        problems = []
        if self._min_angle > DOWN_ANGLE - 5:
            problems.append("shallow")
        if self._min_align < MIN_ALIGN:
            problems.append("sagging hips" if self._hip_below else "piked hips")
        self.reps += 1
        self._last_rep_t = now
        good = not problems
        if good:
            self.good += 1
        for p in problems:
            self.issues[p] = self.issues.get(p, 0) + 1
        depth = "deep" if self._min_angle <= DEEP_ANGLE else "good depth" if self._min_angle <= DOWN_ANGLE else "shallow"
        self.rep_log.append({"n": self.reps, "min_elbow": round(self._min_angle), "body_line": round(self._min_align),
                             "seconds": round(dur, 2), "good": good, "issues": problems})
        if good:
            self.feedback = f"Rep {self.reps} — {depth}, clean form"
        else:
            tips = {"shallow": "go lower", "sagging hips": "tighten your core, hips up",
                    "piked hips": "lower your hips, straight line"}
            self.feedback = f"Rep {self.reps} — " + ", ".join(tips.get(x, x) for x in problems)
        self.last_event = self.feedback
        return self.feedback

    _hip_below = True

    def summary(self) -> dict:
        n = len(self.rep_log)
        return {"reps": self.reps, "good_reps": self.good,
                "avg_elbow_depth": round(sum(r["min_elbow"] for r in self.rep_log) / n) if n else 0,
                "avg_rep_seconds": round(sum(r["seconds"] for r in self.rep_log) / n, 2) if n else 0,
                "issues": {k: v for k, v in self.issues.items() if v}}


class _Pose:

    def __init__(self):
        self.mode = None
        self._lm = None
        self._t0 = time.time()
        import mediapipe as mp
        self.mp = mp
        if hasattr(mp, "solutions") and hasattr(mp.solutions, "pose"):
            self._lm = mp.solutions.pose.Pose(model_complexity=0, min_detection_confidence=0.5, min_tracking_confidence=0.5)
            self.mode = "legacy"
            return
        from mediapipe.tasks import python as mpp
        from mediapipe.tasks.python import vision
        self._ensure_model()
        opts = vision.PoseLandmarkerOptions(
            base_options=mpp.BaseOptions(model_asset_path=str(MODEL_PATH)),
            running_mode=vision.RunningMode.VIDEO, num_poses=1)
        self._lm = vision.PoseLandmarker.create_from_options(opts)
        self.mode = "tasks"

    @staticmethod
    def _ensure_model():
        if MODEL_PATH.exists() and MODEL_PATH.stat().st_size > 1_000_000:
            return
        import requests
        MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        print("[Pushup] Downloading pose model (~6 MB)…")
        r = requests.get(MODEL_URL, stream=True, timeout=30)
        r.raise_for_status()
        tmp = MODEL_PATH.with_suffix(".part")
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(1 << 16):
                fh.write(chunk)
        tmp.replace(MODEL_PATH)

    def detect(self, rgb):
        if self.mode == "legacy":
            res = self._lm.process(rgb)
            if not res.pose_landmarks:
                return None
            return [(l.x, l.y, l.visibility) for l in res.pose_landmarks.landmark]
        img = self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=rgb)
        res = self._lm.detect_for_video(img, int((time.time() - self._t0) * 1000))
        if not res.pose_landmarks:
            return None
        return [(l.x, l.y, getattr(l, "visibility", 1.0) or 0.0) for l in res.pose_landmarks[0]]

    def close(self):
        try:
            self._lm.close()
        except Exception:
            pass




def _make_counter(exercise: str):
    return RepCounter() if exercise == "pushup" else COUNTERS[exercise]()


def _mmss(sec: float) -> str:
    sec = int(max(0, sec))
    return f"{sec // 60}:{sec % 60:02d}"


# ── drawing ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
def _draw_skeleton(cv2, frame, lm, counter):
    h, w = frame.shape[:2]
    k = max(1.0, w / 640.0)                       # lines and dots scale with the picture (HD frames)
    ok = counter.position_ok
    col = {"good": (120, 255, 160), "warn": (80, 170, 255), "dim": (180, 180, 180)}
    if not lm:
        return
    if hasattr(counter, "chains"):               # squat / sit-up / pull-up / plank
        for idx, state in counter.chains(lm):
            pts = [(int(lm[i][0] * w), int(lm[i][1] * h)) for i in idx if lm[i][2] >= 0.3]
            for a, b in zip(pts, pts[1:]):
                cv2.line(frame, a, b, col.get(state, col["dim"]), int(3 * k), cv2.LINE_AA)
            for p in pts:
                cv2.circle(frame, p, int(5 * k), (255, 255, 255), -1, cv2.LINE_AA)
        return
    side, _ = counter._pick_side(lm)             # push-ups
    for name, idx in SIDES.items():
        pts = [(int(lm[i][0] * w), int(lm[i][1] * h)) for i in idx]
        color = (col["good"] if counter.align >= MIN_ALIGN else col["warn"]) if (name == side and ok) else col["dim"]
        for a, b in zip(pts, pts[1:]):
            cv2.line(frame, a, b, color, int(3 * k), cv2.LINE_AA)
        for p in pts:
            cv2.circle(frame, p, int(5 * k), (255, 255, 255), -1, cv2.LINE_AA)


def _draw_bars(cv2, frame, counter, spec, elapsed: float, shown: int, target, set_no: int, sets: int):
    """The small built-in readout (used when the big on-screen counter is not showing: the normal, not full-screen panel)."""
    h, w = frame.shape[:2]
    good_col, warn_col = (120, 255, 160), (80, 170, 255)
    cv2.rectangle(frame, (0, 0), (w, 72), (20, 16, 12), -1)
    if spec.unit == "seconds":
        label = _mmss(shown) + (f" / {_mmss(target)}" if target else "")
    else:
        label = f"{shown}" + (f" / {target}" if target else "")
    cv2.putText(frame, label, (16, 52), cv2.FONT_HERSHEY_SIMPLEX, 1.55, (255, 231, 110), 3, cv2.LINE_AA)
    cv2.putText(frame, f"{spec.label}" + (f"  SET {set_no}/{sets}" if sets > 1 else ""), (250, 28),
                cv2.FONT_HERSHEY_SIMPLEX, 0.68, (225, 225, 225), 1, cv2.LINE_AA)
    cv2.putText(frame, f"{_mmss(elapsed)}   good {counter.good}", (250, 58),
                cv2.FONT_HERSHEY_SIMPLEX, 0.68, (225, 225, 225), 1, cv2.LINE_AA)
    fb = counter.feedback[:48]
    cv2.rectangle(frame, (0, h - 48), (w, h), (20, 16, 12), -1)
    cv2.putText(frame, fb, (14, h - 16), cv2.FONT_HERSHEY_SIMPLEX, 0.70,
                good_col if any(x in fb for x in ("clean", "Good", "Ready", "Strong", "Hold", "Chin over")) else warn_col,
                2, cv2.LINE_AA)


def _draw_rest(cv2, frame, left: float, next_set: int, sets: int, bars: bool):
    """Dim the picture and say REST (with the countdown when the big on-screen counter is not showing it)."""
    h, w = frame.shape[:2]
    k = max(1.0, w / 640.0)
    frame[:] = cv2.convertScaleAbs(frame, alpha=0.42, beta=0)
    txt = "REST"
    (tw, th), _ = cv2.getTextSize(txt, cv2.FONT_HERSHEY_SIMPLEX, 2.6 * k, int(5 * k))
    cv2.putText(frame, txt, ((w - tw) // 2, h // 2 - int(10 * k)), cv2.FONT_HERSHEY_SIMPLEX, 2.6 * k, (255, 231, 110), int(5 * k), cv2.LINE_AA)
    sub = f"{_mmss(left)}  -  next: set {next_set} of {sets}" if bars else f"next: set {next_set} of {sets}"
    (sw, sh), _ = cv2.getTextSize(sub, cv2.FONT_HERSHEY_SIMPLEX, 1.0 * k, int(2 * k))
    cv2.putText(frame, sub, ((w - sw) // 2, h // 2 + int(50 * k)), cv2.FONT_HERSHEY_SIMPLEX, 1.0 * k, (235, 240, 250), int(2 * k), cv2.LINE_AA)



class _HubSource:
    """Frames from Jarvis' shared camera (CameraHub): the workout counter, Face ID and the camera view all use ONE capture.
    Two programs opening the same webcam fight over it on Windows (that was the 'Unknown C++ exception' from OpenCV)."""

    def __init__(self, hub):
        self.hub = hub
        self._last = None
        self._t0 = time.time()
        self._got = False
        self._restarts = 0
        hub.claim("workout")
        hub.refresh()                                   # starts the camera at HD if nothing else has

    def read(self):
        deadline = time.time() + 1.5
        while time.time() < deadline:
            f = self.hub.latest
            if f is not None and f is not self._last:
                self._last, self._got = f, True
                return True, f.copy()                   # the shared frame is read-only: drawing needs a copy
            if not self._got:
                self._check_start()
            time.sleep(0.01)
        return False, None

    def _check_start(self):
        """Waiting for the first picture. Opening a webcam at HD can take many seconds, so be patient while the camera thread
        is alive; only give up when it reported an error, died twice, or nothing came for 40 s."""
        hub, waited = self.hub, time.time() - self._t0
        st = str(getattr(hub, "status", ""))
        if st.startswith(("error", "missing")):
            raise RuntimeError(st)
        if not hub.running and waited > 2.0:
            if self._restarts >= 2:
                raise RuntimeError("Jarvis' camera thread did not start" + (f" ({st})" if st else ""))
            self._restarts += 1
            hub.claim("workout")
            hub.refresh()
            self._t0 = time.time()
        elif waited > 40.0:
            raise RuntimeError(f"the camera sent no picture for 40 s (status: {st or 'unknown'})")

    def release(self):
        hub = self.hub
        hub.release("workout")
        threading.Thread(target=hub.refresh, daemon=True).start()   # stops the camera if nobody else needs it (Face ID keeps it)


class _OwnSource:
    """Fallback when Jarvis' shared camera is not available (a script, another camera index): open the camera directly."""

    def __init__(self, index: int):
        import cv2
        self.cv2 = cv2
        cap = cv2.VideoCapture(index, cv2.CAP_DSHOW) if os.name == "nt" else cv2.VideoCapture(index)
        if not cap.isOpened():
            raise RuntimeError("I couldn't open the camera — is another app or Jarvis' camera view using it?")
        try:                                            # HD for the full-screen picture; the pose model only ever sees a small copy
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            for z in (100, 0):                          # widest view: some webcams start zoomed in
                try:
                    if cap.set(cv2.CAP_PROP_ZOOM, z):
                        break
                except Exception:
                    break
        except Exception:
            pass
        self.cap = cap

    def read(self):
        return self.cap.read()

    def release(self):
        try:
            self.cap.release()
        except Exception:                               # the driver may already be gone ("Unknown C++ exception")
            pass


def _open_source(player, index: int):
    fn = getattr(player, "shared_camera", None)
    if fn is not None and not index:
        try:
            hub = fn()
            if hub is not None:
                return _HubSource(hub)
        except Exception:
            pass
    return _OwnSource(index)


# ── the workout ─────────────────────────────────────────────────────────────────────────────────────────────────────────
class Session:
    """One workout: an exercise, optionally several sets with rests in between, and a personal-record check."""

    def __init__(self, player, speak, exercise, per_set, sets, rest_s, camera, max_minutes, announce):
        self.player, self.speak = player, speak
        self.exercise = exercise
        self.spec = SPECS[exercise]
        self.target = per_set                        # per set: reps (plank: seconds), or None = until you stop
        self.sets, self.rest_s = sets, rest_s
        self.camera, self.announce = camera, announce
        self.max_seconds = max_minutes * 60
        self.counter = _make_counter(exercise)
        self.stop_flag = threading.Event()
        self.skip_rest_flag = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="workout-counter")
        self.started = time.time()
        self.error = ""
        self.finished = False
        self.saved = None
        # progress (read by the tool's status action)
        self.phase = "active"                        # active | rest
        self.set_no = 1
        self.rest_until = 0.0
        self.set_base = 0
        self.good_base = 0
        self.set_t0 = self.started
        self.sets_done: list[dict] = []
        self._set_recorded = False
        # records
        sess = ws.load_sessions()
        self.prev_best = ws.best_before(exercise, sess)
        self.record_hit = False
        # context for the side panels: today's total so far, the daily goal and the streak
        today = datetime.now().strftime("%Y-%m-%d")
        plank = self.spec.unit == "seconds"
        self.today_base = sum(s["value"] for s in sess if str(s["date"]) == today and (s["exercise"] == "plank") == plank)
        self.goal = ws.DAILY_GOAL_PLANK if plank else ws.DAILY_GOAL_REPS
        self.streak_base = ws.streak(sessions=sess)
        self.had_today = any(str(s["date"]) == today and s["value"] > 0 for s in sess)

    # ── small helpers ──────────────────────────────────────────────────────────────────────────────────────────────
    def _say(self, text: str):
        if self.announce and self.speak:
            try:
                self.speak(text)
            except Exception:
                pass

    def _hud(self) -> bool:
        fn = getattr(self.player, "pushup_hud_active", None)
        try:
            return bool(fn()) if fn else False
        except Exception:
            return False

    def _set_value(self) -> int:
        return max(0, self.counter.reps - self.set_base)

    def _push(self, cv2, frame):
        fn = getattr(self.player, "show_media_frame", None)
        if fn is None:
            return
        try:
            h, w = frame.shape[:2]
            if w > 1280:
                frame = cv2.resize(frame, (1280, int(h * 1280 / w)), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 82])
            if ok:
                fn(buf.tobytes(), self.spec.title)
        except Exception:
            pass

    def _stats(self, elapsed: float) -> dict:
        c = self.counter
        resting = self.phase == "rest"
        left = max(0.0, self.rest_until - time.time()) if resting else 0.0
        return {"exercise": self.exercise, "label": self.spec.label, "unit": self.spec.unit,
                "mode": "rest" if resting else ("time" if self.spec.unit == "seconds" else "reps"),
                "value": self._set_value(), "target": self.target, "set": self.set_no, "sets": self.sets,
                "total": c.reps, "good": max(0, c.good - self.good_base), "elapsed": elapsed,
                "rest_left": left, "rest_total": self.rest_s, "ok": bool(c.position_ok),
                "feedback": (f"Rest — next: set {self.set_no + 1} of {self.sets}" if resting else c.feedback),
                "record": self.record_hit,
                # side panels
                "angle": round(float(getattr(c, "align" if self.spec.unit == "seconds" else "angle", 0.0))),
                "rep_log": [[1 if r.get("good") else 0, float(r.get("seconds", 0)), (r.get("issues") or [""])[0]]
                            for r in c.rep_log[-16:]],
                "sets_done": [[x["n"], x["value"], x["good"]] for x in self.sets_done[-6:]],
                "best": self.prev_best, "today": self.today_base + c.reps, "goal": self.goal,
                "streak": self.streak_base + (0 if self.had_today or c.reps <= 0 else 1),
                "best_streak": round(float(getattr(c, "best_streak", 0.0))), "breaks": int(getattr(c, "breaks", 0))}

    def _push_stats(self, elapsed: float):
        fn = getattr(self.player, "show_pushup_stats", None)
        if fn is None:
            return
        try:
            fn(self._stats(elapsed))
        except Exception:
            pass

    # ── sets, rest, records ────────────────────────────────────────────────────────────────────────────────────────
    def _complete_set(self, now: float) -> bool:
        """The set's target is reached. True = that was the last set (the workout is over)."""
        v = self._set_value()
        self.sets_done.append({"n": self.set_no, "value": v, "good": max(0, self.counter.good - self.good_base),
                               "seconds": round(now - self.set_t0)})
        self._set_recorded = True
        if self.set_no >= self.sets:
            return True
        self.phase = "rest"
        self.rest_until = now + self.rest_s
        self.counter.reset_state()
        unit = self.spec.noun
        log(f"[Workout] Set {self.set_no}/{self.sets} done: {v} {unit}. Rest {self.rest_s} s", self.player)
        self._say(f"[Workout] Set {self.set_no} of {self.sets} is done: {v} {unit}. Tell the user in one short upbeat sentence "
                  f"to rest for {self.rest_s} seconds, then set {self.set_no + 1} starts.")
        return False

    def _next_set(self, now: float):
        self.phase = "active"
        self.set_no += 1
        self.set_base = self.counter.reps
        self.good_base = self.counter.good
        self.set_t0 = now
        self._set_recorded = False
        self.counter.reset_state()
        self.skip_rest_flag.clear()
        log(f"[Workout] Set {self.set_no}/{self.sets} — go", self.player)
        self._say(f"[Workout] Rest is over. Set {self.set_no} of {self.sets} starts now. Say 'go' with one short motivating phrase.")

    def _check_record(self):
        total = self.counter.reps
        if self.record_hit or self.prev_best <= 0 or total <= self.prev_best:
            return
        self.record_hit = True
        big = ws.fmt_value(self.exercise, total)
        log(f"[Workout] 🏆 New record: {big} (was {ws.fmt_value(self.exercise, self.prev_best)})", self.player)
        fn = getattr(self.player, "celebrate", None)
        if fn:
            try:
                fn("NEW RECORD", big, f"{self.spec.noun.upper()}  ·  old best {ws.fmt_value(self.exercise, self.prev_best)}")
            except Exception:
                pass
        self._say(f"[Workout] NEW PERSONAL RECORD: {big} {self.spec.noun}! The previous best was "
                  f"{ws.fmt_value(self.exercise, self.prev_best)}. Celebrate in one short, excited sentence.")

    # ── main loop ──────────────────────────────────────────────────────────────────────────────────────────────────
    def _run(self):
        pose = None
        cap = None
        try:
            import cv2
            pose = _Pose()
            cap = _open_source(self.player, self.camera)
            last_push, last_warn, last_key = 0.0, 0.0, None
            last_breaks = 0
            while not self.stop_flag.is_set():
                t0 = time.time()
                ok, frame = cap.read()
                if not ok:
                    time.sleep(0.05)
                    continue
                t0 = time.time()
                elapsed = t0 - self.started
                if elapsed > self.max_seconds:
                    break
                h, w = frame.shape[:2]
                hud = self._hud()

                if self.phase == "rest":
                    left = self.rest_until - t0
                    if left <= 0 or self.skip_rest_flag.is_set():
                        self._next_set(t0)
                    elif t0 - last_push > 0.07:
                        last_push = t0
                        _draw_rest(cv2, frame, left, self.set_no + 1, self.sets, bars=not hud)
                        self._push(cv2, frame)
                        self._push_stats(elapsed)
                        time.sleep(max(0.0, 0.05 - (time.time() - t0)))
                        continue
                    else:
                        time.sleep(0.02)
                        continue

                small = frame if w <= 640 else cv2.resize(frame, (640, max(1, int(h * 640 / w))), interpolation=cv2.INTER_AREA)
                lm = pose.detect(cv2.cvtColor(small, cv2.COLOR_BGR2RGB))
                ev = None
                if lm:
                    ev = self.counter.update(lm, aspect=w / h)
                else:
                    self.counter.position_ok = False
                    self.counter.feedback = "I can't see you — move into the frame"
                    self.counter.miss(t0)
                if ev:
                    log(f"[Workout] {ev}", self.player)
                self._check_record()

                v = self._set_value()
                if self.target and v >= self.target:
                    if self._complete_set(t0):
                        break
                    continue                       # the rest starts: no more speech for this set
                key = (self.set_no, v)
                if v and key != last_key and v % self.spec.milestone == 0:
                    last_key = key
                    self._say(f"[Workout] The user has done {v} {self.spec.noun}. Say only the number, like an energetic coach, one short phrase.")
                elif ev and self.spec.unit == "reps" and self.counter.rep_log and not self.counter.rep_log[-1]["good"] \
                        and t0 - last_warn > 8:
                    last_warn = t0
                    self._say(f"[Workout] {self.spec.label.title()} {self.counter.reps} form issue: {ev}. Give a very short spoken form tip.")
                elif self.spec.unit == "seconds" and getattr(self.counter, "breaks", 0) > last_breaks:
                    last_breaks = self.counter.breaks
                    if t0 - last_warn > 8:
                        last_warn = t0
                        self._say(f"[Workout] Plank form broke: {self.counter.feedback}. Give a very short spoken tip.")

                if t0 - last_push > 0.07:
                    last_push = t0
                    _draw_skeleton(cv2, frame, lm, self.counter)
                    if not hud:
                        _draw_bars(cv2, frame, self.counter, self.spec, elapsed, v, self.target, self.set_no, self.sets)
                    self._push(cv2, frame)
                    self._push_stats(elapsed)
                time.sleep(max(0.0, 0.033 - (time.time() - t0)))
        except Exception as e:
            self.error = str(e)
            log(f"[Workout] ❌ {e}", self.player)
        finally:
            try:
                if cap is not None:
                    cap.release()
                if pose is not None:
                    pose.close()
            except Exception:
                pass
            self._finish()

    def _finish(self):
        dur = time.time() - self.started
        c = self.counter
        s = c.summary()
        self.finished = True
        fn = getattr(self.player, "show_media_frame", None)
        if fn:
            try:
                fn(b"", "")
            except Exception:
                pass
        if not self._set_recorded and self._set_value() > 0:      # stopped in the middle of a set: keep what was done
            self.sets_done.append({"n": self.set_no, "value": self._set_value(),
                                   "good": max(0, c.good - self.good_base), "seconds": round(time.time() - self.set_t0)})
        value = int(s["reps"])
        enough = value >= (3 if self.spec.unit == "seconds" else 1)
        if enough:
            rec = {"id": uuid.uuid4().hex[:8], "date": datetime.now().strftime("%Y-%m-%d"),
                   "start": datetime.fromtimestamp(self.started).strftime("%H:%M"),
                   "duration_s": round(dur), "exercise": self.exercise, "unit": self.spec.unit, "value": value,
                   **s, "sets": self.sets_done,
                   "plan": {"sets": self.sets, "per_set": self.target, "rest_s": self.rest_s if self.sets > 1 else 0},
                   "record": bool(self.record_hit), "prev_best": self.prev_best,
                   "rep_details": c.rep_log}
            data = json_load(STORE, {"sessions": []})
            data.setdefault("sessions", []).append(rec)
            json_save(STORE, data)
            self.saved = rec
            msg = _summary_text(rec)
            log(f"[Workout] Session saved — {msg}", self.player)
            if self.player is not None and hasattr(self.player, "show_content"):
                try:
                    self.player.show_content("WORKOUT COMPLETE", msg)
                except Exception:
                    pass
            extra = " It is a NEW PERSONAL RECORD — celebrate it." if self.record_hit else ""
            self._say(f"[Workout] Session finished. {msg}{extra} Congratulate the user in one or two short sentences.")
        elif self.error:
            self._say(f"[Workout] The workout counter could not run: {self.error}. Tell the user briefly.")
        else:
            log("[Workout] Session ended with nothing counted.", self.player)


_DEPTH = {"pushup": "elbow", "squat": "knee", "situp": "hip", "pullup": "elbow"}


def _summary_text(rec: dict) -> str:
    ex = rec.get("exercise") or "pushup"
    spec = SPECS.get(ex, SPECS["pushup"])
    mins = rec["duration_s"] / 60
    value = int(rec.get("value", rec.get("reps", 0)))
    if spec.unit == "seconds":
        s = f"Plank held {_mmss(value)} in {mins:.1f} min"
        if rec.get("best_streak"):
            s += f", longest hold {_mmss(rec['best_streak'])}"
    else:
        s = f"{value} {spec.noun} in {mins:.1f} min, {rec.get('good_reps', 0)} with clean form"
        depth = rec.get("avg_depth") or rec.get("avg_elbow_depth")
        if depth:
            s += f", average {_DEPTH.get(ex, 'joint')} angle {depth}°"
    sets = rec.get("sets") or []
    if len(sets) > 1:
        vals = [(_mmss(x["value"]) if spec.unit == "seconds" else str(x["value"])) for x in sets]
        s += f". {len(sets)} sets: " + ", ".join(vals)
    if rec.get("issues"):
        s += ". Main issues: " + ", ".join(f"{k} ×{v}" for k, v in rec["issues"].items())
    if rec.get("record"):
        s += ". 🏆 New personal record"
    return s + "."


def _history(days: int, exercise: str | None = None) -> str:
    sessions = ws.load_sessions()
    if exercise:
        sessions = [s for s in sessions if s["exercise"] == exercise]
    if not sessions:
        return "No workouts logged yet, sir."
    cutoff = datetime.now().timestamp() - days * 86400
    recent = []
    for s in sessions:
        try:
            if datetime.strptime(str(s["date"]), "%Y-%m-%d").timestamp() >= cutoff:
                recent.append(s)
        except ValueError:
            continue
    if not recent:
        return f"No workouts in the last {days} days, sir."
    lines = [f"Last {days} days: {len(recent)} sessions."]
    for ex in ws.EXERCISES:
        part = [s for s in recent if s["exercise"] == ex]
        if not part:
            continue
        total = sum(s["value"] for s in part)
        best = max(part, key=lambda s: s["value"])
        lines.append(f"{ws.NAMES[ex]}: {len(part)} sessions, total {ws.fmt_value(ex, total)}"
                     f"{' s' if ws.UNITS[ex] == 'seconds' else ''}, best {ws.fmt_value(ex, best['value'])} on {best['date']}")
    rec = ws.records(sessions)
    if rec:
        lines.append("Records: " + ", ".join(f"{ws.NAMES[e]} {ws.fmt_value(e, r['value'])}" for e, r in rec.items()))
    lines.append(f"Streak: {ws.streak(sessions=sessions)} days in a row.")
    by_day: dict[str, int] = {}
    for r in ws.week_series(7, sessions=sessions):
        if r["total"]:
            by_day[r["date"]] = r["total"]
    lines += [f"{d}: {n} reps" for d, n in by_day.items()]
    return "\n".join(lines)


def stop_session() -> None:
    """Ask a running workout to stop (does not wait): used when the user closes the workout view."""
    s = _SESSION
    if s is not None and not s.finished:
        s.stop_flag.set()


def _int(v, default, lo, hi):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def pushup_counter(parameters: dict, player=None, speak=None) -> str:
    global _SESSION
    p = parameters or {}
    action = str(p.get("action") or "start").lower().strip()

    if action in ("history", "stats", "records", "chart", "progress"):
        ex = normalize(p["exercise"]) if p.get("exercise") else None
        text = _history(int(p.get("days") or 30), ex)
        if player is not None and hasattr(player, "show_content") and "\n" in text:
            player.show_content("WORKOUT HISTORY", text)
        return text

    with _LOCK:
        if action == "start":
            if _SESSION is not None and not _SESSION.finished:
                return "A workout is already running, sir."
            try:
                import cv2
                import mediapipe
            except ImportError as e:
                return f"Sir, I need extra packages for the workout counter: pip install opencv-python mediapipe ({e.name} is missing)."
            ex = normalize(p.get("exercise"))
            spec = SPECS[ex]
            sets = _int(p.get("sets"), 1, 1, 20)
            raw_target = p.get("target_seconds") if spec.unit == "seconds" else None
            raw_target = raw_target if raw_target else p.get("target_reps")
            per_set = _int(raw_target, 0, 1, 5000) or None
            rest = _int(p.get("rest_seconds"), 60, 5, 600)
            default_min = 15 if sets == 1 else max(15, int(sets * (3 + rest / 60)) + 5)
            max_min = _int(p.get("max_minutes"), default_min, 1, 120)
            _SESSION = Session(player, speak, ex, per_set, sets, rest, int(p.get("camera") or 0), max_min,
                               bool(p.get("announce", True)))
            fs = p.get("fullscreen", True)
            fs = fs if isinstance(fs, bool) else str(fs).lower() not in ("false", "0", "no", "off")
            if fs and player is not None and hasattr(player, "pushup_fullscreen"):
                try:
                    player.pushup_fullscreen(True)         # opens full screen with the big counter as soon as the first frame arrives
                except Exception:
                    pass
            _SESSION.thread.start()
            log(f"[Workout] {spec.label.title()} started", player)
            plan = ""
            if per_set:
                unit = "seconds" if spec.unit == "seconds" else "reps"
                plan = f" Plan: {sets} set{'s' if sets > 1 else ''} of {per_set} {unit}" + (f", {rest} s rest between sets." if sets > 1 else ".")
            elif sets > 1:
                plan = f" Plan: {sets} sets (say 'next set' when one is done), {rest} s rest."
            return (f"{spec.label.title()} counter is live, sir. {spec.hint}. Say 'stop' when you're done." + plan)

        if action in ("stop", "finish", "end"):
            if _SESSION is None or _SESSION.finished:
                return "No workout is running, sir."
            _SESSION.stop_flag.set()
            _SESSION.thread.join(timeout=4)
            sess = _SESSION
            s = sess.counter.summary()
            if not s["reps"]:
                return "Workout stopped — nothing was counted."
            rec = sess.saved or {"exercise": sess.exercise, "value": s["reps"], **s, "duration_s": time.time() - sess.started,
                                 "sets": sess.sets_done, "record": sess.record_hit}
            return f"Workout stopped. {_summary_text(rec)}"

        if action in ("skip_rest", "next", "next_set", "skip"):
            if _SESSION is None or _SESSION.finished:
                return "No workout is running, sir."
            if _SESSION.phase != "rest":
                return "You're not resting right now, sir."
            _SESSION.skip_rest_flag.set()
            return f"Skipping the rest — set {_SESSION.set_no + 1} starts now."

        if action in ("status", "count"):
            if _SESSION is None or _SESSION.finished:
                return "No workout is running, sir."
            c, sp = _SESSION.counter, _SESSION.spec
            if _SESSION.phase == "rest":
                return (f"Resting: {max(0, int(_SESSION.rest_until - time.time()))} s left, then set "
                        f"{_SESSION.set_no + 1} of {_SESSION.sets}.")
            if sp.unit == "seconds":
                return f"Plank: {_mmss(c.reps)} held so far (set {_SESSION.set_no}/{_SESSION.sets}). {c.feedback}."
            return (f"{_SESSION._set_value()} {sp.noun} in this set ({c.reps} in total, {c.good} clean), "
                    f"set {_SESSION.set_no}/{_SESSION.sets}. {c.feedback}.")

    return "Unknown action. Available: start, stop, status, skip_rest, history."


TOOL = {
    "name": "pushup_counter",
    "description": (
        "Live workout coach using the webcam and AI pose detection: counts reps in real time, checks form and gives spoken "
        "feedback. Exercises: push-ups (default), squats, sit-ups, pull-ups and plank (holds are timed in seconds). "
        "Workout plans: 'today 3 sets of 15' → sets=3 and target_reps=15 (rest between sets is counted down automatically; "
        "rest_seconds sets it, default 60). Plank: target_seconds per set. The view opens full screen with a big counter and "
        "celebrates a new personal record. "
        "Actions: start, stop, status, skip_rest (end the rest early), history (weekly totals, records, streak). "
        "Use when the user wants to do or count push-ups / squats / sit-ups / pull-ups / plank, plan sets, or asks about workout "
        "progress and records. Side view works best (pull-ups: face the camera)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "start | stop | status | skip_rest | history (default start)"},
            "exercise": {"type": "STRING", "description": "pushup (default) | squat | situp | pullup | plank"},
            "target_reps": {"type": "INTEGER", "description": "Reps PER SET (stops that set when reached; with sets=1 the workout ends)"},
            "target_seconds": {"type": "INTEGER", "description": "Plank: seconds to hold PER SET"},
            "sets": {"type": "INTEGER", "description": "Number of sets (default 1); with several sets Jarvis counts down the rest"},
            "rest_seconds": {"type": "INTEGER", "description": "Rest between sets in seconds (default 60)"},
            "max_minutes": {"type": "INTEGER", "description": "Safety time limit in minutes (default 15, longer for multi-set plans)"},
            "camera": {"type": "INTEGER", "description": "Camera index (default 0)"},
            "fullscreen": {"type": "BOOLEAN", "description": "Open the workout full screen with the big counter in the top-left corner (default true; Esc or 'make it smaller' leaves it)"},
            "announce": {"type": "BOOLEAN", "description": "Speak milestones and form tips (default true)"},
            "days": {"type": "INTEGER", "description": "History window in days (default 30)"},
        },
    },
    "handler": pushup_counter,
}

#by ASDcuber521

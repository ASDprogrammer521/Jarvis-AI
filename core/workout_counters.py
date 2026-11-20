"""Exercise counters for the workout coach (squat, sit-up, pull-up, plank).

Pure Python on top of MediaPipe pose landmarks — no camera, no Qt, so every counter can be tested with made-up skeletons.
Push-ups keep their own counter in actions/pushup_counter.py (RepCounter); it follows the same interface:

    counter.update(lm, aspect, now) -> str | None   a sentence when a rep finishes (or a plank milestone), else None
    counter.reps / counter.good                     reps so far (plank: whole seconds held) / clean ones
    counter.feedback, counter.position_ok           what to show right now
    counter.unit                                    "reps" | "seconds"
    counter.chains(lm)                              [(landmark indices, "good" | "warn" | "dim"), …] — the skeleton to draw
    counter.reset_state()                           forget the current movement (between sets) but keep the totals
    counter.summary()                               dict saved in the workout log

`lm` is a list of (x, y, visibility) with x, y in 0..1 (image coordinates: y grows downwards).
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass

NOSE = 0
L_SH, R_SH, L_EL, R_EL, L_WR, R_WR, L_HIP, R_HIP, L_KN, R_KN, L_AN, R_AN = 11, 12, 13, 14, 15, 16, 23, 24, 25, 26, 27, 28
MIN_VIS = 0.45
MIN_REP_SECONDS = 0.6


def angle(a, b, c) -> float:
    v1 = (a[0] - b[0], a[1] - b[1])
    v2 = (c[0] - b[0], c[1] - b[1])
    n = math.hypot(*v1) * math.hypot(*v2)
    if n < 1e-9:
        return 180.0
    cos = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / n))
    return math.degrees(math.acos(cos))


@dataclass(frozen=True)
class Spec:
    key: str
    label: str            # shown in the HUD, spoken by the coach
    title: str            # the media-panel title (the window recognises workout views by it)
    unit: str             # "reps" | "seconds"
    noun: str             # "squats"
    hint: str             # what to do first
    milestone: int        # the coach says the number every N reps (plank: every N seconds)


SPECS: dict[str, Spec] = {
    "pushup": Spec("pushup", "PUSH-UPS", "PUSH-UPS", "reps", "push-ups", "Get into push-up position (side view)", 5),
    "squat": Spec("squat", "SQUATS", "SQUATS", "reps", "squats", "Stand side-on to the camera, whole body in view", 5),
    "situp": Spec("situp", "SIT-UPS", "SIT-UPS", "reps", "sit-ups", "Lie on your back, knees bent, side-on to the camera", 5),
    "pullup": Spec("pullup", "PULL-UPS", "PULL-UPS", "reps", "pull-ups", "Grab the bar — hang with your arms straight", 2),
    "plank": Spec("plank", "PLANK", "PLANK", "seconds", "seconds of plank", "Get into plank position (side view)", 15),
}

_ALIASES = (
    ("pushup", ("push", "otjim", "отжим", "otzhim", "yotib", "qo'l", "qol")),
    ("squat", ("squat", "sitting", "присед", "o'tirib", "otirib", "tiz")),
    ("situp", ("sit-up", "situp", "sit up", "crunch", "пресс", "качат", "qorin", "abs")),
    ("pullup", ("pull", "chin", "турник", "подтяг", "turnik", "tortil", "tortish")),
    ("plank", ("plank", "планк", "planka")),
)


def normalize(name) -> str:
    """'push-ups' / 'Squats' / 'планка' / 'turnik' → one of SPECS' keys (default: pushup)."""
    t = str(name or "").lower().strip()
    if t in SPECS:
        return t
    for key, words in _ALIASES:
        if any(w in t for w in words):
            return key
    return "pushup"


class _Base:
    key = ""
    unit = "reps"
    GROUPS: dict[str, tuple] = {}

    def __init__(self):
        self.reps = 0
        self.good = 0
        self.state = "idle"
        self.position_ok = False
        self.angle = 180.0
        self.align = 180.0
        self.feedback = SPECS[self.key].hint
        self.rep_log: list[dict] = []
        self.issues: dict[str, int] = {}
        self.last_event = ""
        self._smooth: deque = deque(maxlen=3)
        self._t0 = 0.0
        self._last_rep_t = 0.0

    # ── helpers ──────────────────────────────────────────────────────────────────────────────────────────────────
    def _side(self, lm):
        best, score = None, 0.0
        for name, idx in self.GROUPS.items():
            vis = sum(lm[i][2] for i in idx) / len(idx)
            if vis > score:
                best, score = name, vis
        return best if score >= MIN_VIS else None

    def _pts(self, lm, side, aspect):
        return [(lm[i][0] * aspect, lm[i][1]) for i in self.GROUPS[side]]

    def _count(self, now, dur, problems, fb_good, tips, extra: dict) -> str:
        self.reps += 1
        self._last_rep_t = now
        good = not problems
        if good:
            self.good += 1
        for p in problems:
            self.issues[p] = self.issues.get(p, 0) + 1
        self.rep_log.append({"n": self.reps, "seconds": round(dur, 2), "good": good, "issues": list(problems), **extra})
        self.feedback = fb_good.format(n=self.reps) if good else f"Rep {self.reps} — " + ", ".join(tips.get(x, x) for x in problems)
        self.last_event = self.feedback
        return self.feedback

    def reset_state(self):
        self.state = "idle"
        self._smooth.clear()
        self.feedback = SPECS[self.key].hint

    def chains(self, lm):
        return []

    def miss(self, now: float):
        """The body is not in the picture this frame (the plank clock must not keep running)."""

    def summary(self) -> dict:
        n = len(self.rep_log)
        return {"reps": self.reps, "good_reps": self.good,
                "avg_depth": round(sum(r.get("min_angle", 0) for r in self.rep_log) / n) if n else 0,
                "avg_rep_seconds": round(sum(r["seconds"] for r in self.rep_log) / n, 2) if n else 0,
                "issues": {k: v for k, v in self.issues.items() if v}}


# ══ squat ═════════════════════════════════════════════════════════════════════════════════════════════════════════════
class SquatCounter(_Base):
    """Knee angle (hip–knee–ankle), seen from the side. Down below 120°, up again above 158°; depth ≤ 105° counts as clean."""
    key = "squat"
    GROUPS = {"L": (L_SH, L_HIP, L_KN, L_AN), "R": (R_SH, R_HIP, R_KN, R_AN)}
    DOWN, UP, DEEP_OK, MAX_LEAN = 120.0, 158.0, 105.0, 68.0

    def __init__(self):
        super().__init__()
        self._min = 180.0
        self._lean = 0.0
        self._side_name = "L"

    def update(self, lm, aspect: float = 1.0, now: float | None = None):
        now = time.time() if now is None else now
        side = self._side(lm)
        if side is None:
            self.position_ok = False
            self.feedback = "I can't see you clearly — stand side-on, whole body in view"
            return None
        self._side_name = side
        sh, hip, kn, an = self._pts(lm, side, aspect)
        self._smooth.append(angle(hip, kn, an))
        self.angle = sum(self._smooth) / len(self._smooth)
        self.align = angle(sh, hip, kn)
        self.position_ok = (an[1] - sh[1]) > 0.22
        lean = math.degrees(math.atan2(abs(sh[0] - hip[0]), max(hip[1] - sh[1], 1e-6)))
        if not self.position_ok:
            self.state = "idle" if self.state == "down" else self.state
            self.feedback = SPECS["squat"].hint
            return None
        event = None
        if self.state in ("idle", "up"):
            if self.angle >= self.UP:
                self.state = "up"
                self.feedback = "Ready — squat down"
            if self.state == "up" and self.angle < self.DOWN:
                self.state, self._t0, self._min, self._lean = "down", now, self.angle, lean
                self.feedback = "Good — now stand up"
        elif self.state == "down":
            self._min = min(self._min, self.angle)
            self._lean = max(self._lean, lean) if self.angle < 130 else self._lean
            if self.angle >= self.UP:
                dur = now - self._t0
                if dur >= MIN_REP_SECONDS and now - self._last_rep_t >= MIN_REP_SECONDS:
                    problems = []
                    if self._min > self.DEEP_OK:
                        problems.append("shallow")
                    if self._lean > self.MAX_LEAN:
                        problems.append("leaning forward")
                    event = self._count(now, dur, problems, "Rep {n} — good depth, clean form",
                                        {"shallow": "go lower", "leaning forward": "chest up, keep your back straighter"},
                                        {"min_angle": round(self._min), "lean": round(self._lean)})
                self.state = "up"
        return event

    def chains(self, lm):
        return [(list(self.GROUPS[self._side_name]), "good" if self.position_ok else "dim")]


# ══ sit-up ════════════════════════════════════════════════════════════════════════════════════════════════════════════
class SitupCounter(_Base):
    """Hip angle (shoulder–hip–knee), lying side-on. Lying ≥130°, up when it closes below 100° (≤80° = a full sit-up)."""
    key = "situp"
    GROUPS = {"L": (L_SH, L_HIP, L_KN), "R": (R_SH, R_HIP, R_KN)}
    LYING, UP, FULL = 130.0, 100.0, 82.0

    def __init__(self):
        super().__init__()
        self._min = 180.0
        self._side_name = "L"
        self._leave: float | None = None          # when the back left the floor

    def reset_state(self):
        super().reset_state()
        self._leave = None

    def update(self, lm, aspect: float = 1.0, now: float | None = None):
        now = time.time() if now is None else now
        side = self._side(lm)
        if side is None:
            self.position_ok = False
            self.feedback = "I can't see you clearly — lie side-on to the camera"
            return None
        self._side_name = side
        sh, hip, kn = self._pts(lm, side, aspect)
        self._smooth.append(angle(sh, hip, kn))
        self.angle = sum(self._smooth) / len(self._smooth)
        self.position_ok = self.state in ("lying", "up") or self.angle >= self.LYING
        event = None
        if self.state in ("idle", "lying"):
            if self.angle >= self.LYING:
                self.state = "lying"
                self.feedback = "Ready — sit up"
            elif self.state == "idle":
                self.feedback = SPECS["situp"].hint
            if self.state == "lying":
                if self.angle < self.LYING and self._leave is None:
                    self._leave = now
                elif self.angle >= self.LYING:
                    self._leave = None
            if self.state == "lying" and self.angle < self.UP:
                self.state, self._t0, self._min = "up", (self._leave or now), self.angle
                self.feedback = "Up — now lower slowly"
        elif self.state == "up":
            self._min = min(self._min, self.angle)
            if self.angle >= self.LYING:
                dur = now - self._t0
                if dur >= 0.7 and now - self._last_rep_t >= 0.7:
                    problems = []
                    if self._min > self.FULL:
                        problems.append("not high enough")
                    if dur < 0.9:
                        problems.append("too fast")
                    self._leave = None
                    event = self._count(now, dur, problems, "Rep {n} — full sit-up, clean form",
                                        {"not high enough": "come all the way up", "too fast": "slow down, control it"},
                                        {"min_angle": round(self._min)})
                self.state = "lying"
        return event

    def chains(self, lm):
        return [(list(self.GROUPS[self._side_name]), "good" if self.position_ok else "dim")]


# ══ pull-up ═══════════════════════════════════════════════════════════════════════════════════════════════════════════
class PullupCounter(_Base):
    """Elbow angle of the visible arms; hands must be above the shoulders (on the bar). Works from the front or the side.
    A rep needs a full hang (≥150°) before and after; clean = chin over the bar."""
    key = "pullup"
    ARMS = {"L": (L_SH, L_EL, L_WR), "R": (R_SH, R_EL, R_WR)}
    HANG, PULL = 150.0, 120.0

    def __init__(self):
        super().__init__()
        self._min = 180.0
        self._chin = False
        self._arms: list[str] = []

    def update(self, lm, aspect: float = 1.0, now: float | None = None):
        now = time.time() if now is None else now
        arms = [n for n, idx in self.ARMS.items() if sum(lm[i][2] for i in idx) / 3 >= MIN_VIS]
        self._arms = arms
        if not arms:
            self.position_ok = False
            self.feedback = "I can't see your arms — face the camera, whole upper body in view"
            return None
        angs, wy, sy = [], [], []
        for n in arms:
            sh, el, wr = [(lm[i][0] * aspect, lm[i][1]) for i in self.ARMS[n]]
            angs.append(angle(sh, el, wr))
            wy.append(wr[1])
            sy.append(sh[1])
        self._smooth.append(sum(angs) / len(angs))
        self.angle = sum(self._smooth) / len(self._smooth)
        bar = sum(wy) / len(wy)
        shy = sum(sy) / len(sy)
        self.position_ok = bar < shy - 0.04                       # hands above the shoulders = hanging on a bar
        chin = lm[NOSE][2] >= MIN_VIS and lm[NOSE][1] <= bar + 0.01
        if not self.position_ok:
            self.state = "idle"
            self.feedback = SPECS["pullup"].hint
            return None
        event = None
        if self.state == "idle":
            if self.angle >= self.HANG:
                self.state = "hang"
                self.feedback = "Ready — pull up"
        elif self.state == "hang":
            if self.angle < self.PULL:
                self.state, self._t0, self._min, self._chin = "pull", now, self.angle, chin
                self.feedback = "Pull — chin over the bar"
        elif self.state == "pull":
            self._min = min(self._min, self.angle)
            self._chin = self._chin or chin
            if self._chin:
                self.feedback = "Chin over — lower slowly"
            if self.angle >= self.HANG:
                dur = now - self._t0
                if dur >= 0.8 and now - self._last_rep_t >= 0.8 and (self._min <= 95 or self._chin):
                    problems = [] if self._chin else ["chin below bar"]
                    event = self._count(now, dur, problems, "Rep {n} — chin over the bar, clean",
                                        {"chin below bar": "pull higher, chin over the bar"},
                                        {"min_angle": round(self._min)})
                self.state = "hang"
        return event

    def chains(self, lm):
        return [(list(self.ARMS[n]), "good" if self.position_ok else "dim") for n in self._arms]


# ══ plank (time) ══════════════════════════════════════════════════════════════════════════════════════════════════════
class PlankTimer(_Base):
    """Counts the seconds you hold a straight plank (side view). The clock pauses when the hips sag or pike and starts again
    when the body line is straight. `reps` = whole seconds held."""
    key = "plank"
    unit = "seconds"
    GROUPS = {"L": (L_SH, L_EL, L_WR, L_HIP, L_AN), "R": (R_SH, R_EL, R_WR, R_HIP, R_AN)}
    MIN_ALIGN, START_AFTER, GRACE = 158.0, 0.5, 0.6

    def __init__(self):
        super().__init__()
        self.held = 0.0
        self.streak = 0.0
        self.best_streak = 0.0
        self.breaks = 0
        self.running = False
        self._good_since: float | None = None
        self._bad_since: float | None = None
        self._last_t: float | None = None
        self._side_name = "L"
        self._said = 0

    def reset_state(self):
        super().reset_state()
        self.running = False
        self._good_since = self._bad_since = self._last_t = None
        self.streak = 0.0

    def update(self, lm, aspect: float = 1.0, now: float | None = None):
        now = time.time() if now is None else now
        dt = 0.0 if self._last_t is None else min(0.25, max(0.0, now - self._last_t))
        self._last_t = now
        side = self._side(lm)
        if side is None:
            self.position_ok = False
            self.feedback = "I can't see you clearly — get into plank, side-on to the camera"
            self._pause(now)
            return None
        self._side_name = side
        sh, el, wr, hip, an = self._pts(lm, side, aspect)
        self.align = angle(sh, hip, an)
        dx, dy = abs(an[0] - sh[0]), abs(an[1] - sh[1])
        self.position_ok = dx > 0.15 and dy / (dx + 1e-6) < 0.5
        form_ok = self.position_ok and self.align >= self.MIN_ALIGN
        if form_ok:
            self._bad_since = None
            if not self.running:
                self._good_since = now if self._good_since is None else self._good_since
                if now - self._good_since >= self.START_AFTER:
                    self.running = True
                    self.feedback = "Hold it — you're in plank"
                else:
                    self.feedback = "Hold still…"
            else:
                self.held += dt
                self.streak += dt
                self.best_streak = max(self.best_streak, self.streak)
                self.feedback = "Strong — keep holding"
        else:
            self._good_since = None
            if not self.position_ok:
                self.feedback = SPECS["plank"].hint
            else:
                t_line = (hip[0] - sh[0]) / ((an[0] - sh[0]) or 1e-6)
                below = hip[1] > sh[1] + (an[1] - sh[1]) * t_line
                self.feedback = "Tighten your core, hips up" if below else "Lower your hips, straight line"
            self._pause(now)
        self.reps = self.good = int(self.held)
        if self.reps >= 10 and self.reps % 10 == 0 and self.reps != self._said:
            self._said = self.reps
            return f"{self.reps} seconds"
        return None

    def miss(self, now: float):
        self._pause(now)
        self._last_t = None
        self._good_since = None

    def _pause(self, now: float):
        if not self.running:
            return
        if self._bad_since is None:
            self._bad_since = now
        elif now - self._bad_since > self.GRACE:
            self.running = False
            self.breaks += 1
            self.streak = 0.0
            self._bad_since = None

    def chains(self, lm):
        n = self._side_name
        sh, el, wr, hip, an = self.GROUPS[n]
        st = "good" if self.running else ("warn" if self.position_ok else "dim")
        return [([sh, el, wr], st), ([sh, hip, an], st)]

    def summary(self) -> dict:
        return {"reps": int(self.held), "good_reps": int(self.held), "avg_depth": 0, "avg_rep_seconds": 0,
                "best_streak": round(self.best_streak, 1), "breaks": self.breaks,
                "issues": {"form breaks": self.breaks} if self.breaks else {}}


COUNTERS = {"squat": SquatCounter, "situp": SitupCounter, "pullup": PullupCounter, "plank": PlankTimer}

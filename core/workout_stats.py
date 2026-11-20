"""Workout history: weekly chart data, personal records and the streak — read from memory/workouts.json.

Pure Python (no Qt) so the dashboard card, the voice tool and the tests all use the same numbers.
A session record looks like:
    {"date": "2026-11-18", "exercise": "squat", "unit": "reps", "value": 45, "reps": 45, "good_reps": 41,
     "duration_s": 520, "sets": [{"n": 1, "value": 15, "good": 14, "seconds": 40}, …], …}
Old records (push-ups only) have no "exercise" / "value": they count as push-ups with value = reps.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

try:
    from actions._common import DATA_DIR, json_load
    STORE = DATA_DIR / "workouts.json"
except Exception:                                  # noqa: BLE001  (tests / a broken install: an empty history, never a crash)
    STORE = None

    def json_load(_p, default):
        return default

DAILY_GOAL_REPS = 100                              # the ring on the workout side panel (reps per day, all rep exercises)
DAILY_GOAL_PLANK = 180                             # plank seconds per day

EXERCISES = ("pushup", "squat", "situp", "pullup", "plank")
NAMES = {"pushup": "Push-ups", "squat": "Squats", "situp": "Sit-ups", "pullup": "Pull-ups", "plank": "Plank"}
UNITS = {"pushup": "reps", "squat": "reps", "situp": "reps", "pullup": "reps", "plank": "seconds"}


def load_sessions(path=None) -> list[dict]:
    p = path or STORE
    if p is None:
        return []
    data = json_load(p, {"sessions": []})
    out = []
    for s in data.get("sessions", []) if isinstance(data, dict) else []:
        if not isinstance(s, dict) or "date" not in s:
            continue
        ex = s.get("exercise") or "pushup"
        if ex not in EXERCISES:
            ex = "pushup"
        value = s.get("value", s.get("reps", 0))
        try:
            value = int(value or 0)
        except (TypeError, ValueError):
            value = 0
        out.append({**s, "exercise": ex, "unit": UNITS[ex], "value": value})
    return out


def fmt_value(exercise: str, value: int) -> str:
    """32 → '32', plank 125 → '2:05'."""
    if UNITS.get(exercise) == "seconds":
        return f"{int(value) // 60}:{int(value) % 60:02d}"
    return str(int(value))


def records(sessions: list[dict] | None = None) -> dict[str, dict]:
    """Best single session per exercise: {exercise: {"value": 32, "date": "2026-11-18"}} (only exercises done at least once)."""
    best: dict[str, dict] = {}
    for s in sessions if sessions is not None else load_sessions():
        ex, v = s["exercise"], s["value"]
        if v > 0 and (ex not in best or v > best[ex]["value"]):
            best[ex] = {"value": v, "date": s["date"]}
    return best


def best_before(exercise: str, sessions: list[dict] | None = None) -> int:
    """The record to beat for `exercise` (0 = no earlier session, so nothing to celebrate yet)."""
    r = records(sessions).get(exercise)
    return int(r["value"]) if r else 0


def week_series(days: int = 7, today: date | None = None, sessions: list[dict] | None = None) -> list[dict]:
    """Last `days` days, oldest first: [{"date", "label": "Mon", "today": bool, "by": {exercise: reps}, "total": reps, "plank_s": seconds}].
    `total` / `by` count rep exercises only; plank is kept in seconds so the two units never get mixed on one axis."""
    today = today or date.today()
    sess = sessions if sessions is not None else load_sessions()
    rows = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        rows.append({"date": d.isoformat(), "label": d.strftime("%a"), "today": i == 0, "by": {}, "total": 0, "plank_s": 0})
    index = {r["date"]: r for r in rows}
    for s in sess:
        r = index.get(str(s["date"]))
        if r is None:
            continue
        if s["exercise"] == "plank":
            r["plank_s"] += s["value"]
        else:
            r["by"][s["exercise"]] = r["by"].get(s["exercise"], 0) + s["value"]
            r["total"] += s["value"]
    return rows


def streak(today: date | None = None, sessions: list[dict] | None = None) -> int:
    """Days in a row with at least one workout, counting back from today (or from yesterday if today has none yet)."""
    today = today or date.today()
    days = {str(s["date"]) for s in (sessions if sessions is not None else load_sessions()) if s["value"] > 0}
    d = today if today.isoformat() in days else today - timedelta(days=1)
    n = 0
    while d.isoformat() in days:
        n += 1
        d -= timedelta(days=1)
    return n


def week_summary(today: date | None = None, sessions: list[dict] | None = None) -> dict:
    sess = sessions if sessions is not None else load_sessions()
    rows = week_series(7, today, sess)
    dates = {r["date"] for r in rows}
    week = [s for s in sess if str(s["date"]) in dates]
    return {"sessions": len(week), "reps": sum(r["total"] for r in rows), "plank_s": sum(r["plank_s"] for r in rows),
            "active_days": sum(1 for r in rows if r["total"] or r["plank_s"]), "streak": streak(today, sess)}


def mtime() -> float:
    try:
        return STORE.stat().st_mtime if STORE is not None else 0.0
    except OSError:
        return 0.0

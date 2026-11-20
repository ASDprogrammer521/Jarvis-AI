"""Smart nudges: Jarvis tells you useful things without being asked — and stays quiet when you want quiet.

Rules (each one can be switched off and tuned in Settings → Nudges, or by voice: "turn off the battery nudges"):
  meeting   a calendar event starts in N minutes                         (needs Google connected: say "connect Google")
  battery   the battery is at or below N % and the charger is unplugged
  workout   no workout logged today and it is N o'clock or later          (reads memory/workouts.json)
  streak    your workout streak ends tonight (after N o'clock, nothing logged yet)

Every nudge is shown as a toast and, if "voice" is on and Jarvis is not busy talking, said aloud in one short sentence.
It never fires inside quiet hours or while snoozed, never repeats the same nudge, and a failing source (offline calendar,
no battery on a desktop PC) simply produces nothing. Pure Python — no Qt — so it is easy to test: tick(now) returns what it sent.
"""
from __future__ import annotations

import copy
import json
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

DEFAULTS: dict = {
    "enabled": True,
    "voice": True,
    "quiet": {"on": True, "from": "23:00", "to": "07:00"},
    "rules": {
        "meeting": {"on": True, "minutes": 15},
        "battery": {"on": True, "pct": 15},
        "workout": {"on": True, "hour": 18},
        "streak": {"on": True, "hour": 21},
    },
}
RULE_INFO = {
    "meeting": ("Upcoming meeting", "Tells you before a calendar event starts", "minutes", (5, 10, 15, 30), "{} min before"),
    "battery": ("Low battery", "When the laptop battery runs low and it is not charging", "pct", (10, 15, 20, 30), "{} %"),
    "workout": ("Workout reminder", "If you have not worked out today, from this hour on", "hour", (12, 15, 17, 18, 19, 20), "from {}:00"),
    "streak": ("Streak at risk", "Your streak ends tonight if you skip today", "hour", (19, 20, 21, 22), "from {}:00"),
}
ENGINE: "Nudges | None" = None          # the running engine (set by Jarvis at launch; the voice tool uses it to snooze)
TICK_SECONDS = 30
BATTERY_COOLDOWN = 40 * 60
CALENDAR_REFRESH = 5 * 60
VOICE_PATIENCE = 120            # wait this long for Jarvis to stop talking before showing the nudge as a toast only


def merged_cfg(raw) -> dict:
    """The saved `nudges` config laid over the defaults (unknown / bad values fall back to the defaults)."""
    cfg = copy.deepcopy(DEFAULTS)
    if not isinstance(raw, dict):
        return cfg
    for k in ("enabled", "voice"):
        if isinstance(raw.get(k), bool):
            cfg[k] = raw[k]
    q = raw.get("quiet")
    if isinstance(q, dict):
        if isinstance(q.get("on"), bool):
            cfg["quiet"]["on"] = q["on"]
        for k in ("from", "to"):
            if _hm(q.get(k)) is not None:
                cfg["quiet"][k] = q[k]
    rules = raw.get("rules")
    if isinstance(rules, dict):
        for name, base in cfg["rules"].items():
            r = rules.get(name)
            if not isinstance(r, dict):
                continue
            if isinstance(r.get("on"), bool):
                base["on"] = r["on"]
            for k in base:
                if k != "on" and isinstance(r.get(k), (int, float)) and not isinstance(r.get(k), bool):
                    base[k] = int(r[k])
    return cfg


def _hm(text) -> int | None:
    """'23:30' → minutes since midnight (None if it is not a time)."""
    try:
        h, m = str(text).split(":")
        h, m = int(h), int(m)
        return h * 60 + m if 0 <= h < 24 and 0 <= m < 60 else None
    except (ValueError, AttributeError):
        return None


def in_quiet(now: datetime, quiet: dict) -> bool:
    if not quiet.get("on"):
        return False
    a, b = _hm(quiet.get("from")), _hm(quiet.get("to"))
    if a is None or b is None or a == b:
        return False
    t = now.hour * 60 + now.minute
    return (a <= t < b) if a < b else (t >= a or t < b)          # a window that crosses midnight: 23:00 → 07:00


# ── data sources (replaced by fakes in the tests) ──
def battery_now():
    try:
        import psutil
        b = psutil.sensors_battery()
        return (float(b.percent), bool(b.power_plugged)) if b is not None else None
    except Exception:
        return None


def calendar_events(now: datetime) -> list[dict]:
    """Timed events in the next hours, [{"id", "title", "start": aware datetime}] — [] when Google is not connected."""
    try:
        from actions import google_workspace as gw
        if not gw.TOKEN_FILE.exists():                           # never start a sign-in from a background thread
            return []
        today = now.date()
        out = []
        for ev in gw._cal_range(today, today + timedelta(days=2)):
            s = (ev.get("start") or {}).get("dateTime")
            if not s:
                continue
            out.append({"id": str(ev.get("id", s)), "title": str(ev.get("summary") or "your event"),
                        "start": datetime.fromisoformat(s)})
        return out
    except Exception:
        return []


def workout_sessions() -> list[dict]:
    try:
        from core import workout_stats as ws
        return ws.load_sessions()
    except Exception:
        return []


class Nudges:
    def __init__(self, notify, speak=None, busy=None, cfg_fn=None, state_path: Path | None = None,
                 battery_fn=battery_now, events_fn=calendar_events, sessions_fn=workout_sessions):
        """notify(text): show a toast · speak(text): ask Jarvis to say it · busy(): True while Jarvis is talking
        cfg_fn(): the raw `nudges` config dict · state_path: where "already sent" is remembered across restarts."""
        self.notify, self.speak, self.busy = notify, speak, busy or (lambda: False)
        self.cfg_fn = cfg_fn or (lambda: {})
        self.state_path = state_path
        self.battery_fn, self.events_fn, self.sessions_fn = battery_fn, events_fn, sessions_fn
        self._state = self._load()
        self._defer: dict[str, float] = {}
        self._events: list[dict] = []
        self._events_at = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── persistence ──
    def _load(self) -> dict:
        try:
            d = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path else {}
        except Exception:
            d = {}
        d = d if isinstance(d, dict) else {}
        d.setdefault("sent", {})
        d.setdefault("snooze_until", 0.0)
        return d

    def _save(self):
        if self.state_path is None:
            return
        try:
            cut = time.time() - 3 * 86400
            self._state["sent"] = {k: v for k, v in self._state["sent"].items() if v > cut}
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(self._state), encoding="utf-8")
        except Exception:
            pass

    # ── control ──
    def snooze(self, minutes: float) -> float:
        until = time.time() + max(0.0, minutes) * 60
        self._state["snooze_until"] = until
        self._save()
        return until

    def snoozed(self, now_ts: float | None = None) -> bool:
        return (now_ts if now_ts is not None else time.time()) < float(self._state.get("snooze_until", 0.0))

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="jarvis-nudges")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        self._stop.wait(20)                                      # let the app finish starting first
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                pass
            self._stop.wait(TICK_SECONDS)

    # ── the rules ──
    def tick(self, now: datetime | None = None) -> list[tuple[str, str]]:
        """One pass over the rules; returns [(rule, text)] for what was delivered now."""
        now = now or datetime.now()
        cfg = merged_cfg(self.cfg_fn())
        if not cfg["enabled"] or self.snoozed(now.timestamp()) or in_quiet(now, cfg["quiet"]):
            return []
        sent: list[tuple[str, str]] = []
        voiced = False
        for rule, fn in (("meeting", self._meeting), ("battery", self._battery),
                         ("workout", self._workout), ("streak", self._streak)):
            r = cfg["rules"][rule]
            if not r["on"]:
                continue
            try:
                found = fn(now, r)
            except Exception:
                found = None
            if not found:
                continue
            key, text = found
            if self._deliver(rule, key, text, cfg["voice"] and not voiced, now.timestamp()):
                sent.append((rule, text))
                voiced = voiced or cfg["voice"]                  # one spoken nudge per pass; the others are toasts only
        return sent

    def _fresh(self, key: str, cooldown: float | None = None, now_ts: float | None = None) -> bool:
        t = self._state["sent"].get(key)
        if t is None:
            return True
        return cooldown is not None and (now_ts if now_ts is not None else time.time()) - t >= cooldown

    def _meeting(self, now: datetime, r: dict):
        ts = now.timestamp()
        if ts - self._events_at > CALENDAR_REFRESH:
            self._events, self._events_at = self.events_fn(now), ts
        for ev in self._events:
            start = ev["start"] if ev["start"].tzinfo else ev["start"].astimezone()
            mins = (start - now.astimezone()).total_seconds() / 60.0
            if 0 < mins <= r["minutes"]:
                key = f"meeting:{ev['id']}:{start.isoformat()}"
                if self._fresh(key):
                    n = max(1, int(round(mins)))
                    return key, f"📅 “{ev['title']}” starts in {n} min"
        return None

    def _battery(self, now: datetime, r: dict):
        b = self.battery_fn()
        if b is None:
            return None
        pct, plugged = b
        if plugged or pct > r["pct"]:
            if plugged or pct > r["pct"] + 5:
                self._state["sent"].pop("battery", None)         # charged again: the next drop warns again
            return None
        if self._fresh("battery", BATTERY_COOLDOWN, now.timestamp()):
            return "battery", f"🔋 Battery at {int(pct)}% — time to plug in"
        return None

    def _today_total(self, now: datetime) -> tuple[bool, int]:
        sess = self.sessions_fn()
        today = now.date().isoformat()
        done = any(str(s.get("date")) == today and int(s.get("value", 0) or 0) > 0 for s in sess)
        streak = 0
        if sess:
            from core import workout_stats as ws
            streak = ws.streak(now.date(), sess)
        return done, streak

    def _workout(self, now: datetime, r: dict):
        if now.hour < r["hour"] or now.hour >= 22:
            return None
        key = f"workout:{now.date().isoformat()}"
        if not self._fresh(key):
            return None
        done, streak = self._today_total(now)
        if done:
            return None
        extra = f" Keep your {streak}-day streak going!" if streak else ""
        return key, f"💪 No workout yet today — you have time for 20 minutes.{extra}"

    def _streak(self, now: datetime, r: dict):
        if now.hour < r["hour"]:
            return None
        key = f"streak:{now.date().isoformat()}"
        if not self._fresh(key):
            return None
        done, streak = self._today_total(now)
        if done or streak < 2:
            return None
        return key, f"🔥 Your {streak}-day workout streak ends tonight — a quick set saves it"

    # ── delivery ──
    def _deliver(self, rule: str, key: str, text: str, voice: bool, now_ts: float) -> bool:
        if voice and self.speak is not None and self.busy():
            first = self._defer.setdefault(key, now_ts)          # Jarvis is talking: wait a little, then fall back to a toast
            if now_ts - first < VOICE_PATIENCE:
                return False
            voice = False
        self._defer.pop(key, None)
        self._state["sent"][key] = now_ts
        self._save()
        try:
            self.notify(text)
        except Exception:
            pass
        if voice and self.speak is not None:
            try:
                self.speak("[Nudge] You noticed something useful for the user. Tell them, in one short friendly sentence in the "
                           f"language they speak, and nothing more: {text}")
            except Exception:
                pass
        return True

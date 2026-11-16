"""What the Calendar, Mail and Expenses cards of Hand Space show. Everything is built as plain text for the text card.

Runs in a worker thread (these calls wait for the network), never in the window's thread.

Safety:
  * mail is written by strangers: it is shown as plain text only — control characters removed, lengths capped, never
    interpreted as markup and never handed to the assistant as instructions;
  * a message id must look like a Gmail id before it is used; the card can only READ mail (the connected Google permission is
    read-only for Gmail) — it cannot send, delete or archive;
  * a receipt photo must be a picture file from a Hand Space card, at most 15 MB; it is sent to the assistant's receipt
    reader (the same one "scan receipt" already uses) only when you drop it on the Expenses card or say "scan receipt";
  * a failure is turned into one short sentence, never a traceback with file paths.
"""
from __future__ import annotations

import collections
import json
import os
import re
import sys
import time
from urllib.parse import quote
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime

MAX_RECEIPT_BYTES = 15 * 1024 * 1024
RECEIPT_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
_ID = re.compile(r"^[0-9a-fA-F]{8,32}$")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")      # terminal escape sequences (colours, links …)
NOT_CONNECTED = "Google isn't connected yet. Tell Jarvis “connect Google” and follow the steps."


def clean(s, n: int = 200, keep_newlines: bool = False) -> str:
    """Printable text only, whitespace collapsed, at most n characters (mail / calendar text is not to be trusted)."""
    s = _ANSI.sub("", str(s or ""))
    if keep_newlines:
        s = "\n".join(re.sub(r"[ \t\u00a0]+", " ", "".join(ch if ch.isprintable() else " " for ch in line)).strip() for line in s.splitlines())
        s = re.sub(r"\n{3,}", "\n\n", s).strip()
    else:
        s = re.sub(r"\s+", " ", "".join(ch if ch.isprintable() else " " for ch in s)).strip()
    return s[:n]


def _gw(gw=None):
    if gw is not None:
        return gw
    from actions import google_workspace
    return google_workspace


def _fin(fin=None):
    if fin is not None:
        return fin
    from actions import finance_tracker
    return finance_tracker


def _problem(e: Exception, what: str) -> str:
    if isinstance(e, PermissionError):
        return NOT_CONNECTED
    if isinstance(e, ImportError):
        return f"The {what} needs the Google libraries (pip install google-api-python-client google-auth-oauthlib)."
    return f"I couldn't read the {what} just now ({type(e).__name__}). Try again in a moment."


# ── calendar ─────────────────────────────────────────────────────────────────
def _day_label(d: date, today: date) -> str:
    name = "Today" if d == today else "Tomorrow" if d == today + timedelta(days=1) else d.strftime("%A")
    return f"{name}  ·  {d.strftime('%a %d %b')}"


def calendar_text(gw=None, today: date | None = None, days: int = 3) -> str:
    today = today or date.today()
    try:
        g = _gw(gw)
        events = g._cal_range(today, today + timedelta(days=days))
        tz = g._local_tz()
    except Exception as e:                                       # noqa: BLE001
        return _problem(e, "calendar")
    by_day: dict[date, list] = {today + timedelta(days=i): [] for i in range(days)}
    for ev in events if isinstance(events, list) else []:
        try:
            s, e_ = ev.get("start", {}), ev.get("end", {})
            if "dateTime" in s:
                st = datetime.fromisoformat(s["dateTime"]).astimezone(tz)
                en = datetime.fromisoformat(e_.get("dateTime", s["dateTime"])).astimezone(tz)
                when, day, key = f"{st:%H:%M} – {en:%H:%M}", st.date(), st
            else:
                day = date.fromisoformat(s.get("date", ""))
                when, key = "All day", datetime(day.year, day.month, day.day, tzinfo=tz)
            loc = clean(ev.get("location"), 60)
            line = f"{when}   {clean(ev.get('summary'), 90) or '(no title)'}" + (f"   @ {loc}" if loc else "")
            if day in by_day:
                by_day[day].append((key, line))
        except (ValueError, TypeError, AttributeError):
            continue
    out = []
    for d, items in by_day.items():
        out.append(_day_label(d, today))
        out += ["   " + line for _, line in sorted(items, key=lambda t: t[0])] or ["   — nothing planned —"]
        out.append("")
    return "\n".join(out).strip()


# ── mail (read only) ─────────────────────────────────────────────────────────
def _short_date(raw: str) -> str:
    try:
        return parsedate_to_datetime(raw).astimezone().strftime("%d %b %H:%M")
    except (TypeError, ValueError, IndexError):
        return ""


def mail_list(gw=None, limit: int = 12):
    """→ (text, [message ids in the order shown])."""
    try:
        g = _gw(gw)
        gm = g._service("gmail", "v1")
        refs = gm.users().messages().list(userId="me", q="in:inbox", maxResults=limit).execute().get("messages", []) or []
        msgs: dict = {}
        hdrs = ["From", "Subject", "Date"]
        try:                                                      # one round trip for all messages instead of one each
            batch = gm.new_batch_http_request()

            def cb(rid, resp, exc):
                if exc is None and isinstance(resp, dict):
                    msgs[rid] = resp
            for ref in refs:
                batch.add(gm.users().messages().get(userId="me", id=ref["id"], format="metadata", metadataHeaders=hdrs),
                          callback=cb, request_id=ref["id"])
            batch.execute()
        except Exception:                                         # noqa: BLE001  (no batch support: fetch one by one)
            msgs = {}
        for ref in refs:
            if ref["id"] not in msgs:
                msgs[ref["id"]] = gm.users().messages().get(userId="me", id=ref["id"], format="metadata", metadataHeaders=hdrs).execute()
    except Exception as e:                                       # noqa: BLE001
        return _problem(e, "inbox"), []
    if not refs:
        return "Your inbox is empty.", []
    lines, ids = [f"Inbox  ·  {len(refs)} latest   (● = unread)", ""], []
    for i, ref in enumerate(refs, 1):
        m = msgs.get(ref["id"]) or {}
        if not _ID.match(str(ref["id"])):
            continue
        ids.append(ref["id"])
        unread = "●" if "UNREAD" in (m.get("labelIds") or []) else "○"
        sender = clean(g._short_sender(g._hdr(m, "From")), 40) or "?"
        subject = clean(g._hdr(m, "Subject"), 90) or "(no subject)"
        when = _short_date(g._hdr(m, "Date"))
        lines.append(f"{len(ids)}.  {unread}  {sender}  —  {subject}" + (f"      {when}" if when else ""))
        snip = clean(m.get("snippet"), 110)
        if snip:
            lines.append(f"        {snip}")
        lines.append("")
    lines.append("Say “read mail 2” to open one, “inbox” to come back.")
    return "\n".join(lines).strip(), ids


def mail_body(msg_id: str, gw=None) -> str:
    if not _ID.match(str(msg_id or "")):
        return "I can't open that message."
    try:
        g = _gw(gw)
        m = g._service("gmail", "v1").users().messages().get(userId="me", id=msg_id, format="full").execute()
        body = g._body_text(m.get("payload", {})) or m.get("snippet", "")
    except Exception as e:                                       # noqa: BLE001
        return _problem(e, "message")
    head = [f"From:  {clean(g._hdr(m, 'From'), 100)}", f"Subject:  {clean(g._hdr(m, 'Subject'), 120)}", f"Date:  {clean(g._hdr(m, 'Date'), 40)}", ""]
    return "\n".join(head) + clean(body, 6000, keep_newlines=True) + "\n\n(Say “inbox” to go back to the list.)"


# ── expenses ─────────────────────────────────────────────────────────────────
def _money(v: float) -> str:
    s = f"{v:,.0f}" if abs(v - round(v)) < 0.005 else f"{v:,.2f}"
    return s.replace(",", " ")


def expenses_text(month: str | None = None, fin=None, today: date | None = None, data: dict | None = None) -> str:
    today = today or date.today()
    month = month if (month and re.fullmatch(r"\d{4}-\d{2}", month)) else today.strftime("%Y-%m")
    try:
        if data is None:
            f = _fin(fin)
            from actions._common import cfg, json_load
            data = json_load(f.STORE, {"entries": [], "budgets": {}, "currency": ""})
            cur = str(data.get("currency") or cfg().get("currency") or "USD").upper()
        else:
            cur = str(data.get("currency") or "USD").upper()
    except Exception as e:                                       # noqa: BLE001
        return f"I couldn't read the expenses just now ({type(e).__name__})."
    entries = [e for e in (data.get("entries") or []) if isinstance(e, dict) and str(e.get("date", "")).startswith(month)]
    spent, income = {}, 0.0
    for e in entries:
        try:
            amt = float(e.get("amount", 0))
        except (TypeError, ValueError):
            continue
        if e.get("type") == "income":
            income += amt
        else:
            c = clean(e.get("category"), 20) or "Other"
            spent[c] = spent.get(c, 0.0) + amt
    total = sum(spent.values())
    try:
        title = datetime.strptime(month, "%Y-%m").strftime("%B %Y")
    except ValueError:
        title = month
    out = [title, f"Spent  {_money(total)} {cur}      Income  {_money(income)} {cur}      Balance  {_money(income - total)} {cur}", ""]
    if not spent:
        out.append("Nothing recorded this month yet.")
    else:
        out.append("By category")
        budgets = data.get("budgets") or {}
        for cat, v in sorted(spent.items(), key=lambda kv: -kv[1]):
            share = v / total if total else 0.0
            bar = "█" * round(share * 12) + "░" * (12 - round(share * 12))
            b = budgets.get(cat)
            extra = ""
            if isinstance(b, (int, float)) and b > 0:
                extra = f"     budget {_money(b)} — " + (f"OVER by {_money(v - b)}" if v > b else f"{round(v / b * 100)}% used")
            out.append(f"{cat:<11} {bar}  {_money(v)} {cur}  ({share * 100:.0f}%){extra}")
        out += ["", "Latest"]
        for e in sorted(entries, key=lambda e: (str(e.get("date")), str(e.get("id"))), reverse=True)[:8]:
            try:
                amt = float(e.get("amount", 0))
            except (TypeError, ValueError):
                continue
            sign = "+" if e.get("type") == "income" else "−"
            tag = "  🧾" if e.get("source") == "receipt" else ""
            out.append(f"{str(e.get('date'))[5:]}   {sign}{_money(amt)} {cur}   {clean(e.get('category'), 14)}   {clean(e.get('note'), 50)}{tag}")
    out += ["", "Drop a receipt photo on this card, or open one and say “scan receipt”."]
    return "\n".join(out)


def scan_receipt(path: str, fin=None) -> str:
    try:
        p = os.path.realpath(str(path))
        if os.path.splitext(p)[1].lower() not in RECEIPT_EXT or not os.path.isfile(p):
            return "That isn't a picture I can read as a receipt."
        if os.path.getsize(p) > MAX_RECEIPT_BYTES:
            return "That picture is too big for a receipt (15 MB at most)."
        res = _fin(fin).finance_tracker({"action": "scan_receipt", "image_path": p})
    except Exception as e:                                       # noqa: BLE001
        return f"I couldn't scan that receipt ({type(e).__name__})."
    return clean(res, 220) or "Done."


# ── structure for the rich cards: the plain text above, read back into data the Hand Space overlay can draw ──────────────
def parse_calendar(text: str):
    """calendar_text() → [{"label", "date", "events": [{"allday", "start", "end", "title", "loc"}]}]  (None if it is not an agenda)."""
    days, cur = [], None
    for line in str(text or "").split("\n"):
        m = re.match(r"^(\S.*?)\s{2}·\s{2}(.+)$", line)
        if m and not line.startswith(" "):
            cur = {"label": m.group(1), "date": m.group(2), "events": []}
            days.append(cur)
            continue
        if cur is None or not line.startswith("   "):
            continue
        body = line.strip()
        if body.startswith("—"):                                   # "— nothing planned —"
            continue
        m = re.match(r"^(\d{2}:\d{2}) – (\d{2}:\d{2})\s{3}(.*)$", body) or re.match(r"^(All day)()\s{3}(.*)$", body)
        if not m:
            continue
        title, _, loc = m.group(3).partition("   @ ")
        cur["events"].append({"allday": m.group(1) == "All day", "start": "" if m.group(1) == "All day" else m.group(1),
                              "end": m.group(2), "title": title.strip(), "loc": loc.strip()})
    return days or None


def parse_mail(text: str):
    """mail_list() → {"count", "unread", "items": [{"n", "unread", "sender", "subject", "when", "snippet"}]}  (None for anything else)."""
    items = []
    for line in str(text or "").split("\n"):
        m = re.match(r"^(\d+)\.\s{2}([●○])\s{2}(.*?)\s{2}—\s{2}(.*?)(?:\s{6}(\d{2} \w{3} \d{2}:\d{2}))?$", line)
        if m:
            items.append({"n": int(m.group(1)), "unread": m.group(2) == "●", "sender": m.group(3), "subject": m.group(4),
                          "when": m.group(5) or "", "snippet": ""})
        elif items and line.startswith("        ") and line.strip():
            items[-1]["snippet"] = line.strip()
    if not items:
        return {"count": 0, "unread": 0, "items": []} if "inbox is empty" in str(text or "").lower() else None
    return {"count": len(items), "unread": sum(1 for i in items if i["unread"]), "items": items}


def parse_expenses(text: str):
    """expenses_text() → month, totals, categories (with budgets) and the latest entries  (None if it is an error message)."""
    text = str(text or "")
    lines = text.split("\n")
    m, at = None, 0
    for at, ln in enumerate(lines[:4]):
        m = re.match(r"^Spent\s+(.+?)\s{3,}Income\s+(.+?)\s{3,}Balance\s+(.+)$", ln)
        if m:
            break
    if not m:
        return None
    out = {"month": lines[0].strip(), "spent": m.group(1).strip(), "income": m.group(2).strip(), "balance": m.group(3).strip(),
           "cats": [], "latest": [], "empty": "Nothing recorded this month" in text}
    sect = ""
    for ln in lines[at + 1:]:
        if ln.strip() == "By category":
            sect = "cat"
        elif ln.strip() == "Latest":
            sect = "latest"
        elif sect == "cat":
            c = re.match(r"^(.+?)\s+([█░]{12})\s{2}(.+?)\s\((\d+)%\)(?:\s{5}budget (.+?) — (OVER by .+|\d+% used))?$", ln)
            if c:
                out["cats"].append({"name": c.group(1).strip(), "amount": c.group(3).strip(), "share": int(c.group(4)),
                                    "budget": c.group(5), "status": c.group(6), "over": bool(c.group(6) and c.group(6).startswith("OVER"))})
        elif sect == "latest" and ln.strip():
            parts = ln.split("   ", 3)
            if len(parts) >= 3 and re.match(r"^\d{2}-\d{2}$", parts[0].strip()):
                note = parts[3].strip() if len(parts) > 3 else ""
                out["latest"].append({"date": parts[0].strip(), "income": parts[1].strip().startswith("+"), "amount": parts[1].strip()[1:],
                                      "cat": parts[2].strip(), "note": note.replace("🧾", "").strip(), "receipt": "🧾" in note})
    return out


def parse_json(text: str):
    try:
        d = json.loads(text)
        return d if isinstance(d, dict) else None
    except (TypeError, ValueError):
        return None


# ── live widgets: weather, what is playing, this computer ────────────────────────────────────────────────────────────
_GEO: dict[str, tuple] = {}


def _cfg() -> dict:
    try:
        from actions._common import cfg
        return cfg() or {}
    except Exception:                                            # noqa: BLE001
        return {}


def _http_json(url: str, timeout: float = 8.0):
    import requests
    r = requests.get(url, timeout=timeout, headers={"User-Agent": "Jarvis-HandSpace"})
    r.raise_for_status()
    return r.json()


def _where(city: str):
    """(lat, lon, label): the city named in the config (`hs_weather_city`), else the rough place of this connection."""
    key = city.strip().lower()
    if key in _GEO:
        return _GEO[key]
    if key:
        res = (_http_json("https://geocoding-api.open-meteo.com/v1/search?count=1&name=" + quote(city)).get("results") or [None])[0]
        if not res:
            raise LookupError(city)
        out = (float(res["latitude"]), float(res["longitude"]), clean(res.get("name"), 40))
    else:
        d = _http_json("https://ipapi.co/json/")
        out = (float(d["latitude"]), float(d["longitude"]), clean(d.get("city") or d.get("region"), 40) or "Here")
    _GEO[key] = out
    return out


def wx_kind(code, is_day: bool = True) -> tuple[str, str]:
    """WMO weather code → (words, icon name)."""
    try:
        c = int(code)
    except (TypeError, ValueError):
        return "Weather", "cloud"
    if c == 0:
        return "Clear", "clear" if is_day else "night"
    if c in (1, 2):
        return "Partly cloudy", "partly" if is_day else "cloud"
    if c == 3:
        return "Overcast", "cloud"
    if c in (45, 48):
        return "Fog", "fog"
    if 51 <= c <= 57:
        return "Drizzle", "rain"
    if 61 <= c <= 67 or 80 <= c <= 82:
        return "Rain", "rain"
    if 71 <= c <= 77 or c in (85, 86):
        return "Snow", "snow"
    if c >= 95:
        return "Thunderstorm", "storm"
    return "Cloudy", "cloud"


def weather_json(cfg: dict | None = None) -> str:
    """Current weather and a 5-day outlook from Open-Meteo (free, no key) as JSON text. City: `hs_weather_city` in config, else
    the rough location of this connection. Units: `hs_weather_units` = "f" for °F / mph (default °C / km/h)."""
    cfg = cfg if cfg is not None else _cfg()
    city = str(cfg.get("hs_weather_city") or cfg.get("weather_city") or cfg.get("city") or "")
    fahr = str(cfg.get("hs_weather_units") or "").lower().startswith("f")
    try:
        lat, lon, label = _where(city)
        url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat:.4f}&longitude={lon:.4f}"
               "&current=temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,is_day"
               "&daily=weather_code,temperature_2m_max,temperature_2m_min&forecast_days=5&timezone=auto"
               + ("&temperature_unit=fahrenheit&wind_speed_unit=mph" if fahr else ""))
        d = _http_json(url)
        cur, day = d["current"], d["daily"]
        days = [{"d": day["time"][i], "code": day["weather_code"][i], "hi": round(day["temperature_2m_max"][i]),
                 "lo": round(day["temperature_2m_min"][i])} for i in range(min(5, len(day["time"])))]
        return json.dumps({"city": label, "t": round(cur["temperature_2m"]), "feels": round(cur["apparent_temperature"]),
                           "hum": round(cur["relative_humidity_2m"]), "wind": round(cur["wind_speed_10m"]), "code": cur["weather_code"],
                           "day": bool(cur.get("is_day", 1)), "unit": "°F" if fahr else "°C", "wunit": "mph" if fahr else "km/h", "days": days})
    except Exception:                                            # noqa: BLE001
        hint = "" if city else ' Set "hs_weather_city" in config/api_keys.json.'
        return json.dumps({"error": "I couldn't read the weather just now." + hint})


def nowplaying_json() -> str:
    """What Spotify is playing, as JSON text: {"state": "playing" | "paused" | "none", "title", "artist"}."""
    state, title, artist = "none", "", ""
    try:
        from actions import spotify_controller as sc
        if sys.platform == "win32":
            t = clean(sc._win_title(), 140)                       # Spotify's window title is "Artist - Title" while it plays
            if t:
                artist, sep, title = t.partition(" - ")
                if not sep:
                    title, artist = t, ""
                state = "playing"
        else:
            m = re.match(r"^(Playing|Paused on): (.*) by (.*)\.$", sc._now_playing())
            if m:
                state, title, artist = ("playing" if m.group(1) == "Playing" else "paused"), clean(m.group(2), 100), clean(m.group(3), 100)
    except Exception:                                            # noqa: BLE001
        pass
    return json.dumps({"state": state, "title": title.strip(), "artist": artist.strip()})


class SysSampler:
    """CPU / memory / disk / battery / network readings for the System card — cheap, so the window's thread asks once a second."""

    def __init__(self):
        self.cpu_hist = collections.deque(maxlen=60)
        self.mem_hist = collections.deque(maxlen=60)
        self.cur: dict = {}
        self._t = 0.0
        self._net = None

    def sample(self, now: float | None = None) -> dict:
        now = now if now is not None else time.time()
        if self.cur and now - self._t < 1.0:
            return self.cur
        try:
            import psutil
        except ImportError:
            self._t, self.cur = now, {"error": "psutil isn't installed."}
            return self.cur
        try:
            vm = psutil.virtual_memory()
            du = psutil.disk_usage(os.path.abspath(os.sep))
            cur = {"cpu": float(psutil.cpu_percent(None)), "mem": float(vm.percent), "mem_used": vm.used, "mem_total": vm.total,
                   "disk": float(du.percent), "disk_free": du.free, "disk_total": du.total, "up": 0.0, "down": 0.0,
                   "boot": float(psutil.boot_time()), "bat": None, "plugged": None}
            try:
                b = psutil.sensors_battery()
                if b is not None:
                    cur["bat"], cur["plugged"] = float(b.percent), bool(b.power_plugged)
            except Exception:                                    # noqa: BLE001
                pass
            n = psutil.net_io_counters()
            if self._net is not None and now > self._net[0]:
                dt = now - self._net[0]
                cur["up"], cur["down"] = max(0.0, (n.bytes_sent - self._net[1]) / dt), max(0.0, (n.bytes_recv - self._net[2]) / dt)
            self._net = (now, n.bytes_sent, n.bytes_recv)
            self.cpu_hist.append(cur["cpu"])
            self.mem_hist.append(cur["mem"])
        except Exception:                                        # noqa: BLE001
            cur = {"error": "Couldn't read the system just now."}
        self._t, self.cur = now, cur
        return cur


# ── one entry point for the window's worker thread ───────────────────────────
def handle(req: tuple, gw=None, fin=None):
    """req → (what, payload):  ("feed", {"feed", "text", "ids"}) | ("toast", str) | ("geocode", dict | str)."""
    kind = req[0]
    if kind == "feed":
        which = req[1]
        if which == "calendar":
            return "feed", {"feed": "calendar", "text": calendar_text(gw), "ids": None}
        if which == "mail":
            text, ids = mail_list(gw)
            return "feed", {"feed": "mail", "text": text, "ids": ids}
        if which == "expenses":
            return "feed", {"feed": "expenses", "text": expenses_text(fin=fin), "ids": None}
        if which == "weather":
            return "feed", {"feed": "weather", "text": weather_json(), "ids": None}
        if which == "nowplaying":
            return "feed", {"feed": "nowplaying", "text": nowplaying_json(), "ids": None}
    if kind == "mail_read":
        return "feed", {"feed": "mail", "text": mail_body(req[1], gw), "ids": None, "keep_ids": True}
    if kind == "receipt":
        msg = scan_receipt(req[1], fin)
        return "receipt", {"msg": msg, "text": expenses_text(fin=fin)}
    return "toast", "Not something I can do."

#by ASDcuber

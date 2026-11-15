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

import os
import re
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
    if kind == "mail_read":
        return "feed", {"feed": "mail", "text": mail_body(req[1], gw), "ids": None, "keep_ids": True}
    if kind == "receipt":
        msg = scan_receipt(req[1], fin)
        return "receipt", {"msg": msg, "text": expenses_text(fin=fin)}
    return "toast", "Not something I can do."

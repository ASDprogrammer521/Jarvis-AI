"""Offline mode: Jarvis keeps working when the internet is down or too poor for Gemini Live.

What lives here
  • quiet_network_logs / is_network_error — a dropped connection is not an "error" the user needs to see: no tracebacks,
    no library log spam; the run loop just switches mode.
  • probe() / Connectivity — a real TLS handshake with the Live host: "connected" means Gemini is reachable, not just Wi-Fi.
  • parse() — turns a short command (Uzbek / Russian / English) into an action. Pure Python, no network, unit-testable.
  • OfflineMode — microphone → Vosk (offline speech recognition) → parse() → the same local tools Gemini would have
    called (camera, volume, apps, timer, notes…) → a short spoken + written answer (system voice, no internet).

Voice recognition needs `pip install vosk`; its small model downloads once while the internet works (prepare_async) and
afterwards runs fully offline. Without vosk, typed commands in the input bar still work.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import random
import re
import shutil
import socket
import ssl
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

LIVE_HOST = "generativelanguage.googleapis.com"
SLOW_MS = 3500.0            # a TLS handshake slower than this means the line is too poor for a live voice session
PROBE_TIMEOUT = 4.0


# ══ quiet networking ═════════════════════════════════════════════════════════════════════════════════════════════════

def quiet_network_logs() -> None:
    """Library loggers (websockets, httpx, google-genai…) print tracebacks for every dropped connection."""
    for name in ("websockets", "websockets.client", "httpx", "httpcore", "google_genai", "google.genai", "asyncio"):
        try:
            logging.getLogger(name).setLevel(logging.CRITICAL)
        except Exception:
            pass


class LinkDown(Exception):
    """Raised inside the live session when the connection became too poor to keep it."""


_NET_TYPES = ("ConnectionClosed", "ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
              "RemoteProtocolError", "InvalidStatus", "InvalidHandshake", "ServerDisconnected", "ClientConnector",
              "ClientOSError", "APIConnection", "ReadError", "WriteError", "NetworkError", "ProxyError",
              "TimeoutException", "LinkDown", "IncompleteRead")
_NET_TEXT = ("timed out", "timeout", "getaddrinfo", "name resolution", "name or service", "connection reset",
             "connection closed", "connection aborted", "connection refused", "cannot connect", "network is unreachable",
             "no route to host", "temporary failure", "eof occurred", "broken pipe", "no close frame", "going away",
             "keepalive ping", "service is unavailable", "unavailable", "overloaded", "internal error encountered",
             "1006", "1011", "1001", "502", "503", "504", "ssl", "certificate_verify", "socket", "link too poor")
_NOT_NET = ("api key", "1007", "invalid_argument", "unknown name", "unexpected keyword", "permission_denied",
            "resource_exhausted", "quota")


def flatten(exc: BaseException | None, _seen=None) -> list[BaseException]:
    """Every exception inside a (nested) ExceptionGroup plus the causes — TaskGroup wraps real errors in groups."""
    out: list[BaseException] = []
    seen = _seen if _seen is not None else set()
    if exc is None or id(exc) in seen:
        return out
    seen.add(id(exc))
    out.append(exc)
    for sub in getattr(exc, "exceptions", ()) or ():
        out += flatten(sub, seen)
    out += flatten(exc.__cause__, seen)               # (not __context__: an unrelated earlier OSError must not turn a bug into "network")
    return out


def is_network_error(exc: BaseException) -> bool:
    """True when the failure is the connection's fault (so: switch to offline quietly), not a bug or a bad key."""
    parts = flatten(exc)
    text = " ".join(f"{type(p).__name__} {p}" for p in parts).lower()
    if any(k in text for k in _NOT_NET):
        return False
    for p in parts:
        if isinstance(p, (OSError, EOFError, TimeoutError)) and not isinstance(
                p, (FileNotFoundError, PermissionError, IsADirectoryError, NotADirectoryError, FileExistsError)):
            return True
        if any(t in type(p).__name__ for t in _NET_TYPES):
            return True
    return any(k in text for k in _NET_TEXT)


# ══ connectivity ═════════════════════════════════════════════════════════════════════════════════════════════════════

def _cfg() -> dict:
    try:
        from memory.config_manager import load_api_keys
        return load_api_keys() or {}
    except Exception:
        return {}


def auto_enabled() -> bool:
    """config/api_keys.json → "offline_auto": false turns the automatic switch off (default: on)."""
    return bool(_cfg().get("offline_auto", True))


def probe(timeout: float = PROBE_TIMEOUT) -> float | None:
    """Milliseconds for a TLS handshake with the Gemini Live host, or None when it cannot be reached at all."""
    t0 = time.perf_counter()
    try:
        with socket.create_connection((LIVE_HOST, 443), timeout=timeout) as s:
            s.settimeout(timeout)
            ctx = ssl.create_default_context()
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2          # never negotiate TLS 1.0 / 1.1 (CodeQL: insecure SSL/TLS version)
            with ctx.wrap_socket(s, server_hostname=LIVE_HOST):
                pass
        return (time.perf_counter() - t0) * 1000.0
    except Exception:
        return None


def good(ms: float | None) -> bool:
    return ms is not None and ms <= SLOW_MS


# ══ understanding short commands ═════════════════════════════════════════════════════════════════════════════════════

@dataclass
class Intent:
    kind: str                                   # "tool" | "camera" | "local"
    name: str = ""                              # tool name, or camera action / local topic
    args: dict = field(default_factory=dict)
    lang: str = "uz"


NAME_TOKENS = ("jarvis", "jarviz", "jarvisga", "jarvisni", "жарвис", "джарвис", "жарвисга")

_UZ_UNITS = {"bir": 1, "ikki": 2, "uch": 3, "tort": 4, "besh": 5, "olti": 6, "yetti": 7, "sakkiz": 8, "toqqiz": 9}
_UZ_TENS = {"on": 10, "yigirma": 20, "ottiz": 30, "qirq": 40, "ellik": 50, "oltmish": 60, "yetmish": 70, "sakson": 80,
            "toqson": 90}
_RU_UNITS = {"один": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7,
             "восемь": 8, "девять": 9, "десять": 10, "пятнадцать": 15, "двадцать": 20, "тридцать": 30, "сорок": 40,
             "пятьдесят": 50, "шестьдесят": 60}
_EN_UNITS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
             "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60}
_TIME_UNITS = ("daqiqa", "daqiqadan", "soat", "soatdan", "min", "minut", "minutes", "minute", "hour", "hours",
               "минут", "минуты", "минуту", "час", "часа", "часов", "sekund", "second", "seconds")


def norm(text: str) -> str:
    t = str(text).lower().replace("ё", "е")
    for a in ("'", "’", "ʻ", "ʼ", "‘", "`", "´"):
        t = t.replace(a, "")
    t = re.sub(r"[^\w\s\.\+\-\*/%=()×÷:,]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def words_to_digits(t: str) -> str:
    """'besh daqiqa' → '5 daqiqa', 'yigirma besh minut' → '25 minut' (the speech model writes numbers as words)."""
    toks = t.split()
    out: list[str] = []
    i = 0
    while i < len(toks):
        w = toks[i]
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        if w in _UZ_TENS and (w != "on" or nxt in _UZ_UNITS or nxt in _TIME_UNITS):   # "on" alone is also English "on"
            v = _UZ_TENS[w]
            if nxt in _UZ_UNITS:
                v += _UZ_UNITS[nxt]
                i += 1
            out.append(str(v))
        elif w in _UZ_UNITS:
            out.append(str(_UZ_UNITS[w]))
        elif w in _RU_UNITS:
            out.append(str(_RU_UNITS[w]))
        elif w in _EN_UNITS:
            out.append(str(_EN_UNITS[w]))
        else:
            out.append(w)
        i += 1
    return " ".join(out)


_OPEN = {"och", "oching", "ochib", "ochgin", "ochish", "yoq", "yoqing", "yoqib", "open", "show", "start", "launch",
         "turn", "on", "оч", "очинг", "очиб", "ёк", "ёкинг", "открой", "открыть", "включи", "включить", "покажи",
         "запусти", "запустить"}
_CLOSE = {"yop", "yoping", "yopib", "yopgin", "ochir", "ochiring", "ochirib", "close", "stop", "off", "ёп", "ёпинг",
          "учир", "ўчир", "ўчиринг", "учиринг", "закрой", "закрыть", "выключи", "выключить", "останови", "kill"}
_FULL = {"toliq", "tolik", "fullscreen", "full", "katta", "kattalashtir", "kattaroq", "kengaytir", "bigger", "maximize",
         "maximise", "тулик", "тўлиқ", "полный", "полном", "весь", "увеличь", "больше"}
_SMALL = {"kichik", "kichraytir", "kichikroq", "oddiy", "windowed", "smaller", "exit", "уменьши", "оконный", "меньше"}
_UP = {"kotar", "kotaring", "oshir", "oshiring", "baland", "balandroq", "up", "louder", "increase", "higher", "громче",
       "увеличь", "прибавь", "kopaytir", "yorqinroq", "ярче"}
_DOWN = {"pasaytir", "pasaytiring", "kamaytir", "kamaytiring", "past", "pastroq", "down", "lower", "quieter",
         "decrease", "dim", "тише", "уменьши", "убавь", "тусклее", "xiraroq"}


def _lang_of(raw: str, t: str) -> str:
    if re.search(r"[а-яА-Я]", raw):
        return "ru"
    if re.search(r"\b(open|close|turn|volume|what|time|screenshot|camera|timer|remind|note|help|battery|full|up|down)\b", t):
        return "en"
    return "uz"


def _has(toks: set[str], words: set[str]) -> bool:
    return bool(toks & words)


def _num(t: str, units: tuple = ()) -> int | None:
    m = re.search(r"(\d+)\s*(" + "|".join(units) + r")" if units else r"(\d+)", t)
    return int(m.group(1)) if m else None


def parse(raw: str) -> Intent | None:
    """One short spoken or typed command → Intent, or None when it is not a command this mode understands."""
    t0 = norm(raw)
    if not t0:
        return None
    lang = _lang_of(str(raw), t0)
    toks0 = t0.split()
    t = " ".join(w for w in toks0 if w not in NAME_TOKENS)
    if not t:
        return None
    toks = set(t.split())

    # ── camera ─────────────────────────────────────────────────────────────────────────────────────────────────────
    if any(w.startswith(("kamera", "camera", "камер")) for w in t.split()):
        if _has(toks, _FULL) and not _has(toks, _SMALL):
            return Intent("camera", "full", lang=lang)
        if _has(toks, _SMALL):
            return Intent("camera", "small", lang=lang)
        if _has(toks, _CLOSE):
            return Intent("camera", "close", lang=lang)
        return Intent("camera", "open", lang=lang)

    # ── things that need the internet: say so instead of pretending ────────────────────────────────────────────────
    if re.search(r"\b(yech|yechib|yechish|solve|реши|решить|tushuntir|explain|объясни|tarjima|translate|переведи)", t):
        return Intent("local", "needs_net", lang=lang)

    # ── timer / focus ──────────────────────────────────────────────────────────────────────────────────────────────
    if re.search(r"\b(taymer|timer|таймер|fokus|focus|pomodoro|помодоро)\w*", t):
        tn = words_to_digits(t)
        if _has(toks, _CLOSE | {"toxtat", "toxtating", "bekor", "cancel", "стоп", "отмени"}):
            return Intent("tool", "focus_timer", {"action": "stop"}, lang)
        mins = _num(tn, ("daqiqa", "min", "minut", "minutes", "minute", "минут", "минуты", "мин")) or _num(tn) or 25
        return Intent("tool", "focus_timer", {"action": "start", "minutes": mins}, lang)

    # ── reminder (relative: "10 daqiqadan keyin eslat …") ─────────────────────────────────────────────────────────
    if re.search(r"\beslat(?!ma)\w*|\bremind\w*|напомни\w*", t):
        tn = words_to_digits(t)
        m = re.search(r"(\d+)\s*(daqiqa\w*|min\w*|минут\w*|мин|soat\w*|hour\w*|час\w*)", tn)
        if m:
            n = int(m.group(1))
            hours = m.group(2).startswith(("soat", "hour", "час"))
            when = datetime.now() + (timedelta(hours=n) if hours else timedelta(minutes=n))
            msg = re.sub(r"\beslat\w*|\bremind\w*|напомни\w*|\bkeyin\b|\bdan\b|\bme\b|\bin\b|\bчерез\b|" + re.escape(m.group(0)),
                         " ", tn)
            msg = re.sub(r"\s+", " ", msg).strip() or ("Eslatma" if lang == "uz" else "Reminder")
            return Intent("tool", "reminder", {"date": when.strftime("%Y-%m-%d"), "time": when.strftime("%H:%M"),
                                               "message": msg}, lang)
        return Intent("local", "need_time", lang=lang)

    # ── notes ──────────────────────────────────────────────────────────────────────────────────────────────────────
    m = re.search(r"\b(eslatma|qayd|zametka|note|заметк\w*|запиши)\b\s*(?:yoz\w*|qil\w*|saqla\w*|save|:|-)?\s*(.*)$", t)
    if m:
        body = m.group(2).strip()
        if body:
            return Intent("tool", "notes_tasks", {"action": "add", "kind": "note", "text": body}, lang)
        return Intent("local", "need_text", lang=lang)

    # ── screenshot ─────────────────────────────────────────────────────────────────────────────────────────────────
    if re.search(r"skrinshot|screenshot|скриншот|снимок экрана|ekran rasm|ekranni rasm", t):
        return Intent("tool", "screen_capture", {"action": "screenshot"}, lang)

    # ── volume / brightness ────────────────────────────────────────────────────────────────────────────────────────
    if re.search(r"\b(ovoz\w*|tovush\w*|volume|звук\w*|громкост\w*)\b", t):
        if _has(toks, _UP):
            return Intent("tool", "computer_settings", {"action": "volume_up"}, lang)
        if _has(toks, _DOWN):
            return Intent("tool", "computer_settings", {"action": "volume_down"}, lang)
        if _has(toks, _CLOSE | {"mute", "беззвучно", "без"}):
            return Intent("tool", "computer_settings", {"action": "mute"}, lang)
    if re.search(r"\b(yorqinlik\w*|yorugl\w*|brightness|яркост\w*)\b", t):
        if _has(toks, _UP):
            return Intent("tool", "computer_settings", {"action": "brightness_up"}, lang)
        if _has(toks, _DOWN):
            return Intent("tool", "computer_settings", {"action": "brightness_down"}, lang)

    # ── media ──────────────────────────────────────────────────────────────────────────────────────────────────────
    if re.search(r"\b(pauza|pause|пауза|davom\w*|resume)\b", t):
        return Intent("tool", "computer_settings", {"action": "pause_video"}, lang)

    # ── system / battery / lock ────────────────────────────────────────────────────────────────────────────────────
    if re.search(r"batareya|battery|akkumulyator|батаре|заряд", t):
        return Intent("tool", "power_tools", {"action": "battery"}, lang)
    if re.search(r"qulfla|lock (the )?screen|заблокируй", t):
        return Intent("tool", "power_tools", {"action": "lock"}, lang)
    if re.search(r"\bcpu\b|\bram\b|protsessor|tizim holati|kompyuter\w* (holati|qanday)|system status|состояние (системы|компьютера)|нагрузка", t):
        return Intent("tool", "system_status", {}, lang)

    # ── connection status / help ───────────────────────────────────────────────────────────────────────────────────
    if re.search(r"\b(internet\w*|tarmoq\w*|wifi|wi-fi|интернет\w*)\b", t) and re.search(
            r"bor|tekshir|holat|check|status|ulan|проверь|есть", t):
        return Intent("local", "net_status", lang=lang)
    if re.search(r"yordam|\bhelp\b|nima qila olasan|qanday buyruq|что ты умеешь|помощь", t):
        return Intent("local", "help", lang=lang)

    # ── time / date ────────────────────────────────────────────────────────────────────────────────────────────────
    if re.search(r"bugun.*(qaysi|nechanchi|sana)|\bsana\b|what.*date|today.*date|какое число|какой.*день|\bдата\b", t):
        return Intent("local", "date", lang=lang)
    if re.search(r"soat (necha|nechi)|vaqt (necha|nechi)|hozir soat|what time|\btime\b|который час|сколько времени|\bвремя\b", t):
        return Intent("local", "time", lang=lang)

    # ── calculator ─────────────────────────────────────────────────────────────────────────────────────────────────
    e = " " + words_to_digits(t) + " "
    for a, b in ((r" plus | qosh | qoshib | плюс ", " + "), (r" minus | ayir | ayirib | минус ", " - "),
                 (r" ga kopaytir\w* | kopaytir\w* | times | umnoj\w* | умножить на | x | × ", " * "),
                 (r" ga bol\w* | bolish | divided by | разделить на | ÷ ", " / ")):
        e = re.sub(a, b, e)
    e = re.sub(r"\b(hisobla\w*|nechi|necha|teng|qancha|calculate|equals|сколько|будет|равно|ga|dan|=)\b", " ", e)
    e = re.sub(r"\s+", " ", e).strip().replace(",", ".")
    if re.fullmatch(r"[\d\.\s\+\-\*/\(\)%]+", e) and re.search(r"\d\s*[\+\-\*/%]\s*\d", e):
        return Intent("tool", "utility_tools", {"action": "calculate", "expression": e}, lang)

    # ── open an app ("chrome och", "open chrome", "открой хром") ────────────────────────────────────────────────────
    app = None
    m = (re.match(r"^(.{2,40}?)\s+(?:och|oching|ochib ber|ochib bering|yoq|yoqing|ishga tushir\w*)$", t)
         or re.match(r"^(?:open|launch|start|run)\s+(.{2,40})$", t)
         or re.match(r"^(?:открой|запусти|включи)\s+(.{2,40})$", t))
    if m:
        app = re.sub(r"\b(dasturini|dasturi|ilovasini|ilovani|ilova|dastur|programmani|приложение|программу|the|app)\b", " ", m.group(1))
        app = re.sub(r"(ni|ga)$", "", app.strip()).strip()
        if app and len(app.split()) <= 3 and not app.endswith(("ing", "ingiz", "im", "imiz")):   # "ko'zingni och" is not an app
            return Intent("tool", "open_app", {"app_name": app}, lang)
    return None


_MSG = {
    "uz": {
        "cam_open": "Kamera ochildi.", "cam_full": "Kamera to'liq ekran.", "cam_small": "Kamera oddiy holatda.",
        "cam_close": "Kamera yopildi.", "time": "Soat {t}.", "date": "Bugun {d}.", "done": "Bajarildi.",
        "failed": "Bajarilmadi: {e}",
        "needs_net": "Buni qilish uchun internet kerak, hozir oflayn rejimdaman.",
        "need_time": "Qancha vaqtdan keyin eslatay? Masalan: 10 daqiqadan keyin eslat.",
        "need_text": "Nimani yozay? Masalan: eslatma yoz sut olish.",
        "unknown": "Oflayn rejimdaman — buyruqni tushunmadim. «Yordam» deng.",
        "net_ok": "Internet bor ({ms:.0f} ms).", "net_poor": "Internet juda sekin ({ms:.0f} ms), oflayn rejimdaman.",
        "net_down": "Internet yo'q, oflayn rejimdaman.",
        "help": "Oflayn buyruqlar: kamerani och, to'liq ekran, yop; soat necha; bugun sana; ovozni ko'tar yoki pasaytir; "
                "yorqinlik; skrinshot; chrome och; 5 daqiqa taymer; 10 daqiqadan keyin eslat; eslatma yoz; "
                "2 plus 3; batareya; kompyuter holati.",
    },
    "ru": {
        "cam_open": "Камера открыта.", "cam_full": "Камера на весь экран.", "cam_small": "Камера в обычном режиме.",
        "cam_close": "Камера закрыта.", "time": "Сейчас {t}.", "date": "Сегодня {d}.", "done": "Готово.",
        "failed": "Не получилось: {e}",
        "needs_net": "Для этого нужен интернет, сейчас я офлайн.",
        "need_time": "Через сколько напомнить? Например: напомни через 10 минут.",
        "need_text": "Что записать? Например: запиши заметка купить молоко.",
        "unknown": "Я офлайн и не понял команду. Скажите «помощь».",
        "net_ok": "Интернет есть ({ms:.0f} мс).", "net_poor": "Интернет очень медленный ({ms:.0f} мс), я офлайн.",
        "net_down": "Интернета нет, я офлайн.",
        "help": "Офлайн-команды: открой камеру, на весь экран, закрой; который час; какое число; громче или тише; "
                "яркость; скриншот; открой chrome; таймер 5 минут; напомни через 10 минут; запиши заметка; "
                "2 плюс 3; батарея; состояние компьютера.",
    },
    "en": {
        "cam_open": "Camera is open.", "cam_full": "Camera is full screen.", "cam_small": "Camera is back to normal.",
        "cam_close": "Camera closed.", "time": "It is {t}.", "date": "Today is {d}.", "done": "Done.",
        "failed": "That failed: {e}",
        "needs_net": "That needs the internet, and I'm offline right now.",
        "need_time": "Remind you in how long? For example: remind me in 10 minutes.",
        "need_text": "What should I write down? For example: note buy milk.",
        "unknown": "I'm offline and didn't understand. Say 'help'.",
        "net_ok": "The internet is up ({ms:.0f} ms).", "net_poor": "The internet is very slow ({ms:.0f} ms), staying offline.",
        "net_down": "No internet, I'm offline.",
        "help": "Offline commands: open the camera, full screen, close it; what time is it; what's the date; volume up "
                "or down; brightness; screenshot; open chrome; timer 5 minutes; remind me in 10 minutes; note …; "
                "2 plus 3; battery; system status.",
    },
}
_MONTHS_UZ = ["yanvar", "fevral", "mart", "aprel", "may", "iyun", "iyul", "avgust", "sentabr", "oktabr", "noyabr", "dekabr"]


def msg(lang: str, key: str, **kw) -> str:
    s = _MSG.get(lang, _MSG["uz"]).get(key) or _MSG["uz"][key]
    return s.format(**kw) if kw else s


# ══ speaking without the internet ════════════════════════════════════════════════════════════════════════════════════

class LocalTTS:
    """The system voice (Windows SAPI via PowerShell, macOS `say`, Linux espeak). No packages, no network."""

    def __init__(self):
        self._lock = threading.Lock()

    def available(self) -> bool:
        if os.name == "nt":
            return True
        return bool(shutil.which("say") or shutil.which("espeak-ng") or shutil.which("espeak") or shutil.which("spd-say"))

    def speak(self, text: str) -> None:
        text = str(text or "").strip()
        if not text:
            return
        with self._lock:
            try:
                if os.name == "nt":
                    env = dict(os.environ, JARVIS_SAY=text)
                    subprocess.run(
                        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                         "Add-Type -AssemblyName System.Speech; "
                         "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; $s.Speak($env:JARVIS_SAY)"],
                        env=env, timeout=40, capture_output=True)
                else:
                    exe = shutil.which("say") or shutil.which("espeak-ng") or shutil.which("espeak") or shutil.which("spd-say")
                    if exe:
                        subprocess.run([exe, text], timeout=40, capture_output=True)
            except Exception:
                pass


# ══ offline mode ═════════════════════════════════════════════════════════════════════════════════════════════════════

def _vosk_lang() -> str:
    return str(_cfg().get("offline_lang") or "uz").strip().lower()


def _vosk_cache() -> Path:
    return Path.home() / ".cache" / "vosk"


def vosk_installed() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("vosk") is not None
    except Exception:
        return False


def model_cached() -> bool:
    p = str(_cfg().get("offline_stt_path") or "").strip()
    if p:
        return Path(p).is_dir()
    lang = _vosk_lang()
    try:
        return any(f"-{lang}-" in d.name or d.name.endswith(f"-{lang}") for d in _vosk_cache().glob("vosk-model*"))
    except Exception:
        return False


class OfflineMode:
    """Runs while Gemini Live is unreachable.

    run_tool(name, args, say) -> str     runs a local tool (the same registry Gemini's calls go to), blocking
    asst_name() -> str                   the assistant's name for the chat log
    need_name() -> bool                  True: voice commands must contain "Jarvis" (wake-word mode)
    """

    def __init__(self, ui, run_tool: Callable, asst_name: Callable[[], str], need_name: Callable[[], bool] = lambda: False):
        self.ui = ui
        self.run_tool = run_tool
        self.asst_name = asst_name
        self.need_name = need_name
        self.active = False
        self.speaking = False
        self.tts = LocalTTS()
        self._model = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._work = threading.Lock()                       # one command at a time
        self._warned: set[str] = set()
        self._prepared = False

    # ── lifecycle ────────────────────────────────────────────────────────────────────────────────────────────────
    def start(self) -> None:
        if self.active:
            return
        self.active = True
        self._stop.clear()
        self._log("SYS: 📴 Internet yo'q yoki juda sekin — oflayn rejim. Oddiy buyruqlar ishlaydi («yordam» deng).")
        try:
            self.ui.set_state("LISTENING")
        except Exception:
            pass
        self._thread = threading.Thread(target=self._voice_loop, daemon=True, name="jarvis-offline-voice")
        self._thread.start()

    def stop(self, back_online: bool = True) -> None:
        if not self.active:
            return
        self.active = False
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=3)
        self._thread = None
        if back_online:
            self._log("SYS: 🌐 Internet qaytdi — onlayn rejimga o'tilmoqda.")

    def prepare_async(self) -> None:
        """While the internet works: fetch the offline speech model once, so it is there when the line drops."""
        if self._prepared or not vosk_installed() or model_cached():
            return
        self._prepared = True

        def work():
            try:
                if not good(probe()):
                    self._prepared = False
                    return
                self._log("SYS: Oflayn ovoz modeli yuklab olinmoqda (bir marta)…")
                self._load_model()
                self._log("SYS: Oflayn ovoz modeli tayyor.")
            except Exception:
                self._prepared = False
        threading.Thread(target=work, daemon=True, name="jarvis-offline-prep").start()

    # ── speech recognition ───────────────────────────────────────────────────────────────────────────────────────
    def _load_model(self):
        if self._model is not None:
            return self._model
        from vosk import Model, SetLogLevel
        try:
            SetLogLevel(-1)
        except Exception:
            pass
        path = str(_cfg().get("offline_stt_path") or "").strip()
        self._model = Model(path) if path else Model(lang=_vosk_lang())
        return self._model

    def _warn_once(self, key: str, text: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self._log(text)

    def _voice_loop(self) -> None:
        if not vosk_installed():
            self._warn_once("vosk", "SYS: Oflayn ovoz tanish uchun «pip install vosk» kerak. Hozircha buyruqlarni yozib yuboring.")
            return
        try:
            model = self._load_model()
        except Exception:
            self._warn_once("model", "SYS: Oflayn ovoz modeli yo'q — internet bor paytda yuklab olinadi. Hozircha buyruqlarni yozing.")
            return
        try:
            import numpy as np
            import sounddevice as sd
            from vosk import KaldiRecognizer
            from core import audio_devices
            from memory.config_manager import get_input_device
            rec = KaldiRecognizer(model, 16000)
            q: queue.Queue = queue.Queue(maxsize=40)

            def cb(indata, frames, t, status):
                if self.speaking or getattr(self.ui, "muted", False):
                    return
                try:
                    q.put_nowait(bytes(indata))
                    a = np.frombuffer(indata, dtype=np.int16)
                    self.ui.set_audio_level(min(1.0, float(np.abs(a).mean()) / 32768.0 * 14))
                except Exception:
                    pass

            dev = audio_devices.resolve(get_input_device(), "input")
            try:
                stream = sd.InputStream(samplerate=16000, channels=1, dtype="int16", blocksize=4000, device=dev, callback=cb)
            except Exception:
                stream = sd.InputStream(samplerate=16000, channels=1, dtype="int16", blocksize=4000, callback=cb)
            with stream:
                while not self._stop.is_set():
                    try:
                        data = q.get(timeout=0.3)
                    except queue.Empty:
                        continue
                    if rec.AcceptWaveform(data):
                        text = str(json.loads(rec.Result()).get("text", "")).strip()
                        if text:
                            self._handle(text, voice=True)
                            while not q.empty():
                                try:
                                    q.get_nowait()
                                except queue.Empty:
                                    break
                            rec.Reset()
        except Exception:
            self._warn_once("mic", "SYS: Oflayn rejimda mikrofon ochilmadi — buyruqlarni yozib yuboring.")

    # ── commands ─────────────────────────────────────────────────────────────────────────────────────────────────
    def handle_text(self, text: str) -> None:
        """A command typed into the input bar while offline."""
        threading.Thread(target=self._handle, args=(text, False), daemon=True, name="jarvis-offline-cmd").start()

    def _log(self, text: str) -> None:
        try:
            self.ui.write_log(text)
        except Exception:
            pass

    def _handle(self, text: str, voice: bool) -> None:
        with self._work:
            toks = norm(text).split()
            has_name = any(w in NAME_TOKENS for w in toks)
            if voice and self.need_name() and not has_name:
                return
            intent = parse(text)
            if intent is None:
                if voice and not has_name:
                    return                              # room chatter that is not a command: stay quiet
                self._log(f"You: {text}")
                self._say(msg(_lang_of(text, norm(text)), "unknown"))
                return
            self._log(f"You: {text}")
            try:
                reply = self._execute(intent)
            except Exception as e:
                reply = msg(intent.lang, "failed", e=str(e)[:80])
            self._say(reply, intent.lang)

    def _execute(self, it: Intent) -> str:
        L = it.lang
        if it.kind == "camera":
            if it.name == "open":
                self.ui.start_camera_stream()
                return msg(L, "cam_open")
            if it.name == "full":
                self.ui.start_camera_stream()
                self.ui.camera_fullscreen(True)
                return msg(L, "cam_full")
            if it.name == "small":
                self.ui.camera_fullscreen(False)
                return msg(L, "cam_small")
            self.ui.stop_camera_stream()
            return msg(L, "cam_close")
        if it.kind == "local":
            now = datetime.now()
            if it.name == "time":
                return msg(L, "time", t=now.strftime("%H:%M"))
            if it.name == "date":
                d = f"{now.day}-{_MONTHS_UZ[now.month - 1]}, {now.year}" if L == "uz" else now.strftime("%d.%m.%Y" if L == "ru" else "%A, %B %d")
                return msg(L, "date", d=d)
            if it.name == "net_status":
                ms = probe()
                return msg(L, "net_down") if ms is None else msg(L, "net_ok" if good(ms) else "net_poor", ms=ms)
            return msg(L, it.name)                      # help | needs_net | need_time | need_text
        out = self.run_tool(it.name, dict(it.args), self._say)
        out = str(out or "").strip()
        if not out or out.lower() in ("done.", "ok"):
            return msg(L, "done")
        if it.name in ("system_status", "power_tools", "utility_tools", "screen_capture", "notes_tasks", "reminder", "focus_timer"):
            return out[:300]
        return out[:200]

    # ── answering ────────────────────────────────────────────────────────────────────────────────────────────────
    def _say(self, text: str, lang: str = "uz") -> None:
        text = str(text or "").strip()
        if not text:
            return
        self._log(f"{self.asst_name()}: {text}")
        if not (bool(_cfg().get("offline_tts", True)) and self.tts.available()):
            return
        self.speaking = True
        try:
            self.ui.set_state("SPEAKING")
        except Exception:
            pass
        done = threading.Event()

        def pulse():                                    # the system voice gives no levels: animate the HUD while it talks
            while not done.is_set():
                try:
                    self.ui.set_audio_level(0.25 + 0.55 * random.random())
                except Exception:
                    pass
                time.sleep(0.07)
        threading.Thread(target=pulse, daemon=True).start()
        try:
            self.tts.speak(text)
        finally:
            done.set()
            time.sleep(0.25)                            # let the room go quiet before the microphone listens again
            self.speaking = False
            try:
                if not getattr(self.ui, "muted", False):
                    self.ui.set_state("LISTENING")
            except Exception:
                pass

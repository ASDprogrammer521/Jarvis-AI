from __future__ import annotations

import sys
import webbrowser
from pathlib import Path

from actions._common import BASE_DIR, log

_SECTIONS = {
    "timer": "timer", "solve": "timer", "solves": "timer", "scramble": "timer", "time": "timer",
    "trainer": "trainer", "alg": "trainer", "algs": "trainer", "algorithm": "trainer", "algorithms": "trainer",
    "oll": "trainer", "pll": "trainer", "f2l": "trainer", "practice": "trainer",
    "dashboard": "dashboard", "home": "dashboard", "tools": "dashboard",
}
_LABELS = {"timer": "timer", "trainer": "algorithm trainer", "dashboard": "tools page"}


def _find_page() -> Path | None:
    roots = [BASE_DIR, BASE_DIR / "_internal"]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.insert(0, Path(meipass))
    for root in roots:
        p = root / "dashboard" / "static" / "cubing.html"
        if p.is_file():
            return p
    return None


def cubing_timer(parameters: dict, player=None, speak=None) -> str:
    p = parameters or {}
    key = str(p.get("section") or "timer").lower().strip()
    section = _SECTIONS.get(key, "timer")
    page = _find_page()
    if page is None:
        return "The cubing tools page (dashboard/static/cubing.html) is missing, sir."
    try:
        webbrowser.open(f"{page.as_uri()}#{section}")
    except Exception as e:                                             # noqa: BLE001
        return f"I couldn't open the browser, sir: {e}"
    log(f"[Cubing] opened {page.name}#{section}", player)
    return f"Cubing {_LABELS[section]} is open, sir. Hold Space, release to start, press Space to stop."


TOOL = {
    "name": "cubing_timer",
    "description": (
        "Opens the user's own speedcubing website in the browser: the Rubik's cube timer (2x2 to 7x7, inspection, scrambles, "
        "ao5 / ao12 stats) or the CFOP algorithm trainer (OLL / PLL / F2L). Use for 'open my cubing timer', 'let me time a solve', "
        "'start a cube timer', 'open the alg trainer', 'practice OLL'. Not for the focus/pomodoro timer (use focus_timer)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "section": {"type": "STRING", "description": "timer (default) | trainer (OLL/PLL/F2L algorithms) | dashboard"},
        },
    },
    "handler": cubing_timer,
}

# by ASDcuber521

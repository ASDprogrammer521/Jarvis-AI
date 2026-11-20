"""Voice control of the smart nudges (battery, meetings, workout reminders): turn them on/off, tune them, snooze them.

The engine itself lives in core/nudges.py and is started by Jarvis at launch; this tool only edits its settings
(the same ones Settings → Nudges shows), so "stop reminding me about workouts" works by voice."""
from __future__ import annotations

from actions._common import CONFIG_PATH, cfg
from core import nudges as _core
from core.nudges import RULE_INFO, merged_cfg

_ALIASES = {"meeting": ("meeting", "calendar", "event", "uchrashuv", "встреч"),
            "battery": ("battery", "charge", "batareya", "батаре", "заряд"),
            "workout": ("workout", "exercise", "mashq", "тренир"),
            "streak": ("streak", "seriya", "серия")}


def _rule(name) -> str | None:
    t = str(name or "").lower()
    for key, words in _ALIASES.items():
        if t == key or any(w in t for w in words):
            return key
    return None


def _save(n: dict) -> None:
    try:
        from memory.config_manager import _patch_config
        _patch_config(nudges=n)
        return
    except Exception:
        pass
    import json
    d = cfg() or {}
    d["nudges"] = n
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(d, indent=2), encoding="utf-8")


def _describe(n: dict) -> str:
    if not n["enabled"]:
        return "Smart nudges are off."
    bits = []
    for name, (label, _sub, key, _opts, fmt) in RULE_INFO.items():
        r = n["rules"][name]
        bits.append(f"{label}: " + (fmt.format(r[key]) if r["on"] else "off"))
    q = n["quiet"]
    return ("Smart nudges are on" + (" (voice)" if n["voice"] else " (toasts only)") + ". " + "; ".join(bits)
            + (f". Quiet hours {q['from']}–{q['to']}." if q["on"] else "."))


def nudges(parameters: dict, player=None) -> str:
    p = parameters or {}
    action = str(p.get("action") or "status").lower().strip()
    n = merged_cfg((cfg() or {}).get("nudges"))

    if action in ("status", "list"):
        return _describe(n)

    if action in ("snooze", "pause"):
        minutes = float(p.get("minutes") or 60)
        eng = _core.ENGINE
        if eng is None:
            return "The nudge engine isn't running yet, sir."
        eng.snooze(minutes)
        return f"Quiet for {int(minutes)} minutes, sir. No nudges until then."

    if action in ("resume", "unsnooze"):
        eng = _core.ENGINE
        if eng is not None:
            eng.snooze(0)
        return "Nudges are back on, sir."

    if action in ("on", "enable", "off", "disable", "set"):
        on = action in ("on", "enable") if action != "set" else bool(p.get("on", True))
        name = p.get("rule")
        if not name:                                             # no rule named: the master switch
            n["enabled"] = on
            _save(n)
            return "Smart nudges are on, sir." if on else "Smart nudges are off, sir."
        rule = _rule(name)
        if rule is None:
            return f"I don't know a nudge called '{name}'. They are: " + ", ".join(RULE_INFO) + "."
        n["rules"][rule]["on"] = on
        key = RULE_INFO[rule][2]
        if on and p.get("value") is not None:
            try:
                n["rules"][rule][key] = int(p["value"])
            except (TypeError, ValueError):
                pass
        _save(n)
        label = RULE_INFO[rule][0].lower()
        return (f"The {label} nudge is on ({RULE_INFO[rule][4].format(n['rules'][rule][key])})." if on
                else f"The {label} nudge is off.")

    if action == "voice":
        n["voice"] = bool(p.get("on", True))
        _save(n)
        return "I'll say nudges aloud." if n["voice"] else "Nudges will only appear on screen."

    if action == "quiet":
        if p.get("from") and p.get("to"):
            n["quiet"].update({"from": str(p["from"]), "to": str(p["to"]), "on": True})
        else:
            n["quiet"]["on"] = bool(p.get("on", True))
        n = merged_cfg(n)
        _save(n)
        q = n["quiet"]
        return f"Quiet hours {q['from']}–{q['to']}." if q["on"] else "Quiet hours are off."

    return "Unknown action. Available: status, on/off (optionally with rule), set, snooze, resume, voice, quiet."


TOOL = {
    "name": "nudges",
    "description": (
        "Control Jarvis' proactive reminders ('nudges'): upcoming meetings, low battery, 'you haven't worked out today', "
        "'your streak ends tonight'. Use when the user wants them turned on or off, changed ('remind me about meetings 10 minutes "
        "before', 'battery warning at 20%', 'workout reminder from 7 pm'), snoozed ('be quiet for an hour'), set to quiet hours, "
        "or asks what nudges are active. Actions: status, on, off, set, snooze, resume, voice, quiet."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "status | on | off | set | snooze | resume | voice | quiet"},
            "rule": {"type": "STRING", "description": "meeting | battery | workout | streak (leave out to switch ALL nudges)"},
            "value": {"type": "INTEGER", "description": "New setting for the rule: minutes before a meeting, battery percent, or the hour (0-23)"},
            "minutes": {"type": "NUMBER", "description": "Snooze length in minutes (default 60)"},
            "on": {"type": "BOOLEAN", "description": "For voice / quiet: switch on (true) or off (false)"},
            "from": {"type": "STRING", "description": "Quiet hours start, HH:MM"},
            "to": {"type": "STRING", "description": "Quiet hours end, HH:MM"},
        },
    },
    "handler": nudges,
}

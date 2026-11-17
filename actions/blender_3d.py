"""blender_3d — Jarvis builds and edits 3D models in Blender from plain language.

How it works
  · "create a medieval castle" → a Gemini call writes a short bpy script on top of the `jb` helper library (_blender_lib.py),
    the script runs in a background Blender (no window), the project is saved as a .blend and a preview PNG is rendered.
    If the script fails, the error goes back to Gemini and it is fixed (up to 3 tries). A failed build never touches the project.
  · "make the roof red", "add a chimney" → the same loop on the existing project, seeing the list of objects that are in it.
  · colour / move / delete / lights / export / import / render / undo are direct operations (no AI call, fast).
  · "open it in Blender" opens the .blend in the normal Blender window to finish by hand.

Setup: install Blender (blender.org). Jarvis finds it in the usual folders; otherwise put its path in config/api_keys.json:
  "blender_path": "C:\\\\Program Files\\\\Blender Foundation\\\\Blender 4.2\\\\blender.exe"
Projects live in  Documents/Jarvis/blender/<project>/  (model.blend, renders/, exports/, history/ for undo).
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from actions._common import cfg, log, open_path, output_dir, safe_name, unique_path

_HERE = Path(__file__).resolve().parent
_LOCK = threading.Lock()
_MAX_ATTEMPTS = 3
_HISTORY_KEEP = 12
_VERSION_CACHE: dict[str, str] = {}

# ------------------------------------------------------------------ finding Blender


def _version_key(p: Path):
    m = re.search(r"(\d+)\.(\d+)", p.parent.name)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def find_blender() -> Path | None:
    exe = "blender.exe" if os.name == "nt" else "blender"
    configured = str(cfg().get("blender_path") or os.environ.get("BLENDER_PATH") or "").strip().strip('"')
    if configured:
        p = Path(configured).expanduser()
        if p.is_dir():
            p = p / exe
        if p.is_file():
            return p
    found = shutil.which("blender")
    if found:
        return Path(found)
    cands: list[Path] = []
    if os.name == "nt":
        roots = [Path(v) for k in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)") if (v := os.environ.get(k))]
        if os.environ.get("LOCALAPPDATA"):
            roots.append(Path(os.environ["LOCALAPPDATA"]) / "Programs")
        for r in roots:
            cands += list(r.glob("Blender Foundation/Blender*/blender.exe"))
            cands += list(r.glob("Blender*/blender.exe"))
        cands.append(Path("C:/Program Files (x86)/Steam/steamapps/common/Blender/blender.exe"))
    elif sys.platform == "darwin":
        cands.append(Path("/Applications/Blender.app/Contents/MacOS/Blender"))
    else:
        cands += [Path(x) for x in ("/usr/bin/blender", "/usr/local/bin/blender", "/snap/bin/blender", "/opt/blender/blender")]
    cands = [c for c in cands if c.is_file()]
    return max(cands, key=_version_key) if cands else None


def _blender_version(exe: Path) -> str:
    key = str(exe)
    if key not in _VERSION_CACHE:
        try:
            out = subprocess.run([key, "--version"], capture_output=True, text=True, timeout=40,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            _VERSION_CACHE[key] = (out.strip().splitlines() or ["Blender"])[0]
        except Exception:                                                  # noqa: BLE001
            _VERSION_CACHE[key] = "Blender (version unknown)"
    return _VERSION_CACHE[key]


_NO_BLENDER = (
    "I can't find Blender on this computer, sir. Install it from blender.org (the free version is enough), "
    "or put its path into config/api_keys.json as \"blender_path\"."
)

# ------------------------------------------------------------------ projects


def _root() -> Path:
    return output_dir("blender")


def _read_state() -> dict:
    try:
        return json.loads((_root() / "state.json").read_text(encoding="utf-8"))
    except Exception:                                                      # noqa: BLE001
        return {}


def _set_current(name: str) -> None:
    try:
        (_root() / "state.json").write_text(json.dumps({"current": name}), encoding="utf-8")
    except Exception:                                                      # noqa: BLE001
        pass


def _paths(name: str) -> dict:
    d = _root() / name
    return {"name": name, "dir": d, "blend": d / "model.blend", "renders": d / "renders", "exports": d / "exports", "history": d / "history"}


def _project_names() -> list[str]:
    try:
        return sorted(p.name for p in _root().iterdir() if (p / "model.blend").is_file())
    except Exception:                                                      # noqa: BLE001
        return []


def _unique_name(text: str) -> str:
    base = safe_name(" ".join(str(text).split()[:4]), "model", 30).lower() or "model"
    name, n = base, 2
    while (_root() / name).exists():
        name = f"{base}_{n}"
        n += 1
    return name


def _pick_project(p: dict) -> tuple[dict | None, str]:
    name = safe_name(str(p.get("project") or ""), "", 40) or _read_state().get("current") or ""
    if not name or not _paths(name)["blend"].is_file():
        return None, "There's no Blender project yet, sir. Ask me to create something first."
    return _paths(name), ""


# ------------------------------------------------------------------ running Blender


def _run_blender(project: Path, *, ops=None, code="", render=None, exports=None, save=True, new=False,
                 stage=True, timeout=180) -> dict:
    exe = find_blender()
    if exe is None:
        return {"ok": False, "error": "NO_BLENDER"}
    work = Path(tempfile.mkdtemp(prefix="jarvis_blender_"))
    result_path = work / "result.json"
    job = {"lib_dir": str(_HERE), "project": str(project), "new": bool(new), "ops": ops or [], "code": code or "",
           "render": render, "exports": exports or [], "save": bool(save), "stage": bool(stage), "result": str(result_path)}
    (work / "job.json").write_text(json.dumps(job), encoding="utf-8")
    cmd = [str(exe), "-b", "--factory-startup"]
    if project.is_file() and not new:
        cmd.append(str(project))
    cmd += ["--python", str(_HERE / "_blender_runner.py"), "--python-exit-code", "1", "--", str(work / "job.json")]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        tail = ((proc.stderr or "") + "\n" + (proc.stdout or ""))[-700:].strip()
        if result_path.is_file():
            try:
                return json.loads(result_path.read_text(encoding="utf-8"))
            except Exception:                                              # noqa: BLE001
                pass
        return {"ok": False, "error": f"Blender ended without a result (exit code {proc.returncode}). {tail}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Blender took longer than {timeout} s and was stopped (the script is probably too heavy)."}
    except Exception as e:                                                 # noqa: BLE001
        return {"ok": False, "error": f"Could not start Blender: {e}"}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _backup(paths: dict) -> Path | None:
    if not paths["blend"].is_file():
        return None
    paths["history"].mkdir(parents=True, exist_ok=True)
    dst = paths["history"] / f"{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000) % 1000:03d}.blend"
    shutil.copy2(paths["blend"], dst)
    old = sorted(paths["history"].glob("*.blend"))
    for f in old[:-_HISTORY_KEEP]:
        try:
            f.unlink()
        except OSError:
            pass
    return dst


def _render(paths: dict, view: str, res, samples: int) -> tuple[Path | None, str]:
    """A separate Blender run, so a render crash can never hurt the model. EEVEE first, Cycles as the fallback."""
    paths["renders"].mkdir(parents=True, exist_ok=True)
    out = unique_path(paths["renders"], "render", ".png")
    err = ""
    for engine in (None, "CYCLES"):
        spec = {"path": str(out), "view": view, "res": list(res), "samples": samples, "engine": engine}
        r = _run_blender(paths["blend"], render=spec, save=False, timeout=240)
        if r.get("ok") and not r.get("render_error") and out.is_file():
            return out, ""
        err = r.get("render_error") or r.get("error") or "render failed"
    return None, err


# ------------------------------------------------------------------ safety check for generated scripts

_ALLOWED_IMPORTS = {"bpy", "bmesh", "mathutils", "math", "random", "colorsys", "itertools", "functools", "collections", "typing", "numpy"}
_BAD_CALLS = {"exec", "eval", "compile", "__import__", "open", "input", "breakpoint", "globals", "locals", "vars", "getattr",
              "setattr", "delattr", "exit", "quit"}
_BAD_ATTRS = {"__class__", "__subclasses__", "__globals__", "__builtins__", "__bases__", "__mro__", "__dict__", "__code__",
              "__import__", "preferences", "addons", "execfile", "system", "popen"}
_BAD_OPS = {"wm", "script", "render", "preferences", "screen", "outliner", "file", "image", "sequencer", "text"}


def check_code(code: str) -> str:
    """'' when the script looks safe to run, otherwise the reason. A guard against mistakes and obvious abuse, not a sandbox."""
    if len(code) > 60_000:
        return "script is too long"
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"SyntaxError: {e.msg} (line {e.lineno})"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] not in _ALLOWED_IMPORTS:
                    return f"import of '{a.name}' is not allowed (only bpy, bmesh, mathutils, math, random…)"
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] not in _ALLOWED_IMPORTS:
                return f"import from '{node.module}' is not allowed"
            if (node.module or "").startswith("bpy") and any(a.name in ("ops", "app", "utils") for a in node.names):
                return "from bpy import ops/app/utils is not allowed (use the jb helpers)"
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _BAD_CALLS:
            return f"calling {node.func.id}() is not allowed"
        elif isinstance(node, ast.Attribute):
            if node.attr in _BAD_ATTRS:
                return f"access to '.{node.attr}' is not allowed"
            if node.attr in _BAD_OPS and isinstance(node.value, ast.Attribute) and node.value.attr == "ops":
                return f"bpy.ops.{node.attr} is not allowed (use the jb helpers)"
    return ""


# ------------------------------------------------------------------ the AI that writes the script


def _api_reference() -> str:
    try:
        tree = ast.parse((_HERE / "_blender_lib.py").read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "API_REFERENCE" for t in node.targets):
                return str(ast.literal_eval(node.value)).strip()
    except Exception:                                                      # noqa: BLE001
        pass
    return ""


_RULES = """You are Jarvis' Blender modeller. You write a short Python script that builds or edits a 3D model with the `jb` helper library.

OUTPUT: only Python code. No explanations, no markdown fences.
ALREADY AVAILABLE (do not import them): jb, bpy, bmesh, math, random, Vector, Euler, Matrix. Importing anything else (os, sys, subprocess…) or calling open() is forbidden and the script will be rejected.
Build with jb helpers. ALWAYS keep the object that every jb shape returns in a Python variable and pass THAT VARIABLE on (jb.bevel(top, 0.01), parent=table, jb.boolean(a, b)). Never write object names as code inside a string such as "bpy.data.objects['X']", and never use bpy.data.objects[...] at all. Use only the colour names from the API reference or "#rrggbb" hex codes.
Organic things (donut, fruit, rocket, animal, character) are built from jb.torus / jb.sphere / jb.lathe / jb.cone / jb.cylinder, scaled and combined; keep them simple, 5-15 parts.
Do not call bpy.ops.* (render/wm/script are blocked) and do not add a camera, rendering or saving: Jarvis does that. Lights and background are automatic too; the only exception is that for a finished character or showpiece you may end the script with jb.showcase("#rrggbb") to give it a coloured studio backdrop.

QUALITY – make it look like a real, recognisable, good-looking model, not a pile of boxes:
 • real-world proportions in metres (person ≈ 1.75 m, door ≈ 2 m, chair seat ≈ 0.45 m), standing on the ground at z = 0 (use bottom=0 for the lowest parts).
 • many distinct, well-named parts ("Door", "Roof", "Wheel_FL"), details (handles, trims, windows, buttons, screws), jb.bevel on hard edges.
 • believable colours and materials (jb.mat for metal, glass, wood, glowing parts), not everything the same grey.
 • use loops, jb.array, jb.ring, jb.grid, jb.mirror for repetition; wrap the whole model in jb.group("Name") and pass parent=… so it can be moved as one.
 • keep it light: at most ~300 objects and a few thousand vertices per shape; it must run in well under 30 seconds.

CHARACTERS AND CREATURES: for any person, zombie, soldier, robot, humanoid monster or NPC call jb.humanoid(...) first (it makes a complete faceted low-poly figure), then adjust parts by name or add props with jb.limb / jb.blob (hat, sword, backpack, wings, horns, tail, spikes). For animals and other creatures build the body with jb.blob and the legs, neck, tail and horns with jb.limb. These faceted, flat-shaded parts give the modern low-poly look; do not build characters out of plain boxes.
LOW-POLY STYLE: few sides (5-8), flat shading (smooth=False), a small jitter for a hand-made look, two or three well-chosen colours per object, one glowing accent (emission) where it suits.
If a reference image is attached, copy its subject, proportions, colour palette and art style as closely as the jb helpers allow.

MODES
 • create: the scene is empty. Add jb.ground() only when the subject naturally stands on a floor.
 • edit: the model described under SCENE already exists. Change ONLY what was asked and keep every other object. Refer to existing parts by their exact names (jb.find, jb.recolor, jb.transform, jb.delete) and give new parts new unique names. Never call jb.clear().

EXAMPLE (create "a wooden table"):
top = jb.box((1.6, 0.9, 0.06), name="TableTop", color="wood", bottom=0.72)
jb.bevel(top, 0.01)
table = jb.group("Table", [top])
for i, (x, y) in enumerate([(-0.72, -0.38), (0.72, -0.38), (-0.72, 0.38), (0.72, 0.38)]):
    jb.cylinder(r=0.04, h=0.72, name=f"Leg{i+1}", loc=(x, y, 0), bottom=0, color="darkwood", parent=table)

EXAMPLE (create "a low-poly zombie with glowing orange eyes"):
z = jb.humanoid("Zombie", style="zombie", pose="zombie", height=1.8)
jb.recolor("Zombie_Shirt*", "#5b7a3a")
jb.showcase("#2f4a3a")

EXAMPLE (create "a low-poly christmas tree with a gold star and baubles"):
trunk = jb.cylinder(r=0.14, h=0.5, verts=8, name="Trunk", color="darkwood", bottom=0, smooth=False)
tree = jb.group("Tree", [trunk])
for i, (r, h, z) in enumerate([(1.25, 1.0, 0.45), (1.0, 0.95, 1.1), (0.75, 0.85, 1.7), (0.5, 0.75, 2.25)]):
    jb.cone(r1=r, r2=0.0, h=h, verts=8, name=f"Layer{i+1}", color="darkgreen", bottom=z, smooth=False, parent=tree)
pts = [(math.cos(math.radians(90 + a * 36)) * (0.22 if a % 2 == 0 else 0.1), math.sin(math.radians(90 + a * 36)) * (0.22 if a % 2 == 0 else 0.1)) for a in range(10)]
jb.extrude_poly(pts, depth=0.06, name="Star", color="gold", metallic=0.9, roughness=0.3, rot=(90, 0, 0), loc=(0, 0.03, 3.1), parent=tree)
b = jb.sphere(r=0.07, segments=12, rings=8, name="Bauble", color="red", metallic=0.6, roughness=0.25, loc=(0.85, 0, 0.9))
jb.ring(b, count=7, radius=0.85, center=(0, 0, 0.9), face_out=False)
jb.showcase("#1f2f4a")

EXAMPLE (edit "make the roof red and add a chimney"):
jb.recolor("Roof*", "red")
jb.box((0.4, 0.4, 1.0), name="Chimney", loc=(1.2, 0.5, 4.2), color="brick", parent=jb.find("House")[0])
"""


def _extract_code(text: str) -> str:
    text = (text or "").strip()
    m = re.search(r"```(?:python|py)?\s*\n(.*?)```", text, re.S)
    if m:
        return m.group(1).strip()
    return re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()


def _image_part(path):
    """A reference image for Gemini, or None when it can't be read."""
    try:
        f = Path(str(path or "").strip().strip('"')).expanduser()
        if not f.is_file():
            return None
        mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}.get(f.suffix.lower())
        if mime is None or f.stat().st_size > 15_000_000:
            return None
        from google.genai import types
        return types.Part.from_bytes(data=f.read_bytes(), mime_type=mime)
    except Exception:                                                      # noqa: BLE001
        return None


def _ask_llm(request: str, mode: str, scene: list | None, previous: str = "", error: str = "", image=None) -> str:
    from core import gemini
    parts = [_RULES, "API REFERENCE\n" + _api_reference(), f"MODE: {mode}"]
    if mode == "edit":
        parts.append("SCENE (objects that already exist):\n" + json.dumps((scene or [])[:70], separators=(",", ":"))[:6000])
    parts.append(f"REQUEST: {request}")
    if previous and error:
        parts.append("YOUR PREVIOUS SCRIPT FAILED.\n--- script ---\n" + previous[-9000:] + "\n--- error ---\n" + error[-1500:] +
                     "\nFix it and return the complete corrected script.")
    prompt = "\n\n".join(parts)
    part = _image_part(image) if image else None
    contents = [part, "A reference image is attached: model what it shows.\n\n" + prompt] if part is not None else prompt
    return _extract_code(gemini.text(contents, tier=gemini.SMART, timeout_ms=120_000))


def _generate_and_run(request: str, mode: str, paths: dict, scene: list | None, new: bool, player=None, image=None) -> tuple[dict, str]:
    """AI writes the script, Blender runs it, errors go back to the AI. Returns (result, last_error)."""
    code, error, short = "", "", ""
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        code = _ask_llm(request, mode, scene, previous=code, error=error, image=image)
        if not code:
            return {"ok": False}, "the AI model returned nothing (check the Gemini key / connection)"
        bad = check_code(code)
        if bad:
            error = short = bad
            log(f"[Blender] attempt {attempt}: script rejected — {bad}", player)
            continue
        backup = _backup(paths)
        paths["dir"].mkdir(parents=True, exist_ok=True)
        res = _run_blender(paths["blend"], code=code, new=new, timeout=150)
        if res.get("error") == "NO_BLENDER":
            return res, "NO_BLENDER"
        if res.get("ok"):
            return res, ""
        if backup is not None:                                             # failed run: the project must stay exactly as it was
            shutil.copy2(backup, paths["blend"])
            backup.unlink(missing_ok=True)
        error = (res.get("error") or "") + "\n" + (res.get("trace") or "")
        short = ((res.get("error") or "unknown error").splitlines() or ["unknown error"])[0][:220]
        log(f"[Blender] attempt {attempt} failed — {(res.get('error') or '')[:160]}", player)
    return {"ok": False}, short or "unknown error"


# ------------------------------------------------------------------ helpers for parameters


def _vec(v):
    if v is None or v == "":
        return None
    if isinstance(v, str):
        v = re.findall(r"-?\d+(?:\.\d+)?", v)
    try:
        out = [float(x) for x in v]
    except (TypeError, ValueError):
        return None
    return out or None


def _num(v):
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def _flag(v, default=True) -> bool:
    if v is None or v == "":
        return default
    return str(v).strip().lower() not in ("0", "false", "no", "off")


def _res(p: dict) -> tuple[int, int]:
    w = int(_num(p.get("width")) or 960)
    h = int(_num(p.get("height")) or 720)
    return max(160, min(w, 3840)), max(120, min(h, 2160))


def _preview_view(p: dict) -> str:
    return str(p.get("view") or "iso").lower().strip()


def _summary(res: dict) -> str:
    rows = res.get("summary") or []
    names = [r["name"] for r in rows if r.get("type") != "EMPTY"][:10]
    extra = "" if len(rows) <= 10 else f" and {len(rows) - 10} more"
    return f"{len(rows)} objects ({', '.join(names)}{extra})" if rows else "an empty scene"


def _finish(p: dict, paths: dict, headline: str, res: dict, player=None) -> str:
    """Render a preview (unless switched off) and compose the reply."""
    msg = f"{headline} {_summary(res)}." if "summary" in res else headline
    if _flag(p.get("preview"), True):
        png, err = _render(paths, _preview_view(p), _res(p), int(_num(p.get("samples")) or 48))
        if png is not None:
            msg += f" Preview: {png}."
            if _flag(p.get("show"), True):
                open_path(png)
        else:
            msg += f" (The model is saved, but the preview render failed: {err[:160]}.)"
    msg += f" Project: {paths['blend']}. Say 'open it in Blender' to refine it by hand, or tell me what to change."
    log(f"[Blender] {headline}", player)
    return msg


def _direct(p: dict, ops: list, headline: str, player=None) -> str:
    paths, err = _pick_project(p)
    if paths is None:
        return err
    backup = _backup(paths)
    res = _run_blender(paths["blend"], ops=ops, timeout=150)
    if res.get("error") == "NO_BLENDER":
        return _NO_BLENDER
    if not res.get("ok"):
        if backup is not None:
            shutil.copy2(backup, paths["blend"])
            backup.unlink(missing_ok=True)
        return f"That didn't work, sir: {res.get('error', 'unknown error')}"
    return _finish(p, paths, headline, res, player)


# ------------------------------------------------------------------ actions


def _status(p: dict) -> str:
    exe = find_blender()
    if exe is None:
        return _NO_BLENDER
    cur = _read_state().get("current") or "none yet"
    return f"Blender is ready: {_blender_version(exe)} at {exe}. Current project: {cur}. Projects: {len(_project_names())}. Folder: {_root()}"


def _create(p: dict, player=None) -> str:
    prompt = str(p.get("prompt") or "").strip()
    if not prompt:
        return "What should I build, sir? For example: 'a medieval castle with four towers'."
    if find_blender() is None:
        return _NO_BLENDER
    name = _unique_name(p.get("name") or prompt)
    paths = _paths(name)
    paths["dir"].mkdir(parents=True, exist_ok=True)
    log(f"[Blender] building '{prompt[:60]}' …", player)
    res, err = _generate_and_run(prompt, "create", paths, None, new=True, player=player, image=p.get("image"))
    if not res.get("ok"):
        if not paths["blend"].is_file():
            shutil.rmtree(paths["dir"], ignore_errors=True)
        return _NO_BLENDER if err == "NO_BLENDER" else f"I couldn't get that model to build after {_MAX_ATTEMPTS} tries, sir: {err}"
    _set_current(name)
    return _finish(p, paths, f"Built '{name}' in Blender —", res, player)


def _edit(p: dict, player=None) -> str:
    prompt = str(p.get("prompt") or "").strip()
    if not prompt:
        return "What should I change, sir?"
    if find_blender() is None:
        return _NO_BLENDER
    paths, err = _pick_project(p)
    if paths is None:
        return err
    inspect = _run_blender(paths["blend"], save=False, timeout=90)
    if not inspect.get("ok"):
        return f"I couldn't read the project: {inspect.get('error', 'unknown error')}"
    log(f"[Blender] editing '{paths['name']}': {prompt[:60]} …", player)
    res, err = _generate_and_run(prompt, "edit", paths, inspect.get("summary") or [], new=False, player=player, image=p.get("image"))
    if not res.get("ok"):
        return _NO_BLENDER if err == "NO_BLENDER" else f"I couldn't make that change after {_MAX_ATTEMPTS} tries, sir (the project is unchanged): {err}"
    return _finish(p, paths, "Done —", res, player)


def _script(p: dict, player=None) -> str:
    code = str(p.get("code") or "").strip()
    if not code:
        return "No script given."
    bad = check_code(code)
    if bad:
        return f"I can't run that script, sir: {bad}"
    if find_blender() is None:
        return _NO_BLENDER
    paths, _ = _pick_project(p)
    new = _flag(p.get("new_scene"), False) or paths is None
    if paths is None:
        paths = _paths(_unique_name(p.get("name") or "script"))
    paths["dir"].mkdir(parents=True, exist_ok=True)
    backup = _backup(paths)
    res = _run_blender(paths["blend"], code=code, new=new, timeout=150)
    if not res.get("ok"):
        if backup is not None:
            shutil.copy2(backup, paths["blend"])
            backup.unlink(missing_ok=True)
        return f"The script failed, sir: {res.get('error')}\n{(res.get('trace') or '')[-500:]}"
    _set_current(paths["name"])
    return _finish(p, paths, "Script applied —", res, player)


def _color(p: dict, player=None) -> str:
    kw = {"target": p.get("target") or "all"}
    for key in ("color", "emission"):
        if p.get(key):
            kw[key] = p[key]
    for key in ("metallic", "roughness", "alpha", "emission_strength"):
        if _num(p.get(key)) is not None:
            kw[key] = _num(p[key])
    if len(kw) == 1:
        return "Which colour, sir?"
    return _direct(p, [{"fn": "recolor", "kwargs": kw}], f"Recoloured {kw['target']} —", player)


def _transform(p: dict, player=None) -> str:
    if not p.get("target"):
        return "Which object should I change, sir?"
    kw = {"target": p["target"]}
    for key in ("loc", "rot", "scale", "move", "rotate", "scale_by"):
        v = _vec(p.get(key))
        if v is not None:
            kw[key] = v
    if len(kw) == 1:
        return "Tell me where to move it, how to rotate it or what size to make it."
    return _direct(p, [{"fn": "transform", "kwargs": kw}], f"Changed {kw['target']} —", player)


def _delete(p: dict, player=None) -> str:
    if not p.get("target"):
        return "Which object should I delete, sir?"
    return _direct(p, [{"fn": "delete", "kwargs": {"target": p["target"]}}], f"Deleted {p['target']} —", player)


def _lighting(p: dict, player=None) -> str:
    style = str(p.get("style") or "studio").lower()
    strength = _num(p.get("strength")) or 1.0
    if style in ("sun", "day", "daylight", "outdoor"):
        ops = [{"fn": "clear_lights", "kwargs": {}}, {"fn": "sky", "kwargs": {"color": "#bfdcff", "strength": 1.2}},
               {"fn": "light", "kwargs": {"kind": "SUN", "energy": 4.0 * strength, "rot": [50, 0, 35], "name": "Sun"}}]
    elif style in ("dark", "night", "dramatic", "moody"):
        ops = [{"fn": "clear_lights", "kwargs": {}}, {"fn": "sky", "kwargs": {"color": "#05070d", "strength": 0.25}},
               {"fn": "studio", "kwargs": {"strength": 0.7 * strength}}]
    else:
        ops = [{"fn": "clear_lights", "kwargs": {}}, {"fn": "sky", "kwargs": {"color": "#e8eef5", "strength": 1.0}},
               {"fn": "studio", "kwargs": {"strength": strength}}]
    return _direct(p, ops, f"Lighting set to {style} —", player)


def _background(p: dict, player=None) -> str:
    color = p.get("color") or "#e8eef5"
    return _direct(p, [{"fn": "sky", "kwargs": {"color": color, "strength": _num(p.get("strength")) or 1.0}}], f"Background set to {color} —", player)


def _import(p: dict, player=None) -> str:
    f = str(p.get("file") or "").strip().strip('"')
    if not f or not Path(f).is_file():
        return f"I can't find that file, sir: {f or '(none given)'}"
    paths, _ = _pick_project(p)
    if paths is None:
        name = _unique_name(Path(f).stem)
        paths = _paths(name)
        paths["dir"].mkdir(parents=True, exist_ok=True)
        res = _run_blender(paths["blend"], ops=[{"fn": "import_model", "kwargs": {"path": f}}], new=True, timeout=150)
        if not res.get("ok"):
            return f"Import failed, sir: {res.get('error')}"
        _set_current(name)
        return _finish(p, paths, f"Imported {Path(f).name} —", res, player)
    return _direct(p, [{"fn": "import_model", "kwargs": {"path": f}}], f"Imported {Path(f).name} —", player)


def _list(p: dict, player=None) -> str:
    paths, err = _pick_project(p)
    if paths is None:
        return err
    res = _run_blender(paths["blend"], save=False, timeout=90)
    if not res.get("ok"):
        return f"I couldn't read the project: {res.get('error')}"
    rows = res.get("summary") or []
    lines = [f"{r['name']} ({r['type'].lower()}, {r['dim'][0]}×{r['dim'][1]}×{r['dim'][2]} m)" for r in rows[:40]]
    return f"'{paths['name']}' has {len(rows)} objects: " + "; ".join(lines)


def _render_action(p: dict, player=None) -> str:
    paths, err = _pick_project(p)
    if paths is None:
        return err
    png, rerr = _render(paths, _preview_view(p), _res(p), int(_num(p.get("samples")) or 64))
    if png is None:
        return f"The render failed, sir: {rerr}"
    if _flag(p.get("show"), True):
        open_path(png)
    return f"Rendered the {_preview_view(p)} view: {png}"


def _export(p: dict, player=None) -> str:
    paths, err = _pick_project(p)
    if paths is None:
        return err
    fmt = str(p.get("format") or "glb").lower().lstrip(".")
    if fmt not in ("glb", "gltf", "fbx", "obj", "stl"):
        return "Export formats: glb, gltf, fbx, obj, stl."
    paths["exports"].mkdir(parents=True, exist_ok=True)
    out = unique_path(paths["exports"], paths["name"], f".{fmt}")
    res = _run_blender(paths["blend"], exports=[{"path": str(out), "fmt": fmt}], save=False, timeout=150)
    if res.get("error") == "NO_BLENDER":
        return _NO_BLENDER
    if not res.get("ok") or res.get("export_error") or not out.is_file():
        return f"Export failed, sir: {res.get('export_error') or res.get('error') or 'no file was written'}"
    if _flag(p.get("show"), False):
        open_path(paths["exports"])
    return f"Exported {fmt.upper()}: {out}"


def _open(p: dict, player=None) -> str:
    exe = find_blender()
    if exe is None:
        return _NO_BLENDER
    paths, err = _pick_project(p)
    if paths is None:
        return err
    try:
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) if os.name == "nt" else 0
        subprocess.Popen([str(exe), str(paths["blend"])], creationflags=flags, close_fds=True)
    except Exception as e:                                                 # noqa: BLE001
        return f"I couldn't open Blender: {e}"
    return (f"Opening '{paths['name']}' in Blender, sir. If it was already open, use File → Revert to see my latest changes. "
            f"Save your own edits (Ctrl+S) before asking me to change it again.")


def _undo(p: dict, player=None) -> str:
    paths, err = _pick_project(p)
    if paths is None:
        return err
    files = sorted(paths["history"].glob("*.blend")) if paths["history"].is_dir() else []
    if not files:
        return "There's nothing to undo, sir."
    last = files[-1]
    shutil.copy2(last, paths["blend"])
    last.unlink(missing_ok=True)
    return _finish(p, paths, f"Undid the last change — {len(files) - 1} more step(s) available.", {}, player)


def _projects(p: dict, player=None) -> str:
    names = _project_names()
    if not names:
        return "You have no Blender projects yet, sir."
    cur = _read_state().get("current")
    return "Projects: " + ", ".join(f"{n}{' (current)' if n == cur else ''}" for n in names)


def _switch(p: dict, player=None) -> str:
    want = safe_name(str(p.get("name") or p.get("project") or ""), "", 40).lower()
    names = _project_names()
    hit = next((n for n in names if n.lower() == want), None) or next((n for n in names if want and want in n.lower()), None)
    if not hit:
        return f"No such project. You have: {', '.join(names) or 'none'}."
    _set_current(hit)
    return f"Switched to '{hit}', sir."


def _new(p: dict, player=None) -> str:
    if find_blender() is None:
        return _NO_BLENDER
    name = _unique_name(p.get("name") or "empty scene")
    paths = _paths(name)
    paths["dir"].mkdir(parents=True, exist_ok=True)
    res = _run_blender(paths["blend"], new=True, timeout=90)
    if not res.get("ok"):
        return f"Couldn't create the project: {res.get('error')}"
    _set_current(name)
    return f"Created the empty project '{name}', sir. Tell me what to build in it."


_ALIASES = {
    "build": "create", "make": "create", "generate": "create", "model": "create",
    "modify": "edit", "change": "edit", "add": "edit", "update": "edit", "improve": "edit", "refine": "edit",
    "paint": "color", "colour": "color", "recolor": "color",
    "move": "transform", "rotate": "transform", "scale": "transform", "resize": "transform",
    "remove": "delete", "light": "lighting", "lights": "lighting", "sky": "background",
    "download": "export", "show": "open", "launch": "open", "revert": "undo",
    "inspect": "list", "objects": "list", "describe": "list", "preview": "render", "screenshot": "render",
    "code": "script", "run": "script", "help": "status", "doctor": "status", "load": "switch",
    "list_projects": "projects",
}
_HANDLERS = {
    "status": lambda p, pl: _status(p), "create": _create, "edit": _edit, "script": _script, "color": _color,
    "transform": _transform, "delete": _delete, "lighting": _lighting, "background": _background, "import": _import,
    "list": _list, "render": _render_action, "export": _export, "open": _open, "undo": _undo, "projects": _projects,
    "switch": _switch, "new": _new,
}


def blender_3d(parameters: dict, player=None, speak=None) -> str:
    p = dict(parameters or {})
    action = str(p.get("action") or ("create" if p.get("prompt") else "status")).lower().strip().replace(" ", "_")
    action = _ALIASES.get(action, action)
    handler = _HANDLERS.get(action)
    if handler is None:
        return "Unknown action. Available: " + ", ".join(sorted(_HANDLERS))
    if not _LOCK.acquire(timeout=1):
        return "Blender is still busy with the previous job, sir. One moment."
    try:
        return handler(p, player)
    except Exception as e:                                                 # noqa: BLE001
        return f"Blender action failed: {type(e).__name__}: {e}"
    finally:
        _LOCK.release()


TOOL = {
    "name": "blender_3d",
    "description": (
        "Builds and edits 3D models in Blender (runs Blender in the background, saves a .blend project and shows a rendered preview). "
        "Use for 'create/model/build a 3D <thing> in Blender', 'make the roof red', 'add a door', 'move/scale/rotate/delete <part>', "
        "'change the lighting', 'render it from the front', 'export it as glb/fbx/obj/stl', 'open it in Blender', 'undo'. "
        "action=create (needs prompt = what to build, in English, as detailed as the user said) | edit (prompt = the change to the current model) | "
        "color | transform | delete | lighting | background | render | export | import | open | undo | list | projects | switch | new | status. "
        "For create/edit pass the user's request in prompt; Jarvis writes the Blender script itself, so do NOT write code."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "create | edit | color | transform | delete | lighting | background | render | export | import | open | undo | list | projects | switch | new | status"},
            "prompt": {"type": "STRING", "description": "create/edit: what to build or change, e.g. 'a low-poly pine tree with snow' or 'add a red door and make the walls white'"},
            "name": {"type": "STRING", "description": "project name for create/new/switch (optional)"},
            "image": {"type": "STRING", "description": "create/edit: full path of a reference image (png/jpg/webp) to model after, e.g. a screenshot or a picture the user sent (optional)"},
            "project": {"type": "STRING", "description": "work on this project instead of the current one (optional)"},
            "target": {"type": "STRING", "description": "object name, wildcard like 'Wheel*', comma list, or 'all' (color/transform/delete)"},
            "color": {"type": "STRING", "description": "color name ('red', 'gold') or #rrggbb"},
            "metallic": {"type": "NUMBER", "description": "0 to 1"},
            "roughness": {"type": "NUMBER", "description": "0 (mirror) to 1 (matte)"},
            "emission": {"type": "STRING", "description": "glow color, e.g. '#ffcc66'; 'none' turns glow off"},
            "alpha": {"type": "NUMBER", "description": "1 solid … 0 invisible"},
            "loc": {"type": "ARRAY", "items": {"type": "NUMBER"}, "description": "absolute position [x,y,z] in metres (Z is up)"},
            "move": {"type": "ARRAY", "items": {"type": "NUMBER"}, "description": "relative move [x,y,z] in metres"},
            "rot": {"type": "ARRAY", "items": {"type": "NUMBER"}, "description": "absolute rotation [x,y,z] in degrees"},
            "rotate": {"type": "ARRAY", "items": {"type": "NUMBER"}, "description": "relative rotation [x,y,z] in degrees"},
            "scale": {"type": "ARRAY", "items": {"type": "NUMBER"}, "description": "absolute scale [x,y,z] (1 = original)"},
            "scale_by": {"type": "ARRAY", "items": {"type": "NUMBER"}, "description": "multiply the current scale by [x,y,z], e.g. [2,2,2] = twice as big"},
            "style": {"type": "STRING", "description": "lighting: studio (default) | sun | dark"},
            "view": {"type": "STRING", "description": "render/preview angle: iso (default) | front | back | left | right | top | three_quarter"},
            "format": {"type": "STRING", "description": "export format: glb (default) | gltf | fbx | obj | stl"},
            "file": {"type": "STRING", "description": "import: path of a .glb/.gltf/.fbx/.obj/.stl file"},
            "preview": {"type": "BOOLEAN", "description": "render and show a preview after the change (default true)"},
            "show": {"type": "BOOLEAN", "description": "open the rendered image / exported folder (default true for previews)"},
            "width": {"type": "INTEGER", "description": "render width in pixels (default 960)"},
            "height": {"type": "INTEGER", "description": "render height in pixels (default 720)"},
        },
    },
    "handler": blender_3d,
}

# by ASDcuber521

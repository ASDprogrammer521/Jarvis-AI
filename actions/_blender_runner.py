"""Runs INSIDE Blender:  blender -b [project.blend] --python _blender_runner.py -- args.json

Reads the job (operations, an optional generated script, a render request, exports), applies it to the scene, saves the
project and writes a result.json that blender_3d.py reads. A failed job is NOT saved, so the project is never left half-built.
"""
import json
import linecache
import sys
import traceback


def _load_args():
    argv = sys.argv
    path = argv[argv.index("--") + 1]
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main():
    args = _load_args()
    result = {"ok": False, "error": "", "trace": "", "summary": [], "render": "", "engine": "", "exports": [], "blender": "", "saved": False,
              "render_error": "", "export_error": ""}
    try:
        sys.path.insert(0, args["lib_dir"])
        import bpy
        import _blender_lib as jb

        result["blender"] = getattr(bpy.app, "version_string", "")
        if args.get("new"):
            jb.clear()

        for op in args.get("ops") or []:
            jb.run_op(op["fn"], op.get("kwargs") or {})

        code = args.get("code") or ""
        if code.strip():
            name = "<jarvis_blender>"
            linecache.cache[name] = (len(code), None, code.splitlines(True), name)     # tracebacks can show the failing line
            exec(compile(code, name, "exec"), jb.namespace())

        rnd = args.get("render")
        if args.get("save", True):
            if args.get("stage", True):                                 # lights + camera, so the .blend also looks right when opened by hand
                jb.stage()
                jb.auto_camera("iso")
            jb.save(args["project"])

        result["saved"] = bool(args.get("save", True))
        # the model is built and saved by now: a render or export problem is reported but is not a build failure
        if rnd:
            try:
                result["engine"] = jb.render(rnd["path"], view=rnd.get("view", "iso"), res=tuple(rnd.get("res") or (960, 720)),
                                             samples=int(rnd.get("samples") or 48), transparent=bool(rnd.get("transparent")),
                                             engine=rnd.get("engine") or None)
                result["render"] = rnd["path"]
            except Exception as e:                                      # noqa: BLE001
                result["render_error"] = f"{type(e).__name__}: {e}"

        for ex in args.get("exports") or []:
            try:
                result["exports"].append(jb.export(ex["path"], ex.get("fmt")))
            except Exception as e:                                      # noqa: BLE001
                result["export_error"] = f"{type(e).__name__}: {e}"

        result["summary"] = jb.describe()
        result["ok"] = True
    except BaseException as e:                                          # noqa: BLE001 - report everything back to Jarvis
        result["error"] = f"{type(e).__name__}: {e}"
        result["trace"] = traceback.format_exc()[-2500:]
    finally:
        try:
            with open(args["result"], "w", encoding="utf-8") as fh:
                json.dump(result, fh)
        except Exception:                                               # noqa: BLE001
            pass


main()

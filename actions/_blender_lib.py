"""jb — Jarvis' helper library for Blender.

It runs INSIDE Blender (3.6 … 5.x) and is imported by _blender_runner.py; it is NOT a normal Jarvis action (the
leading underscore keeps the action loader from touching it). It hides the Blender API differences between versions so the
scripts Jarvis writes stay short and reliable.
"""
from __future__ import annotations

import fnmatch
import math
import os
import random
import re

import bmesh
import bpy
from mathutils import Euler, Matrix, Vector

# The text the LLM sees (blender_3d.py reads it straight from this file with ast, so keep it a plain string literal).
API_REFERENCE = """
Everything is called as jb.<name>(...). Z is UP, units are metres, angles are in DEGREES. Colours are a name ("red",
"gold", "wood", "concrete"…), "#rrggbb" or (r,g,b) in 0..1. Every shape returns the Blender object.

COMMON KEYWORDS of every shape: name="Door", loc=(x,y,z) world position of its CENTRE, rot=(rx,ry,rz), scale=(sx,sy,sz),
color="red", metallic=0..1, roughness=0..1, material=<jb.mat(...)>, parent=<object or group>, bottom=0 (put its lowest
point at that height – the easy way to stand things on the ground), smooth=True/False.

SHAPES
 jb.box(size=(x,y,z))                          cuboid                 (smooth defaults False)
 jb.sphere(r=0.5, segments=32, rings=16)       use scale=(a,b,c) for an ellipsoid
 jb.ico(r=0.5, subdiv=2)                       low-poly / rock-like sphere
 jb.cylinder(r=0.5, h=1.0, verts=32)           axis along Z
 jb.cone(r1=0.5, r2=0.0, h=1.0, verts=32)      r1 = bottom radius, r2 = top radius (r2>0 makes a frustum)
 jb.torus(R=1.0, r=0.25, major=48, minor=16)   ring / donut / wheel tyre (lies flat, axis Z)
 jb.plane(size=(x,y))                          flat rectangle facing up
 jb.lathe(profile, steps=48)                   spin a profile [(radius, z), …] round the Z axis: vases, bottles, cups, bowls,
                                               chess pieces, lamp shades, columns. Start AND end at radius 0 for a closed solid;
                                               use smooth=False for sharp profile corners.
 jb.extrude_poly(points, depth=1.0)            2D outline [(x,y), …] extruded upward from z=0: floor plans, gears, stars,
                                               letters, roof gables, hearts, arrows
 jb.tube(points, radius=0.05, smooth_path=False, closed=False)   pipe / wire / rope / tree branch along 3D points [(x,y,z), …]
 jb.text(body, size=1.0, depth=0.1)            3D text; rot=(90,0,0) makes it stand upright facing the camera
 jb.ground(size=40, color="#8a8f98")           a big floor (ignored when the camera is framed)
 jb.group(name, children=(), loc=(0,0,0))      an empty that holds parts, so the whole thing can be moved / scaled together

LOW-POLY CHARACTERS (use these for people, zombies, robots, monsters, animals' legs, spikes – they look far better than boxes)
 jb.humanoid(name="Human", style="human"|"zombie"|"robot", pose="stand"|"zombie"|"tpose"|"walk"|"run"|"wave", height=1.75, build=1.0,
             skin=None, shirt=None, pants=None, shoes=None, belt=None, eye_glow=None, hair=None, sleeves="short"|"long"|"none",
             loc=(0,0,0), facing=0)
     a complete faceted figure on the ground (head, jaw, nose, glowing eyes, torso, belt, arms, hands with fingers, legs, shoes)
     returned as one group. Colours default from style; override any of them. Parts are named <name>_Head, <name>_ForeArmR ...
     so jb.recolor("Zombie_Shirt*", "red") or jb.transform("Zombie_Head", rot=(0,0,20)) still work afterwards.
 jb.limb(a, b, r1=0.05, r2=None, sides=6, squash=1.0, jitter=0.0)   tapered faceted prism from point a to point b (radius r1 -> r2):
     arms, legs, necks, tails, horns, spikes, fingers, beaks. a and b are (x,y,z). Use r2=0.0... as a tiny number like 0.003 for a tip.
 jb.blob(center, size=(rx,ry,rz), subdiv=1, jitter=0.0)             faceted ellipsoid: heads, hands, feet, rocks, fruit, joints
 jb.showcase(bg="#2f4a3a", strength=0.8)    nice finished look: coloured background + key/fill/rim lights (call once at the END)

MATERIALS
 jb.mat(name, color="gray", metallic=0, roughness=0.5, emission=None, emission_strength=3, alpha=1, glass=False, ior=1.45)
     emission="#ffd080" makes it glow; glass=True makes clear glass; alpha<1 makes it see-through.
     Pass the result as material=… or use jb.assign(obj, material).

MODIFIERS (non-destructive; applied when possible)
 jb.bevel(obj, width=0.02, segments=3)    rounded edges – use on most hard-surface parts, it looks far better
 jb.subsurf(obj, levels=2)                smooth organic surface
 jb.array(obj, count=5, offset=(1,0,0), relative=False)    repeat (fences, windows, stairs, teeth)
 jb.mirror(obj, axis="X")                 symmetric parts (build half, mirror it)
 jb.solidify(obj, thickness=0.05)         give a flat surface thickness (shells, cloth, leaves)
 jb.boolean(obj, other, op="DIFFERENCE")  cut a hole ("DIFFERENCE"), merge ("UNION") or overlap ("INTERSECT"); the cutter is hidden
 jb.apply_modifiers(obj)
 jb.join([objs], name="Merged")           merge several meshes into one object

COPIES
 jb.duplicate(obj, name=None, loc=…, rot=…, scale=…, linked=False)
 jb.ring(obj, count=8, radius=1.0, center=(0,0,0), face_out=True)   spreads count objects on a circle (the original takes slot 1;
                                                                    face_out makes each local +X point away from the centre)
 jb.grid(obj, nx=3, ny=3, dx=1.0, dy=1.0)                           nx*ny objects on a grid starting at the original's position

CHANGING WHAT EXISTS (targets: an object name, "Wing*" wildcards, a part of a name, a list, or "all")
 jb.recolor(target, color=None, metallic=None, roughness=None, emission=None, emission_strength=None, alpha=None)
 jb.transform(target, loc=None, rot=None, scale=None, move=None, rotate=None, scale_by=None)    (move/rotate/scale_by are relative)
 jb.delete(target)    jb.rename(old, new)    jb.hide(target, hide=True)    jb.set_smooth(target, smooth=True)
 jb.find(target) -> list of objects     jb.describe() -> list of dicts (name, type, loc, dim, materials, parent)

STAGE (lights, camera and world are added automatically at render time – only touch them for a specific look)
 jb.sky(color="#e8eef5", strength=1.0)    world background colour / brightness
 jb.studio(strength=1.0)                  3-point studio lighting framed to the model
 jb.light(kind="POINT"|"SUN"|"AREA"|"SPOT", loc=(0,0,3), energy=None, color="white", rot=None, target=None, size=None, name=None)
 jb.bounds() -> (min, max) corner Vectors of everything that is not ground / light / camera
"""

_STAGE = "jb_stage"
_GROUND = "jb_ground"
_GEOMETRY = ("MESH", "CURVE", "FONT", "SURFACE", "META")

NAMED = {
    "red": "#d62828", "green": "#2e9e44", "blue": "#2563eb", "yellow": "#f5c518", "orange": "#f77f00",
    "purple": "#7b2cbf", "violet": "#8e44ad", "pink": "#ff6fa5", "hotpink": "#ff3d8b", "brown": "#7a4a21",
    "black": "#101010", "white": "#f4f4f4", "gray": "#8a8a8a", "grey": "#8a8a8a", "darkgray": "#3c3c3c",
    "darkgrey": "#3c3c3c", "lightgray": "#c8c8c8", "lightgrey": "#c8c8c8", "cyan": "#22c7d6", "magenta": "#d4249b",
    "gold": "#d4a017", "silver": "#c0c0c8", "bronze": "#a9692b", "copper": "#b8602f", "steel": "#9aa3ad",
    "iron": "#4b4f54", "wood": "#9b6a3c", "darkwood": "#5a3820", "lightwood": "#c99a64", "oak": "#a87b45",
    "skin": "#e0ac84", "sand": "#d9c28c", "grass": "#4f9a3a", "leaf": "#3d8b37", "darkgreen": "#1f5a2a",
    "sky": "#87c5f0", "navy": "#14213d", "teal": "#16a3a3", "maroon": "#7a1f2b", "olive": "#6b7a2a", "lime": "#8fd12f",
    "turquoise": "#2dd4bf", "beige": "#e3d2b0", "ivory": "#f3eedb", "cream": "#f6ecd2", "concrete": "#9d9d98",
    "brick": "#9c4a3a", "stone": "#7d7a74", "rock": "#6f6b64", "snow": "#fbfdff", "ice": "#bfe6f5", "water": "#2f7fc1",
    "lava": "#ff4d00", "rubber": "#1c1c1c", "plastic": "#e8e8e8", "glass": "#cfe9f2", "tan": "#c9a36b",
    "lavender": "#b8a2e0", "mint": "#9ce5c0", "coral": "#ff7f6e", "crimson": "#a50f2c", "indigo": "#3b2f8f",
}


# ---------------------------------------------------------------- small helpers

def _scene():
    return bpy.context.scene


_EXTRA_COLOURS = {
    "dough": "#d9a566", "bread": "#c98a4b", "frosting": "#ff8fb8", "icing": "#ff9ec4", "sprinkle": "#ffd23f",
    "chocolate": "#4a2a17", "caramel": "#b7752e", "candy": "#ff5fa2", "cake": "#f2d09a", "cookie": "#c08a4a",
    "strawberry": "#e8344f", "banana": "#f5d442", "apple": "#c8202f", "cherry": "#8b0a1e", "milk": "#f7f4ee",
    "metal": "#9aa3ad", "chrome": "#d8dce0", "aluminum": "#c4c8cc", "aluminium": "#c4c8cc", "paint": "#c8c8c8",
    "fabric": "#8a7f72", "leather": "#5b3a24", "paper": "#f1ead6", "bone": "#e6dfc8", "marble": "#e9e7e2",
    "ceramic": "#ece8e0", "clay": "#b0643c", "mud": "#5a4632", "dirt": "#6b5239", "soil": "#5a4128", "flesh": "#e0ac84",
    "fire": "#ff6a00", "flame": "#ff8a1c", "light": "#fff2c0", "dark": "#222222", "bark": "#5a3d28", "trunk": "#6b4a2e",
    "pink": "#ff7fb0", "hotpink": "#ff4d94", "rose": "#e8588a", "peach": "#ffcba4", "salmon": "#fa8072",
    "yellow": "#f2cf1d", "orange": "#f08a1c", "purple": "#7a3fb5", "violet": "#8a4fd0", "brown": "#7a4a28",
    "blue": "#2a5ad6", "green": "#2f9e44", "red": "#d62828", "darkred": "#7a1010", "darkblue": "#15286b",
    "lightblue": "#8cc4ee", "lightgreen": "#9be28a", "khaki": "#c3b37a", "amber": "#ffb000", "rust": "#a64a1f",
}


def _guess_colour(word) -> str:
    """Hex (no #) for an unknown colour word: known names inside it, then the closest spelling, otherwise neutral grey."""
    key = "".join(ch for ch in str(word).lower() if ch.isalnum())
    table = {**_EXTRA_COLOURS, **{k: v for k, v in NAMED.items()}}
    if key in table:
        return table[key].lstrip("#")[:6]
    hits = [k for k in table if len(k) >= 3 and (k in key or key in k)]
    if hits:
        return table[max(hits, key=len)].lstrip("#")[:6]
    import difflib
    close = difflib.get_close_matches(key, list(table), n=1, cutoff=0.6)
    return (table[close[0]] if close else "#8a8a8a").lstrip("#")[:6]


_OBJ_REPR = re.compile(r"""objects\s*(?:\[\s*|\.get\(\s*)['"](.+?)['"]""")
_OBJ_REPR2 = re.compile(r"""Object\(\s*['"](.+?)['"]""")


def _name_of(spec):
    """Strip things like "bpy.data.objects['Frosting']" or "<bpy_struct, Object("Frosting")>" down to the plain name."""
    if not isinstance(spec, str):
        return spec
    for rx in (_OBJ_REPR, _OBJ_REPR2):
        m = rx.search(spec)
        if m:
            return m.group(1)
    return spec.strip()


def _ob(x):
    """Accept an object, a name (also in bpy.data.objects['X'] form) or a one-item list and return the Blender object."""
    if isinstance(x, (list, tuple)) and len(x) == 1:
        x = x[0]
    if isinstance(x, str):
        return find(x)[0]
    return x


def _lin(v: float) -> float:
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def col(c, alpha: float = 1.0):
    """Any colour spec -> linear RGBA for Blender."""
    if c is None:
        return None
    if isinstance(c, str):
        key = c.strip().lower().replace(" ", "").replace("_", "").replace("-", "")
        key = NAMED.get(key, key).lstrip("#")
        if len(key) == 3:
            key = "".join(ch * 2 for ch in key)
        try:
            if len(key) not in (6, 8):
                raise ValueError
            rgb = [int(key[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
            a = int(key[6:8], 16) / 255.0 if len(key) == 8 else alpha
        except ValueError:
            guess = _guess_colour(c)                                    # never fail a whole model over a colour word
            rgb = [int(guess[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
            a = alpha
    else:
        vals = [float(x) for x in c]
        if len(vals) < 3:
            raise ValueError(f"colour needs 3 numbers, got {c!r}")
        if max(vals[:3]) > 1.0:
            vals = [v / 255.0 for v in vals]
        rgb = vals[:3]
        a = vals[3] if len(vals) > 3 else alpha
    return (_lin(rgb[0]), _lin(rgb[1]), _lin(rgb[2]), a)


def _v3(v, default: float = 0.0):
    if v is None:
        return (default, default, default)
    if isinstance(v, (int, float)):
        return (float(v), float(v), float(v))
    vals = [float(x) for x in v]
    if len(vals) == 1:
        return (vals[0], vals[0], vals[0])
    if len(vals) == 2:
        return (vals[0], vals[1], default)
    return (vals[0], vals[1], vals[2])


def _slug(text) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in str(text))[:24].strip("_") or "c"


def _link(ob):
    _scene().collection.objects.link(ob)
    return ob


def _update():
    try:
        bpy.context.view_layer.update()
    except Exception:                                                   # noqa: BLE001
        pass


# ---------------------------------------------------------------- materials

def _set(node, names, value) -> bool:
    for n in names:
        sock = node.inputs.get(n)
        if sock is None:
            continue
        try:
            sock.default_value = value
            return True
        except Exception:                                               # noqa: BLE001
            continue
    return False


def _bsdf(m):
    m.use_nodes = True
    nt = m.node_tree
    node = next((n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if node is None:
        node = nt.nodes.new("ShaderNodeBsdfPrincipled")
        out = next((n for n in nt.nodes if n.type == "OUTPUT_MATERIAL"), None) or nt.nodes.new("ShaderNodeOutputMaterial")
        nt.links.new(node.outputs["BSDF"], out.inputs["Surface"])
    return node


def _apply_props(m, color=None, metallic=None, roughness=None, emission=None, emission_strength=None,
                 alpha=None, glass=None, ior=None):
    b = _bsdf(m)
    if color is not None:
        rgba = col(color)
        _set(b, ("Base Color",), rgba)
        m.diffuse_color = rgba
    if metallic is not None:
        _set(b, ("Metallic",), float(metallic))
        try:
            m.metallic = float(metallic)
        except Exception:                                               # noqa: BLE001
            pass
    if roughness is not None:
        _set(b, ("Roughness",), float(roughness))
        try:
            m.roughness = float(roughness)
        except Exception:                                               # noqa: BLE001
            pass
    if glass is not None:
        _set(b, ("Transmission Weight", "Transmission"), 1.0 if glass else 0.0)
        if glass:
            _set(b, ("Roughness",), 0.05)
    if ior is not None:
        _set(b, ("IOR",), float(ior))
    if emission is not None:
        if emission is False or (isinstance(emission, str) and emission.lower() in ("none", "off", "")):
            _set(b, ("Emission Strength",), 0.0)
        else:
            _set(b, ("Emission Color", "Emission"), col(emission))
            _set(b, ("Emission Strength",), 3.0 if emission_strength is None else float(emission_strength))
    elif emission_strength is not None:
        _set(b, ("Emission Strength",), float(emission_strength))
    if alpha is not None:
        a = float(alpha)
        _set(b, ("Alpha",), a)
        if a < 1.0:
            for attr, val in (("blend_method", "BLEND"), ("surface_render_method", "BLENDED")):
                try:
                    setattr(m, attr, val)
                except Exception:                                       # noqa: BLE001
                    pass


def mat(name, color="gray", metallic=0.0, roughness=0.5, emission=None, emission_strength=3.0, alpha=1.0,
        glass=False, ior=1.45):
    m = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    _apply_props(m, color=color, metallic=metallic, roughness=roughness, emission=emission if emission is not None else False,
                 emission_strength=emission_strength, alpha=alpha, glass=bool(glass), ior=ior)
    return m


def _auto_mat(color, metallic, roughness):
    label = color if isinstance(color, str) else "rgb" + "".join(f"{int(float(v) * 255):02x}" for v in list(color)[:3])
    name = f"{_slug(label)}_m{metallic:g}_r{roughness:g}"
    return mat(name, color=color, metallic=metallic, roughness=roughness)


def assign(ob, material):
    ob = _ob(ob)
    if isinstance(material, str):
        found = bpy.data.materials.get(material)
        if found is None:
            raise ValueError(f"no material called {material!r}")
        material = found
    data = getattr(ob, "data", None)
    if data is not None and hasattr(data, "materials"):
        data.materials.clear()
        data.materials.append(material)
    return ob


# ---------------------------------------------------------------- placing objects

def _min_z(ob) -> float:
    mw = Matrix.LocRotScale(Vector(ob.location), Euler(ob.rotation_euler), Vector(ob.scale))
    return min((mw @ Vector(c)).z for c in ob.bound_box)


def _parent(ob, parent):
    ob, parent = _ob(ob), _ob(parent)
    _update()
    ob.parent = parent
    ob.matrix_parent_inverse = parent.matrix_world.inverted()


def _smooth_polys(ob, mode):
    me = ob.data
    if mode == "sides":
        for poly in me.polygons:
            poly.use_smooth = abs(poly.normal.z) < 0.9999
    else:
        for poly in me.polygons:
            poly.use_smooth = True


def _place(ob, name=None, loc=None, rot=None, scale=None, color=None, material=None, metallic=None, roughness=None,
           smooth=False, parent=None, bottom=None):
    if name:
        ob.name = str(name)
    if loc is not None:
        ob.location = _v3(loc)
    if rot is not None:
        ob.rotation_euler = tuple(math.radians(a) for a in _v3(rot))
    if scale is not None:
        ob.scale = _v3(scale, 1.0)
    if material is not None:
        assign(ob, material)
    elif color is not None:
        assign(ob, _auto_mat(color, 0.0 if metallic is None else float(metallic), 0.5 if roughness is None else float(roughness)))
    elif metallic is not None or roughness is not None:
        assign(ob, _auto_mat("gray", 0.0 if metallic is None else float(metallic), 0.5 if roughness is None else float(roughness)))
    if smooth and ob.type == "MESH":
        _smooth_polys(ob, smooth)
    if bottom is not None and ob.type in _GEOMETRY:
        ob.location.z += float(bottom) - _min_z(ob)
    if parent is not None:
        _parent(ob, parent)
    return ob


def _mesh_obj(name, bm):
    me = bpy.data.meshes.new(str(name))
    bm.to_mesh(me)
    bm.free()
    return _link(bpy.data.objects.new(str(name), me))


def _pydata_obj(name, verts, faces, recalc=True):
    me = bpy.data.meshes.new(str(name))
    me.from_pydata(verts, [], faces)
    me.update()
    if recalc:
        bm = bmesh.new()
        bm.from_mesh(me)
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
        bm.to_mesh(me)
        bm.free()
    return _link(bpy.data.objects.new(str(name), me))


# ---------------------------------------------------------------- shapes

def box(size=(1, 1, 1), name="Box", smooth=False, **kw):
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    bmesh.ops.scale(bm, vec=Vector(_v3(size, 1.0)), verts=bm.verts)
    return _place(_mesh_obj(name, bm), name=name, smooth=smooth, **kw)


def sphere(r=0.5, segments=32, rings=16, name="Sphere", smooth=True, **kw):
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=int(segments), v_segments=int(rings), radius=float(r))
    return _place(_mesh_obj(name, bm), name=name, smooth=smooth, **kw)


def ico(r=0.5, subdiv=2, name="Ico", smooth=False, **kw):
    bm = bmesh.new()
    bmesh.ops.create_icosphere(bm, subdivisions=int(subdiv), radius=float(r))
    return _place(_mesh_obj(name, bm), name=name, smooth=smooth, **kw)


def _cone_bm(r1, r2, h, verts):
    bm = bmesh.new()
    try:
        bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=int(verts), radius1=float(r1), radius2=float(r2), depth=float(h))
    except TypeError:                                                   # very old Blender: diameters
        bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=int(verts), diameter1=float(r1), diameter2=float(r2), depth=float(h))
    return bm


def cylinder(r=0.5, h=1.0, verts=32, name="Cylinder", smooth=True, **kw):
    return _place(_mesh_obj(name, _cone_bm(r, r, h, verts)), name=name, smooth="sides" if smooth else False, **kw)


def cone(r1=0.5, r2=0.0, h=1.0, verts=32, name="Cone", smooth=True, **kw):
    return _place(_mesh_obj(name, _cone_bm(r1, r2, h, verts)), name=name, smooth="sides" if smooth else False, **kw)


def torus(R=1.0, r=0.25, major=48, minor=16, name="Torus", smooth=True, **kw):
    major, minor = int(major), int(minor)
    verts = []
    for i in range(major):
        a = 2 * math.pi * i / major
        for j in range(minor):
            b = 2 * math.pi * j / minor
            ring = float(R) + float(r) * math.cos(b)
            verts.append((ring * math.cos(a), ring * math.sin(a), float(r) * math.sin(b)))
    faces = []
    for i in range(major):
        for j in range(minor):
            faces.append((i * minor + j, i * minor + (j + 1) % minor,
                          ((i + 1) % major) * minor + (j + 1) % minor, ((i + 1) % major) * minor + j))
    return _place(_pydata_obj(name, verts, faces), name=name, smooth=smooth, **kw)


def plane(size=(1, 1), name="Plane", smooth=False, **kw):
    sx, sy, _ = _v3(size, 1.0)
    x, y = sx / 2, sy / 2
    return _place(_pydata_obj(name, [(-x, -y, 0), (x, -y, 0), (x, y, 0), (-x, y, 0)], [(0, 1, 2, 3)], recalc=False),
                  name=name, smooth=smooth, **kw)


def lathe(profile, steps=48, name="Lathe", smooth=True, **kw):
    pts = [(float(p[0]), float(p[1])) for p in profile]
    if len(pts) < 2:
        raise ValueError("lathe needs at least 2 profile points [(radius, z), ...]")
    steps = max(3, int(steps))
    verts, rows = [], []
    for r, z in pts:
        if r <= 1e-6:
            verts.append((0.0, 0.0, z))
            rows.append([len(verts) - 1])
        else:
            idx = []
            for k in range(steps):
                a = 2 * math.pi * k / steps
                verts.append((r * math.cos(a), r * math.sin(a), z))
                idx.append(len(verts) - 1)
            rows.append(idx)
    faces = []
    for a, b in zip(rows, rows[1:]):
        if len(a) == 1 and len(b) == 1:
            continue
        for k in range(steps):
            k2 = (k + 1) % steps
            if len(a) == 1:
                faces.append((a[0], b[k2], b[k]))
            elif len(b) == 1:
                faces.append((a[k], a[k2], b[0]))
            else:
                faces.append((a[k], a[k2], b[k2], b[k]))
    return _place(_pydata_obj(name, verts, faces), name=name, smooth=smooth, **kw)


def extrude_poly(points, depth=1.0, name="Shape", smooth=False, **kw):
    pts = [(float(p[0]), float(p[1])) for p in points]
    n = len(pts)
    if n < 3:
        raise ValueError("extrude_poly needs at least 3 points [(x, y), ...]")
    d = float(depth)
    verts = [(x, y, 0.0) for x, y in pts] + [(x, y, d) for x, y in pts]
    faces = [tuple(range(n - 1, -1, -1)), tuple(range(n, 2 * n))]
    for i in range(n):
        j = (i + 1) % n
        faces.append((i, j, n + j, n + i))
    return _place(_pydata_obj(name, verts, faces), name=name, smooth=smooth, **kw)


def tube(points, radius=0.05, smooth_path=False, closed=False, name="Tube", **kw):
    pts = [_v3(p) for p in points]
    if len(pts) < 2:
        raise ValueError("tube needs at least 2 points [(x, y, z), ...]")
    cu = bpy.data.curves.new(str(name), "CURVE")
    cu.dimensions = "3D"
    cu.bevel_depth = float(radius)
    cu.bevel_resolution = 4
    cu.fill_mode = "FULL"
    try:
        cu.use_fill_caps = True
    except Exception:                                                   # noqa: BLE001
        pass
    sp = cu.splines.new("NURBS" if smooth_path else "POLY")
    sp.points.add(len(pts) - 1)
    for p, (x, y, z) in zip(sp.points, pts):
        p.co = (x, y, z, 1.0)
    if smooth_path:
        sp.order_u = min(4, len(pts))
        sp.use_endpoint_u = True
    sp.use_cyclic_u = bool(closed)
    return _place(_link(bpy.data.objects.new(str(name), cu)), name=name, **kw)


def text(body, size=1.0, depth=0.1, name="Text", align="CENTER", **kw):
    cu = bpy.data.curves.new(str(name), "FONT")
    cu.body = str(body)
    cu.size = float(size)
    cu.extrude = float(depth) / 2.0
    cu.align_x = align
    return _place(_link(bpy.data.objects.new(str(name), cu)), name=name, **kw)


def ground(size=40.0, color="#8a8f98", name="Ground", roughness=0.9, z=0.0):
    ob = plane((size, size), name=name, color=color, roughness=roughness, loc=(0, 0, z))
    ob[_GROUND] = True
    return ob


def group(name="Group", children=(), loc=(0, 0, 0)):
    g = _link(bpy.data.objects.new(str(name), None))
    g.location = _v3(loc)
    for c in children:
        _parent(c, g)
    return g


# ---------------------------------------------------------------- low-poly characters

def limb(a, b, r1=0.05, r2=None, sides=6, squash=1.0, jitter=0.0, caps=True, seed=0, name="Limb", smooth=False, **kw):
    """A tapered faceted prism from point a to point b (radius r1 at a, r2 at b): arms, legs, torso, neck, fingers, horns, spikes."""
    A, B = Vector(_v3(a)), Vector(_v3(b))
    axis = B - A
    if axis.length < 1e-6:
        raise ValueError("limb: the two end points are the same")
    d = axis.normalized()
    ref = Vector((1, 0, 0)) if abs(d.z) > 0.9 else Vector((0, 0, 1))
    u = d.cross(ref).normalized()
    w = d.cross(u).normalized()
    r1 = float(r1)
    r2 = r1 if r2 is None else float(r2)
    n = max(3, int(sides))
    rng = random.Random(int(seed))
    verts = []
    for centre, r in ((A, r1), (B, r2)):
        for i in range(n):
            t = 2 * math.pi * i / n + math.pi / n
            k = 1.0 + (rng.uniform(-jitter, jitter) if jitter else 0.0)
            p = centre + u * (math.cos(t) * r * k * float(squash)) + w * (math.sin(t) * r * k)
            if jitter:
                p = p + d * rng.uniform(-jitter, jitter) * max(r1, r2)
            verts.append(tuple(p))
    faces = [[i, (i + 1) % n, n + (i + 1) % n, n + i] for i in range(n)]
    if caps:
        faces.append(list(range(n - 1, -1, -1)))
        faces.append([n + i for i in range(n)])
    return _place(_pydata_obj(name, verts, faces), name=name, smooth=smooth, **kw)


def blob(center=(0, 0, 0), size=(0.1, 0.1, 0.1), subdiv=1, jitter=0.0, seed=0, name="Blob", **kw):
    """A faceted rock-like ellipsoid with half-sizes `size` around `center`: heads, hands, feet, joints, stones, fruit."""
    ob = ico(r=1.0, subdiv=max(1, int(subdiv)), name=name, smooth=False, scale=_v3(size, 1.0), **kw)
    if jitter:
        rng = random.Random(int(seed))
        for v in ob.data.vertices:
            v.co = v.co * (1.0 + rng.uniform(-jitter, jitter))
    ob.location = _v3(center)
    return ob


_STYLES = {
    "zombie": dict(skin="#7f9c52", shirt="#6f8548", pants="#6a4a2c", shoes="#2a2118", belt="#241a12", eye_glow="#ff9a1a", hair=None),
    "human": dict(skin="#e0ac84", shirt="#3a6ea5", pants="#2f3b52", shoes="#222222", belt="#1c1c1c", eye_glow=None, hair="#3b2a1a"),
    "robot": dict(skin="#aab2bb", shirt="#7d8791", pants="#5d6670", shoes="#2e3338", belt="#ffb000", eye_glow="#38d6ff", hair=None),
}

_L, _R = -1, 1


def _pose_table():
    # (out, forward, up) direction of each segment; "out" points away from the body, "forward" is the way the figure faces.
    stand_arm = ((0.12, 0.0, -1.0), (0.05, 0.12, -1.0))
    stand_leg = ((0.04, 0.0, -1.0), (0.0, 0.0, -1.0))
    return {
        "stand": {"arm": {_R: stand_arm, _L: stand_arm}, "leg": {_R: stand_leg, _L: stand_leg}},
        "tpose": {"arm": {_R: ((1, 0, 0), (1, 0, 0)), _L: ((1, 0, 0), (1, 0, 0))}, "leg": {_R: stand_leg, _L: stand_leg}},
        "zombie": {"arm": {_R: ((0.22, 0.85, -0.45), (0.08, 1.0, 0.05)), _L: ((0.22, 0.85, -0.45), (0.08, 1.0, 0.08))},
                   "leg": {_R: ((0.05, 0.28, -1.0), (0.0, -0.08, -1.0)), _L: ((0.05, -0.18, -1.0), (0.0, -0.3, -1.0))}},
        "walk": {"arm": {_R: ((0.1, -0.35, -1.0), (0.05, -0.2, -1.0)), _L: ((0.1, 0.45, -1.0), (0.05, 0.6, -0.8))},
                 "leg": {_R: ((0.0, 0.45, -1.0), (0.0, 0.05, -1.0)), _L: ((0.0, -0.4, -1.0), (0.0, -0.55, -0.95))}},
        "wave": {"arm": {_R: ((0.7, 0.0, 0.7), (0.2, 0.0, 1.0)), _L: stand_arm}, "leg": {_R: stand_leg, _L: stand_leg}},
        "run": {"arm": {_R: ((0.1, -0.7, -0.6), (0.0, 0.5, -0.9)), _L: ((0.1, 0.8, -0.5), (0.0, 0.9, 0.3))},
                "leg": {_R: ((0.0, 0.8, -0.7), (0.0, 0.1, -1.0)), _L: ((0.0, -0.6, -0.8), (0.0, -1.0, -0.4))}},
    }


def humanoid(name="Human", style="human", pose="stand", height=1.75, build=1.0, skin=None, shirt=None, pants=None, shoes=None,
             belt=None, eye_glow=None, hair=None, sleeves="short", jitter=0.04, seed=3, loc=(0, 0, 0), facing=0.0):
    """A complete low-poly person standing on z=0, made of faceted tapered parts and returned as one group.

    style : "human" | "zombie" | "robot" (sets the colours; every colour can still be overridden)
    pose  : "stand" | "zombie" (arms out front, shuffling) | "tpose" | "walk" | "run" | "wave"
    height: metres.  build: 1.0 normal, 1.2 broad, 0.85 thin.  sleeves: "short" | "long" | "none".
    The figure faces -Y (towards the default camera). facing=180 turns it round. Parts are named <name>_Head, <name>_Torso ...
    """
    st = _STYLES.get(str(style).lower(), _STYLES["human"])
    skin_c, shirt_c, pants_c = skin or st["skin"], shirt or st["shirt"], pants or st["pants"]
    shoe_c, belt_c = shoes or st["shoes"], belt or st["belt"]
    glow = eye_glow if eye_glow is not None else st["eye_glow"]
    hair_c = hair if hair is not None else st["hair"]
    robot = str(style).lower() == "robot"
    metal = 0.8 if robot else 0.0
    s, b = float(height) / 1.75, float(build)
    table = _pose_table()
    pz = table.get(str(pose).lower(), table["stand"])

    m_skin = mat(f"{name}_Skin", color=skin_c, metallic=metal, roughness=0.35 if robot else 0.85)
    m_shirt = mat(f"{name}_Shirt", color=shirt_c, metallic=metal * 0.6, roughness=0.5 if robot else 0.9)
    m_pants = mat(f"{name}_Pants", color=pants_c, metallic=metal * 0.6, roughness=0.5 if robot else 0.9)
    m_shoe = mat(f"{name}_Shoes", color=shoe_c, roughness=0.7)
    m_belt = mat(f"{name}_Belt", color=belt_c, metallic=0.5, roughness=0.4)
    parts = []

    def add(o):
        parts.append(o)
        return o

    def unit(v, side):
        out, fwd, up = v
        return Vector((side * out, -fwd, up)).normalized()

    j = float(jitter)
    # torso, hips, belt, neck, head
    add(limb((0, 0, 0.93 * s), (0, 0, 1.44 * s), 0.150 * s * b, 0.195 * s * b, sides=8, squash=0.62, jitter=j, seed=seed, name=f"{name}_Torso", material=m_shirt))
    add(limb((0, 0, 0.84 * s), (0, 0, 0.98 * s), 0.160 * s * b, 0.150 * s * b, sides=8, squash=0.66, jitter=j, seed=seed + 1, name=f"{name}_Hips", material=m_pants))
    add(limb((0, 0, 0.935 * s), (0, 0, 0.985 * s), 0.168 * s * b, 0.168 * s * b, sides=8, squash=0.67, name=f"{name}_Belt", material=m_belt))
    add(limb((0, 0, 1.43 * s), (0, 0, 1.57 * s), 0.052 * s, 0.045 * s, sides=6, name=f"{name}_Neck", material=m_skin))
    add(blob((0, 0, 1.67 * s), (0.100 * s, 0.112 * s, 0.128 * s), 1, j * 1.5, seed + 2, f"{name}_Head", material=m_skin))
    add(blob((0, -0.025 * s, 1.60 * s), (0.068 * s, 0.075 * s, 0.055 * s), 1, j, seed + 3, f"{name}_Jaw", material=m_skin))
    add(blob((0, -0.112 * s, 1.655 * s), (0.016 * s, 0.026 * s, 0.032 * s), 0, 0, 0, f"{name}_Nose", material=m_skin))
    if hair_c:
        add(blob((0, 0.012 * s, 1.735 * s), (0.106 * s, 0.116 * s, 0.07 * s), 1, j, seed + 4, f"{name}_Hair", color=hair_c, roughness=0.9))
    m_eye = mat(f"{name}_Eye", color=glow or "#141414", emission=glow, emission_strength=8.0, roughness=0.4) if glow else mat(f"{name}_Eye", color="#141414", roughness=0.3)
    for sd, tag in ((_R, "R"), (_L, "L")):
        add(blob((sd * 0.042 * s, -0.098 * s, 1.685 * s), (0.017 * s, 0.012 * s, 0.014 * s), 0, 0, 0, f"{name}_Eye{tag}", material=m_eye))

    # arms
    for sd, tag in ((_R, "R"), (_L, "L")):
        sh = Vector((sd * 0.215 * s * b, 0, 1.455 * s))
        up_dir, fo_dir = pz["arm"][sd]
        el = sh + unit(up_dir, sd) * 0.30 * s
        wr = el + unit(fo_dir, sd) * 0.27 * s
        sleeve_m = m_skin if sleeves == "none" else m_shirt
        fore_m = m_shirt if sleeves == "long" else m_skin
        add(blob(tuple(sh), (0.062 * s * b,) * 3, 1, j, seed + 5, f"{name}_Shoulder{tag}", material=sleeve_m))
        add(limb(sh, el, 0.052 * s * b, 0.042 * s * b, sides=6, jitter=j, seed=seed + 6, name=f"{name}_UpperArm{tag}", material=sleeve_m))
        add(limb(el, wr, 0.042 * s * b, 0.032 * s * b, sides=6, jitter=j, seed=seed + 7, name=f"{name}_ForeArm{tag}", material=fore_m))
        d = (wr - el).normalized()
        hand = wr + d * 0.045 * s
        add(blob(tuple(hand), (0.034 * s, 0.034 * s, 0.05 * s), 1, j, seed + 8, f"{name}_Hand{tag}", material=m_skin))
        side_v = d.cross(Vector((0, 0, 1)))
        side_v = side_v.normalized() if side_v.length > 1e-4 else Vector((1, 0, 0))
        for k in (-1, 0, 1):
            base = hand + side_v * (k * 0.02 * s) + d * 0.02 * s
            add(limb(tuple(base), tuple(base + d * 0.075 * s), 0.014 * s, 0.006 * s, sides=4, name=f"{name}_Finger{tag}{k + 2}", material=m_skin))

    # legs
    for sd, tag in ((_R, "R"), (_L, "L")):
        hp = Vector((sd * 0.100 * s * b, 0, 0.92 * s))
        th_dir, sh_dir = pz["leg"][sd]
        kn = hp + unit(th_dir, sd) * 0.45 * s
        an = kn + unit(sh_dir, sd) * 0.43 * s
        add(limb(hp, kn, 0.078 * s * b, 0.058 * s * b, sides=7, jitter=j, seed=seed + 9, name=f"{name}_Thigh{tag}", material=m_pants))
        add(blob(tuple(kn), (0.062 * s * b,) * 3, 1, j, seed + 10, f"{name}_Knee{tag}", material=m_pants))
        add(limb(kn, an, 0.058 * s * b, 0.040 * s * b, sides=7, jitter=j, seed=seed + 11, name=f"{name}_Shin{tag}", material=m_pants))
        add(blob((an.x, an.y - 0.055 * s, an.z - 0.04 * s), (0.052 * s * b, 0.115 * s, 0.045 * s), 1, j, seed + 12, f"{name}_Foot{tag}", material=m_shoe))

    low = min(_min_z(o) for o in parts)
    for o in parts:
        o.location.z -= low
    g = group(name, parts)
    g.location = _v3(loc)
    g.rotation_euler.z = math.radians(float(facing))
    return g


def showcase(bg="#2f4a3a", strength=0.8, light=1.0):
    """Studio look for a finished model: a solid coloured background and key / fill / rim lights framed to the model."""
    sky(bg, strength)
    studio(light)
    return True


# ---------------------------------------------------------------- modifiers

def _modify(ob, kind, label, **props):
    m = ob.modifiers.new(label, kind)
    for k, v in props.items():
        setattr(m, k, v)
    return m


def _apply_mod(ob, mod) -> bool:
    if ob.type != "MESH":
        return False
    try:
        with bpy.context.temp_override(object=ob, active_object=ob, selected_objects=[ob], selected_editable_objects=[ob]):
            bpy.ops.object.modifier_apply(modifier=mod.name)
        return True
    except Exception:                                                   # noqa: BLE001
        return False


def bevel(ob, width=0.02, segments=3, angle=30):
    ob = _ob(ob)
    _modify(ob, "BEVEL", "Bevel", width=float(width), segments=int(segments), limit_method="ANGLE", angle_limit=math.radians(angle))
    return ob


def subsurf(ob, levels=2, render_levels=None):
    ob = _ob(ob)
    _modify(ob, "SUBSURF", "Subsurf", levels=int(levels), render_levels=int(render_levels if render_levels is not None else levels))
    if ob.type == "MESH":
        _smooth_polys(ob, True)
    return ob


def array(ob, count=3, offset=(1, 0, 0), relative=False):
    ob = _ob(ob)
    m = _modify(ob, "ARRAY", "Array", count=int(count))
    if relative:
        m.use_relative_offset = True
        m.use_constant_offset = False
        m.relative_offset_displace = _v3(offset)
    else:
        m.use_relative_offset = False
        m.use_constant_offset = True
        m.constant_offset_displace = _v3(offset)
    return ob


def mirror(ob, axis="X", merge=True):
    ob = _ob(ob)
    m = _modify(ob, "MIRROR", "Mirror", use_mirror_merge=bool(merge))
    ax = str(axis).upper()
    m.use_axis = (("X" in ax), ("Y" in ax), ("Z" in ax))
    return ob


def solidify(ob, thickness=0.05, offset=-1):
    ob = _ob(ob)
    _modify(ob, "SOLIDIFY", "Solidify", thickness=float(thickness), offset=float(offset))
    return ob


def boolean(ob, other, op="DIFFERENCE", apply=True, hide_other=True):
    ob, other = _ob(ob), _ob(other)
    m = _modify(ob, "BOOLEAN", "Boolean", object=other, operation=str(op).upper())
    try:
        m.solver = "EXACT"
    except Exception:                                                   # noqa: BLE001
        pass
    if apply:
        _apply_mod(ob, m)
    if hide_other:
        other.hide_render = True
        other.hide_viewport = True
    return ob


def apply_modifiers(ob):
    ob = _ob(ob)
    for m in list(ob.modifiers):
        _apply_mod(ob, m)
    return ob


def join(objs, name="Merged"):
    objs = [_ob(o) for o in (objs if isinstance(objs, (list, tuple)) else [objs])]
    objs = [o for o in objs if o.type == "MESH"]
    if len(objs) < 2:
        return objs[0] if objs else None
    for o in objs:
        apply_modifiers(o)
    try:
        with bpy.context.temp_override(object=objs[0], active_object=objs[0], selected_objects=objs, selected_editable_objects=objs):
            bpy.ops.object.join()
    except Exception as e:                                              # noqa: BLE001
        raise RuntimeError(f"join failed: {e}") from None
    objs[0].name = str(name)
    return objs[0]


# ---------------------------------------------------------------- copies

def duplicate(ob, name=None, linked=False, **kw):
    ob = _ob(ob)
    new = ob.copy()
    if getattr(ob, "data", None) is not None and not linked:
        new.data = ob.data.copy()
    _link(new)
    return _place(new, name=name, smooth=False, **kw)


def ring(ob, count=8, radius=1.0, center=(0, 0, 0), face_out=True):
    ob = _ob(ob)
    """Spread `count` objects on a circle: the original takes the first slot, copies fill the rest. Returns all of them."""
    cx, cy, cz = _v3(center)
    n = max(1, int(count))
    out = []
    for i in range(n):
        a = 2 * math.pi * i / n
        spot = (cx + radius * math.cos(a), cy + radius * math.sin(a), cz)
        rot = (math.degrees(ob.rotation_euler.x), math.degrees(ob.rotation_euler.y), math.degrees(a) if face_out else math.degrees(ob.rotation_euler.z))
        if i == 0:
            _place(ob, loc=spot, rot=rot)
            out.append(ob)
        else:
            out.append(duplicate(ob, loc=spot, rot=rot))
    return out


def grid(ob, nx=3, ny=3, dx=1.0, dy=1.0):
    ob = _ob(ob)
    """nx * ny objects on a grid that starts at the object's own position (the original is the first cell)."""
    x0, y0, z0 = ob.location.x, ob.location.y, ob.location.z
    out = [ob]
    for i in range(int(nx)):
        for j in range(int(ny)):
            if i == 0 and j == 0:
                continue
            out.append(duplicate(ob, loc=(x0 + i * dx, y0 + j * dy, z0)))
    return out


# ---------------------------------------------------------------- finding & editing

def _is_geometry(o) -> bool:
    return o.type in _GEOMETRY


def find(spec):
    objs = list(_scene().objects)
    if isinstance(spec, (list, tuple, set)):
        names = [_name_of(n) for n in spec]
    elif isinstance(spec, str):
        spec = _name_of(spec)
        names = [spec] if spec in bpy.data.objects.keys() else [s.strip() for s in str(spec).split(",") if s.strip()]
    elif hasattr(spec, "name") and hasattr(spec, "type"):               # a Blender object
        names = [spec]
    else:
        names = [s.strip() for s in str(spec).split(",") if s.strip()]
    if not names or (len(names) == 1 and isinstance(names[0], str) and names[0].lower() in ("all", "*", "everything")):
        return [o for o in objs if _is_geometry(o) and not o.get(_STAGE)]
    out = []
    for n in names:
        if not isinstance(n, str):
            out.append(n)
            continue
        low = n.lower()
        hit = ([o for o in objs if o.name == n]
               or [o for o in objs if fnmatch.fnmatch(o.name.lower(), low)]
               or [o for o in objs if low in o.name.lower()])
        out.extend(hit)
    seen, uniq = set(), []
    for o in out:
        if id(o) not in seen:
            seen.add(id(o))
            uniq.append(o)
    if not uniq:
        have = ", ".join(o.name for o in objs[:40])
        raise ValueError(f"no object matches {spec!r}. Objects in the scene: {have}")
    return uniq


def recolor(target="all", color=None, metallic=None, roughness=None, emission=None, emission_strength=None, alpha=None):
    changed = 0
    for ob in find(target):
        if not _is_geometry(ob):
            continue
        slots = getattr(ob, "material_slots", [])
        base = slots[0].material if len(slots) and slots[0].material else None
        m = base.copy() if base is not None else bpy.data.materials.new(f"{ob.name}_mat")
        _apply_props(m, color=color, metallic=metallic, roughness=roughness, emission=emission,
                     emission_strength=emission_strength, alpha=alpha)
        assign(ob, m)
        changed += 1
    return changed


def transform(target, loc=None, rot=None, scale=None, move=None, rotate=None, scale_by=None):
    objs = find(target)
    for ob in objs:
        if loc is not None:
            ob.location = _v3(loc)
        if move is not None:
            d = _v3(move)
            ob.location = (ob.location.x + d[0], ob.location.y + d[1], ob.location.z + d[2])
        if rot is not None:
            ob.rotation_euler = tuple(math.radians(a) for a in _v3(rot))
        if rotate is not None:
            d = _v3(rotate)
            ob.rotation_euler = (ob.rotation_euler.x + math.radians(d[0]), ob.rotation_euler.y + math.radians(d[1]),
                                 ob.rotation_euler.z + math.radians(d[2]))
        if scale is not None:
            ob.scale = _v3(scale, 1.0)
        if scale_by is not None:
            s = _v3(scale_by, 1.0)
            ob.scale = (ob.scale.x * s[0], ob.scale.y * s[1], ob.scale.z * s[2])
    return len(objs)


def delete(target):
    victims = []
    for ob in find(target):
        victims.append(ob)
        victims.extend(ob.children_recursive)
    seen = set()
    for ob in victims:
        if id(ob) in seen:
            continue
        seen.add(id(ob))
        bpy.data.objects.remove(ob, do_unlink=True)
    return len(seen)


def rename(old, new):
    ob = find(old)[0]
    ob.name = str(new)
    return ob


def hide(target, hide=True):
    for ob in find(target):
        ob.hide_render = bool(hide)
        ob.hide_viewport = bool(hide)
    return True


def set_smooth(target, smooth=True):
    for ob in find(target):
        if ob.type == "MESH":
            for poly in ob.data.polygons:
                poly.use_smooth = bool(smooth)
    return True


# ---------------------------------------------------------------- scene info

def bounds(include_ground=False):
    _update()
    try:
        dg = bpy.context.evaluated_depsgraph_get()
    except Exception:                                                   # noqa: BLE001
        dg = None
    pts = []
    for ob in _scene().objects:
        if ob.type not in _GEOMETRY or ob.get(_STAGE) or ob.hide_render:
            continue
        if ob.get(_GROUND) and not include_ground:
            continue
        src = ob
        if dg is not None:
            try:
                src = ob.evaluated_get(dg)                              # includes array / mirror / boolean results
            except Exception:                                           # noqa: BLE001
                src = ob
        mw = src.matrix_world
        pts.extend(mw @ Vector(c) for c in src.bound_box)
    if not pts:
        return Vector((-1.0, -1.0, 0.0)), Vector((1.0, 1.0, 1.0))
    lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    return lo, hi


def describe(limit=80):
    rows = []
    for ob in _scene().objects:
        if ob.get(_STAGE):
            continue
        mats = []
        if getattr(ob, "material_slots", None):
            mats = [s.material.name for s in ob.material_slots if s.material]
        rows.append({
            "name": ob.name, "type": ob.type,
            "loc": [round(v, 3) for v in ob.location],
            "dim": [round(v, 3) for v in ob.dimensions],
            "materials": mats,
            "parent": ob.parent.name if ob.parent else None,
        })
        if len(rows) >= limit:
            break
    return rows


# ---------------------------------------------------------------- lights, world, camera

def _aim(ob, target):
    d = Vector(target) - ob.location
    ob.rotation_euler = d.to_track_quat("-Z", "Y").to_euler()


def _remove_stage(prefix, types=None):
    for ob in list(bpy.data.objects):
        if ob.get(_STAGE) and ob.name.startswith(prefix) and (types is None or ob.type in types):
            bpy.data.objects.remove(ob, do_unlink=True)


def light(kind="POINT", loc=(0, 0, 3), energy=None, color="white", rot=None, target=None, size=None, name=None):
    kind = str(kind).upper()
    if kind not in ("POINT", "SUN", "AREA", "SPOT"):
        raise ValueError("light kind must be POINT, SUN, AREA or SPOT")
    data = bpy.data.lights.new(str(name or kind.title()), kind)
    data.energy = float({"SUN": 3.0, "POINT": 1000.0, "SPOT": 1000.0, "AREA": 500.0}[kind] if energy is None else energy)
    data.color = col(color)[:3]
    if kind == "AREA" and size:
        data.size = float(size)
    ob = _link(bpy.data.objects.new(str(name or kind.title()), data))
    ob.location = _v3(loc)
    if rot is not None:
        ob.rotation_euler = tuple(math.radians(a) for a in _v3(rot))
    elif target is not None:
        _aim(ob, _v3(target))
    return ob


def studio(strength=1.0):
    lo, hi = bounds()
    center = (lo + hi) / 2
    radius = max((hi - lo).length / 2, 0.5)
    k = float(strength) * radius * radius
    _remove_stage("JB_", ("LIGHT",))
    for name, kind, off, energy, size in (
        ("JB_Key", "AREA", (1.6, -1.8, 1.9), 700 * k, radius * 2.0),
        ("JB_Fill", "AREA", (-2.2, -1.2, 1.0), 220 * k, radius * 2.5),
        ("JB_Rim", "AREA", (0.2, 2.2, 1.7), 450 * k, radius * 1.5),
    ):
        pos = (center.x + off[0] * radius * 1.4, center.y + off[1] * radius * 1.4, center.z + off[2] * radius * 1.4)
        ob = light(kind, loc=pos, energy=energy, size=size, target=tuple(center), name=name)
        ob[_STAGE] = True


def clear_lights():
    n = 0
    for ob in list(bpy.data.objects):
        if ob.type == "LIGHT":
            bpy.data.objects.remove(ob, do_unlink=True)
            n += 1
    return n


def sun(strength=3.0, rot=(50, 0, 35), color="white"):
    ob = light("SUN", energy=strength, color=color, rot=rot, name="Sun")
    return ob


def sky(color="#e8eef5", strength=1.0):
    sc = _scene()
    if sc.world is None:
        sc.world = bpy.data.worlds.new("World")
    w = sc.world
    w.use_nodes = True
    bg = w.node_tree.nodes.get("Background") or next((n for n in w.node_tree.nodes if n.type == "BACKGROUND"), None)
    if bg is None:
        bg = w.node_tree.nodes.new("ShaderNodeBackground")
        out = next((n for n in w.node_tree.nodes if n.type == "OUTPUT_WORLD"), None) or w.node_tree.nodes.new("ShaderNodeOutputWorld")
        w.node_tree.links.new(bg.outputs["Background"], out.inputs["Surface"])
    bg.inputs["Color"].default_value = col(color)
    bg.inputs["Strength"].default_value = float(strength)
    w["jb_sky"] = True
    return w


_VIEWS = {
    "iso": (1.0, -1.0, 0.8), "three_quarter": (0.8, -1.0, 0.35), "front": (0.0, -1.0, 0.12), "back": (0.0, 1.0, 0.12),
    "left": (-1.0, 0.0, 0.12), "right": (1.0, 0.0, 0.12), "top": (0.0, -0.001, 1.0),
}


def auto_camera(view="iso", lens=50.0, margin=1.15):
    sc = _scene()
    view = str(view or "iso").lower()
    cam = sc.camera
    if view == "scene" and cam is not None:
        return cam
    if view not in _VIEWS:
        raise ValueError(f"unknown view {view!r}; use one of: {', '.join(_VIEWS)} or 'scene'")
    if cam is None or cam.type != "CAMERA" or not cam.get(_STAGE):
        _remove_stage("JB_Camera", ("CAMERA",))
        data = bpy.data.cameras.new("JB_Camera")
        cam = _link(bpy.data.objects.new("JB_Camera", data))
        cam[_STAGE] = True
    lo, hi = bounds()
    center = (lo + hi) / 2
    radius = max((hi - lo).length / 2, 0.25)
    rx, ry = max(sc.render.resolution_x, 1), max(sc.render.resolution_y, 1)
    aspect = min(rx, ry) / max(rx, ry)                       # the narrow side limits what fits
    wide = math.atan(cam.data.sensor_width / 2 / float(lens))
    half = math.atan(math.tan(wide) * aspect)
    dist = radius * float(margin) / max(math.sin(half), 0.05)
    cam.data.lens = float(lens)
    cam.data.clip_start = 0.01
    cam.data.clip_end = max(1000.0, dist * 10)
    cam.location = center + Vector(_VIEWS[view]).normalized() * dist
    _aim(cam, tuple(center))
    sc.camera = cam
    return cam


def stage():
    """Make sure there is a world, light and camera so a render is never black."""
    sc = _scene()
    if sc.world is None or not sc.world.get("jb_sky"):
        sky()
    if not any(o.type == "LIGHT" for o in sc.objects):
        studio()
    try:
        sc.view_settings.view_transform = "Standard"
    except Exception:                                                   # noqa: BLE001
        pass


# ---------------------------------------------------------------- project i/o

def clear():
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    purge()


def purge():
    for coll in (bpy.data.meshes, bpy.data.curves, bpy.data.materials, bpy.data.lights, bpy.data.cameras):
        for block in list(coll):
            if block.users == 0:
                coll.remove(block)


def save(path):
    purge()
    bpy.ops.wm.save_as_mainfile(filepath=str(path), compress=True, check_existing=False)


def render(path, view="iso", res=(960, 720), samples=48, transparent=False, engine=None):
    """Render a PNG. Tries EEVEE first (fast), falls back to Cycles on CPU. Returns the engine that worked."""
    sc = _scene()
    sc.render.resolution_x, sc.render.resolution_y = int(res[0]), int(res[1])
    sc.render.resolution_percentage = 100
    sc.render.image_settings.file_format = "PNG"
    sc.render.film_transparent = bool(transparent)
    sc.render.filepath = str(path)
    stage()
    auto_camera(view)
    last = None
    for eng in ((engine,) if engine else ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE", "CYCLES")):
        try:
            sc.render.engine = eng
        except (TypeError, ValueError):
            continue
        try:
            if eng == "CYCLES":
                sc.cycles.samples = min(int(samples), 40)
                sc.cycles.device = "CPU"
                try:
                    sc.cycles.use_denoising = True
                except Exception:                                       # noqa: BLE001
                    pass
            else:
                try:
                    sc.eevee.taa_render_samples = int(samples)
                except Exception:                                       # noqa: BLE001
                    pass
            bpy.ops.render.render(write_still=True)
            if os.path.exists(str(path)):
                return eng
        except Exception as e:                                          # noqa: BLE001
            last = e
    raise RuntimeError(f"render failed in every engine: {last}")


def _select(objs):
    for o in _scene().objects:
        o.select_set(False)
    for o in objs:
        o.select_set(True)
    if objs:
        bpy.context.view_layer.objects.active = objs[0]


def export(path, fmt=None):
    path = str(path)
    fmt = (fmt or os.path.splitext(path)[1].lstrip(".")).lower()
    sel = [o for o in _scene().objects if (o.type in _GEOMETRY or o.type == "EMPTY") and not o.get(_STAGE) and not o.get(_GROUND) and not o.hide_render]
    if not sel:
        raise RuntimeError("nothing to export: the scene has no visible models")
    curves = [o for o in sel if o.type in ("CURVE", "FONT", "SURFACE")]
    if curves:
        _select(curves)
        try:
            with bpy.context.temp_override(object=curves[0], active_object=curves[0], selected_objects=curves, selected_editable_objects=curves):
                bpy.ops.object.convert(target="MESH")
        except Exception:                                               # noqa: BLE001
            pass
    sel = [o for o in _scene().objects if (o.type == "MESH" or o.type == "EMPTY") and not o.get(_STAGE) and not o.get(_GROUND) and not o.hide_render]
    _select(sel)

    def attempt(*calls):
        err = None
        for fn, kwargs in calls:
            try:
                fn(filepath=path, **kwargs)
                return
            except (TypeError, AttributeError, RuntimeError) as e:
                err = e
        raise RuntimeError(f"{fmt} export failed: {err}")

    ops = bpy.ops
    if fmt in ("glb", "gltf"):
        kind = "GLB" if fmt == "glb" else "GLTF_SEPARATE"
        attempt((ops.export_scene.gltf, dict(export_format=kind, use_selection=True, export_apply=True)),
                (ops.export_scene.gltf, dict(export_format=kind, use_selection=True)))
    elif fmt == "fbx":
        attempt((ops.export_scene.fbx, dict(use_selection=True)))
    elif fmt == "obj":
        attempt((ops.wm.obj_export, dict(export_selected_objects=True)),
                (ops.export_scene.obj, dict(use_selection=True)))
    elif fmt == "stl":
        attempt((ops.wm.stl_export, dict(export_selected_objects=True)),
                (ops.export_mesh.stl, dict(use_selection=True)))
    else:
        raise ValueError("export format must be glb, gltf, fbx, obj or stl")
    return path


def import_model(path):
    path = str(path)
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    before = {o.name for o in _scene().objects}
    ops = bpy.ops
    if ext in ("glb", "gltf"):
        ops.import_scene.gltf(filepath=path)
    elif ext == "fbx":
        ops.import_scene.fbx(filepath=path)
    elif ext == "obj":
        try:
            ops.wm.obj_import(filepath=path)
        except (AttributeError, RuntimeError):
            ops.import_scene.obj(filepath=path)
    elif ext == "stl":
        try:
            ops.wm.stl_import(filepath=path)
        except (AttributeError, RuntimeError):
            ops.import_mesh.stl(filepath=path)
    else:
        raise ValueError("import format must be glb, gltf, fbx, obj or stl")
    return [o.name for o in _scene().objects if o.name not in before]


# Operations Jarvis can request directly (no script): name -> function
OPS = {
    "recolor": recolor, "transform": transform, "delete": delete, "rename": rename, "hide": hide, "set_smooth": set_smooth,
    "studio": studio, "sky": sky, "sun": sun, "clear_lights": clear_lights, "light": light, "ground": ground, "clear": clear, "import_model": import_model,
}


def run_op(fn, kwargs):
    if fn not in OPS:
        raise ValueError(f"unknown operation {fn!r}")
    return OPS[fn](**(kwargs or {}))


def namespace():
    """Globals for generated scripts."""
    import random
    import sys
    return {"__name__": "jarvis_blender_script", "bpy": bpy, "bmesh": bmesh, "math": math, "random": random,
            "Vector": Vector, "Euler": Euler, "Matrix": Matrix, "jb": sys.modules[__name__]}

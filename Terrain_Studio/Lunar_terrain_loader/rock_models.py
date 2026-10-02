"""
Rock Model Inspection
---------------------
Locates rock model files and measures their real-world size outside Unreal,
so planned rock sizes can be matched to the meshes being placed.

Used by both the studio and unreal_rock_placer (inside Unreal), hence standard
library only.

Conventions: models are Y-up and in metres; the footprint radius is half the
larger horizontal extent and the height is the vertical extent.
"""

import json
import math
import os
import struct

GLTF_EXTENSIONS = (".gltf", ".glb")
MESH_EXTENSIONS = (".obj", ".fbx") + GLTF_EXTENSIONS


def find_model_files(models_folder):
    """Returns every .obj/.fbx/.gltf/.glb file under `models_folder`, recursively."""
    found = []
    for root, _dirs, files in os.walk(models_folder):
        found += [os.path.join(root, f) for f in files if f.lower().endswith(MESH_EXTENSIONS)]
    return sorted(found)


def _read_gltf_json(path):
    if path.lower().endswith(".glb"):
        with open(path, "rb") as handle:
            magic, _version, _length = struct.unpack("<4sII", handle.read(12))
            if magic != b"glTF":
                raise ValueError("not a GLB file")
            chunk_length, chunk_type = struct.unpack("<I4s", handle.read(8))
            if chunk_type != b"JSON":
                raise ValueError("GLB has no JSON chunk")
            return json.loads(handle.read(chunk_length))
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _gltf_extent(path):
    """Returns the (dx, dy, dz) extent in metres of the mesh the placer selects (LOD0, else the densest)."""
    doc = _read_gltf_json(path)
    meshes, accessors, nodes = doc.get("meshes", []), doc.get("accessors", []), doc.get("nodes", [])
    if not meshes:
        raise ValueError("no meshes")

    def vertex_count(mesh):
        return sum(accessors[p["attributes"]["POSITION"]].get("count", 0)
                   for p in mesh.get("primitives", []) if "POSITION" in p.get("attributes", {}))

    node = next((n for n in nodes if "mesh" in n and "lod0" in n.get("name", "").lower()), None)
    if node is None:
        mesh_nodes = [n for n in nodes if "mesh" in n]
        node = (max(mesh_nodes, key=lambda n: vertex_count(meshes[n["mesh"]]))
                if mesh_nodes else {"mesh": max(range(len(meshes)),
                                                key=lambda i: vertex_count(meshes[i]))})
    scale = node.get("scale", [1.0, 1.0, 1.0])

    lo, hi = [float("inf")] * 3, [float("-inf")] * 3
    for primitive in meshes[node["mesh"]].get("primitives", []):
        index = primitive.get("attributes", {}).get("POSITION")
        if index is None or "min" not in accessors[index]:
            continue
        lo = [min(a, b) for a, b in zip(lo, accessors[index]["min"])]
        hi = [max(a, b) for a, b in zip(hi, accessors[index]["max"])]
    if lo[0] == float("inf"):
        raise ValueError("no position bounds")
    return tuple(abs(s) * (b - a) for s, a, b in zip(scale, lo, hi))


def _obj_extent(path):
    """Returns the (dx, dy, dz) vertex extent of an OBJ file."""
    lo, hi = [float("inf")] * 3, [float("-inf")] * 3
    with open(path, encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            if line.startswith("v "):
                values = [float(v) for v in line.split()[1:4]]
                lo = [min(a, b) for a, b in zip(lo, values)]
                hi = [max(a, b) for a, b in zip(hi, values)]
    if lo[0] == float("inf"):
        raise ValueError("no vertices")
    return tuple(b - a for a, b in zip(lo, hi))


def model_dimensions(path):
    """
    Returns (footprint_radius_m, height_m), or None for unreadable files and
    formats that cannot be measured outside Unreal (.fbx).
    """
    try:
        if path.lower().endswith(GLTF_EXTENSIONS):
            dx, dy, dz = _gltf_extent(path)
        elif path.lower().endswith(".obj"):
            dx, dy, dz = _obj_extent(path)
        else:
            return None
    except (OSError, ValueError, KeyError, IndexError, struct.error):
        return None
    return max(dx, dz) / 2.0, dy


def folder_dimensions(models_folder):
    """Returns [(file_name, footprint_radius_m, height_m)] per model; sizes are None if unknown."""
    rows = []
    for path in find_model_files(models_folder):
        size = model_dimensions(path)
        rows.append((os.path.basename(path),) + (size if size else (None, None)))
    return rows


def size_summary(rows):
    """
    Returns (min_radius_m, max_radius_m, median height/radius) over the models
    of known size, or None if there are none.
    """
    known = [(r, h) for _name, r, h in rows if r]
    if not known:
        return None
    ratios = sorted(h / r for r, h in known)
    middle = len(ratios) // 2
    median = ratios[middle] if len(ratios) % 2 else 0.5 * (ratios[middle - 1] + ratios[middle])
    return min(r for r, _h in known), max(r for r, _h in known), median


# Size ratio, relative to the best fit, within which a model is eligible for a
# rock. Keeps variety without scaling models far from their native size.
FIT_TOLERANCE = 1.5


def fitting_models(model_radii, radius):
    """
    Returns the indices of the models eligible for a rock of footprint `radius`:
    the closest by size ratio and any within FIT_TOLERANCE of it (consistent units).
    """
    misfit = [abs(math.log(radius / r)) for r in model_radii]
    limit = min(misfit) + math.log(FIT_TOLERANCE) + 1e-9
    return [i for i, m in enumerate(misfit) if m <= limit]


def scale_range(model_radii, planned_min, planned_max, samples=256):
    """
    Returns the (min, max) uniform scale applied to models for planned radii
    in [planned_min, planned_max], each rock using an eligible model.
    """
    planned_max = max(planned_min, planned_max)
    low, high = float("inf"), 0.0
    for k in range(samples):
        radius = planned_min * (planned_max / planned_min) ** (k / (samples - 1.0))
        for i in fitting_models(model_radii, radius):
            factor = radius / model_radii[i]
            low, high = min(low, factor), max(high, factor)
    return low, high

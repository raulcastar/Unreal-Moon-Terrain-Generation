"""
Rock Model Inspection
---------------------
Finds rock model files and reads their real-world size without Unreal, so the
studio can match the planned rock sizes to the meshes that will be placed.

Shared by the studio and by unreal_rock_placer (which runs inside Unreal), so
it uses the standard library only.

Size convention, matching the placer: models are Y-up (glTF and most OBJ
exports) and in metres; the footprint radius is half the larger horizontal
extent and the height is the vertical extent.
"""

import json
import math
import os
import struct

GLTF_EXTENSIONS = (".gltf", ".glb")
MESH_EXTENSIONS = (".obj", ".fbx") + GLTF_EXTENSIONS


def find_model_files(models_folder):
    """Every .obj/.fbx/.gltf/.glb under `models_folder`, including subfolders."""
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
    """(dx, dy, dz) in metres of the mesh the placer would pick: LOD0, else the densest."""
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
    """(dx, dy, dz) of the vertices in an OBJ file."""
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
    (footprint_radius_m, height_m) of one model, or None when the size cannot
    be read without Unreal (.fbx) or the file is unreadable.
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
    """[(file name, footprint_radius_m or None, height_m or None)] for every model."""
    rows = []
    for path in find_model_files(models_folder):
        size = model_dimensions(path)
        rows.append((os.path.basename(path),) + (size if size else (None, None)))
    return rows


def size_summary(rows):
    """
    (min_radius_m, max_radius_m, median height/radius) over the models whose
    size is known, or None if none is.
    """
    known = [(r, h) for _name, r, h in rows if r]
    if not known:
        return None
    ratios = sorted(h / r for r, h in known)
    middle = len(ratios) // 2
    median = ratios[middle] if len(ratios) % 2 else 0.5 * (ratios[middle - 1] + ratios[middle])
    return min(r for r, _h in known), max(r for r, _h in known), median


# A rock may use any model whose size is within this factor of the best fit, so
# a field keeps some variety without stretching models far from their own size.
FIT_TOLERANCE = 1.5


def fitting_models(model_radii, radius):
    """
    Indices of the models a rock of footprint `radius` may use: the closest in
    size (by ratio) and any within FIT_TOLERANCE of it. Units only need to match.
    """
    misfit = [abs(math.log(radius / r)) for r in model_radii]
    limit = min(misfit) + math.log(FIT_TOLERANCE) + 1e-9
    return [i for i, m in enumerate(misfit) if m <= limit]


def scale_range(model_radii, planned_min, planned_max, samples=256):
    """
    (smallest, largest) uniform scale a placed model gets when rocks of
    planned_min..planned_max radius each use a fitting model.
    """
    planned_max = max(planned_min, planned_max)
    low, high = float("inf"), 0.0
    for k in range(samples):
        radius = planned_min * (planned_max / planned_min) ** (k / (samples - 1.0))
        for i in fitting_models(model_radii, radius):
            factor = radius / model_radii[i]
            low, high = min(low, factor), max(high, factor)
    return low, high

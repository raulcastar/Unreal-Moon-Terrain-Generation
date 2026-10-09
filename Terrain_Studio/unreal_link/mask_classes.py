"""
Object-mask classes: ids, names, colours and groups, from config/mask_classes.json.

The one definition the studio uses: the mask material's palette and the actors'
stencils (unreal_ground_truth, unreal_landscape, unreal_rocks), the crater map
and the GUI. The GNC simulator keeps a copy of the same file
(component_stereo_depth/config/mask_classes.json); gnc_difference() compares the
two when that repository sits next to this one.

Plain Python (no Unreal, no Tk), so it runs both in the studio and inside the
editor, like rock_models. The file is read on every call, so an edit applies to
the next action without restarting the editor.
"""

import json
import os

REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CLASSES_FILE = os.path.join(REPOSITORY_ROOT, "config", "mask_classes.json")
# The GNC simulator's copy, when its repository is next to this one.
GNC_CLASSES_FILE = os.path.join(os.path.dirname(REPOSITORY_ROOT), "GNC-Moon-Simulator",
                                "component_stereo_depth", "config", "mask_classes.json")


def classes(path=CLASSES_FILE):
    """
    The classes as a list of {id, name, group, rgb, about}, sorted by id.
    Raises ValueError naming the problem when the file is malformed.
    """
    with open(path, encoding="utf-8") as handle:
        entries = json.load(handle).get("classes")
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{path}: 'classes' must be a non-empty list")
    seen_ids, seen_names = set(), set()
    for entry in entries:
        cid, name, rgb = entry.get("id"), entry.get("name"), entry.get("rgb")
        if not isinstance(cid, int) or not 0 <= cid <= 255:
            raise ValueError(f"{path}: class {name!r} has id {cid!r}; ids are 0-255")
        if not isinstance(name, str) or not name:
            raise ValueError(f"{path}: class {cid} has no name")
        if cid in seen_ids or name in seen_names:
            raise ValueError(f"{path}: class {cid} {name!r} is listed twice")
        if (not isinstance(rgb, list) or len(rgb) != 3
                or any(not isinstance(v, int) or not 0 <= v <= 254 or v % 2 for v in rgb)):
            # The lowest bit of each channel carries the shadow mask.
            raise ValueError(f"{path}: class {cid} {name!r} has rgb {rgb!r}; "
                             "expected three even values 0-254")
        seen_ids.add(cid)
        seen_names.add(name)
    return sorted(entries, key=lambda e: e["id"])


def ids(path=CLASSES_FILE):
    """{id: name}"""
    return {e["id"]: e["name"] for e in classes(path)}


def palette(path=CLASSES_FILE):
    """{id: (r, g, b)}"""
    return {e["id"]: tuple(e["rgb"]) for e in classes(path)}


def id_of(name, path=CLASSES_FILE):
    """The id of the class called `name`."""
    for entry in classes(path):
        if entry["name"] == name:
            return entry["id"]
    raise KeyError(f"{path} has no class named {name!r}")


def gnc_difference(table=None, gnc_path=GNC_CLASSES_FILE):
    """
    How the GNC copy differs from `table` ({id: name}; this repository's classes
    by default), as a short text; "" when they agree, None when the GNC copy is
    not found next to this repository.
    """
    if not os.path.isfile(gnc_path):
        return None
    table = ids() if table is None else table
    try:
        theirs = ids(gnc_path)
    except (OSError, ValueError) as exc:
        return f"GNC copy unreadable: {exc}"
    lines = []
    for cid in sorted(set(table) | set(theirs)):
        mine, other = table.get(cid), theirs.get(cid)
        if mine != other:
            lines.append(f"id {cid}: studio {mine or '-'}, GNC {other or '-'}")
    if not lines and palette() != palette(gnc_path):
        lines.append("same ids and names, different colours")
    return "\n".join(lines)

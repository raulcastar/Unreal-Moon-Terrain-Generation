"""
Ground Truth (crater masks) logic: mask ids and classes, shadow ids, the
crater source, the wheel-track look and the mask cameras. No Tk.
"""

import json
import os

from gui.backend import crater_map

import mask_classes  # noqa: E402  (unreal_link, put on the path by gui.paths)

# Object-mask ids, from config/mask_classes.json. Rocks can be changed in the
# rock planner and craters in Ground Truth; both views read them for placement
# and the GNC labels.
DEFAULT_MASK_IDS = {"boulder": mask_classes.id_of("boulder"),
                    "small_rock": mask_classes.id_of("small_rock"), "boulder_radius_m": 0.15,
                    "crater": crater_map.CRATER_ID, "rim": crater_map.RIM_ID}
SUN_GLARE_ID = mask_classes.id_of("sun_glare")

GNC_CLASSES_FILE = "GNC-Moon-Simulator/component_stereo_depth/config/mask_classes.json"

# Tread looks (unreal_wheel_tracks.TRACK_PATTERNS) and lug spacings that divide its
# 12 cm decal spacing.
TRACK_PATTERNS = (("Straight lugs", "straight"), ("Chevron", "chevron"),
                  ("Staggered blocks", "blocks"), ("Smooth (no tread)", "smooth"))
LUG_SPACINGS_CM = ("2", "3", "4", "6")

# Shadow class ids published by the GNC stereo node (SHADOW_IDS there).
SHADOW_IDS = ((0, "sky"), (1, "sunlit ground"), (2, "shadowed ground"),
              (3, "sunlit rock / rover"), (4, "shadowed rock / rover"),
              (5, "rover shadow (on ground or rocks)"))

CRATER_ID_RULE = "Crater ids must be different whole numbers\n1-255 (rim 0-255)."


def mask_ids(app):
    """The studio's current object-mask ids (defaults when not set)."""
    return getattr(app, "mask_ids", None) or dict(DEFAULT_MASK_IDS)


def parse_crater_ids(shared_ids, crater_text, rim_text):
    """
    All mask ids: rocks from the shared ids, crater and rim from the fields;
    None if invalid (crater 1-255, rim 0-255, different).
    """
    ids = dict(DEFAULT_MASK_IDS, **shared_ids)
    try:
        ids["crater"], ids["rim"] = int(crater_text), int(rim_text)
    except ValueError:
        return None
    if not (1 <= ids["crater"] <= 255 and 0 <= ids["rim"] <= 255 and ids["crater"] != ids["rim"]):
        return None
    return ids


def effective_classes(ids, classes=None):
    """
    The mask classes (config/mask_classes.json) with the crater and rock ids
    chosen in the studio, sorted by id. The rim class is left out when the rim
    is off (id 0), small_rock when it shares the boulder id.
    """
    chosen = {"crater": ids["crater"], "crater_rim": ids["rim"], "boulder": ids["boulder"],
              "small_rock": ids["small_rock"]}
    out = []
    for entry in (mask_classes.classes() if classes is None else classes):
        entry = dict(entry, id=chosen.get(entry["name"], entry["id"]))
        if entry["name"] == "crater_rim" and not ids["rim"]:
            continue                                   # rim off
        if entry["name"] == "small_rock" and ids["small_rock"] == ids["boulder"]:
            continue                                   # a single rock class
        out.append(entry)
    return sorted(out, key=lambda e: e["id"])


def class_lines(classes):
    """One line per class for the labels box: id, name, group, colour."""
    return [f"{c['id']:3d}  {c['name']:<12} {c['group']:<8} rgb{tuple(c['rgb'])}" for c in classes]


def gnc_status(difference):
    """The GNC-copy status line from mask_classes.gnc_difference (None, '' or a diff)."""
    if difference is None:
        return "GNC copy not found next to this repository: keep it in step by hand."
    if difference == "":
        return "GNC copy matches."
    return "GNC copy differs; Copy, then paste over it:\n" + difference


def classes_document(classes_file, classes):
    """The mask_classes.json text with `classes` in place of the file's own."""
    with open(classes_file, encoding="utf-8") as handle:
        document = json.load(handle)
    document["classes"] = classes
    return json.dumps(document, indent=2) + "\n"


def shadow_id_lines(shadows, rover_shadow):
    """The shadow-label legend lines, as the GNC stereo node publishes them."""
    if not shadows:
        return ["Shadow feed off."]
    lines = [f"{i}  {name}" for i, name in SHADOW_IDS if i != 5 or rover_shadow]
    if not rover_shadow:
        lines.append("(5 rover shadow: off)")
    return lines


def parse_glare_deg(text):
    """Glare ring radius in degrees from the field; 0 when blank or invalid."""
    try:
        return max(float(text), 0.0)
    except ValueError:
        return 0.0


def crater_list_for(dem_path, group_name):
    """The generator's crater list next to a DEM (<group>_craters.csv), or None."""
    candidate = os.path.join(os.path.dirname(dem_path), f"{group_name}_craters.csv")
    return candidate if os.path.isfile(candidate) else None


def crater_source_info(has_list, from_generator, list_name=None):
    """Which crater source is in use, for the read-out under the choice."""
    if from_generator:
        return f"Generated here: using {list_name}."
    if has_list:
        return "A generator crater list exists; detecting anyway."
    return "No generator crater list for this map: detecting."


def crater_summary(count, cover_pct, from_generator):
    """(read-out, log line) after craters were found or loaded."""
    origin = "from the generator" if from_generator else "detected"
    return (f"{count} craters ({origin}), {cover_pct:.1f}% of the map.",
            f"{count} craters {origin}.")


def load_crater_mask_meta(folder, group):
    """(png path, metadata dict or None) of an exported crater map."""
    png = os.path.join(folder, f"{group}_craterMask.png")
    try:
        with open(os.path.join(folder, f"{group}_craterMask.json")) as handle:
            meta = json.load(handle)
    except (OSError, ValueError):
        meta = None
    return png, meta


# --------------------------------------------------------------- wheel tracks

def track_style(pattern_label, spacing_text, relief, shade):
    """The tread look (unreal_wheel_tracks style) from the Wheel Tracks fields."""
    return {"pattern": dict(TRACK_PATTERNS).get(pattern_label, "straight"),
            "period_cm": float(spacing_text or 3),
            "relief": round(float(relief), 2), "shade": round(float(shade), 2)}


def track_style_info(pattern_label, style):
    return (f"{pattern_label}, lugs every {style['period_cm']:g} cm, relief "
            f"{style['relief']:.1f}, shade {style['shade']:.2f}")


def track_preview_shapes(style, width, height=46):
    """
    What the track preview draws: 18 cm of one wheel's track from above,
    travel left to right. Returns [(kind, coordinates, colour)], kind being
    'rect' (x0, y0, x1, y1) or 'polygon' (x0, y0, x1, y1, ...); the first is
    the ground strip.
    """
    width = max(width, 160)
    grey = int(208 * (0.85 + 0.15 * style["shade"]))
    top, bottom = 8, height - 8
    shapes = [("rect", (0, top, width, bottom), f"#{grey:02x}{grey:02x}{grey - 6:02x}")]
    if style["pattern"] == "smooth" or style["relief"] <= 0.01:
        return shapes
    dark = max(0, grey - int(60 * min(style["relief"], 2.0)))
    colour = f"#{dark:02x}{dark:02x}{max(dark - 6, 0):02x}"
    cm = width / 18.0                       # 18 cm of track across the preview
    period, lug = style["period_cm"] * cm, 0.4 * style["period_cm"] * cm
    middle, inset = (top + bottom) / 2, 3
    x = 0.0
    while x < width + period:
        if style["pattern"] == "straight":
            shapes.append(("rect", (x, top + inset, x + lug, bottom - inset), colour))
        elif style["pattern"] == "chevron":
            skew = 0.5 * period
            shapes.append(("polygon", (x + skew, top + inset, x + skew + lug, top + inset, x + lug, middle,
                                       x + skew + lug, bottom - inset, x + skew, bottom - inset, x, middle),
                           colour))
        else:                                # staggered blocks
            shapes.append(("rect", (x, top + inset, x + lug, middle - 2), colour))
            shapes.append(("rect", (x + period / 2, middle + 2, x + period / 2 + lug, bottom - inset),
                           colour))
        x += period
    return shapes


# -------------------------------------------------------------------- cameras

def parse_camera(text):
    """(actor label, capture name) from the camera box, or ('', '') when blank."""
    text = text.strip()
    if " / " not in text:
        return "", ""
    actor, capture = text.split(" / ", 1)
    return actor.strip(), capture.strip()


def camera_choices(found):
    """
    From send_masks.list_mask_cameras' answer: (options for the camera box,
    the capture streamed as the object mask now or None, the info line).
    """
    options, streamed = [], None
    for actor in found.get("actors", []):
        for capture in actor["captures"]:
            options.append(f"{actor['actor']} / {capture}")
        if actor.get("streamer_mask"):
            streamed = f"{actor['actor']} / {actor['streamer_mask']}"
    info = ((f"Streamed as the object mask now: {streamed}." if streamed
             else "No UnrealStereoCam found: the mask is not streamed.")
            + (" Play In Editor is running: camera changes last until it stops."
               if found.get("pie") else ""))
    return options, streamed, info

"""
Rock Field & Rover Paths logic: the parameter schema, input parsing and the
text read-outs. No Tk, no Matplotlib.
"""

import json
import os

import numpy as np

# Measured Unreal placement cost (10,338 rocks in 158 s, one actor per rock;
# fields are now instanced). Above UNREAL_ROCKS_WARN rocks, the estimate also
# reports the expected placement time.
UNREAL_SECONDS_PER_ROCK = 0.015
UNREAL_ROCKS_WARN = 20_000

# Parameter schema for the Rock Field & Rover Paths window (same tuple layout as
# generator_logic.PARAM_GROUPS). Keys map directly onto rock_field.DEFAULTS.
ROCK_PARAM_GROUPS = [
    ("Rover Path", [
        ("PATH_WIDTH_M", "Path width (m)", 0.5, 30.0, 'float',
         "Full width of the cleared rover corridor. No rock is placed within half\n"
         "this distance of a road, so the whole lane stays drivable (this now\n"
         "includes what used to be the separate path margin)."),
    ]),
    ("Crater Hazards", [
        ("MIN_CRATER_DEPTH_M", "Min crater depth (m)", 0.05, 20.0, 'log',
         "A closed depression at least this deep is treated as a crater: impassable\n"
         "to the rover and excluded from rock placement. With the generator's\n"
         "craters, the bowls at least this deep are impassable.\n"
         "Lower values flag shallower craters; higher values ignore them."),
    ]),
    ("Rock Population", [
        # Logarithmic: on 1 km2 the useful range runs from tens of rocks (1e-4)
        # to tens of thousands (0.05), and a linear slider crammed everything
        # below a few thousand rocks into its first few pixels.
        ("ROCK_COVERAGE", "Coverage (fraction)", 1e-4, 0.05, 'log',
         "Target fraction of the drivable ground covered by rock footprints.\n"
         "On 1 km2: 1e-3 is about 900 rocks, 0.01 about 8,600, 0.03 about 26,000.\n"
         "Small rocks need many more instances for the same coverage: the\n"
         "estimate below shows the count before you preview. Type 0 for none."),
        ("ROCK_MIN_SCALE", "Min radius (m)", 0.01, 10.0, 'log',
         "Smallest rock footprint radius, in metres. In Unreal each model is\n"
         "scaled uniformly so its footprint matches the planned radius, whatever\n"
         "size the model itself is."),
        ("ROCK_MAX_SCALE", "Max radius (m)", 0.01, 30.0, 'log',
         "Largest rock footprint radius, in metres. Radii follow the lunar\n"
         "size-frequency law (count of rocks larger than r ~ r^-2.5), so most\n"
         "rocks are near the minimum and large boulders are rare."),
        ("ROCK_HEIGHT_RATIO", "Height / radius", 0.1, 3.0, 'float',
         "Rock height as a multiple of its footprint radius, written to the\n"
         "instance list as height_m. In Unreal this is each rock's maximum\n"
         "height: models taller than that are flattened to it, squatter models\n"
         "keep their own proportions."),
    ]),
]

STENCIL_RULE = "Mask ids must be whole numbers 1-255 and the radius >= 0."


def parse_resolution(text, fallback=1.0):
    """Resolution in m/px from the field: at least 1e-6, `fallback` when not a number."""
    try:
        return max(1e-6, float(text))
    except ValueError:
        return fallback


def parse_stencils(boulder_text, small_text, radius_text):
    """(boulder_id, small_rock_id, boulder_min_radius_m), or None if invalid."""
    try:
        boulder, small, radius = int(boulder_text), int(small_text), float(radius_text)
    except ValueError:
        return None
    if not (1 <= boulder <= 255 and 1 <= small <= 255 and radius >= 0):
        return None
    return boulder, small, radius


# What an exported rock field was made from, saved beside it so placing can
# tell whether the file on disk is what the controls now describe.
SETTINGS_LABELS = {
    "PATH_WIDTH_M": "path width", "MIN_CRATER_DEPTH_M": "min crater depth",
    "ROCK_COVERAGE": "coverage", "ROCK_MIN_SCALE": "min radius", "ROCK_MAX_SCALE": "max radius",
    "ROCK_HEIGHT_RATIO": "height / radius", "seed": "seed", "resolution": "resolution",
    "dem": "DEM", "craters": "crater source",
}


def export_settings(params, seed, resolution, dem_path, craters):
    """The settings an export is made from (craters: 'generator' or 'detected')."""
    out = {key: round(float(params[key]), 9) for key in SETTINGS_LABELS if key in params}
    out.update({"seed": seed, "resolution": round(float(resolution), 9),
                "dem": os.path.normcase(os.path.abspath(dem_path)) if dem_path else None,
                "craters": craters})
    return out


def settings_path(rocks_csv):
    """<group>_rocks_settings.json, beside <group>_rocks.csv."""
    return os.path.splitext(rocks_csv)[0] + "_settings.json"


def save_settings(rocks_csv, settings):
    with open(settings_path(rocks_csv), "w", encoding="utf-8") as handle:
        json.dump(settings, handle, indent=1)


def load_settings(rocks_csv):
    """The settings saved with an export, or None (none saved, or unreadable)."""
    try:
        with open(settings_path(rocks_csv), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def settings_changes(saved, current):
    """Readable names of what differs between an export's settings and the current ones."""
    if saved is None:
        return None
    keys = [k for k in SETTINGS_LABELS if saved.get(k) != current.get(k)]
    return [SETTINGS_LABELS[k] for k in keys]


def export_status(rocks_exists, changes):
    """(text, level) under the Export & Unreal buttons: is the rock file current?"""
    if not rocks_exists:
        return "No rock field exported yet: Generate & Export first.", "warn"
    if changes is None:
        return "Rock file exported earlier; its settings were not recorded.", "warn"
    if changes:
        return ("Settings changed since the last export (" + ", ".join(changes) +
                "): Place would use the old field.", "warn")
    return "The exported rock field matches the current settings.", "ok"


def rocks_csv_path(output_dir, base_name):
    """Where a field's rock list is exported: <output_dir>/<base>/<base>_rocks.csv."""
    return os.path.join(output_dir, base_name, f"{base_name}_rocks.csv")


def count_rows(csv_path):
    """Rows of an exported rock list (header excluded), at least 1; 1 if unreadable."""
    try:
        with open(csv_path) as handle:
            return max(sum(1 for _ in handle) - 1, 1)
    except OSError:
        return 1


def export_confirmation(dem_name, files, path_block):
    file_list = "\n".join(f"    {name}\n        {desc}" for name, desc in files)
    return (f"Plan the rock field on {dem_name} and write:\n\n{file_list}\n\nInto:\n"
            + path_block + "\n\nContinue?")


def model_info_text(model_sizes, scale_range, planned_ratio):
    """
    The read-out comparing planned rock sizes with the models: model_sizes is
    rock_models.size_summary (min radius, max radius, median height ratio) or
    None when unknown.
    """
    if not model_sizes:
        return ("Model sizes unknown (only .fbx, or none found): "
                "rocks are scaled to the planned radius.")
    low, high, ratio = model_sizes
    smallest, largest = scale_range
    text = (f"Models: radius {low:.3f}-{high:.3f} m, height {ratio:.2f} x radius.\n"
            f"Planned radii scale them {smallest:.2g}x-{largest:.2g}x.")
    # height_m is a maximum in Unreal (unreal_rocks): taller models are flattened
    # to it, squatter ones keep their proportions and stay below it.
    if ratio > 1.25 * planned_ratio:
        text += (f"\nThe models are taller ({ratio:.2f} x radius) than Height / radius "
                 f"({planned_ratio:.2f}): Unreal flattens them to height_m.")
    elif ratio < 0.8 * planned_ratio:
        text += (f"\nThe models are squatter ({ratio:.2f} x radius) than Height / radius "
                 f"({planned_ratio:.2f}): placed rocks stay lower than height_m in the CSV.")
    return text


def rock_estimate_text(count, max_rocks, on_drivable_ground):
    """The rock-count estimate read-out for `count` rocks."""
    where = ("the drivable ground" if on_drivable_ground
             else "the whole map (before craters are excluded)")
    text = f"About {count:,} rocks on {where}."
    if count > max_rocks:
        text += (f"\nOver the {max_rocks:,} limit: the plan stops there and "
                 "covers less than asked. Lower the coverage or raise the radii.")
    placed = min(count, max_rocks)
    if placed > UNREAL_ROCKS_WARN:
        minutes = placed * UNREAL_SECONDS_PER_ROCK / 60.0
        text += f"\nPlacing them in Unreal takes roughly {minutes:.0f} min."
    return text


def stencil_split_text(scales, boulder, small, radius):
    """How an exported field (`scales`: footprint radii) splits between the two ids."""
    scales = np.asarray(scales)
    n_boulder = int((scales >= radius).sum())
    text = (f"This field: {n_boulder} boulders (id {boulder}), "
            f"{len(scales) - n_boulder} small rocks (id {small}).")
    if len(scales) and n_boulder in (0, len(scales)):
        text += (f"\nAll rocks fall in one class: planned radii run "
                 f"{scales.min():.2f}-{scales.max():.2f} m.")
    return text


def read_rock_scales(csv_path):
    """The footprint radii (column 5) of an exported rock list; raises if unreadable."""
    return np.loadtxt(csv_path, delimiter=",", skiprows=1, usecols=5, ndmin=1)


def result_log_lines(result, max_rocks):
    """[(text, level)] summarising a rock_field.build_rock_field result."""
    lines = []
    if result.get('rock_capped'):
        lines.append((f"The coverage asked for {result['rock_requested']:,} rocks; the plan "
                      f"stops at {max_rocks:,}, so it covers less than asked.", "warn"))
    if result.get('crater_source') == 'generator':
        lines.append((f"Craters: the generator's {result['crater_count']} (exact).", None))
    lines.append((
        f"Placed {result['rock_count']} rocks. "
        f"Drivable {result['drivable_frac'] * 100:.1f}% of map "
        f"(craters cover {result['crater_frac'] * 100:.1f}%, impassable), "
        f"free after rocks {result['free_frac'] * 100:.1f}%.", None))
    if result['road_segments'] == 0:
        lines.append(("Fewer than two craters found, so no Voronoi roads were laid. "
                      "Lower the crater depth threshold to catch shallower craters.", "warn"))
    elif result['connected']:
        lines.append(("Voronoi road network is one connected route.", "ok"))
    else:
        lines.append(("The Voronoi road network is split by impassable terrain into "
                      "more than one piece.", "warn"))
    return lines


def route_status(result):
    """One-phrase state of the road network, for the plan's title."""
    if result['road_segments'] == 0:
        return "no craters found for roads"
    return "connected route OK" if result['connected'] else "route split by terrain"

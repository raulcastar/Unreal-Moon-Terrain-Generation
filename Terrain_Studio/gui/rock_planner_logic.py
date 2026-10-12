"""
Rock Field & Rover Paths logic: the parameter schema, input parsing and the
text read-outs. No Tk, no Matplotlib.
"""

import os

import numpy as np

# Planned-to-model size ratio beyond which the planner warns about visible scaling.
MODEL_SCALE_WARN = 3.0

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
         "to the rover and excluded from rock placement.\n"
         "Lower values flag shallower craters; higher values ignore them."),
    ]),
    ("Rock Population", [
        ("ROCK_COVERAGE", "Coverage (fraction)", 0.0, 0.05, 'float',
         "Target fraction of the drivable ground covered by rock footprints.\n"
         "Small rocks need many more instances for the same coverage: the\n"
         "estimate below shows the count before you preview."),
        ("ROCK_MIN_SCALE", "Min radius (m)", 0.01, 10.0, 'log',
         "Smallest rock footprint radius, in metres. In Unreal each model is\n"
         "scaled uniformly so its footprint matches the planned radius, so plan\n"
         "near the models' own size (Place in Unreal > Match Rock Sizes to\n"
         "Models) to avoid stretching them."),
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


def is_stretched(scale_range):
    """Whether the models are scaled far from their own size (MODEL_SCALE_WARN)."""
    return bool(scale_range) and (scale_range[1] > MODEL_SCALE_WARN
                                  or scale_range[0] < 1.0 / MODEL_SCALE_WARN)


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
    if is_stretched(scale_range):
        text += (" Rocks will look much larger or smaller than modelled: "
                 "use Match Rock Sizes to Models.")
    if abs(planned_ratio - ratio) > 0.25 * ratio:
        text += (f"\nHeight / radius is {planned_ratio:.2f}, the models are {ratio:.2f}: "
                 "height_m in the CSV will not match the placed rocks.")
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

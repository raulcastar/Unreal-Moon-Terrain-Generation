"""
Generate view logic: the parameter schema, seeds, preview size, the text
read-outs and the geometry of what is on screen. No Tk, no Matplotlib.
"""

import os

import numpy as np

DEFAULT_BASE_NAME = "procedural_lunar_terrain"

# (key, label, minimum, maximum, kind, tooltip)
#   kind: 'int'   -> integer slider
#         'float' -> linear float slider
#         'log'   -> logarithmic slider (for values spanning orders of magnitude)
PARAM_GROUPS = [
    ("Map Geometry", [
        ("ROWS", "Rows (px)", 64, 8192, 'int',
         "Vertical pixel count of the generated DEM."),
        ("COLS", "Columns (px)", 64, 8192, 'int',
         "Horizontal pixel count of the generated DEM."),
        ("RESOLUTION_M_PER_PX", "Resolution (m/px)", 0.01, 10.0, 'float',
         "Ground distance covered by one pixel. Drives the Unreal landscape XY scale;\n"
         "it does not change the generated relief."),
        ("BASE_ELEVATION", "Base elevation (m)", 0.0, 500.0, 'float',
         "Datum the terrain is built around. Kept above zero because the Unreal\n"
         "converter treats exact zeros as 'no data'."),
    ]),
    ("Macro Terrain (rolling hills)", [
        ("MACRO_ELEVATION_MIN", "Relief min (m)", 0.0, 200.0, 'float',
         "Lower bound of the total hill height differential across the map."),
        ("MACRO_ELEVATION_MAX", "Relief max (m)", 0.0, 200.0, 'float',
         "Upper bound of the total hill height differential. The actual value is\n"
         "drawn uniformly between min and max each run."),
        ("EROSION_BLUR_DIVISOR", "Erosion divisor", 2.0, 400.0, 'float',
         "Smoothing sigma is (largest map dimension / this value).\n"
         "Lower = heavily eroded and smooth. Higher = sharper terrain."),
    ]),
    ("Micro Terrain", [
        ("MICRO_ROUGHNESS", "Micro roughness (m)", 1e-5, 1.0, 'log',
         "Amplitude of high-frequency noise. Acts as dithering that prevents\n"
         "16-bit quantization banding (terracing) once imported into Unreal."),
    ]),
    ("Crater Morphology", [
        ("CRATER_DENSITY", "Crater density", 1e-7, 1e-2, 'log',
         "Impacts per pixel of surface area. Total craters = rows x cols x density."),
        ("CRATER_MIN_RADIUS_PCT", "Min radius (% of map)", 0.0005, 0.3, 'log',
         "Smallest crater radius as a fraction of the largest map dimension."),
        ("CRATER_MAX_RADIUS_PCT", "Max radius (% of map)", 0.001, 0.5, 'log',
         "Largest crater radius as a fraction of the largest map dimension.\n"
         "Radii are cubically biased towards the minimum, as on real surfaces."),
        ("CRATER_DEPTH_RATIO", "Depth ratio", 0.001, 1.0, 'log',
         "Floor depth below the surrounding ground, relative to crater radius.\n"
         "Rim-to-floor depth is this x (1 + rim ratio). Mature mare craters sit\n"
         "near 0.15, fresh craters up to about 0.3."),
        ("CRATER_RIM_HEIGHT_RATIO", "Rim height ratio", 0.0, 1.0, 'float',
         "Ejecta rim height relative to the bowl depth. 1.0 raises the rim as far\n"
         "as the bowl digs down."),
    ]),
]

COLORMAPS = ["gray", "magma", "cividis", "bone", "terrain", "gist_earth", "inferno"]

PREVIEW_SIZES = ["128", "192", "256", "384", "512", "768"]

# Above this many pixels, the confirmation warns about time and memory.
LARGE_MAP_PIXELS = 25_000_000


def base_name_or_default(text):
    """The export base filename, or the default when the field is blank."""
    return text.strip() or DEFAULT_BASE_NAME


def parse_seed(text):
    """An integer seed from the seed field, or None when blank or not a number."""
    text = text.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def roll_seed():
    return int(np.random.randint(0, 2 ** 31 - 1))


def preview_shape(rows, cols, target):
    """(rows, cols) of a preview whose longest edge is at most `target`, at least 32 px."""
    scale = min(1.0, target / max(rows, cols))
    return max(32, int(round(rows * scale))), max(32, int(round(cols * scale)))


def list_presets(config_dir):
    """The preset files (.yaml/.yml) in config_dir, sorted; creates the folder if missing."""
    os.makedirs(config_dir, exist_ok=True)
    return sorted(f for f in os.listdir(config_dir) if f.endswith((".yaml", ".yml")))


def output_preview_text(base_name, files):
    """The files Step 1 writes, as listed under the base filename field."""
    return f"outputs/{base_name}/\n" + "\n".join(f"  {name}" for name, _ in files)


def generation_confirmation(params, seed, path_block, files):
    """The confirmation text for a full-resolution run (path_block: the formatted folder)."""
    pixels = params['ROWS'] * params['COLS']
    file_list = "\n".join(f"    {name}\n        {desc}" for name, desc in files)
    message = (
        f"Generate a {params['COLS']} x {params['ROWS']} px DEM "
        f"({pixels / 1e6:.1f} megapixels) with seed {seed}.\n\n"
        "Files will be written to:\n"
        + path_block + f"\n\n{file_list}"
    )
    if pixels > LARGE_MAP_PIXELS:
        message += (
            "\n\nThis map is large: generation and file writing may take a "
            "while and use several GB of RAM. You can abort with the Stop "
            "button once it starts."
        )
    return message + "\n\nContinue?"


def slope_degrees(elevation, resolution):
    """Slope magnitude (deg) of an elevation grid with square cells of `resolution` m."""
    gradient_y, gradient_x = np.gradient(elevation, resolution, resolution)
    return np.degrees(np.arctan(np.hypot(gradient_x, gradient_y)))


def display_geometry(elevation, params):
    """
    (extent_in_metres, effective_resolution) of a render of `elevation`, which
    may be a downsampled preview of the map params describe.
    """
    rows, cols = elevation.shape
    scale = max(params['ROWS'], params['COLS']) / max(rows, cols)
    effective_res = params['RESOLUTION_M_PER_PX'] * scale
    return (0, cols * effective_res, 0, rows * effective_res), effective_res


def stats_lines(params, crater_count, displayed=None, full_resolution=False):
    """
    The Surface Statistics read-out: what the parameters imply, and, when a
    render is on screen (`displayed`, a generate_terrain result), its measures.
    """
    rows, cols = params['ROWS'], params['COLS']
    resolution = params['RESOLUTION_M_PER_PX']
    lines = [
        f"DEM grid        {cols} x {rows} px",
        f"Ground extent   {cols * resolution:,.0f} x {rows * resolution:,.0f} m",
        f"Resolution      {resolution:g} m/px",
        f"Target craters  {crater_count}",
        f"Crater radii    {max(rows, cols) * params['CRATER_MIN_RADIUS_PCT'] * resolution:,.1f}"
        f" - {max(rows, cols) * params['CRATER_MAX_RADIUS_PCT'] * resolution:,.1f} m",
        f"Erosion sigma   {max(1.0, max(rows, cols) / params['EROSION_BLUR_DIVISOR']):,.1f} px",
    ]
    if displayed is not None:
        elevation = displayed['elevation']
        _extent, effective_res = display_geometry(elevation, params)
        slope = slope_degrees(elevation, effective_res)
        source = "full resolution" if full_resolution else "preview"
        lines += [
            "",
            f"-- rendered ({source}) --",
            f"Min elevation   {np.min(elevation):,.3f} m",
            f"Max elevation   {np.max(elevation):,.3f} m",
            f"Mean elevation  {np.mean(elevation):,.3f} m",
            f"Total relief    {np.ptp(elevation):,.3f} m",
            f"Craters placed  {displayed['crater_count']}",
            f"Mean slope      {np.mean(slope):,.2f} deg",
            f"P99 slope       {np.percentile(slope, 99):,.2f} deg",
            f"Max slope       {np.max(slope):,.2f} deg",
        ]
    return lines


def topology_text(params, config, loader_error=None):
    """
    The Unreal Landscape Topology read-out for the map size, from the
    loader's component solution (heightmap_files.calculate_optimal_landscape_config).
    `loader_error` set: the loader modules are unavailable.
    """
    if loader_error is not None:
        return "Unreal loader modules unavailable.\n" + str(loader_error)
    if config is None:
        return ("DEM exceeds the 50x50 component\n"
                "ceiling of a single Landscape.\n\n"
                "Reduce rows/columns or split the\n"
                "map across World Partition tiles.")
    rows, cols = params['ROWS'], params['COLS']
    resolution = params['RESOLUTION_M_PER_PX']
    comp_x, comp_y, quads, sections, final_width, final_height = config
    return "\n".join([
        f"Section size    {quads} quads",
        f"Sections/comp   {sections}",
        f"Components      {comp_x} x {comp_y}",
        f"Landscape verts {final_width} x {final_height}",
        f"Total vertices  {final_width * final_height:,}",
        f"Edge padding    {(final_width - cols) + (final_height - rows)} px",
        f"World size      {final_width * resolution:,.0f} x"
        f" {final_height * resolution:,.0f} m",
        f"XY scale        {resolution * 100:g} (cm/px)",
    ])


def title_suffix(displayed, full_resolution):
    """'<cols>x<rows> preview - seed <seed>' for the figure titles."""
    rows, cols = displayed['elevation'].shape
    source = "FULL RESOLUTION" if full_resolution else "preview"
    return f"{cols}x{rows} {source} - seed {displayed['seed']}"

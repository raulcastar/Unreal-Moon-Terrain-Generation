"""
Cropping a DEM to a subsection before it goes to Unreal (no Tk).

Real lunar DEMs often exceed 10 000 px on a side, far more than one Landscape:
past 4097 x 4097 vertices Unreal needs the manual import, and past 50 x 50
components it cannot hold the map at all. The Send to Unreal and Lunar Datasets
views let the user drag a rectangle over the map; this module turns that
rectangle into pixel bounds, snaps it to a size Unreal builds without padding,
describes the Landscape it would give, and writes the crop as a new DEM so the
rock planner, the masks and the sun use the same subsection as the Landscape.

Bounds are always (row, col, rows, cols) in full-resolution pixels of the
source map, with (row, col) the top-left corner.
"""

import json
import os

import numpy as np

from gui import paths
from gui.dem_files import dem_group_name, load_dem_array

import heightmap_files


# The vertex counts per component edge that Unreal's Landscape accepts
# (quads per section x sections per component).
_COMPONENT_EDGES = sorted({quads * sections
                           for quads in (63, 127, 255) for sections in (1, 2)})


# Every Landscape edge length (in vertices) that needs no edge padding: a whole
# number of components plus the shared last vertex.
LANDSCAPE_SIZES = sorted({components * edge + 1
                          for edge in _COMPONENT_EDGES
                          for components in range(1, heightmap_files.MAX_COMPONENT_GRID_SIZE + 1)})


# The smallest crop: one component of the smallest topology. Smaller rectangles
# are almost always a stray click, and Unreal cannot build less anyway.
MIN_CROP = LANDSCAPE_SIZES[0]


# The largest Landscape the studio imports automatically; bigger ones fall back
# to the manual import in the editor (docs/terrain.md, Send to Unreal).
AUTO_IMPORT_MAX = 4097


# Past this many vertices a side, building the Nanite mesh on save takes a lot of
# memory: a 2269 x 2080 Landscape ran a 64 GB machine out of memory while the
# editor held other Landscapes (docs/terrain.md, Send to Unreal).
NANITE_WARN_EDGE = 2017


def nanite_warning(shape):
    """
    A warning for a Nanite Landscape of `shape` (rows, cols) large enough to
    strain the editor's memory when it saves, or None.
    """
    rows, cols = shape
    if max(rows, cols) <= NANITE_WARN_EDGE:
        return None
    return (f"Nanite on a {cols} x {rows} Landscape: Unreal builds its Nanite mesh when the "
            f"level is saved, which can freeze the editor for minutes and run it out of "
            f"memory. Untick 'Nanite Landscape' (or send a smaller region) if the "
            f"editor is short of memory.")


def clamp_bounds(row, col, rows, cols, shape):
    """
    Bounds kept inside a map of `shape` (rows, cols), at least MIN_CROP a side
    when the map allows it. Returns integers.
    """
    map_rows, map_cols = shape
    rows = int(round(min(max(rows, min(MIN_CROP, map_rows)), map_rows)))
    cols = int(round(min(max(cols, min(MIN_CROP, map_cols)), map_cols)))
    row = int(round(min(max(row, 0), map_rows - rows)))
    col = int(round(min(max(col, 0), map_cols - cols)))
    return row, col, rows, cols


def whole_map(shape):
    """The bounds that select the entire map."""
    return 0, 0, int(shape[0]), int(shape[1])


def default_crop(shape):
    """
    Where the rectangle starts: the whole map when the studio can import it
    itself, otherwise the largest padding-free square it can, centred.
    """
    if max(shape) <= AUTO_IMPORT_MAX:
        return whole_map(shape)
    side_rows = snap_size(min(shape[0], AUTO_IMPORT_MAX))
    side_cols = snap_size(min(shape[1], AUTO_IMPORT_MAX))
    side = min(side_rows, side_cols)
    return clamp_bounds((shape[0] - side) / 2.0, (shape[1] - side) / 2.0, side, side, shape)


def display_to_bounds(x0, x1, y0, y1, shape):
    """
    Full-resolution bounds of a rectangle drawn on the preview.

    The preview image spans the map's pixel edges (0..cols, 0..rows) whatever
    its downsampling, so its coordinates are already map pixels.
    """
    left, right = sorted((x0, x1))
    top, bottom = sorted((y0, y1))
    return clamp_bounds(top, left, bottom - top, right - left, shape)


def bounds_to_display(bounds):
    """The preview rectangle (x0, x1, y0, y1) that draws `bounds`; inverse of display_to_bounds."""
    row, col, rows, cols = bounds
    return (col, col + cols, row, row + rows)


def snap_size(size):
    """The largest padding-free Landscape edge that fits in `size` pixels (MIN_CROP at least)."""
    fitting = [s for s in LANDSCAPE_SIZES if s <= size]
    return fitting[-1] if fitting else LANDSCAPE_SIZES[0]


def snap_pair(rows, cols):
    """
    The largest padding-free (rows, cols) that fits in `rows` x `cols`.

    One Landscape uses a single component size in both directions, so the two
    edges are snapped together: snapping them one by one can pick, say, 63-quad
    components for the width and 255-quad ones for the height, and Unreal then
    pads one side. Ties go to the larger component (fewer of them).
    """
    best = None
    for edge in _COMPONENT_EDGES:
        components_y = min((rows - 1) // edge, heightmap_files.MAX_COMPONENT_GRID_SIZE)
        components_x = min((cols - 1) // edge, heightmap_files.MAX_COMPONENT_GRID_SIZE)
        if components_x < 1 or components_y < 1:
            continue
        candidate = (components_y * edge + 1, components_x * edge + 1)
        area = candidate[0] * candidate[1]
        if best is None or area >= best[0]:
            best = (area, candidate)
    return best[1] if best else (rows, cols)


def snap_bounds(bounds, shape):
    """
    `bounds` shrunk to a padding-free Landscape size, keeping its centre, then
    kept inside the map. A side smaller than one component is left as it is:
    there is nothing smaller to snap to.
    """
    row, col, rows, cols = bounds
    new_rows, new_cols = snap_pair(rows, cols)
    centre_row, centre_col = row + rows / 2.0, col + cols / 2.0
    return clamp_bounds(centre_row - new_rows / 2.0, centre_col - new_cols / 2.0,
                        new_rows, new_cols, shape)


def describe_crop(bounds, resolution, shape=None):
    """
    What `bounds` would give in Unreal, as a dict:

        rows, cols, width_m, height_m        the crop in pixels and metres
        layout                               (comp_x, comp_y, quads, sections,
                                              vert_x, vert_y), or None
        padding                              (extra rows, extra cols) Unreal adds
        fits                                 one Landscape can hold it
        auto_import                          the studio can import it itself
        whole                                it is the entire map
        lines                                the read-out, one string per line
    """
    row, col, rows, cols = bounds
    layout = heightmap_files.calculate_optimal_landscape_config(cols, rows)
    whole = shape is not None and tuple(bounds) == whole_map(shape)

    lines = [f"{cols} x {rows} px"
             + ("  (whole map)" if whole else f"  at x {col}, y {row}"),
             f"{cols * resolution:,.0f} x {rows * resolution:,.0f} m at {resolution:g} m/px"]
    if layout is None:
        lines.append("Too large for one Landscape (more than 50 x 50 components).")
        return {"rows": rows, "cols": cols, "width_m": cols * resolution,
                "height_m": rows * resolution, "layout": None, "padding": None,
                "fits": False, "auto_import": False, "whole": whole, "lines": lines}

    comp_x, comp_y, quads, sections, vert_x, vert_y = layout
    padding = (vert_y - rows, vert_x - cols)
    lines.append(f"Landscape {comp_x} x {comp_y} components, {quads} quads x "
                 f"{sections} section{'s' if sections > 1 else ''}")
    lines.append(f"{vert_x} x {vert_y} vertices, "
                 + ("no padding" if padding == (0, 0)
                    else f"{padding[1]} x {padding[0]} px of edge padding"))
    auto_import = max(vert_x, vert_y) <= AUTO_IMPORT_MAX
    if not auto_import:
        lines.append(f"Over {AUTO_IMPORT_MAX} vertices: Unreal needs the manual import.")
    return {"rows": rows, "cols": cols, "width_m": cols * resolution,
            "height_m": rows * resolution, "layout": layout, "padding": padding,
            "fits": True, "auto_import": auto_import, "whole": whole, "lines": lines}


def native_resolution(path):
    """
    Metres per pixel a map carries itself: the resolution recorded beside a
    saved crop, a generated map's crater list (each crater in pixels and in
    metres), or a GeoTIFF's pixel scale. None when the map has none.
    """
    info = read_crop_info(path)
    if info and info.get("resolution_m_per_px"):
        return float(info["resolution_m_per_px"])
    from_craters = _crater_list_resolution(path)
    if from_craters:
        return from_craters
    if os.path.splitext(path)[1].lower() not in (".tif", ".tiff"):
        return None
    try:
        import tifffile
        with tifffile.TiffFile(path) as tif:
            tags = tif.pages[0].tags
            if "ModelPixelScaleTag" in tags:
                scale = float(tags["ModelPixelScaleTag"].value[0])
                return scale if scale > 0 else None
    except Exception:
        return None
    return None


def _crater_list_resolution(path):
    """
    Metres per pixel from the generator's <group>_craters.csv beside a map
    (radius_m / radius_px of its largest crater, the most precise), or None.
    """
    craters = os.path.join(os.path.dirname(path), f"{dem_group_name(path)}_craters.csv")
    try:
        with open(craters, encoding="utf-8") as handle:
            header = handle.readline().strip().split(",")
            px, m = header.index("radius_px"), header.index("radius_m")
            best = max((line.strip().split(",") for line in handle if line.strip()),
                       key=lambda row: float(row[px]), default=None)
    except (OSError, ValueError):
        return None
    if best is None or float(best[px]) <= 0:
        return None
    return round(float(best[m]) / float(best[px]), 6)


def crop_name(source_path, bounds):
    """The crop's name: its source's map name and where it was cut from."""
    row, col, rows, cols = bounds
    return f"{dem_group_name(source_path)}_crop_x{col}_y{row}_{cols}x{rows}"


def crop_paths(source_path, bounds):
    """(folder, DEM path, info path) a crop of `source_path` is written to."""
    name = crop_name(source_path, bounds)
    # Read at call time: tests redirect the outputs folder.
    folder = os.path.join(paths.OUTPUT_ROOT, name)
    return folder, os.path.join(folder, f"{name}.npy"), os.path.join(folder, f"{name}_crop.json")


def read_crop_info(path):
    """The info written beside a saved crop DEM, or None for any other map."""
    stem = os.path.splitext(path)[0]
    try:
        with open(f"{stem}_crop.json", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def read_window(source_path, bounds):
    """
    The `bounds` window of a DEM as float32, missing data as NaN.

    A .npy is memory-mapped so only the window is read; other formats have no
    random access here and are loaded whole first.
    """
    row, col, rows, cols = bounds
    if os.path.splitext(source_path)[1].lower() == ".npy":
        data = np.load(source_path, mmap_mode="r")
        if data.ndim != 2:
            raise ValueError(f"Expected a 2D map, got shape {data.shape}.")
        window = np.array(data[row:row + rows, col:col + cols], dtype=np.float32)
    else:
        window = load_dem_array(source_path)[row:row + rows, col:col + cols].astype(np.float32)
    if window.shape != (rows, cols):
        raise ValueError(f"The crop {cols} x {rows} at x {col}, y {row} is outside the map.")
    return window


def save_crop(source_path, bounds, resolution=None):
    """
    Writes the `bounds` window of `source_path` as a new DEM with its info, and
    returns (DEM path, info dict). See write_crop.
    """
    window = read_window(source_path, bounds)
    return write_crop(source_path, bounds, window,
                      resolution or native_resolution(source_path),
                      source=os.path.abspath(source_path))


def write_crop(source_name, bounds, window, resolution, source=None):
    """
    Writes an already-read crop `window` as a new DEM with its info, and
    returns (DEM path, info dict). `source_name` (a path or a URL) names the
    crop; `source`, when given, is what the info records as its origin.

    The crop is a .npy in its own outputs/ folder, so every later step (rocks,
    masks, sun, the Landscape) groups its files there, as for a generated map.
    The info keeps the source and the bounds, so the crop can be traced back,
    and the resolution, which a GeoTIFF source carried in its tags and a .npy
    cannot.
    """
    if not np.isfinite(window).any():
        raise ValueError("The selected region has no elevation data (all missing).")
    folder, dem_path, info_path = crop_paths(source_name, bounds)
    os.makedirs(folder, exist_ok=True)
    np.save(dem_path, np.asarray(window, dtype=np.float32))

    row, col, rows, cols = bounds
    info = {"source": source or source_name,
            "x": col, "y": row, "width": cols, "height": rows,
            "resolution_m_per_px": resolution,
            "missing_fraction": round(float(np.mean(~np.isfinite(window))), 6)}
    with open(info_path, "w", encoding="utf-8") as handle:
        json.dump(info, handle, indent=2)
    return dem_path, info

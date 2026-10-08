"""
Finding, describing and loading DEM files, and the files each step writes.
"""

import os

import tkinter as tk
import numpy as np

from gui import paths
from gui.backend import LOADER_AVAILABLE


# =========================================================================
# UNREAL EXPORT PIPELINE
# =========================================================================

SUPPORTED_DEM_EXTENSIONS = ('.npy', '.csv', '.txt', '.tif', '.tiff')


DEM_FILETYPES = [("DEM files", "*.npy *.csv *.txt *.tif *.tiff"),
                 ("All files", "*.*")]


# Rasters written beside a DEM with the same extensions but which are not
# elevation. Importing one would build a Landscape out of mask values, so they
# are excluded from the map lists.
NON_DEM_TAGS = ('_classmap', '_soilmap', '_obstaclemap', '_rocks',
                '_roverpaths', '_converted', '_heightmap')


def dem_source_note(path):
    """
    Warning about `path` as an elevation source, or "" when it looks right.

    The file picker cannot filter companion rasters, and using one fails
    silently, so the mismatch is reported rather than left to the output.
    """
    if not path:
        return ""
    name = os.path.basename(path)
    if looks_like_dem(name):
        return ""
    extension = os.path.splitext(name)[1].lower()
    if extension not in SUPPORTED_DEM_EXTENSIONS:
        return f"{extension or 'This file'} is not a supported DEM format."
    return ("This looks like a companion raster, not an elevation map. "
            "Its values are mask indices, not heights.")


def build_dem_menu(parent, on_pick, on_browse):
    """
    Drop-down of the DEMs in the project's folders, grouped by folder.

    Companion rasters are filtered out. The file picker stays on the menu for
    maps held elsewhere.
    """
    menu = tk.Menu(parent, tearoff=0)
    groups = find_dem_files()

    if not groups:
        menu.add_command(state="disabled",
                         label="No maps in input_maps/ or outputs/")
    for index, (label, entries, total) in enumerate(groups):
        if index:
            menu.add_separator()
        menu.add_command(label=f"{label}/", state="disabled")
        for name, path in entries:
            menu.add_command(label=f"    {name}",
                             command=lambda p=path: on_pick(p))
        if total > len(entries):
            menu.add_command(state="disabled",
                             label=f"    ... {total - len(entries)} more, use Browse below")

    # The list is one level deep; the picker covers anything further down.
    menu.add_separator()
    for root, label in ((paths.OUTPUT_ROOT, "outputs"), (paths.INPUT_MAPS_DIR, "input_maps")):
        if os.path.isdir(root):
            menu.add_command(label=f"Browse in {label}/...",
                             command=lambda d=root: on_browse(d))
        else:
            menu.add_command(state="disabled", label=f"Browse in {label}/  (no folder yet)")
    menu.add_command(label="Browse elsewhere...", command=lambda: on_browse(None))
    return menu


def drop_dem_menu(parent, button, on_pick, on_browse):
    """Posts the DEM menu under `button`, rebuilt each time so it stays current."""
    menu = build_dem_menu(parent, on_pick, on_browse)
    x = button.winfo_rootx()
    y = button.winfo_rooty() + button.winfo_height()
    try:
        menu.tk_popup(x, y)
    finally:
        menu.grab_release()


def planned_output_files(base_name, export_csv):
    """
    Files a full-resolution generation writes, as [(filename, description)].

    Shared by the panel that previews them and the dialog that confirms them,
    so neither can drift from what is written.
    """
    elevation_name = f"{base_name}_elevationMap"
    files = [(f"{elevation_name}.npy",
              "Elevation grid as a NumPy binary; the input for Send to Unreal."),
             (f"{base_name}_craters.csv",
              "Every crater placed (centre, radius, depth): exact crater ground truth.")]
    if export_csv:
        files += [
            (f"{elevation_name}.csv",
             "Elevation grid as space-separated metres, one row per line."),
            (f"{base_name}_soilMap.csv",
             "Soil/material mask (interior 1, border 0) for engine blending."),
        ]
    return files


def planned_rock_files(base_name, obstacle_map):
    """
    Files a rock-field export writes, as [(filename, description)].

    Same contract as planned_output_files.
    """
    files = [
        (f"{base_name}_rocks.csv",
         "Rock instances: id, row, col, x_m, y_m, scale, height_m."),
        (f"{base_name}_roverPaths.csv",
         "Rover corridor polylines: path_id, x_m, y_m."),
    ]
    if obstacle_map:
        files.append((f"{base_name}_obstacleMap.csv",
                      "Full-resolution 0/1 rock-footprint raster."))
    return files


def dem_group_name(path):
    """The output folder name a DEM belongs to: its stem without '_elevationMap'."""
    stem = os.path.splitext(os.path.basename(path))[0]
    return stem.replace("_elevationMap", "")


def looks_like_dem(filename):
    """True when `filename` is a supported elevation raster rather than a companion."""
    stem, extension = os.path.splitext(filename)
    if extension.lower() not in SUPPORTED_DEM_EXTENSIONS:
        return False
    lowered = stem.lower()
    return not any(tag in lowered for tag in NON_DEM_TAGS)


def find_dem_files(max_per_group=30):
    """DEMs in the project folders, as [(group label, [(name, path)], total)]."""
    groups = []
    for root, label in ((paths.INPUT_MAPS_DIR, "input_maps"), (paths.OUTPUT_ROOT, "outputs")):
        found = scan_dem_folder(root)
        if found:
            groups.append((label, found[:max_per_group], len(found)))
    return groups


def scan_dem_folder(root, include_companions=False):
    """
    Maps in `root` and one level of subfolders, as [(display name, path)].

    One level matches how both project folders are laid out and keeps the walk
    cheap. `include_companions` also lists the masks and tables.
    """
    if not os.path.isdir(root):
        return []

    def wanted(filename):
        if include_companions:
            return os.path.splitext(filename)[1].lower() in SUPPORTED_DEM_EXTENSIONS
        return looks_like_dem(filename)

    found = []
    try:
        entries = sorted(os.listdir(root), key=str.lower)
    except OSError:
        return []
    for entry in entries:
        full = os.path.join(root, entry)
        if os.path.isfile(full):
            if wanted(entry):
                found.append((entry, full))
        elif os.path.isdir(full):
            try:
                children = sorted(os.listdir(full), key=str.lower)
            except OSError:
                continue
            for child in children:
                child_path = os.path.join(full, child)
                if os.path.isfile(child_path) and wanted(child):
                    found.append((f"{entry}/{child}", child_path))
    return found


def dem_source_problem(source_path):
    """
    Why `source_path` cannot be sent to Unreal, as (message, level), or None.

    Shared by both entry points so they refuse the same inputs.
    """
    if not LOADER_AVAILABLE:
        return ("Unreal loader modules are not importable.", "error")
    if not source_path:
        return ("Select a map to send: generate one, or use Browse.", "warn")
    if not os.path.exists(source_path):
        return (f"Source map not found: {source_path}", "error")
    if os.path.splitext(source_path)[1].lower() not in SUPPORTED_DEM_EXTENSIONS:
        return ("Unsupported map format (use .npy, .csv, .txt, or .tif).", "error")
    return None


def load_dem_array(path):
    """
    Loads a DEM file into a 2D float array. Supports .npy, .csv/.txt (whitespace
    or comma delimited), and .tif/.tiff (requires tifffile). Raises ValueError
    with a readable message on failure.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == '.npy':
        data = np.load(path)
    elif ext in ('.tif', '.tiff'):
        try:
            import tifffile
        except ImportError:
            raise ValueError("Reading .tif needs the 'tifffile' package.")
        data = tifffile.imread(path)
    elif ext in ('.csv', '.txt'):
        try:
            data = np.loadtxt(path)              # whitespace-delimited
        except ValueError:
            data = np.loadtxt(path, delimiter=',')
    else:
        raise ValueError(f"Unsupported map format '{ext}'.")

    data = np.asarray(data, dtype=float)
    if data.ndim != 2:
        raise ValueError(f"Expected a 2D map, got shape {data.shape}.")
    return data

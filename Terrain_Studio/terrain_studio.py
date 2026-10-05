"""
Lunar Terrain Studio
--------------------
Graphical front-end for the procedural lunar DEM pipeline.

Three stages are exposed in a single window:

  1. PARAMETERS  - every knob of the procedural simulation, with live tooltips
                   and YAML preset load/save (shared with the CLI generator).
  2. PREVIEW     - a fast, reduced-resolution render of the exact same pipeline,
                   shown as a sun-lit shaded relief, a 3D surface, and a slope
                   analysis, so the surface can be judged before committing to a
                   full-resolution run.
  3. UNREAL      - full-resolution generation, conversion to a 16-bit .r16
                   heightmap, and dispatch to a running Unreal Editor over the
                   Remote Execution protocol.

Dependencies are limited to what the pipeline already requires (numpy, scipy,
pandas, pyyaml, matplotlib) plus tkinter from the standard library.

Run with:  python Terrain_Studio/terrain_studio.py
"""

import ctypes
import io
import json
import math
import os
import queue
import subprocess
import sys
import textwrap
import threading
import traceback
from contextlib import redirect_stdout

import tkinter as tk
from tkinter import filedialog, ttk

import matplotlib
matplotlib.use("TkAgg")

import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.cm import ScalarMappable
from matplotlib.colors import LightSource, Normalize
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.ticker import MaxNLocator

# =========================================================================
# PROJECT PATHS & SIBLING MODULE IMPORTS
# =========================================================================

STUDIO_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(STUDIO_DIR)
GENERATOR_DIR = os.path.join(STUDIO_DIR, "Procedural_generator")
LOADER_DIR = os.path.join(STUDIO_DIR, "Lunar_terrain_loader")
# Presets, source DEMs and generated files all sit at the repository root, so
# they are visible without digging into the code folders.
CONFIG_DIR = os.path.join(PROJECT_ROOT, "config")

# All generated files go under outputs/<map name>/ at the repository root.
OUTPUT_ROOT = os.path.join(PROJECT_ROOT, "outputs")
GENERATOR_OUTPUT_DIR = OUTPUT_ROOT
INPUT_MAPS_DIR = os.path.join(PROJECT_ROOT, "input_maps")

# Home screen: banner rendered in the Moon Simulator, and its dark palette.
HOME_BANNER = os.path.join(STUDIO_DIR, "assets", "home_banner.jpg")
HOME_COLOURS = {
    "bg": "#2e3540", "card": "#3a4250", "hover": "#465063", "border": "#525d6e",
    "accent": "#f5c060", "text": "#f2f5f9", "muted": "#b7c0cd",
}


def map_browse_dir():
    """Where a DEM picker opens: generated maps first, then the raw input maps."""
    for candidate in (OUTPUT_ROOT, INPUT_MAPS_DIR):
        if os.path.isdir(candidate):
            return candidate
    return PROJECT_ROOT


def output_group_dir(source_path):
    """Per-map output folder: the filename without extension or '_elevationMap'."""
    stem = os.path.splitext(os.path.basename(source_path))[0]
    return os.path.join(OUTPUT_ROOT, stem.replace("_elevationMap", ""))

# Both are plain script directories living beside this file, so they are placed
# on the import path rather than being restructured into installable packages.
for path in (GENERATOR_DIR, LOADER_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

import terrain_core  # noqa: E402  (path setup must precede the import)
import rock_field     # noqa: E402
import sun_shadows    # noqa: E402
import crater_mask    # noqa: E402

# The Unreal loader is optional: the studio still generates and previews terrain
# on a machine without the loader dependencies available.
try:
    import terrain_files_generator  # noqa: E402
    import main as lunar_loader     # noqa: E402
    import rocks_to_unreal          # noqa: E402
    import rock_models              # noqa: E402
    import sun_to_unreal            # noqa: E402
    import crater_mask_to_unreal    # noqa: E402
    LOADER_AVAILABLE = True
    LOADER_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - environment dependent
    LOADER_AVAILABLE = False
    LOADER_IMPORT_ERROR = exc


# =========================================================================
# PARAMETER SCHEMA FOR THE UI
# =========================================================================

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

# Longest edge of the 3D mesh, at rest and while the view is dragged. Matplotlib
# depth-sorts every quad per frame, so redraw cost grows with the square of these;
# the drag value is kept low to stay interactive (~60 ms/frame vs ~600 at full).
SURFACE_MESH_DETAIL = 100
SURFACE_DRAG_DETAIL = 36


# Planned-to-model size ratio beyond which the planner warns about visible scaling.
MODEL_SCALE_WARN = 3.0
# Measured Unreal placement cost (10,338 rocks in 158 s). Above UNREAL_ROCKS_WARN
# rocks, the estimate also reports the expected placement time.
UNREAL_SECONDS_PER_ROCK = 0.015
UNREAL_ROCKS_WARN = 20_000


# Parameter schema for the Rock Field & Rover Paths window (same tuple layout as
# PARAM_GROUPS). Keys map directly onto rock_field.DEFAULTS.
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
         "Largest rock footprint radius, in metres. Radii are cubically biased\n"
         "towards the minimum."),
        ("ROCK_HEIGHT_RATIO", "Height / radius", 0.1, 3.0, 'float',
         "Rock height as a multiple of its footprint radius, written to the\n"
         "instance list as height_m. Unreal keeps each model's own proportions,\n"
         "so set this to the models' ratio for height_m to match what is placed."),
    ]),
]


# =========================================================================
# SMALL UI HELPERS
# =========================================================================

def enable_dpi_awareness():
    """
    Opts the process into per-monitor DPI awareness on Windows.

    Without awareness a scaled display (125%/150%) hands Tk a virtualized
    resolution and bitmap-upscales the window, blurring it. With only *system*
    awareness the window is still bitmap-stretched when dragged to a monitor with
    a different scale factor. Per-Monitor-v2 keeps every monitor crisp; the app
    then rescales its widgets on the DPI-change events Windows sends (see
    TerrainStudio._apply_dpi_change). Falls back to older modes on old Windows.
    """
    if sys.platform != "win32":
        return
    user32 = ctypes.windll.user32
    # Per-Monitor-Aware v2 (Windows 10 1703+).
    try:
        user32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    # Per-Monitor aware (Windows 8.1+); HRESULT S_OK == 0.
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return
    except Exception:
        pass
    # System DPI aware, then the legacy call.
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


class Tooltip:
    """Lightweight hover tooltip for any Tk widget."""

    def __init__(self, widget, text, delay=450):
        self.widget = widget
        self.text = text
        self.delay = delay
        self._after_id = None
        self._window = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self._after_id = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._after_id:
            self.widget.after_cancel(self._after_id)
            self._after_id = None

    def _show(self):
        if self._window or not self.text:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self._window = tk.Toplevel(self.widget)
        self._window.wm_overrideredirect(True)
        self._window.wm_geometry(f"+{x}+{y}")
        tk.Label(
            self._window, text=self.text, justify="left",
            background="#232323", foreground="#e8e8e8", relief="solid",
            borderwidth=1, font=("Segoe UI", 8), padx=8, pady=5,
        ).pack()

    def _hide(self, _event=None):
        self._cancel()
        if self._window:
            self._window.destroy()
            self._window = None


class MessageDialog(tk.Toplevel):
    """
    Modal dialog for long, pre-formatted messages.

    Replaces tkinter's messagebox, whose platform dialog re-wraps text at its own
    width and clips long paths. Text is shown verbatim in a monospace widget
    sized to the content, selectable, and scrollable when taller than the screen.
    """

    # Content bounds in character cells, so a short message stays a small dialog
    # and a long one stops growing before it runs off the screen.
    MIN_WIDTH, MAX_WIDTH = 46, 104
    MIN_HEIGHT, MAX_HEIGHT = 3, 26

    def __init__(self, parent, title, message, ok_label="OK", cancel_label=None):
        super().__init__(parent)
        self.result = False
        self.title(title)
        self.transient(parent)
        self.resizable(True, True)

        body = ttk.Frame(self, padding=(14, 12, 14, 8))
        body.pack(fill="both", expand=True)
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        font = ("Consolas", 9)
        lines = message.splitlines() or [""]
        width = min(max(max(len(line) for line in lines), self.MIN_WIDTH), self.MAX_WIDTH)

        self._body = body
        self._scrollbar = None
        text = tk.Text(body, wrap="word", width=width, height=self.MIN_HEIGHT, font=font,
                       relief="flat", padx=6, pady=4, cursor="arrow",
                       background=self.cget("background"),
                       highlightthickness=0, borderwidth=0)
        text.grid(row=0, column=0, sticky="nsew")
        text.insert("1.0", message)
        self._text = text

        # Size from an estimate now so the dialog opens at roughly the right
        # size, then let _fit_to_content correct it against Tk's own layout.
        self._apply_height(self._wrapped_rows(lines, width))

        # Read-only, but still selectable: 'disabled' would block copying, so the
        # edit keys are swallowed instead.
        text.bind("<Key>", self._swallow_key)

        buttons = ttk.Frame(self, padding=(14, 0, 14, 12))
        buttons.pack(fill="x")
        ok = ttk.Button(buttons, text=ok_label, command=self._accept)
        if cancel_label:
            ttk.Button(buttons, text=cancel_label,
                       command=self._dismiss).pack(side="right")
            ok.pack(side="right", padx=(0, 6))
        else:
            ok.pack(side="right")

        self.bind("<Return>", self._accept)
        self.bind("<Escape>", self._dismiss)
        self.protocol("WM_DELETE_WINDOW", self._dismiss)

        self._centre_on(parent)
        ok.focus_set()
        self.grab_set()
        # Runs once the window is on screen and the text widget knows its real
        # width, which is when Tk can be asked how the message actually wrapped.
        self.after(0, self._fit_to_content)
        self.wait_window(self)

    @staticmethod
    def _wrapped_rows(lines, width):
        """
        Estimated rows the message occupies when word-wrapped to `width`.

        Accounts for words that do not fit the remainder of a row, which
        dividing line length by width underestimates.
        """
        rows = 0
        for line in lines:
            wrapped = textwrap.wrap(
                line, width, break_long_words=True, break_on_hyphens=False,
                drop_whitespace=False, replace_whitespace=False)
            rows += max(len(wrapped), 1)
        return rows

    def _apply_height(self, rows):
        """Sizes the text to `rows`, adding or removing the scrollbar to match."""
        height = min(max(rows, self.MIN_HEIGHT), self.MAX_HEIGHT)
        self._text.configure(height=height)

        if rows > height and self._scrollbar is None:
            self._scrollbar = ttk.Scrollbar(self._body, orient="vertical",
                                            command=self._text.yview)
            self._scrollbar.grid(row=0, column=1, sticky="ns")
            self._text.configure(yscrollcommand=self._scrollbar.set)
        elif rows <= height and self._scrollbar is not None:
            self._text.configure(yscrollcommand="")
            self._scrollbar.destroy()
            self._scrollbar = None

    def _fit_to_content(self):
        """
        Re-sizes against Tk's actual layout once the window is mapped.

        The textwrap estimate can differ from Tk by a row on long unbroken
        tokens; a row short without a scrollbar would hide text.
        """
        if not self.winfo_exists():
            return
        self.update_idletasks()
        counted = self._text.count("1.0", "end-1c", "displaylines")
        if counted is None:  # a single display line: Tk counts the gaps, not the rows
            rows = 1
        else:
            gaps = counted[0] if isinstance(counted, (tuple, list)) else counted
            rows = int(gaps) + 1
        if rows != int(self._text.cget("height")) or self._scrollbar is not None:
            self._apply_height(rows)

    # Keys that must still reach the Text widget: copy, select-all, and moving
    # the insertion point. Everything else would edit the message.
    _ALLOWED_CTRL_KEYS = frozenset({"c", "C", "a", "A", "Insert"})
    _NAVIGATION_KEYS = frozenset({"Left", "Right", "Up", "Down",
                                  "Home", "End", "Prior", "Next"})

    @classmethod
    def _swallow_key(cls, event):
        """Blocks edits while leaving the copy and navigation bindings intact."""
        control_held = bool(event.state & 0x4)
        if control_held and event.keysym in cls._ALLOWED_CTRL_KEYS:
            return None
        if not control_held and event.keysym in cls._NAVIGATION_KEYS:
            return None
        return "break"

    def _centre_on(self, parent):
        self.update_idletasks()
        width, height = self.winfo_reqwidth(), self.winfo_reqheight()
        if parent is not None and parent.winfo_viewable():
            x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - height) // 3
        else:
            x = (self.winfo_screenwidth() - width) // 2
            y = (self.winfo_screenheight() - height) // 3
        # Keep the whole dialog on screen even when the parent sits near an edge.
        x = max(0, min(x, self.winfo_screenwidth() - width))
        y = max(0, min(y, self.winfo_screenheight() - height))
        self.geometry(f"+{x}+{y}")

    def _accept(self, _event=None):
        self.result = True
        self.destroy()

    def _dismiss(self, _event=None):
        self.result = False
        self.destroy()


def show_message(parent, title, message):
    """Information dialog: one dismiss button."""
    MessageDialog(parent, title, message, ok_label="Close")


def ask_confirmation(parent, title, message, ok_label="Continue"):
    """Confirmation dialog. Returns True only if the user accepted."""
    return MessageDialog(parent, title, message,
                         ok_label=ok_label, cancel_label="Cancel").result


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


def maximize_window(window):
    """
    Maximizes a window across platforms, kept movable rather than borderless.

    Windows takes state("zoomed"), many Linux WMs only the -zoomed attribute;
    anything else falls back to filling the screen.
    """
    for attempt in (lambda: window.state("zoomed"),
                    lambda: window.attributes("-zoomed", True)):
        try:
            attempt()
            return
        except tk.TclError:
            continue
    window.geometry(f"{window.winfo_screenwidth()}x{window.winfo_screenheight()}+0+0")


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
    for root, label in ((OUTPUT_ROOT, "outputs"), (INPUT_MAPS_DIR, "input_maps")):
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
              "Elevation grid as a NumPy binary; the input for Send to Unreal.")]
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
    for root, label in ((INPUT_MAPS_DIR, "input_maps"), (OUTPUT_ROOT, "outputs")):
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

IMPORT_STEPS = (
    "To apply it to a Landscape in Unreal:\n"
    "    1. Enter Landscape Mode (Shift + 2).\n"
    "    2. Select a Landscape sized to the heightmap. (With 'Dispatch\n"
    "       to running Unreal Editor' enabled the studio spawns it and\n"
    "       applies the heightmap itself.)\n"
    "    3. Right-click it in the Manage tab list and choose 'Import'.\n"
    "    4. Set the Heightmap File to the path above.\n"
    "    5. Click Import.\n\n"
    "The relief is applied immediately.")


def format_path_block(path, width=84, indent="    "):
    """
    Lays a long path over several lines, broken at its separators.

    Word wrapping breaks a path at the spaces inside folder names, splitting it
    mid-component and dropping the indent. Breaking at the separators keeps each
    line a whole path component.
    """
    parts = path.replace("/", os.sep).split(os.sep)
    # Keep each separator on the component before it, so no line starts with one.
    pieces = [p + os.sep for p in parts[:-1]] + parts[-1:]

    lines, current = [], indent
    hanging = indent + "  "
    for piece in pieces:
        candidate = current + piece
        if len(candidate) > width and current.strip():
            lines.append(current)
            current = hanging + piece
        else:
            current = candidate
    lines.append(current)
    return "\n".join(lines)


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


def convert_and_dispatch(source_path, resolution, dispatch, output_dir,
                         log, progress, dialog, cancelled, save_level=True):
    """
    Converts a DEM to Unreal's 16-bit heightmap and optionally spawns the
    Landscape actor. Runs on a worker thread.

    Reporting is injected so the studio footer and the export view can share
    one pipeline:

        log(text, level=None)    one line of progress text
        progress(message, frac)  status text and a 0..1 bar position
        dialog(title, message)   a modal dialog, raised on the main thread
        cancelled()              True once the user has asked to stop

    Returns the path of the written heightmap.
    """
    os.makedirs(output_dir, exist_ok=True)
    progress("Converting DEM to Unreal 16-bit heightmap", 0.2)

    # The loader prints its pipeline status; funnel it into the caller's log.
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        config, interp_resolution, z_scale, heightmap, z_offset = \
            lunar_loader.generate_lunar_terrain(source_path, output_dir, resolution)
    for line in buffer.getvalue().splitlines():
        if line.strip():
            log(line)

    heightmap_path = os.path.normpath(os.path.join(output_dir, heightmap))
    log(f"Heightmap written: {heightmap_path}", level="ok")

    if not dispatch:
        # No dispatch: show the manual import instructions and the file path.
        dialog("Heightmap ready for Unreal",
               "The 16-bit heightmap has been written to:\n\n"
               + format_path_block(heightmap_path) + "\n\n" + IMPORT_STEPS)
        progress("Conversion complete (dispatch disabled)", 1.0)
        return heightmap_path

    # Cancellation checkpoint between the (uninterruptible) conversion and the
    # network dispatch.
    if cancelled():
        raise terrain_core.TerrainCancelled()

    progress("Searching for a running Unreal Editor", 0.7)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        lunar_loader.send_to_unreal(config, interp_resolution, z_scale, heightmap,
                                    z_offset=z_offset, output_directory=output_dir,
                                    save_level=save_level)
    output = buffer.getvalue()
    for line in output.splitlines():
        if line.strip():
            log(line, level="error" if "[-]" in line else None)
    if "[-]" in output:
        log("No editor responded. Enable the Python Editor Script Plugin and "
            "'Remote Execution' in Project Settings, then retry.", level="warn")
    progress("Dispatch complete", 1.0)
    return heightmap_path


class ParamRow:
    """
    A single parameter control: label, slider, and a numeric entry kept in sync.

    Logarithmic parameters store log10(value) on the slider while the entry
    always shows the real value, so densities spanning several decades stay
    controllable with a single drag.
    """

    def __init__(self, parent, spec, on_change):
        self.key, label, self.minimum, self.maximum, self.kind, tooltip = spec
        self.on_change = on_change
        self._syncing = False
        # Authoritative value. The slider position is a lossy view of it (a log
        # slider cannot round-trip 8e-4 exactly), so typed values are kept here
        # verbatim and only the widget reads from the transformed position.
        self.value = float(self.minimum)

        frame = ttk.Frame(parent)
        frame.pack(fill="x", pady=(2, 4))
        frame.columnconfigure(0, weight=1)

        name_label = ttk.Label(frame, text=label, font=("Segoe UI", 8))
        name_label.grid(row=0, column=0, sticky="w")
        Tooltip(name_label, tooltip)

        self.entry_var = tk.StringVar()
        self.entry = ttk.Entry(frame, textvariable=self.entry_var, width=11,
                               justify="right", font=("Consolas", 9))
        self.entry.grid(row=0, column=1, sticky="e")
        self.entry.bind("<Return>", self._commit_entry)
        self.entry.bind("<FocusOut>", self._commit_entry)

        self.slider_var = tk.DoubleVar()
        self.slider = ttk.Scale(
            frame, from_=self._to_slider(self.minimum), to=self._to_slider(self.maximum),
            variable=self.slider_var, command=self._slider_moved, orient="horizontal",
        )
        self.slider.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(1, 0))
        Tooltip(self.slider, tooltip)

    # --- value transforms -------------------------------------------------
    def _to_slider(self, value):
        if self.kind == 'log':
            return float(np.log10(max(value, 1e-12)))
        return float(value)

    def _from_slider(self, value):
        if self.kind == 'log':
            return float(10.0 ** value)
        if self.kind == 'int':
            return int(round(value))
        return float(value)

    def _format(self, value):
        if self.kind == 'int':
            return str(int(round(value)))
        if self.kind == 'log' and value < 0.01:
            return f"{value:.2e}"
        return f"{value:.4g}"

    # --- events -----------------------------------------------------------
    def _slider_moved(self, _raw):
        if self._syncing:
            return
        self._syncing = True
        self.value = self._from_slider(self.slider_var.get())
        self.entry_var.set(self._format(self.value))
        self._syncing = False
        self.on_change()

    def _commit_entry(self, _event=None):
        if self._syncing:
            return
        try:
            value = float(self.entry_var.get())
        except ValueError:
            self.set(self.get())  # Revert to the last valid value
            return
        self.set(value)
        self.on_change()

    # --- accessors --------------------------------------------------------
    def get(self):
        return int(round(self.value)) if self.kind == 'int' else self.value

    def set(self, value):
        # Values outside the slider range are clamped for the widget, but the
        # stored value and the entry keep showing what the config actually holds.
        self.value = float(value)
        clamped = min(max(self.value, self.minimum), self.maximum)
        self._syncing = True
        self.slider_var.set(self._to_slider(clamped))
        self.entry_var.set(self._format(self.value))
        self._syncing = False


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


class MapView(ttk.Frame):
    """
    Embeddable DEM view: shaded relief with an elevation colourbar, and
    optionally a 3D surface tab.

    Maps load on a worker thread and are drawn on the main thread; a token
    discards stale loads when a newer map is requested. The 3D surface is drawn
    only while its tab is showing. The host supplies `on_status` (log line) and
    `on_title` (map name).
    """

    DISPLAY_MAX_EDGE = 1200

    def __init__(self, parent, figure_dpi=100, on_status=None, on_title=None,
                 show_3d=False):
        super().__init__(parent)
        self._on_status = on_status or (lambda text, level=None: None)
        self._on_title = on_title or (lambda name: None)

        self._load_queue = queue.Queue()
        self._load_token = 0
        self._awaiting = None
        self._poll_job = None

        self._name = None
        self._payload = None
        self._surface_stale = True
        self._surface_artists = None

        if show_3d:
            self.notebook = ttk.Notebook(self)
            self.notebook.pack(fill="both", expand=True)
            relief_tab = ttk.Frame(self.notebook)
            surface_tab = ttk.Frame(self.notebook)
            self.notebook.add(relief_tab, text="Shaded Relief")
            self.notebook.add(surface_tab, text="3D Surface")
            self.figure, self.canvas = self._make_canvas(
                relief_tab, figure_dpi, "constrained")
            self.surface_figure, self.surface_canvas = self._make_canvas(
                surface_tab, figure_dpi, None)
            # Coarse mesh while rotating, full mesh on release.
            self.surface_canvas.mpl_connect(
                "button_press_event",
                lambda e: e.inaxes is not None and self._set_surface_detail(True))
            self.surface_canvas.mpl_connect(
                "button_release_event", lambda e: self._set_surface_detail(False))
            self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        else:
            self.notebook = None
            self.figure, self.canvas = self._make_canvas(self, figure_dpi, "constrained")
            self.surface_figure = self.surface_canvas = None

        self.show_message("No map selected")

    @staticmethod
    def _make_canvas(parent, figure_dpi, layout):
        figure = Figure(figsize=(7.0, 6.0), dpi=figure_dpi, layout=layout)
        canvas = FigureCanvasTkAgg(figure, master=parent)
        canvas.get_tk_widget().pack(fill="both", expand=True)
        NavigationToolbar2Tk(canvas, parent).update()
        return figure, canvas

    @property
    def busy(self):
        return self._awaiting is not None

    def _surface_showing(self):
        return (self.notebook is not None
                and self.notebook.index(self.notebook.select()) == 1)

    def show_path(self, path):
        """Shows a loading indicator, then loads and renders `path` off-thread."""
        name = os.path.basename(path)
        self._load_token += 1
        token = self._load_token
        self._awaiting = token

        self._on_title(f"loading {name}...")
        self.show_message(f"Loading  {name} ...")
        self._on_status(f"Loading {name} ...")

        def worker():
            try:
                data = load_dem_array(path)
                self._load_queue.put((token, "ok", name, self._prepare(data)))
            except Exception as exc:
                self._load_queue.put((token, "error", name, str(exc)))

        threading.Thread(target=worker, daemon=True).start()
        if self._poll_job is None:
            self._poll_job = self.after(80, self._poll_load)

    def _prepare(self, data):
        """Downsamples and shades the DEM (runs in the worker thread; numpy only)."""
        step = max(1, int(np.ceil(max(data.shape) / self.DISPLAY_MAX_EDGE)))
        disp = data[::step, ::step]

        finite = np.isfinite(disp)
        vmin = float(np.min(disp[finite])) if finite.any() else 0.0
        vmax = float(np.max(disp[finite])) if finite.any() else 1.0
        relief = max(vmax - vmin, 1e-6)
        filled = np.where(finite, disp, (vmin + vmax) / 2.0)

        # Exaggerate so relief is visible regardless of the map's units/scale.
        light = LightSource(azdeg=315, altdeg=45)
        vert_exag = (0.12 * max(disp.shape)) / relief
        shaded = light.shade(filled, cmap=matplotlib.colormaps["gray"],
                             blend_mode="soft", vert_exag=vert_exag)
        return {'orig_shape': data.shape, 'step': step, 'shaded': shaded,
                'elevation': filled, 'vmin': vmin, 'vmax': vmax, 'relief': relief}

    def _poll_load(self):
        """Main-thread pump: renders a finished load, ignoring superseded ones."""
        self._poll_job = None
        try:
            while True:
                token, kind, name, payload = self._load_queue.get_nowait()
                if token != self._load_token:
                    continue  # a newer map was requested; drop this result
                self._awaiting = None
                if kind == "ok":
                    self._render(name, payload)
                    rows, cols = payload['orig_shape']
                    self._on_status(f"Loaded {name}  ({cols} x {rows} px, "
                                    f"relief {payload['relief']:.3g})", level="ok")
                else:
                    self.show_message(f"Could not load\n{name}")
                    self._on_status(f"Failed to load {name}: {payload}", level="error")
                self._on_title(name)
        except queue.Empty:
            pass
        if self._awaiting is not None and self.winfo_exists():
            self._poll_job = self.after(80, self._poll_load)

    def show_message(self, text):
        """Draws a centred status message in place of the plots."""
        self._payload = None
        self._surface_artists = None
        figures = [(self.figure, self.canvas)]
        if self.surface_figure is not None:
            figures.append((self.surface_figure, self.surface_canvas))
        for figure, canvas in figures:
            figure.clear()
            axes = figure.add_subplot(111)
            axes.axis("off")
            axes.text(0.5, 0.5, text, ha="center", va="center", fontsize=12,
                      transform=axes.transAxes)
            canvas.draw_idle()

    def _render(self, name, payload):
        """Draws the shaded relief prepared by the worker, and the 3D view if shown."""
        self._name = name
        self._payload = payload
        self._surface_stale = True

        self.figure.clear()
        axes = self.figure.add_subplot(111)
        axes.imshow(payload['shaded'], origin="upper", interpolation="bilinear")

        step = payload['step']
        note = f" (shown at 1/{step})" if step > 1 else ""
        rows, cols = payload['orig_shape']
        axes.set_title(
            f"{name}{note}\n{cols} x {rows} px   min {payload['vmin']:.3g}   "
            f"max {payload['vmax']:.3g}   relief {payload['relief']:.3g}", fontsize=9)
        axes.set_xlabel("pixels")
        axes.set_ylabel("pixels")

        mappable = ScalarMappable(
            norm=Normalize(vmin=payload['vmin'], vmax=payload['vmax']), cmap="gray")
        self.figure.colorbar(mappable, ax=axes, shrink=0.8, label="Elevation")
        self.canvas.draw_idle()

        if self._surface_showing():
            self._draw_surface()

    def _on_tab_changed(self, _event=None):
        if self._surface_showing() and self._surface_stale and self._payload:
            self._draw_surface()

    def _draw_surface(self):
        """3D mesh of the loaded map, with an auto-fitted vertical exaggeration."""
        payload = self._payload
        elevation = payload['elevation']
        rows_full, cols_full = payload['orig_shape']
        rows, cols = elevation.shape

        figure = self.surface_figure
        figure.clear()
        self._surface_artists = None
        axes = figure.add_subplot(111, projection="3d")

        span_x, span_y = float(cols_full), float(rows_full)
        span = max(span_x, span_y)
        relief = max(float(np.ptp(elevation)), 1e-6)
        z_box = 0.35
        exaggeration = (z_box * span) / relief
        light = LightSource(azdeg=315, altdeg=45)

        def plot_mesh(detail):
            step = max(1, int(np.ceil(max(rows, cols) / detail)))
            mesh = elevation[::step, ::step]
            mesh_x = np.linspace(0, span_x, mesh.shape[1])
            mesh_y = np.linspace(0, span_y, mesh.shape[0])
            grid_x, grid_y = np.meshgrid(mesh_x, mesh_y)
            cell = span / max(mesh.shape)
            colors = light.shade(mesh, cmap=matplotlib.colormaps["gray"],
                                 blend_mode="soft", vert_exag=exaggeration,
                                 dx=cell, dy=cell)
            # rcount/ccount match the mesh so the face colours stay aligned.
            return axes.plot_surface(grid_x, grid_y, mesh, facecolors=colors,
                                     shade=False, linewidth=0, antialiased=False,
                                     rcount=mesh.shape[0], ccount=mesh.shape[1]), step

        fine, fine_step = plot_mesh(SURFACE_MESH_DETAIL)
        coarse, coarse_step = plot_mesh(SURFACE_DRAG_DETAIL)
        if coarse_step > fine_step:
            coarse.set_visible(False)
            self._surface_artists = (fine, coarse)
        else:
            coarse.remove()

        # No exaggeration factor in the title: the map's resolution is unknown
        # here, so X/Y are in pixels and a ratio against Z would mix units.
        axes.set_title(f"{self._name} - 3D surface\n"
                       f"vertical scale auto-fitted", fontsize=9)
        axes.set_xlabel("pixels")
        axes.set_ylabel("pixels")
        axes.set_zlabel("elevation")
        axes.view_init(elev=32, azim=-125)
        axes.set_zlim(float(np.min(elevation)) - relief * 0.15,
                      float(np.max(elevation)) + relief * 0.15)
        axes.zaxis.set_major_locator(MaxNLocator(5))
        axes.set_box_aspect((span_x / span, span_y / span, z_box))
        figure.subplots_adjust(left=0.0, right=0.97, top=0.93, bottom=0.02)
        self.surface_canvas.draw_idle()
        self._surface_stale = False

    def _set_surface_detail(self, coarse):
        """Swaps the coarse mesh in while rotating and back on release."""
        if not self._surface_artists:
            return
        fine, rough = self._surface_artists
        if rough.get_visible() == coarse:
            return
        fine.set_visible(not coarse)
        rough.set_visible(coarse)
        self.surface_canvas.draw()


class ActivityLog(ttk.Frame):
    """The dark log strip the studio uses, as a reusable widget."""

    def __init__(self, parent, height=7, title="Activity log"):
        super().__init__(parent, padding=(8, 4, 8, 8))
        ttk.Label(self, text=title, font=("Segoe UI Semibold", 10)).pack(anchor="w")
        self.text = tk.Text(self, height=height, font=("Consolas", 8), wrap="word",
                            relief="solid", borderwidth=1, background="#1e1e1e",
                            foreground="#d4d4d4", insertbackground="#d4d4d4")
        self.text.pack(fill="both", expand=True, pady=(4, 0))
        self.text.tag_configure("warn", foreground="#e6c07b")
        self.text.tag_configure("error", foreground="#e06c75")
        self.text.tag_configure("ok", foreground="#98c379")
        self.text.configure(state="disabled")

    def log(self, message, level=None):
        self.text.configure(state="normal")
        self.text.insert("end", message.rstrip() + "\n", level or ())
        self.text.see("end")
        self.text.configure(state="disabled")


class ViewHeader(ttk.Frame):
    """
    Header shared by the full-window views: Home button, title, subtitle.
    """

    def __init__(self, parent, title, on_home, subtitle=""):
        super().__init__(parent)
        ttk.Button(self, text="⌂ Home", width=9,
                   command=on_home).pack(side="left")
        ttk.Label(self, text=title,
                  font=("Segoe UI Semibold", 11)).pack(side="left", padx=(8, 0))
        self.subtitle_var = tk.StringVar(value=subtitle)
        ttk.Label(self, textvariable=self.subtitle_var,
                  style="HomeHint.TLabel").pack(side="left", padx=(10, 0))

    def set_subtitle(self, text):
        self.subtitle_var.set(text)


class MapViewer(tk.Toplevel):
    """
    Standalone window around a MapView, used for one-off previews from the
    studio's export panel. Folder browsing is handled by MapBrowser.
    """

    def __init__(self, master, figure_dpi=100):
        super().__init__(master)
        self.title("Map Viewer")

        # A large, centred window rather than full-screen. Maximised to a wide
        # monitor, a square DEM would sit in a broad empty margin (once the title
        # bar, taskbar and log are subtracted, the plot area is far wider than it
        # is tall). A near-4:3 window keeps the preview big and crisp while the
        # map stays close to the frame; the title-bar button still maximises it.
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        width = min(1200, int(screen_w * 0.64))
        height = min(1040, int(screen_h * 0.90))
        self.geometry(f"{width}x{height}+{max(0, (screen_w - width) // 2)}+"
                      f"{max(0, (screen_h - height) // 3)}")
        self.minsize(560, 520)

        self._log = ActivityLog(self)
        self.view = MapView(self, figure_dpi=figure_dpi,
                            on_status=self.log,
                            on_title=lambda name: self.title(f"Map Viewer - {name}"))

        # Pack the fixed-height log first so the expanding plot cannot squeeze it.
        self._log.pack(side="bottom", fill="x")
        self.view.pack(side="top", fill="both", expand=True)

    def log(self, text, level=None):
        """Appends a line to the viewer's activity log."""
        self._log.log(text, level)

    def show_path(self, path):
        self.view.show_path(path)


class MapBrowser(ttk.Frame):
    """
    Map list on the left, rendered map on the right, updated on selection.

    The selected map becomes the app's current map for the other stages.
    """

    # Default list width in pixels; fits a nested "<folder>/<file>" name.
    LIST_WIDTH = 420

    def __init__(self, master, figure_dpi=100, initial_path=None):
        super().__init__(master, padding=(8, 8, 8, 8))

        self.app = master
        self._paths = {}          # tree item id -> file path
        self._extra_folders = []  # folders added through "Add folder..."
        self._selected = None
        self._show_job = None     # pending debounced load

        self.header = ViewHeader(self, "Browse Maps", self.app.show_home,
                                 subtitle="select a map to view it")
        self.header.pack(fill="x", pady=(0, 8))

        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True)

        # --- left: the map list ---
        left = ttk.Frame(panes, padding=(8, 8, 4, 8))
        right = ttk.Frame(panes, padding=(4, 8, 8, 8))
        panes.add(left, weight=0)
        panes.add(right, weight=1)
        self._panes = panes

        ttk.Label(left, text="Maps", style="Header.TLabel").pack(anchor="w")

        tree_frame = ttk.Frame(left)
        tree_frame.pack(fill="both", expand=True, pady=(4, 6))
        self.tree = ttk.Treeview(tree_frame, show="tree", selectmode="browse")
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        self.include_all_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(left, text="Include masks and other rasters",
                        variable=self.include_all_var,
                        command=self.refresh).pack(anchor="w")

        buttons = ttk.Frame(left)
        buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(buttons, text="Refresh", command=self.refresh).pack(side="left")
        ttk.Button(buttons, text="Add folder...",
                   command=self.add_folder).pack(side="left", padx=(6, 0))

        self.count_var = tk.StringVar()
        ttk.Label(left, textvariable=self.count_var,
                  style="Readout.TLabel").pack(anchor="w", pady=(6, 0))

        # --- right: the map itself ---
        self._log = ActivityLog(right, height=6)
        self.view = MapView(right, figure_dpi=figure_dpi, on_status=self.log,
                            on_title=self.header.set_subtitle, show_3d=True)
        self._log.pack(side="bottom", fill="x")
        self.view.pack(side="top", fill="both", expand=True)

        self.refresh()
        if initial_path:
            self.select_path(initial_path)

        # Sash position needs a realized width; placed once the view is packed.
        self.after_idle(self._place_sash)

    def _place_sash(self, attempt=0):
        """Gives the list its default width, retrying while the panes settle."""
        if not self.winfo_exists():
            return
        self.update_idletasks()
        span = self._panes.winfo_width()
        if span > self.LIST_WIDTH * 2:
            self._panes.sashpos(0, self.LIST_WIDTH)
            self.update_idletasks()
            if abs(self._panes.sashpos(0) - self.LIST_WIDTH) <= 2:
                return
        if attempt < 6:
            self.after(60, lambda: self._place_sash(attempt + 1))

    def log(self, message, level=None):
        self._log.log(message, level)

    # ----------------------------------------------------------- the list
    def _folders(self):
        """(label, path) for every folder shown, project ones first."""
        folders = [("input_maps", INPUT_MAPS_DIR), ("outputs", OUTPUT_ROOT)]
        folders += [(os.path.basename(p.rstrip(os.sep)) or p, p)
                    for p in self._extra_folders]
        return folders

    def refresh(self):
        """Rebuilds the list, keeping the current map selected where possible."""
        keep = self._selected
        self.tree.delete(*self.tree.get_children())
        self._paths.clear()

        include_all = self.include_all_var.get()
        total = 0
        for label, folder in self._folders():
            exists = os.path.isdir(folder)
            entries = scan_dem_folder(folder, include_companions=include_all)

            # Project folders are always listed, even before they exist.
            count = f"({len(entries)})" if exists else "(not created yet)"
            parent = self.tree.insert("", "end", text=f"{label}/  {count}", open=True)
            if not entries:
                self.tree.insert(parent, "end", tags=("empty",),
                                 text="(no maps yet)" if exists else "(no folder yet)")
            for name, path in entries:
                item = self.tree.insert(parent, "end", text=name)
                self._paths[item] = path
            total += len(entries)

        self.count_var.set(f"{total} map{'' if total == 1 else 's'}"
                           + ("  (masks included)" if include_all else ""))
        if keep:
            self.select_path(keep, render=False)

    def add_folder(self):
        """Adds any folder on disk to the list."""
        folder = filedialog.askdirectory(parent=self, title="Add a folder of maps",
                                         initialdir=map_browse_dir())
        if not folder:
            return
        folder = os.path.normpath(folder)
        if folder in self._extra_folders:
            self.log(f"{folder} is already listed.", level="warn")
            return
        self._extra_folders.append(folder)
        self.refresh()
        self.log(f"Added {folder}")

    def select_path(self, path, render=True):
        """Selects `path` in the tree if it is listed; returns whether it was."""
        target = os.path.normcase(os.path.normpath(path))
        for item, item_path in self._paths.items():
            if os.path.normcase(os.path.normpath(item_path)) == target:
                self.tree.selection_set(item)
                self.tree.see(item)
                if render:
                    self.show(item_path)
                return True
        return False

    def _on_select(self, _event=None):
        selection = self.tree.selection()
        if not selection:
            return
        path = self._paths.get(selection[0])
        if path:
            self.show(path)

    # Debounce, so moving quickly through the list loads only the final map.
    SELECTION_DELAY_MS = 250

    def show(self, path):
        if path == self._selected:
            return
        self._selected = path
        # The viewed map becomes the current map for the other stages.
        self.app._set_unreal_source(path)
        if self._show_job is not None:
            self.after_cancel(self._show_job)
        self._show_job = self.after(self.SELECTION_DELAY_MS, self._show_now)

    def _show_now(self):
        self._show_job = None
        if self._selected and self.winfo_exists():
            self.view.show_path(self._selected)


class RockPlannerStudio(ttk.Frame):
    """
    Window that turns an elevation map into a rock field with guaranteed rover
    corridors. The user picks a DEM, tunes the rover and rock parameters, previews
    the plan (traversability, corridors, rocks), and exports the placement files.

    Computation runs in a worker thread and is drawn on the main thread through a
    queue, mirroring the studio; the plan itself lives in the UI-free rock_field
    module.
    """

    def __init__(self, master, figure_dpi=100, source_path=None, resolution=1.0):
        super().__init__(master)
        self.app = master

        self.figure_dpi = figure_dpi
        self.dem = None               # Loaded elevation array
        self.dem_path = None
        self.result = None            # Last rock_field.build_rock_field() output
        self.params = rock_field.default_params()
        self.rows_widgets = {}

        self._queue = queue.Queue()
        self._worker = None
        self._cancel = threading.Event()
        self._preview_job = None

        self._build_ui()
        self._apply_params_to_widgets()
        self.resolution_var.set(f"{resolution:g}")
        self._refresh_rock_estimate()

        self.after(80, self._drain_queue)
        self.log("Rock planner ready. Load a DEM to begin.")

        if source_path and os.path.exists(source_path):
            self.load_dem(source_path)

        self.after_idle(self._place_sash)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        panel_width = self.master.px(348) if hasattr(self.master, "px") else 348
        self._panel_width = panel_width
        # Labels re-wrapped on resize by _rewrap_labels.
        self._wrapping_labels = []

        # Resizable split between parameters and plot.
        self._panes = ttk.PanedWindow(self, orient="horizontal")
        self._panes.pack(fill="both", expand=True)
        left = ttk.Frame(self._panes, padding=(8, 8, 4, 8))

        # Top bar: Home + title, always visible above the scroll area.
        top_bar = ttk.Frame(left)
        top_bar.pack(fill="x")
        ttk.Button(top_bar, text="⌂ Home", width=9, command=self.go_home).pack(side="left")
        ttk.Label(top_bar, text="Rock Field & Rover Paths",
                  font=("Segoe UI Semibold", 11)).pack(side="left", padx=(8, 0))

        # Action bar pinned to the bottom, packed BEFORE the scroll area so it
        # always keeps its space and the buttons stay reachable at any height.
        actions = ttk.Frame(left)
        actions.pack(side="bottom", fill="x", pady=(6, 0))
        self.preview_button = ttk.Button(actions, text="Update Preview",
                                         command=self.request_preview)
        self.preview_button.pack(fill="x")
        self.stop_button = ttk.Button(actions, text="Stop", command=self.stop_job)
        self.stop_button.pack(fill="x", pady=(4, 0))
        self.stop_button.state(["disabled"])
        Tooltip(self.stop_button,
                "Stops the running plan at its next checkpoint (slope, corridors, or\n"
                "rock placement). The DEM stays loaded so you can retune and retry.")
        self.export_button = ttk.Button(actions, text="Generate & Export",
                                        style="Accent.TButton", command=self.export_plan)
        self.export_button.pack(fill="x", pady=(4, 0))
        ttk.Button(actions, text="Open output folder",
                   command=self.open_output_folder).pack(fill="x", pady=(4, 0))

        # Scrollable parameter area between the top bar and the action bar.
        scroll_host = ttk.Frame(left)
        scroll_host.pack(side="top", fill="both", expand=True, pady=(8, 0))
        # A ttk.Frame has no -background of its own, so take the window's.
        canvas = tk.Canvas(scroll_host, highlightthickness=0, borderwidth=0,
                           background=self.app.cget("background"))
        scrollbar = ttk.Scrollbar(scroll_host, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        window = canvas.create_window((0, 0), window=inner, anchor="nw")

        def on_canvas_resize(event):
            canvas.itemconfigure(window, width=event.width)
            # During <Configure>, winfo_width() still reports the old width.
            self._rewrap_labels(event.width)

        canvas.bind("<Configure>", on_canvas_resize)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self._wrapping_labels.append(ttk.Label(
            inner, justify="left", font=("Segoe UI", 8), foreground="#555555",
            text="Scatters rocks as obstacles across the drivable terrain while "
                 "keeping a connected corridor network the rover can always "
                 "traverse."))
        self._wrapping_labels[-1].pack(anchor="w", pady=(0, 8), fill="x")

        # Source DEM
        source_group = ttk.Labelframe(inner, text="Source DEM", padding=(8, 4, 8, 8),
                                      style="Group.TLabelframe")
        source_group.pack(fill="x", pady=(0, 8))
        self.source_var = tk.StringVar(value="(none - load a DEM)")
        ttk.Entry(source_group, textvariable=self.source_var, state="readonly",
                  font=("Consolas", 8)).pack(fill="x")
        res_row = ttk.Frame(source_group)
        res_row.pack(fill="x", pady=(6, 0))
        self.load_button = ttk.Button(res_row, text="Load DEM  ▾",
                                      command=self.open_dem_menu)
        self.load_button.pack(side="left", fill="x", expand=True)
        Tooltip(self.load_button,
                "Lists the DEMs already in input_maps/ and outputs/, or opens\n"
                "the file picker for a map kept anywhere else.")
        ttk.Label(res_row, text="  Resolution (m/px)", font=("Segoe UI", 8)).pack(side="left")
        self.resolution_var = tk.StringVar(value="1")
        res_entry = ttk.Entry(res_row, textvariable=self.resolution_var, width=7,
                              justify="right", font=("Consolas", 9))
        res_entry.pack(side="left", padx=(4, 0))
        res_entry.bind("<Return>", lambda e: self.request_preview())
        res_entry.bind("<FocusOut>", lambda e: self.request_preview())
        Tooltip(res_entry,
                "Ground metres per DEM pixel. Sets the real-world scale, so the path\n"
                "width, crater depth and rock scale are all interpreted in metres.\n"
                "Defaults to the studio's Map Geometry resolution.")

        # Warns when the loaded file looks like a mask rather than a DEM.
        self.source_note_var = tk.StringVar()
        self._wrapping_labels.append(ttk.Label(
            source_group, textvariable=self.source_note_var, justify="left",
            style="Readout.TLabel", foreground="#b8860b"))
        self._wrapping_labels[-1].pack(anchor="w", pady=(4, 0))

        # Parameters
        for group_name, specs in ROCK_PARAM_GROUPS:
            group = ttk.Labelframe(inner, text=group_name, padding=(8, 4, 8, 8),
                                   style="Group.TLabelframe")
            group.pack(fill="x", pady=(0, 8))
            for spec in specs:
                self.rows_widgets[spec[0]] = ParamRow(group, spec, self._on_param_change)
            if group_name == "Rock Population":
                # Rock count implied by the coverage, shown before planning.
                self.rock_estimate_var = tk.StringVar()
                self._wrapping_labels.append(ttk.Label(
                    group, textvariable=self.rock_estimate_var, justify="left",
                    style="Readout.TLabel"))
                self._wrapping_labels[-1].pack(anchor="w", pady=(4, 0))

        seed_group = ttk.Labelframe(inner, text="Determinism", padding=(8, 4, 8, 8),
                                    style="Group.TLabelframe")
        seed_group.pack(fill="x", pady=(0, 8))
        seed_row = ttk.Frame(seed_group)
        seed_row.pack(fill="x")
        ttk.Label(seed_row, text="Random seed", font=("Segoe UI", 8)).pack(side="left")
        self.seed_var = tk.StringVar(value="1")
        seed_entry = ttk.Entry(seed_row, textvariable=self.seed_var, width=11,
                               justify="right", font=("Consolas", 9))
        seed_entry.pack(side="right")
        seed_entry.bind("<Return>", lambda e: self.request_preview())
        seed_entry.bind("<FocusOut>", lambda e: self.request_preview())

        self.auto_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(inner, text="Auto-update preview on change",
                        variable=self.auto_var).pack(anchor="w", pady=(0, 8))

        # Export options (the buttons themselves live in the pinned action bar).
        export_group = ttk.Labelframe(inner, text="Export", padding=(8, 4, 8, 8),
                                      style="Group.TLabelframe")
        export_group.pack(fill="x", pady=(0, 4))
        ttk.Label(export_group, text="Base filename", font=("Segoe UI", 8)).pack(anchor="w")
        self.export_name_var = tk.StringVar(value="procedural_lunar_terrain")
        ttk.Entry(export_group, textvariable=self.export_name_var,
                  font=("Consolas", 9)).pack(fill="x", pady=(2, 4))

        self.obstacle_map_var = tk.BooleanVar(value=False)
        obstacle_check = ttk.Checkbutton(
            export_group, text="Also write full-res obstacle map (large)",
            variable=self.obstacle_map_var, command=self._refresh_output_preview)
        obstacle_check.pack(anchor="w")
        Tooltip(obstacle_check,
                "Writes a full-resolution 0/1 rock-footprint raster alongside the\n"
                "instance list. Useful as a mask, but large for big DEMs.")

        # Lists the files that will be written, derived from the base filename.
        self.output_preview_var = tk.StringVar()
        preview_label = ttk.Label(export_group, textvariable=self.output_preview_var,
                                  justify="left", style="Readout.TLabel")
        preview_label.pack(anchor="w", pady=(6, 0))
        Tooltip(preview_label,
                "The files Generate & Export writes, derived from the base filename.\n"
                "Loading a DEM sets the name to that map's, so its rocks land in the\n"
                "same output folder as the map itself.\n\n"
                "_rocks.csv        one row per rock: id, row, col, x_m, y_m, scale, height_m\n"
                "_roverPaths.csv   corridor polylines: path_id, x_m, y_m, in order\n"
                "_obstacleMap.csv  0/1 rock-footprint raster at full DEM resolution")
        self.export_name_var.trace_add("write", self._refresh_output_preview)
        self._refresh_output_preview()

        # Place the exported rock field in a running Unreal Editor.
        unreal_group = ttk.Labelframe(inner, text="Place in Unreal", padding=(8, 4, 8, 8),
                                      style="Group.TLabelframe")
        unreal_group.pack(fill="x", pady=(4, 4))
        ttk.Label(unreal_group, text="Rock models folder",
                  font=("Segoe UI", 8)).pack(anchor="w")
        models_row = ttk.Frame(unreal_group)
        models_row.pack(fill="x", pady=(2, 2))
        self.models_folder_var = tk.StringVar(value="(none selected)")
        self.models_folder = None
        ttk.Entry(models_row, textvariable=self.models_folder_var, state="readonly",
                  font=("Consolas", 8)).pack(side="left", fill="x", expand=True)
        ttk.Button(models_row, text="Browse...", width=9,
                   command=self.browse_models_folder).pack(side="left", padx=(4, 0))
        self._wrapping_labels.append(ttk.Label(
            unreal_group, justify="left", style="HomeHint.TLabel",
            text=".gltf, .glb, .obj or .fbx, in subfolders too. glTF keeps its own "
                 "materials; .obj/.fbx take the texture with the same name. "
                 "Import the heightmap into the Landscape first: rocks are "
                 "dropped onto its surface."))
        self._wrapping_labels[-1].pack(anchor="w", pady=(0, 4))

        # Model sizes versus planned sizes, to flag scale mismatches before placing.
        self.model_sizes = None       # rock_models.size_summary() of the folder
        self.model_radii = []         # Footprint radius (m) of each sized model
        self.model_info_var = tk.StringVar()
        self._wrapping_labels.append(ttk.Label(
            unreal_group, textvariable=self.model_info_var, justify="left",
            style="Readout.TLabel"))
        self._wrapping_labels[-1].pack(anchor="w", pady=(0, 4))
        self.match_button = ttk.Button(unreal_group, text="Match Rock Sizes to Models",
                                       command=self.match_sizes_to_models)
        self.match_button.pack(fill="x", pady=(0, 6))
        self.match_button.state(["disabled"])
        Tooltip(self.match_button,
                "Sets Min/Max radius to the smallest and largest model footprint\n"
                "and Height / radius to the models' median, so rocks are placed\n"
                "at their real size. Re-export before placing.")

        self.physics_var = tk.BooleanVar(value=False)
        physics_check = ttk.Checkbutton(unreal_group, text="Simulate physics",
                                        variable=self.physics_var)
        physics_check.pack(anchor="w")

        self.rock_save_level_var = tk.BooleanVar(value=True)
        rock_save_check = ttk.Checkbutton(unreal_group, text="Save the level afterwards",
                                          variable=self.rock_save_level_var)
        rock_save_check.pack(anchor="w")
        Tooltip(rock_save_check,
                "Saves the open Unreal level, and the imported rock assets, once the\n"
                "rocks are placed.")
        Tooltip(physics_check,
                "Off: rocks are static obstacles at their planned positions.\n"
                "On: rocks are movable rigid bodies and may settle or roll.")

        # Object-mask ids come from the Ground Truth Masks view.
        self.mask_ids_var = tk.StringVar()
        self._wrapping_labels.append(ttk.Label(
            unreal_group, textvariable=self.mask_ids_var, justify="left",
            style="HomeHint.TLabel"))
        self._wrapping_labels[-1].pack(anchor="w", pady=(6, 0))

        self.place_button = ttk.Button(unreal_group, text="Place Rocks in Unreal",
                                       command=self.place_in_unreal)
        self.place_button.pack(fill="x", pady=(6, 0))
        if not LOADER_AVAILABLE:
            self.place_button.state(["disabled"])
        self.refresh_mask_ids()
        self._refresh_model_info()

        # Mouse-wheel scrolling over the panel, bound locally (not app-wide) so it
        # never fights the main studio's own wheel scrolling.
        self._bind_mousewheel(inner, canvas)
        self._bind_mousewheel(canvas, canvas)

        # Right: plot on top, log at bottom.
        right = ttk.Frame(self._panes)
        # Only the plot absorbs a resize; the parameter column keeps its width.
        self._panes.add(left, weight=0)
        self._panes.add(right, weight=1)

        log_frame = ttk.Frame(right, padding=(8, 4, 8, 8))
        ttk.Label(log_frame, text="Activity log",
                  font=("Segoe UI Semibold", 10)).pack(anchor="w")
        self.status_var = tk.StringVar(value="Idle")
        self.progress = ttk.Progressbar(log_frame, mode="determinate", maximum=1.0)
        self.progress.pack(fill="x", pady=(4, 4))
        self.log_text = tk.Text(log_frame, height=7, font=("Consolas", 8), wrap="word",
                                relief="solid", borderwidth=1, background="#1e1e1e",
                                foreground="#d4d4d4", insertbackground="#d4d4d4")
        self.log_text.pack(fill="x")
        self.log_text.tag_configure("warn", foreground="#e6c07b")
        self.log_text.tag_configure("error", foreground="#e06c75")
        self.log_text.tag_configure("ok", foreground="#98c379")
        self.log_text.configure(state="disabled")
        log_frame.pack(side="bottom", fill="x")

        plot_frame = ttk.Frame(right)
        self.figure = Figure(figsize=(7.6, 6.4), dpi=self.figure_dpi, layout="constrained")
        self.canvas = FigureCanvasTkAgg(self.figure, master=plot_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        NavigationToolbar2Tk(self.canvas, plot_frame).update()
        plot_frame.pack(side="top", fill="both", expand=True)
        self._show_message("Load a DEM to plan a rock field.")

    def _place_sash(self, attempt=0):
        """Gives the parameter column its width once the panes have a real size."""
        if not self.winfo_exists():
            return
        self.update_idletasks()
        if self._panes.winfo_width() > self._panel_width * 2:
            self._panes.sashpos(0, self._panel_width)
            self.update_idletasks()
            if abs(self._panes.sashpos(0) - self._panel_width) <= 2:
                return
        if attempt < 6:
            self.after(60, lambda: self._place_sash(attempt + 1))

    def _bind_mousewheel(self, widget, canvas):
        """Binds vertical wheel scrolling on `widget` and all its descendants."""
        widget.bind("<MouseWheel>",
                    lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))
        for child in widget.winfo_children():
            self._bind_mousewheel(child, canvas)

    def _rewrap_labels(self, width):
        """Retunes the column's wrapped text to the width the drag handle gave it."""
        wrap = max(160, width - 24)
        for label in self._wrapping_labels:
            if label.winfo_exists():
                label.configure(wraplength=wrap)

    def go_home(self):
        """Back to the home screen, stopping any plan still running."""
        self._cancel.set()
        self.app.show_home()

    def stop_job(self):
        """Requests cooperative cancellation of the running plan/export."""
        if not self._busy():
            return
        self._cancel.set()
        self.stop_button.state(["disabled"])
        self.status_var.set("Cancelling...")
        self.log("Cancellation requested; stopping at the next checkpoint...",
                 level="warn")

    # ------------------------------------------------------------ params
    def _apply_params_to_widgets(self):
        for key, row in self.rows_widgets.items():
            row.set(self.params[key])

    def _collect_params(self):
        for key, row in self.rows_widgets.items():
            self.params[key] = row.get()
        self.params = rock_field.coerce_params(self.params)
        return self.params

    def _on_param_change(self):
        self._refresh_rock_estimate()
        self._refresh_model_info()
        if self.auto_var.get():
            self.request_preview(debounce_ms=500)

    def _seed(self):
        text = self.seed_var.get().strip()
        try:
            return int(text) if text else None
        except ValueError:
            return None

    def _resolution(self):
        try:
            return max(1e-6, float(self.resolution_var.get()))
        except ValueError:
            return 1.0

    # --------------------------------------------------------------- I/O
    def _refresh_output_preview(self, *_trace_args):
        """Lists the filenames an export will write, under the base filename field."""
        base_name = self.export_name_var.get().strip() or "procedural_lunar_terrain"
        files = planned_rock_files(base_name, self.obstacle_map_var.get())
        self.output_preview_var.set(
            f"outputs/{base_name}/\n"
            + "\n".join(f"  {name}" for name, _ in files))

    def open_dem_menu(self):
        """Drops the list of project DEMs under the Load button."""
        drop_dem_menu(self, self.load_button, self.load_dem, self.browse_dem)

    def browse_dem(self, initialdir=None):
        path = filedialog.askopenfilename(
            parent=self, title="Select a DEM for rock planning",
            initialdir=initialdir or map_browse_dir(), filetypes=DEM_FILETYPES)
        if path:
            self.load_dem(path)

    def load_dem(self, path):
        """Loads the DEM off-thread, then triggers a preview."""
        self.source_var.set(os.path.basename(path))
        self.dem_path = path
        self.source_note_var.set(dem_source_note(path))

        # Name the export after the map so the rocks land in its output folder.
        self.export_name_var.set(dem_group_name(path))

        self.log(f"Loading DEM: {os.path.basename(path)} ...")
        self._show_message(f"Loading {os.path.basename(path)} ...")

        def worker():
            try:
                data = load_dem_array(path)
                self._queue.put(("dem", data))
            except Exception as exc:
                self._queue.put(("error", f"Could not load DEM: {exc}"))
        threading.Thread(target=worker, daemon=True).start()

    # ---------------------------------------------------------- preview
    def request_preview(self, debounce_ms=0):
        self._refresh_rock_estimate()
        if self.dem is None:
            return
        if self._preview_job:
            self.after_cancel(self._preview_job)
            self._preview_job = None
        if debounce_ms:
            self._preview_job = self.after(debounce_ms, self._launch)
        else:
            self._launch()

    def _busy(self):
        return self._worker is not None and self._worker.is_alive()

    def _launch(self, export=False):
        self._preview_job = None
        if self.dem is None:
            self.log("Load a DEM first.", level="warn")
            return
        if self._busy():
            self.log("A job is already running; wait for it to finish.", level="warn")
            return

        self._collect_params()
        params = dict(self.params)
        seed = self._seed()
        resolution = self._resolution()
        dem = self.dem
        do_map = self.obstacle_map_var.get()
        base_name = self.export_name_var.get().strip() or "procedural_lunar_terrain"

        self._cancel.clear()
        self.preview_button.state(["disabled"])
        self.export_button.state(["disabled"])
        self.stop_button.state(["!disabled"])
        self.status_var.set("Exporting..." if export else "Planning...")

        def worker():
            try:
                result = rock_field.build_rock_field(
                    dem, resolution, params, seed=seed,
                    progress=lambda m, f: self._queue.put(("progress", (m, f))),
                    cancel_check=self._cancel.is_set)
                self._queue.put(("result", result))
                if export:
                    written = rock_field.export_rock_field(
                        result, GENERATOR_OUTPUT_DIR, f"{base_name}_elevationMap",
                        export_obstacle_map=do_map,
                        progress=lambda m, f: self._queue.put(("progress", (m, f))),
                        cancel_check=self._cancel.is_set)
                    self._queue.put(("written", written))
            except terrain_core.TerrainCancelled:
                self._queue.put(("cancelled", None))
            except Exception:
                self._queue.put(("error", traceback.format_exc()))
            finally:
                self._queue.put(("done", None))

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()

    def export_plan(self):
        if self.dem is None:
            self.log("Load a DEM first.", level="warn")
            return
        self._collect_params()
        base_name = self.export_name_var.get().strip() or "procedural_lunar_terrain"
        group_dir = os.path.join(GENERATOR_OUTPUT_DIR, base_name)
        files = planned_rock_files(base_name, self.obstacle_map_var.get())
        file_list = "\n".join(f"    {name}\n        {desc}" for name, desc in files)
        message = (f"Plan the rock field on {os.path.basename(self.dem_path)} "
                   f"and write:\n\n{file_list}\n\nInto:\n"
                   + format_path_block(group_dir) + "\n\nContinue?")
        if not ask_confirmation(self, "Generate & export rock field", message,
                                ok_label="Generate"):
            return
        self._launch(export=True)

    # ------------------------------------------------------------ unreal
    def browse_models_folder(self):
        folder = filedialog.askdirectory(parent=self, title="Folder with rock models",
                                         initialdir=self.models_folder or PROJECT_ROOT)
        if folder:
            self.models_folder = os.path.normpath(folder)
            self.models_folder_var.set(self.models_folder)
            self._read_model_sizes()

    def _read_model_sizes(self):
        """Measures the models in the selected folder."""
        self.model_sizes = None
        rows = rock_models.folder_dimensions(self.models_folder) if self.models_folder else []
        self.model_radii = [radius for _name, radius, _h in rows if radius]
        if rows:
            self.model_sizes = rock_models.size_summary(rows)
            unknown = sum(1 for _name, radius, _h in rows if not radius)
            if self.model_sizes:
                low, high, ratio = self.model_sizes
                self.log(f"{len(rows)} rock models: footprint radius {low:.3f}-{high:.3f} m, "
                         f"height {ratio:.2f} x radius (median).")
            if unknown:
                self.log(f"{unknown} model(s) have no size readable outside Unreal "
                         "(.fbx); they are left out of the size match.", level="warn")
        elif self.models_folder:
            self.log("No .gltf, .glb, .obj or .fbx models in that folder.", level="warn")
        self._refresh_model_info()

    def _model_scale_range(self):
        """Returns the (min, max) model scale for the planned radii, or None without model sizes."""
        if not self.model_radii:
            return None
        return rock_models.scale_range(self.model_radii,
                                       self.rows_widgets['ROCK_MIN_SCALE'].get(),
                                       self.rows_widgets['ROCK_MAX_SCALE'].get())

    def _refresh_model_info(self, *_trace_args):
        """Updates the readout comparing planned rock sizes with the model sizes."""
        if not hasattr(self, "model_info_var"):
            return
        if not self.models_folder:
            self.model_info_var.set("")
            self.match_button.state(["disabled"])
            return
        if not self.model_sizes:
            self.model_info_var.set("Model sizes unknown (only .fbx, or none found): "
                                    "rocks are scaled to the planned radius.")
            self.match_button.state(["disabled"])
            return
        low, high, ratio = self.model_sizes
        smallest, largest = self._model_scale_range()
        text = (f"Models: radius {low:.3f}-{high:.3f} m, height {ratio:.2f} x radius.\n"
                f"Planned radii scale them {smallest:.2g}x-{largest:.2g}x.")
        planned_ratio = self.rows_widgets['ROCK_HEIGHT_RATIO'].get()
        stretched = largest > MODEL_SCALE_WARN or smallest < 1.0 / MODEL_SCALE_WARN
        if stretched:
            text += (" Rocks will look much larger or smaller than modelled: "
                     "use Match Rock Sizes to Models.")
        if abs(planned_ratio - ratio) > 0.25 * ratio:
            text += (f"\nHeight / radius is {planned_ratio:.2f}, the models are {ratio:.2f}: "
                     "height_m in the CSV will not match the placed rocks.")
        self.model_info_var.set(text)
        self.match_button.state(["!disabled"] if LOADER_AVAILABLE else ["disabled"])

    def match_sizes_to_models(self):
        """Sets the radius range and height ratio from the measured models."""
        if not self.model_sizes:
            return
        low, high, ratio = self.model_sizes
        self.rows_widgets['ROCK_MIN_SCALE'].set(low)
        self.rows_widgets['ROCK_MAX_SCALE'].set(high)
        self.rows_widgets['ROCK_HEIGHT_RATIO'].set(ratio)
        self._collect_params()
        self.log(f"Rock radii set to the models: {low:.3f}-{high:.3f} m, "
                 f"height {ratio:.2f} x radius. Generate & Export again before placing.",
                 level="ok")
        self._on_param_change()

    def _refresh_rock_estimate(self, *_trace_args):
        """Updates the rock-count estimate for the loaded map."""
        if not hasattr(self, "rock_estimate_var"):
            return
        if self.dem is None:
            self.rock_estimate_var.set("Load a DEM to estimate the rock count.")
            return
        params = rock_field.coerce_params(
            {key: row.get() for key, row in self.rows_widgets.items()})
        area = self.dem.shape[0] * self.dem.shape[1] * self._resolution() ** 2
        if self.result:
            area *= self.result['drivable_frac']
            where = "the drivable ground"
        else:
            where = "the whole map (before craters are excluded)"
        count = rock_field.estimate_rock_count(params, area)
        text = f"About {count:,} rocks on {where}."
        if count > rock_field.MAX_ROCKS:
            text += (f"\nOver the {rock_field.MAX_ROCKS:,} limit: the plan stops there and "
                     "covers less than asked. Lower the coverage or raise the radii.")
        placed = min(count, rock_field.MAX_ROCKS)
        if placed > UNREAL_ROCKS_WARN:
            minutes = placed * UNREAL_SECONDS_PER_ROCK / 60.0
            text += (f"\nPlacing them in Unreal takes roughly {minutes:.0f} min "
                     "(one actor per rock).")
        self.rock_estimate_var.set(text)

    def _rocks_csv_path(self):
        base_name = self.export_name_var.get().strip() or "procedural_lunar_terrain"
        return base_name, os.path.join(GENERATOR_OUTPUT_DIR, base_name,
                                       f"{base_name}_rocks.csv")

    def refresh_mask_ids(self):
        """Shows the object-mask ids placed rocks will be tagged with."""
        ids = mask_ids(self.app)
        self.mask_ids_var.set(
            f"Rocks are tagged for the rover's object mask as boulders (id {ids['boulder']}, "
            f"radius >= {ids['boulder_radius_m']:g} m) and small rocks (id {ids['small_rock']}). "
            "Change the ids, or re-tag placed rocks, in Ground Truth Masks.")

    def _start_unreal_job(self, status, job):
        """Runs `job(log)` on a worker thread, disabling the planner buttons meanwhile."""
        self.status_var.set(status)
        for button in (self.preview_button, self.export_button, self.place_button):
            button.state(["disabled"])

        def worker():
            try:
                job(lambda text, level=None: self._queue.put(("log", (text, level))))
            except Exception:
                self._queue.put(("log", (traceback.format_exc(), "error")))
            finally:
                self._queue.put(("done", None))

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()

    def place_in_unreal(self):
        """Sends the exported rock field to a running Unreal Editor."""
        if self._busy():
            self.log("A job is already running; wait for it to finish.", level="warn")
            return
        if not self.models_folder:
            self.log("Choose the folder with the rock models first.", level="warn")
            return
        base_name, rocks_csv = self._rocks_csv_path()
        if not os.path.isfile(rocks_csv):
            self.log(f"No {base_name}_rocks.csv yet: run Generate & Export first.",
                     level="warn")
            return
        try:
            resolution = float(self.resolution_var.get())
        except ValueError:
            resolution = 0.0
        if not resolution > 0:
            self.log("Resolution must be a positive number.", level="error")
            return
        ids = mask_ids(self.app)
        boulder, small, radius = ids["boulder"], ids["small_rock"], ids["boulder_radius_m"]

        # The Landscape is labelled after its heightmap; its width is read from
        # the heightmap JSON.
        dem_stem = (os.path.splitext(os.path.basename(self.dem_path))[0]
                    if self.dem_path else base_name)
        width = rocks_to_unreal.heightmap_width(
            os.path.join(GENERATOR_OUTPUT_DIR, dem_group_name(dem_stem)), dem_stem)
        seed = self._seed() or 1
        physics = self.physics_var.get()
        save_level = self.rock_save_level_var.get()

        if self.model_sizes is None:
            self._read_model_sizes()
        scale_range = self._model_scale_range()
        if scale_range and (scale_range[1] > MODEL_SCALE_WARN
                            or scale_range[0] < 1.0 / MODEL_SCALE_WARN):
            self.log(f"The planned radii scale the models {scale_range[0]:.2g}x-"
                     f"{scale_range[1]:.2g}x. Use Match Rock Sizes to Models and "
                     "re-export to place them at their real size.", level="warn")

        models_folder = self.models_folder
        self.log(f"Placing {os.path.basename(rocks_csv)} in Unreal "
                 f"(models: {os.path.basename(models_folder)}).")
        self._start_unreal_job(
            "Placing rocks in Unreal...",
            lambda log: rocks_to_unreal.send_rocks_to_unreal(
                rocks_csv, models_folder, resolution,
                landscape_label=f"{dem_stem}_heightmap", landscape_width=width,
                seed=seed, simulate_physics=physics, boulder_stencil=boulder,
                small_rock_stencil=small, boulder_min_radius_m=radius,
                save_level=save_level, log=log))

    def open_output_folder(self):
        base_name = self.export_name_var.get().strip() or "procedural_lunar_terrain"
        target = os.path.join(GENERATOR_OUTPUT_DIR, base_name)
        target = target if os.path.isdir(target) else GENERATOR_OUTPUT_DIR
        os.makedirs(target, exist_ok=True)
        try:
            os.startfile(target)  # noqa: S606 - Windows shell open
        except AttributeError:  # pragma: no cover - non-Windows fallback
            subprocess.Popen(["xdg-open", target])

    # --------------------------------------------------------- main pump
    def _drain_queue(self):
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "dem":
                    self.dem = payload
                    self.log(f"Loaded DEM {payload.shape[1]} x {payload.shape[0]} px.",
                             level="ok")
                    self.request_preview()
                elif kind == "progress":
                    message, fraction = payload
                    self.status_var.set(message)
                    self.progress.configure(value=fraction)
                elif kind == "result":
                    self.result = payload
                    self._render(payload)
                    self._log_result(payload)
                    self._refresh_rock_estimate()
                elif kind == "written":
                    self.log("Rock field written:", level="ok")
                    for label, path in payload.items():
                        self.log(f"  {label}: {path}")
                elif kind == "log":
                    text, level = payload
                    self.log(text, level=level)
                elif kind == "cancelled":
                    self.log("Job cancelled.", level="warn")
                    self.status_var.set("Cancelled")
                elif kind == "error":
                    self.log(payload, level="error")
                    self.status_var.set("Failed")
                    self._show_message("Rock planning failed.\nSee the activity log.")
                elif kind == "done":
                    self.preview_button.state(["!disabled"])
                    self.export_button.state(["!disabled"])
                    if LOADER_AVAILABLE:
                        self.place_button.state(["!disabled"])
                    self.stop_button.state(["disabled"])
                    if self.status_var.get() not in ("Failed", "Cancelled"):
                        self.status_var.set("Idle")
                    self.progress.configure(value=0)
        except queue.Empty:
            pass
        self.after(80, self._drain_queue)

    def _log_result(self, result):
        if result.get('rock_capped'):
            self.log(f"The coverage asked for {result['rock_requested']:,} rocks; the plan "
                     f"stops at {rock_field.MAX_ROCKS:,}, so it covers less than asked.",
                     level="warn")
        self.log(
            f"Placed {result['rock_count']} rocks. "
            f"Drivable {result['drivable_frac'] * 100:.1f}% of map "
            f"(craters cover {result['crater_frac'] * 100:.1f}%, impassable), "
            f"free after rocks {result['free_frac'] * 100:.1f}%.")
        if result['road_segments'] == 0:
            self.log("Fewer than two craters found, so no Voronoi roads were laid. "
                     "Lower the crater depth threshold to catch shallower craters.",
                     level="warn")
        elif result['connected']:
            self.log("Voronoi road network is one connected route.", level="ok")
        else:
            self.log("The Voronoi road network is split by impassable terrain into "
                     "more than one piece.", level="warn")

    def log(self, text, level=None):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text.rstrip() + "\n", level or ())
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ---------------------------------------------------------- drawing
    def _show_message(self, text):
        self.figure.clear()
        axes = self.figure.add_subplot(111)
        axes.axis("off")
        axes.text(0.5, 0.5, text, ha="center", va="center", fontsize=12,
                  transform=axes.transAxes)
        self.canvas.draw_idle()

    def _render(self, result):
        """Draws the hillshade with the impassable, corridor and rock overlays."""
        self.figure.clear()
        axes = self.figure.add_subplot(111)

        plan = result['plan_elevation']
        res_row, res_col = result['plan_res']
        relief = max(float(np.ptp(plan)), 1e-6)
        light = LightSource(azdeg=315, altdeg=45)
        vert_exag = (0.1 * max(plan.shape)) / relief
        shaded = light.shade(plan, cmap=matplotlib.colormaps["gray"],
                             blend_mode="soft", vert_exag=vert_exag)
        axes.imshow(shaded, origin="upper", interpolation="bilinear")

        # Craters: impassable and off-limits for rocks. Fill value 0.0 so the
        # winter colormap paints them blue (its 1.0 end is green, which would
        # clash with the corridors).
        crater = result['crater_mask']
        axes.imshow(np.where(crater, 0.0, np.nan), origin="upper",
                    cmap="winter", alpha=0.45, vmin=0, vmax=1)

        # Guaranteed corridors.
        for rr, cc in result['paths']:
            axes.plot(cc, rr, color="#39d353", linewidth=1.4, alpha=0.9)

        # Markers: crater centres, i.e. the Voronoi sites.
        marker_label = result.get('marker_label', 'Waypoint')
        if result.get('markers'):
            mr = [r for r, _ in result['markers']]
            mc = [c for _, c in result['markers']]
            axes.plot(mc, mr, "o", color="#58a6ff", markersize=5,
                      markeredgecolor="white", linewidth=0, label=marker_label)

        # Rocks, sized by footprint radius (points^2 area at the display scale).
        rocks = result['rocks_px']
        if len(rocks):
            axes.scatter(rocks[:, 1], rocks[:, 0], s=np.maximum(2, (rocks[:, 2] * 1.6) ** 2),
                         facecolor="#d9a066", edgecolor="#3a2a17", linewidths=0.2,
                         alpha=0.8, label="Rocks")

        rows, cols = result['plan_shape']
        if result['road_segments'] == 0:
            status = "no craters found for roads"
        elif result['connected']:
            status = "connected route OK"
        else:
            status = "route split by terrain"
        axes.set_title(
            f"Rock field & Voronoi rover corridors  -  {result['rock_count']} rocks, "
            f"{result['drivable_frac'] * 100:.0f}% drivable, {status}\n"
            f"craters cover {result['crater_frac'] * 100:.0f}%   |   "
            f"planning grid {cols} x {rows} px  ({res_col:.2f} m/px)", fontsize=9)
        axes.set_xlabel("planning pixels")
        axes.set_ylabel("planning pixels")

        legend_handles = [
            Line2D([0], [0], color="#39d353", lw=2, label="Rover corridor"),
            Line2D([0], [0], marker="o", color="#58a6ff", lw=0,
                   markeredgecolor="white", label=marker_label),
            Patch(facecolor="#d9a066", edgecolor="#3a2a17", label="Rock"),
            Patch(facecolor="blue", alpha=0.45, label="Crater"),
        ]
        axes.legend(handles=legend_handles, loc="upper right", fontsize=7,
                    framealpha=0.9)
        self.canvas.draw_idle()


# =========================================================================
# GROUND TRUTH MASKS
# =========================================================================

# Object-mask ids shared by the rock planner (placement) and this view.
DEFAULT_MASK_IDS = {"boulder": 20, "small_rock": 21, "boulder_radius_m": 0.15,
                    "crater": crater_mask.CRATER_ID, "rim": crater_mask.RIM_ID}
GNC_MASK_LABELS = "GNC-Moon-Simulator/component_stereo_depth/config/params.yaml"


def mask_ids(app):
    """The studio's current object-mask ids (defaults when not set)."""
    return getattr(app, "mask_ids", None) or dict(DEFAULT_MASK_IDS)


class GroundTruthStudio(ttk.Frame):
    """
    Labels for the rover's object mask: the stencil ids rocks are tagged with,
    and the crater map the mask material draws on the Landscape. Shows the
    craters and rock classes over the map, exports the crater map and sends
    both to Unreal.
    """

    def __init__(self, master, figure_dpi=100, source_path=None, resolution=1.0):
        super().__init__(master)
        self.app = master
        self.figure_dpi = figure_dpi
        self.dem = None
        self.dem_path = None
        self.craters = None           # find_craters() result for the loaded DEM
        self._crater_depth = None     # Min depth the craters were found with
        self._queue = queue.Queue()
        self._worker = None

        self._build_ui()
        self.resolution_var.set(f"{resolution:g}")
        self._load_ids()
        self.after(80, self._drain_queue)
        self.log("Set the mask ids, find the craters, then send them to Unreal.")
        if source_path and os.path.exists(source_path):
            self.load_dem(source_path)
        self.after_idle(self._place_sash)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        self._panel_width = self.master.px(348) if hasattr(self.master, "px") else 348
        self._wrapping_labels = []
        self._panes = ttk.PanedWindow(self, orient="horizontal")
        self._panes.pack(fill="both", expand=True)
        left = ttk.Frame(self._panes, padding=(8, 8, 4, 8))

        top_bar = ttk.Frame(left)
        top_bar.pack(fill="x")
        ttk.Button(top_bar, text="⌂ Home", width=9, command=self.app.show_home).pack(side="left")
        ttk.Label(top_bar, text="Ground Truth Masks",
                  font=("Segoe UI Semibold", 11)).pack(side="left", padx=(8, 0))

        actions = ttk.Frame(left)
        actions.pack(side="bottom", fill="x", pady=(6, 0))
        ttk.Button(actions, text="Open output folder",
                   command=self.open_output_folder).pack(fill="x")

        scroll_host = ttk.Frame(left)
        scroll_host.pack(side="top", fill="both", expand=True, pady=(8, 0))
        canvas = tk.Canvas(scroll_host, highlightthickness=0, borderwidth=0,
                           background=self.app.cget("background"))
        scrollbar = ttk.Scrollbar(scroll_host, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        window = canvas.create_window((0, 0), window=inner, anchor="nw")

        def on_canvas_resize(event):
            canvas.itemconfigure(window, width=event.width)
            wrap = max(160, event.width - 24)
            for label in self._wrapping_labels:
                if label.winfo_exists():
                    label.configure(wraplength=wrap)

        canvas.bind("<Configure>", on_canvas_resize)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self._hint(inner, "Labels for the rover's object mask (navcam/central/object_mask). "
                          "Rocks carry their id from placement; craters are drawn on the "
                          "Landscape by the mask material from a crater map.", pady=(0, 8))

        # Source DEM
        source = self._group(inner, "Map")
        self.source_var = tk.StringVar(value="(none - load a DEM)")
        ttk.Entry(source, textvariable=self.source_var, state="readonly",
                  font=("Consolas", 8)).pack(fill="x")
        row = ttk.Frame(source)
        row.pack(fill="x", pady=(6, 0))
        self.load_button = ttk.Button(row, text="Load DEM  ▾", command=self.open_dem_menu)
        self.load_button.pack(side="left", fill="x", expand=True)
        ttk.Label(row, text="  Resolution (m/px)", font=("Segoe UI", 8)).pack(side="left")
        self.resolution_var = tk.StringVar(value="1")
        ttk.Entry(row, textvariable=self.resolution_var, width=7, justify="right",
                  font=("Consolas", 9)).pack(side="left", padx=(4, 0))

        # Rocks
        rocks = self._group(inner, "Rocks")
        self.boulder_var, self.small_var, self.radius_var = (tk.StringVar() for _ in range(3))
        self._id_rows(rocks, (
            ("Boulder id", self.boulder_var,
             "Mask id (1-255) of boulders; 20 is 'boulder' in the GNC labels."),
            ("Small rock id", self.small_var,
             "Mask id (1-255) of small rocks; 21 is 'small_rock'. Use the boulder\n"
             "id here too for a single rock class."),
            ("Boulder if radius >= (m)", self.radius_var,
             "Rocks with a planned footprint radius at least this are boulders.")))
        self.rock_split_var = tk.StringVar()
        self._readout(rocks, self.rock_split_var)
        self.save_level_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(rocks, text="Save the level afterwards",
                        variable=self.save_level_var).pack(anchor="w", pady=(4, 0))
        self.rock_button = ttk.Button(rocks, text="Update Rock Ids in Unreal",
                                      command=self.update_rock_ids)
        self.rock_button.pack(fill="x", pady=(6, 0))
        Tooltip(self.rock_button,
                "Re-tags the rocks already placed for this map with the ids above,\n"
                "without moving them. New placements use these ids automatically.")

        # Craters
        craters = self._group(inner, "Craters")
        self.depth_row = ParamRow(craters, (
            "MIN_CRATER_DEPTH_M", "Min crater depth (m)", 0.05, 20.0, 'log',
            "Closed depressions at least this deep seed a crater; each is then\n"
            "fitted to its rim crest. Same setting as the rock planner's craters."),
            self._craters_changed)
        self.depth_row.set(rock_field.default_params()['MIN_CRATER_DEPTH_M'])
        self.rim_row = ParamRow(craters, (
            "RIM_FRACTION", "Rim width (x radius)", 0.0, 0.6, 'float',
            "Width of the rim ring outside the crest, as a fraction of the crater\n"
            "radius. 0 leaves rims as plain landscape."),
            self._redraw)
        self.rim_row.set(crater_mask.RIM_FRACTION)
        self.crater_var, self.rim_var = tk.StringVar(), tk.StringVar()
        self._id_rows(craters, (
            ("Crater id", self.crater_var,
             "Mask id (1-255) of the ground inside a crater's rim crest; 30 is 'crater'."),
            ("Rim id", self.rim_var,
             "Mask id of the rim ring; 31 is 'crater_rim'. 0 disables rims.")))
        self.crater_info_var = tk.StringVar(value="Load a DEM, then Find Craters.")
        self._readout(craters, self.crater_info_var)
        self.find_button = ttk.Button(craters, text="Find Craters", command=self.find_craters)
        self.find_button.pack(fill="x", pady=(6, 0))
        self.export_button = ttk.Button(craters, text="Export Crater Map",
                                        command=self.export_crater_map)
        self.export_button.pack(fill="x", pady=(4, 0))
        Tooltip(self.export_button,
                "Writes <map>_craterMask.png (mask ids, 4 cells per DEM cell) and its\n"
                ".json into the map's output folder.")
        self.send_button = ttk.Button(craters, text="Send Crater Map to Unreal",
                                      style="Accent.TButton", command=self.send_crater_map)
        self.send_button.pack(fill="x", pady=(4, 0))
        Tooltip(self.send_button,
                "Imports the exported crater map and rebuilds PPM_ObjectMask so Landscape\n"
                "pixels take the crater ids. Rocks keep theirs. The original material is\n"
                "kept as PPM_ObjectMask_Original.")
        self.restore_button = ttk.Button(craters, text="Restore Original Mask Material",
                                         command=self.restore_material)
        self.restore_button.pack(fill="x", pady=(4, 0))
        Tooltip(self.restore_button, "Puts back PPM_ObjectMask as it was before the crater map.")

        # GNC labels
        labels = self._group(inner, "GNC Labels")
        self._hint(labels, f"mask_labels in {GNC_MASK_LABELS} must list these ids:")
        self.labels_text = tk.Text(labels, height=6, font=("Consolas", 8), relief="solid",
                                   borderwidth=1, wrap="none")
        self.labels_text.pack(fill="x", pady=(4, 0))
        ttk.Button(labels, text="Copy", command=self.copy_labels).pack(anchor="e", pady=(4, 0))

        for var in (self.boulder_var, self.small_var, self.radius_var,
                    self.crater_var, self.rim_var):
            var.trace_add("write", self._ids_changed)
        self._unreal_buttons = (self.rock_button, self.send_button, self.restore_button)
        if not LOADER_AVAILABLE:
            for button in self._unreal_buttons:
                button.state(["disabled"])

        for widget in (inner, canvas):
            self._bind_mousewheel(widget, canvas)

        # Right: map on top, log below.
        right = ttk.Frame(self._panes)
        self._panes.add(left, weight=0)
        self._panes.add(right, weight=1)
        log_frame = ttk.Frame(right, padding=(8, 4, 8, 8))
        ttk.Label(log_frame, text="Activity log", font=("Segoe UI Semibold", 10)).pack(anchor="w")
        self.status_var = tk.StringVar(value="Idle")
        self.log_text = tk.Text(log_frame, height=6, font=("Consolas", 8), wrap="word",
                                relief="solid", borderwidth=1, background="#1e1e1e",
                                foreground="#d4d4d4", insertbackground="#d4d4d4")
        self.log_text.pack(fill="x", pady=(4, 0))
        for tag, colour in (("warn", "#e6c07b"), ("error", "#e06c75"), ("ok", "#98c379")):
            self.log_text.tag_configure(tag, foreground=colour)
        self.log_text.configure(state="disabled")
        log_frame.pack(side="bottom", fill="x")

        plot = ttk.Frame(right)
        plot.pack(side="top", fill="both", expand=True, padx=(4, 8), pady=(8, 0))
        self.figure = Figure(figsize=(7.6, 6.4), dpi=self.figure_dpi, layout="constrained")
        self.canvas = FigureCanvasTkAgg(self.figure, master=plot)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        NavigationToolbar2Tk(self.canvas, plot).update()
        self._message("Load a DEM to see its craters and rocks.")

    def _group(self, parent, title):
        group = ttk.Labelframe(parent, text=title, padding=(8, 4, 8, 8), style="Group.TLabelframe")
        group.pack(fill="x", pady=(0, 8))
        return group

    def _hint(self, parent, text, pady=(0, 0)):
        label = ttk.Label(parent, text=text, justify="left", style="HomeHint.TLabel")
        label.pack(anchor="w", fill="x", pady=pady)
        self._wrapping_labels.append(label)

    def _readout(self, parent, var):
        label = ttk.Label(parent, textvariable=var, justify="left", style="Readout.TLabel")
        label.pack(anchor="w", pady=(4, 0))
        self._wrapping_labels.append(label)

    def _id_rows(self, parent, rows):
        grid = ttk.Frame(parent)
        grid.pack(fill="x", pady=(4, 0))
        grid.columnconfigure(0, weight=1)
        for index, (label, var, tip) in enumerate(rows):
            name = ttk.Label(grid, text=label, font=("Segoe UI", 8))
            name.grid(row=index, column=0, sticky="w", pady=1)
            ttk.Entry(grid, textvariable=var, width=8, justify="right",
                      font=("Consolas", 9)).grid(row=index, column=1, sticky="e", pady=1)
            Tooltip(name, tip)

    def _place_sash(self, attempt=0):
        if not self.winfo_exists():
            return
        self.update_idletasks()
        if self._panes.winfo_width() > self._panel_width * 2:
            self._panes.sashpos(0, self._panel_width)
            self.update_idletasks()
            if abs(self._panes.sashpos(0) - self._panel_width) <= 2:
                return
        if attempt < 6:
            self.after(60, lambda: self._place_sash(attempt + 1))

    def _bind_mousewheel(self, widget, canvas):
        widget.bind("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))
        for child in widget.winfo_children():
            self._bind_mousewheel(child, canvas)

    def log(self, text, level=None):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text.rstrip() + "\n", level or ())
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ----------------------------------------------------------------- ids
    def _load_ids(self):
        ids = mask_ids(self.app)
        self.boulder_var.set(str(ids["boulder"]))
        self.small_var.set(str(ids["small_rock"]))
        self.radius_var.set(f"{ids['boulder_radius_m']:g}")
        self.crater_var.set(str(ids["crater"]))
        self.rim_var.set(str(ids["rim"]))

    def _read_ids(self):
        """The ids in the fields, or None (with a log line) when one is invalid."""
        try:
            ids = {"boulder": int(self.boulder_var.get()), "small_rock": int(self.small_var.get()),
                   "boulder_radius_m": float(self.radius_var.get()),
                   "crater": int(self.crater_var.get()), "rim": int(self.rim_var.get())}
        except ValueError:
            return None
        if not (1 <= ids["boulder"] <= 255 and 1 <= ids["small_rock"] <= 255
                and 1 <= ids["crater"] <= 255 and 0 <= ids["rim"] <= 255
                and ids["boulder_radius_m"] >= 0 and ids["crater"] != ids["rim"]):
            return None
        return ids

    def _ids_changed(self, *_trace_args):
        ids = self._read_ids()
        if ids is not None:
            self.app.mask_ids = ids         # Shared with the rock planner's placement
        self._refresh_labels(ids)
        self._refresh_rock_split(ids)
        self._redraw()

    def _refresh_labels(self, ids):
        self.labels_text.configure(state="normal")
        self.labels_text.delete("1.0", "end")
        if ids is None:
            self.labels_text.insert("end", "Ids must be whole numbers 1-255\n(rim 0-255, radius >= 0).")
        else:
            lines = ['- "2:rover"', '- "10:landscape"', f'- "{ids["boulder"]}:boulder"']
            if ids["small_rock"] != ids["boulder"]:
                lines.append(f'- "{ids["small_rock"]}:small_rock"')
            lines.append(f'- "{ids["crater"]}:crater"')
            if ids["rim"]:
                lines.append(f'- "{ids["rim"]}:crater_rim"')
            self.labels_text.insert("end", "\n".join(lines))
        self.labels_text.configure(state="disabled")

    def copy_labels(self):
        self.clipboard_clear()
        self.clipboard_append(self.labels_text.get("1.0", "end").strip())
        self.log("mask_labels copied to the clipboard.", level="ok")

    # --------------------------------------------------------------- files
    def _group_name(self):
        return dem_group_name(self.dem_path) if self.dem_path else None

    def _rock_scales(self):
        group = self._group_name()
        if not group:
            return None
        try:
            return np.loadtxt(os.path.join(GENERATOR_OUTPUT_DIR, group, f"{group}_rocks.csv"),
                              delimiter=",", skiprows=1, usecols=(3, 4, 5), ndmin=2)
        except (OSError, ValueError, IndexError):
            return None

    def _refresh_rock_split(self, ids):
        rocks = self._rock_scales()
        if ids is None:
            self.rock_split_var.set("")
        elif rocks is None or not len(rocks):
            self.rock_split_var.set("No exported rock field for this map yet (stage 2).")
        else:
            boulders = int((rocks[:, 2] >= ids["boulder_radius_m"]).sum())
            self.rock_split_var.set(f"This map's rocks: {boulders} boulders (id {ids['boulder']}), "
                                    f"{len(rocks) - boulders} small (id {ids['small_rock']}).")

    def open_dem_menu(self):
        drop_dem_menu(self, self.load_button, self.load_dem, self.browse_dem)

    def browse_dem(self, initialdir=None):
        path = filedialog.askopenfilename(parent=self, title="Select a DEM",
                                          initialdir=initialdir or map_browse_dir(),
                                          filetypes=DEM_FILETYPES)
        if path:
            self.load_dem(path)

    def load_dem(self, path):
        self.dem_path = path
        self.source_var.set(os.path.basename(path))
        self.craters = None
        self.log(f"Loading DEM: {os.path.basename(path)} ...")
        self._message(f"Loading {os.path.basename(path)} ...")

        def worker():
            try:
                self._queue.put(("dem", load_dem_array(path)))
            except Exception as exc:
                self._queue.put(("log", (f"Could not load DEM: {exc}", "error")))
        threading.Thread(target=worker, daemon=True).start()

    def _resolution(self):
        try:
            return max(1e-6, float(self.resolution_var.get()))
        except ValueError:
            return 1.0

    def open_output_folder(self):
        group = self._group_name()
        target = os.path.join(GENERATOR_OUTPUT_DIR, group) if group else GENERATOR_OUTPUT_DIR
        target = target if os.path.isdir(target) else GENERATOR_OUTPUT_DIR
        os.makedirs(target, exist_ok=True)
        try:
            os.startfile(target)  # noqa: S606 - Windows shell open
        except AttributeError:  # pragma: no cover - non-Windows fallback
            subprocess.Popen(["xdg-open", target])

    # ------------------------------------------------------------- craters
    def _busy(self):
        return self._worker is not None and self._worker.is_alive()

    def _run(self, status, job):
        """Runs `job(log)` on a worker thread with the buttons disabled."""
        if self._busy():
            self.log("A job is already running; wait for it to finish.", level="warn")
            return
        self.status_var.set(status)
        for button in (self.find_button, self.export_button) + self._unreal_buttons:
            button.state(["disabled"])

        def worker():
            try:
                job(lambda text, level=None: self._queue.put(("log", (text, level))))
            except Exception:
                self._queue.put(("log", (traceback.format_exc(), "error")))
            finally:
                self._queue.put(("done", None))
        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()

    def _craters_changed(self):
        if self.craters is not None:
            self.crater_info_var.set("Depth changed: Find Craters again.")

    def find_craters(self, then=None):
        if self.dem is None:
            self.log("Load a DEM first.", level="warn")
            return
        dem, resolution, depth = self.dem, self._resolution(), self.depth_row.get()

        def job(log):
            log(f"Finding craters (min depth {depth:g} m) ...")
            craters = crater_mask.find_craters(dem, resolution, depth)
            self._queue.put(("craters", (craters, depth, then)))
        self._run("Finding craters...", job)

    def _labels(self, supersample=1):
        ids = self._read_ids()
        rim = ids["rim"] if self.rim_row.get() > 0 else 0
        return crater_mask.render_labels(self.dem.shape, self.craters, ids["crater"], rim,
                                         self.rim_row.get(), supersample), ids

    def export_crater_map(self, then=None):
        if self._read_ids() is None:
            self.log("Fix the ids first (1-255, rim 0-255).", level="error")
            return
        if self.craters is None or self._crater_depth != self.depth_row.get():
            self.find_craters(then=lambda: self.export_crater_map(then))
            return
        k = crater_mask.supersample_for(self.dem.shape)
        labels, ids = self._labels(k)
        rim = ids["rim"] if self.rim_row.get() > 0 else 0
        written = crater_mask.export_crater_mask(
            labels, GENERATOR_OUTPUT_DIR, self._group_name(), self._resolution(),
            self._crater_depth, ids["crater"], rim, self.rim_row.get(), supersample=k)
        self.log(f"Crater map written ({labels.shape[1]} x {labels.shape[0]}, {k} per DEM cell):",
                 level="ok")
        for path in written.values():
            self.log(f"  {path}")
        if then:
            self.after(10, then)

    def send_crater_map(self):
        if self.dem_path is None:
            self.log("Load a DEM first.", level="warn")
            return
        group = self._group_name()
        folder = os.path.join(GENERATOR_OUTPUT_DIR, group)
        png = os.path.join(folder, f"{group}_craterMask.png")
        try:
            with open(os.path.join(folder, f"{group}_craterMask.json")) as handle:
                meta = json.load(handle)
        except (OSError, ValueError):
            meta = None
        if meta is None or not os.path.isfile(png):
            self.export_crater_map(then=self.send_crater_map)
            return
        stem = os.path.splitext(os.path.basename(self.dem_path))[0]
        width = rocks_to_unreal.heightmap_width(os.path.join(GENERATOR_OUTPUT_DIR, group), stem)
        self.log(f"Sending {os.path.basename(png)} to Unreal (crater id {meta['crater_id']}, "
                 f"rim id {meta['rim_id']}).")
        self._run("Sending the crater map to Unreal...",
                  lambda log: crater_mask_to_unreal.send_crater_mask_to_unreal(
                      png, meta["width"], meta["height"], meta["resolution_m"],
                      landscape_label=f"{stem}_heightmap", landscape_width=width,
                      supersample=meta.get("supersample", 1), log=log))

    def restore_material(self):
        self._run("Restoring the mask material...",
                  lambda log: crater_mask_to_unreal.remove_crater_mask_from_unreal(log=log))

    def update_rock_ids(self):
        ids, group = self._read_ids(), self._group_name()
        if ids is None or group is None:
            self.log("Load the map and set valid ids first.", level="warn")
            return
        save = self.save_level_var.get()
        self.log(f"Updating the ids of the '{group}' rocks in Unreal.")
        self._run("Updating rock ids in Unreal...",
                  lambda log: rocks_to_unreal.send_stencils_to_unreal(
                      group, ids["boulder"], ids["small_rock"], ids["boulder_radius_m"],
                      save_level=save, log=log))

    # ------------------------------------------------------------- drawing
    def _message(self, text):
        self.figure.clear()
        axes = self.figure.add_subplot(111)
        axes.axis("off")
        axes.text(0.5, 0.5, text, ha="center", va="center", fontsize=12, transform=axes.transAxes)
        self.canvas.draw_idle()

    def _redraw(self):
        """Relief with the crater map and the exported rocks, coloured by mask class."""
        if self.dem is None or not hasattr(self, "canvas"):
            return
        self.figure.clear()
        axes = self.figure.add_subplot(111)
        dem, res = self.dem, self._resolution()
        finite = np.where(np.isfinite(dem), dem, np.nanmin(dem))
        relief = LightSource(azdeg=315, altdeg=45).hillshade(
            finite, vert_exag=(0.1 * max(dem.shape)) / max(float(np.ptp(finite)), 1e-6))
        extent = (-0.5 * res, (dem.shape[1] - 0.5) * res, (dem.shape[0] - 0.5) * res, -0.5 * res)
        axes.imshow(relief, cmap="gray", extent=extent, vmin=0, vmax=1)
        handles = []
        ids = self._read_ids()
        title = "Object mask classes"
        if self.craters is not None and ids is not None:
            labels, _ids = self._labels()
            overlay = np.zeros(labels.shape + (4,))
            overlay[labels == ids["crater"]] = (1.0, 0.69, 0.0, 0.45)
            overlay[(labels == ids["rim"]) & (ids["rim"] > 0)] = (0.23, 0.63, 1.0, 0.45)
            axes.imshow(overlay, extent=extent, interpolation="nearest")
            handles += [Patch(facecolor=(1.0, 0.69, 0.0, 0.6), label=f"Crater ({ids['crater']})")]
            if ids["rim"] and self.rim_row.get() > 0:
                handles.append(Patch(facecolor=(0.23, 0.63, 1.0, 0.6), label=f"Rim ({ids['rim']})"))
            title += f"  -  {len(self.craters)} craters"
        rocks = self._rock_scales()
        if rocks is not None and len(rocks) and ids is not None:
            big = rocks[:, 2] >= ids["boulder_radius_m"]
            for mask, colour, name in ((~big, "#ffe14d", f"Small rock ({ids['small_rock']})"),
                                       (big, "#ff4d4d", f"Boulder ({ids['boulder']})")):
                if mask.any():
                    axes.scatter(rocks[mask, 0], rocks[mask, 1],
                                 s=np.clip(rocks[mask, 2] * 40, 1, 30), c=colour,
                                 edgecolors="none", label=name)
            handles += [Line2D([0], [0], marker="o", lw=0, color="#ff4d4d",
                               label=f"Boulder ({ids['boulder']})"),
                        Line2D([0], [0], marker="o", lw=0, color="#ffe14d",
                               label=f"Small rock ({ids['small_rock']})")]
            title += f", {len(rocks)} rocks"
        if handles:
            axes.legend(handles=handles, loc="upper right", fontsize=7, framealpha=0.9)
        axes.set_title(title, fontsize=9)
        axes.set_xlabel("x (m)")
        axes.set_ylabel("y (m)")
        self.canvas.draw_idle()

    # ---------------------------------------------------------- main pump
    def _drain_queue(self):
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "dem":
                    self.dem = payload
                    self.log(f"Loaded DEM {payload.shape[1]} x {payload.shape[0]} px.", level="ok")
                    self.crater_info_var.set("Press Find Craters to map them.")
                    self._refresh_rock_split(self._read_ids())
                    self._redraw()
                elif kind == "craters":
                    self.craters, self._crater_depth, then = payload
                    labels, ids = self._labels()
                    cover = 100.0 * (labels == ids["crater"]).mean()
                    self.crater_info_var.set(f"{len(self.craters)} craters, {cover:.1f}% of the map.")
                    self.log(f"Found {len(self.craters)} craters.", level="ok")
                    self._redraw()
                    if then:
                        self.after(250, then)   # Once the worker thread has ended
                elif kind == "log":
                    text, level = payload
                    self.log(text, level=level)
                elif kind == "done":
                    self.find_button.state(["!disabled"])
                    self.export_button.state(["!disabled"])
                    if LOADER_AVAILABLE:
                        for button in self._unreal_buttons:
                            button.state(["!disabled"])
                    self.status_var.set("Idle")
        except queue.Empty:
            pass
        self.after(80, self._drain_queue)


# =========================================================================
# SUN & SHADOWS
# =========================================================================

SUN_PREVIEW_SIZES = ("500", "1000", "1500", "2000")
SUN_DISPLAY_MODES = ("Lunar render", "Lunar render, tinted shadows", "Shadows over relief")
# The Landscape mirrors the DEM (columns run towards -X): Unreal's Top view
# (+X right, +Y down) is the DEM image flipped left to right.
SUN_ORIENTATIONS = ("Unreal Top view (+X right, +Y down)", "DEM image (row 0 at the top)")
SUN_SHADOW_RGBA = (0.16, 0.35, 0.85, 0.55)     # Cast-shadow tint
SUN_3D_CELLS = 140                             # Longest side of the 3D mesh

# ACES filmic curve followed by display gamma, tabulated for lunar_tone.
SUN_TONE_X = np.linspace(0.0, 16.0, 4097)
SUN_TONE_Y = np.clip(SUN_TONE_X * (2.51 * SUN_TONE_X + 0.03)
                     / (SUN_TONE_X * (2.43 * SUN_TONE_X + 0.59) + 0.14), 0.0, 1.0) ** (1 / 2.2)
SUN_TONE_GROUND = 0.67                         # Display value of the median lit ground


def lunar_tone(light):
    """
    Maps direct light (0..1) to display values the way Unreal renders it:
    filmic curve, exposure normalised to the median lit ground (as auto-exposure
    does). Grazing light falls to black, as in the engine.
    """
    light = np.nan_to_num(light)
    lit = light[light > 0]
    median = float(np.median(lit)) if lit.size else 1.0
    gain = np.interp(SUN_TONE_GROUND, SUN_TONE_Y, SUN_TONE_X) / max(median, 1e-6)
    return np.interp(light * gain, SUN_TONE_X, SUN_TONE_Y)


class SunStudio(ttk.Frame):
    """
    Sets the sun over a DEM and sends it to Unreal as a lunar Directional Light.
    The sun is placed with the sliders, on the sky dome, or by drawing the shadow
    an object should cast; the map shows the resulting light and cast shadows.

    Shading runs on a worker thread over a reduced DEM (sun_shadows module);
    requests made while it runs collapse into one re-render.
    """

    def __init__(self, master, figure_dpi=100, source_path=None, resolution=1.0):
        super().__init__(master)
        self.app = master
        self.figure_dpi = figure_dpi
        self.dem = None               # Loaded elevation array
        self.dem_path = None
        self.view = None              # (reduced DEM, metres per cell) the map shows
        self.lit = None               # Last computed {'az', 'el', 'shadow', 'light'}
        self.probe = None             # (x_m, y_m) base of the object whose shadow is drawn
        self._drag = None             # (x_m, y_m) where a shadow drag started
        self._sky_drag = False        # Sun being dragged on the sky dome
        self._map_artists = None
        self._probe_artists = []
        self._3d_stale = True

        self._queue = queue.Queue()
        self._worker = None           # Shadow computation
        self._unreal_worker = None
        self._token = 0               # Incremented on every render request
        self._render_job = None

        self._build_ui()
        self.resolution_var.set(f"{resolution:g}")
        self._on_sun_change(render=False)
        self.after(80, self._drain_queue)
        self.log("Load a DEM, then set the sun with the sliders, the sky dome, or by "
                 "drawing a shadow on the map.")
        if source_path and os.path.exists(source_path):
            self.load_dem(source_path)
        self.after_idle(self._place_sash)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        panel_width = self.master.px(348) if hasattr(self.master, "px") else 348
        self._panel_width = panel_width
        self._wrapping_labels = []

        self._panes = ttk.PanedWindow(self, orient="horizontal")
        self._panes.pack(fill="both", expand=True)
        left = ttk.Frame(self._panes, padding=(8, 8, 4, 8))

        top_bar = ttk.Frame(left)
        top_bar.pack(fill="x")
        ttk.Button(top_bar, text="⌂ Home", width=9, command=self.go_home).pack(side="left")
        ttk.Label(top_bar, text="Sun & Shadows",
                  font=("Segoe UI Semibold", 11)).pack(side="left", padx=(8, 0))

        actions = ttk.Frame(left)
        actions.pack(side="bottom", fill="x", pady=(6, 0))
        self.send_button = ttk.Button(actions, text="Send Sun to Unreal",
                                      style="Accent.TButton", command=self.send_to_unreal)
        self.send_button.pack(fill="x")
        Tooltip(self.send_button,
                "Points the level's sun at this position, with lunar settings.\n"
                "See 'Unreal Light' below for what is changed.")
        self.read_button = ttk.Button(actions, text="Read Sun from Unreal",
                                      command=self.read_from_unreal)
        self.read_button.pack(fill="x", pady=(4, 0))
        Tooltip(self.read_button, "Sets the sliders to the sun currently in the Unreal level.")
        if not LOADER_AVAILABLE:
            self.send_button.state(["disabled"])
            self.read_button.state(["disabled"])

        scroll_host = ttk.Frame(left)
        scroll_host.pack(side="top", fill="both", expand=True, pady=(8, 0))
        canvas = tk.Canvas(scroll_host, highlightthickness=0, borderwidth=0,
                           background=self.app.cget("background"))
        scrollbar = ttk.Scrollbar(scroll_host, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        window = canvas.create_window((0, 0), window=inner, anchor="nw")

        def on_canvas_resize(event):
            canvas.itemconfigure(window, width=event.width)
            self._rewrap_labels(event.width)

        canvas.bind("<Configure>", on_canvas_resize)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self._add_hint(inner, "Places the sun so shadows fall where you want them, shows "
                              "the terrain lit by it, and sets the matching light in Unreal.",
                       pady=(0, 8))

        # Source DEM
        source_group = self._group(inner, "Source DEM")
        self.source_var = tk.StringVar(value="(none - load a DEM)")
        ttk.Entry(source_group, textvariable=self.source_var, state="readonly",
                  font=("Consolas", 8)).pack(fill="x")
        res_row = ttk.Frame(source_group)
        res_row.pack(fill="x", pady=(6, 0))
        self.load_button = ttk.Button(res_row, text="Load DEM  ▾", command=self.open_dem_menu)
        self.load_button.pack(side="left", fill="x", expand=True)
        Tooltip(self.load_button,
                "Lists the DEMs already in input_maps/ and outputs/, or opens\n"
                "the file picker for a map kept anywhere else.")
        ttk.Label(res_row, text="  Resolution (m/px)", font=("Segoe UI", 8)).pack(side="left")
        self.resolution_var = tk.StringVar(value="1")
        res_entry = ttk.Entry(res_row, textvariable=self.resolution_var, width=7,
                              justify="right", font=("Consolas", 9))
        res_entry.pack(side="left", padx=(4, 0))
        res_entry.bind("<Return>", lambda e: self._rebuild_view())
        res_entry.bind("<FocusOut>", lambda e: self._rebuild_view())
        Tooltip(res_entry,
                "Ground metres per DEM pixel. Shadow lengths depend on it: a wrong\n"
                "resolution stretches or shrinks every shadow.")
        detail_row = ttk.Frame(source_group)
        detail_row.pack(fill="x", pady=(6, 0))
        ttk.Label(detail_row, text="Preview detail (px)", font=("Segoe UI", 8)).pack(side="left")
        self.preview_size_var = tk.StringVar(value="1000")
        detail = ttk.Combobox(detail_row, textvariable=self.preview_size_var, width=7,
                              values=SUN_PREVIEW_SIZES, state="readonly")
        detail.pack(side="right")
        detail.bind("<<ComboboxSelected>>", lambda e: self._rebuild_view())
        Tooltip(detail,
                "Longest side of the map the shadows are computed on. Larger is\n"
                "sharper but slower to follow the sliders.")
        self.source_note_var = tk.StringVar()
        self._wrapping_labels.append(ttk.Label(
            source_group, textvariable=self.source_note_var, justify="left",
            style="Readout.TLabel", foreground="#b8860b"))
        self._wrapping_labels[-1].pack(anchor="w", pady=(4, 0))

        # Sun position
        sun_group = self._group(inner, "Sun Position")
        self.azimuth_row = ParamRow(sun_group, (
            "SUN_AZ", "Azimuth (deg)", 0.0, 360.0, 'float',
            "Direction the sunlight comes from, clockwise from the top of the DEM\n"
            "image: 0 = Unreal -Y, 90 = -X, 180 = +Y, 270 = +X. The sky dome shows\n"
            "it in the map's orientation."),
            self._on_sun_change)
        self.elevation_row = ParamRow(sun_group, (
            "SUN_EL", "Elevation (deg)", 0.5, 90.0, 'float',
            "Height of the sun above the horizon. Low suns cast long shadows;\n"
            "near the lunar poles it stays within a few degrees."),
            self._on_sun_change)
        self.azimuth_row.set(135.0)
        self.elevation_row.set(15.0)
        self.sun_info_var = tk.StringVar()
        self._wrapping_labels.append(ttk.Label(sun_group, textvariable=self.sun_info_var,
                                               justify="left", style="Readout.TLabel"))
        self._wrapping_labels[-1].pack(anchor="w", pady=(4, 0))

        # Shadow tool
        shadow_group = self._group(inner, "Place a Shadow")
        self._add_hint(shadow_group,
                       "On the map, click where an object stands and drag to where its "
                       "shadow should end: the sun moves to cast exactly that shadow. "
                       "A click without dragging shows the shadow under the current sun. "
                       "Lengths assume flat ground.", parent_pad=(0, 6))
        grid = ttk.Frame(shadow_group)
        grid.pack(fill="x")
        grid.columnconfigure(0, weight=1)
        self.object_height_var = tk.StringVar(value="1.0")
        self.shadow_dir_var = tk.StringVar()
        self.shadow_len_var = tk.StringVar()
        for row, (label, var, tip) in enumerate((
                ("Object height (m)", self.object_height_var,
                 "Height of the reference object (a rock, the rover mast...)."),
                ("Shadow points to (deg)", self.shadow_dir_var,
                 "Direction the shadow falls, clockwise from map-up."),
                ("Shadow length (m)", self.shadow_len_var,
                 "Length of the object's shadow on flat ground."))):
            name = ttk.Label(grid, text=label, font=("Segoe UI", 8))
            name.grid(row=row, column=0, sticky="w", pady=1)
            entry = ttk.Entry(grid, textvariable=var, width=9, justify="right",
                              font=("Consolas", 9))
            entry.grid(row=row, column=1, sticky="e", pady=1)
            entry.bind("<Return>", lambda e, r=row: self._shadow_fields_changed(r))
            Tooltip(name, tip)
        buttons = ttk.Frame(shadow_group)
        buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(buttons, text="Apply Shadow",
                   command=self.apply_shadow_fields).pack(side="left", fill="x", expand=True)
        ttk.Button(buttons, text="Clear Marker",
                   command=self.clear_probe).pack(side="left", fill="x", expand=True, padx=(4, 0))

        # Display
        display_group = self._group(inner, "Display")
        mode_row = ttk.Frame(display_group)
        mode_row.pack(fill="x")
        ttk.Label(mode_row, text="Map", font=("Segoe UI", 8)).pack(side="left")
        self.display_var = tk.StringVar(value=SUN_DISPLAY_MODES[1])
        mode = ttk.Combobox(mode_row, textvariable=self.display_var, values=SUN_DISPLAY_MODES,
                            state="readonly", width=26)
        mode.pack(side="right")
        mode.bind("<<ComboboxSelected>>", lambda e: self._redraw_lighting())
        Tooltip(mode,
                "Lunar render: sunlight only, as the Moon has no sky light, so shadows\n"
                "are black, with Unreal's filmic tone curve. Tinted: cast shadows in\n"
                "blue, to tell them from slopes facing away. Over relief: a neutral\n"
                "relief with the cast shadows on top.")
        view_row = ttk.Frame(display_group)
        view_row.pack(fill="x", pady=(4, 0))
        ttk.Label(view_row, text="View", font=("Segoe UI", 8)).pack(side="left")
        self.orientation_var = tk.StringVar(value=SUN_ORIENTATIONS[0])
        orientation = ttk.Combobox(view_row, textvariable=self.orientation_var,
                                   values=SUN_ORIENTATIONS, state="readonly", width=32)
        orientation.pack(side="right")
        orientation.bind("<<ComboboxSelected>>", lambda e: self._rebuild_map())
        Tooltip(orientation,
                "Unreal Top view: the map as Unreal's Top viewport shows it, to compare\n"
                "shadows side by side. The Landscape mirrors the DEM, so this is the\n"
                "DEM image flipped left to right.")

        # Unreal light settings
        unreal_group = self._group(inner, "Unreal Light")
        settings = ttk.Frame(unreal_group)
        settings.pack(fill="x")
        settings.columnconfigure(0, weight=1)
        self.intensity_var = tk.StringVar(value="120000")
        self.disc_var = tk.StringVar(value="0.53")
        for row, (label, var, tip) in enumerate((
                ("Intensity (lux)", self.intensity_var,
                 "Illuminance of the sunlight. Direct sunlight on the Moon is about\n"
                 "130,000 lux; 120,000 is what the level's camera exposure is set for."),
                ("Sun disc (deg)", self.disc_var,
                 "Angular diameter of the sun, which sets how soft shadow edges are.\n"
                 "0.53 deg is the Sun seen from the Moon."))):
            name = ttk.Label(settings, text=label, font=("Segoe UI", 8))
            name.grid(row=row, column=0, sticky="w", pady=1)
            ttk.Entry(settings, textvariable=var, width=9, justify="right",
                      font=("Consolas", 9)).grid(row=row, column=1, sticky="e", pady=1)
            Tooltip(name, tip)
        self.save_level_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(unreal_group, text="Save the level afterwards",
                        variable=self.save_level_var).pack(anchor="w", pady=(4, 0))
        self._add_hint(unreal_group,
                       "Moves the light labelled 'LunarSun', or the level's only "
                       "Directional Light, and spawns 'LunarSun' if there is neither. "
                       "Lunar settings: white light with no atmospheric reddening, movable, "
                       "shadows up to at least 200 m. During Play In Editor only the "
                       "running game changes; send again after stopping it to keep the sun.",
                       parent_pad=(6, 0))

        self._bind_mousewheel(inner, canvas)
        self._bind_mousewheel(canvas, canvas)

        # Right: map / 3D tabs on top, log at the bottom.
        right = ttk.Frame(self._panes)
        self._panes.add(left, weight=0)
        self._panes.add(right, weight=1)

        log_frame = ttk.Frame(right, padding=(8, 4, 8, 8))
        ttk.Label(log_frame, text="Activity log",
                  font=("Segoe UI Semibold", 10)).pack(anchor="w")
        self.status_var = tk.StringVar(value="Idle")
        self.log_text = tk.Text(log_frame, height=6, font=("Consolas", 8), wrap="word",
                                relief="solid", borderwidth=1, background="#1e1e1e",
                                foreground="#d4d4d4", insertbackground="#d4d4d4")
        self.log_text.pack(fill="x", pady=(4, 0))
        self.log_text.tag_configure("warn", foreground="#e6c07b")
        self.log_text.tag_configure("error", foreground="#e06c75")
        self.log_text.tag_configure("ok", foreground="#98c379")
        self.log_text.configure(state="disabled")
        log_frame.pack(side="bottom", fill="x")

        self.tabs = ttk.Notebook(right)
        self.tabs.pack(side="top", fill="both", expand=True, padx=(4, 8), pady=(8, 0))
        map_tab = ttk.Frame(self.tabs)
        self.tabs.add(map_tab, text="  Map  ")
        self.figure = Figure(figsize=(8.2, 6.2), dpi=self.figure_dpi, layout="constrained")
        self.canvas = FigureCanvasTkAgg(self.figure, master=map_tab)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self.toolbar = NavigationToolbar2Tk(self.canvas, map_tab)
        self.toolbar.update()
        self.canvas.mpl_connect("button_press_event", self._on_press)
        self.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.canvas.mpl_connect("button_release_event", self._on_release)

        surface_tab = ttk.Frame(self.tabs)
        self.tabs.add(surface_tab, text="  3D View  ")
        self.figure3d = Figure(figsize=(8.2, 6.2), dpi=self.figure_dpi, layout="constrained")
        self.canvas3d = FigureCanvasTkAgg(self.figure3d, master=surface_tab)
        self.canvas3d.get_tk_widget().pack(fill="both", expand=True)
        NavigationToolbar2Tk(self.canvas3d, surface_tab).update()
        self.tabs.bind("<<NotebookTabChanged>>", lambda e: self._draw_3d())

        self._show_message("Load a DEM to light it.")

    def _group(self, parent, title):
        group = ttk.Labelframe(parent, text=title, padding=(8, 4, 8, 8),
                               style="Group.TLabelframe")
        group.pack(fill="x", pady=(0, 8))
        return group

    def _add_hint(self, parent, text, pady=(0, 0), parent_pad=None):
        label = ttk.Label(parent, text=text, justify="left", style="HomeHint.TLabel")
        label.pack(anchor="w", fill="x", pady=parent_pad or pady)
        self._wrapping_labels.append(label)
        return label

    def _place_sash(self, attempt=0):
        """Gives the parameter column its width once the panes have a real size."""
        if not self.winfo_exists():
            return
        self.update_idletasks()
        if self._panes.winfo_width() > self._panel_width * 2:
            self._panes.sashpos(0, self._panel_width)
            self.update_idletasks()
            if abs(self._panes.sashpos(0) - self._panel_width) <= 2:
                return
        if attempt < 6:
            self.after(60, lambda: self._place_sash(attempt + 1))

    def _bind_mousewheel(self, widget, canvas):
        widget.bind("<MouseWheel>",
                    lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))
        for child in widget.winfo_children():
            self._bind_mousewheel(child, canvas)

    def _rewrap_labels(self, width):
        wrap = max(160, width - 24)
        for label in self._wrapping_labels:
            if label.winfo_exists():
                label.configure(wraplength=wrap)

    def go_home(self):
        self.app.show_home()

    def log(self, text, level=None):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text.rstrip() + "\n", level or ())
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ---------------------------------------------------------------- sun
    def sun(self):
        """Current (azimuth, elevation) in degrees."""
        return self.azimuth_row.get() % 360.0, min(max(self.elevation_row.get(), 0.5), 90.0)

    def set_sun(self, azimuth, elevation, render=True):
        self.azimuth_row.set(round(azimuth % 360.0, 2))
        self.elevation_row.set(round(min(max(elevation, 0.5), 90.0), 2))
        self._on_sun_change(render=render)

    def _object_height(self):
        try:
            return max(1e-3, float(self.object_height_var.get()))
        except ValueError:
            return 1.0

    def _on_sun_change(self, render=True):
        azimuth, elevation = self.sun()
        height = self._object_height()
        length = sun_shadows.shadow_length(height, elevation)
        self.shadow_dir_var.set(f"{(azimuth + 180.0) % 360.0:.1f}")
        self.shadow_len_var.set(f"{length:.2f}")
        pitch, yaw = -elevation, math.degrees(math.atan2(math.cos(math.radians(azimuth)),
                                                         math.sin(math.radians(azimuth))))
        self.sun_info_var.set(
            f"Light from {azimuth:.1f} deg, {elevation:.1f} deg up: a {height:g} m object "
            f"casts a {length:.2f} m shadow towards {(azimuth + 180.0) % 360.0:.0f} deg.\n"
            f"Unreal light rotation: pitch {pitch:.1f}, yaw {yaw:.1f}.")
        self._draw_probe()
        self._draw_sky()
        if render:
            self.request_render(debounce_ms=60)

    def _shadow_fields_changed(self, row):
        if row == 0:                   # Height edited: keep the sun, refresh the length
            self._on_sun_change(render=False)
        else:
            self.apply_shadow_fields()

    def apply_shadow_fields(self):
        """Sets the sun from the shadow direction and length fields."""
        try:
            direction = float(self.shadow_dir_var.get())
            length = float(self.shadow_len_var.get())
        except ValueError:
            self.log("Shadow direction and length must be numbers.", level="warn")
            return
        if length <= 0:
            self.log("The shadow length must be positive.", level="warn")
            return
        elevation = math.degrees(math.atan2(self._object_height(), length))
        self.set_sun(direction + 180.0, elevation)

    def clear_probe(self):
        self.probe = None
        self._draw_probe()

    # ---------------------------------------------------------------- DEM
    def open_dem_menu(self):
        drop_dem_menu(self, self.load_button, self.load_dem, self.browse_dem)

    def browse_dem(self, initialdir=None):
        path = filedialog.askopenfilename(
            parent=self, title="Select a DEM to light",
            initialdir=initialdir or map_browse_dir(), filetypes=DEM_FILETYPES)
        if path:
            self.load_dem(path)

    def load_dem(self, path):
        self.source_var.set(os.path.basename(path))
        self.dem_path = path
        self.source_note_var.set(dem_source_note(path))
        self.log(f"Loading DEM: {os.path.basename(path)} ...")
        self._show_message(f"Loading {os.path.basename(path)} ...")

        def worker():
            try:
                self._queue.put(("dem", load_dem_array(path)))
            except Exception as exc:
                self._queue.put(("error", f"Could not load DEM: {exc}"))
        threading.Thread(target=worker, daemon=True).start()

    def _resolution(self):
        try:
            return max(1e-6, float(self.resolution_var.get()))
        except ValueError:
            return 1.0

    def _rebuild_view(self):
        """Reduces the DEM to the preview size and redraws everything."""
        if self.dem is None:
            return
        size = int(self.preview_size_var.get() or 1000)
        stride = max(1, int(math.ceil(max(self.dem.shape) / size)))
        reduced = self.dem[::stride, ::stride]
        self.view = (reduced, self._resolution() * stride)
        self.lit = None
        self._map_artists = None
        self._3d_stale = True
        self.request_render()

    # ------------------------------------------------------------ shading
    def request_render(self, debounce_ms=0):
        if self.view is None:
            return
        self._token += 1
        if self._render_job:
            self.after_cancel(self._render_job)
        self._render_job = self.after(debounce_ms, self._launch_render)

    def _launch_render(self):
        self._render_job = None
        if self._worker is not None and self._worker.is_alive():
            return                     # _drain_queue relaunches it when the worker ends
        token, (dem, res) = self._token, self.view
        azimuth, elevation = self.sun()

        def worker():
            try:
                shadow = sun_shadows.cast_shadows(dem, res, azimuth, elevation)
                light = sun_shadows.illumination(dem, res, azimuth, elevation, shadow)
                self._queue.put(("lit", (token, dem, {"az": azimuth, "el": elevation,
                                                      "shadow": shadow, "light": light})))
            except Exception:
                self._queue.put(("error", traceback.format_exc()))
        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()

    def _display_rgb(self):
        """RGB image of the map for the current display mode."""
        light, shadow = self.lit["light"], self.lit["shadow"]
        mode = self.display_var.get()
        if mode == SUN_DISPLAY_MODES[2]:
            base = self._relief
        else:
            base = lunar_tone(light)
        rgb = np.repeat(base[..., None], 3, axis=2)
        if mode != SUN_DISPLAY_MODES[0]:
            tint, alpha = np.array(SUN_SHADOW_RGBA[:3]), SUN_SHADOW_RGBA[3]
            rgb[shadow] = rgb[shadow] * (1 - alpha) + tint * alpha
        return np.clip(rgb, 0.0, 1.0)

    # ------------------------------------------------------------ drawing
    def _show_message(self, text):
        self.figure.clear()
        self._map_artists = None
        axes = self.figure.add_subplot(111)
        axes.axis("off")
        axes.text(0.5, 0.5, text, ha="center", va="center", fontsize=12,
                  transform=axes.transAxes)
        self.canvas.draw_idle()

    def _build_map(self):
        """Creates the map, sky dome and readout axes for the current view."""
        dem, res = self.view
        rows, cols = dem.shape
        finite = np.where(np.isfinite(dem), dem, np.nanmin(dem))
        relief = LightSource(azdeg=315, altdeg=45).hillshade(
            finite, vert_exag=(0.1 * max(dem.shape)) / max(float(np.ptp(finite)), 1e-6))
        self._relief = 0.25 + 0.6 * relief

        self.figure.clear()
        grid = self.figure.add_gridspec(2, 2, width_ratios=[3.3, 1], height_ratios=[1, 1.1])
        axes = self.figure.add_subplot(grid[:, 0])
        sky = self.figure.add_subplot(grid[0, 1], projection="polar")
        info = self.figure.add_subplot(grid[1, 1])
        info.axis("off")

        extent = (-0.5 * res, (cols - 0.5) * res, (rows - 0.5) * res, -0.5 * res)
        image = axes.imshow(np.zeros((rows, cols, 3)), extent=extent, origin="upper",
                            interpolation="antialiased")
        unreal_view = self._unreal_view()
        if unreal_view:
            axes.invert_xaxis()        # Columns run towards -X
            axes.set_xlabel("x (m) along the DEM columns   |   Unreal +X to the right")
            axes.set_ylabel("y (m) along the DEM rows   |   Unreal +Y down")
        else:
            axes.set_xlabel("x (m) along the DEM columns   |   Unreal -X to the right")
            axes.set_ylabel("y (m) along the DEM rows   |   Unreal +Y down")
        axes.set_facecolor("black")

        # The dome is mirrored with the map, so each azimuth sits on the same side.
        sky.set_theta_zero_location("N")
        sky.set_theta_direction(1 if unreal_view else -1)
        sky.set_rlim(0, 90)
        sky.set_rticks([30, 60])
        sky.set_yticklabels(["60°", "30°"], fontsize=7, color="#777777")
        sky.set_xticks(np.radians([0, 90, 180, 270]))
        sky.set_xticklabels(["-Y", "-X", "+Y", "+X"], fontsize=8)
        sky.set_facecolor("#0b1020")
        sky.grid(color="#3a4660", linewidth=0.6)
        sky.set_title("Sky - click or drag the sun", fontsize=8)
        sun_line, = sky.plot([], [], color="#ffd34d", linewidth=1, linestyle=":")
        sun_dot, = sky.plot([], [], "o", color="#ffd34d", markersize=11,
                            markeredgecolor="white")
        text = info.text(0.0, 1.0, "", va="top", ha="left", family="monospace",
                         fontsize=8, transform=info.transAxes)
        self._map_artists = {"axes": axes, "image": image, "sky": sky, "info": info,
                             "sun_line": sun_line, "sun_dot": sun_dot, "text": text}
        self._probe_artists = []

    def _unreal_view(self):
        return self.orientation_var.get() == SUN_ORIENTATIONS[0]

    def _rebuild_map(self):
        self._map_artists = None
        self._redraw_lighting()

    def _redraw_lighting(self):
        if self.lit is None or self.view is None:
            return
        if self._map_artists is None:
            self._build_map()
        art = self._map_artists
        art["image"].set_data(self._display_rgb())

        shadow, light = self.lit["shadow"], self.lit["light"]
        valid = np.isfinite(self.view[0])
        cells = max(int(valid.sum()), 1)
        cast = 100.0 * (shadow & valid).sum() / cells
        dark = 100.0 * ((light <= 0) & ~shadow & valid).sum() / cells
        azimuth, elevation = self.lit["az"], self.lit["el"]
        art["axes"].set_title(
            f"Sun from {azimuth:.1f} deg at {elevation:.1f} deg  -  "
            f"{cast:.1f}% of the map in cast shadow", fontsize=9)
        dem, res = self.view
        art["text"].set_text(
            f"Sun\n  azimuth   {azimuth:6.1f} deg\n  elevation {elevation:6.1f} deg\n\n"
            f"Sun lies towards Unreal\n  X {-math.sin(math.radians(azimuth)):+5.2f}  "
            f"Y {-math.cos(math.radians(azimuth)):+5.2f}\n"
            f"Shadows point {(azimuth + 180) % 360:5.1f} deg\n"
            f"  length  {1 / math.tan(math.radians(elevation)):6.2f} x height\n\n"
            f"Map\n  cast shadow  {cast:5.1f} %\n  facing away  {dark:5.1f} %\n\n"
            f"Preview {dem.shape[1]} x {dem.shape[0]} px\n  at {res:.3g} m/px")
        self._draw_sky(draw=False)
        self._draw_probe(draw=False)
        self.canvas.draw_idle()
        self._3d_stale = True
        self._draw_3d()

    def _draw_sky(self, draw=True):
        if not self._map_artists:
            return
        azimuth, elevation = self.sun()
        theta, radius = math.radians(azimuth), 90.0 - elevation
        self._map_artists["sun_dot"].set_data([theta], [radius])
        self._map_artists["sun_line"].set_data([theta, theta], [0.0, radius])
        if draw:
            self.canvas.draw_idle()

    def _draw_probe(self, draw=True, tip=None):
        """Draws the reference object and its shadow (or the shadow being dragged)."""
        if not self._map_artists:
            return
        for artist in self._probe_artists:
            artist.remove()
        self._probe_artists = []
        if self.probe is not None:
            axes = self._map_artists["axes"]
            x0, y0 = self.probe
            if tip is None:
                azimuth, elevation = self.sun()
                length = sun_shadows.shadow_length(self._object_height(), elevation)
                a = math.radians(azimuth)
                tip = (x0 - math.sin(a) * length, y0 + math.cos(a) * length)
            length = math.hypot(tip[0] - x0, tip[1] - y0)
            line, = axes.plot([x0, tip[0]], [y0, tip[1]], color="#ff9f1c", linewidth=2.2,
                              solid_capstyle="round")
            base, = axes.plot([x0], [y0], "^", color="white", markeredgecolor="black",
                              markersize=8)
            end, = axes.plot([tip[0]], [tip[1]], "o", color="#ff9f1c", markersize=4)
            label = axes.annotate(f"{self._object_height():g} m object: {length:.2f} m shadow",
                                  (tip[0], tip[1]), xytext=(6, 6), textcoords="offset points",
                                  fontsize=8, color="white",
                                  bbox=dict(boxstyle="round,pad=0.2", fc="#000000aa", ec="none"))
            self._probe_artists = [line, base, end, label]
        if draw:
            self.canvas.draw_idle()

    def _draw_3d(self):
        """Lit 3D surface with the incoming sunlight; drawn only while its tab shows."""
        if (self.lit is None or not self._3d_stale
                or self.tabs.index(self.tabs.select()) != 1):
            return
        self._3d_stale = False
        dem, res = self.view
        stride = max(1, int(math.ceil(max(dem.shape) / SUN_3D_CELLS)))
        z = dem[::stride, ::stride]
        z = np.where(np.isfinite(z), z, np.nanmin(z))
        colours = self._display_rgb()[::stride, ::stride]
        rows, cols = z.shape
        step = res * stride
        xs, ys = np.meshgrid(np.arange(cols) * step, np.arange(rows) * step)

        self.figure3d.clear()
        # Draw in call order so the sun ray stays above the surface.
        axes = self.figure3d.add_subplot(111, projection="3d", computed_zorder=False)
        axes.plot_surface(xs, ys, z, facecolors=colours, rstride=1, cstride=1,
                          shade=False, linewidth=0, antialiased=False)
        width, depth = cols * step, rows * step
        relief = max(float(np.ptp(z)), 1e-6)
        # Flat maps are exaggerated to a relief of at least 8% of their width.
        exaggeration = max(1.0, 0.08 * max(width, depth) / relief)

        # Sun ray to the map centre, at its true angle before exaggeration.
        azimuth, elevation = self.lit["az"], self.lit["el"]
        toward_sun = sun_shadows.sun_vector(azimuth, elevation)
        centre = np.array([width / 2, depth / 2, float(np.nanmax(z))])
        start = centre + toward_sun * 0.45 * max(width, depth)
        axes.plot(*zip(start, centre), color="#ffd34d", linewidth=2.5)
        axes.scatter(*start, color="#ffd34d", s=90, edgecolor="white", depthshade=False)

        z_low, z_high = float(np.nanmin(z)), max(float(np.nanmax(z)), start[2])
        axes.set_xlim(0, width)
        axes.set_ylim(depth, 0)        # Rows grow towards the viewer, as on the map
        axes.set_zlim(z_low, z_high)
        axes.set_box_aspect((width, depth, (z_high - z_low) * exaggeration))
        axes.zaxis.set_major_locator(MaxNLocator(4))
        axes.set_title(f"Sun from {azimuth:.1f} deg at {elevation:.1f} deg"
                       + (f"  -  heights x{exaggeration:.1f}" if exaggeration > 1.05 else ""),
                       fontsize=9)
        axes.set_xlabel("x (m)")
        axes.set_ylabel("y (m)")
        axes.set_zlabel("z (m)")
        axes.view_init(elev=35, azim=-60)
        self.canvas3d.draw_idle()

    # -------------------------------------------------------------- mouse
    def _toolbar_busy(self):
        return getattr(self.toolbar.mode, "name", "NONE") != "NONE"

    def _sun_at_cursor(self, event):
        """Places the sun under the cursor on the sky dome, clamped to the horizon."""
        theta, radius = self._map_artists["sky"].transData.inverted().transform((event.x, event.y))
        self.set_sun(math.degrees(theta), 90.0 - min(max(radius, 0.0), 89.5))

    def _on_press(self, event):
        if self._map_artists is None or self._toolbar_busy() or event.button != 1:
            return
        if event.inaxes is self._map_artists["sky"]:
            self._sky_drag = True
            self._sun_at_cursor(event)
        elif event.inaxes is self._map_artists["axes"] and event.xdata is not None:
            self._drag = (event.xdata, event.ydata)
            self.probe = self._drag
            self._draw_probe()

    def _on_motion(self, event):
        if self._sky_drag:
            self._sun_at_cursor(event)     # Keeps following outside the dome
            return
        if (self._drag is None or event.inaxes is not self._map_artists["axes"]
                or event.xdata is None):
            return
        x0, y0 = self._drag
        if math.hypot(event.xdata - x0, event.ydata - y0) < 1e-9:
            return
        azimuth, elevation = sun_shadows.sun_from_shadow(
            event.xdata - x0, event.ydata - y0, self._object_height())
        self.set_sun(azimuth, elevation)
        self._draw_probe(tip=(event.xdata, event.ydata))

    def _on_release(self, event):
        self._sky_drag = False
        if self._drag is None:
            return
        self._drag = None
        self._draw_probe()

    # ------------------------------------------------------------- unreal
    def _unreal_busy(self):
        return self._unreal_worker is not None and self._unreal_worker.is_alive()

    def _start_unreal_job(self, status, job):
        self.status_var.set(status)
        self.send_button.state(["disabled"])
        self.read_button.state(["disabled"])

        def worker():
            try:
                job(lambda text, level=None: self._queue.put(("log", (text, level))))
            except Exception:
                self._queue.put(("log", (traceback.format_exc(), "error")))
            finally:
                self._queue.put(("unreal_done", None))
        self._unreal_worker = threading.Thread(target=worker, daemon=True)
        self._unreal_worker.start()

    def send_to_unreal(self):
        if self._unreal_busy():
            self.log("Unreal is still busy with the last request.", level="warn")
            return
        try:
            intensity = float(self.intensity_var.get())
            disc = float(self.disc_var.get())
        except ValueError:
            self.log("Intensity and sun disc must be numbers.", level="error")
            return
        if intensity <= 0 or disc < 0:
            self.log("Intensity must be positive and the sun disc not negative.", level="error")
            return
        azimuth, elevation = self.sun()
        save_level = self.save_level_var.get()
        self.log(f"Sending the sun to Unreal: azimuth {azimuth:.1f}, elevation "
                 f"{elevation:.1f} deg, {intensity:.0f} lux.")
        self._start_unreal_job(
            "Setting the sun in Unreal...",
            lambda log: sun_to_unreal.send_sun_to_unreal(
                azimuth, elevation, intensity_lux=intensity, source_angle_deg=disc,
                save_level=save_level, log=log))

    def read_from_unreal(self):
        if self._unreal_busy():
            self.log("Unreal is still busy with the last request.", level="warn")
            return

        def job(log):
            found = sun_to_unreal.read_sun_from_unreal(log=log)
            if found:
                self._queue.put(("unreal_sun", found))
        self._start_unreal_job("Reading the sun from Unreal...", job)

    # ---------------------------------------------------------- main pump
    def _drain_queue(self):
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "dem":
                    self.dem = payload
                    self.log(f"Loaded DEM {payload.shape[1]} x {payload.shape[0]} px.",
                             level="ok")
                    self._rebuild_view()
                elif kind == "lit":
                    token, dem, lit = payload
                    if self.view is not None and dem is self.view[0]:
                        self.lit = lit
                        self._redraw_lighting()
                    if token != self._token:
                        self._launch_render()     # A newer sun is pending
                elif kind == "unreal_sun":
                    self.set_sun(payload["azimuth"], payload["elevation"])
                    self.intensity_var.set(f"{payload['intensity']:.0f}")
                    self.disc_var.set(f"{payload['source_angle']:.2f}")
                    where = "running game" if payload.get("pie") else "level"
                    self.log(f"Read '{payload['label']}' from the {where}: azimuth "
                             f"{payload['azimuth']:.1f}, elevation {payload['elevation']:.1f} deg, "
                             f"{payload['intensity']:.0f} lux.", level="ok")
                elif kind == "log":
                    text, level = payload
                    self.log(text, level=level)
                elif kind == "error":
                    self.log(payload, level="error")
                    self._show_message("Could not light the map.\nSee the activity log.")
                elif kind == "unreal_done":
                    if LOADER_AVAILABLE:
                        self.send_button.state(["!disabled"])
                        self.read_button.state(["!disabled"])
                    self.status_var.set("Idle")
        except queue.Empty:
            pass
        self.after(80, self._drain_queue)


# =========================================================================
# MAIN APPLICATION
# =========================================================================


class UnrealDispatchWindow(ttk.Frame):
    """
    View that converts an existing DEM to an Unreal Landscape.

    Offers only the inputs the export reads: the map, its resolution, and
    whether to dispatch to a running editor.
    """

    def __init__(self, parent, source_path=None, resolution=1.0, figure_dpi=100):
        super().__init__(parent, padding=(8, 8, 8, 8))
        self.app = parent

        self.source_path = None
        self.destination_path = None
        self.worker = None
        self._cancel_event = threading.Event()
        self._queue = queue.Queue()

        self._build(resolution, figure_dpi=figure_dpi)
        self.after_idle(self._place_sash)

        if source_path:
            self.set_source(source_path, announce=False)
        else:
            self.log("Pick a DEM with Browse, then Send to Unreal.")
        if not LOADER_AVAILABLE:
            self.log(f"Unreal loader modules are not importable: {LOADER_IMPORT_ERROR}",
                     level="error")
            self.send_button.state(["disabled"])

        self._drain()

    # ------------------------------------------------------------- layout
    # Controls column width in pixels; the drag handle adjusts it from there.
    CONTROLS_WIDTH = 400

    def _build(self, resolution, figure_dpi=100):
        self.header = ViewHeader(
            self, "Send a Map to Unreal", self._on_close,
            subtitle="DEM to 16-bit heightmap, then the Landscape actor")
        self.header.pack(fill="x", pady=(0, 8))

        # Controls on the left, map preview on the right.
        self._panes = ttk.PanedWindow(self, orient="horizontal")
        self._panes.pack(fill="both", expand=True)

        outer = ttk.Frame(self._panes, padding=(2, 0, 6, 0))
        right = ttk.Frame(self._panes, padding=(6, 0, 2, 0))
        self._panes.add(outer, weight=0)
        self._panes.add(right, weight=1)

        # --- source map ---
        source_group = ttk.Labelframe(outer, text="Source map",
                                      style="Group.TLabelframe", padding=8)
        source_group.pack(fill="x")

        self.source_var = tk.StringVar(value="(no map selected)")
        self.source_entry = ttk.Entry(source_group, textvariable=self.source_var,
                                      font=("Consolas", 8), state="readonly")
        self.source_entry.pack(fill="x")
        Tooltip(self.source_entry,
                "The DEM to convert. Long paths scroll: the filename is kept\n"
                "in view because that is the part worth checking.")

        source_buttons = ttk.Frame(source_group)
        source_buttons.pack(fill="x", pady=(6, 0))
        self.browse_button = ttk.Button(source_buttons, text="Browse  \u25be",
                                        command=self.open_source_menu)
        self.browse_button.pack(side="left")
        Tooltip(self.browse_button,
                "Lists the DEMs already in input_maps/ and outputs/, or opens\n"
                "the file picker for a map kept anywhere else.")

        ttk.Label(source_group, style="HomeHint.TLabel", justify="left",
                  text="An elevation map in metres "
                       "(.npy, .csv, .txt, .tif, .tiff).").pack(
            anchor="w", pady=(6, 0))

        # Empty unless the pick looks wrong; the menu filters these out but the
        # file picker cannot.
        self.source_note_var = tk.StringVar()
        ttk.Label(source_group, textvariable=self.source_note_var, justify="left",
                  style="Readout.TLabel", foreground="#b8860b",
                  wraplength=self.CONTROLS_WIDTH - 60).pack(anchor="w")

        # --- settings ---
        settings = ttk.Labelframe(outer, text="Conversion",
                                  style="Group.TLabelframe", padding=8)
        settings.pack(fill="x", pady=(8, 0))
        settings.columnconfigure(1, weight=1)

        resolution_label = ttk.Label(settings, text="Resolution (m/px)",
                                     font=("Segoe UI", 8))
        resolution_label.grid(row=0, column=0, sticky="w")
        Tooltip(resolution_label,
                "Ground distance covered by one pixel. Drives the Unreal\n"
                "landscape XY scale; it does not change the relief.\n"
                "GeoTIFFs carry their own resolution and ignore this.")
        self.resolution_var = tk.StringVar(value=f"{resolution:g}")
        ttk.Entry(settings, textvariable=self.resolution_var, width=12,
                  justify="right", font=("Consolas", 9)).grid(
            row=0, column=1, sticky="w", padx=(8, 0))

        self.dispatch_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(settings, text="Dispatch to running Unreal Editor",
                        variable=self.dispatch_var).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(settings, style="HomeHint.TLabel",
                  text="Unticked, the heightmap is written but no editor is "
                       "contacted.").grid(row=2, column=0, columnspan=2, sticky="w")
        self.save_level_var = tk.BooleanVar(value=True)
        save_check = ttk.Checkbutton(settings, text="Save the level afterwards",
                                     variable=self.save_level_var)
        save_check.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        Tooltip(save_check, 'Saves the open Unreal level once the Landscape is spawned and its\nheightmap applied.')

        # --- destination ---
        destination = ttk.Frame(outer)
        destination.pack(fill="x", pady=(8, 0))
        ttk.Label(destination, text="Writes to", font=("Segoe UI", 8)).pack(anchor="w")
        self.destination_var = tk.StringVar(value="-")
        # No wraplength: the path is pre-broken at its separators.
        self.destination_label = ttk.Label(destination,
                                           textvariable=self.destination_var,
                                           style="Readout.TLabel", justify="left")
        self.destination_label.pack(anchor="w", fill="x")
        destination.bind("<Configure>", self._reflow_destination)
        self.open_folder_button = ttk.Button(destination, text="Open output folder",
                                             command=self.open_output_folder,
                                             state="disabled")
        self.open_folder_button.pack(anchor="w", pady=(4, 0))

        # --- progress ---
        progress_frame = ttk.Frame(outer)
        progress_frame.pack(fill="x", pady=(10, 0))
        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(progress_frame, textvariable=self.status_var,
                  style="Readout.TLabel").pack(anchor="w")
        self.progress = ttk.Progressbar(progress_frame, mode="determinate", maximum=1.0)
        self.progress.pack(fill="x", pady=(4, 0))

        # --- actions ---
        actions = ttk.Frame(outer)
        actions.pack(side="bottom", fill="x", pady=(10, 0))
        self.send_button = ttk.Button(actions, text="Send to Unreal",
                                      style="Accent.TButton", command=self.send)
        self.send_button.pack(side="right")
        self.stop_button = ttk.Button(actions, text="Stop", command=self.stop,
                                      state="disabled")
        self.stop_button.pack(side="right", padx=(0, 6))
        # Home lives in the view header.

        # --- right: the map, with the log beneath it ---
        self._log = ActivityLog(right, height=6)
        self.view = MapView(right, figure_dpi=figure_dpi, on_status=self.log)
        self._log.pack(side="bottom", fill="x")
        self.view.pack(side="top", fill="both", expand=True)

    # -------------------------------------------------------------- input
    def set_source(self, path, announce=True):
        """Points the export at `path` and refreshes the destination readout."""
        self.source_path = path
        self.source_var.set(path)
        # Scroll to the end: a full path overflows the field, and the filename
        # is the half worth reading, not the drive letter.
        self.source_entry.after_idle(self.source_entry.xview_moveto, 1.0)
        self.destination_path = output_group_dir(path)
        self._reflow_destination()
        self.source_note_var.set(dem_source_note(path))
        self.open_folder_button.state(["!disabled"])
        # Preview the map as soon as it is selected.
        self.view.show_path(path)
        if announce:
            self.log(f"Source map: {path}")

    def _reflow_destination(self, event=None):
        """
        Re-lays the destination path for the column's current width.
        """
        if not self.destination_path:
            self.destination_var.set("-")
            return
        # During <Configure>, winfo_width() still reports the old width.
        available = event.width if event is not None else \
            self.destination_label.winfo_width()
        available = max(available, self.CONTROLS_WIDTH - 40)
        # Consolas 8 is about 6.6 px per character; leave a margin for padding.
        columns = max(24, int(available / 6.6) - 2)
        self.destination_var.set(format_path_block(self.destination_path,
                                                   width=columns, indent=""))

    def _place_sash(self, attempt=0):
        """Gives the controls column its width once the panes have a real size."""
        if not self.winfo_exists():
            return
        self.update_idletasks()
        if self._panes.winfo_width() > self.CONTROLS_WIDTH * 2:
            self._panes.sashpos(0, self.CONTROLS_WIDTH)
            self.update_idletasks()
            if abs(self._panes.sashpos(0) - self.CONTROLS_WIDTH) <= 2:
                return
        if attempt < 6:
            self.after(60, lambda: self._place_sash(attempt + 1))

    def open_source_menu(self):
        """Drops the map list under the Browse button, rebuilt so it stays current."""
        drop_dem_menu(self, self.browse_button, self.set_source, self.browse)

    def browse(self, initialdir=None):
        path = filedialog.askopenfilename(
            parent=self, title="Select a DEM to send to Unreal",
            initialdir=initialdir or map_browse_dir(), filetypes=DEM_FILETYPES)
        if path:
            self.set_source(path)

    def open_output_folder(self):
        """Opens the folder the heightmap is written to."""
        target = output_group_dir(self.source_path) if self.source_path else OUTPUT_ROOT
        os.makedirs(target, exist_ok=True)
        try:
            os.startfile(target)  # noqa: S606 - Windows shell open
        except AttributeError:  # pragma: no cover - non-Windows fallback
            subprocess.Popen(["xdg-open", target])

    def _read_resolution(self):
        """The resolution entry as a positive float, or None if it is unusable."""
        try:
            value = float(self.resolution_var.get().strip())
        except ValueError:
            self.log("Resolution must be a number, e.g. 1.0", level="error")
            return None
        if not value > 0:
            self.log("Resolution must be greater than zero.", level="error")
            return None
        return value

    # ------------------------------------------------------------- action
    def send(self):
        if self._busy():
            self.log("A conversion is already running.", level="warn")
            return

        problem = dem_source_problem(self.source_path)
        if problem:
            self.log(problem[0], level=problem[1])
            return

        resolution = self._read_resolution()
        if resolution is None:
            return

        source_path = self.source_path
        dispatch = self.dispatch_var.get()
        save_level = self.save_level_var.get()
        output_dir = output_group_dir(source_path)

        self.log(f"Sending to Unreal: {os.path.basename(source_path)}")
        self._cancel_event.clear()
        self.status_var.set("Sending to Unreal...")
        self.send_button.state(["disabled"])
        self.stop_button.state(["!disabled"])

        def job():
            try:
                convert_and_dispatch(
                    source_path, resolution, dispatch, output_dir,
                    log=lambda text, level=None: self._post("log", (text, level)),
                    progress=lambda message, fraction: self._post(
                        "progress", (message, fraction)),
                    dialog=lambda title, message: self._post(
                        "dialog", (title, message)),
                    cancelled=self._cancel_event.is_set, save_level=save_level)
            except terrain_core.TerrainCancelled:
                self._post("cancelled", None)
            except Exception:
                self._post("error", traceback.format_exc())
            finally:
                self._post("done", None)

        self.worker = threading.Thread(target=job, daemon=True)
        self.worker.start()

    def stop(self):
        if not self._busy():
            return
        self._cancel_event.set()
        self.stop_button.state(["disabled"])
        self.status_var.set("Cancelling...")
        self.log("Cancellation requested; stopping at the next checkpoint...",
                 level="warn")

    def _busy(self):
        return self.worker is not None and self.worker.is_alive()

    # --------------------------------------------------------- main pump
    def _post(self, kind, payload):
        self._queue.put((kind, payload))

    def _drain(self):
        """Main-thread pump: every Tk call for this window happens here."""
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "log":
                    text, level = payload
                    self.log(text, level=level)
                elif kind == "progress":
                    message, fraction = payload
                    self.status_var.set(message)
                    self.progress.configure(value=fraction)
                elif kind == "dialog":
                    title, message = payload
                    show_message(self, title, message)
                elif kind == "cancelled":
                    self.log("Conversion cancelled.", level="warn")
                    self.status_var.set("Cancelled")
                elif kind == "error":
                    self.log(payload, level="error")
                    self.status_var.set("Failed")
                elif kind == "done":
                    self.send_button.state(["!disabled"])
                    self.stop_button.state(["disabled"])
                    if self.status_var.get() not in ("Failed", "Cancelled"):
                        self.status_var.set("Idle")
                    self.progress.configure(value=0)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self._drain)

    def log(self, text, level=None):
        self._log.log(text, level)

    def _on_close(self):
        """Back to the home screen, asking first if a conversion is in flight."""
        if self._busy():
            if not ask_confirmation(
                    self, "Conversion running",
                    "A conversion is still running.\n\nLeave anyway?",
                    ok_label="Leave"):
                return
            # Daemon worker; its remaining work only writes files.
            self._cancel_event.set()
        self.app.show_home()


class TerrainStudio(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("Lunar Terrain Studio - Procedural DEM to Unreal Engine")

        # Every hard-coded pixel dimension below is expressed at 96 DPI and
        # multiplied by ui_scale; point-sized fonts scale via Tk's 'scaling'.
        # Both are recomputed if the window moves to a different-DPI monitor.
        self._built_dpi = self._query_dpi()
        self._dpi_job = None
        self._apply_scale_from_dpi(self._built_dpi)

        # Fit the preferred size to the actual display; laptop panels running at
        # a scaled resolution are frequently narrower than the ideal layout.
        screen_width, screen_height = self.winfo_screenwidth(), self.winfo_screenheight()
        width = min(self.px(1560), screen_width - self.px(40))
        height = min(self.px(950), screen_height - self.px(80))
        self.geometry(f"{width}x{height}+{max(0, (screen_width - width) // 2)}+"
                      f"{max(0, (screen_height - height) // 3)}")
        self.minsize(min(self.px(1100), width), min(self.px(720), height))

        # --- Runtime state ---
        self.params = terrain_core.default_params()
        self.rows_widgets = {}
        self.message_queue = queue.Queue()
        self.worker = None
        self._cancel_event = threading.Event()
        self.preview_job = None
        self.preview_result = None     # Reduced-resolution render
        self.full_result = None        # Full-resolution render (what gets exported)
        self.displayed = None          # Whichever result the canvas is showing
        self.exported_paths = {}
        self.unreal_source_path = None  # DEM chosen for "Send to Unreal"
        self.unreal_window = None       # standalone export window, reused
        self.map_viewer = None          # Reused MapViewer window
        self.map_browser = None         # Reused MapBrowser window
        self.rock_planner = None         # Reused RockPlannerStudio window
        self.sun_studio = None           # Reused SunStudio window
        self.ground_truth = None         # Reused GroundTruthStudio window
        self.mask_ids = dict(DEFAULT_MASK_IDS)   # Object-mask ids, shared by stages 2 and 5
        self.stale_axes = {'relief': True, 'surface': True, 'slope': True}
        self._surface_artists = None   # (full detail, drag detail) collections
        self._redraw_job = None
        self._preview_started = False  # First preview is deferred until the studio opens
        # Column widths in design units, None until the user drags a handle.
        self._param_width = None
        self._side_width = None
        self._sash_placed = False

        self._build_style()
        self._build_layout()
        self._apply_params_to_widgets()
        self._refresh_derived_readouts()

        self._drain_job = self.after(100, self._drain_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Destroy>", self._cancel_drain, add="+")
        self.bind("<Configure>", self._on_root_configure, add="+")

        self.log("Lunar Terrain Studio ready.")
        if not LOADER_AVAILABLE:
            self.log(f"Unreal loader unavailable: {LOADER_IMPORT_ERROR}", level="warn")

        # Start on the home screen; the preview begins when the studio is opened.
        self.show_home()
        # Open maximized (kept as a normal window so it can still be moved between
        # monitors); the centred geometry above becomes the restored size.
        self.after(0, self._maximize)

    # =====================================================================
    # LAYOUT CONSTRUCTION
    # =====================================================================

    def px(self, value):
        """Converts a 96-DPI design dimension into physical pixels."""
        return int(round(value * self.ui_scale))

    def _maximize(self):
        """Maximizes the window across platforms (kept movable, not borderless)."""
        maximize_window(self)

    def _is_maximized(self):
        try:
            return self.state() == "zoomed" or bool(self.attributes("-zoomed"))
        except tk.TclError:
            return False

    # ---------------------------------------------------------------- DPI
    def _query_dpi(self):
        """Returns the DPI of the monitor the window is currently on."""
        if sys.platform == "win32":
            try:
                user32 = ctypes.windll.user32
                user32.GetDpiForWindow.restype = ctypes.c_uint
                user32.GetDpiForWindow.argtypes = [ctypes.c_void_p]
                dpi = int(user32.GetDpiForWindow(self.winfo_id()))
                if dpi:
                    return dpi
            except Exception:
                pass
        try:
            return int(round(self.winfo_fpixels("1i")))
        except Exception:
            return 96

    def _apply_scale_from_dpi(self, dpi):
        """Sets ui_scale, the Matplotlib figure DPI, and Tk's font scaling."""
        self.ui_scale = dpi / 96.0
        self.figure_dpi = max(72, round(100 * self.ui_scale))
        try:
            self.tk.call("tk", "scaling", dpi / 72.0)
        except Exception:
            pass

    def _on_root_configure(self, event):
        """Watches for the window crossing onto a monitor with different DPI."""
        if event.widget is not self or self._dpi_job is not None:
            return
        if self._query_dpi() != self._built_dpi:
            # Debounce: dragging between monitors fires many Configure events.
            self._dpi_job = self.after(300, self._apply_dpi_change)

    def _apply_dpi_change(self):
        """
        Rebuilds the UI at the new monitor's DPI, preserving state. Tkinter does
        not reflow for per-monitor DPI on its own, so the widget tree is recreated
        at the new scale rather than left bitmap-crisp-but-mis-sized.
        """
        self._dpi_job = None
        new_dpi = self._query_dpi()
        if new_dpi == self._built_dpi:
            return
        if self._busy():
            # Never rebuild while a worker owns the log/canvas; retry shortly.
            self._dpi_job = self.after(1500, self._apply_dpi_change)
            return

        # Snapshot everything the rebuilt widgets need to restore.
        self._collect_params()
        on_studio = bool(self.studio_frame.winfo_ismapped())
        snapshot = {
            'source': self.unreal_source_path,
            'preview_size': self.preview_size_var.get(),
            'auto_preview': self.auto_preview_var.get(),
            'cmap': self.cmap_var.get(),
            'sun_az': self.sun_azimuth_var.get(),
            'sun_alt': self.sun_altitude_var.get(),
            'exag': self.exaggeration_var.get(),
            'export_name': self.export_name_var.get(),
            'dispatch': self.dispatch_var.get(),
            'save_level': self.save_level_var.get(),
            'displayed': self.displayed,
            'full_result': self.full_result,
            'preview_result': self.preview_result,
            'preview_started': self._preview_started,
        }

        self._built_dpi = new_dpi
        self._apply_scale_from_dpi(new_dpi)

        self._surface_artists = None
        self.stale_axes = {key: True for key in self.stale_axes}
        self.home_frame.destroy()
        self.studio_frame.destroy()
        # Views are rebuilt lazily at the new scale on their next visit.
        for attribute in ("map_browser", "rock_planner", "unreal_window", "sun_studio",
                          "ground_truth"):
            view = getattr(self, attribute)
            if view is not None and view.winfo_exists():
                view.destroy()
            setattr(self, attribute, None)
        self._build_layout()

        self._apply_params_to_widgets()
        self._set_unreal_source(snapshot['source'])
        self.preview_size_var.set(snapshot['preview_size'])
        self.auto_preview_var.set(snapshot['auto_preview'])
        self.cmap_var.set(snapshot['cmap'])
        self.sun_azimuth_var.set(snapshot['sun_az'])
        self.sun_altitude_var.set(snapshot['sun_alt'])
        self.exaggeration_var.set(snapshot['exag'])
        self.export_name_var.set(snapshot['export_name'])
        self.dispatch_var.set(snapshot['dispatch'])
        self.save_level_var.set(snapshot['save_level'])
        self.displayed = snapshot['displayed']
        self.full_result = snapshot['full_result']
        self.preview_result = snapshot['preview_result']
        self._preview_started = snapshot['preview_started']

        self._refresh_derived_readouts()
        if on_studio:
            self.show_studio()
            if self.displayed is not None:
                self._invalidate_axes()
                self.redraw()
        else:
            self.show_home()

        # Tk does not resize for WM_DPICHANGED, so on a higher-DPI monitor the
        # rescaled widgets can outgrow the old window. Grow to the new minimum if
        # needed; never shrink, to avoid fighting any resize Windows applied. Skip
        # when maximized (the window already fills the monitor).
        self.update_idletasks()
        min_w, min_h = self.px(1100), self.px(720)
        self.minsize(min_w, min_h)
        if not self._is_maximized():
            grow_w = max(self.winfo_width(), min_w)
            grow_h = max(self.winfo_height(), min_h)
            if grow_w > self.winfo_width() or grow_h > self.winfo_height():
                self.geometry(f"{grow_w}x{grow_h}")

        self.log(f"Rescaled UI for {new_dpi} DPI ({int(self.ui_scale * 100)}%).")

    def _build_style(self):
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Header.TLabel", font=("Segoe UI Semibold", 10))
        style.configure("Group.TLabelframe.Label", font=("Segoe UI Semibold", 9))
        style.configure("Readout.TLabel", font=("Consolas", 8))
        style.configure("Accent.TButton", font=("Segoe UI Semibold", 9))
        style.configure("HomeHint.TLabel", font=("Segoe UI", 8), foreground="#777777")

    def _build_layout(self):
        # Two top-level views swapped by show_home / show_studio.
        self._build_home()

        self.studio_frame = ttk.Frame(self)

        # Resizable split between the parameter column and the rest.
        self.studio_panes = ttk.PanedWindow(self.studio_frame, orient="horizontal")
        self.studio_panes.pack(fill="both", expand=True)

        # --- Left: scrollable parameter column ---
        left = ttk.Frame(self.studio_panes, padding=(8, 8, 4, 8))
        self._build_param_panel(left)

        # --- Right: preview + footer ---
        # Pack order matters: the fixed-height footer must claim its space before
        # the expanding preview area, or it gets squeezed off the window.
        right = ttk.Frame(self.studio_panes, padding=(4, 8, 8, 8))
        self._build_footer(right)
        self._build_preview_panel(right)

        # Only the preview side absorbs a window resize; the parameter column
        # keeps whatever width it was given.
        self.studio_panes.add(left, weight=0)
        self.studio_panes.add(right, weight=1)

        # A drag on either handle is remembered, so the widths survive a trip
        # through the home screen and the rebuild a DPI change triggers.
        for panes in (self.studio_panes, self.preview_panes):
            panes.bind("<ButtonRelease-1>", self._record_sash_positions, add="+")

        # Sash positions only take effect once the panes have a real size, which
        # is after the studio is first shown.
        self._sash_placed = False

    # ---------------------------------------------------------------- home
    def _build_home(self):
        """
        Landing screen: a banner rendered in the simulator, the workflow stages
        as cards, and the map library with the maps on disk and the current map.
        """
        colours = HOME_COLOURS
        self.home_frame = tk.Frame(self, background=colours["bg"])

        # The banner is decorative: without Pillow or the file, only the title shows.
        try:
            from PIL import Image
            self._banner_source = Image.open(HOME_BANNER).convert("RGB")
        except Exception:
            self._banner_source = None
        self._banner_image = None
        self._banner_size = None
        self._banner_job = None
        self.banner = tk.Canvas(self.home_frame, background=colours["bg"],
                                highlightthickness=0, height=self.px(320))
        self.banner.pack(fill="x")
        self.banner.bind("<Configure>", self._schedule_banner)
        self.home_frame.bind("<Configure>", self._fit_banner_height)

        body = tk.Frame(self.home_frame, background=colours["bg"])
        body.pack(fill="both", expand=True, padx=self.px(40), pady=(0, self.px(28)))
        self._home_body = body
        grid = tk.Frame(body, background=colours["bg"])
        grid.pack(fill="x")
        self._home_grid = grid
        grid.bind("<Configure>", self._fit_banner_height)
        gap = self.px(14)

        stages = [
            ("01", "Create a Terrain",
             "Generate a procedural lunar DEM with craters, erosion and surface "
             "roughness.", self.enter_generate, False),
            ("02", "Rocks & Rover Paths",
             "Scatter rocks over the drivable ground, keeping a connected network "
             "of rover corridors.", self.enter_rock_planner, True),
            ("03", "Send to Unreal",
             "Convert a map to a 16-bit heightmap and spawn the Landscape in the "
             "running editor.", self.enter_send_to_unreal, False),
            ("04", "Sun & Shadows",
             "Draw the shadow you want and set a lunar sun in Unreal to cast it.",
             self.enter_sun_studio, True),
            ("05", "Ground Truth Masks",
             "Label rocks and craters for the rover's object mask and send them "
             "to Unreal.", self.enter_ground_truth, True),
        ]
        self._home_caption(grid, "WORKFLOW", row=0, span=len(stages))
        for column, (number, title, blurb, command, optional) in enumerate(stages):
            grid.columnconfigure(column, weight=1, uniform="card")
            self._home_card(grid, number, title, blurb, command, optional).grid(
                row=1, column=column, sticky="nsew", padx=(0 if column == 0 else gap, 0))

        self._home_caption(grid, "LIBRARY", row=2, top=self.px(26), span=len(stages))
        self._home_card(grid, None, "Browse Maps",
                        "Preview every map in the project. The one you select becomes "
                        "the current map for the other stages.", self.browse_maps).grid(
            row=3, column=0, sticky="nsew")

        # Maps on disk and the current map; refreshed by show_home.
        status = tk.Frame(grid, background=colours["card"], highlightthickness=1,
                          highlightbackground=colours["border"])
        status.grid(row=3, column=1, columnspan=len(stages) - 1, sticky="nsew", padx=(gap, 0))
        tk.Label(status, text="PROJECT", font=("Segoe UI Semibold", 8),
                 foreground=colours["muted"], background=colours["card"]).pack(
            anchor="w", padx=self.px(18), pady=(self.px(16), self.px(4)))
        self.home_status_var = tk.StringVar()
        tk.Label(status, textvariable=self.home_status_var, font=("Segoe UI", 10),
                 foreground=colours["text"], background=colours["card"],
                 justify="left").pack(anchor="w", padx=self.px(18))

    def _home_caption(self, parent, text, row, top=0, span=4):
        tk.Label(parent, text=text, font=("Segoe UI Semibold", 8),
                 foreground=HOME_COLOURS["muted"], background=HOME_COLOURS["bg"]).grid(
            row=row, column=0, columnspan=span, sticky="w", pady=(top, self.px(8)))

    def _home_card(self, parent, number, title, blurb, command, optional=False):
        """A clickable home-screen card; highlights on hover and keyboard focus."""
        colours = HOME_COLOURS
        pad = self.px(18)
        card = tk.Frame(parent, background=colours["card"], highlightthickness=1,
                        highlightbackground=colours["border"], highlightcolor=colours["accent"],
                        takefocus=1, cursor="hand2")
        head = tk.Frame(card, background=colours["card"])
        head.pack(fill="x", padx=pad, pady=(pad, 0))
        if number:
            tk.Label(head, text=number, font=("Segoe UI Semibold", 18),
                     foreground=colours["accent"], background=colours["card"]).pack(side="left")
        if optional:
            tk.Label(head, text="OPTIONAL", font=("Segoe UI Semibold", 7),
                     foreground=colours["muted"], background=colours["card"]).pack(
                side="right", anchor="n")
        tk.Label(card, text=title, font=("Segoe UI Semibold", 13), anchor="w",
                 foreground=colours["text"], background=colours["card"]).pack(
            fill="x", padx=pad, pady=(self.px(4), 0))
        text = tk.Label(card, text=blurb, font=("Segoe UI", 9), anchor="nw", justify="left",
                        foreground=colours["muted"], background=colours["card"])
        text.pack(fill="both", expand=True, padx=pad, pady=(self.px(4), 0))
        tk.Label(card, text="Open  \u2192", font=("Segoe UI Semibold", 9), anchor="w",
                 foreground=colours["accent"], background=colours["card"]).pack(
            fill="x", padx=pad, pady=(self.px(12), pad))

        widgets = [card] + list(card.winfo_children()) + list(head.winfo_children())

        def paint(hover):
            fill = colours["hover"] if hover else colours["card"]
            for widget in widgets:
                widget.configure(background=fill)
            card.configure(highlightbackground=colours["accent"] if hover else colours["border"])

        def leave(event):
            # Leaving into one of the card's own widgets keeps the highlight.
            inside = card.winfo_containing(event.x_root, event.y_root)
            if inside is None or str(inside).find(str(card)) != 0:
                paint(False)

        for widget in widgets:
            widget.bind("<Enter>", lambda e: paint(True))
            widget.bind("<Leave>", leave)
            widget.bind("<Button-1>", lambda e: command())
        card.bind("<Return>", lambda e: command())
        card.bind("<space>", lambda e: command())
        card.bind("<Configure>", lambda e: text.configure(wraplength=max(e.width - 2 * pad, 80)))
        return card

    def _fit_banner_height(self, event=None):
        """Gives the banner the height the cards leave free, within sensible limits."""
        if event is not None and event.widget not in (self.home_frame, self._home_grid):
            return
        total = self.home_frame.winfo_height()
        if total < 2:
            return
        # Re-run when the cards re-wrap their text and grow.
        free = total - self._home_body.winfo_reqheight() - self.px(64)
        height = int(min(max(free, self.px(140)), total * 0.5, self.px(560)))
        if int(self.banner.cget("height")) != height:
            self.banner.configure(height=height)

    def _schedule_banner(self, _event=None):
        if self._banner_job is not None:
            self.after_cancel(self._banner_job)
        self._banner_job = self.after(50, self._draw_banner)

    def _draw_banner(self):
        """Fills the banner with the photo (cropped to cover it) and the title."""
        self._banner_job = None
        width, height = self.banner.winfo_width(), self.banner.winfo_height()
        if width < 2 or height < 2:
            return
        colours = HOME_COLOURS
        if self._banner_source is not None and self._banner_size != (width, height):
            from PIL import Image, ImageTk
            source = self._banner_source
            scale = max(width / source.width, height / source.height)
            size = (max(width, round(source.width * scale)), max(height, round(source.height * scale)))
            image = source.resize(size, Image.LANCZOS)
            left, top = (size[0] - width) // 2, int((size[1] - height) * 0.45)
            pixels = np.asarray(image.crop((left, top, left + width, top + height)), dtype=np.float32)
            # Fade the lower edge into the page background.
            fade = np.clip((np.arange(height) - 0.7 * height) / (0.3 * height), 0.0, 1.0)
            page = np.array(matplotlib.colors.to_rgb(colours["bg"])) * 255.0
            pixels = pixels * (1 - fade[:, None, None]) + page * fade[:, None, None]
            self._banner_image = ImageTk.PhotoImage(Image.fromarray(pixels.astype(np.uint8)))
            self._banner_size = (width, height)

        canvas = self.banner
        canvas.delete("all")
        if self._banner_image is not None:
            canvas.create_image(0, 0, image=self._banner_image, anchor="nw")
        x, y = self.px(40), self.px(36)
        for dx, fill in ((2, "#000000"), (0, colours["text"])):      # Drop shadow, then text
            canvas.create_text(x + dx, y + dx, text="Lunar Terrain Studio", anchor="nw",
                               fill=fill, font=("Segoe UI Semibold", 30))
        canvas.create_text(x, y + self.px(58), anchor="nw", fill="#c3ccd8",
                           font=("Segoe UI", 12),
                           text="From a lunar elevation map to an Unreal Landscape")
        canvas.create_text(width - self.px(20), self.px(16), anchor="ne", fill="#6f7a8a",
                           font=("Segoe UI", 8),
                           text="Rendered in the Moon Simulator, Unreal Engine 5")

    def refresh_home_status(self):
        """Summarises what is on disk and which map the stages would act on."""
        counts = []
        for label, folder in (("input_maps", INPUT_MAPS_DIR),
                              ("outputs", OUTPUT_ROOT)):
            if os.path.isdir(folder):
                counts.append(f"{len(scan_dem_folder(folder))} in {label}/")
            else:
                counts.append(f"no {label}/ yet")

        current = (self.exported_paths.get('elevation_npy')
                   or self.unreal_source_path)
        in_hand = os.path.basename(current) if current else "none yet"
        self.home_status_var.set(
            f"Maps on disk: {', '.join(counts)}\n"
            f"Current map: {in_hand}")

    BASE_TITLE = "Lunar Terrain Studio"

    def _all_views(self):
        """Every frame that can occupy the window, home and studio included."""
        return [self.home_frame, self.studio_frame, self.map_browser,
                self.rock_planner, self.unreal_window, self.sun_studio, self.ground_truth]

    def show_view(self, view, title=None):
        """
        Shows `view` in the window and hides the others.
        """
        for other in self._all_views():
            if other is not None and other is not view and other.winfo_exists():
                other.pack_forget()
        view.pack(fill="both", expand=True)
        self.title(f"{self.BASE_TITLE} - {title}" if title else self.BASE_TITLE)

    def show_home(self):
        """Switches to the landing screen, refreshing what it reports."""
        self.refresh_home_status()
        self.show_view(self.home_frame)

    # Column widths in 96-DPI design pixels, used until the user drags a handle.
    DEFAULT_PARAM_WIDTH = 300
    DEFAULT_SIDE_WIDTH = 290
    MIN_COLUMN_WIDTH = 140

    def show_studio(self):
        """Switches to the main studio, starting the preview on first entry."""
        self.show_view(self.studio_frame, "Generate Terrain")
        if not self._sash_placed:
            self.after_idle(self._apply_sash_positions)
        if not self._preview_started:
            self._preview_started = True
            self.request_preview()

    def _apply_sash_positions(self, attempt=0):
        """
        Places the two drag handles, on first show and after a DPI rebuild.

        Tk may snap a sash back to the pane's requested width while the layout
        settles, so the placement is verified and retried.
        """
        if not self.studio_frame.winfo_exists():
            return
        self.update_idletasks()

        # Widths are held in 96-DPI design units, so a drag made on one monitor
        # still means the same thing after a rebuild at another monitor's DPI.
        param_width = self.px(self._param_width or self.DEFAULT_PARAM_WIDTH)
        side_width = self.px(self._side_width or self.DEFAULT_SIDE_WIDTH)
        minimum = self.px(self.MIN_COLUMN_WIDTH)

        settled = True

        total = self.studio_panes.winfo_width()
        if total > minimum * 2:
            target = max(minimum, min(param_width, total - minimum))
            self.studio_panes.sashpos(0, target)
            self.update_idletasks()
            settled &= abs(self.studio_panes.sashpos(0) - target) <= 2
        else:
            settled = False

        # The preview handle is positioned from the left, so the readout column
        # keeps its width only if the sash follows the pane's own width.
        span = self.preview_panes.winfo_width()
        if span > minimum * 2:
            target = max(minimum, span - max(minimum, side_width))
            self.preview_panes.sashpos(0, target)
            self.update_idletasks()
            settled &= abs(self.preview_panes.sashpos(0) - target) <= 2
        else:
            settled = False

        if not settled and attempt < 6:
            self.after(60, lambda: self._apply_sash_positions(attempt + 1))
            return

        self._sash_placed = True

    def _rewrap_side_labels(self, event):
        """Retunes the export panel's text to the width the drag handle gave it."""
        wrap = max(self.px(150), event.width - self.px(40))
        for label in self._wrapping_labels:
            if label.winfo_exists():
                label.configure(wraplength=wrap)

    def _record_sash_positions(self, _event=None):
        """Remembers the widths a drag produced, back in design units."""
        scale = self.ui_scale or 1.0
        try:
            self._param_width = self.studio_panes.sashpos(0) / scale
            self._side_width = (self.preview_panes.winfo_width()
                                - self.preview_panes.sashpos(0)) / scale
        except tk.TclError:  # a pane was destroyed mid-drag
            pass

    def enter_generate(self):
        self.show_studio()

    def enter_send_to_unreal(self):
        """Opens the standalone export window, seeded with the map in hand."""
        source = self.exported_paths.get('elevation_npy') or self.unreal_source_path
        if self.unreal_window is None or not self.unreal_window.winfo_exists():
            self.unreal_window = UnrealDispatchWindow(
                self, source_path=source, figure_dpi=self.figure_dpi,
                resolution=self.params.get('RESOLUTION_M_PER_PX', 1.0))
        elif source:
            self.unreal_window.set_source(source)
        self.show_view(self.unreal_window, "Send a Map to Unreal")

    def enter_rock_planner(self):
        """Opens the rock-field planner, seeded with the last generated / chosen DEM."""
        source = None
        if self.exported_paths.get('elevation_npy'):
            source = self.exported_paths['elevation_npy']
        elif self.unreal_source_path:
            source = self.unreal_source_path
        if self.rock_planner is None or not self.rock_planner.winfo_exists():
            self.rock_planner = RockPlannerStudio(
                self, figure_dpi=self.figure_dpi, source_path=source,
                resolution=self.params.get('RESOLUTION_M_PER_PX', 1.0))
        elif source:
            self.rock_planner.load_dem(source)
        self.rock_planner.refresh_mask_ids()
        self.show_view(self.rock_planner, "Rock Field & Rover Paths")

    def enter_ground_truth(self):
        """Opens the object-mask labels view, seeded with the last generated / chosen DEM."""
        source = self.exported_paths.get('elevation_npy') or self.unreal_source_path
        if self.ground_truth is None or not self.ground_truth.winfo_exists():
            self.ground_truth = GroundTruthStudio(
                self, figure_dpi=self.figure_dpi, source_path=source,
                resolution=self.params.get('RESOLUTION_M_PER_PX', 1.0))
        elif source and source != self.ground_truth.dem_path:
            self.ground_truth.load_dem(source)
        self.show_view(self.ground_truth, "Ground Truth Masks")

    def enter_sun_studio(self):
        """Opens the sun & shadows view, seeded with the last generated / chosen DEM."""
        source = self.exported_paths.get('elevation_npy') or self.unreal_source_path
        if self.sun_studio is None or not self.sun_studio.winfo_exists():
            self.sun_studio = SunStudio(
                self, figure_dpi=self.figure_dpi, source_path=source,
                resolution=self.params.get('RESOLUTION_M_PER_PX', 1.0))
        elif source and source != self.sun_studio.dem_path:
            self.sun_studio.load_dem(source)
        self.show_view(self.sun_studio, "Sun & Shadows")

    def browse_maps(self):
        """Opens the map browser, seeded with whatever map is already in hand."""
        source = (self.exported_paths.get('elevation_npy')
                  or self.unreal_source_path)
        if self.map_browser is None or not self.map_browser.winfo_exists():
            self.map_browser = MapBrowser(self, figure_dpi=self.figure_dpi,
                                          initial_path=source)
        else:
            self.map_browser.refresh()
            if source:
                self.map_browser.select_path(source)
        self.show_view(self.map_browser, "Browse Maps")


    # ---------------------------------------------------------------- left
    def _scrollable_column(self, parent, width=None):
        """
        A vertically scrolling column; returns the frame to fill.

        The mouse wheel is bound only while the pointer is over the column, so
        it does not scroll the column from elsewhere in the window.
        """
        container = ttk.Frame(parent)
        container.pack(fill="both", expand=True)

        canvas = tk.Canvas(container, highlightthickness=0,
                           background=self.cget("background"),
                           **({"width": width} if width else {}))
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)

        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def on_wheel(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")
            return "break"

        def enter(_event):
            canvas.bind_all("<MouseWheel>", on_wheel)

        def leave(_event):
            # Leave also fires when moving onto a child widget; tkinter does not
            # reliably expose event.detail, so check the pointer position.
            x, y = canvas.winfo_pointerxy()
            left, top = canvas.winfo_rootx(), canvas.winfo_rooty()
            inside = (left <= x < left + canvas.winfo_width()
                      and top <= y < top + canvas.winfo_height())
            if not inside:
                canvas.unbind_all("<MouseWheel>")

        canvas.bind("<Enter>", enter)
        canvas.bind("<Leave>", leave)
        return inner

    def _build_param_panel(self, parent):
        top_row = ttk.Frame(parent)
        top_row.pack(fill="x", pady=(0, 6))
        ttk.Button(top_row, text="⌂ Home", width=9,
                   command=self.show_home).pack(side="left")
        ttk.Label(top_row, text="Simulation Parameters",
                  style="Header.TLabel").pack(side="left", padx=(8, 0))

        # Preset bar
        presets = ttk.Frame(parent)
        presets.pack(fill="x", pady=(6, 8))
        self.preset_var = tk.StringVar()
        self.preset_combo = ttk.Combobox(presets, textvariable=self.preset_var,
                                         state="readonly", width=26)
        self.preset_combo.pack(side="left", fill="x", expand=True)
        self.preset_combo.bind("<<ComboboxSelected>>", self._on_preset_selected)
        ttk.Button(presets, text="Save as...", width=10,
                   command=self.save_config_as).pack(side="left", padx=(4, 0))
        self._refresh_preset_list()

        # Preview controls are pinned outside the scroll area: they drive the
        # main loop of the tool, so they must stay reachable at any scroll offset.
        preview_group = ttk.Labelframe(parent, text="Preview", padding=(8, 4, 8, 8),
                                       style="Group.TLabelframe")
        preview_group.pack(fill="x", pady=(0, 8))

        res_row = ttk.Frame(preview_group)
        res_row.pack(fill="x", pady=(0, 4))
        ttk.Label(res_row, text="Preview resolution", font=("Segoe UI", 8)).pack(side="left")
        self.preview_size_var = tk.StringVar(value="256")
        preview_combo = ttk.Combobox(res_row, textvariable=self.preview_size_var,
                                     values=PREVIEW_SIZES, state="readonly", width=6)
        preview_combo.pack(side="right")
        preview_combo.bind("<<ComboboxSelected>>", lambda e: self.request_preview())
        Tooltip(preview_combo,
                "Longest edge of the preview render. Crater radii and erosion sigma are\n"
                "fractions of the map size, so a downscaled preview keeps the same\n"
                "morphology as the full-resolution export.")

        self.auto_preview_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(preview_group, text="Auto-update on change",
                        variable=self.auto_preview_var).pack(anchor="w")

        self.preview_button = ttk.Button(preview_group, text="Update Preview",
                                         command=self.request_preview)
        self.preview_button.pack(fill="x", pady=(6, 0))

        # Scroll container
        inner = self._scrollable_column(parent, width=self.px(336))

        # Parameter groups
        for group_name, specs in PARAM_GROUPS:
            group = ttk.Labelframe(inner, text=group_name, padding=(8, 4, 8, 8),
                                   style="Group.TLabelframe")
            group.pack(fill="x", pady=(0, 8), padx=(0, 4))
            for spec in specs:
                self.rows_widgets[spec[0]] = ParamRow(group, spec, self.on_param_change)

        # Seed control
        seed_group = ttk.Labelframe(inner, text="Determinism", padding=(8, 4, 8, 8),
                                    style="Group.TLabelframe")
        seed_group.pack(fill="x", pady=(0, 8), padx=(0, 4))

        seed_row = ttk.Frame(seed_group)
        seed_row.pack(fill="x")
        ttk.Label(seed_row, text="Random seed", font=("Segoe UI", 8)).pack(side="left")
        self.seed_var = tk.StringVar(value="")
        seed_entry = ttk.Entry(seed_row, textvariable=self.seed_var, width=11,
                               justify="right", font=("Consolas", 9))
        seed_entry.pack(side="right")
        seed_entry.bind("<Return>", lambda e: self.on_param_change())
        seed_entry.bind("<FocusOut>", lambda e: self.on_param_change())
        Tooltip(seed_entry,
                "Locks the pseudo-random stream so a preview and its full-resolution\n"
                "export share the same layout. Left blank, a seed is rolled for you\n"
                "and written here so the result stays reproducible.")
        ttk.Button(seed_group, text="Roll new seed", command=self.roll_seed).pack(
            fill="x", pady=(6, 0))

        # Lighting / display
        light_group = ttk.Labelframe(inner, text="Display & Sun", padding=(8, 4, 8, 8),
                                     style="Group.TLabelframe")
        light_group.pack(fill="x", pady=(0, 8), padx=(0, 4))

        cmap_row = ttk.Frame(light_group)
        cmap_row.pack(fill="x", pady=(0, 4))
        ttk.Label(cmap_row, text="Colormap", font=("Segoe UI", 8)).pack(side="left")
        self.cmap_var = tk.StringVar(value="gray")
        cmap_combo = ttk.Combobox(cmap_row, textvariable=self.cmap_var, values=COLORMAPS,
                                  state="readonly", width=11)
        cmap_combo.pack(side="right")
        cmap_combo.bind("<<ComboboxSelected>>", lambda e: self.redraw(force=True))

        self.sun_azimuth_var = self._add_display_slider(
            light_group, "Sun azimuth (deg)", 0, 360, 315,
            "Compass direction of the sun used to shade the preview.")
        self.sun_altitude_var = self._add_display_slider(
            light_group, "Sun elevation (deg)", 1, 89, 25,
            "Sun height above the horizon. Low angles exaggerate shadows, matching\n"
            "the grazing illumination of the lunar polar regions.")
        self.exaggeration_var = self._add_display_slider(
            light_group, "Shading exaggeration", 1, 100, 12,
            "Relief multiplier used when shading the hillshade. These terrains are\n"
            "metres of relief over kilometres of ground, so unexaggerated lighting\n"
            "looks flat. The 3D tab fits its own exaggeration and reports it.\n"
            "Exported data is never exaggerated.")

    def _add_display_slider(self, parent, label, minimum, maximum, initial, tooltip):
        """Creates a display-only slider (does not invalidate the generated terrain)."""
        frame = ttk.Frame(parent)
        frame.pack(fill="x", pady=(2, 2))
        frame.columnconfigure(0, weight=1)

        text = ttk.Label(frame, text=label, font=("Segoe UI", 8))
        text.grid(row=0, column=0, sticky="w")
        Tooltip(text, tooltip)

        value_var = tk.DoubleVar(value=initial)
        readout = ttk.Label(frame, text=f"{initial:g}", font=("Consolas", 8), width=6,
                            anchor="e")
        readout.grid(row=0, column=1, sticky="e")

        def _moved(_raw):
            readout.configure(text=f"{value_var.get():.0f}")
            # Debounced: a slider drag emits events far faster than the 3D tab
            # can redraw, and queueing them makes the whole window feel stuck.
            self._schedule_redraw()

        slider = ttk.Scale(frame, from_=minimum, to=maximum, variable=value_var,
                           command=_moved, orient="horizontal")
        slider.grid(row=1, column=0, columnspan=2, sticky="ew")
        return value_var

    # --------------------------------------------------------------- right
    def _build_preview_panel(self, parent):
        top = ttk.Frame(parent)
        top.pack(side="top", fill="both", expand=True)

        # Resizable split between preview and side column; window growth goes
        # to the preview.
        self.preview_panes = ttk.PanedWindow(top, orient="horizontal")
        self.preview_panes.pack(fill="both", expand=True)

        notebook_pane = ttk.Frame(self.preview_panes)
        side_pane = ttk.Frame(self.preview_panes, padding=(8, 0, 0, 0))
        self.preview_panes.add(notebook_pane, weight=1)
        self.preview_panes.add(side_pane, weight=0)

        # Readouts plus the export panel are taller than most windows, so the
        # column scrolls rather than dropping whatever does not fit.
        side = self._scrollable_column(side_pane)

        # Text in this column wraps to whatever width the drag handle gives it,
        # so widening the column makes the panel shorter instead of just wider.
        self._wrapping_labels = []
        side_pane.bind("<Configure>", self._rewrap_side_labels)

        # Figure notebook
        self.notebook = ttk.Notebook(notebook_pane)
        self.notebook.pack(fill="both", expand=True)
        self.notebook.bind("<<NotebookTabChanged>>", lambda e: self.redraw())

        self.figures = {}
        self.canvases = {}
        for key, title in (("relief", "Shaded Relief"),
                           ("surface", "3D Surface"),
                           ("slope", "Slope Analysis")):
            tab = ttk.Frame(self.notebook)
            self.notebook.add(tab, text=title)
            # Constrained layout wastes most of the frame on a 3D axes, so the
            # surface tab manages its own margins instead.
            figure = Figure(figsize=(7.4, 6.0), dpi=self.figure_dpi,
                            layout=None if key == "surface" else "constrained")
            canvas = FigureCanvasTkAgg(figure, master=tab)
            canvas.get_tk_widget().pack(fill="both", expand=True)
            NavigationToolbar2Tk(canvas, tab).update()
            self.figures[key] = figure
            self.canvases[key] = canvas

        # Level-of-detail swapping while the 3D view is being rotated or zoomed
        surface_canvas = self.canvases['surface']
        surface_canvas.mpl_connect("button_press_event", self._on_surface_press)
        surface_canvas.mpl_connect("button_release_event", self._on_surface_release)

        # --- Export & Unreal ---
        # Packed first, against the bottom, so the action buttons stay reachable
        # no matter how little vertical room the readouts leave.
        export_group = ttk.Labelframe(side, text="Export & Unreal", padding=(8, 4, 8, 8),
                                      style="Group.TLabelframe")
        export_group.pack(side="bottom", fill="x", pady=(4, 0))
        wrap = self.px(250)

        ttk.Label(export_group, text="Base filename", font=("Segoe UI", 8)).pack(anchor="w")
        self.export_name_var = tk.StringVar(value="procedural_lunar_terrain")
        ttk.Entry(export_group, textvariable=self.export_name_var,
                  font=("Consolas", 9)).pack(fill="x", pady=(2, 6))

        self._wrapping_labels.append(ttk.Label(
            export_group, wraplength=wrap, justify="left", font=("Segoe UI", 8),
            text="Step 1 - generate the full-resolution DEM."))
        self._wrapping_labels[-1].pack(anchor="w", pady=(0, 2))

        # Lists the files that will be written, derived from the base filename.
        self.output_preview_var = tk.StringVar()
        output_preview = ttk.Label(export_group, textvariable=self.output_preview_var,
                                   justify="left", style="Readout.TLabel")
        output_preview.pack(anchor="w", pady=(0, 4))
        Tooltip(output_preview,
                "The files Step 1 writes, derived from the base filename above.\n"
                "The .csv pair depends on EXPORT_CSV in the loaded preset.")
        self.export_name_var.trace_add("write", self._refresh_output_preview)
        self._refresh_output_preview()

        self.generate_button = ttk.Button(export_group, text="Generate Full Resolution",
                                          style="Accent.TButton",
                                          command=self.run_full_generation)
        self.generate_button.pack(fill="x")

        # Cancels whichever long job is running (generation or Unreal export).
        # Disabled until one starts.
        self.stop_button = ttk.Button(export_group, text="Stop",
                                      command=self.stop_generation)
        self.stop_button.pack(fill="x", pady=(4, 0))
        self.stop_button.state(["disabled"])
        Tooltip(self.stop_button,
                "Stops the running generation or Unreal export at its next checkpoint.\n"
                "Partial output files may remain and should be regenerated.")

        ttk.Separator(export_group, orient="horizontal").pack(fill="x", pady=8)

        self._wrapping_labels.append(ttk.Label(
            export_group, wraplength=wrap, justify="left", font=("Segoe UI", 8),
            text="Step 2 - convert a DEM to a .r16 heightmap and spawn "
                 "the Landscape actor."))
        self._wrapping_labels[-1].pack(anchor="w", pady=(0, 2))

        # Expected input; companion rasters are filtered or flagged elsewhere.
        self._wrapping_labels.append(ttk.Label(
            export_group, wraplength=wrap, justify="left",
            style="HomeHint.TLabel",
            text="Needs an elevation map in metres, usually Step 1's "
                 "_elevationMap.npy. Accepts .npy, .csv, .txt, .tif, .tiff."))
        self._wrapping_labels[-1].pack(anchor="w", pady=(0, 5))

        ttk.Label(export_group, text="Source map", font=("Segoe UI", 8)).pack(anchor="w")
        source_row = ttk.Frame(export_group)
        source_row.pack(fill="x", pady=(2, 2))
        self.unreal_source_var = tk.StringVar(value="(none - generate or browse)")
        source_entry = ttk.Entry(source_row, textvariable=self.unreal_source_var,
                                 state="readonly", font=("Consolas", 8))
        source_entry.pack(side="left", fill="x", expand=True)
        self.unreal_browse_button = ttk.Button(
            source_row, text="Browse ▾", width=10,
            command=self.open_unreal_source_menu)
        self.unreal_browse_button.pack(side="left", padx=(4, 0))
        Tooltip(self.unreal_browse_button,
                "Lists the DEMs already in input_maps/ and outputs/, or opens\n"
                "the file picker for a map kept anywhere else.")
        Tooltip(source_entry,
                "DEM sent to Unreal: the last full-resolution generation, or any\n"
                ".npy / .csv / .txt / .tif you pick (which also previews it).\n"
                "Resolution comes from the Map Geometry field (GeoTIFFs use their\n"
                "embedded scale).")

        # Warns when a pick is one of the companion rasters; empty otherwise.
        self.unreal_source_note_var = tk.StringVar()
        self.unreal_source_note = ttk.Label(
            export_group, textvariable=self.unreal_source_note_var,
            wraplength=wrap, justify="left", style="Readout.TLabel",
            foreground="#b8860b")
        self._wrapping_labels.append(self.unreal_source_note)
        self.unreal_source_note.pack(anchor="w", pady=(0, 4))

        self.dispatch_var = tk.BooleanVar(value=True)
        dispatch_check = ttk.Checkbutton(
            export_group, text="Dispatch to running Unreal Editor",
            variable=self.dispatch_var)
        dispatch_check.pack(anchor="w")
        Tooltip(dispatch_check,
                "When enabled, the studio broadcasts for a running Unreal Editor with\n"
                "Remote Execution enabled, spawns the Landscape actor and applies the\n"
                "heightmap automatically.\n"
                "When disabled, only the .r16 / .json / .txt files are written.")
        self.save_level_var = tk.BooleanVar(value=True)
        save_check = ttk.Checkbutton(export_group, text="Save the level afterwards",
                                     variable=self.save_level_var)
        save_check.pack(anchor="w")
        Tooltip(save_check, 'Saves the open Unreal level once the Landscape is spawned and its\nheightmap applied.')

        self.unreal_button = ttk.Button(export_group, text="Send to Unreal",
                                        style="Accent.TButton",
                                        command=self.run_unreal_dispatch)
        self.unreal_button.pack(fill="x", pady=(4, 0))
        if not LOADER_AVAILABLE:
            self.unreal_button.state(["disabled"])

        ttk.Button(export_group, text="Open output folder",
                   command=self.open_output_folder).pack(fill="x", pady=(8, 0))

        # --- Readouts (fill whatever space is left above the actions) ---
        ttk.Label(side, text="Surface Statistics", style="Header.TLabel").pack(anchor="w")
        self.stats_text = tk.Text(side, height=16, width=34, font=("Consolas", 8),
                                  relief="solid", borderwidth=1, wrap="none",
                                  background="#fbfbfb")
        self.stats_text.pack(fill="x", pady=(4, 10))
        self.stats_text.configure(state="disabled")

        ttk.Label(side, text="Unreal Landscape Topology",
                  style="Header.TLabel").pack(anchor="w")
        self.topology_text = tk.Text(side, height=8, width=34, font=("Consolas", 8),
                                     relief="solid", borderwidth=1, wrap="none",
                                     background="#fbfbfb")
        self.topology_text.pack(fill="x", pady=(4, 10))
        self.topology_text.configure(state="disabled")

    def _build_footer(self, parent):
        footer = ttk.Frame(parent, padding=(0, 8, 0, 0))
        footer.pack(side="bottom", fill="x")

        # Status strip only: the actions live beside the parameters and the
        # outputs they belong to (preview controls left, export controls right).
        status_row = ttk.Frame(footer)
        status_row.pack(fill="x")

        ttk.Label(status_row, text="Activity log", style="Header.TLabel").pack(side="left")

        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(status_row, textvariable=self.status_var,
                  font=("Segoe UI", 8)).pack(side="right")

        self.progress = ttk.Progressbar(footer, mode="determinate", maximum=1.0)
        self.progress.pack(fill="x", pady=(6, 4))

        self.log_text = tk.Text(footer, height=8, font=("Consolas", 8), wrap="word",
                                relief="solid", borderwidth=1, background="#1e1e1e",
                                foreground="#d4d4d4", insertbackground="#d4d4d4")
        self.log_text.pack(fill="x")
        self.log_text.tag_configure("warn", foreground="#e6c07b")
        self.log_text.tag_configure("error", foreground="#e06c75")
        self.log_text.tag_configure("ok", foreground="#98c379")
        self.log_text.configure(state="disabled")

    # =====================================================================
    # PARAMETER PLUMBING
    # =====================================================================

    def _refresh_output_preview(self, *_trace_args):
        """Lists the filenames Step 1 will write, under the base filename field."""
        base_name = self.export_name_var.get().strip() or "procedural_lunar_terrain"
        files = planned_output_files(base_name, self.params.get('EXPORT_CSV', True))
        folder = f"outputs/{base_name}/"
        self.output_preview_var.set(
            folder + "\n" + "\n".join(f"  {name}" for name, _ in files))

    def _apply_params_to_widgets(self):
        for key, row in self.rows_widgets.items():
            row.set(self.params[key])
        seed = self.params.get('RANDOM_SEED')
        self.seed_var.set("" if seed is None else str(seed))
        # A preset can switch EXPORT_CSV, which changes the file list.
        self._refresh_output_preview()

    def _collect_params(self):
        """Reads every widget back into the parameter dictionary."""
        for key, row in self.rows_widgets.items():
            self.params[key] = row.get()

        seed_text = self.seed_var.get().strip()
        try:
            self.params['RANDOM_SEED'] = int(seed_text) if seed_text else None
        except ValueError:
            self.params['RANDOM_SEED'] = None
            self.seed_var.set("")

        self.params = terrain_core.coerce_params(self.params)
        return self.params

    def on_param_change(self):
        """Invalidates the current render and schedules a debounced preview."""
        self._collect_params()
        self._refresh_derived_readouts()
        if self.auto_preview_var.get():
            self.request_preview(debounce_ms=450)

    def roll_seed(self):
        self.seed_var.set(str(int(np.random.randint(0, 2 ** 31 - 1))))
        self.on_param_change()

    def _ensure_seed(self):
        """
        Guarantees an explicit seed so the preview and the full-resolution export
        describe the same surface. A blank field is filled with a rolled value.
        """
        if self.params.get('RANDOM_SEED') is None:
            seed = int(np.random.randint(0, 2 ** 31 - 1))
            self.params['RANDOM_SEED'] = seed
            self.seed_var.set(str(seed))
        return self.params['RANDOM_SEED']

    # =====================================================================
    # PRESETS
    # =====================================================================

    def _refresh_preset_list(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        files = sorted(f for f in os.listdir(CONFIG_DIR) if f.endswith((".yaml", ".yml")))
        self.preset_combo.configure(values=files + ["Browse..."])
        if files and not self.preset_var.get():
            self.preset_var.set(files[0])

    def _on_preset_selected(self, _event=None):
        choice = self.preset_var.get()
        if choice == "Browse...":
            path = filedialog.askopenfilename(
                title="Load terrain configuration", initialdir=CONFIG_DIR,
                filetypes=[("YAML configuration", "*.yaml *.yml"), ("All files", "*.*")])
            if not path:
                self._refresh_preset_list()
                return
        else:
            path = os.path.join(CONFIG_DIR, choice)
        self.load_config(path)

    def load_config(self, path):
        try:
            self.params = terrain_core.load_config(path)
        except Exception as exc:
            self.log(f"Failed to load '{path}': {exc}", level="error")
            return
        self._apply_params_to_widgets()
        self._refresh_derived_readouts()
        self.log(f"Loaded configuration: {os.path.basename(path)}", level="ok")
        self.request_preview()

    def save_config_as(self):
        self._collect_params()
        path = filedialog.asksaveasfilename(
            title="Save terrain configuration", initialdir=CONFIG_DIR,
            defaultextension=".yaml", initialfile="my_lunar_terrain.yaml",
            filetypes=[("YAML configuration", "*.yaml"), ("All files", "*.*")])
        if not path:
            return
        try:
            terrain_core.save_config(path, self.params)
        except Exception as exc:
            self.log(f"Failed to save configuration: {exc}", level="error")
            return
        self.log(f"Saved configuration: {path}", level="ok")
        self._refresh_preset_list()
        if os.path.dirname(os.path.abspath(path)) == os.path.abspath(CONFIG_DIR):
            self.preset_var.set(os.path.basename(path))

    # =====================================================================
    # DERIVED READOUTS (no generation required)
    # =====================================================================

    def _refresh_derived_readouts(self):
        params = self.params
        rows, cols = params['ROWS'], params['COLS']
        resolution = params['RESOLUTION_M_PER_PX']
        craters = terrain_core.full_crater_count(params)

        lines = [
            f"DEM grid        {cols} x {rows} px",
            f"Ground extent   {cols * resolution:,.0f} x {rows * resolution:,.0f} m",
            f"Resolution      {resolution:g} m/px",
            f"Target craters  {craters}",
            f"Crater radii    {max(rows, cols) * params['CRATER_MIN_RADIUS_PCT'] * resolution:,.1f}"
            f" - {max(rows, cols) * params['CRATER_MAX_RADIUS_PCT'] * resolution:,.1f} m",
            f"Erosion sigma   {max(1.0, max(rows, cols) / params['EROSION_BLUR_DIVISOR']):,.1f} px",
        ]

        if self.displayed is not None:
            elevation = self.displayed['elevation']
            scale = max(rows, cols) / max(elevation.shape)
            effective_res = resolution * scale
            gradient_y, gradient_x = np.gradient(elevation, effective_res, effective_res)
            slope_degrees = np.degrees(np.arctan(np.hypot(gradient_x, gradient_y)))
            source = "full resolution" if self.displayed is self.full_result else "preview"
            lines += [
                "",
                f"-- rendered ({source}) --",
                f"Min elevation   {np.min(elevation):,.3f} m",
                f"Max elevation   {np.max(elevation):,.3f} m",
                f"Mean elevation  {np.mean(elevation):,.3f} m",
                f"Total relief    {np.ptp(elevation):,.3f} m",
                f"Craters placed  {self.displayed['crater_count']}",
                f"Mean slope      {np.mean(slope_degrees):,.2f} deg",
                f"P99 slope       {np.percentile(slope_degrees, 99):,.2f} deg",
                f"Max slope       {np.max(slope_degrees):,.2f} deg",
            ]

        self._set_text(self.stats_text, "\n".join(lines))
        self._refresh_topology_readout()

    def _refresh_topology_readout(self):
        """Mirrors the loader's component solver so the UI can warn before export."""
        rows, cols = self.params['ROWS'], self.params['COLS']
        resolution = self.params['RESOLUTION_M_PER_PX']

        if not LOADER_AVAILABLE:
            self._set_text(self.topology_text,
                           "Unreal loader modules unavailable.\n"
                           f"{LOADER_IMPORT_ERROR}")
            return

        config = terrain_files_generator.calculate_optimal_landscape_config(cols, rows)
        if config is None:
            self._set_text(
                self.topology_text,
                "DEM exceeds the 50x50 component\n"
                "ceiling of a single Landscape.\n\n"
                "Reduce rows/columns or split the\n"
                "map across World Partition tiles.")
            return

        comp_x, comp_y, quads, sections, final_width, final_height = config
        padded = (final_width - cols) + (final_height - rows)
        vertices = final_width * final_height
        self._set_text(self.topology_text, "\n".join([
            f"Section size    {quads} quads",
            f"Sections/comp   {sections}",
            f"Components      {comp_x} x {comp_y}",
            f"Landscape verts {final_width} x {final_height}",
            f"Total vertices  {vertices:,}",
            f"Edge padding    {padded} px",
            f"World size      {final_width * resolution:,.0f} x"
            f" {final_height * resolution:,.0f} m",
            f"XY scale        {resolution * 100:g} (cm/px)",
        ]))

    def _set_text(self, widget, content):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", content)
        widget.configure(state="disabled")

    # =====================================================================
    # BACKGROUND WORK
    # =====================================================================

    def _busy(self):
        return self.worker is not None and self.worker.is_alive()

    def _start_worker(self, target, label, cancellable=False):
        if self._busy():
            self.log("A job is already running; wait for it to finish.", level="warn")
            return False
        self.status_var.set(label)
        self._cancel_event.clear()
        for button in (self.preview_button, self.generate_button, self.unreal_button):
            button.state(["disabled"])
        # The Stop button only arms for long, cancellable jobs (generation and
        # the Unreal export); previews are fast and relaunch on their own.
        self.stop_button.state(["!disabled"] if cancellable else ["disabled"])
        self.worker = threading.Thread(target=self._worker_wrapper, args=(target,),
                                       daemon=True)
        self.worker.start()
        return True

    def _worker_wrapper(self, target):
        try:
            target()
        except terrain_core.TerrainCancelled:
            self.message_queue.put(("cancelled", None))
        except Exception:
            self.message_queue.put(("error", traceback.format_exc()))
        finally:
            self.message_queue.put(("done", None))

    def stop_generation(self):
        """Requests cooperative cancellation of the running job."""
        if not self._busy():
            return
        self._cancel_event.set()
        self.stop_button.state(["disabled"])
        self.status_var.set("Cancelling...")
        self.log("Cancellation requested; stopping at the next checkpoint...",
                 level="warn")

    def _post(self, kind, payload):
        self.message_queue.put((kind, payload))

    def _progress_hook(self, message, fraction):
        self._post("progress", (message, fraction))

    def _drain_queue(self):
        """Main-thread pump for worker messages. All Tk/Matplotlib work happens here."""
        try:
            while True:
                kind, payload = self.message_queue.get_nowait()

                if kind == "log":
                    text, level = payload
                    self.log(text, level=level)
                elif kind == "progress":
                    message, fraction = payload
                    self.status_var.set(message)
                    self.progress.configure(value=fraction)
                elif kind == "preview":
                    self.preview_result = payload
                    self.displayed = payload
                    self._invalidate_axes()
                    self.redraw()
                    self._refresh_derived_readouts()
                elif kind == "full":
                    self.full_result = payload
                    self.displayed = payload
                    self._invalidate_axes()
                    self.redraw()
                    self._refresh_derived_readouts()
                    # Newly generated map becomes the default Unreal source.
                    if 'elevation_npy' in self.exported_paths:
                        self._set_unreal_source(self.exported_paths['elevation_npy'])
                elif kind == "error":
                    self.log(payload, level="error")
                    self.status_var.set("Failed")
                elif kind == "dialog":
                    title, message = payload
                    show_message(self, title, message)
                elif kind == "cancelled":
                    self.log("Job cancelled.", level="warn")
                    self.status_var.set("Cancelled")
                elif kind == "done":
                    for button in (self.preview_button, self.generate_button):
                        button.state(["!disabled"])
                    if LOADER_AVAILABLE:
                        self.unreal_button.state(["!disabled"])
                    self.stop_button.state(["disabled"])
                    if self.status_var.get() not in ("Failed", "Cancelled"):
                        self.status_var.set("Idle")
                    self.progress.configure(value=0)
        except queue.Empty:
            pass
        # Keep the id so _cancel_drain can stop the pending timer on destroy;
        # otherwise Tk reports "invalid command name" on exit.
        if self.winfo_exists():
            self._drain_job = self.after(100, self._drain_queue)

    def _cancel_drain(self, event):
        """Stops the queue pump when the root window goes away."""
        if event.widget is not self:
            return  # child widgets bubble their own Destroy events
        if self._drain_job is not None:
            try:
                self.after_cancel(self._drain_job)
            except tk.TclError:
                pass
            self._drain_job = None

    def log(self, text, level=None):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text.rstrip() + "\n", level or ())
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _log_from_worker(self, text, level=None):
        self._post("log", (text, level))

    def _dialog_from_worker(self, title, message):
        """Requests an info dialog on the main thread from a worker."""
        self._post("dialog", (title, message))

    # =====================================================================
    # ACTIONS
    # =====================================================================

    def request_preview(self, debounce_ms=0):
        """Schedules a preview render, coalescing rapid slider movements."""
        if self.preview_job:
            self.after_cancel(self.preview_job)
            self.preview_job = None
        if debounce_ms:
            self.preview_job = self.after(debounce_ms, self._launch_preview)
        else:
            self._launch_preview()

    def _launch_preview(self):
        self.preview_job = None
        self._collect_params()
        seed = self._ensure_seed()

        # Preserve the map aspect ratio while capping the longest edge.
        target = int(self.preview_size_var.get())
        rows, cols = self.params['ROWS'], self.params['COLS']
        longest = max(rows, cols)
        scale = min(1.0, target / longest)
        preview_rows = max(32, int(round(rows * scale)))
        preview_cols = max(32, int(round(cols * scale)))
        crater_count = terrain_core.full_crater_count(self.params)
        params = dict(self.params)

        def job():
            result = terrain_core.generate_terrain(
                params, rows=preview_rows, cols=preview_cols,
                crater_count=crater_count, seed=seed,
                progress=self._progress_hook,
                cancel_check=self._cancel_event.is_set)
            self._post("preview", result)
            self._log_from_worker(
                f"Preview rendered at {preview_cols}x{preview_rows} "
                f"({result['crater_count']} craters, seed {seed}).")

        self._start_worker(job, "Rendering preview...")

    def run_full_generation(self):
        self._collect_params()
        seed = self._ensure_seed()
        params = dict(self.params)
        base_name = self.export_name_var.get().strip() or "procedural_lunar_terrain"
        elevation_name = f"{base_name}_elevationMap"

        # Describe the run and its outputs, and let the user confirm.
        # Outputs land in a per-run subfolder named after the base filename.
        pixels = params['ROWS'] * params['COLS']
        target_dir = os.path.join(GENERATOR_OUTPUT_DIR, base_name)
        files = planned_output_files(base_name, params['EXPORT_CSV'])
        file_list = "\n".join(f"    {name}\n        {desc}" for name, desc in files)

        message = (
            f"Generate a {params['COLS']} x {params['ROWS']} px DEM "
            f"({pixels / 1e6:.1f} megapixels) with seed {seed}.\n\n"
            "Files will be written to:\n"
            + format_path_block(target_dir) + f"\n\n{file_list}"
        )
        if pixels > 25_000_000:
            message += (
                "\n\nThis map is large: generation and file writing may take a "
                "while and use several GB of RAM. You can abort with the Stop "
                "button once it starts."
            )
        message += "\n\nContinue?"

        if not ask_confirmation(self, "Generate full-resolution DEM", message,
                                ok_label="Generate"):
            return

        def job():
            self._log_from_worker(
                f"Generating {params['COLS']}x{params['ROWS']} DEM (seed {seed})...")
            result = terrain_core.generate_terrain(
                params, seed=seed, progress=self._progress_hook,
                cancel_check=self._cancel_event.is_set)
            written = terrain_core.export_outputs(
                result['elevation'], GENERATOR_OUTPUT_DIR, base_name=elevation_name,
                export_csv=params['EXPORT_CSV'], export_npy=True,
                progress=self._progress_hook, cancel_check=self._cancel_event.is_set)
            self.exported_paths = written
            self._post("full", result)
            self._log_from_worker(
                f"Generated {result['crater_count']} craters.", level="ok")
            for label, path in written.items():
                self._log_from_worker(f"  wrote {label}: {path}")

        self._start_worker(job, "Generating full resolution...", cancellable=True)

    def _set_unreal_source(self, path):
        """Sets the DEM that 'Send to Unreal' will convert, and updates the field."""
        self.unreal_source_path = path
        self.unreal_source_var.set(
            os.path.basename(path) if path else "(none - generate or browse)")

        self.unreal_source_note_var.set(dem_source_note(path))

    def open_unreal_source_menu(self):
        """Drops the DEM list under Step 2's Browse button."""
        drop_dem_menu(self, self.unreal_browse_button,
                      self.pick_unreal_source, self.browse_unreal_source)

    def pick_unreal_source(self, path):
        """Accepts a map chosen from the drop-down."""
        self._set_unreal_source(path)
        self.log(f"Unreal source map: {path}")
        self._open_viewer(path)

    def browse_unreal_source(self, initialdir=None):
        """Lets the user pick any supported DEM to send to Unreal."""
        path = filedialog.askopenfilename(
            title="Select a DEM to send to Unreal",
            initialdir=initialdir or map_browse_dir(), filetypes=DEM_FILETYPES)
        if path:
            self.pick_unreal_source(path)

    def _open_viewer(self, path):
        """Opens (or refreshes) the reused Map Viewer window on `path`."""
        if self.map_viewer is None or not self.map_viewer.winfo_exists():
            self.map_viewer = MapViewer(self, figure_dpi=self.figure_dpi)
        self.map_viewer.show_path(path)
        self.map_viewer.deiconify()
        self.map_viewer.lift()

    def run_unreal_dispatch(self):
        """Converts the selected DEM, using the studio's own log and progress bar."""
        source_path = self.unreal_source_path
        problem = dem_source_problem(source_path)
        if problem:
            self.log(problem[0], level=problem[1])
            return

        resolution = self.params['RESOLUTION_M_PER_PX']
        dispatch = self.dispatch_var.get()
        save_level = self.save_level_var.get()
        output_dir = output_group_dir(source_path)

        self.log(f"Sending to Unreal: {os.path.basename(source_path)}")

        def job():
            convert_and_dispatch(
                source_path, resolution, dispatch, output_dir,
                log=self._log_from_worker,
                progress=self._progress_hook,
                dialog=self._dialog_from_worker,
                cancelled=self._cancel_event.is_set, save_level=save_level)

        self._start_worker(job, "Sending to Unreal...", cancellable=True)

    def open_output_folder(self):
        base_name = self.export_name_var.get().strip() or "procedural_lunar_terrain"
        target = os.path.join(OUTPUT_ROOT, base_name)
        target = target if os.path.isdir(target) else OUTPUT_ROOT
        os.makedirs(target, exist_ok=True)
        try:
            os.startfile(target)  # noqa: S606 - Windows shell open
        except AttributeError:  # pragma: no cover - non-Windows fallback
            subprocess.Popen(["xdg-open", target])

    # =====================================================================
    # RENDERING
    # =====================================================================

    def _invalidate_axes(self):
        self.stale_axes = {key: True for key in self.stale_axes}

    def _schedule_redraw(self, delay_ms=140):
        """Coalesces rapid display-only changes into a single redraw."""
        if self._redraw_job:
            self.after_cancel(self._redraw_job)
        self._redraw_job = self.after(delay_ms, self._run_scheduled_redraw)

    def _run_scheduled_redraw(self):
        self._redraw_job = None
        self.redraw(force=True)

    def redraw(self, force=False):
        """Renders only the visible tab; other tabs are marked stale and drawn lazily."""
        if self.displayed is None:
            return
        if force:
            self._invalidate_axes()

        index = self.notebook.index(self.notebook.select())
        key = ("relief", "surface", "slope")[index]
        if not self.stale_axes[key]:
            return

        renderer = {"relief": self._draw_relief,
                    "surface": self._draw_surface,
                    "slope": self._draw_slope}[key]
        renderer()
        self.stale_axes[key] = False
        self.canvases[key].draw_idle()

    def _set_surface_detail(self, dragging):
        """Swaps the 3D mesh between drag detail and full detail."""
        if not self._surface_artists:
            return
        fine, coarse = self._surface_artists
        if coarse.get_visible() == dragging:
            return
        fine.set_visible(not dragging)
        coarse.set_visible(dragging)
        # A blocking draw here, so the first drag frame already shows the coarse
        # mesh instead of queueing behind a full-detail redraw.
        self.canvases['surface'].draw()

    def _on_surface_press(self, event):
        if event.inaxes is not None:
            self._set_surface_detail(True)

    def _on_surface_release(self, _event):
        self._set_surface_detail(False)

    def _display_geometry(self):
        """
        Returns (elevation, extent_in_metres, effective_resolution) for the render
        currently on screen, accounting for preview downsampling.
        """
        elevation = self.displayed['elevation']
        rows, cols = elevation.shape
        scale = max(self.params['ROWS'], self.params['COLS']) / max(rows, cols)
        effective_res = self.params['RESOLUTION_M_PER_PX'] * scale
        extent = (0, cols * effective_res, 0, rows * effective_res)
        return elevation, extent, effective_res

    def _title_suffix(self):
        source = "FULL RESOLUTION" if self.displayed is self.full_result else "preview"
        rows, cols = self.displayed['elevation'].shape
        return f"{cols}x{rows} {source} - seed {self.displayed['seed']}"

    def _draw_relief(self):
        elevation, extent, effective_res = self._display_geometry()
        figure = self.figures['relief']
        figure.clear()
        axes = figure.add_subplot(111)

        light = LightSource(azdeg=self.sun_azimuth_var.get(),
                            altdeg=self.sun_altitude_var.get())
        shaded = light.shade(
            elevation, cmap=matplotlib.colormaps[self.cmap_var.get()],
            blend_mode="soft", vert_exag=self.exaggeration_var.get(),
            dx=effective_res, dy=effective_res)

        axes.imshow(shaded, extent=extent, origin="lower", interpolation="bilinear")
        axes.set_title(f"Sun-lit shaded relief - {self._title_suffix()}", fontsize=10)
        axes.set_xlabel("metres")
        axes.set_ylabel("metres")
        axes.set_aspect("equal")

        # The shaded image carries lighting rather than scalar values, so the
        # colourbar is driven by a standalone mappable over the elevation range.
        mappable = ScalarMappable(
            norm=Normalize(vmin=float(np.min(elevation)), vmax=float(np.max(elevation))),
            cmap=self.cmap_var.get())
        figure.colorbar(mappable, ax=axes, shrink=0.8, label="Elevation (m)")

    def _draw_surface(self):
        elevation, extent, effective_res = self._display_geometry()
        figure = self.figures['surface']
        figure.clear()
        self._surface_artists = None
        axes = figure.add_subplot(111, projection="3d")

        rows, cols = elevation.shape
        # Cap the mesh density so large previews stay interactive
        step = max(1, int(np.ceil(max(rows, cols) / SURFACE_MESH_DETAIL)))
        sub = elevation[::step, ::step]
        sub_res = effective_res * step
        x_axis = np.linspace(extent[0], extent[1], sub.shape[1])
        y_axis = np.linspace(extent[2], extent[3], sub.shape[0])
        grid_x, grid_y = np.meshgrid(x_axis, y_axis)

        span_x = extent[1] - extent[0]
        span_y = extent[3] - extent[2]
        span = max(span_x, span_y)
        relief = max(float(np.ptp(sub)), 1e-6)

        # Relief is metres over kilometres, so a true-scale box would look flat.
        # Fit the Z box to a readable height and report the implied exaggeration
        # in the title instead of hiding it.
        z_box = 0.35
        implied_exaggeration = (z_box * span) / relief

        light = LightSource(azdeg=self.sun_azimuth_var.get(),
                            altdeg=self.sun_altitude_var.get())

        def plot_mesh(mesh, mesh_res):
            """Draws one level of detail and returns its collection."""
            mesh_x = np.linspace(extent[0], extent[1], mesh.shape[1])
            mesh_y = np.linspace(extent[2], extent[3], mesh.shape[0])
            mesh_gx, mesh_gy = np.meshgrid(mesh_x, mesh_y)
            colors = light.shade(
                mesh, cmap=matplotlib.colormaps[self.cmap_var.get()],
                blend_mode="soft", vert_exag=implied_exaggeration,
                dx=mesh_res, dy=mesh_res)
            # rcount/ccount match the mesh so Matplotlib does not downsample the
            # grid out of alignment with the pre-computed face colours.
            return axes.plot_surface(
                mesh_gx, mesh_gy, mesh, facecolors=colors, shade=False,
                linewidth=0, antialiased=False,
                rcount=mesh.shape[0], ccount=mesh.shape[1])

        fine_surface = plot_mesh(sub, sub_res)

        # A coarse copy, drawn once and swapped in during a drag to keep rotation
        # responsive (see _on_surface_press).
        drag_step = max(1, int(np.ceil(max(rows, cols) / SURFACE_DRAG_DETAIL)))
        if drag_step > step:
            coarse_surface = plot_mesh(elevation[::drag_step, ::drag_step],
                                       effective_res * drag_step)
            coarse_surface.set_visible(False)
            self._surface_artists = (fine_surface, coarse_surface)

        axes.set_title(
            f"3D surface - {self._title_suffix()}\n"
            f"vertical exaggeration x{implied_exaggeration:,.0f} (auto-fit)",
            fontsize=10)
        axes.set_xlabel("metres")
        axes.set_ylabel("metres")
        axes.set_zlabel("elevation (m)")
        axes.view_init(elev=32, azim=-125)
        axes.set_zlim(float(np.min(sub)) - relief * 0.15,
                      float(np.max(sub)) + relief * 0.15)
        axes.zaxis.set_major_locator(MaxNLocator(5))
        axes.set_box_aspect((span_x / span, span_y / span, z_box))
        figure.subplots_adjust(left=0.0, right=0.97, top=0.93, bottom=0.02)

    def _draw_slope(self):
        elevation, extent, effective_res = self._display_geometry()
        figure = self.figures['slope']
        figure.clear()

        gradient_y, gradient_x = np.gradient(elevation, effective_res, effective_res)
        slope_degrees = np.degrees(np.arctan(np.hypot(gradient_x, gradient_y)))

        # The map carries the detail, so it gets two thirds of the frame.
        grid = figure.add_gridspec(3, 1)
        axes = figure.add_subplot(grid[0:2, 0])
        image = axes.imshow(slope_degrees, extent=extent, origin="lower",
                            cmap="inferno", interpolation="bilinear",
                            vmax=max(1.0, np.percentile(slope_degrees, 99.5)))
        axes.set_title(f"Slope magnitude - {self._title_suffix()}", fontsize=10)
        axes.set_xlabel("metres")
        axes.set_ylabel("metres")
        axes.set_aspect("equal")
        figure.colorbar(image, ax=axes, shrink=0.85, label="Slope (deg)")

        histogram_axes = figure.add_subplot(grid[2, 0])
        histogram_axes.hist(elevation.ravel(), bins=120, color="#4a7ebb")
        histogram_axes.set_title("Elevation distribution", fontsize=10)
        histogram_axes.set_xlabel("Elevation (m)")
        histogram_axes.set_ylabel("Pixels")

    # =====================================================================
    # SHUTDOWN
    # =====================================================================

    def _on_close(self):
        if self._busy():
            if not ask_confirmation(
                    self, "Job running",
                    "A generation job is still running.\n\nQuit anyway?",
                    ok_label="Quit"):
                return
        self.destroy()


if __name__ == "__main__":
    enable_dpi_awareness()  # Must precede the creation of the Tk root window
    TerrainStudio().mainloop()

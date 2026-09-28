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

# Every artefact the studio produces -- DEMs, rock fields, and Unreal .r16
# heightmaps -- goes under one folder at the repository root, grouped per map,
# so a surface and everything derived from it stay together.
OUTPUT_ROOT = os.path.join(PROJECT_ROOT, "outputs")
GENERATOR_OUTPUT_DIR = OUTPUT_ROOT
INPUT_MAPS_DIR = os.path.join(PROJECT_ROOT, "input_maps")


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

# The Unreal loader is optional: the studio still generates and previews terrain
# on a machine without the loader dependencies available.
try:
    import terrain_files_generator  # noqa: E402
    import main as lunar_loader     # noqa: E402
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
         "Bowl depth relative to crater radius. Real lunar craters sit near 0.2."),
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
         "The resulting rock count is reported after each preview."),
        ("ROCK_MIN_SCALE", "Min scale", 0.05, 10.0, 'log',
         "Smallest rock scale: the uniform mesh scale, i.e. the metre radius of a\n"
         "1 m base mesh."),
        ("ROCK_MAX_SCALE", "Max scale", 0.1, 30.0, 'log',
         "Largest rock scale. Scales are cubically biased towards the minimum."),
        ("ROCK_HEIGHT_RATIO", "Height / scale", 0.1, 2.0, 'float',
         "Rock height as a fraction of its radius, written to the instance list\n"
         "for scaling the mesh in Unreal."),
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
    Modal dialog for the studio's long, pre-formatted messages.

    tkinter's messagebox hands the string to the platform dialog, which re-wraps
    it at a width of its own choosing: indented file listings lose their
    alignment and long output paths get clipped. This renders the text verbatim
    in a monospace widget sized to the content, so the caller's layout survives,
    the text can be selected and copied, and anything taller than the screen
    scrolls instead of being cut off.
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
        Estimated rows the message occupies once wrapped to `width` columns.

        Mirrors the widget's word wrapping, including the part that matters:
        a word that does not fit in what is left of a row starts a new one, so
        a row can end well short of `width`. Dividing the line length by the
        width misses that and comes out too low.
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
        Re-sizes against Tk's real layout.

        The estimate above is close but not exact -- Tk and textwrap disagree by
        a row on where a long unbroken path splits -- and being one row short
        with no scrollbar is the failure this dialog exists to avoid. Once the
        window is mapped the widget can report what it actually laid out.
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

# Rasters the pipeline writes alongside a DEM, in the same folders and with the
# same extensions, but which are not elevation: class and soil masks, rock and
# path tables, and the loader's own intermediates. Offering them as a map to
# import would silently spawn a Landscape out of, say, a class raster, so they
# are kept out of the map lists even though their extension is supported.
NON_DEM_TAGS = ('_classmap', '_soilmap', '_obstaclemap', '_rocks',
                '_roverpaths', '_converted', '_heightmap')


def maximize_window(window):
    """
    Maximizes a window across platforms, kept movable rather than borderless.

    Windows and some WMs take state("zoomed"); many Linux WMs only take the
    -zoomed attribute; anything else just gets sized to the screen.
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
    A warning about `path` as an elevation source, or "" when it looks right.

    The drop-down filters the companion rasters out, but the file picker cannot,
    and converting one yields a Landscape built from class indices or a 0/1
    mask with no error to show for it. This is the quiet failure worth naming.
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
            "Converting it will build terrain out of mask values.")


def build_dem_menu(parent, on_pick, on_browse):
    """
    A drop-down of the DEMs already in the project's folders.

    Nearly every export uses a map that is already in `input_maps/` or was just
    written to `outputs/`, and walking the file picker to it each time is the
    slow way to say so. Companion rasters (class and soil masks, rock tables)
    share those folders and extensions but are not terrain, so they are
    filtered out rather than offered as a trap. The picker stays on the menu
    for anything kept elsewhere.
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

    # Aim the file picker at a specific folder rather than guessing. The
    # drop-down only lists one level down, so a map buried deeper in outputs/
    # still needs the picker, and it should open where the map is.
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
    The files a full-resolution generation will write, as [(filename, what it is)].

    One source of truth for the panel that previews them and the dialog that
    confirms them, so the names shown before the run and the names written by
    it cannot drift apart.
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


def looks_like_dem(filename):
    """True when `filename` is a supported elevation raster rather than a companion."""
    stem, extension = os.path.splitext(filename)
    if extension.lower() not in SUPPORTED_DEM_EXTENSIONS:
        return False
    lowered = stem.lower()
    return not any(tag in lowered for tag in NON_DEM_TAGS)


def find_dem_files(max_per_group=30):
    """
    DEMs sitting in the project's own folders, as [(group label, [(name, path)])].

    Covers `input_maps/` and `outputs/`, each one level of subfolders deep,
    which is how both are laid out. Anything elsewhere on disk is still
    reachable through the file picker, so this only has to be quick and
    unsurprising, not exhaustive.
    """
    groups = []
    for root, label in ((INPUT_MAPS_DIR, "input_maps"), (OUTPUT_ROOT, "outputs")):
        found = scan_dem_folder(root)
        if found:
            groups.append((label, found[:max_per_group], len(found)))
    return groups


def scan_dem_folder(root, include_companions=False):
    """
    Maps in `root` and one level of subfolders, as [(display name, path)].

    One level is how both project folders are laid out, and keeping the walk
    shallow keeps the menu instant even when outputs/ has grown. Set
    `include_companions` to list the masks and tables too -- useful for looking
    at one, never for importing it.
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
    "    2. Select the Landscape actor. With 'Dispatch to running\n"
    "       Unreal Editor' enabled the studio spawns one; otherwise\n"
    "       create a Landscape sized to the heightmap.\n"
    "    3. Right-click it in the Manage tab list and choose 'Import'.\n"
    "    4. Set the Heightmap File to the path above.\n"
    "    5. Click Import.\n\n"
    "The relief is applied immediately.")


def dem_source_problem(source_path):
    """
    Why `source_path` cannot be sent to Unreal, as (message, level), or None
    when it can. Kept apart from the widgets so both entry points refuse the
    same inputs for the same reasons.
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
                         log, progress, dialog, cancelled):
    """
    Converts a DEM into Unreal's 16-bit heightmap and, optionally, spawns the
    Landscape in a running editor. Runs on a worker thread.

    The caller passes in how to report back, so the same pipeline drives either
    the studio's footer or the standalone dispatch window:

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

    # Import into Unreal is a manual step, so show the instructions and the
    # exact file path here, not only inside the editor.
    dialog("Heightmap ready for Unreal",
           "The 16-bit heightmap has been written to:\n\n"
           f"    {heightmap_path}\n\n" + IMPORT_STEPS)

    if not dispatch:
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
                                    z_offset=z_offset, output_directory=output_dir)
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
    Embeddable DEM view: a sun-lit shaded relief with an elevation colourbar.

    Loading a large map takes seconds, so it runs on a worker thread and the
    result is drawn here on the main thread. A token discards a stale load when
    a newer map is asked for first, which is what makes clicking down a list of
    maps feel immediate rather than queueing up every one.

    The host supplies `on_status` (a log line) and `on_title` (the map's name),
    so the same view serves a standalone window and a browser pane.
    """

    DISPLAY_MAX_EDGE = 1200

    def __init__(self, parent, figure_dpi=100, on_status=None, on_title=None):
        super().__init__(parent)
        self._on_status = on_status or (lambda text, level=None: None)
        self._on_title = on_title or (lambda name: None)

        self.figure = Figure(figsize=(7.0, 6.0), dpi=figure_dpi, layout="constrained")
        self.canvas = FigureCanvasTkAgg(self.figure, master=self)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        NavigationToolbar2Tk(self.canvas, self).update()

        self._load_queue = queue.Queue()
        self._load_token = 0
        self._awaiting = None
        self._poll_job = None

        self.show_message("No map selected")

    @property
    def busy(self):
        return self._awaiting is not None

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
                'vmin': vmin, 'vmax': vmax, 'relief': relief}

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
        """Draws a centred status message in place of the plot."""
        self.figure.clear()
        axes = self.figure.add_subplot(111)
        axes.axis("off")
        axes.text(0.5, 0.5, text, ha="center", va="center", fontsize=12,
                  transform=axes.transAxes)
        self.canvas.draw_idle()

    def _render(self, name, payload):
        """Draws the shaded relief prepared by the worker."""
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


class MapViewer(tk.Toplevel):
    """
    Standalone window around a single MapView, reused across selections.

    Kept for the one-off previews raised from the export panels, where the point
    is to confirm a map that has already been chosen. Browsing a folder is what
    MapBrowser is for.
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


class MapBrowser(tk.Toplevel):
    """
    Folder on the left, map on the right, updating as the selection moves.

    Opening a file picker per map made comparing them tedious: pick, look,
    close, pick again. Here the maps in the project's folders are listed once
    and arrowing down the list redraws the view, so a folder can be read
    through in one pass.
    """

    # Wide enough for a nested name like
    # "procedural_lunar_terrain_1/procedural_lunar_terrain_1_elevationMap.npy"
    # without truncating it; the drag handle adjusts it from there.
    LIST_WIDTH = 420

    def __init__(self, master, figure_dpi=100, initial_path=None):
        super().__init__(master)
        self.title("Browse Maps")

        # A restored size to fall back on, then opened maximized: comparing maps
        # is what this window is for, and that wants the whole screen.
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        width = min(1400, int(screen_w * 0.78))
        height = min(1000, int(screen_h * 0.88))
        self.geometry(f"{width}x{height}+{max(0, (screen_w - width) // 2)}+"
                      f"{max(0, (screen_h - height) // 4)}")
        self.minsize(780, 560)
        maximize_window(self)

        self.app = master
        self._paths = {}          # tree item id -> file path
        self._extra_folders = []  # folders added through "Add folder..."
        self._selected = None
        self._show_job = None     # pending debounced load

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
                            on_title=lambda name: self.title(f"Browse Maps - {name}"))
        self._log.pack(side="bottom", fill="x")
        self.view.pack(side="top", fill="both", expand=True)

        self.bind("<Escape>", lambda _e: self.destroy())
        self.refresh()
        if initial_path:
            self.select_path(initial_path)

        # The handle can only be placed once the panes have a real width, which
        # is after the window has been mapped and maximized.
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

            # Both project folders are always shown, even before anything has
            # been generated: a missing outputs/ is a state worth seeing, not a
            # reason to leave the user wondering where their exports went.
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

    # Arrowing down the list moves the selection once per key press. Loading on
    # each one would start a read per map, and the folders hold DEMs of several
    # hundred megabytes, so the selection is allowed to settle first.
    SELECTION_DELAY_MS = 250

    def show(self, path):
        if path == self._selected:
            return
        self._selected = path
        if self._show_job is not None:
            self.after_cancel(self._show_job)
        self._show_job = self.after(self.SELECTION_DELAY_MS, self._show_now)

    def _show_now(self):
        self._show_job = None
        if self._selected and self.winfo_exists():
            self.view.show_path(self._selected)


class RockPlannerStudio(tk.Toplevel):
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
        self.title("Rock Field & Rover Paths")
        self.geometry("1180x820")     # restored size once un-maximized
        self.minsize(900, 640)

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

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._drain_queue)
        self.log("Rock planner ready. Load a DEM to begin.")

        if source_path and os.path.exists(source_path):
            self.load_dem(source_path)

        self.after(0, self._maximize)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        panel_width = self.master.px(348) if hasattr(self.master, "px") else 348
        left = ttk.Frame(self, width=panel_width, padding=(8, 8, 4, 8))
        left.pack(side="left", fill="y")
        left.pack_propagate(False)   # honour the fixed width; never collapse

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
        canvas = tk.Canvas(scroll_host, highlightthickness=0, borderwidth=0,
                           background=self.cget("background"))
        scrollbar = ttk.Scrollbar(scroll_host, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        ttk.Label(inner, wraplength=panel_width - self.master.px(40)
                  if hasattr(self.master, "px") else panel_width - 40,
                  justify="left", font=("Segoe UI", 8), foreground="#555555",
                  text="Scatters rocks as obstacles across the drivable terrain while "
                       "keeping a connected corridor network the rover can always "
                       "traverse.").pack(anchor="w", pady=(0, 8), fill="x")

        # Source DEM
        source_group = ttk.Labelframe(inner, text="Source DEM", padding=(8, 4, 8, 8),
                                      style="Group.TLabelframe")
        source_group.pack(fill="x", pady=(0, 8))
        self.source_var = tk.StringVar(value="(none - load a DEM)")
        ttk.Entry(source_group, textvariable=self.source_var, state="readonly",
                  font=("Consolas", 8)).pack(fill="x")
        res_row = ttk.Frame(source_group)
        res_row.pack(fill="x", pady=(6, 0))
        ttk.Button(res_row, text="Load DEM...", command=self.browse_dem).pack(
            side="left", fill="x", expand=True)
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

        # Parameters
        for group_name, specs in ROCK_PARAM_GROUPS:
            group = ttk.Labelframe(inner, text=group_name, padding=(8, 4, 8, 8),
                                   style="Group.TLabelframe")
            group.pack(fill="x", pady=(0, 8))
            for spec in specs:
                self.rows_widgets[spec[0]] = ParamRow(group, spec, self._on_param_change)

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
                  font=("Consolas", 9)).pack(fill="x", pady=(2, 6))
        self.obstacle_map_var = tk.BooleanVar(value=False)
        obstacle_check = ttk.Checkbutton(
            export_group, text="Also write full-res obstacle map (large)",
            variable=self.obstacle_map_var)
        obstacle_check.pack(anchor="w")
        Tooltip(obstacle_check,
                "Writes a full-resolution 0/1 rock-footprint raster alongside the\n"
                "instance list. Useful as a mask, but large for big DEMs.")

        # Mouse-wheel scrolling over the panel, bound locally (not app-wide) so it
        # never fights the main studio's own wheel scrolling.
        self._bind_mousewheel(inner, canvas)
        self._bind_mousewheel(canvas, canvas)

        # Right: plot on top, log at bottom.
        right = ttk.Frame(self)
        right.pack(side="right", fill="both", expand=True)

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

    def _maximize(self):
        maximize_window(self)

    def _bind_mousewheel(self, widget, canvas):
        """Binds vertical wheel scrolling on `widget` and all its descendants."""
        widget.bind("<MouseWheel>",
                    lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))
        for child in widget.winfo_children():
            self._bind_mousewheel(child, canvas)

    def go_home(self):
        """Closes the planner and returns to the studio's home screen."""
        self._cancel.set()
        master = self.master
        self.destroy()
        try:
            master.show_home()
            master.deiconify()
            master.lift()
        except Exception:
            pass

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
    def browse_dem(self):
        path = filedialog.askopenfilename(
            parent=self, title="Select a DEM for rock planning",
            initialdir=map_browse_dir(),
            filetypes=[("DEM files", "*.npy *.csv *.txt *.tif *.tiff"),
                       ("All files", "*.*")])
        if path:
            self.load_dem(path)

    def load_dem(self, path):
        """Loads the DEM off-thread, then triggers a preview."""
        self.source_var.set(os.path.basename(path))
        self.dem_path = path
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
        files = [f"{base_name}_rocks.csv    - rock instances (id, row, col, x_m, y_m, scale, height_m)",
                 f"{base_name}_roverPaths.csv - rover corridor polylines (path_id, x_m, y_m)"]
        if self.obstacle_map_var.get():
            files.append(f"{base_name}_obstacleMap.csv - full-resolution 0/1 rock mask")
        message = ("Plan the rock field and write:\n\n    " + "\n    ".join(files)
                   + f"\n\nInto:\n    {group_dir}\n\nContinue?")
        if not ask_confirmation(self, "Generate & export rock field", message,
                                ok_label="Generate"):
            return
        self._launch(export=True)

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
                elif kind == "written":
                    self.log("Rock field written:", level="ok")
                    for label, path in payload.items():
                        self.log(f"  {label}: {path}")
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
                    self.stop_button.state(["disabled"])
                    if self.status_var.get() not in ("Failed", "Cancelled"):
                        self.status_var.set("Idle")
                    self.progress.configure(value=0)
        except queue.Empty:
            pass
        self.after(80, self._drain_queue)

    def _log_result(self, result):
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

    # --------------------------------------------------------- shutdown
    def _on_close(self):
        self._cancel.set()
        self.destroy()


# =========================================================================
# MAIN APPLICATION
# =========================================================================


class UnrealDispatchWindow(tk.Toplevel):
    """
    Standalone window for the single job of turning an existing DEM into an
    Unreal Landscape.

    The studio's own Send to Unreal button sits beside the generator controls,
    which is where it belongs right after generating a surface. Arriving from
    the home screen with a map already on disk, none of those controls apply,
    so this window offers only what the export actually reads: the map, its
    resolution, and whether to dispatch to a running editor.
    """

    def __init__(self, parent, source_path=None, resolution=1.0):
        super().__init__(parent)
        self.app = parent
        self.title("Send a Map to Unreal")
        self.transient(parent)
        self.minsize(620, 470)

        self.source_path = None
        self.worker = None
        self._cancel_event = threading.Event()
        self._queue = queue.Queue()

        self._build(resolution)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Escape>", lambda _e: self._on_close())

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
    def _build(self, resolution):
        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="Send a Map to Unreal",
                  style="Header.TLabel").pack(anchor="w")
        ttk.Label(outer, style="HomeHint.TLabel",
                  text="Converts a DEM to Unreal's 16-bit heightmap and spawns "
                       "the Landscape actor.").pack(anchor="w", pady=(0, 10))

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
        self.browse_button = ttk.Button(source_buttons, text="Browse  ▾",
                                        command=self.open_source_menu)
        self.browse_button.pack(side="left")
        Tooltip(self.browse_button,
                "Lists the DEMs already in input_maps/ and outputs/, or opens\n"
                "the file picker for a map kept anywhere else.")
        self.preview_button = ttk.Button(source_buttons, text="Preview",
                                         command=self.preview, state="disabled")
        self.preview_button.pack(side="left", padx=(6, 0))

        ttk.Label(source_group, style="HomeHint.TLabel", justify="left",
                  text="An elevation map in metres "
                       "(.npy, .csv, .txt, .tif, .tiff).").pack(
            anchor="w", pady=(6, 0))

        # Empty unless the pick looks wrong; the menu filters these out but the
        # file picker cannot.
        self.source_note_var = tk.StringVar()
        ttk.Label(source_group, textvariable=self.source_note_var, justify="left",
                  style="Readout.TLabel", foreground="#b8860b",
                  wraplength=560).pack(anchor="w")

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

        # --- destination ---
        destination = ttk.Frame(outer)
        destination.pack(fill="x", pady=(8, 0))
        ttk.Label(destination, text="Writes to", font=("Segoe UI", 8)).pack(anchor="w")
        self.destination_var = tk.StringVar(value="-")
        ttk.Label(destination, textvariable=self.destination_var,
                  style="Readout.TLabel", wraplength=560,
                  justify="left").pack(anchor="w")

        # --- log ---
        log_group = ttk.Labelframe(outer, text="Log", style="Group.TLabelframe",
                                   padding=8)
        log_group.pack(fill="both", expand=True, pady=(8, 0))

        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(log_group, textvariable=self.status_var,
                  style="Readout.TLabel").pack(anchor="w")
        self.progress = ttk.Progressbar(log_group, mode="determinate", maximum=1.0)
        self.progress.pack(fill="x", pady=(4, 4))

        self.log_text = tk.Text(log_group, height=9, font=("Consolas", 8), wrap="word",
                                relief="solid", borderwidth=1, background="#1e1e1e",
                                foreground="#d4d4d4", insertbackground="#d4d4d4",
                                state="disabled")
        self.log_text.pack(fill="both", expand=True)
        self.log_text.tag_configure("warn", foreground="#e6c07b")
        self.log_text.tag_configure("error", foreground="#e06c75")
        self.log_text.tag_configure("ok", foreground="#98c379")

        # --- actions ---
        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(10, 0))
        self.send_button = ttk.Button(actions, text="Send to Unreal",
                                      style="Accent.TButton", command=self.send)
        self.send_button.pack(side="right")
        self.stop_button = ttk.Button(actions, text="Stop", command=self.stop,
                                      state="disabled")
        self.stop_button.pack(side="right", padx=(0, 6))
        ttk.Button(actions, text="Close", command=self._on_close).pack(side="left")

    # -------------------------------------------------------------- input
    def set_source(self, path, announce=True):
        """Points the export at `path` and refreshes the destination readout."""
        self.source_path = path
        self.source_var.set(path)
        # Scroll to the end: a full path overflows the field, and the filename
        # is the half worth reading, not the drive letter.
        self.source_entry.after_idle(self.source_entry.xview_moveto, 1.0)
        self.destination_var.set(output_group_dir(path))
        self.preview_button.state(["!disabled"])
        self.source_note_var.set(dem_source_note(path))
        if announce:
            self.log(f"Source map: {path}")

    def open_source_menu(self):
        """Drops the map list under the Browse button, rebuilt so it stays current."""
        drop_dem_menu(self, self.browse_button, self.set_source, self.browse)

    def browse(self, initialdir=None):
        path = filedialog.askopenfilename(
            parent=self, title="Select a DEM to send to Unreal",
            initialdir=initialdir or map_browse_dir(), filetypes=DEM_FILETYPES)
        if path:
            self.set_source(path)

    def preview(self):
        """Shows the chosen map in the app's shared viewer window."""
        if self.source_path:
            self.app._open_viewer(self.source_path)

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
                    cancelled=self._cancel_event.is_set)
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
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text.rstrip() + "\n", level or ())
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _on_close(self):
        if self._busy():
            if not ask_confirmation(
                    self, "Conversion running",
                    "A conversion is still running.\n\nClose anyway?",
                    ok_label="Close"):
                return
            # The worker is a daemon thread and its remaining work only writes
            # files, so asking it to stop and letting the window go is safe.
            self._cancel_event.set()
        self.destroy()


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
        style.configure("Home.TButton", font=("Segoe UI Semibold", 12))
        style.configure("HomeTitle.TLabel", font=("Segoe UI Semibold", 22))
        style.configure("HomeSub.TLabel", font=("Segoe UI", 10), foreground="#666666")
        style.configure("HomeHint.TLabel", font=("Segoe UI", 8), foreground="#777777")

    def _build_layout(self):
        # Two top-level views swapped by show_home / show_studio.
        self._build_home()

        self.studio_frame = ttk.Frame(self)

        # The three columns are split by drag handles rather than fixed widths:
        # how much room the parameters, the preview, and the readouts deserve
        # depends on the map and the screen, and only the user knows which.
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
        """Landing screen with the app's top-level actions."""
        self.home_frame = ttk.Frame(self)

        center = ttk.Frame(self.home_frame)
        center.place(relx=0.5, rely=0.44, anchor="center")

        ttk.Label(center, text="Lunar Terrain Studio",
                  style="HomeTitle.TLabel").pack()
        ttk.Label(center, text="Procedural lunar DEM generation, preview, and Unreal import",
                  style="HomeSub.TLabel").pack(pady=(2, 26))

        actions = [
            ("Generate New Terrain",
             "Set parameters, preview, and export a new DEM.",
             self.enter_generate),
            ("View an Existing Map",
             "Browse the project's maps and see each one as you select it.",
             self.browse_maps),
            ("Place Rocks & Paths",
             "Scatter rocks on a DEM while keeping drivable rover corridors.",
             self.enter_rock_planner),
            ("Send a Map to Unreal",
             "Pick a DEM and build its Unreal Landscape heightmap.",
             self.enter_send_to_unreal),
        ]
        for label, hint, command in actions:
            ttk.Button(center, text=label, style="Home.TButton", width=30,
                       command=command).pack(fill="x", ipady=8)
            ttk.Label(center, text=hint, style="HomeHint.TLabel").pack(pady=(3, 16))

    def show_home(self):
        """Switches to the landing screen."""
        self.studio_frame.pack_forget()
        self.home_frame.pack(fill="both", expand=True)

    # Column widths in 96-DPI design pixels, used until the user drags a handle.
    DEFAULT_PARAM_WIDTH = 300
    DEFAULT_SIDE_WIDTH = 290
    MIN_COLUMN_WIDTH = 140

    def show_studio(self):
        """Switches to the main studio, starting the preview on first entry."""
        self.home_frame.pack_forget()
        self.studio_frame.pack(fill="both", expand=True)
        if not self._sash_placed:
            self.after_idle(self._apply_sash_positions)
        if not self._preview_started:
            self._preview_started = True
            self.request_preview()

    def _apply_sash_positions(self, attempt=0):
        """
        Places the two drag handles, on first show and after a DPI rebuild.

        A sash position is measured in pixels from the left of its own paned
        window and only takes effect once that window has a real width. While
        the panes are still settling into their first layout Tk quietly snaps
        the sash back to each pane's requested width, so the placement is
        verified and retried briefly rather than assumed.
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
                self, source_path=source,
                resolution=self.params.get('RESOLUTION_M_PER_PX', 1.0))
        else:
            self.unreal_window.deiconify()
            if source:
                self.unreal_window.set_source(source)
        self.unreal_window.lift()

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
        else:
            self.rock_planner.deiconify()
            if source:
                self.rock_planner.load_dem(source)
        self.rock_planner.lift()

    def browse_maps(self):
        """Opens the map browser, seeded with whatever map is already in hand."""
        source = (self.exported_paths.get('elevation_npy')
                  or self.unreal_source_path)
        if self.map_browser is None or not self.map_browser.winfo_exists():
            self.map_browser = MapBrowser(self, figure_dpi=self.figure_dpi,
                                          initial_path=source)
        else:
            self.map_browser.deiconify()
            self.map_browser.refresh()
            if source:
                self.map_browser.select_path(source)
        self.map_browser.lift()


    # ---------------------------------------------------------------- left
    def _scrollable_column(self, parent, width=None):
        """
        A vertically scrolling column; returns the frame to fill with content.

        Both side columns can outgrow the window -- the parameter list always
        has, and the export panel does now that it explains itself -- and a
        column that simply drops the widgets that do not fit is worse than one
        that scrolls.

        The wheel is bound only while the pointer is over this column. Binding
        it globally, as the parameter panel did, meant the wheel scrolled that
        panel wherever the pointer was, including over the log and the preview.
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
            # Moving onto a widget inside the column still counts as inside.
            # Tk signals that through the event's detail field, but tkinter does
            # not always populate it, so compare the pointer against the
            # canvas bounds instead.
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

        # A drag handle between the preview and the readout/export column, so
        # the split can be set per map: a wide DEM wants the preview, a long
        # statistics read wants the column. The preview carries all the weight
        # so growing the window grows the map, not the fixed-width readouts.
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

        # The base filename is a stem several files are derived from, so show the
        # names that will actually appear on disk, extensions and all, rather
        # than leaving the user to infer them.
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

        # Says what the input is without listing what it is not: the drop-down
        # already filters the companion rasters out, and picking one by hand
        # raises a warning under the field.
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
                "Remote Execution enabled and spawns the Landscape actor automatically.\n"
                "When disabled, only the .r16 / .json / .txt files are written.")

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
        # Re-arming unconditionally leaves a timer pointing at a destroyed
        # widget when the window closes, which Tk reports as an "invalid command
        # name" on the way out. The id is kept so the pending one can be
        # cancelled too; skipping the re-arm alone is not enough, because the
        # timer already in flight still fires after destroy().
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
            f"Files will be written to:\n    {target_dir}\n\n{file_list}"
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
        output_dir = output_group_dir(source_path)

        self.log(f"Sending to Unreal: {os.path.basename(source_path)}")

        def job():
            convert_and_dispatch(
                source_path, resolution, dispatch, output_dir,
                log=self._log_from_worker,
                progress=self._progress_hook,
                dialog=self._dialog_from_worker,
                cancelled=self._cancel_event.is_set)

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

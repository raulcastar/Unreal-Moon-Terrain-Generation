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
import threading
import traceback
from contextlib import redirect_stdout

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import matplotlib
matplotlib.use("TkAgg")

import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.cm import ScalarMappable
from matplotlib.colors import LightSource, Normalize
from matplotlib.figure import Figure
from matplotlib.ticker import MaxNLocator

# =========================================================================
# PROJECT PATHS & SIBLING MODULE IMPORTS
# =========================================================================

STUDIO_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(STUDIO_DIR)
GENERATOR_DIR = os.path.join(PROJECT_ROOT, "Procedural_generator")
LOADER_DIR = os.path.join(PROJECT_ROOT, "Lunar_terrain_loader")
CONFIG_DIR = os.path.join(GENERATOR_DIR, "config")
GENERATOR_OUTPUT_DIR = os.path.join(GENERATOR_DIR, "outputs")

# Both sibling packages are plain script directories, so they are placed on the
# import path rather than being restructured into installable packages.
for path in (GENERATOR_DIR, LOADER_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

import terrain_core  # noqa: E402  (path setup must precede the import)

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


# =========================================================================
# SMALL UI HELPERS
# =========================================================================

def enable_dpi_awareness():
    """
    Opts the process into real pixels on Windows.

    Without this, a display running at 125%/150% scaling hands Tk a virtualized
    resolution and then bitmap-upscales the window, which both blurs the
    Matplotlib canvases and makes the window overflow the physical screen.
    """
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # System DPI aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
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


# =========================================================================
# MAIN APPLICATION
# =========================================================================

class TerrainStudio(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("Lunar Terrain Studio - Procedural DEM to Unreal Engine")

        # Every hard-coded pixel dimension below is expressed at 96 DPI and
        # multiplied by this factor; point-sized fonts are scaled by Tk itself.
        self.ui_scale = self.winfo_fpixels('1i') / 96.0
        self.figure_dpi = round(100 * self.ui_scale)

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
        self.stale_axes = {'relief': True, 'surface': True, 'slope': True}
        self._surface_artists = None   # (full detail, drag detail) collections
        self._redraw_job = None

        self._build_style()
        self._build_layout()
        self._apply_params_to_widgets()
        self._refresh_derived_readouts()

        self.after(100, self._drain_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.log("Lunar Terrain Studio ready.")
        if not LOADER_AVAILABLE:
            self.log(f"Unreal loader unavailable: {LOADER_IMPORT_ERROR}", level="warn")
        self.request_preview()

    # =====================================================================
    # LAYOUT CONSTRUCTION
    # =====================================================================

    def px(self, value):
        """Converts a 96-DPI design dimension into physical pixels."""
        return int(round(value * self.ui_scale))

    def _build_style(self):
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Header.TLabel", font=("Segoe UI Semibold", 10))
        style.configure("Group.TLabelframe.Label", font=("Segoe UI Semibold", 9))
        style.configure("Readout.TLabel", font=("Consolas", 8))
        style.configure("Accent.TButton", font=("Segoe UI Semibold", 9))

    def _build_layout(self):
        # --- Left: scrollable parameter column ---
        left = ttk.Frame(self, padding=(8, 8, 4, 8))
        left.pack(side="left", fill="y")
        self._build_param_panel(left)

        # --- Right: preview + footer ---
        # Pack order matters: the fixed-height footer must claim its space before
        # the expanding preview area, or it gets squeezed off the window.
        right = ttk.Frame(self, padding=(4, 8, 8, 8))
        right.pack(side="right", fill="both", expand=True)
        self._build_footer(right)
        self._build_preview_panel(right)

    # ---------------------------------------------------------------- left
    def _build_param_panel(self, parent):
        ttk.Label(parent, text="Simulation Parameters", style="Header.TLabel").pack(anchor="w")

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
        container = ttk.Frame(parent)
        container.pack(fill="both", expand=True)

        canvas = tk.Canvas(container, width=self.px(336), highlightthickness=0,
                           background=self.cget("background"))
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)

        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def _on_wheel(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")
        canvas.bind_all("<MouseWheel>", _on_wheel)

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

        # Fixed-width readout column first, so the expanding notebook cannot
        # consume the entire cavity and push it off-screen.
        side = ttk.Frame(top, width=self.px(290), padding=(8, 0, 0, 0))
        side.pack(side="right", fill="y")
        side.pack_propagate(False)

        # Figure notebook
        self.notebook = ttk.Notebook(top)
        self.notebook.pack(side="left", fill="both", expand=True)
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

        ttk.Label(export_group, wraplength=wrap, justify="left", font=("Segoe UI", 8),
                  text="Step 1 - run the simulation at the full grid size and write "
                       "the DEM (.npy / .csv) to Procedural_generator/outputs.").pack(
            anchor="w", pady=(0, 4))

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

        ttk.Label(export_group, wraplength=wrap, justify="left", font=("Segoe UI", 8),
                  text="Step 2 - convert that DEM to Unreal's 16-bit .r16 heightmap, "
                       "then spawn the Landscape actor in the running editor. "
                       "Requires a full-resolution generation first.").pack(
            anchor="w", pady=(0, 4))

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
        self.stats_text = tk.Text(side, height=19, width=34, font=("Consolas", 8),
                                  relief="solid", borderwidth=1, wrap="none",
                                  background="#fbfbfb")
        self.stats_text.pack(fill="x", pady=(4, 10))
        self.stats_text.configure(state="disabled")

        ttk.Label(side, text="Unreal Landscape Topology",
                  style="Header.TLabel").pack(anchor="w")
        self.topology_text = tk.Text(side, height=9, width=34, font=("Consolas", 8),
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

    def _apply_params_to_widgets(self):
        for key, row in self.rows_widgets.items():
            row.set(self.params[key])
        seed = self.params.get('RANDOM_SEED')
        self.seed_var.set("" if seed is None else str(seed))

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
                elif kind == "error":
                    self.log(payload, level="error")
                    self.status_var.set("Failed")
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
        self.after(100, self._drain_queue)

    def log(self, text, level=None):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text.rstrip() + "\n", level or ())
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _log_from_worker(self, text, level=None):
        self._post("log", (text, level))

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

        pixels = params['ROWS'] * params['COLS']
        if pixels > 25_000_000:
            proceed = messagebox.askokcancel(
                "Large map",
                f"{params['COLS']} x {params['ROWS']} is {pixels / 1e6:.1f} megapixels.\n\n"
                "Crater placement and file writing scale with the map and can take a "
                "while, using multiple gigabytes of RAM.\n\n"
                "You can abort with the Stop button once it starts.\n\nContinue?")
            if not proceed:
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

    def run_unreal_dispatch(self):
        if not LOADER_AVAILABLE:
            self.log("Unreal loader modules are not importable.", level="error")
            return
        if self.full_result is None or 'elevation_npy' not in self.exported_paths:
            self.log("Generate a full-resolution map first "
                     "(the preview is not exported).", level="warn")
            return

        npy_path = self.exported_paths['elevation_npy']
        resolution = self.params['RESOLUTION_M_PER_PX']
        dispatch = self.dispatch_var.get()
        output_dir = lunar_loader.BASE_OUTPUT_DIRECTORY

        def job():
            os.makedirs(output_dir, exist_ok=True)
            self._progress_hook("Converting DEM to Unreal 16-bit heightmap", 0.2)

            # The loader prints its pipeline status; funnel it into the studio log.
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                config, interp_resolution, z_scale, heightmap = \
                    lunar_loader.generate_lunar_terrain(npy_path, output_dir, resolution)
            for line in buffer.getvalue().splitlines():
                if line.strip():
                    self._log_from_worker(line)

            self._log_from_worker(
                f"Heightmap written: {output_dir}/{heightmap}", level="ok")

            if not dispatch:
                self._progress_hook("Conversion complete (dispatch disabled)", 1.0)
                return

            # Cancellation checkpoint between the (uninterruptible) conversion and
            # the network dispatch.
            if self._cancel_event.is_set():
                raise terrain_core.TerrainCancelled()

            self._progress_hook("Searching for a running Unreal Editor", 0.7)
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                lunar_loader.send_to_unreal(config, interp_resolution, z_scale, heightmap)
            output = buffer.getvalue()
            for line in output.splitlines():
                if line.strip():
                    self._log_from_worker(line, level="error" if "[-]" in line else None)
            if "[-]" in output:
                self._log_from_worker(
                    "No editor responded. Enable the Python Editor Script Plugin and "
                    "'Remote Execution' in Project Settings, then retry.", level="warn")
            self._progress_hook("Dispatch complete", 1.0)

        self._start_worker(job, "Sending to Unreal...", cancellable=True)

    def open_output_folder(self):
        target = GENERATOR_OUTPUT_DIR
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
            if not messagebox.askokcancel(
                    "Job running", "A generation job is still running. Quit anyway?"):
                return
        self.destroy()


if __name__ == "__main__":
    enable_dpi_awareness()  # Must precede the creation of the Tk root window
    TerrainStudio().mainloop()

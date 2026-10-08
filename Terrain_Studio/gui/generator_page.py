"""
Generate view: the procedural terrain's parameters, the fast preview (relief,
3D surface, slope) and the full-resolution run. A mixin of the main window.
"""

import os
import queue
import subprocess
import threading
import traceback

import tkinter as tk
from tkinter import filedialog, ttk
import matplotlib
import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.cm import ScalarMappable
from matplotlib.colors import LightSource, Normalize
from matplotlib.figure import Figure
from matplotlib.ticker import MaxNLocator

from gui import paths
from gui.backend import terrain_core, heightmap_files, LOADER_AVAILABLE, LOADER_IMPORT_ERROR
from gui.dem_files import (DEM_FILETYPES, dem_source_note, dem_source_problem, drop_dem_menu,
                           planned_output_files)
from gui.landscape_page import UnrealUnavailable, convert_and_dispatch
from gui.map_views import MapViewer, SURFACE_DRAG_DETAIL, SURFACE_MESH_DETAIL
from gui.paths import map_browse_dir, output_group_dir
from gui.widgets import (ParamRow, ProgressPanel, Tooltip, ask_confirmation, format_path_block,
                         show_message)


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


class GeneratorPage:
    """
    The Generate view's methods, mixed into the main window (TerrainStudio):
    parameter column, preview panel, footer, presets, the generation workers
    and the preview drawing.
    """

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

        self.progress = ProgressPanel(footer)
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
        os.makedirs(paths.CONFIG_DIR, exist_ok=True)
        files = sorted(f for f in os.listdir(paths.CONFIG_DIR) if f.endswith((".yaml", ".yml")))
        self.preset_combo.configure(values=files + ["Browse..."])
        if files and not self.preset_var.get():
            self.preset_var.set(files[0])

    def _on_preset_selected(self, _event=None):
        choice = self.preset_var.get()
        if choice == "Browse...":
            path = filedialog.askopenfilename(
                title="Load terrain configuration", initialdir=paths.CONFIG_DIR,
                filetypes=[("YAML configuration", "*.yaml *.yml"), ("All files", "*.*")])
            if not path:
                self._refresh_preset_list()
                return
        else:
            path = os.path.join(paths.CONFIG_DIR, choice)
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
            title="Save terrain configuration", initialdir=paths.CONFIG_DIR,
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
        if os.path.dirname(os.path.abspath(path)) == os.path.abspath(paths.CONFIG_DIR):
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

        config = heightmap_files.calculate_optimal_landscape_config(cols, rows)
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

    def _start_worker(self, target, label, cancellable=False, key=None, work=1.0):
        if self._busy():
            self.log("A job is already running; wait for it to finish.", level="warn")
            return False
        self.progress.start(label, key, work)
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
        except UnrealUnavailable as exc:
            self.message_queue.put(("failed", str(exc)))
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
        self.progress.note("Cancelling...")
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
                    self.progress.update(*payload)
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
                elif kind in ("error", "failed"):
                    self.log(payload, level="error" if kind == "error" else "warn")
                    self.progress.finish("failed")
                elif kind == "dialog":
                    title, message = payload
                    show_message(self, title, message)
                elif kind == "cancelled":
                    self.log("Job cancelled.", level="warn")
                    self.progress.finish("cancelled")
                elif kind == "done":
                    for button in (self.preview_button, self.generate_button):
                        button.state(["!disabled"])
                    if LOADER_AVAILABLE:
                        self.unreal_button.state(["!disabled"])
                    self.stop_button.state(["disabled"])
                    self.progress.finish()
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

        self._start_worker(job, "Rendering preview...", key="preview")

    def run_full_generation(self):
        self._collect_params()
        seed = self._ensure_seed()
        params = dict(self.params)
        base_name = self.export_name_var.get().strip() or "procedural_lunar_terrain"
        elevation_name = f"{base_name}_elevationMap"

        # Describe the run and its outputs, and let the user confirm.
        # Outputs land in a per-run subfolder named after the base filename.
        pixels = params['ROWS'] * params['COLS']
        target_dir = os.path.join(paths.GENERATOR_OUTPUT_DIR, base_name)
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
                result['elevation'], paths.GENERATOR_OUTPUT_DIR, base_name=elevation_name,
                export_csv=params['EXPORT_CSV'], export_npy=True,
                progress=self._progress_hook, cancel_check=self._cancel_event.is_set,
                craters=result['craters'], params=params)
            self.exported_paths = written
            self._post("full", result)
            self._log_from_worker(
                f"Generated {result['crater_count']} craters.", level="ok")
            for label, path in written.items():
                self._log_from_worker(f"  wrote {label}: {path}")

        self._start_worker(job, "Generating full resolution...", cancellable=True,
                           key="generate", work=pixels / 1e6)

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

        self._start_worker(job, "Sending to Unreal...", cancellable=True, key="dispatch",
                           work=max(os.path.getsize(source_path) / 1e6, 0.01) if os.path.exists(source_path) else 1.0)

    def open_output_folder(self):
        base_name = self.export_name_var.get().strip() or "procedural_lunar_terrain"
        target = os.path.join(paths.OUTPUT_ROOT, base_name)
        target = target if os.path.isdir(target) else paths.OUTPUT_ROOT
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

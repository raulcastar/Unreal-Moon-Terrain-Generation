"""
Rock Planner view: turns an elevation map into a rock field with guaranteed
rover corridors, and places it in Unreal.
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
from matplotlib.colors import LightSource
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

from gui import paths
from gui.backend import (terrain_core, rock_field, heightmap_files, send_rocks, rock_models,
                         LOADER_AVAILABLE)
from gui.dem_files import (DEM_FILETYPES, dem_group_name, dem_source_note, drop_dem_menu,
                           load_dem_array, planned_rock_files)
from gui.ground_truth_page import DEFAULT_MASK_IDS, mask_ids
from gui.paths import map_browse_dir
from gui.widgets import ParamRow, ProgressPanel, Tooltip, ask_confirmation, format_path_block


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


class RockPlannerPage(ttk.Frame):
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
                "rocks are placed or their mask ids updated.")
        Tooltip(physics_check,
                "Off: rocks are static obstacles at their planned positions.\n"
                "On: rocks are movable rigid bodies and may settle or roll.")

        # Object-mask ids the placed rocks are tagged with (Custom Depth Stencil);
        # defaults from config/mask_classes.json (the GNC stereo node reads its copy).
        ttk.Label(unreal_group, text="Object mask ids",
                  font=("Segoe UI Semibold", 8)).pack(anchor="w", pady=(8, 2))
        stencil_grid = ttk.Frame(unreal_group)
        stencil_grid.pack(fill="x")
        stencil_grid.columnconfigure(0, weight=1)
        self.boulder_stencil_var = tk.StringVar()
        self.small_rock_stencil_var = tk.StringVar()
        self.boulder_radius_var = tk.StringVar()
        for row, (label, var, tip) in enumerate((
                ("Boulder id", self.boulder_stencil_var,
                 f"Mask id (1-255) for boulders; {DEFAULT_MASK_IDS['boulder']} is 'boulder' in "
                 "config/mask_classes.json."),
                ("Small rock id", self.small_rock_stencil_var,
                 f"Mask id (1-255) for small rocks; {DEFAULT_MASK_IDS['small_rock']} is 'small_rock' in\n"
                 "config/mask_classes.json. Use the boulder id here too for a single rock class."),
                ("Boulder if radius >= (m)", self.boulder_radius_var,
                 "Rocks whose planned footprint radius is at least this are boulders;\n"
                 "the rest are small rocks."))):
            name = ttk.Label(stencil_grid, text=label, font=("Segoe UI", 8))
            name.grid(row=row, column=0, sticky="w", pady=1)
            ttk.Entry(stencil_grid, textvariable=var, width=8, justify="right",
                      font=("Consolas", 9)).grid(row=row, column=1, sticky="e", pady=1)
            Tooltip(name, tip)

        # Class split of the exported field, to flag an empty class early.
        self.stencil_preview_var = tk.StringVar()
        self._wrapping_labels.append(ttk.Label(
            unreal_group, textvariable=self.stencil_preview_var, justify="left",
            style="Readout.TLabel"))
        self._wrapping_labels[-1].pack(anchor="w", pady=(4, 0))

        self.place_button = ttk.Button(unreal_group, text="Place Rocks in Unreal",
                                       command=self.place_in_unreal)
        self.place_button.pack(fill="x", pady=(6, 0))
        self.stencil_button = ttk.Button(unreal_group, text="Update Mask Ids Only",
                                         command=self.update_stencils_in_unreal)
        self.stencil_button.pack(fill="x", pady=(4, 0))
        Tooltip(self.stencil_button,
                "Re-tags rocks already placed in Unreal with the ids above,\n"
                "without moving or re-importing them.")
        if not LOADER_AVAILABLE:
            self.place_button.state(["disabled"])
            self.stencil_button.state(["disabled"])
        self.refresh_mask_ids()
        for var in (self.boulder_stencil_var, self.small_rock_stencil_var,
                    self.boulder_radius_var):
            var.trace_add("write", self._stencils_changed)
        self.export_name_var.trace_add("write", self._refresh_stencil_preview)
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
        self.progress = ProgressPanel(log_frame)
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
        self.progress.note("Cancelling...")
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
        self.progress.start(f"Loading {os.path.basename(path)}", "load_dem",
                            max(os.path.getsize(path) / 1e6, 0.01) if os.path.exists(path) else 1.0)

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
        self.progress.start("Exporting..." if export else "Planning...",
                            "rock_export" if export else "rock_plan", dem.size / 1e6)

        def worker():
            try:
                result = rock_field.build_rock_field(
                    dem, resolution, params, seed=seed,
                    progress=lambda m, f: self._queue.put(("progress", (m, f))),
                    cancel_check=self._cancel.is_set)
                self._queue.put(("result", result))
                if export:
                    written = rock_field.export_rock_field(
                        result, paths.GENERATOR_OUTPUT_DIR, f"{base_name}_elevationMap",
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
        group_dir = os.path.join(paths.GENERATOR_OUTPUT_DIR, base_name)
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
                                         initialdir=self.models_folder or paths.PROJECT_ROOT)
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
        return base_name, os.path.join(paths.GENERATOR_OUTPUT_DIR, base_name,
                                       f"{base_name}_rocks.csv")

    def refresh_mask_ids(self):
        """Loads the shared rock mask ids into the fields."""
        ids = mask_ids(self.app)
        self.boulder_stencil_var.set(str(ids["boulder"]))
        self.small_rock_stencil_var.set(str(ids["small_rock"]))
        self.boulder_radius_var.set(f"{ids['boulder_radius_m']:g}")
        self._refresh_stencil_preview()

    def _stencil_settings(self):
        """Returns (boulder_id, small_rock_id, boulder_min_radius_m), or None if invalid."""
        try:
            boulder = int(self.boulder_stencil_var.get())
            small = int(self.small_rock_stencil_var.get())
            radius = float(self.boulder_radius_var.get())
        except ValueError:
            return None
        if not (1 <= boulder <= 255 and 1 <= small <= 255 and radius >= 0):
            return None
        return boulder, small, radius

    def _stencils_changed(self, *_trace_args):
        settings = self._stencil_settings()
        if settings is not None:
            ids = dict(mask_ids(self.app))
            ids["boulder"], ids["small_rock"], ids["boulder_radius_m"] = settings
            self.app.mask_ids = ids         # Shared with Crater Masks' labels
        self._refresh_stencil_preview()

    def _refresh_stencil_preview(self, *_trace_args):
        """Updates the readout of how the exported field splits between the ids."""
        settings = self._stencil_settings()
        if settings is None:
            self.stencil_preview_var.set("Ids must be 1-255 and the radius >= 0.")
            return
        boulder, small, radius = settings
        _base, rocks_csv = self._rocks_csv_path()
        try:
            scales = np.loadtxt(rocks_csv, delimiter=",", skiprows=1, usecols=5, ndmin=1)
        except (OSError, ValueError, IndexError):
            self.stencil_preview_var.set("Export the rock field to see how it splits.")
            return
        n_boulder = int((scales >= radius).sum())
        text = (f"This field: {n_boulder} boulders (id {boulder}), "
                f"{len(scales) - n_boulder} small rocks (id {small}).")
        if len(scales) and n_boulder in (0, len(scales)):
            text += (f"\nAll rocks fall in one class: planned radii run "
                     f"{scales.min():.2f}-{scales.max():.2f} m.")
        self.stencil_preview_var.set(text)

    def _rock_count(self):
        """Rows in the exported rock list (the work an Unreal job scales with)."""
        try:
            with open(self._rocks_csv_path()[1]) as handle:
                return max(sum(1 for _ in handle) - 1, 1)
        except OSError:
            return 1

    def _start_unreal_job(self, status, job, key="rock_unreal"):
        """Runs `job(log)` on a worker thread, disabling the planner buttons meanwhile."""
        self.progress.start(status, key, self._rock_count())
        for button in (self.preview_button, self.export_button, self.place_button,
                       self.stencil_button):
            button.state(["disabled"])

        def worker():
            try:
                if job(lambda text, level=None: self._queue.put(("log", (text, level)))) is False:
                    self._queue.put(("job_failed", None))
            except Exception:
                self._queue.put(("log", (traceback.format_exc(), "error")))
            finally:
                self._queue.put(("done", None))

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()

    def update_stencils_in_unreal(self):
        """Re-tags the rocks placed in Unreal with the current mask ids."""
        if self._busy():
            self.log("A job is already running; wait for it to finish.", level="warn")
            return
        settings = self._stencil_settings()
        if settings is None:
            self.log("Mask ids must be whole numbers 1-255 and the radius >= 0.", level="error")
            return
        boulder, small, radius = settings
        base_name, _csv = self._rocks_csv_path()
        save_level = self.rock_save_level_var.get()
        self.log(f"Updating the mask ids of the '{base_name}' rocks in Unreal "
                 f"(boulder {boulder} if radius >= {radius:g} m, else {small}).")
        self._start_unreal_job(
            "Updating mask ids in Unreal...", key="rock_ids",
            job=
            lambda log: send_rocks.send_stencils_to_unreal(
                base_name, boulder, small, radius, save_level=save_level, log=log))

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
        settings = self._stencil_settings()
        if settings is None:
            self.log("Mask ids must be whole numbers 1-255 and the radius >= 0.", level="error")
            return
        boulder, small, radius = settings

        # The Landscape is labelled after its heightmap; its width is read from
        # the heightmap JSON.
        dem_stem = (os.path.splitext(os.path.basename(self.dem_path))[0]
                    if self.dem_path else base_name)
        width = heightmap_files.heightmap_width(
            os.path.join(paths.GENERATOR_OUTPUT_DIR, dem_group_name(dem_stem)), dem_stem)
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
            "Placing rocks in Unreal...", key="place_rocks",
            job=
            lambda log: send_rocks.send_rocks_to_unreal(
                rocks_csv, models_folder, resolution,
                landscape_label=f"{dem_stem}_heightmap", landscape_width=width,
                seed=seed, simulate_physics=physics, boulder_stencil=boulder,
                small_rock_stencil=small, boulder_min_radius_m=radius,
                save_level=save_level, log=log))

    def open_output_folder(self):
        base_name = self.export_name_var.get().strip() or "procedural_lunar_terrain"
        target = os.path.join(paths.GENERATOR_OUTPUT_DIR, base_name)
        target = target if os.path.isdir(target) else paths.GENERATOR_OUTPUT_DIR
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
                    self.progress.finish()
                    self.log(f"Loaded DEM {payload.shape[1]} x {payload.shape[0]} px.",
                             level="ok")
                    self.request_preview()
                elif kind == "progress":
                    message, fraction = payload
                    self.progress.update(message, fraction)
                elif kind == "result":
                    self.result = payload
                    self._render(payload)
                    self._log_result(payload)
                    self._refresh_rock_estimate()
                elif kind == "written":
                    self.log("Rock field written:", level="ok")
                    for label, path in payload.items():
                        self.log(f"  {label}: {path}")
                    self._refresh_stencil_preview()
                elif kind == "log":
                    text, level = payload
                    self.log(text, level=level)
                elif kind == "cancelled":
                    self.log("Job cancelled.", level="warn")
                    self.progress.finish("cancelled")
                elif kind == "job_failed":
                    self.progress.finish("failed")
                elif kind == "error":
                    self.log(payload, level="error")
                    self.progress.finish("failed")
                    self._show_message("Rock planning failed.\nSee the activity log.")
                elif kind == "done":
                    self.preview_button.state(["!disabled"])
                    self.export_button.state(["!disabled"])
                    if LOADER_AVAILABLE:
                        self.place_button.state(["!disabled"])
                        self.stencil_button.state(["!disabled"])
                    self.stop_button.state(["disabled"])
                    self.progress.finish()
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

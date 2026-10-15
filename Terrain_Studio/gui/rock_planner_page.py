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
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure

from gui import generator_logic
from gui import paths
from gui import rock_planner_logic as logic
from gui import rock_planner_plots as plots
from gui.rock_planner_logic import ROCK_PARAM_GROUPS
from gui.backend import (terrain_core, rock_field, crater_map, heightmap_files, send_rocks,
                         rock_models, LOADER_AVAILABLE)
from gui.dem_files import (DEM_FILETYPES, dem_group_name, dem_source_note, drop_dem_menu,
                           load_dem_array, planned_rock_files)
from gui.ground_truth_logic import DEFAULT_MASK_IDS, crater_list_for, crater_source_info, mask_ids
from gui.paths import map_browse_dir
from gui.widgets import (ParamRow, ProgressPanel, StepTabs, Tooltip, ask_confirmation,
                         format_path_block)


class RockPlannerPage(ttk.Frame):
    """
    Window that turns an elevation map into a rock field with guaranteed rover
    corridors. The user picks a DEM, tunes the rover and rock parameters, previews
    the plan (traversability, corridors, rocks), and exports the placement files.

    Computation runs in a worker thread and is drawn on the main thread through a
    queue, mirroring the studio; the plan itself lives in the UI-free rock_field
    module, the read-outs in rock_planner_logic and the drawing in
    rock_planner_plots.
    """

    def __init__(self, master, figure_dpi=100, source_path=None, resolution=1.0):
        super().__init__(master)
        self.app = master

        self.figure_dpi = figure_dpi
        self.dem = None               # Loaded elevation array
        self.dem_path = None
        self.crater_list_path = None  # the generator's <group>_craters.csv for this DEM, if any
        self.result = None            # Last rock_field.build_rock_field() output
        self.params = rock_field.default_params()
        self.rows_widgets = {}

        self._queue = queue.Queue()
        self._worker = None
        self._cancel = threading.Event()
        self._preview_job = None

        self._build_ui()
        self._apply_params_to_widgets()
        default_preset = os.path.join(paths.ROCK_CONFIG_DIR, paths.DEFAULT_ROCK_PRESET)
        if os.path.isfile(default_preset):
            self.load_preset(default_preset, preview=False)
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

        # Each step of the planner is a tab with its settings and, pinned beneath
        # them, its own buttons; the map they all work on stays above the tabs.
        self.steps = StepTabs(left, background=self.app.cget("background"),
                              on_width=self._rewrap_labels)
        plan, plan_actions = self.steps.add_step("Plan")
        self._build_preset_bar(plan)
        # Export and placing share one tab: Place Rocks in Unreal uses the field
        # Generate & Export wrote, so the two buttons sit together, with a line
        # saying whether that file still matches the settings.
        export_tab, export_actions = self.steps.add_step("Export & Unreal")
        unreal_tab, unreal_actions = export_tab, export_actions
        self.export_status_var = tk.StringVar()
        self.export_status_label = ttk.Label(export_actions, textvariable=self.export_status_var,
                                             justify="left", style="Readout.TLabel")
        self.export_status_label.pack(anchor="w", pady=(0, 4))
        self._wrapping_labels.append(self.export_status_label)

        self.preview_button = ttk.Button(plan_actions, text="Update Preview",
                                         style="Accent.TButton", command=self.request_preview)
        self.preview_button.pack(fill="x")
        self.export_button = ttk.Button(export_actions, text="Generate & Export",
                                        style="Accent.TButton", command=self.export_plan)
        self.export_button.pack(fill="x")
        self.open_folder_button = ttk.Button(export_actions, text="Open output folder",
                                             command=self.open_output_folder)


        # Source DEM
        source_group = ttk.Labelframe(left, text="Source DEM", padding=(8, 4, 8, 8),
                                      style="Group.TLabelframe")
        source_group.pack(fill="x", pady=(8, 6))
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
            group = ttk.Labelframe(plan, text=group_name, padding=(8, 4, 8, 8),
                                   style="Group.TLabelframe")
            group.pack(fill="x", pady=(0, 8))
            if group_name == "Crater Hazards":
                self._build_crater_source(group)
            for spec in specs:
                self.rows_widgets[spec[0]] = ParamRow(group, spec, self._on_param_change)
            if group_name == "Rock Population":
                # Rock count implied by the coverage, shown before planning.
                self.rock_estimate_var = tk.StringVar()
                self._wrapping_labels.append(ttk.Label(
                    group, textvariable=self.rock_estimate_var, justify="left",
                    style="Readout.TLabel"))
                self._wrapping_labels[-1].pack(anchor="w", pady=(4, 0))
                # The models are scaled to these sizes, so they sit with the size
                # parameters (with how much they are scaled); filled further down.
                self._models_group = ttk.Labelframe(plan, text="Rock Models",
                                                    padding=(8, 4, 8, 8), style="Group.TLabelframe")
                self._models_group.pack(fill="x", pady=(0, 8))

        seed_group = ttk.Labelframe(plan, text="Determinism", padding=(8, 4, 8, 8),
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
        # With the button it controls, pinned under the tab.
        ttk.Checkbutton(self.preview_button.master, text="Auto-update preview on change",
                        variable=self.auto_var).pack(anchor="w", pady=(0, 4),
                                                     before=self.preview_button)

        # Export options (the buttons themselves are pinned under the tab).
        export_group = ttk.Labelframe(export_tab, text="Export", padding=(8, 4, 8, 8),
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
        unreal_group = ttk.Labelframe(unreal_tab, text="Place in Unreal", padding=(8, 4, 8, 8),
                                      style="Group.TLabelframe")
        unreal_group.pack(fill="x", pady=(0, 4))
        models_group = self._models_group
        ttk.Label(models_group, text="Folder (.gltf, .glb, .obj, .fbx)",
                  font=("Segoe UI", 8)).pack(anchor="w")
        models_row = ttk.Frame(models_group)
        models_row.pack(fill="x", pady=(2, 2))
        self.models_folder_var = tk.StringVar(value="(none selected)")
        self.models_folder = None
        ttk.Entry(models_row, textvariable=self.models_folder_var, state="readonly",
                  font=("Consolas", 8)).pack(side="left", fill="x", expand=True)
        ttk.Button(models_row, text="Browse...", width=9,
                   command=self.browse_models_folder).pack(side="left", padx=(4, 0))
        self._wrapping_labels.append(ttk.Label(
            models_group, justify="left", style="HomeHint.TLabel",
            text="Subfolders too. glTF keeps its own materials; .obj/.fbx take the "
                 "texture with the same name. The models are what Unreal places."))
        self._wrapping_labels[-1].pack(anchor="w", pady=(0, 4))
        self._wrapping_labels.append(ttk.Label(
            unreal_group, justify="left", style="HomeHint.TLabel",
            text="Places the exported rock field with the models chosen on the Plan "
                 "tab. Send the heightmap to Unreal first: rocks are dropped onto the "
                 "Landscape's surface."))
        self._wrapping_labels[-1].pack(anchor="w", pady=(0, 6))

        # Model sizes versus planned sizes, to flag scale mismatches before placing.
        self.model_sizes = None       # rock_models.size_summary() of the folder
        self.model_radii = []         # Footprint radius (m) of each sized model
        self.model_info_var = tk.StringVar()
        self._wrapping_labels.append(ttk.Label(
            models_group, textvariable=self.model_info_var, justify="left",
            style="Readout.TLabel"))
        self._wrapping_labels[-1].pack(anchor="w", pady=(0, 4))

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

        self.place_button = ttk.Button(unreal_actions, text="Place Rocks in Unreal",
                                       style="Accent.TButton", command=self.place_in_unreal)
        self.place_button.pack(fill="x", pady=(4, 0))
        self.stencil_button = ttk.Button(unreal_actions, text="Update Mask Ids Only",
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

        self.open_folder_button.pack(fill="x", pady=(4, 0))       # last, under the Unreal buttons
        self._exported = (None, None)       # (rocks csv path, its saved settings), cached
        self._place_after_export = False
        self._export_ok = False
        self.after(500, self._refresh_export_status)

        self.steps.pack(fill="both", expand=True)
        # Mouse-wheel scrolling over each tab, bound locally (not app-wide) so it
        # never fights the main studio's own wheel scrolling.
        self.steps.bind_mousewheel()

        # Right: plot on top, log at bottom.
        right = ttk.Frame(self._panes)
        # Only the plot absorbs a resize; the parameter column keeps its width.
        self._panes.add(left, weight=0)
        self._panes.add(right, weight=1)

        log_frame = ttk.Frame(right, padding=(8, 4, 8, 8))
        log_head = ttk.Frame(log_frame)
        log_head.pack(fill="x")
        ttk.Label(log_head, text="Activity log",
                  font=("Segoe UI Semibold", 10)).pack(side="left")
        # Stop is next to the progress it stops: it ends whichever job runs,
        # a preview from Plan or an export from Export.
        self.stop_button = ttk.Button(log_head, text="Stop", width=8, command=self.stop_job)
        self.stop_button.pack(side="right")
        self.stop_button.state(["disabled"])
        Tooltip(self.stop_button,
                "Stops the running plan or export at its next checkpoint (slope,\n"
                "corridors, or rock placement). The DEM stays loaded so you can retune.")
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
        # The boulder threshold is a rock setting too (presets carry it), edited
        # with the mask ids on the Export & Unreal tab.
        stencils = self._stencil_settings() if hasattr(self, "boulder_radius_var") else None
        if stencils is not None:
            self.params['BOULDER_MIN_RADIUS_M'] = stencils[2]
        self.params = rock_field.coerce_params(self.params)
        return self.params

    def _on_param_change(self):
        self._refresh_rock_estimate()
        self._refresh_model_info()
        if self.auto_var.get():
            self.request_preview(debounce_ms=500)

    def _seed(self):
        return generator_logic.parse_seed(self.seed_var.get())

    def _resolution(self):
        return logic.parse_resolution(self.resolution_var.get())

    def _base_name(self):
        return generator_logic.base_name_or_default(self.export_name_var.get())

    # --------------------------------------------------------------- I/O
    def _refresh_output_preview(self, *_trace_args):
        """Lists the filenames an export will write, under the base filename field."""
        base_name = self._base_name()
        self.output_preview_var.set(generator_logic.output_preview_text(
            base_name, planned_rock_files(base_name, self.obstacle_map_var.get())))

    def _build_preset_bar(self, parent):
        """Rock presets (config/rocks/*.yaml): pick one to load it, or save the current values."""
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Label(bar, text="Preset", font=("Segoe UI", 8)).pack(side="left")
        self.preset_var = tk.StringVar()
        self.preset_combo = ttk.Combobox(bar, textvariable=self.preset_var, state="readonly")
        self.preset_combo.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self.preset_combo.bind("<<ComboboxSelected>>", self._on_preset_selected)
        Tooltip(self.preset_combo,
                "Rock field presets in config/rocks/: lunar_mare (typical surface, the\n"
                "default), lunar_highlands, blocky_ejecta, quick_test. Each file explains\n"
                "its values; Save as... writes the current ones as a new preset.")
        ttk.Button(bar, text="Save as...", width=10,
                   command=self.save_preset_as).pack(side="left", padx=(4, 0))
        self._refresh_presets()

    def _refresh_presets(self):
        self.preset_combo.configure(values=generator_logic.list_presets(paths.ROCK_CONFIG_DIR))

    def _on_preset_selected(self, _event=None):
        self.load_preset(os.path.join(paths.ROCK_CONFIG_DIR, self.preset_var.get()))

    def load_preset(self, path, preview=True):
        """Puts a preset's values in the controls (and previews, when a DEM is loaded)."""
        try:
            self.params = rock_field.load_preset(path)
        except Exception as exc:
            self.log(f"Could not load the rock preset {os.path.basename(path)}: {exc}", level="error")
            return
        self.preset_var.set(os.path.basename(path))
        self._apply_params_to_widgets()
        self.boulder_radius_var.set(f"{self.params['BOULDER_MIN_RADIUS_M']:g}")
        self._refresh_rock_estimate()
        self._refresh_model_info()
        self.log(f"Rock preset: {os.path.basename(path)}.", level="ok")
        if preview:
            self.request_preview()

    def save_preset_as(self):
        self._collect_params()
        path = filedialog.asksaveasfilename(
            parent=self, title="Save rock preset", initialdir=paths.ROCK_CONFIG_DIR,
            defaultextension=".yaml", initialfile="my_rock_field.yaml",
            filetypes=[("YAML preset", "*.yaml"), ("All files", "*.*")])
        if not path:
            return
        try:
            rock_field.save_preset(path, self.params)
        except Exception as exc:
            self.log(f"Could not save the rock preset: {exc}", level="error")
            return
        self._refresh_presets()
        if os.path.dirname(os.path.abspath(path)) == os.path.abspath(paths.ROCK_CONFIG_DIR):
            self.preset_var.set(os.path.basename(path))
        self.log(f"Saved rock preset: {path}", level="ok")

    def _build_crater_source(self, group):
        """Exact craters from the generator's list, or detected in the DEM (as in Crater Masks)."""
        self.crater_source_var = tk.StringVar(value="detect")
        self.generator_radio = ttk.Radiobutton(
            group, text="Exact, from the generator", value="generator",
            variable=self.crater_source_var, command=self._crater_source_changed)
        self.generator_radio.pack(anchor="w")
        Tooltip(self.generator_radio,
                "Terrains generated in this studio list every crater they placed\n"
                "(<map>_craters.csv): centre, rim radius and depth are exact.\n"
                "Craters at least the depth below deep are impassable and the corridors'\n"
                "sites; every crater's bowl, up to its rim, is kept free of rocks.")
        ttk.Radiobutton(group, text="Detected in the DEM", value="detect",
                        variable=self.crater_source_var,
                        command=self._crater_source_changed).pack(anchor="w")
        self.crater_source_info_var = tk.StringVar()
        self._wrapping_labels.append(ttk.Label(
            group, textvariable=self.crater_source_info_var, justify="left",
            style="Readout.TLabel"))
        self._wrapping_labels[-1].pack(anchor="w", pady=(2, 4))
        self._refresh_crater_source()

    def _use_generator_craters(self):
        return self.crater_source_var.get() == "generator" and self.crater_list_path is not None

    def _refresh_crater_source(self):
        has_list = self.crater_list_path is not None
        self.generator_radio.state(["!disabled"] if has_list else ["disabled"])
        if not has_list and self.crater_source_var.get() == "generator":
            self.crater_source_var.set("detect")
        self.crater_source_info_var.set(crater_source_info(
            has_list, self._use_generator_craters(),
            os.path.basename(self.crater_list_path) if has_list else None))

    def _crater_source_changed(self):
        self._refresh_crater_source()
        self.request_preview()

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
        # A map generated here comes with its exact craters: use them by default.
        self.crater_list_path = crater_list_for(path, dem_group_name(path))
        self.crater_source_var.set("generator" if self.crater_list_path else "detect")
        self._refresh_crater_source()

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
        base_name = self._base_name()
        crater_list = self.crater_list_path if self._use_generator_craters() else None
        settings = self._current_settings()

        self._cancel.clear()
        self.preview_button.state(["disabled"])
        self.export_button.state(["disabled"])
        self.stop_button.state(["!disabled"])
        self.progress.start("Exporting..." if export else "Planning...",
                            "rock_export" if export else "rock_plan", dem.size / 1e6)

        def worker():
            try:
                craters = (crater_map.load_crater_list(crater_list, with_depth=True)
                           if crater_list else None)
                result = rock_field.build_rock_field(
                    dem, resolution, params, seed=seed, craters=craters,
                    progress=lambda m, f: self._queue.put(("progress", (m, f))),
                    cancel_check=self._cancel.is_set)
                self._queue.put(("result", result))
                if export:
                    written = rock_field.export_rock_field(
                        result, paths.GENERATOR_OUTPUT_DIR, f"{base_name}_elevationMap",
                        export_obstacle_map=do_map,
                        progress=lambda m, f: self._queue.put(("progress", (m, f))),
                        cancel_check=self._cancel.is_set)
                    logic.save_settings(written.get("rocks_csv") or self._rocks_csv_path()[1], settings)
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
        base_name = self._base_name()
        group_dir = os.path.join(paths.GENERATOR_OUTPUT_DIR, base_name)
        message = logic.export_confirmation(
            os.path.basename(self.dem_path),
            planned_rock_files(base_name, self.obstacle_map_var.get()),
            format_path_block(group_dir))
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
            return
        if not self.model_sizes:
            self.model_info_var.set(logic.model_info_text(None, None, 0.0))
            return
        self.model_info_var.set(logic.model_info_text(
            self.model_sizes, self._model_scale_range(),
            self.rows_widgets['ROCK_HEIGHT_RATIO'].get()))

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
        count = rock_field.estimate_rock_count(params, area)
        self.rock_estimate_var.set(logic.rock_estimate_text(
            count, rock_field.MAX_ROCKS, bool(self.result)))

    def _rocks_csv_path(self):
        base_name = self._base_name()
        return base_name, logic.rocks_csv_path(paths.GENERATOR_OUTPUT_DIR, base_name)

    def refresh_mask_ids(self):
        """Loads the shared rock mask ids into the fields."""
        ids = mask_ids(self.app)
        self.boulder_stencil_var.set(str(ids["boulder"]))
        self.small_rock_stencil_var.set(str(ids["small_rock"]))
        self.boulder_radius_var.set(f"{ids['boulder_radius_m']:g}")
        self._refresh_stencil_preview()

    def _stencil_settings(self):
        """Returns (boulder_id, small_rock_id, boulder_min_radius_m), or None if invalid."""
        return logic.parse_stencils(self.boulder_stencil_var.get(),
                                    self.small_rock_stencil_var.get(),
                                    self.boulder_radius_var.get())

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
            scales = logic.read_rock_scales(rocks_csv)
        except (OSError, ValueError, IndexError):
            self.stencil_preview_var.set("Export the rock field to see how it splits.")
            return
        self.stencil_preview_var.set(logic.stencil_split_text(scales, boulder, small, radius))

    def _rock_count(self):
        """Rows in the exported rock list (the work an Unreal job scales with)."""
        return logic.count_rows(self._rocks_csv_path()[1])

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
            self.log(logic.STENCIL_RULE, level="error")
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

    def _current_settings(self):
        self._collect_params()
        return logic.export_settings(self.params, self._seed(), self._resolution(), self.dem_path,
                                     "generator" if self._use_generator_craters() else "detected")

    def _export_changes(self):
        """(rocks csv exists, what changed since its export or None if unrecorded)."""
        _base, rocks_csv = self._rocks_csv_path()
        if not os.path.isfile(rocks_csv):
            return False, []
        if self._exported[0] != rocks_csv or self._exported[1] is None:
            self._exported = (rocks_csv, logic.load_settings(rocks_csv))
        return True, logic.settings_changes(self._exported[1], self._current_settings())

    def _refresh_export_status(self):
        """Keeps the line under the Export & Unreal buttons current."""
        if not self.winfo_exists():
            return
        if self.dem_path:
            text, level = logic.export_status(*self._export_changes())
            self.export_status_var.set(text)
            self.export_status_label.configure(foreground="#2e7d32" if level == "ok" else "#b8860b")
        else:
            self.export_status_var.set("")
        self.after(700, self._refresh_export_status)

    def place_in_unreal(self, checked=False):
        """Sends the exported rock field to a running Unreal Editor."""
        if self._busy():
            self.log("A job is already running; wait for it to finish.", level="warn")
            return
        if not self.models_folder:
            self.log("Choose the rock models folder first (Plan tab, Rock Models).", level="warn")
            return
        base_name, rocks_csv = self._rocks_csv_path()
        exists, changes = self._export_changes()
        if not checked and self.dem is not None and (not exists or changes is None or changes):
            # The file on disk is not what the controls describe (or cannot be told):
            # export the current settings first, then place.
            reason = ("No rock field has been exported for this map yet." if not exists else
                      "The exported rock field's settings were not recorded." if changes is None else
                      "The settings changed since the last export: " + ", ".join(changes) + ".")
            if ask_confirmation(self, "Export the current rock field first?",
                                reason + "\n\nPlacing uses the exported file, so it would not show "
                                "the current settings. Export them now, then place?",
                                ok_label="Export, then Place"):
                self._place_after_export = True
                self._launch(export=True)
            return
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
            self.log(logic.STENCIL_RULE, level="error")
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
        base_name = self._base_name()
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
                    self._export_ok = True
                    self._exported = (None, None)          # re-read the saved settings
                    self.log("Rock field written:", level="ok")
                    for label, path in payload.items():
                        self.log(f"  {label}: {path}")
                    self._refresh_stencil_preview()
                elif kind == "log":
                    text, level = payload
                    self.log(text, level=level)
                elif kind == "cancelled":
                    self._place_after_export = False
                    self.log("Job cancelled.", level="warn")
                    self.progress.finish("cancelled")
                elif kind == "job_failed":
                    self.progress.finish("failed")
                elif kind == "error":
                    self._place_after_export = False
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
                    if self._place_after_export and self._export_ok:
                        self._place_after_export = False
                        self.after(200, lambda: self.place_in_unreal(checked=True))
                    self._place_after_export = self._export_ok = False
        except queue.Empty:
            pass
        self.after(80, self._drain_queue)

    def _log_result(self, result):
        for text, level in logic.result_log_lines(result, rock_field.MAX_ROCKS):
            self.log(text, level=level)

    def log(self, text, level=None):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text.rstrip() + "\n", level or ())
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ---------------------------------------------------------- drawing
    def _show_message(self, text):
        plots.draw_message(self.figure, text)
        self.canvas.draw_idle()

    def _render(self, result):
        """Draws the hillshade with the impassable, corridor and rock overlays."""
        plots.draw_plan(self.figure, result)
        self.canvas.draw_idle()

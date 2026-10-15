"""
Generate view: the procedural terrain's parameters, the fast preview (relief,
3D surface, slope) and the full-resolution run. A mixin of the main window.

This file is the Tk side: layout, events and the background jobs. The
parameter schema, seeds and read-outs are in generator_logic, the drawing in
generator_plots.
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

from gui import generator_logic as logic
from gui import generator_plots as plots
from gui import paths
from gui.generator_logic import COLORMAPS, PARAM_GROUPS, PREVIEW_SIZES  # noqa: F401  (used by other pages)
from gui.backend import terrain_core, heightmap_files, LOADER_AVAILABLE, LOADER_IMPORT_ERROR
from gui.dem_files import (DEM_FILETYPES, dem_source_note, dem_source_problem, drop_dem_menu,
                           planned_output_files)
from gui.landscape_page import UnrealUnavailable, convert_and_dispatch
from gui.map_views import MapViewer, SURFACE_DRAG_DETAIL, SURFACE_MESH_DETAIL
from gui.paths import map_browse_dir, output_group_dir
from gui.widgets import (ParamRow, ProgressPanel, Tooltip, ask_confirmation, format_path_block,
                         show_message)


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
        # The scrollbar is packed first: packed after an expanding canvas it is the
        # one squeezed out when the panel is narrow, and the panel no longer shows
        # that it scrolls (the controls below the window looked missing).
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

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
        base_name = logic.base_name_or_default(self.export_name_var.get())
        files = planned_output_files(base_name, self.params.get('EXPORT_CSV', True))
        self.output_preview_var.set(logic.output_preview_text(base_name, files))

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

        self.params['RANDOM_SEED'] = logic.parse_seed(self.seed_var.get())
        if self.params['RANDOM_SEED'] is None and self.seed_var.get().strip():
            self.seed_var.set("")                    # not a number: cleared

        self.params = terrain_core.coerce_params(self.params)
        return self.params

    def on_param_change(self):
        """Invalidates the current render and schedules a debounced preview."""
        self._collect_params()
        self._refresh_derived_readouts()
        if self.auto_preview_var.get():
            self.request_preview(debounce_ms=450)

    def roll_seed(self):
        self.seed_var.set(str(logic.roll_seed()))
        self.on_param_change()

    def _ensure_seed(self):
        """
        Guarantees an explicit seed so the preview and the full-resolution export
        describe the same surface. A blank field is filled with a rolled value.
        """
        if self.params.get('RANDOM_SEED') is None:
            seed = logic.roll_seed()
            self.params['RANDOM_SEED'] = seed
            self.seed_var.set(str(seed))
        return self.params['RANDOM_SEED']

    # =====================================================================
    # PRESETS
    # =====================================================================

    def _refresh_preset_list(self):
        files = logic.list_presets(paths.CONFIG_DIR)
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
        lines = logic.stats_lines(self.params, terrain_core.full_crater_count(self.params),
                                  self.displayed, self.displayed is self.full_result)
        self._set_text(self.stats_text, "\n".join(lines))
        self._refresh_topology_readout()

    def _refresh_topology_readout(self):
        """Mirrors the loader's component solver so the UI can warn before export."""
        if not LOADER_AVAILABLE:
            text = logic.topology_text(self.params, None, LOADER_IMPORT_ERROR)
        else:
            text = logic.topology_text(self.params, heightmap_files.calculate_optimal_landscape_config(
                self.params['COLS'], self.params['ROWS']))
        self._set_text(self.topology_text, text)

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
        preview_rows, preview_cols = logic.preview_shape(
            self.params['ROWS'], self.params['COLS'], int(self.preview_size_var.get()))
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
        base_name = logic.base_name_or_default(self.export_name_var.get())
        elevation_name = f"{base_name}_elevationMap"

        # Describe the run and its outputs, and let the user confirm.
        # Outputs land in a per-run subfolder named after the base filename.
        pixels = params['ROWS'] * params['COLS']
        target_dir = os.path.join(paths.GENERATOR_OUTPUT_DIR, base_name)
        message = logic.generation_confirmation(
            params, seed, format_path_block(target_dir),
            planned_output_files(base_name, params['EXPORT_CSV']))

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
        base_name = logic.base_name_or_default(self.export_name_var.get())
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
        extent, effective_res = logic.display_geometry(elevation, self.params)
        return elevation, extent, effective_res

    def _title_suffix(self):
        return logic.title_suffix(self.displayed, self.displayed is self.full_result)

    def _draw_relief(self):
        elevation, extent, effective_res = self._display_geometry()
        plots.draw_relief(self.figures['relief'], elevation, extent, effective_res,
                          self.cmap_var.get(), self.sun_azimuth_var.get(),
                          self.sun_altitude_var.get(), self.exaggeration_var.get(),
                          self._title_suffix())

    def _draw_surface(self):
        elevation, extent, effective_res = self._display_geometry()
        # (fine, coarse) meshes, swapped during a drag (see _on_surface_press)
        self._surface_artists = plots.draw_surface(
            self.figures['surface'], elevation, extent, effective_res, self.cmap_var.get(),
            self.sun_azimuth_var.get(), self.sun_altitude_var.get(), self._title_suffix(),
            SURFACE_MESH_DETAIL, SURFACE_DRAG_DETAIL)

    def _draw_slope(self):
        elevation, extent, effective_res = self._display_geometry()
        plots.draw_slope(self.figures['slope'], elevation, extent, effective_res,
                         self._title_suffix())

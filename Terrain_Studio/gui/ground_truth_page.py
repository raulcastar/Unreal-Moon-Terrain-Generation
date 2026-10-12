"""
Ground Truth view: crater labels for the rover's object mask, shadow, sun and
wheel-track ids, the mask camera, and the ids GNC uses.
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

from gui import ground_truth_logic as logic
from gui import ground_truth_plots as plots
from gui import paths
from gui import rock_planner_logic
from gui.ground_truth_logic import (DEFAULT_MASK_IDS, GNC_CLASSES_FILE, LUG_SPACINGS_CM,  # noqa: F401
                                    SUN_GLARE_ID, TRACK_PATTERNS, mask_ids)
from gui.backend import rock_field, crater_map, heightmap_files, send_masks, LOADER_AVAILABLE
from gui.dem_files import DEM_FILETYPES, dem_group_name, drop_dem_menu, load_dem_array
from gui.paths import map_browse_dir
from gui.widgets import ParamRow, ProgressPanel, Tooltip

import mask_classes  # noqa: E402  (unreal_link, put on the path by gui.paths)


class GroundTruthPage(ttk.Frame):
    """
    Crater labels for the rover's object mask. Maps the craters on a DEM,
    exports the crater map, and sends it to Unreal, where the mask material
    draws it on the Landscape for the chosen mask camera. Also lists the GNC
    mask labels for every id in use. The ids, classes and read-outs are in
    ground_truth_logic, the drawing in ground_truth_plots.
    """

    def __init__(self, master, figure_dpi=100, source_path=None, resolution=1.0):
        super().__init__(master)
        self.app = master
        self.figure_dpi = figure_dpi
        self.dem = None
        self.dem_path = None
        self.craters = None           # [(row, col, radius)] for the loaded DEM
        self._crater_depth = None     # Min depth they were found with, or "generator"
        self.crater_list_path = None  # The generator's crater list for this map, if any
        self._cameras_listed = False
        self._queue = queue.Queue()
        self._worker = None

        self._build_ui()
        self.resolution_var.set(f"{resolution:g}")
        self._load_ids()
        self.after(80, self._drain_queue)
        self._source_changed()
        self.log("Find the craters, pick the mask camera, then send them to Unreal.")
        if source_path and os.path.exists(source_path):
            self.load_dem(source_path)
        self.after_idle(self._place_sash)
        if LOADER_AVAILABLE:
            self.after(300, lambda: self.find_cameras(quiet=True))

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
        ttk.Label(top_bar, text="Crater & Shadow Masks",
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

        self._hint(inner, "Shows craters in the rover's object mask "
                          "(navcam/central/object_mask): the mask camera's material draws "
                          "this crater map on the Landscape. Rocks get their ids when they "
                          "are placed (step 2).", pady=(0, 8))

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

        # Craters
        craters = self._group(inner, "Craters")
        self.crater_source_var = tk.StringVar(value="detect")
        self.generator_radio = ttk.Radiobutton(
            craters, text="Exact, from the generator", value="generator",
            variable=self.crater_source_var, command=self._source_changed)
        self.generator_radio.pack(anchor="w")
        Tooltip(self.generator_radio,
                "Terrains generated in this studio list every crater they placed\n"
                "(<map>_craters.csv): centres and radii are exact, nothing is guessed.\n"
                "The rim crest is at the crater radius; the ejecta rim reaches 1.5 x.")
        detect_radio = ttk.Radiobutton(
            craters, text="Detected in the DEM", value="detect",
            variable=self.crater_source_var, command=self._source_changed)
        detect_radio.pack(anchor="w")
        Tooltip(detect_radio,
                "For maps from elsewhere: closed depressions are fitted to their rim\n"
                "crests (min depth below).")
        self.crater_source_info_var = tk.StringVar()
        self._readout(craters, self.crater_source_info_var)
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
        self.rim_row.set(crater_map.RIM_FRACTION)
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

        # Shadows
        shadows = self._group(inner, "Shadows")
        self.shadows_var = tk.BooleanVar(value=True)
        shadow_check = ttk.Checkbutton(shadows, text="Stream a shadow mask feed",
                                       variable=self.shadows_var)
        shadow_check.pack(anchor="w")
        Tooltip(shadow_check,
                "Extra feeds, navcam/central/shadow_mask (colours) and shadow_label (ids):\n"
                "every surface without sunlight (shadows of the rover, rocks, crater walls\n"
                "and hills, and slopes facing away). The object mask stays free of shadows.")
        self.rover_shadow_var = tk.BooleanVar(value=True)
        rover_check = ttk.Checkbutton(shadows, text="Tag the rover's shadow separately (id 5)",
                                      variable=self.rover_shadow_var)
        rover_check.pack(anchor="w")
        Tooltip(rover_check,
                "Adds the rover's shadow map to the level: a small depth-only capture of\n"
                "the mask camera's actor (the rover) seen from the sun, which the editor\n"
                "watcher keeps on the rover. A shadowed pixel is the rover's shadow when\n"
                "the rover lies between it and the sun, even where rocks, crater walls or\n"
                "slopes shade it too. Needs the editor (not Play).")
        self._hint(shadows, "The mask camera renders sunlight alone for this, so the shadows "
                            "match what the cameras see. Both feeds come from the same frame.")
        self._hint(shadows, "Shadow ids (navcam/central/shadow_label):", pady=(6, 0))
        self.shadow_ids_text = tk.Text(shadows, height=6, font=("Consolas", 8), relief="solid",
                                       borderwidth=1, wrap="none")
        self.shadow_ids_text.pack(fill="x", pady=(4, 0))
        for var in (self.shadows_var, self.rover_shadow_var):
            var.trace_add("write", self._refresh_shadow_ids)
        self._refresh_shadow_ids()

        # Sun
        sun = self._group(inner, "Sun")
        self.sun_var = tk.BooleanVar(value=True)
        sun_check = ttk.Checkbutton(sun, text=f"Tag the sun's disc in the object mask (id {mask_classes.id_of('sun')})",
                                    variable=self.sun_var)
        sun_check.pack(anchor="w")
        Tooltip(sun_check,
                "Sky pixels within the sun's disc get their own id in object_mask.\n"
                "Its direction and size (Source Angle) come from the level's Directional\n"
                "Light and follow it live, also during Play. The shadow feeds keep it as sky.")
        self.sun_glare_var = tk.StringVar(value="1.1")
        self._id_rows(sun, (
            ("Glare radius (deg)", self.sun_glare_var,
             f"Sky around the disc out to this angle gets id {SUN_GLARE_ID} (sun_glare): the part\n"
             "the navcam washes out with bloom. 1.1 matches its saturated core (about\n"
             "16 px across); half brightness reaches about 3.5 deg. 0 for no glare ring."),))
        self.sun_var.trace_add("write", self._ids_changed)
        self.sun_glare_var.trace_add("write", self._ids_changed)

        # Wheel tracks
        tracks = self._group(inner, "Wheel Tracks")
        self.tracks_var = tk.BooleanVar(value=True)
        tracks_check = ttk.Checkbutton(
            tracks, text=f"Rover leaves wheel tracks (id {mask_classes.id_of('wheel_track')})", variable=self.tracks_var)
        tracks_check.pack(anchor="w")
        Tooltip(tracks_check,
                "Places a pool of track decals in the level (hidden). During Play each wheel\n"
                "of the mask camera's actor that touches the Landscape leaves a track: a\n"
                "shallow rut with the tread below, in packed regolith that keeps the ground's\n"
                "grain. About 60 m are kept per wheel, the oldest reused; tracks start afresh\n"
                "each Play. Track pixels get their own id in object_mask.")
        self.tracks_var.trace_add("write", self._ids_changed)

        self.track_pattern_var = tk.StringVar(value=TRACK_PATTERNS[0][0])
        self.track_spacing_var = tk.StringVar(value="3")
        self.track_relief_var = tk.DoubleVar(value=1.0)
        self.track_shade_var = tk.DoubleVar(value=0.7)
        form = ttk.Frame(tracks)
        form.pack(fill="x", pady=(4, 0))
        form.columnconfigure(1, weight=1)
        rows = (
            ("Tread pattern", ttk.Combobox(form, textvariable=self.track_pattern_var, state="readonly",
                                           values=[label for label, _ in TRACK_PATTERNS], width=16),
             "The imprint the wheels leave: straight lugs across the track, chevrons (V),\n"
             "staggered blocks (left and right halves offset), or a smooth band."),
            ("Lug spacing (cm)", ttk.Combobox(form, textvariable=self.track_spacing_var, state="readonly",
                                              values=LUG_SPACINGS_CM, width=16),
             "Distance between lugs along the track. These all divide the 12 cm decal\n"
             "spacing, so the tread stays continuous."),
            ("Tread relief", ttk.Scale(form, variable=self.track_relief_var, from_=0.0, to=2.0),
             "How strongly the lugs show: 0 none, 1 default, 2 deep imprints."),
            ("Regolith shade", ttk.Scale(form, variable=self.track_shade_var, from_=0.5, to=1.0),
             "Colour of the packed regolith relative to the ground's texture: 1 the same,\n"
             "0.7 default (tracks read about 9% darker), 0.5 dark tracks."))
        for row, (label, widget, tip) in enumerate(rows):
            name = ttk.Label(form, text=label, font=("Segoe UI", 8))
            name.grid(row=row, column=0, sticky="w", pady=1)
            widget.grid(row=row, column=1, sticky="ew", padx=(6, 0), pady=1)
            Tooltip(name, tip)
        self.track_style_info = tk.StringVar()
        self._readout(tracks, self.track_style_info)
        self.track_preview = tk.Canvas(tracks, height=46, highlightthickness=0, background="#d9d6d0")
        self.track_preview.pack(fill="x", pady=(4, 0))
        Tooltip(self.track_preview, "Preview of one wheel's track from above (18 cm, travel left to right).")
        self.track_style_button = ttk.Button(tracks, text="Apply Track Look", command=self.apply_track_style)
        self.track_style_button.pack(fill="x", pady=(4, 0))
        Tooltip(self.track_style_button,
                "Updates only the tread look in Unreal (the track decal material), also during\n"
                "Play. Send Masks to Unreal applies it too.")
        for var in (self.track_pattern_var, self.track_spacing_var, self.track_relief_var,
                    self.track_shade_var):
            var.trace_add("write", self._refresh_track_preview)
        self.track_preview.bind("<Configure>", self._refresh_track_preview)
        self._refresh_track_preview()

        # Mask camera and sending
        camera = self._group(inner, "Mask Camera in Unreal")
        self._hint(camera, "The scene capture that renders the object mask. It gets "
                           "PPM_GroundTruthMask and, if its actor has an UnrealStereoCam, is "
                           "streamed as the mask camera.")
        camera_row = ttk.Frame(camera)
        camera_row.pack(fill="x", pady=(4, 0))
        self.camera_var = tk.StringVar(value="Leo_Raph0 / Navcam_Mask")
        self.camera_box = ttk.Combobox(camera_row, textvariable=self.camera_var,
                                       font=("Consolas", 8))
        self.camera_box.pack(side="left", fill="x", expand=True)
        self.cameras_button = ttk.Button(camera_row, text="Find", width=6,
                                         command=self.find_cameras)
        self.cameras_button.pack(side="left", padx=(4, 0))
        Tooltip(self.cameras_button, "Lists the scene-capture cameras in the open Unreal level.")
        self.camera_info_var = tk.StringVar()
        self._readout(camera, self.camera_info_var)
        self.save_level_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(camera, text="Save the level afterwards",
                        variable=self.save_level_var).pack(anchor="w", pady=(4, 0))
        self.send_button = ttk.Button(camera, text="Send Masks to Unreal",
                                      style="Accent.TButton", command=self.send_crater_map)
        self.send_button.pack(fill="x", pady=(6, 0))
        Tooltip(self.send_button,
                "Exports the crater map if needed, rebuilds PPM_GroundTruthMask so Landscape\n"
                "pixels take the crater ids (rocks keep theirs), and sets up the mask\n"
                "camera above. The original material is kept as PPM_GroundTruthMask_Backup.")
        self.restore_button = ttk.Button(camera, text="Restore Original Mask Material",
                                         command=self.restore_material)
        self.restore_button.pack(fill="x", pady=(4, 0))
        Tooltip(self.restore_button,
                "Puts back PPM_GroundTruthMask as it was before the studio first changed it\n"
                "(from PPM_GroundTruthMask_Backup) and removes the rover-shadow helper.")

        # GNC labels
        labels = self._group(inner, "GNC Labels")
        self._hint(labels, f"Object-mask classes (config/mask_classes.json and the ids above).\n"
                           f"The GNC stereo node reads its copy, {GNC_CLASSES_FILE}:")
        self.labels_text = tk.Text(labels, height=11, font=("Consolas", 8), relief="solid",
                                   borderwidth=1, wrap="none")
        self.labels_text.pack(fill="x", pady=(4, 0))
        self.labels_status = ttk.Label(labels, text="", wraplength=360, justify="left")
        self.labels_status.pack(fill="x", pady=(4, 0))
        copy = ttk.Button(labels, text="Copy", command=self.copy_labels)
        copy.pack(anchor="e", pady=(4, 0))
        Tooltip(copy, "Copies a complete mask_classes.json with these ids, to paste into\n"
                      "the GNC copy (and into config/ here to keep changed ids).")

        for var in (self.crater_var, self.rim_var):
            var.trace_add("write", self._ids_changed)
        self._unreal_buttons = (self.send_button, self.restore_button, self.track_style_button)
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
        self.progress = ProgressPanel(log_frame)
        self.progress.pack(fill="x", pady=(4, 0))
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
        self._message("Load a DEM to see its craters.")

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
        ids = dict(DEFAULT_MASK_IDS, **mask_ids(self.app))
        self.crater_var.set(str(ids["crater"]))
        self.rim_var.set(str(ids["rim"]))

    def refresh_ids(self):
        """Re-reads the shared ids (rock ids may have changed in the rock planner)."""
        self._load_ids()
        self._ids_changed()

    def _read_ids(self):
        """All mask ids: rocks from the shared ids, craters from the fields; None if invalid."""
        return logic.parse_crater_ids(mask_ids(self.app), self.crater_var.get(), self.rim_var.get())

    def _ids_changed(self, *_trace_args):
        ids = self._read_ids()
        if ids is not None:
            self.app.mask_ids = ids         # Shared with the rock planner
        self._refresh_labels(ids)
        self._redraw()

    def _refresh_labels(self, ids):
        self.labels_text.configure(state="normal")
        self.labels_text.delete("1.0", "end")
        self._classes = None
        if ids is None:
            self.labels_text.insert("end", logic.CRATER_ID_RULE)
            status = ""
        else:
            # Every class, in order (an id stays reserved when its option is off).
            self._classes = logic.effective_classes(ids)
            self.labels_text.insert("end", "\n".join(logic.class_lines(self._classes)))
            status = logic.gnc_status(
                mask_classes.gnc_difference({c["id"]: c["name"] for c in self._classes}))
        self.labels_status.configure(text=status)
        self.labels_text.configure(state="disabled")

    def _refresh_shadow_ids(self, *_trace_args):
        lines = logic.shadow_id_lines(self.shadows_var.get(), self.rover_shadow_var.get())
        self.shadow_ids_text.configure(state="normal", height=len(lines))
        self.shadow_ids_text.delete("1.0", "end")
        self.shadow_ids_text.insert("end", "\n".join(lines))
        self.shadow_ids_text.configure(state="disabled")

    def _glare_deg(self):
        """Glare ring radius in degrees from the field; 0 when blank or invalid."""
        return logic.parse_glare_deg(self.sun_glare_var.get())

    def copy_labels(self):
        if not self._classes:
            self.log("Fix the crater ids first.", level="warn")
            return
        self.clipboard_clear()
        self.clipboard_append(logic.classes_document(mask_classes.CLASSES_FILE, self._classes))
        self.log("mask_classes.json copied to the clipboard.", level="ok")

    # --------------------------------------------------------------- files
    def _group_name(self):
        return dem_group_name(self.dem_path) if self.dem_path else None

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
        self.crater_list_path = logic.crater_list_for(path, dem_group_name(path))
        self.crater_source_var.set("generator" if self.crater_list_path else "detect")
        self._source_changed()
        self.log(f"Loading DEM: {os.path.basename(path)} ...")
        self._message(f"Loading {os.path.basename(path)} ...")
        self.progress.start(f"Loading {os.path.basename(path)}", "load_dem",
                            max(os.path.getsize(path) / 1e6, 0.01) if os.path.exists(path) else 1.0)

        def worker():
            try:
                self._queue.put(("dem", load_dem_array(path)))
            except Exception as exc:
                self._queue.put(("log", (f"Could not load DEM: {exc}", "error")))
                self._queue.put(("failed", None))
        threading.Thread(target=worker, daemon=True).start()

    def _resolution(self):
        return rock_planner_logic.parse_resolution(self.resolution_var.get())

    def open_output_folder(self):
        group = self._group_name()
        target = os.path.join(paths.GENERATOR_OUTPUT_DIR, group) if group else paths.GENERATOR_OUTPUT_DIR
        target = target if os.path.isdir(target) else paths.GENERATOR_OUTPUT_DIR
        os.makedirs(target, exist_ok=True)
        try:
            os.startfile(target)  # noqa: S606 - Windows shell open
        except AttributeError:  # pragma: no cover - non-Windows fallback
            subprocess.Popen(["xdg-open", target])

    # ------------------------------------------------------------- craters
    def _busy(self):
        return self._worker is not None and self._worker.is_alive()

    def _run(self, status, job, key=None, work=1.0):
        """Runs `job(log)` on a worker thread with the buttons disabled."""
        if self._busy():
            self.log("A job is already running; wait for it to finish.", level="warn")
            return
        self.progress.start(status, key, work)
        for button in (self.find_button, self.export_button) + self._unreal_buttons:
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

    def _from_generator(self):
        return self.crater_source_var.get() == "generator" and self.crater_list_path is not None

    def _source_changed(self):
        """Shows which crater source is in use and enables what it needs."""
        has_list = self.crater_list_path is not None
        self.generator_radio.state(["!disabled"] if has_list else ["disabled"])
        if not has_list and self.crater_source_var.get() == "generator":
            self.crater_source_var.set("detect")
        generator = self._from_generator()
        for widget in (self.depth_row.slider, self.depth_row.entry):
            widget.state(["disabled"] if generator else ["!disabled"])
        self.find_button.configure(text="Load Craters" if generator else "Find Craters")
        self.crater_source_info_var.set(logic.crater_source_info(
            has_list, generator, os.path.basename(self.crater_list_path) if has_list else None))
        if self.craters is not None and self._crater_depth != self._crater_key():
            self.craters = None
            self.crater_info_var.set("Crater source changed: press "
                                     f"{self.find_button.cget('text')}.")
            self._redraw()

    def _crater_key(self):
        """What the current craters must have been made with to be reused."""
        return "generator" if self._from_generator() else self.depth_row.get()

    def _craters_changed(self):
        if self.craters is not None:
            self.crater_info_var.set("Depth changed: Find Craters again.")

    def find_craters(self, then=None):
        if self.dem is None:
            self.log("Load a DEM first.", level="warn")
            return
        if self._from_generator():
            path = self.crater_list_path

            def load(log):
                craters = crater_map.load_crater_list(path)
                log(f"Loaded the generator's crater list: {os.path.basename(path)}")
                self._queue.put(("craters", (craters, "generator", then)))
            self._run("Loading craters...", load, "load_crater_list")
            return
        dem, resolution, depth = self.dem, self._resolution(), self.depth_row.get()

        def job(log):
            log(f"Finding craters (min depth {depth:g} m) ...")
            craters = crater_map.find_craters(
                dem, resolution, depth,
                progress=lambda m, f: self._queue.put(("progress", (m, f))))
            self._queue.put(("craters", (craters, depth, then)))
        self._run("Finding craters...", job, "find_craters", dem.size / 1e6)

    def _labels(self, supersample=1):
        ids = self._read_ids()
        rim = ids["rim"] if self.rim_row.get() > 0 else 0
        return crater_map.render_labels(self.dem.shape, self.craters, ids["crater"], rim,
                                         self.rim_row.get(), supersample), ids

    def export_crater_map(self, then=None):
        if self._read_ids() is None:
            self.log("Fix the ids first (1-255, rim 0-255).", level="error")
            return
        if self.craters is None or self._crater_depth != self._crater_key():
            self.find_craters(then=lambda: self.export_crater_map(then))
            return
        k = crater_map.supersample_for(self.dem.shape)
        ids = self._read_ids()
        rim_fraction = self.rim_row.get()
        rim = ids["rim"] if rim_fraction > 0 else 0
        shape, craters, group = self.dem.shape, self.craters, self._group_name()
        resolution = self._resolution()
        source = "generator" if self._crater_depth == "generator" else "detected"
        depth = 0.0 if source == "generator" else self._crater_depth

        def job(log):
            labels = crater_map.render_labels(shape, craters, ids["crater"], rim,
                                               rim_fraction, k)
            written = crater_map.export_crater_mask(
                labels, paths.GENERATOR_OUTPUT_DIR, group, resolution, depth,
                ids["crater"], rim, rim_fraction, supersample=k, source=source)
            log(f"Crater map written ({labels.shape[1]} x {labels.shape[0]}, {k} per DEM cell):",
                "ok")
            for path in written.values():
                log(f"  {path}")
            if then:
                self._queue.put(("then", then))
        self._run("Writing the crater map...", job, "crater_export", shape[0] * shape[1] * k * k / 1e6)

    def send_crater_map(self):
        if self.dem_path is None:
            self.log("Load a DEM first.", level="warn")
            return
        group = self._group_name()
        png, meta = logic.load_crater_mask_meta(os.path.join(paths.GENERATOR_OUTPUT_DIR, group), group)
        if meta is None or not os.path.isfile(png):
            self.export_crater_map(then=self.send_crater_map)
            return
        stem = os.path.splitext(os.path.basename(self.dem_path))[0]
        width = heightmap_files.heightmap_width(os.path.join(paths.GENERATOR_OUTPUT_DIR, group), stem)
        camera_actor, camera_capture = self._camera()
        save_level = self.save_level_var.get()
        shadows = self.shadows_var.get()
        rover_shadow = shadows and self.rover_shadow_var.get()
        sun = self.sun_var.get()
        sun_glare = self._glare_deg()
        wheel_tracks = self.tracks_var.get()
        track_style = self._track_style()
        self.log(f"Sending {os.path.basename(png)} to Unreal (crater id {meta['crater_id']}, "
                 f"rim id {meta['rim_id']}; shadow feed {'on' if shadows else 'off'}, rover "
                 f"shadow {'on' if rover_shadow else 'off'}; mask camera "
                 f"{camera_actor + ' / ' + camera_capture if camera_actor else 'unchanged'}).")
        self._run("Sending the masks to Unreal...", key="crater_send",
                  work=max(os.path.getsize(png) / 1e6, 0.01),
                  job=
                  lambda log: send_masks.send_crater_mask_to_unreal(
                      png, meta["width"], meta["height"], meta["resolution_m"],
                      landscape_label=f"{stem}_heightmap", landscape_width=width,
                      supersample=meta.get("supersample", 1), camera_actor=camera_actor,
                      camera_capture=camera_capture, save_level=save_level,
                      shadows=shadows, rover_shadow=rover_shadow, sun=sun,
                      sun_glare_deg=sun_glare, wheel_tracks=wheel_tracks,
                      track_style=track_style, log=log))

    def _track_style(self):
        """The tread look chosen in the Wheel Tracks group (unreal_wheel_tracks style)."""
        return logic.track_style(self.track_pattern_var.get(), self.track_spacing_var.get(),
                                 self.track_relief_var.get(), self.track_shade_var.get())

    def _refresh_track_preview(self, *_args):
        """Draws one wheel's track from above: 18 cm of tread, travel left to right."""
        style = self._track_style()
        self.track_style_info.set(logic.track_style_info(self.track_pattern_var.get(), style))
        canvas = self.track_preview
        canvas.delete("all")
        for kind, coords, colour in logic.track_preview_shapes(style, canvas.winfo_width()):
            draw = canvas.create_rectangle if kind == "rect" else canvas.create_polygon
            draw(*coords, outline="", fill=colour)

    def apply_track_style(self):
        style = self._track_style()
        self.log(f"Updating the wheel-track look in Unreal: {self.track_style_info.get()}.")
        self._run("Updating the track look...",
                  lambda log: send_masks.send_track_style(style, log=log), "track_style")

    def restore_material(self):
        self._run("Restoring the mask material...",
                  lambda log: send_masks.remove_crater_mask_from_unreal(log=log),
                  "restore_mask")

    def find_cameras(self, quiet=False):
        """Lists the scene-capture cameras in Unreal into the camera box (own thread)."""
        self.cameras_button.state(["disabled"])
        self.camera_info_var.set("Listing cameras in Unreal...")
        log = (lambda text, level=None: None) if quiet else (
            lambda text, level=None: self._queue.put(("log", (text, level))))

        def worker():
            found = None
            try:
                found = send_masks.list_mask_cameras(log=log, discovery_seconds=1.5)
            finally:
                self._queue.put(("cameras", (found, quiet)))
        threading.Thread(target=worker, daemon=True).start()

    def _show_cameras(self, found, quiet):
        if LOADER_AVAILABLE:
            self.cameras_button.state(["!disabled"])
        if not found:
            self.camera_info_var.set("Unreal not reached: press Find to try again.")
            if not quiet:
                self.log("No answer from Unreal: is the editor running with Remote Execution?",
                         level="warn")
            return
        options, streamed, info = logic.camera_choices(found)
        self.camera_box.configure(values=options)
        if streamed and (self.camera_var.get() not in options):
            self.camera_var.set(streamed)
        self.camera_info_var.set(info)
        if not quiet:
            self.log(f"{len(options)} cameras listed.", level="ok")

    def _camera(self):
        """(actor label, capture name) from the camera box, or ('', '') when blank."""
        return logic.parse_camera(self.camera_var.get())

    # ------------------------------------------------------------- drawing
    def _message(self, text):
        plots.draw_message(self.figure, text)
        self.canvas.draw_idle()

    def _redraw(self):
        """Relief with the crater map, coloured by mask class."""
        if self.dem is None or not hasattr(self, "canvas"):
            return
        ids = self._read_ids()
        labels = None
        if self.craters is not None and ids is not None:
            labels, _ids = self._labels()
        plots.draw_crater_mask(self.figure, self.dem, self._resolution(), labels, ids,
                               self.rim_row.get() > 0,
                               len(self.craters) if self.craters is not None else None)
        self.canvas.draw_idle()

    # ---------------------------------------------------------- main pump
    def _drain_queue(self):
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "dem":
                    self.dem = payload
                    self.progress.finish()
                    self.log(f"Loaded DEM {payload.shape[1]} x {payload.shape[0]} px.", level="ok")
                    self.crater_info_var.set("Press Find Craters to map them.")
                    self._redraw()
                elif kind == "craters":
                    self.craters, self._crater_depth, then = payload
                    labels, ids = self._labels()
                    readout, line = logic.crater_summary(
                        len(self.craters), 100.0 * (labels == ids["crater"]).mean(),
                        self._crater_depth == "generator")
                    self.crater_info_var.set(readout)
                    self.log(line, level="ok")
                    self._redraw()
                    if then:
                        self.after(250, then)   # Once the worker thread has ended
                elif kind == "cameras":
                    self._show_cameras(*payload)
                elif kind == "progress":
                    self.progress.update(*payload)
                elif kind == "then":
                    self.after(250, payload)   # Once the worker thread has ended
                elif kind in ("failed", "job_failed"):
                    self.progress.finish("failed")
                elif kind == "log":
                    text, level = payload
                    self.log(text, level=level)
                elif kind == "done":
                    self.find_button.state(["!disabled"])
                    self.export_button.state(["!disabled"])
                    if LOADER_AVAILABLE:
                        for button in self._unreal_buttons:
                            button.state(["!disabled"])
                    self.progress.finish()
        except queue.Empty:
            pass
        self.after(80, self._drain_queue)

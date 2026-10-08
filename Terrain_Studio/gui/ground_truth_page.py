"""
Ground Truth view: crater labels for the rover's object mask, shadow, sun and
wheel-track ids, the mask camera, and the ids GNC uses.
"""

import json
import os
import queue
import subprocess
import threading
import traceback

import tkinter as tk
from tkinter import filedialog, ttk
import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.colors import LightSource
from matplotlib.figure import Figure
from matplotlib.patches import Patch

from gui import paths
from gui.backend import rock_field, crater_map, heightmap_files, send_masks, LOADER_AVAILABLE
from gui.dem_files import DEM_FILETYPES, dem_group_name, drop_dem_menu, load_dem_array
from gui.paths import map_browse_dir
from gui.widgets import ParamRow, ProgressPanel, Tooltip


# =========================================================================
# CRATER MASKS
# =========================================================================

# Object-mask ids: rocks are set in the rock planner, craters in Crater Masks;
# both views read them for placement and the GNC labels.
DEFAULT_MASK_IDS = {"boulder": 20, "small_rock": 21, "boulder_radius_m": 0.15,
                    "crater": crater_map.CRATER_ID, "rim": crater_map.RIM_ID}


GNC_MASK_LABELS = "GNC-Moon-Simulator/component_stereo_depth/config/params.yaml"


SUN_MASK_ID = 50   # The sun's disc in the object mask (SUN_ID in unreal_ground_truth)


SUN_GLARE_ID = 51  # Its glare ring (SUN_GLARE_ID there)


TRACK_MASK_ID = 60  # Wheel tracks (TRACK_ID there)


# Tread looks (unreal_wheel_tracks.TRACK_PATTERNS) and lug spacings that divide its
# 12 cm decal spacing.
TRACK_PATTERNS = (("Straight lugs", "straight"), ("Chevron", "chevron"),
                  ("Staggered blocks", "blocks"), ("Smooth (no tread)", "smooth"))


LUG_SPACINGS_CM = ("2", "3", "4", "6")


def mask_ids(app):
    """The studio's current object-mask ids (defaults when not set)."""
    return getattr(app, "mask_ids", None) or dict(DEFAULT_MASK_IDS)


class GroundTruthPage(ttk.Frame):
    """
    Crater labels for the rover's object mask. Maps the craters on a DEM,
    exports the crater map, and sends it to Unreal, where the mask material
    draws it on the Landscape for the chosen mask camera. Also lists the GNC
    mask labels for every id in use.
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
                "Adds a helper capture to the level, attached to the mask camera, that\n"
                "renders the same view without the mask camera's actor (the rover). A\n"
                "pixel in shadow but sunlit without the rover is the rover's shadow.\n"
                "Costs one more sun-only render per frame. Needs the editor (not Play).")
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
        sun_check = ttk.Checkbutton(sun, text=f"Tag the sun's disc in the object mask (id {SUN_MASK_ID})",
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
            tracks, text=f"Rover leaves wheel tracks (id {TRACK_MASK_ID})", variable=self.tracks_var)
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
                           "PPM_GroundTruthMask and, if its actor has a StereoCamStreamer, is "
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
        self._hint(labels, f"mask_labels in {GNC_MASK_LABELS} must list these ids:")
        self.labels_text = tk.Text(labels, height=6, font=("Consolas", 8), relief="solid",
                                   borderwidth=1, wrap="none")
        self.labels_text.pack(fill="x", pady=(4, 0))
        ttk.Button(labels, text="Copy", command=self.copy_labels).pack(anchor="e", pady=(4, 0))

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
        ids = dict(DEFAULT_MASK_IDS, **mask_ids(self.app))
        try:
            ids["crater"], ids["rim"] = int(self.crater_var.get()), int(self.rim_var.get())
        except ValueError:
            return None
        if not (1 <= ids["crater"] <= 255 and 0 <= ids["rim"] <= 255 and ids["crater"] != ids["rim"]):
            return None
        return ids

    def _ids_changed(self, *_trace_args):
        ids = self._read_ids()
        if ids is not None:
            self.app.mask_ids = ids         # Shared with the rock planner
        self._refresh_labels(ids)
        self._redraw()

    def _refresh_labels(self, ids):
        self.labels_text.configure(state="normal")
        self.labels_text.delete("1.0", "end")
        if ids is None:
            self.labels_text.insert("end", "Crater ids must be different whole numbers\n1-255 (rim 0-255).")
        else:
            lines = ['- "2:rover"', '- "10:landscape"', f'- "{ids["boulder"]}:boulder"']
            if ids["small_rock"] != ids["boulder"]:
                lines.append(f'- "{ids["small_rock"]}:small_rock"')
            lines.append(f'- "{ids["crater"]}:crater"')
            if ids["rim"]:
                lines.append(f'- "{ids["rim"]}:crater_rim"')
            if self.sun_var.get():
                lines.append(f'- "{SUN_MASK_ID}:sun"')
                if self._glare_deg() > 0:
                    lines.append(f'- "{SUN_GLARE_ID}:sun_glare"')
            if self.tracks_var.get():
                lines.append(f'- "{TRACK_MASK_ID}:wheel_track"')
            self.labels_text.insert("end", "\n".join(lines))
        self.labels_text.configure(state="disabled")

    # Shadow class ids published by the GNC stereo node (SHADOW_IDS there).
    SHADOW_IDS = ((0, "sky"), (1, "sunlit ground"), (2, "shadowed ground"),
                  (3, "sunlit rock / rover"), (4, "shadowed rock / rover"),
                  (5, "rover shadow (on ground or rocks)"))

    def _refresh_shadow_ids(self, *_trace_args):
        shadows, rover = self.shadows_var.get(), self.rover_shadow_var.get()
        if not shadows:
            lines = ["Shadow feed off."]
        else:
            lines = [f"{i}  {name}" for i, name in self.SHADOW_IDS if i != 5 or rover]
            if not rover:
                lines.append("(5 rover shadow: off)")
        self.shadow_ids_text.configure(state="normal", height=len(lines))
        self.shadow_ids_text.delete("1.0", "end")
        self.shadow_ids_text.insert("end", "\n".join(lines))
        self.shadow_ids_text.configure(state="disabled")

    def _glare_deg(self):
        """Glare ring radius in degrees from the field; 0 when blank or invalid."""
        try:
            return max(float(self.sun_glare_var.get()), 0.0)
        except ValueError:
            return 0.0

    def copy_labels(self):
        self.clipboard_clear()
        self.clipboard_append(self.labels_text.get("1.0", "end").strip())
        self.log("mask_labels copied to the clipboard.", level="ok")

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
        candidate = os.path.join(os.path.dirname(path), f"{dem_group_name(path)}_craters.csv")
        self.crater_list_path = candidate if os.path.isfile(candidate) else None
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
        try:
            return max(1e-6, float(self.resolution_var.get()))
        except ValueError:
            return 1.0

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
        if generator:
            self.crater_source_info_var.set(
                f"Generated here: using {os.path.basename(self.crater_list_path)}.")
        elif has_list:
            self.crater_source_info_var.set("A generator crater list exists; detecting anyway.")
        else:
            self.crater_source_info_var.set("No generator crater list for this map: detecting.")
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
        folder = os.path.join(paths.GENERATOR_OUTPUT_DIR, group)
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
        pattern = dict(TRACK_PATTERNS).get(self.track_pattern_var.get(), "straight")
        return {"pattern": pattern, "period_cm": float(self.track_spacing_var.get() or 3),
                "relief": round(float(self.track_relief_var.get()), 2),
                "shade": round(float(self.track_shade_var.get()), 2)}

    def _refresh_track_preview(self, *_args):
        """Draws one wheel's track from above: 18 cm of tread, travel left to right."""
        style = self._track_style()
        self.track_style_info.set(
            f"{self.track_pattern_var.get()}, lugs every {style['period_cm']:g} cm, relief "
            f"{style['relief']:.1f}, shade {style['shade']:.2f}")
        canvas = self.track_preview
        canvas.delete("all")
        width, height = max(canvas.winfo_width(), 160), 46
        grey = int(208 * (0.85 + 0.15 * style["shade"]))
        top, bottom = 8, height - 8
        canvas.create_rectangle(0, top, width, bottom, outline="", fill=f"#{grey:02x}{grey:02x}{grey - 6:02x}")
        if style["pattern"] == "smooth" or style["relief"] <= 0.01:
            return
        dark = max(0, grey - int(60 * min(style["relief"], 2.0)))
        colour = f"#{dark:02x}{dark:02x}{max(dark - 6, 0):02x}"
        cm = width / 18.0                       # 18 cm of track across the preview
        period, lug = style["period_cm"] * cm, 0.4 * style["period_cm"] * cm
        middle, inset = (top + bottom) / 2, 3
        x = 0.0
        while x < width + period:
            if style["pattern"] == "straight":
                canvas.create_rectangle(x, top + inset, x + lug, bottom - inset, outline="", fill=colour)
            elif style["pattern"] == "chevron":
                skew = 0.5 * period
                canvas.create_polygon(x + skew, top + inset, x + skew + lug, top + inset, x + lug, middle,
                                      x + skew + lug, bottom - inset, x + skew, bottom - inset, x, middle,
                                      outline="", fill=colour)
            else:                                # staggered blocks
                canvas.create_rectangle(x, top + inset, x + lug, middle - 2, outline="", fill=colour)
                canvas.create_rectangle(x + period / 2, middle + 2, x + period / 2 + lug, bottom - inset,
                                        outline="", fill=colour)
            x += period

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
        options, streamed, loose, fixed = [], None, [], []
        for actor in found.get("actors", []):
            for capture in actor["captures"]:
                options.append(f"{actor['actor']} / {capture}")
            if actor.get("streamer_mask"):
                streamed = f"{actor['actor']} / {actor['streamer_mask']}"
            if actor.get("rover_shadow_helper") == "detached":
                loose.append(actor["actor"])
            elif actor.get("rover_shadow_helper") == "reattached":
                fixed.append(actor["actor"])
        for name in fixed:
            self.log(f"Re-attached the rover-shadow helper of {name} to its mask camera "
                     "(its Blueprint had been recompiled).", level="ok")
        for name in loose:
            self.log(f"The rover-shadow helper of {name} is not attached to its mask camera "
                     "and could not be re-attached, so rocks would be tagged as rover "
                     "shadow. Send the masks again.", level="warn")
        self.camera_box.configure(values=options)
        if streamed and (self.camera_var.get() not in options):
            self.camera_var.set(streamed)
        self.camera_info_var.set(
            (f"Streamed as the object mask now: {streamed}." if streamed
             else "No StereoCamStreamer found: the mask is not streamed.")
            + (" Play In Editor is running: camera changes last until it stops."
               if found.get("pie") else ""))
        if not quiet:
            self.log(f"{len(options)} cameras listed.", level="ok")

    def _camera(self):
        """(actor label, capture name) from the camera box, or ('', '') when blank."""
        text = self.camera_var.get().strip()
        if " / " not in text:
            return "", ""
        actor, capture = text.split(" / ", 1)
        return actor.strip(), capture.strip()

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
        title = "Crater mask"
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
                    self.progress.finish()
                    self.log(f"Loaded DEM {payload.shape[1]} x {payload.shape[0]} px.", level="ok")
                    self.crater_info_var.set("Press Find Craters to map them.")
                    self._redraw()
                elif kind == "craters":
                    self.craters, self._crater_depth, then = payload
                    labels, ids = self._labels()
                    cover = 100.0 * (labels == ids["crater"]).mean()
                    origin = "from the generator" if self._crater_depth == "generator" else "detected"
                    self.crater_info_var.set(f"{len(self.craters)} craters ({origin}), "
                                             f"{cover:.1f}% of the map.")
                    self.log(f"{len(self.craters)} craters {origin}.", level="ok")
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

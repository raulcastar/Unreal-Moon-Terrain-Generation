"""
Sun view: sets the sun over a DEM (with cast shadows) and sends it to Unreal as
a lunar Directional Light.
"""

import math
import os
import queue
import threading
import traceback

import tkinter as tk
from tkinter import filedialog, ttk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure

from gui import rock_planner_logic
from gui import sun_logic as logic
from gui import sun_plots as plots
from gui.sun_logic import SUN_DISPLAY_MODES, SUN_ORIENTATIONS, SUN_PREVIEW_SIZES
from gui.backend import sun_shadows, send_sun, LOADER_AVAILABLE
from gui.dem_files import DEM_FILETYPES, dem_source_note, drop_dem_menu, load_dem_array
from gui.paths import map_browse_dir
from gui.widgets import ParamRow, ProgressPanel, StepTabs, Tooltip


class SunPage(ttk.Frame):
    """
    Sets the sun over a DEM and sends it to Unreal as a lunar Directional Light.
    The sun is placed with the sliders, on the sky dome, or by drawing the shadow
    an object should cast; the map shows the resulting light and cast shadows.

    Shading runs on a worker thread over a reduced DEM (sun_shadows module);
    requests made while it runs collapse into one re-render. The sun's maths
    and read-outs are in sun_logic, the drawing in sun_plots.
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

        # One tab per step, each with its settings and, pinned beneath them, its
        # buttons; the map they all use stays above the tabs.
        self.steps = StepTabs(left, background=self.app.cget("background"),
                              on_width=self._rewrap_labels)
        sun_tab, _ = self.steps.add_step("Sun")
        unreal_tab, unreal_actions = self.steps.add_step("Unreal")

        self.send_button = ttk.Button(unreal_actions, text="Send Sun to Unreal",
                                      style="Accent.TButton", command=self.send_to_unreal)
        self.send_button.pack(fill="x")
        Tooltip(self.send_button,
                "Points the level's sun at this position, with lunar settings.")
        self.read_button = ttk.Button(unreal_actions, text="Read Sun from Unreal",
                                      command=self.read_from_unreal)
        self.read_button.pack(fill="x", pady=(4, 0))
        Tooltip(self.read_button, "Sets the sliders to the sun currently in the Unreal level.")
        if not LOADER_AVAILABLE:
            self.send_button.state(["disabled"])
            self.read_button.state(["disabled"])

        self._add_hint(sun_tab, "Places the sun so shadows fall where you want them, shows "
                              "the terrain lit by it, and sets the matching light in Unreal.",
                       pady=(0, 8))

        # Source DEM
        source_group = self._group(left, "Source DEM")
        source_group.pack_configure(pady=(8, 6))
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
        sun_group = self._group(sun_tab, "Sun Position")
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
        shadow_group = self._group(sun_tab, "Place a Shadow")
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
        display_group = self._group(sun_tab, "Display")
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
        unreal_group = self._group(unreal_tab, "Unreal Light")
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

        self.steps.pack(fill="both", expand=True)
        self.steps.bind_mousewheel()

        # Right: map / 3D tabs on top, log at the bottom.
        right = ttk.Frame(self._panes)
        self._panes.add(left, weight=0)
        self._panes.add(right, weight=1)

        log_frame = ttk.Frame(right, padding=(8, 4, 8, 8))
        ttk.Label(log_frame, text="Activity log",
                  font=("Segoe UI Semibold", 10)).pack(anchor="w")
        self.progress = ProgressPanel(log_frame)
        self.progress.pack(fill="x", pady=(4, 0))
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
        return logic.clamp_sun(self.azimuth_row.get(), self.elevation_row.get())

    def set_sun(self, azimuth, elevation, render=True):
        azimuth, elevation = logic.clamp_sun(azimuth, elevation)
        self.azimuth_row.set(round(azimuth, 2))
        self.elevation_row.set(round(elevation, 2))
        self._on_sun_change(render=render)

    def _object_height(self):
        return logic.parse_positive(self.object_height_var.get(), 1.0)

    def _on_sun_change(self, render=True):
        azimuth, elevation = self.sun()
        height = self._object_height()
        length = sun_shadows.shadow_length(height, elevation)
        self.shadow_dir_var.set(f"{logic.shadow_direction(azimuth):.1f}")
        self.shadow_len_var.set(f"{length:.2f}")
        self.sun_info_var.set(logic.sun_info_text(azimuth, elevation, height, length))
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
        sun = logic.sun_from_shadow_fields(self.shadow_dir_var.get(), self.shadow_len_var.get(),
                                           self._object_height())
        if isinstance(sun, str):
            self.log(sun, level="warn")
            return
        self.set_sun(*sun)

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
        self.progress.start(f"Loading {os.path.basename(path)}", "load_dem",
                            max(os.path.getsize(path) / 1e6, 0.01) if os.path.exists(path) else 1.0)

        def worker():
            try:
                self._queue.put(("dem", load_dem_array(path)))
            except Exception as exc:
                self._queue.put(("error", f"Could not load DEM: {exc}"))
        threading.Thread(target=worker, daemon=True).start()

    def _resolution(self):
        return rock_planner_logic.parse_resolution(self.resolution_var.get())

    def _rebuild_view(self):
        """Reduces the DEM to the preview size and redraws everything."""
        if self.dem is None:
            return
        self.view = logic.reduce_dem(self.dem, int(self.preview_size_var.get() or 1000),
                                     self._resolution())
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
        return logic.display_rgb(self.lit["light"], self.lit["shadow"], self._relief,
                                 self.display_var.get())

    # ------------------------------------------------------------ drawing
    def _show_message(self, text):
        self._map_artists = None
        plots.draw_message(self.figure, text)
        self.canvas.draw_idle()

    def _build_map(self):
        """Creates the map, sky dome and readout axes for the current view."""
        dem, res = self.view
        self._relief = logic.relief_shading(dem)
        self._map_artists = plots.build_map(self.figure, dem.shape, res, self._unreal_view())
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

        dem, res = self.view
        cast, dark = logic.shadow_shares(dem, self.lit["shadow"], self.lit["light"])
        azimuth, elevation = self.lit["az"], self.lit["el"]
        art["axes"].set_title(logic.map_title(azimuth, elevation, cast), fontsize=9)
        art["text"].set_text(logic.readout_text(azimuth, elevation, cast, dark, dem.shape, res))
        self._draw_sky(draw=False)
        self._draw_probe(draw=False)
        self.canvas.draw_idle()
        self._3d_stale = True
        self._draw_3d()

    def _draw_sky(self, draw=True):
        if not self._map_artists:
            return
        plots.place_sun(self._map_artists, *self.sun())
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
            x0, y0 = self.probe
            if tip is None:
                azimuth, elevation = self.sun()
                tip = logic.probe_tip(x0, y0, azimuth, sun_shadows.shadow_length(
                    self._object_height(), elevation))
            self._probe_artists = plots.draw_probe(self._map_artists["axes"], x0, y0, tip,
                                                   self._object_height())
        if draw:
            self.canvas.draw_idle()

    def _draw_3d(self):
        """Lit 3D surface with the incoming sunlight; drawn only while its tab shows."""
        if (self.lit is None or not self._3d_stale
                or self.tabs.index(self.tabs.select()) != 1):
            return
        self._3d_stale = False
        dem, res = self.view
        plots.draw_3d(self.figure3d, dem, res, self._display_rgb(), self.lit["az"], self.lit["el"])
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

    def _start_unreal_job(self, status, job, key=None):
        self.progress.start(status, key)
        self.send_button.state(["disabled"])
        self.read_button.state(["disabled"])

        def worker():
            try:
                if job(lambda text, level=None: self._queue.put(("log", (text, level)))) is False:
                    self._queue.put(("job_failed", None))
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
        settings = logic.parse_sun_settings(self.intensity_var.get(), self.disc_var.get())
        if isinstance(settings, str):
            self.log(settings, level="error")
            return
        intensity, disc = settings
        azimuth, elevation = self.sun()
        save_level = self.save_level_var.get()
        self.log(f"Sending the sun to Unreal: azimuth {azimuth:.1f}, elevation "
                 f"{elevation:.1f} deg, {intensity:.0f} lux.")
        self._start_unreal_job(
            "Setting the sun in Unreal...", key="sun_send", job=
            lambda log: send_sun.send_sun_to_unreal(
                azimuth, elevation, intensity_lux=intensity, source_angle_deg=disc,
                save_level=save_level, log=log))

    def read_from_unreal(self):
        if self._unreal_busy():
            self.log("Unreal is still busy with the last request.", level="warn")
            return

        def job(log):
            found = send_sun.read_sun_from_unreal(log=log)
            if found:
                self._queue.put(("unreal_sun", found))
            return bool(found)
        self._start_unreal_job("Reading the sun from Unreal...", job, "sun_read")

    # ---------------------------------------------------------- main pump
    def _drain_queue(self):
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "dem":
                    self.dem = payload
                    self.progress.finish()
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
                    self.log(logic.unreal_sun_log(payload), level="ok")
                elif kind == "log":
                    text, level = payload
                    self.log(text, level=level)
                elif kind == "job_failed":
                    self.progress.finish("failed")
                elif kind == "error":
                    self.log(payload, level="error")
                    self.progress.finish("failed")
                    self._show_message("Could not light the map.\nSee the activity log.")
                elif kind == "unreal_done":
                    if LOADER_AVAILABLE:
                        self.send_button.state(["!disabled"])
                        self.read_button.state(["!disabled"])
                    self.progress.finish()
        except queue.Empty:
            pass
        self.after(80, self._drain_queue)

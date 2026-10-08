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
import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.colors import LightSource
from matplotlib.figure import Figure
from matplotlib.ticker import MaxNLocator

from gui.backend import sun_shadows, send_sun, LOADER_AVAILABLE
from gui.dem_files import DEM_FILETYPES, dem_source_note, drop_dem_menu, load_dem_array
from gui.paths import map_browse_dir
from gui.widgets import ParamRow, ProgressPanel, Tooltip


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


class SunPage(ttk.Frame):
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
        self.progress.start(f"Loading {os.path.basename(path)}", "load_dem",
                            max(os.path.getsize(path) / 1e6, 0.01) if os.path.exists(path) else 1.0)

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
                    where = "running game" if payload.get("pie") else "level"
                    self.log(f"Read '{payload['label']}' from the {where}: azimuth "
                             f"{payload['azimuth']:.1f}, elevation {payload['elevation']:.1f} deg, "
                             f"{payload['intensity']:.0f} lux.", level="ok")
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

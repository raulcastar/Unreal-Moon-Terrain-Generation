"""
The Terrain Studio window: home screen, view switching, DPI scaling, and the
Generate view (generator_page). main() starts the studio.
"""

import ctypes
import logging
import os
import queue
import sys
import threading

import tkinter as tk
from tkinter import ttk
import matplotlib
import numpy as np

from gui import paths
from gui.backend import terrain_core, LOADER_AVAILABLE, LOADER_IMPORT_ERROR
from gui.dem_files import scan_dem_folder
from gui.generator_page import GeneratorPage
from gui.ground_truth_page import GroundTruthPage, DEFAULT_MASK_IDS
from gui.landscape_page import LandscapePage
from gui.map_views import MapBrowser
from gui.rock_planner_page import RockPlannerPage
from gui.sun_page import SunPage
from gui.widgets import ask_confirmation, enable_dpi_awareness, maximize_window

log = logging.getLogger(__name__)


# Home screen: banner rendered in the Moon Simulator, and its dark palette.
HOME_BANNER = os.path.join(paths.STUDIO_DIR, "assets", "home_banner.jpg")


HOME_COLOURS = {
    "bg": "#2e3540", "card": "#3a4250", "hover": "#465063", "border": "#525d6e",
    "accent": "#f5c060", "text": "#f2f5f9", "muted": "#b7c0cd",
}


class TerrainStudio(GeneratorPage, tk.Tk):

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
        self.landscape_page = None       # standalone export window, reused
        self.map_viewer = None          # Reused MapViewer window
        self.map_browser = None         # Reused MapBrowser window
        self.rock_planner_page = None         # Reused RockPlannerPage window
        self.sun_page = None           # Reused SunPage window
        self.ground_truth_page = None         # Reused GroundTruthPage window
        self.mask_ids = dict(DEFAULT_MASK_IDS)   # Object-mask ids, shared by steps 2 and 3
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
            except Exception as exc:     # Windows before 10 1607: ask Tk instead
                log.debug("GetDpiForWindow unavailable, asking Tk for the DPI: %s", exc)
        try:
            return int(round(self.winfo_fpixels("1i")))
        except Exception as exc:
            log.warning("Could not read the monitor DPI; assuming 96: %s", exc)
            return 96

    def _apply_scale_from_dpi(self, dpi):
        """Sets ui_scale, the Matplotlib figure DPI, and Tk's font scaling."""
        self.ui_scale = dpi / 96.0
        self.figure_dpi = max(72, round(100 * self.ui_scale))
        try:
            self.tk.call("tk", "scaling", dpi / 72.0)
        except Exception as exc:
            log.warning("Could not set Tk font scaling for %d DPI: %s", dpi, exc)

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
            'save_level': self.save_level_var.get(),
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
        # Views are rebuilt lazily at the new scale on their next visit.
        for attribute in ("map_browser", "rock_planner_page", "landscape_page", "sun_page",
                          "ground_truth_page"):
            view = getattr(self, attribute)
            if view is not None and view.winfo_exists():
                view.destroy()
            setattr(self, attribute, None)
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
        self.save_level_var.set(snapshot['save_level'])
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
        style.configure("HomeHint.TLabel", font=("Segoe UI", 8), foreground="#777777")

    def _build_layout(self):
        # Two top-level views swapped by show_home / show_studio.
        self._build_home()

        self.studio_frame = ttk.Frame(self)

        # Resizable split between the parameter column and the rest.
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
        """
        Landing screen: a banner rendered in the simulator, the workflow stages
        as cards, and the map library with the maps on disk and the current map.
        """
        colours = HOME_COLOURS
        self.home_frame = tk.Frame(self, background=colours["bg"])

        # The banner is decorative: without Pillow or the file, only the title shows.
        try:
            from PIL import Image
            self._banner_source = Image.open(HOME_BANNER).convert("RGB")
        except Exception:
            self._banner_source = None
        self._banner_image = None
        self._banner_size = None
        self._banner_job = None
        self.banner = tk.Canvas(self.home_frame, background=colours["bg"],
                                highlightthickness=0, height=self.px(320))
        self.banner.pack(fill="x")
        self.banner.bind("<Configure>", self._schedule_banner)
        self.home_frame.bind("<Configure>", self._fit_banner_height)

        body = tk.Frame(self.home_frame, background=colours["bg"])
        body.pack(fill="both", expand=True, padx=self.px(40), pady=(0, self.px(28)))
        self._home_body = body
        grid = tk.Frame(body, background=colours["bg"])
        grid.pack(fill="x")
        self._home_grid = grid
        grid.bind("<Configure>", self._fit_banner_height)
        gap = self.px(14)

        stages = [
            ("01", "Create & Send Terrain",
             "Generate a procedural lunar DEM, then turn it into a heightmap and "
             "spawn the Landscape in Unreal.", self.enter_generate, False),
            ("02", "Rocks & Rover Paths",
             "Scatter rocks around drivable rover corridors and place them in Unreal, "
             "tagged for the object mask.", self.enter_rock_planner, True),
            ("03", "Crater & Shadow Masks",
             "Map the craters and shadows and show them in the rover's object-mask "
             "camera.",
             self.enter_ground_truth, True),
            ("04", "Sun & Shadows",
             "Draw the shadow you want and set a lunar sun in Unreal to cast it.",
             self.enter_sun, True),
        ]
        self._home_caption(grid, "WORKFLOW", row=0, span=len(stages))
        for column, (number, title, blurb, command, optional) in enumerate(stages):
            grid.columnconfigure(column, weight=1, uniform="card")
            self._home_card(grid, number, title, blurb, command, optional).grid(
                row=1, column=column, sticky="nsew", padx=(0 if column == 0 else gap, 0))

        self._home_caption(grid, "LIBRARY", row=2, top=self.px(26), span=len(stages))
        self._home_card(grid, None, "Browse Maps",
                        "Preview every map in the project. The one you select becomes "
                        "the current map for the other steps.", self.browse_maps).grid(
            row=3, column=0, sticky="nsew")
        self._home_card(grid, None, "Send an Existing Map",
                        "Turn any elevation map into a heightmap and spawn its Landscape "
                        "in Unreal.", self.enter_landscape).grid(
            row=3, column=1, sticky="nsew", padx=(gap, 0))

        # Maps on disk and the current map; refreshed by show_home.
        status = tk.Frame(grid, background=colours["card"], highlightthickness=1,
                          highlightbackground=colours["border"])
        status.grid(row=3, column=2, columnspan=len(stages) - 2, sticky="nsew", padx=(gap, 0))
        tk.Label(status, text="PROJECT", font=("Segoe UI Semibold", 8),
                 foreground=colours["muted"], background=colours["card"]).pack(
            anchor="w", padx=self.px(18), pady=(self.px(16), self.px(4)))
        self.home_status_var = tk.StringVar()
        tk.Label(status, textvariable=self.home_status_var, font=("Segoe UI", 10),
                 foreground=colours["text"], background=colours["card"],
                 justify="left").pack(anchor="w", padx=self.px(18))

    def _home_caption(self, parent, text, row, top=0, span=4):
        tk.Label(parent, text=text, font=("Segoe UI Semibold", 8),
                 foreground=HOME_COLOURS["muted"], background=HOME_COLOURS["bg"]).grid(
            row=row, column=0, columnspan=span, sticky="w", pady=(top, self.px(8)))

    def _home_card(self, parent, number, title, blurb, command, optional=False):
        """A clickable home-screen card; highlights on hover and keyboard focus."""
        colours = HOME_COLOURS
        pad = self.px(18)
        card = tk.Frame(parent, background=colours["card"], highlightthickness=1,
                        highlightbackground=colours["border"], highlightcolor=colours["accent"],
                        takefocus=1, cursor="hand2")
        head = tk.Frame(card, background=colours["card"])
        head.pack(fill="x", padx=pad, pady=(pad, 0))
        if number:
            tk.Label(head, text=number, font=("Segoe UI Semibold", 18),
                     foreground=colours["accent"], background=colours["card"]).pack(side="left")
        if optional:
            tk.Label(head, text="OPTIONAL", font=("Segoe UI Semibold", 7),
                     foreground=colours["muted"], background=colours["card"]).pack(
                side="right", anchor="n")
        tk.Label(card, text=title, font=("Segoe UI Semibold", 13), anchor="w",
                 foreground=colours["text"], background=colours["card"]).pack(
            fill="x", padx=pad, pady=(self.px(4), 0))
        text = tk.Label(card, text=blurb, font=("Segoe UI", 9), anchor="nw", justify="left",
                        foreground=colours["muted"], background=colours["card"])
        text.pack(fill="both", expand=True, padx=pad, pady=(self.px(4), 0))
        tk.Label(card, text="Open  \u2192", font=("Segoe UI Semibold", 9), anchor="w",
                 foreground=colours["accent"], background=colours["card"]).pack(
            fill="x", padx=pad, pady=(self.px(12), pad))

        widgets = [card] + list(card.winfo_children()) + list(head.winfo_children())

        def paint(hover):
            fill = colours["hover"] if hover else colours["card"]
            for widget in widgets:
                widget.configure(background=fill)
            card.configure(highlightbackground=colours["accent"] if hover else colours["border"])

        def leave(event):
            # Leaving into one of the card's own widgets keeps the highlight.
            inside = card.winfo_containing(event.x_root, event.y_root)
            if inside is None or str(inside).find(str(card)) != 0:
                paint(False)

        for widget in widgets:
            widget.bind("<Enter>", lambda e: paint(True))
            widget.bind("<Leave>", leave)
            widget.bind("<Button-1>", lambda e: command())
        card.bind("<Return>", lambda e: command())
        card.bind("<space>", lambda e: command())
        card.bind("<Configure>", lambda e: text.configure(wraplength=max(e.width - 2 * pad, 80)))
        return card

    def _fit_banner_height(self, event=None):
        """Gives the banner the height the cards leave free, within sensible limits."""
        if event is not None and event.widget not in (self.home_frame, self._home_grid):
            return
        total = self.home_frame.winfo_height()
        if total < 2:
            return
        # Re-run when the cards re-wrap their text and grow.
        free = total - self._home_body.winfo_reqheight() - self.px(64)
        height = int(min(max(free, self.px(140)), total * 0.5, self.px(560)))
        if int(self.banner.cget("height")) != height:
            self.banner.configure(height=height)

    def _schedule_banner(self, _event=None):
        if self._banner_job is not None:
            self.after_cancel(self._banner_job)
        self._banner_job = self.after(50, self._draw_banner)

    def _draw_banner(self):
        """Fills the banner with the photo (cropped to cover it) and the title."""
        self._banner_job = None
        width, height = self.banner.winfo_width(), self.banner.winfo_height()
        if width < 2 or height < 2:
            return
        colours = HOME_COLOURS
        if self._banner_source is not None and self._banner_size != (width, height):
            from PIL import Image, ImageTk
            source = self._banner_source
            scale = max(width / source.width, height / source.height)
            size = (max(width, round(source.width * scale)), max(height, round(source.height * scale)))
            image = source.resize(size, Image.LANCZOS)
            left, top = (size[0] - width) // 2, int((size[1] - height) * 0.45)
            pixels = np.asarray(image.crop((left, top, left + width, top + height)), dtype=np.float32)
            # Fade the lower edge into the page background.
            fade = np.clip((np.arange(height) - 0.7 * height) / (0.3 * height), 0.0, 1.0)
            page = np.array(matplotlib.colors.to_rgb(colours["bg"])) * 255.0
            pixels = pixels * (1 - fade[:, None, None]) + page * fade[:, None, None]
            self._banner_image = ImageTk.PhotoImage(Image.fromarray(pixels.astype(np.uint8)))
            self._banner_size = (width, height)

        canvas = self.banner
        canvas.delete("all")
        if self._banner_image is not None:
            canvas.create_image(0, 0, image=self._banner_image, anchor="nw")
        x, y = self.px(40), self.px(36)
        for dx, fill in ((2, "#000000"), (0, colours["text"])):      # Drop shadow, then text
            canvas.create_text(x + dx, y + dx, text="Lunar Terrain Studio", anchor="nw",
                               fill=fill, font=("Segoe UI Semibold", 30))
        canvas.create_text(x, y + self.px(58), anchor="nw", fill="#c3ccd8",
                           font=("Segoe UI", 12),
                           text="From a lunar elevation map to an Unreal Landscape")
        canvas.create_text(width - self.px(20), self.px(16), anchor="ne", fill="#6f7a8a",
                           font=("Segoe UI", 8),
                           text="Rendered in the Moon Simulator, Unreal Engine 5")

    def refresh_home_status(self):
        """Summarises what is on disk and which map the stages would act on."""
        counts = []
        for label, folder in (("input_maps", paths.INPUT_MAPS_DIR),
                              ("outputs", paths.OUTPUT_ROOT)):
            if os.path.isdir(folder):
                counts.append(f"{len(scan_dem_folder(folder))} in {label}/")
            else:
                counts.append(f"no {label}/ yet")

        current = (self.exported_paths.get('elevation_npy')
                   or self.unreal_source_path)
        in_hand = os.path.basename(current) if current else "none yet"
        self.home_status_var.set(
            f"Maps on disk: {', '.join(counts)}\n"
            f"Current map: {in_hand}")

    BASE_TITLE = "Lunar Terrain Studio"

    def _all_views(self):
        """Every frame that can occupy the window, home and studio included."""
        return [self.home_frame, self.studio_frame, self.map_browser,
                self.rock_planner_page, self.landscape_page, self.sun_page, self.ground_truth_page]

    def show_view(self, view, title=None):
        """
        Shows `view` in the window and hides the others.
        """
        for other in self._all_views():
            if other is not None and other is not view and other.winfo_exists():
                other.pack_forget()
        view.pack(fill="both", expand=True)
        self.title(f"{self.BASE_TITLE} - {title}" if title else self.BASE_TITLE)

    def show_home(self):
        """Switches to the landing screen, refreshing what it reports."""
        self.refresh_home_status()
        self.show_view(self.home_frame)

    # Column widths in 96-DPI design pixels, used until the user drags a handle.
    DEFAULT_PARAM_WIDTH = 300
    DEFAULT_SIDE_WIDTH = 290
    MIN_COLUMN_WIDTH = 140

    def show_studio(self):
        """Switches to the main studio, starting the preview on first entry."""
        self.show_view(self.studio_frame, "Generate Terrain")
        if not self._sash_placed:
            self.after_idle(self._apply_sash_positions)
        if not self._preview_started:
            self._preview_started = True
            self.request_preview()

    def _apply_sash_positions(self, attempt=0):
        """
        Places the two drag handles, on first show and after a DPI rebuild.

        Tk may snap a sash back to the pane's requested width while the layout
        settles, so the placement is verified and retried.
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

    def enter_landscape(self):
        """Opens the standalone export window, seeded with the map in hand."""
        source = self.exported_paths.get('elevation_npy') or self.unreal_source_path
        if self.landscape_page is None or not self.landscape_page.winfo_exists():
            self.landscape_page = LandscapePage(
                self, source_path=source, figure_dpi=self.figure_dpi,
                resolution=self.params.get('RESOLUTION_M_PER_PX', 1.0))
        elif source:
            self.landscape_page.set_source(source)
        self.show_view(self.landscape_page, "Send a Map to Unreal")

    def enter_rock_planner(self):
        """Opens the rock-field planner, seeded with the last generated / chosen DEM."""
        source = None
        if self.exported_paths.get('elevation_npy'):
            source = self.exported_paths['elevation_npy']
        elif self.unreal_source_path:
            source = self.unreal_source_path
        if self.rock_planner_page is None or not self.rock_planner_page.winfo_exists():
            self.rock_planner_page = RockPlannerPage(
                self, figure_dpi=self.figure_dpi, source_path=source,
                resolution=self.params.get('RESOLUTION_M_PER_PX', 1.0))
        elif source:
            self.rock_planner_page.load_dem(source)
        self.rock_planner_page.refresh_mask_ids()
        self.show_view(self.rock_planner_page, "Rock Field & Rover Paths")

    def enter_ground_truth(self):
        """Opens the crater-mask view, seeded with the last generated / chosen DEM."""
        source = self.exported_paths.get('elevation_npy') or self.unreal_source_path
        if self.ground_truth_page is None or not self.ground_truth_page.winfo_exists():
            self.ground_truth_page = GroundTruthPage(
                self, figure_dpi=self.figure_dpi, source_path=source,
                resolution=self.params.get('RESOLUTION_M_PER_PX', 1.0))
        else:
            self.ground_truth_page.refresh_ids()
            if source and source != self.ground_truth_page.dem_path:
                self.ground_truth_page.load_dem(source)
        self.show_view(self.ground_truth_page, "Crater & Shadow Masks")

    def enter_sun(self):
        """Opens the sun & shadows view, seeded with the last generated / chosen DEM."""
        source = self.exported_paths.get('elevation_npy') or self.unreal_source_path
        if self.sun_page is None or not self.sun_page.winfo_exists():
            self.sun_page = SunPage(
                self, figure_dpi=self.figure_dpi, source_path=source,
                resolution=self.params.get('RESOLUTION_M_PER_PX', 1.0))
        elif source and source != self.sun_page.dem_path:
            self.sun_page.load_dem(source)
        self.show_view(self.sun_page, "Sun & Shadows")

    def browse_maps(self):
        """Opens the map browser, seeded with whatever map is already in hand."""
        source = (self.exported_paths.get('elevation_npy')
                  or self.unreal_source_path)
        if self.map_browser is None or not self.map_browser.winfo_exists():
            self.map_browser = MapBrowser(self, figure_dpi=self.figure_dpi,
                                          initial_path=source)
        else:
            self.map_browser.refresh()
            if source:
                self.map_browser.select_path(source)
        self.show_view(self.map_browser, "Browse Maps")

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


def main():
    enable_dpi_awareness()  # Must precede the creation of the Tk root window
    TerrainStudio().mainloop()

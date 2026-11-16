"""
Map displays: the embeddable shaded-relief view, its standalone window, and
the map browser.
"""

import os
import queue
import threading

import tkinter as tk
from tkinter import filedialog, ttk
import matplotlib
import matplotlib.image
import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.cm import ScalarMappable
from matplotlib.colors import LightSource, Normalize
from matplotlib.figure import Figure
from matplotlib.ticker import MaxNLocator
from matplotlib.widgets import RectangleSelector

from gui import crop_logic, paths
from gui.dem_files import load_dem_array, scan_dem_folder
from gui.paths import map_browse_dir
from gui.widgets import ActivityLog, ViewHeader


# Longest edge of the 3D mesh, at rest and while the view is dragged. Matplotlib
# depth-sorts every quad per frame, so redraw cost grows with the square of these;
# the drag value is kept low to stay interactive (~60 ms/frame vs ~600 at full).
SURFACE_MESH_DETAIL = 100


SURFACE_DRAG_DETAIL = 36


class MapView(ttk.Frame):
    """
    Embeddable DEM view: shaded relief with an elevation colourbar, and
    optionally a 3D surface tab.

    Maps load on a worker thread and are drawn on the main thread; a token
    discards stale loads when a newer map is requested. The 3D surface is drawn
    only while its tab is showing. The host supplies `on_status` (log line),
    `on_title` (map name) and `on_loaded` (full-resolution shape, once a map is
    drawn).

    A host can also turn on a crop rectangle (enable_crop): the user drags it
    over the relief, and the host is told its full-resolution bounds.
    """

    DISPLAY_MAX_EDGE = 1200

    # The crop rectangle in the studio's accent colour, translucent so the
    # relief under it stays readable.
    CROP_PROPS = dict(facecolor="#e8923a", edgecolor="#e8923a", alpha=0.22,
                      fill=True, linewidth=1.5)

    def __init__(self, parent, figure_dpi=100, on_status=None, on_title=None,
                 show_3d=False, on_loaded=None):
        super().__init__(parent)
        self._on_status = on_status or (lambda text, level=None: None)
        self._on_title = on_title or (lambda name: None)
        self._on_loaded = on_loaded or (lambda shape: None)

        # Crop rectangle: on while _on_crop is set; bounds in full-resolution
        # pixels (row, col, rows, cols), so they survive a redraw or a resize.
        self._on_crop = None
        self._crop_bounds = None
        self._crop_selector = None
        self._relief_axes = None

        self._load_queue = queue.Queue()
        self._load_token = 0
        self._awaiting = None
        self._poll_job = None

        self._name = None
        self._payload = None
        self._surface_stale = True
        self._surface_artists = None

        if show_3d:
            self.notebook = ttk.Notebook(self)
            self.notebook.pack(fill="both", expand=True)
            relief_tab = ttk.Frame(self.notebook)
            surface_tab = ttk.Frame(self.notebook)
            self.notebook.add(relief_tab, text="Shaded Relief")
            self.notebook.add(surface_tab, text="3D Surface")
            self.figure, self.canvas = self._make_canvas(
                relief_tab, figure_dpi, "constrained")
            self.surface_figure, self.surface_canvas = self._make_canvas(
                surface_tab, figure_dpi, None)
            # Coarse mesh while rotating, full mesh on release.
            self.surface_canvas.mpl_connect(
                "button_press_event",
                lambda e: e.inaxes is not None and self._set_surface_detail(True))
            self.surface_canvas.mpl_connect(
                "button_release_event", lambda e: self._set_surface_detail(False))
            self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        else:
            self.notebook = None
            self.figure, self.canvas = self._make_canvas(self, figure_dpi, "constrained")
            self.surface_figure = self.surface_canvas = None

        self.show_message("No map selected")

    @staticmethod
    def _make_canvas(parent, figure_dpi, layout):
        figure = Figure(figsize=(7.0, 6.0), dpi=figure_dpi, layout=layout)
        canvas = FigureCanvasTkAgg(figure, master=parent)
        canvas.get_tk_widget().pack(fill="both", expand=True)
        NavigationToolbar2Tk(canvas, parent).update()
        return figure, canvas

    @property
    def busy(self):
        return self._awaiting is not None

    def _surface_showing(self):
        return (self.notebook is not None
                and self.notebook.index(self.notebook.select()) == 1)

    def show_path(self, path):
        """Shows a loading indicator, then loads and renders `path` off-thread."""
        name = os.path.basename(path)
        self._load_token += 1
        token = self._load_token
        self._awaiting = token
        # A crop rectangle belongs to the map it was drawn on.
        self._crop_bounds = None

        self._on_title(f"loading {name}...")
        self.show_message(f"Loading  {name} ...")
        self._on_status(f"Loading {name} ...")

        def worker():
            try:
                data = load_dem_array(path)
                self._load_queue.put((token, "ok", name, self._prepare(data)))
            except Exception as exc:
                self._load_queue.put((token, "error", name, str(exc)))

        threading.Thread(target=worker, daemon=True).start()
        if self._poll_job is None:
            self._poll_job = self.after(80, self._poll_load)

    def show_array(self, name, data, full_shape=None):
        """
        Draws an elevation array already in memory (on the main thread).

        `full_shape` is the shape of the map `data` was reduced from, such as a
        remote GeoTIFF read at an overview level: the axes, the read-outs and
        the crop rectangle then work in that map's full-resolution pixels.
        """
        self._load_token += 1           # drop any file still loading
        self._awaiting = None
        self._crop_bounds = None
        payload = self._prepare(data)
        if full_shape is not None and tuple(full_shape) != tuple(data.shape):
            payload['orig_shape'] = tuple(full_shape)
            payload['step'] = max(1, int(round(full_shape[1] / payload['shaded'].shape[1])))
        self._render(name, payload)
        self._on_title(name)
        self._on_loaded(payload['orig_shape'])

    def show_picture(self, path, title):
        """Draws a picture (a ready-made preview, not elevation) in place of the map."""
        # A new token drops any map still loading; show_message clears the drawn
        # map, its crop rectangle and its stored state, so nothing of the
        # previous map is left behind the picture.
        self._load_token += 1
        self._awaiting = None
        self.show_message("")
        if self.notebook is not None:
            self.notebook.select(0)     # a picture has no 3D surface
        self.figure.clear()
        axes = self.figure.add_subplot(111)
        picture =matplotlib.image.imread(path)
        # Archive shaded reliefs are single-channel: drawn in grey, not in a false-colour map.
        axes.imshow(picture, interpolation="bilinear", cmap="gray" if picture.ndim == 2 else None)
        axes.set_title(title, fontsize=9)
        axes.axis("off")
        self.canvas.draw_idle()
        self._on_title(os.path.basename(path))

    def _prepare(self, data):
        """Downsamples and shades the DEM (runs in the worker thread; numpy only)."""
        step = max(1, int(np.ceil(max(data.shape) / self.DISPLAY_MAX_EDGE)))
        disp = data[::step, ::step]

        finite = np.isfinite(disp)
        vmin = float(np.min(disp[finite])) if finite.any() else 0.0
        vmax = float(np.max(disp[finite])) if finite.any() else 1.0
        relief = max(vmax - vmin, 1e-6)
        filled = np.where(finite, disp, (vmin + vmax) / 2.0)

        # Exaggerate so relief is visible regardless of the map's units/scale.
        light = LightSource(azdeg=315, altdeg=45)
        vert_exag = (0.12 * max(disp.shape)) / relief
        shaded = light.shade(filled, cmap=matplotlib.colormaps["gray"],
                             blend_mode="soft", vert_exag=vert_exag)
        return {'orig_shape': data.shape, 'step': step, 'shaded': shaded,
                'elevation': filled, 'vmin': vmin, 'vmax': vmax, 'relief': relief}

    def _poll_load(self):
        """Main-thread pump: renders a finished load, ignoring superseded ones."""
        self._poll_job = None
        try:
            while True:
                token, kind, name, payload = self._load_queue.get_nowait()
                if token != self._load_token:
                    continue  # a newer map was requested; drop this result
                self._awaiting = None
                if kind == "ok":
                    self._render(name, payload)
                    rows, cols = payload['orig_shape']
                    self._on_status(f"Loaded {name}  ({cols} x {rows} px, "
                                    f"relief {payload['relief']:.3g})", level="ok")
                    self._on_loaded(payload['orig_shape'])
                else:
                    self.show_message(f"Could not load\n{name}")
                    self._on_status(f"Failed to load {name}: {payload}", level="error")
                self._on_title(name)
        except queue.Empty:
            pass
        if self._awaiting is not None and self.winfo_exists():
            self._poll_job = self.after(80, self._poll_load)

    def show_message(self, text):
        """Draws a centred status message in place of the plots."""
        self._payload = None
        self._surface_artists = None
        # The figure is cleared below, taking the crop rectangle's axes with it.
        self._crop_selector = None
        self._relief_axes = None
        figures = [(self.figure, self.canvas)]
        if self.surface_figure is not None:
            figures.append((self.surface_figure, self.surface_canvas))
        for figure, canvas in figures:
            figure.clear()
            axes = figure.add_subplot(111)
            axes.axis("off")
            axes.text(0.5, 0.5, text, ha="center", va="center", fontsize=12,
                      transform=axes.transAxes)
            canvas.draw_idle()

    def _render(self, name, payload):
        """Draws the shaded relief prepared by the worker, and the 3D view if shown."""
        self._name = name
        self._payload = payload
        self._surface_stale = True

        self.figure.clear()
        axes = self.figure.add_subplot(111)
        step = payload['step']
        note = f" (shown at 1/{step})" if step > 1 else ""
        rows, cols = payload['orig_shape']
        # The image spans the map's full-resolution pixel edges, so the axes and
        # the crop rectangle read in the map's own pixels even when the preview
        # is downsampled.
        axes.imshow(payload['shaded'], origin="upper", interpolation="bilinear",
                    extent=(0, cols, rows, 0))
        axes.set_title(
            f"{name}{note}\n{cols} x {rows} px   min {payload['vmin']:.3g}   "
            f"max {payload['vmax']:.3g}   relief {payload['relief']:.3g}", fontsize=9)
        axes.set_xlabel("pixels")
        axes.set_ylabel("pixels")

        mappable = ScalarMappable(
            norm=Normalize(vmin=payload['vmin'], vmax=payload['vmax']), cmap="gray")
        self.figure.colorbar(mappable, ax=axes, shrink=0.8, label="Elevation")

        # figure.clear() above dropped any crop rectangle; put it back.
        self._relief_axes = axes
        self._crop_selector = None
        if self._on_crop is not None:
            self._attach_crop_selector()
        self.canvas.draw_idle()

        if self._surface_showing():
            self._draw_surface()

    # ------------------------------------------------------------ crop
    def enable_crop(self, on_change, bounds=None):
        """
        Shows a draggable crop rectangle; `on_change(bounds)` gets its
        full-resolution bounds whenever the user draws or adjusts it.
        """
        self._on_crop = on_change
        if bounds is not None:
            self._crop_bounds = tuple(bounds)
        if self._relief_axes is not None and self._crop_selector is None:
            self._attach_crop_selector()
            self.canvas.draw_idle()
        self._crop_changed()

    def disable_crop(self):
        """Removes the crop rectangle."""
        self._on_crop = None
        if self._crop_selector is not None:
            self._crop_selector.set_visible(False)
            self._crop_selector.set_active(False)
            self._crop_selector = None
            self.canvas.draw_idle()
        self._crop_changed()

    def set_crop_bounds(self, bounds):
        """Moves the rectangle to `bounds` without reporting it back to the host."""
        self._crop_bounds = tuple(bounds)
        if self._crop_selector is not None and self._payload:
            self._crop_selector.extents = crop_logic.bounds_to_display(self._crop_bounds)
            self.canvas.draw_idle()
        self._crop_changed()

    def _attach_crop_selector(self):
        """Creates the rectangle on the relief axes, at the remembered bounds."""
        shape = self._payload['orig_shape']
        if self._crop_bounds is None:
            self._crop_bounds = crop_logic.default_crop(shape)
        self._crop_selector = RectangleSelector(
            self._relief_axes, self._on_crop_drawn, useblit=True, button=[1],
            interactive=True, minspanx=2, minspany=2, spancoords="data",
            props=self.CROP_PROPS, handle_props=dict(markeredgecolor="#e8923a"),
            ignore_event_outside=False)
        self._crop_selector.extents = crop_logic.bounds_to_display(self._crop_bounds)

    def _on_crop_drawn(self, _press, _release):
        """Converts the drawn rectangle to full-resolution bounds for the host."""
        if self._crop_selector is None or not self._payload or self._on_crop is None:
            return
        x0, x1, y0, y1 = self._crop_selector.extents
        self._crop_bounds = crop_logic.display_to_bounds(
            x0, x1, y0, y1, self._payload['orig_shape'])
        self._on_crop(self._crop_bounds)

    def _on_tab_changed(self, _event=None):
        if self._surface_showing() and self._surface_stale and self._payload:
            self._draw_surface()

    def _surface_region(self):
        """
        (elevation, (row, col, rows, cols)) for the 3D surface: the crop while
        a rectangle is on, so the 3D view shows what goes to Unreal, otherwise
        the whole map. Bounds are full-resolution pixels; the elevation is the
        preview's (downsampled) grid cut to them.
        """
        payload = self._payload
        elevation = payload['elevation']
        rows_full, cols_full = payload['orig_shape']
        if self._on_crop is None or self._crop_bounds is None:
            return elevation, (0, 0, rows_full, cols_full)
        row, col, rows, cols = self._crop_bounds
        scale_y = elevation.shape[0] / rows_full
        scale_x = elevation.shape[1] / cols_full
        y0, x0 = int(row * scale_y), int(col * scale_x)
        # At least two samples a side, or there is no surface to draw.
        y1 = max(y0 + 2, int(np.ceil((row + rows) * scale_y)))
        x1 = max(x0 + 2, int(np.ceil((col + cols) * scale_x)))
        return elevation[y0:y1, x0:x1], (row, col, rows, cols)

    def _crop_changed(self):
        """The rectangle moved or was switched: the 3D surface follows it."""
        self._surface_stale = True
        if self._payload and self._surface_showing():
            self._draw_surface()

    def _draw_surface(self):
        """3D mesh of the loaded map (or its crop), with an auto-fitted vertical exaggeration."""
        elevation, (row0, col0, rows_full, cols_full) = self._surface_region()
        cropped = (rows_full, cols_full) != tuple(self._payload['orig_shape'])
        rows, cols = elevation.shape

        figure = self.surface_figure
        figure.clear()
        self._surface_artists = None
        axes = figure.add_subplot(111, projection="3d")

        span_x, span_y = float(cols_full), float(rows_full)
        span = max(span_x, span_y)
        relief = max(float(np.ptp(elevation)), 1e-6)
        z_box = 0.35
        exaggeration = (z_box * span) / relief
        light = LightSource(azdeg=315, altdeg=45)

        def plot_mesh(detail):
            step = max(1, int(np.ceil(max(rows, cols) / detail)))
            mesh = elevation[::step, ::step]
            # In the map's own pixels, so a crop's axes read where it was cut.
            mesh_x = np.linspace(col0, col0 + span_x, mesh.shape[1])
            mesh_y = np.linspace(row0, row0 + span_y, mesh.shape[0])
            grid_x, grid_y = np.meshgrid(mesh_x, mesh_y)
            cell = span / max(mesh.shape)
            colors = light.shade(mesh, cmap=matplotlib.colormaps["gray"],
                                 blend_mode="soft", vert_exag=exaggeration,
                                 dx=cell, dy=cell)
            # rcount/ccount match the mesh so the face colours stay aligned.
            return axes.plot_surface(grid_x, grid_y, mesh, facecolors=colors,
                                     shade=False, linewidth=0, antialiased=False,
                                     rcount=mesh.shape[0], ccount=mesh.shape[1]), step

        fine, fine_step = plot_mesh(SURFACE_MESH_DETAIL)
        coarse, coarse_step = plot_mesh(SURFACE_DRAG_DETAIL)
        if coarse_step > fine_step:
            coarse.set_visible(False)
            self._surface_artists = (fine, coarse)
        else:
            coarse.remove()

        # No exaggeration factor in the title: the map's resolution is unknown
        # here, so X/Y are in pixels and a ratio against Z would mix units.
        what = (f"crop {cols_full} x {rows_full} px at x {col0}, y {row0}" if cropped
                else "3D surface")
        axes.set_title(f"{self._name} - {what}\n"
                       f"vertical scale auto-fitted", fontsize=9)
        axes.set_xlabel("pixels")
        axes.set_ylabel("pixels")
        axes.set_zlabel("elevation")
        axes.view_init(elev=32, azim=-125)
        axes.set_zlim(float(np.min(elevation)) - relief * 0.15,
                      float(np.max(elevation)) + relief * 0.15)
        axes.zaxis.set_major_locator(MaxNLocator(5))
        axes.set_box_aspect((span_x / span, span_y / span, z_box))
        figure.subplots_adjust(left=0.0, right=0.97, top=0.93, bottom=0.02)
        self.surface_canvas.draw_idle()
        self._surface_stale = False

    def _set_surface_detail(self, coarse):
        """Swaps the coarse mesh in while rotating and back on release."""
        if not self._surface_artists:
            return
        fine, rough = self._surface_artists
        if rough.get_visible() == coarse:
            return
        fine.set_visible(not coarse)
        rough.set_visible(coarse)
        self.surface_canvas.draw()


class MapViewer(tk.Toplevel):
    """
    Standalone window around a MapView, used for one-off previews from the
    studio's export panel. Folder browsing is handled by MapBrowser.
    """

    def __init__(self, master, figure_dpi=100):
        super().__init__(master)
        self.title("Map Viewer")

        # A large, centred window rather than full-screen. Maximised to a wide
        # monitor, a square DEM would sit in a broad empty margin (once the title
        # bar, taskbar and log are subtracted, the plot area is far wider than it
        # is tall). A near-4:3 window keeps the preview big and crisp while the
        # map stays close to the frame; the title-bar button still maximises it.
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        width = min(1200, int(screen_w * 0.64))
        height = min(1040, int(screen_h * 0.90))
        self.geometry(f"{width}x{height}+{max(0, (screen_w - width) // 2)}+"
                      f"{max(0, (screen_h - height) // 3)}")
        self.minsize(560, 520)

        self._log = ActivityLog(self)
        self.view = MapView(self, figure_dpi=figure_dpi,
                            on_status=self.log,
                            on_title=lambda name: self.title(f"Map Viewer - {name}"))

        # Pack the fixed-height log first so the expanding plot cannot squeeze it.
        self._log.pack(side="bottom", fill="x")
        self.view.pack(side="top", fill="both", expand=True)

    def log(self, text, level=None):
        """Appends a line to the viewer's activity log."""
        self._log.log(text, level)

    def show_path(self, path):
        self.view.show_path(path)


class MapBrowser(ttk.Frame):
    """
    Map list on the left, rendered map on the right, updated on selection.

    The selected map becomes the app's current map for the other stages.
    """

    # Default list width in pixels; fits a nested "<folder>/<file>" name.
    LIST_WIDTH = 420

    def __init__(self, master, figure_dpi=100, initial_path=None):
        super().__init__(master, padding=(8, 8, 8, 8))

        self.app = master
        self._paths = {}          # tree item id -> file path
        self._extra_folders = []  # folders added through "Add folder..."
        self._selected = None
        self._show_job = None     # pending debounced load

        self.header = ViewHeader(self, "Browse Maps", self.app.show_home,
                                 subtitle="select a map to view it")
        self.header.pack(fill="x", pady=(0, 8))

        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True)

        # --- left: the map list ---
        left = ttk.Frame(panes, padding=(8, 8, 4, 8))
        right = ttk.Frame(panes, padding=(4, 8, 8, 8))
        panes.add(left, weight=0)
        panes.add(right, weight=1)
        self._panes = panes

        ttk.Label(left, text="Maps", style="Header.TLabel").pack(anchor="w")

        tree_frame = ttk.Frame(left)
        tree_frame.pack(fill="both", expand=True, pady=(4, 6))
        self.tree = ttk.Treeview(tree_frame, show="tree", selectmode="browse")
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        self.include_all_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(left, text="Include masks and other rasters",
                        variable=self.include_all_var,
                        command=self.refresh).pack(anchor="w")

        buttons = ttk.Frame(left)
        buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(buttons, text="Refresh", command=self.refresh).pack(side="left")
        ttk.Button(buttons, text="Add folder...",
                   command=self.add_folder).pack(side="left", padx=(6, 0))

        self.count_var = tk.StringVar()
        ttk.Label(left, textvariable=self.count_var,
                  style="Readout.TLabel").pack(anchor="w", pady=(6, 0))

        # --- right: the map itself ---
        self._log = ActivityLog(right, height=6)
        self.view = MapView(right, figure_dpi=figure_dpi, on_status=self.log,
                            on_title=self.header.set_subtitle, show_3d=True)
        self._log.pack(side="bottom", fill="x")
        self.view.pack(side="top", fill="both", expand=True)

        self.refresh()
        if initial_path:
            self.select_path(initial_path)

        # Sash position needs a realized width; placed once the view is packed.
        self.after_idle(self._place_sash)

    def _place_sash(self, attempt=0):
        """Gives the list its default width, retrying while the panes settle."""
        if not self.winfo_exists():
            return
        self.update_idletasks()
        span = self._panes.winfo_width()
        if span > self.LIST_WIDTH * 2:
            self._panes.sashpos(0, self.LIST_WIDTH)
            self.update_idletasks()
            if abs(self._panes.sashpos(0) - self.LIST_WIDTH) <= 2:
                return
        if attempt < 6:
            self.after(60, lambda: self._place_sash(attempt + 1))

    def log(self, message, level=None):
        self._log.log(message, level)

    # ----------------------------------------------------------- the list
    def _folders(self):
        """(label, path) for every folder shown, project ones first."""
        folders = [("input_maps", paths.INPUT_MAPS_DIR), ("outputs", paths.OUTPUT_ROOT)]
        folders += [(os.path.basename(p.rstrip(os.sep)) or p, p)
                    for p in self._extra_folders]
        return folders

    def refresh(self):
        """Rebuilds the list, keeping the current map selected where possible."""
        keep = self._selected
        self.tree.delete(*self.tree.get_children())
        self._paths.clear()

        include_all = self.include_all_var.get()
        total = 0
        for label, folder in self._folders():
            exists = os.path.isdir(folder)
            entries = scan_dem_folder(folder, include_companions=include_all)

            # Project folders are always listed, even before they exist.
            count = f"({len(entries)})" if exists else "(not created yet)"
            parent = self.tree.insert("", "end", text=f"{label}/  {count}", open=True)
            if not entries:
                self.tree.insert(parent, "end", tags=("empty",),
                                 text="(no maps yet)" if exists else "(no folder yet)")
            for name, path in entries:
                item = self.tree.insert(parent, "end", text=name)
                self._paths[item] = path
            total += len(entries)

        self.count_var.set(f"{total} map{'' if total == 1 else 's'}"
                           + ("  (masks included)" if include_all else ""))
        if keep:
            self.select_path(keep, render=False)

    def add_folder(self):
        """Adds any folder on disk to the list."""
        folder = filedialog.askdirectory(parent=self, title="Add a folder of maps",
                                         initialdir=map_browse_dir())
        if not folder:
            return
        folder = os.path.normpath(folder)
        if folder in self._extra_folders:
            self.log(f"{folder} is already listed.", level="warn")
            return
        self._extra_folders.append(folder)
        self.refresh()
        self.log(f"Added {folder}")

    def select_path(self, path, render=True):
        """Selects `path` in the tree if it is listed; returns whether it was."""
        target = os.path.normcase(os.path.normpath(path))
        for item, item_path in self._paths.items():
            if os.path.normcase(os.path.normpath(item_path)) == target:
                self.tree.selection_set(item)
                self.tree.see(item)
                if render:
                    self.show(item_path)
                return True
        return False

    def _on_select(self, _event=None):
        selection = self.tree.selection()
        if not selection:
            return
        path = self._paths.get(selection[0])
        if path:
            self.show(path)

    # Debounce, so moving quickly through the list loads only the final map.
    SELECTION_DELAY_MS = 250

    def show(self, path):
        if path == self._selected:
            return
        self._selected = path
        # The viewed map becomes the current map for the other stages.
        self.app._set_unreal_source(path)
        if self._show_job is not None:
            self.after_cancel(self._show_job)
        self._show_job = self.after(self.SELECTION_DELAY_MS, self._show_now)

    def _show_now(self):
        self._show_job = None
        if self._selected and self.winfo_exists():
            self.view.show_path(self._selected)

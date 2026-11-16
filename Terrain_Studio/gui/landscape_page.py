"""
Send to Unreal view: converts an existing DEM to Unreal's 16-bit heightmap and
builds the Landscape in a running Unreal Editor.
"""

import io
import os
import queue
import subprocess
import threading
import traceback
from contextlib import redirect_stdout

import tkinter as tk
from tkinter import filedialog, ttk

from gui import crop_logic, paths
from gui.backend import terrain_core, send_terrain, LOADER_AVAILABLE, LOADER_IMPORT_ERROR
from gui.crop_panel import CropPanel
from gui.dem_files import DEM_FILETYPES, dem_source_note, dem_source_problem, drop_dem_menu
from gui.map_views import MapView
from gui.paths import map_browse_dir, output_group_dir
from gui.widgets import (ActivityLog, ProgressPanel, Tooltip, ViewHeader, ask_confirmation,
                         format_path_block, show_message)


IMPORT_STEPS = (
    "To apply it to a Landscape in Unreal:\n"
    "    1. Enter Landscape Mode (Shift + 2).\n"
    "    2. Select a Landscape sized to the heightmap. (With 'Dispatch\n"
    "       to running Unreal Editor' enabled the studio spawns it and\n"
    "       applies the heightmap itself.)\n"
    "    3. Right-click it in the Manage tab list and choose 'Import'.\n"
    "    4. Set the Heightmap File to the path above.\n"
    "    5. Click Import.\n\n"
    "The relief is applied immediately.")


class UnrealUnavailable(RuntimeError):
    """No Unreal Editor answered the Remote Execution broadcast."""


def convert_and_dispatch(source_path, resolution, dispatch, output_dir,
                         log, progress, dialog, cancelled, save_level=True, nanite=True):
    """
    Converts a DEM to Unreal's 16-bit heightmap and optionally spawns the
    Landscape actor. Runs on a worker thread.

    Reporting is injected so the studio footer and the export view can share
    one pipeline:

        log(text, level=None)    one line of progress text
        progress(message, frac)  status text and a 0..1 bar position
        dialog(title, message)   a modal dialog, raised on the main thread
        cancelled()              True once the user has asked to stop

    Returns the path of the written heightmap.
    """
    os.makedirs(output_dir, exist_ok=True)
    progress("Converting DEM to Unreal 16-bit heightmap", 0.2)

    # The loader prints its pipeline status; funnel it into the caller's log.
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        config, interp_resolution, z_scale, heightmap, z_offset = \
            send_terrain.generate_lunar_terrain(source_path, output_dir, resolution)
    for line in buffer.getvalue().splitlines():
        if line.strip():
            log(line)

    heightmap_path = os.path.normpath(os.path.join(output_dir, heightmap))
    log(f"Heightmap written: {heightmap_path}", level="ok")

    if not dispatch:
        # No dispatch: show the manual import instructions and the file path.
        dialog("Heightmap ready for Unreal",
               "The 16-bit heightmap has been written to:\n\n"
               + format_path_block(heightmap_path) + "\n\n" + IMPORT_STEPS)
        progress("Conversion complete (dispatch disabled)", 1.0)
        return heightmap_path

    # Cancellation checkpoint between the (uninterruptible) conversion and the
    # network dispatch.
    if cancelled():
        raise terrain_core.TerrainCancelled()

    progress("Searching for a running Unreal Editor", 0.7)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        send_terrain.send_to_unreal(config, interp_resolution, z_scale, heightmap,
                                    z_offset=z_offset, output_directory=output_dir,
                                    save_level=save_level, nanite=nanite)
    output = buffer.getvalue()
    for line in output.splitlines():
        if line.strip():
            log(line, level="error" if "[-]" in line else None)
    if "[-]" in output:
        # Report Unreal's own reason (e.g. it is in Play) rather than a guess.
        reason = next(line.split("[-]", 1)[1].strip() for line in output.splitlines() if "[-]" in line)
        raise UnrealUnavailable(
            f"Unreal did not build the Landscape: {reason} The heightmap is written: "
            f"{heightmap_path}")
    # This text is logged by unreal_landscape.create_landscape; keep the two in step.
    if "Heightmap not applied" in output:
        # The Landscape was spawned flat: the manual import steps, shown here
        # rather than in a modal dialog that would freeze the editor.
        dialog("Import the heightmap in Unreal",
               "The Landscape was created, but its heightmap was not applied "
               "automatically. Heightmap:\n\n" + format_path_block(heightmap_path)
               + "\n\n" + IMPORT_STEPS)
    progress("Dispatch complete", 1.0)
    return heightmap_path


# =========================================================================
# MAIN APPLICATION
# =========================================================================


class LandscapePage(ttk.Frame):
    """
    View that converts an existing DEM to an Unreal Landscape.

    Offers only the inputs the export reads: the map, its resolution, and
    whether to dispatch to a running editor.
    """

    def __init__(self, parent, source_path=None, resolution=1.0, figure_dpi=100):
        super().__init__(parent, padding=(8, 8, 8, 8))
        self.app = parent

        self.source_path = None
        self.destination_path = None
        self.worker = None
        self._cancel_event = threading.Event()
        self._queue = queue.Queue()

        self._build(resolution, figure_dpi=figure_dpi)
        self.after_idle(self._place_sash)

        if source_path:
            self.set_source(source_path, announce=False)
        else:
            self.log("Pick a DEM with Browse, then Send to Unreal.")
        if not LOADER_AVAILABLE:
            self.log(f"Unreal loader modules are not importable: {LOADER_IMPORT_ERROR}",
                     level="error")
            self.send_button.state(["disabled"])

        self._drain()

    # ------------------------------------------------------------- layout
    # Controls column width in pixels; the drag handle adjusts it from there.
    CONTROLS_WIDTH = 400

    def _build(self, resolution, figure_dpi=100):
        self.header = ViewHeader(
            self, "Send a Map to Unreal", self._on_close,
            subtitle="DEM to 16-bit heightmap, then the Landscape actor")
        self.header.pack(fill="x", pady=(0, 8))

        # Controls on the left, map preview on the right.
        self._panes = ttk.PanedWindow(self, orient="horizontal")
        self._panes.pack(fill="both", expand=True)

        outer = ttk.Frame(self._panes, padding=(2, 0, 6, 0))
        right = ttk.Frame(self._panes, padding=(6, 0, 2, 0))
        self._panes.add(outer, weight=0)
        self._panes.add(right, weight=1)

        # --- source map ---
        source_group = ttk.Labelframe(outer, text="Source map",
                                      style="Group.TLabelframe", padding=8)
        source_group.pack(fill="x")

        self.source_var = tk.StringVar(value="(no map selected)")
        self.source_entry = ttk.Entry(source_group, textvariable=self.source_var,
                                      font=("Consolas", 8), state="readonly")
        self.source_entry.pack(fill="x")
        Tooltip(self.source_entry,
                "The DEM to convert. Long paths scroll: the filename is kept\n"
                "in view because that is the part worth checking.")

        source_buttons = ttk.Frame(source_group)
        source_buttons.pack(fill="x", pady=(6, 0))
        self.browse_button = ttk.Button(source_buttons, text="Browse  \u25be",
                                        command=self.open_source_menu)
        self.browse_button.pack(side="left")
        Tooltip(self.browse_button,
                "Lists the DEMs already in input_maps/ and outputs/, or opens\n"
                "the file picker for a map kept anywhere else.")

        ttk.Label(source_group, style="HomeHint.TLabel", justify="left",
                  text="An elevation map in metres "
                       "(.npy, .csv, .txt, .tif, .tiff).").pack(
            anchor="w", pady=(6, 0))

        # Empty unless the pick looks wrong; the menu filters these out but the
        # file picker cannot.
        self.source_note_var = tk.StringVar()
        ttk.Label(source_group, textvariable=self.source_note_var, justify="left",
                  style="Readout.TLabel", foreground="#b8860b",
                  wraplength=self.CONTROLS_WIDTH - 60).pack(anchor="w")

        # --- crop ---
        self.crop = CropPanel(
            outer, get_resolution=lambda: self._read_resolution(quiet=True),
            toggle_text="Send only a region of the map",
            toggle_tip="Draws a rectangle on the map: drag to draw a new one, drag its\n"
                       "inside to move it and its handles to resize it. The region is\n"
                       "saved as its own map, so rocks, masks and the sun use it too.",
            buttons=[("Save crop", self.save_crop,
                      "Writes the region as a new map in outputs/<name>_crop_.../ and\n"
                      "makes it the current map, without sending it. Send to Unreal\n"
                      "saves the crop too, when the region is on.")],
            wraplength=self.CONTROLS_WIDTH - 60)
        self.crop.pack(fill="x", pady=(8, 0))
        self.save_crop_button = self.crop.buttons["Save crop"]

        # --- settings ---
        settings = ttk.Labelframe(outer, text="Conversion",
                                  style="Group.TLabelframe", padding=8)
        settings.pack(fill="x", pady=(8, 0))
        settings.columnconfigure(1, weight=1)

        resolution_label = ttk.Label(settings, text="Resolution (m/px)",
                                     font=("Segoe UI", 8))
        resolution_label.grid(row=0, column=0, sticky="w")
        Tooltip(resolution_label,
                "Ground distance covered by one pixel. Drives the Unreal\n"
                "landscape XY scale; it does not change the relief.\n"
                "GeoTIFFs carry their own resolution and ignore this.")
        self.resolution_var = tk.StringVar(value=f"{resolution:g}")
        ttk.Entry(settings, textvariable=self.resolution_var, width=12,
                  justify="right", font=("Consolas", 9)).grid(
            row=0, column=1, sticky="w", padx=(8, 0))
        # The crop read-out gives sizes in metres, which follow this field.
        self.resolution_var.trace_add("write", lambda *_: self.crop.refresh_readout())

        self.dispatch_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(settings, text="Dispatch to running Unreal Editor",
                        variable=self.dispatch_var).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(settings, style="HomeHint.TLabel",
                  text="Unticked, the heightmap is written but no editor is "
                       "contacted.").grid(row=2, column=0, columnspan=2, sticky="w")
        self.save_level_var = tk.BooleanVar(value=True)
        save_check = ttk.Checkbutton(settings, text="Save the level afterwards",
                                     variable=self.save_level_var)
        save_check.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        Tooltip(save_check, 'Saves the open Unreal level once the Landscape is spawned and its\nheightmap applied.')
        self.nanite_var = tk.BooleanVar(value=True)
        nanite_check = ttk.Checkbutton(settings, text="Nanite Landscape",
                                       variable=self.nanite_var)
        nanite_check.grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))
        Tooltip(nanite_check,
                "Renders large Landscapes faster. Unreal rebuilds the Nanite mesh\n"
                "on every save, which freezes the editor: about 15 s more for a\n"
                "1009 x 1009 Landscape, longer for bigger ones. Untick it for quick\n"
                "saves while iterating.")

        # --- destination ---
        destination = ttk.Frame(outer)
        destination.pack(fill="x", pady=(8, 0))
        ttk.Label(destination, text="Writes to", font=("Segoe UI", 8)).pack(anchor="w")
        self.destination_var = tk.StringVar(value="-")
        # No wraplength: the path is pre-broken at its separators.
        self.destination_label = ttk.Label(destination,
                                           textvariable=self.destination_var,
                                           style="Readout.TLabel", justify="left")
        self.destination_label.pack(anchor="w", fill="x")
        destination.bind("<Configure>", self._reflow_destination)
        self.open_folder_button = ttk.Button(destination, text="Open output folder",
                                             command=self.open_output_folder,
                                             state="disabled")
        self.open_folder_button.pack(anchor="w", pady=(4, 0))

        # --- progress ---
        self.progress = ProgressPanel(outer)
        self.progress.pack(fill="x", pady=(10, 0))

        # --- actions ---
        actions = ttk.Frame(outer)
        actions.pack(side="bottom", fill="x", pady=(10, 0))
        self.send_button = ttk.Button(actions, text="Send to Unreal",
                                      style="Accent.TButton", command=self.send)
        self.send_button.pack(side="right")
        self.stop_button = ttk.Button(actions, text="Stop", command=self.stop,
                                      state="disabled")
        self.stop_button.pack(side="right", padx=(0, 6))
        # Home lives in the view header.

        # --- right: the map, with the log beneath it ---
        self._log = ActivityLog(right, height=6)
        self.view = MapView(right, figure_dpi=figure_dpi, on_status=self.log,
                            on_loaded=self.crop.map_loaded, show_3d=True)
        self.crop.view = self.view
        self._log.pack(side="bottom", fill="x")
        self.view.pack(side="top", fill="both", expand=True)

    # -------------------------------------------------------------- input
    def set_source(self, path, announce=True):
        """Points the export at `path` and refreshes the destination readout."""
        self.source_path = path
        self.source_var.set(path)
        # Scroll to the end: a full path overflows the field, and the filename
        # is the half worth reading, not the drive letter.
        self.source_entry.after_idle(self.source_entry.xview_moveto, 1.0)
        self.destination_path = output_group_dir(path)
        self._reflow_destination()
        self.source_note_var.set(dem_source_note(path))
        self.open_folder_button.state(["!disabled"])
        # A new map starts uncropped; its rectangle is placed once it is drawn.
        self.crop.reset()
        # Preview the map as soon as it is selected.
        self.view.show_path(path)
        if announce:
            self.log(f"Source map: {path}")
        # A GeoTIFF knows its pixel size and a saved crop recorded its own, so
        # the field is filled from the map rather than left at a guess.
        native = crop_logic.native_resolution(path)
        if native and abs(native - (self._read_resolution(quiet=True) or 0)) > 1e-9:
            self.resolution_var.set(f"{native:g}")
            self.log(f"Resolution set to {native:g} m/px, from the map itself.")

    def open_with_crop(self, path):
        """Shows `path` with the crop turned on, for a map too large to send whole."""
        self.set_source(path)
        self.crop.set_enabled(True)   # the rectangle is placed once the map is drawn

    def _reflow_destination(self, event=None):
        """
        Re-lays the destination path for the column's current width.
        """
        if not self.destination_path:
            self.destination_var.set("-")
            return
        # During <Configure>, winfo_width() still reports the old width.
        available = event.width if event is not None else \
            self.destination_label.winfo_width()
        available = max(available, self.CONTROLS_WIDTH - 40)
        # Consolas 8 is about 6.6 px per character; leave a margin for padding.
        columns = max(24, int(available / 6.6) - 2)
        self.destination_var.set(format_path_block(self.destination_path,
                                                   width=columns, indent=""))

    def _place_sash(self, attempt=0):
        """Gives the controls column its width once the panes have a real size."""
        if not self.winfo_exists():
            return
        self.update_idletasks()
        if self._panes.winfo_width() > self.CONTROLS_WIDTH * 2:
            self._panes.sashpos(0, self.CONTROLS_WIDTH)
            self.update_idletasks()
            if abs(self._panes.sashpos(0) - self.CONTROLS_WIDTH) <= 2:
                return
        if attempt < 6:
            self.after(60, lambda: self._place_sash(attempt + 1))

    def open_source_menu(self):
        """Drops the map list under the Browse button, rebuilt so it stays current."""
        drop_dem_menu(self, self.browse_button, self.set_source, self.browse)

    def browse(self, initialdir=None):
        path = filedialog.askopenfilename(
            parent=self, title="Select a DEM to send to Unreal",
            initialdir=initialdir or map_browse_dir(), filetypes=DEM_FILETYPES)
        if path:
            self.set_source(path)

    def open_output_folder(self):
        """Opens the folder the heightmap is written to."""
        target = output_group_dir(self.source_path) if self.source_path else paths.OUTPUT_ROOT
        os.makedirs(target, exist_ok=True)
        try:
            os.startfile(target)  # noqa: S606 - Windows shell open
        except AttributeError:  # pragma: no cover - non-Windows fallback
            subprocess.Popen(["xdg-open", target])

    def _read_resolution(self, quiet=False):
        """The resolution entry as a positive float, or None if it is unusable."""
        try:
            value = float(self.resolution_var.get().strip())
        except ValueError:
            if not quiet:
                self.log("Resolution must be a number, e.g. 1.0", level="error")
            return None
        if not value > 0:
            if not quiet:
                self.log("Resolution must be greater than zero.", level="error")
            return None
        return value

    # ------------------------------------------------------------- action
    def send(self):
        """Converts the map (or its crop) and sends it to Unreal on a worker thread."""
        if self._busy():
            self.log("A conversion is already running.", level="warn")
            return

        problem = dem_source_problem(self.source_path)
        if problem:
            self.log(problem[0], level=problem[1])
            return

        resolution = self._read_resolution()
        if resolution is None:
            return

        source_path = self.source_path
        dispatch = self.dispatch_var.get()
        save_level = self.save_level_var.get()
        nanite = self.nanite_var.get()
        crop_bounds = self.crop.bounds if self.crop.active() else None

        if crop_bounds:
            row, col, rows, cols = crop_bounds
            self.log(f"Sending a {cols} x {rows} px region of "
                     f"{os.path.basename(source_path)} to Unreal")
        else:
            self.log(f"Sending to Unreal: {os.path.basename(source_path)}")
        # The size Unreal gets: the region, or the whole map once it is drawn.
        sent_shape = crop_bounds[2:] if crop_bounds else self.crop.map_shape
        warning = crop_logic.nanite_warning(sent_shape) if nanite and sent_shape else None
        if warning and dispatch:
            self.log(warning, level="warn")
        self._cancel_event.clear()
        self.progress.start("Sending to Unreal...", "dispatch", max(os.path.getsize(source_path) / 1e6, 0.01) if os.path.exists(source_path) else 1.0)
        self._set_running(True)

        def job():
            try:
                # The crop is written first as its own map, so the Landscape
                # and every later step share the same subsection.
                map_path = (self._cut_crop(source_path, crop_bounds, resolution)
                            if crop_bounds else source_path)
                convert_and_dispatch(
                    map_path, resolution, dispatch, output_group_dir(map_path),
                    log=lambda text, level=None: self._post("log", (text, level)),
                    progress=lambda message, fraction: self._post(
                        "progress", (message, fraction)),
                    dialog=lambda title, message: self._post(
                        "dialog", (title, message)),
                    cancelled=self._cancel_event.is_set, save_level=save_level,
                    nanite=nanite)
            except terrain_core.TerrainCancelled:
                self._post("cancelled", None)
            except UnrealUnavailable as exc:
                self._post("failed", str(exc))
            except Exception:
                self._post("error", traceback.format_exc())
            finally:
                self._post("done", None)

        self.worker = threading.Thread(target=job, daemon=True)
        self.worker.start()

    def save_crop(self):
        """Writes the selected region as a new map and switches to it, without sending."""
        if self._busy():
            self.log("A conversion is already running.", level="warn")
            return
        if not self.crop.active():
            self.log("Turn on 'Send only a region' and draw a region smaller than the map.",
                     level="warn")
            return
        resolution = self._read_resolution()
        if resolution is None:
            return
        source_path, bounds = self.source_path, self.crop.bounds
        self._cancel_event.clear()
        self.progress.start("Saving the crop...", "crop", 1.0)
        self._set_running(True)

        def job():
            try:
                self._cut_crop(source_path, bounds, resolution)
            except Exception:
                self._post("error", traceback.format_exc())
            finally:
                self._post("done", None)

        self.worker = threading.Thread(target=job, daemon=True)
        self.worker.start()

    def _cut_crop(self, source_path, bounds, resolution):
        """
        Saves the crop (worker thread) and has the view switch to it; returns
        the new map's path.
        """
        self._post("progress", ("Cutting the selected region", 0.05))
        dem_path, info = crop_logic.save_crop(source_path, bounds, resolution)
        note = (f", {info['missing_fraction']:.1%} of it without data"
                if info["missing_fraction"] else "")
        self._post("log", (f"Crop saved: {dem_path}{note}", "ok"))
        self._post("source", dem_path)
        return dem_path

    def _set_running(self, running):
        """Locks the actions while a job runs, and frees them after."""
        # Without the loader modules Send stays off; saving a crop still works.
        self.send_button.state(["disabled"] if running or not LOADER_AVAILABLE else ["!disabled"])
        self.stop_button.state(["!disabled"] if running else ["disabled"])
        self.crop.update_states(busy=running)

    def stop(self):
        if not self._busy():
            return
        self._cancel_event.set()
        self.stop_button.state(["disabled"])
        self.progress.note("Cancelling...")
        self.log("Cancellation requested; stopping at the next checkpoint...",
                 level="warn")

    def _busy(self):
        return self.worker is not None and self.worker.is_alive()

    # --------------------------------------------------------- main pump
    def _post(self, kind, payload):
        self._queue.put((kind, payload))

    def _drain(self):
        """Main-thread pump: every Tk call for this window happens here."""
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "log":
                    text, level = payload
                    self.log(text, level=level)
                elif kind == "progress":
                    self.progress.update(*payload)
                elif kind == "dialog":
                    title, message = payload
                    show_message(self, title, message)
                elif kind == "source":
                    # A crop was saved: it is now the map, here and for the
                    # other steps, and it needs no rectangle of its own.
                    self.crop.set_enabled(False)
                    self.set_source(payload)
                    if hasattr(self.app, "_set_unreal_source"):
                        self.app._set_unreal_source(payload)
                    self.log("The crop is now the current map for the other steps.")
                elif kind == "cancelled":
                    self.log("Conversion cancelled.", level="warn")
                    self.progress.finish("cancelled")
                elif kind in ("error", "failed"):
                    self.log(payload, level="error" if kind == "error" else "warn")
                    self.progress.finish("failed")
                elif kind == "done":
                    self._set_running(False)
                    self.progress.finish()
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self._drain)

    def log(self, text, level=None):
        self._log.log(text, level)

    def _on_close(self):
        """Back to the home screen, asking first if a conversion is in flight."""
        if self._busy():
            if not ask_confirmation(
                    self, "Conversion running",
                    "A conversion is still running.\n\nLeave anyway?",
                    ok_label="Leave"):
                return
            # Daemon worker; its remaining work only writes files.
            self._cancel_event.set()
        self.app.show_home()

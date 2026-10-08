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

from gui import paths
from gui.backend import terrain_core, send_terrain, LOADER_AVAILABLE, LOADER_IMPORT_ERROR
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
                         log, progress, dialog, cancelled, save_level=True):
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
                                    save_level=save_level)
    output = buffer.getvalue()
    for line in output.splitlines():
        if line.strip():
            log(line, level="error" if "[-]" in line else None)
    if "[-]" in output:
        raise UnrealUnavailable(
            "Unreal did not complete the import. Check that the editor is running with the "
            "Python Editor Script Plugin and 'Remote Execution' enabled, then retry. The "
            f"heightmap is written: {heightmap_path}")
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
        self.view = MapView(right, figure_dpi=figure_dpi, on_status=self.log)
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
        # Preview the map as soon as it is selected.
        self.view.show_path(path)
        if announce:
            self.log(f"Source map: {path}")

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

    def _read_resolution(self):
        """The resolution entry as a positive float, or None if it is unusable."""
        try:
            value = float(self.resolution_var.get().strip())
        except ValueError:
            self.log("Resolution must be a number, e.g. 1.0", level="error")
            return None
        if not value > 0:
            self.log("Resolution must be greater than zero.", level="error")
            return None
        return value

    # ------------------------------------------------------------- action
    def send(self):
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
        output_dir = output_group_dir(source_path)

        self.log(f"Sending to Unreal: {os.path.basename(source_path)}")
        self._cancel_event.clear()
        self.progress.start("Sending to Unreal...", "dispatch", max(os.path.getsize(source_path) / 1e6, 0.01) if os.path.exists(source_path) else 1.0)
        self.send_button.state(["disabled"])
        self.stop_button.state(["!disabled"])

        def job():
            try:
                convert_and_dispatch(
                    source_path, resolution, dispatch, output_dir,
                    log=lambda text, level=None: self._post("log", (text, level)),
                    progress=lambda message, fraction: self._post(
                        "progress", (message, fraction)),
                    dialog=lambda title, message: self._post(
                        "dialog", (title, message)),
                    cancelled=self._cancel_event.is_set, save_level=save_level)
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
                elif kind == "cancelled":
                    self.log("Conversion cancelled.", level="warn")
                    self.progress.finish("cancelled")
                elif kind in ("error", "failed"):
                    self.log(payload, level="error" if kind == "error" else "warn")
                    self.progress.finish("failed")
                elif kind == "done":
                    self.send_button.state(["!disabled"])
                    self.stop_button.state(["disabled"])
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

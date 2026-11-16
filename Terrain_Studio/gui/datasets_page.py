"""
Lunar Datasets view: browse public lunar DEMs (LROC NAC DTMs and LOLA
south-pole maps), preview them, download them whole or crop a region on the
server, then send them to Unreal. The data handling is in datasets_logic and
remote_tiff; this view lays it out and runs the network work off the Tk thread.
"""

import os
import queue
import subprocess
import threading
import traceback
import webbrowser

import tkinter as tk
from tkinter import ttk

from gui import crop_logic
from gui import datasets_logic as data
from gui.crop_panel import CropPanel
from gui.map_views import MapView
from gui.remote_tiff import Cancelled
from gui.widgets import ActivityLog, ProgressPanel, Tooltip, ViewHeader, ask_confirmation


# Whole files above this ask first: a 5 GB mosaic is rarely what one Landscape needs.
LARGE_DOWNLOAD = 1_000_000_000


class DatasetsPage(ttk.Frame):
    """Catalogue on the left, the selected map's preview on the right."""

    CONTROLS_WIDTH = 440
    SELECTION_DELAY_MS = 300

    def __init__(self, parent, figure_dpi=100):
        super().__init__(parent, padding=(8, 8, 8, 8))
        self.app = parent
        self._queue = queue.Queue()
        self._cancel = threading.Event()
        self.worker = None
        self._select_job = None
        self._select_token = 0
        self.sources, self.entries, self.lroc_fetched = {}, [], None
        self.entry = None              # the selected entry, with its details once fetched
        self._build(figure_dpi)
        self.after_idle(self._place_sash)
        self.reload_catalog()
        self._drain()

    # ------------------------------------------------------------- layout
    def _build(self, figure_dpi):
        ViewHeader(self, "Lunar Datasets", self._on_close,
                   subtitle="real lunar DEMs: preview, download or crop, then send to Unreal").pack(
            fill="x", pady=(0, 8))
        self._panes = ttk.PanedWindow(self, orient="horizontal")
        self._panes.pack(fill="both", expand=True)
        left = ttk.Frame(self._panes, padding=(2, 0, 6, 0))
        right = ttk.Frame(self._panes, padding=(6, 0, 2, 0))
        self._panes.add(left, weight=0)
        self._panes.add(right, weight=1)

        # --- catalogue ---
        catalogue = ttk.Labelframe(left, text="Catalogue", style="Group.TLabelframe", padding=8)
        catalogue.pack(fill="both", expand=True)
        search_row = ttk.Frame(catalogue)
        search_row.pack(fill="x")
        ttk.Label(search_row, text="Search", font=("Segoe UI", 8)).pack(side="left")
        self.search_var = tk.StringVar()
        search = ttk.Entry(search_row, textvariable=self.search_var)
        search.pack(side="left", fill="x", expand=True, padx=(6, 0))
        Tooltip(search, "Filters by name, title and region, e.g. 'apollo', 'shackleton'\n"
                        "or 'site'. NAC DTM titles appear once a map has been opened.")
        self.search_var.trace_add("write", lambda *_: self._fill_tree())

        tree_frame = ttk.Frame(catalogue)
        tree_frame.pack(fill="both", expand=True, pady=(6, 0))
        self.tree = ttk.Treeview(tree_frame, columns=("res", "size", "disk"), height=9,
                                 selectmode="browse")
        self.tree.heading("#0", text="Map")
        self.tree.heading("res", text="m/px")
        self.tree.heading("size", text="Size")
        self.tree.heading("disk", text="On disk")
        self.tree.column("#0", width=200, stretch=True)
        self.tree.column("res", width=48, anchor="e", stretch=False)
        self.tree.column("size", width=70, anchor="e", stretch=False)
        self.tree.column("disk", width=56, anchor="center", stretch=False)
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        list_row = ttk.Frame(catalogue)
        list_row.pack(fill="x", pady=(6, 0))
        self.count_var = tk.StringVar()
        ttk.Label(list_row, textvariable=self.count_var, style="Readout.TLabel").pack(side="left")
        self.lroc_button = ttk.Button(list_row, text="Load NAC DTM list",
                                      command=self.load_lroc_list)
        self.lroc_button.pack(side="right")
        Tooltip(self.lroc_button,
                "Lists the ~670 LROC NAC DTMs from NASA's PDS (one ~0.5 MB\n"
                "query). The list is kept, so this is needed once, or to refresh.")

        # --- details ---
        details = ttk.Labelframe(left, text="Selected map", style="Group.TLabelframe", padding=8)
        details.pack(fill="x", pady=(8, 0))
        self.details_var = tk.StringVar(value="Select a map in the catalogue.")
        ttk.Label(details, textvariable=self.details_var, justify="left",
                  wraplength=self.CONTROLS_WIDTH - 40).pack(anchor="w")
        self.source_link = ttk.Button(details, text="About this source", command=self._open_source_page)
        self.source_link.pack(anchor="w", pady=(6, 0))

        # --- crop on the server (tiled maps only) ---
        self.crop = CropPanel(
            left, get_resolution=lambda: (self.entry or {}).get("resolution_m"),
            toggle_text="Download only a region",
            toggle_tip="For tiled mosaics: draw the region on the preview, and only the\n"
                       "tiles under it are downloaded, not the whole file. Other maps\n"
                       "are downloaded whole and cropped in Send to Unreal.",
            buttons=[("Download region", self.download_region,
                      "Fetches the region from the server, saves it as its own map in\n"
                      "outputs/<map>_crop_.../ and opens it in Send to Unreal.")],
            wraplength=self.CONTROLS_WIDTH - 40)
        self.crop.pack(fill="x", pady=(8, 0))
        self.crop.set_available(False)

        # --- progress and actions ---
        self.progress = ProgressPanel(left)
        self.progress.pack(fill="x", pady=(8, 0))
        actions = ttk.Frame(left)
        actions.pack(fill="x", pady=(8, 0))
        self.send_button = ttk.Button(actions, text="Send to Unreal", style="Accent.TButton",
                                      command=self.send_to_unreal)
        self.send_button.pack(side="right")
        Tooltip(self.send_button,
                "Opens the map in Send to Unreal, downloading it first if needed.\n"
                "Maps too large for one Landscape open with the crop turned on.")
        self.download_button = ttk.Button(actions, text="Download", command=self.download)
        self.download_button.pack(side="right", padx=(0, 6))
        self.stop_button = ttk.Button(actions, text="Stop", command=self.stop, state="disabled")
        self.stop_button.pack(side="right", padx=(0, 6))
        self.folder_button = ttk.Button(actions, text="Open folder", command=self.open_folder)
        self.folder_button.pack(side="left")

        # --- right: preview and log ---
        self._log = ActivityLog(right, height=6)
        self.view = MapView(right, figure_dpi=figure_dpi, on_status=self.log,
                            on_loaded=self.crop.map_loaded, show_3d=True)
        self.crop.view = self.view
        self._log.pack(side="bottom", fill="x")
        self.view.pack(side="top", fill="both", expand=True)
        self.view.show_message("Select a map to preview it")
        self._update_buttons()

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

    # ---------------------------------------------------------- catalogue
    def reload_catalog(self):
        """Reads the catalogue from disk (no network) and fills the list."""
        try:
            self.sources, self.entries, self.lroc_fetched = data.load_catalog()
        except Exception as exc:
            self.log(f"Could not read config/lunar_datasets.json: {exc}", level="error")
            return
        self.lroc_button.configure(text="Refresh NAC DTM list" if self.lroc_fetched
                                   else "Load NAC DTM list")
        self._fill_tree()
        if not self.lroc_fetched:
            self.log("The LROC NAC DTMs are listed from NASA's PDS: press 'Load NAC DTM list'.")

    def _fill_tree(self):
        """Groups the entries matching the search by source, keeping the selection."""
        selected = self.entry["id"] if self.entry else None
        self.tree.delete(*self.tree.get_children())
        text = self.search_var.get()
        shown = 0
        for key in data.SOURCE_ORDER:
            source = self.sources.get(key, {})
            entries = data.filter_entries(self.entries, key, text)
            parent = self.tree.insert("", "end", iid=f"source:{key}", open=bool(text) or key != "lroc_nac",
                                      text=f"{source.get('label', key)}  ({len(entries)})")
            if key == "lroc_nac" and not self.lroc_fetched:
                self.tree.insert(parent, "end", text="(press 'Load NAC DTM list')", tags=("hint",))
            for entry in entries:
                self.tree.insert(parent, "end", iid=entry["id"], text=entry.get("title") or entry["name"],
                                 values=self._row_values(entry))
            shown += len(entries)
        self.tree.tag_configure("hint", foreground="#888888")
        self.count_var.set(f"{shown} map{'s' if shown != 1 else ''}")
        if selected and self.tree.exists(selected):
            self.tree.selection_set(selected)
            self.tree.see(selected)

    @staticmethod
    def _row_values(entry):
        """The m/px, size and on-disk columns of an entry's row."""
        resolution = entry.get("resolution_m")
        return (f"{resolution:g}" if resolution else "?",
                data.human_size(entry.get("size_bytes")),
                "✓" if data.is_downloaded(entry) else "")

    def _entry_by_id(self, entry_id):
        """The catalogue entry with `entry_id`, or None (a source heading or hint row)."""
        return next((entry for entry in self.entries if entry["id"] == entry_id), None)

    def _update_entry(self, updated):
        """Stores details fetched for an entry and refreshes its row."""
        for index, entry in enumerate(self.entries):
            if entry["id"] == updated["id"]:
                self.entries[index] = dict(entry, **updated)
        if self.tree.exists(updated["id"]):
            self.tree.item(updated["id"], text=updated.get("title") or updated["name"],
                           values=self._row_values(updated))

    def load_lroc_list(self):
        """Fetches (or refreshes) the NAC DTM list from the PDS."""
        if self._busy():
            return
        self._run("Listing the LROC NAC DTMs...", "lroc_list",
                  lambda: ("lroc_list", data.fetch_lroc_entries()))

    # ---------------------------------------------------------- selection
    def _on_select(self, _event=None):
        """Shows the selected entry's details at once and its preview shortly after."""
        selection = self.tree.selection()
        entry = self._entry_by_id(selection[0]) if selection else None
        if entry is None:
            return
        self.entry = dict(entry)
        self._show_details()
        self.crop.reset()
        self.crop.set_available(False)
        self._update_buttons()
        # Debounced: moving through the list only previews where it stops.
        if self._select_job is not None:
            self.after_cancel(self._select_job)
        self._select_job = self.after(self.SELECTION_DELAY_MS, self._preview_selected)

    def _preview_selected(self):
        """Fetches the selected map's details and preview off the Tk thread."""
        self._select_job = None
        entry = self.entry
        if entry is None:
            return
        self._select_token += 1
        token = self._select_token
        self.view.show_message(f"Loading the preview of\n{entry.get('title') or entry['name']} ...")

        def job():
            try:
                details = data.entry_details(entry)
                self._post("details", (token, details))
                if data.is_downloaded(details):
                    self._post("preview_file", (token, data.local_path(details)))
                elif details.get("tiled"):
                    array, shape, resolution = data.remote_preview(details)
                    self._post("preview_array", (token, details, array, shape, resolution))
                elif details.get("preview_url"):
                    self._post("preview_picture", (token, details, data.preview_image(details)))
                else:
                    self._post("preview_none", (token, details))
            except Exception as exc:
                self._post("preview_failed", (token, str(exc)))

        threading.Thread(target=job, daemon=True).start()

    def _show_details(self):
        """Fills the 'Selected map' group from the selected entry."""
        entry = self.entry
        source = self.sources.get(entry["source"], {})
        lines = [entry.get("title") or entry["name"], source.get("label", "")]
        if entry.get("region"):
            lines.append(f"Where: {entry['region']}")
        size = data.human_size(entry.get("size_bytes"))
        resolution = entry.get("resolution_m")
        lines.append(f"{resolution:g} m/px" if resolution else "Resolution: in the README (loading)")
        lines[-1] += f"  ·  {size}  ·  " + ("downloaded" if data.is_downloaded(entry) else "not downloaded")
        if entry.get("features"):
            lines.append("Features: " + ", ".join(entry["features"]))
        if entry.get("tiled"):
            lines.append("Tiled: previewed from the server; a region can be downloaded alone.")
        lines.append(f"Credit: {source.get('credit', '?')}")
        if source.get("reference"):
            lines.append(source["reference"])
        self.details_var.set("\n".join(lines))

    def _open_source_page(self):
        """Opens the selected source's web page (credits, documentation)."""
        source = self.sources.get((self.entry or {}).get("source"), {})
        if source.get("info_url"):
            webbrowser.open(source["info_url"])

    # ------------------------------------------------------------ actions
    def download(self, then_send=False):
        """Downloads the selected map whole (asking first above 1 GB)."""
        entry = self.entry
        if entry is None or self._busy():
            return
        if data.is_downloaded(entry):
            self.log(f"Already downloaded: {data.local_path(entry)}")
            if then_send:
                self._open_in_unreal_view(data.local_path(entry))
            return
        size = entry.get("size_bytes") or 0
        if size > LARGE_DOWNLOAD:
            note = (" It is tiled: 'Download only a region' fetches just the part you need."
                    if entry.get("tiled") else "")
            if not ask_confirmation(self, "Large download",
                                    f"{entry['filename']} is {data.human_size(size)}.{note}\n\n"
                                    f"Download the whole file?", ok_label="Download"):
                return

        def job():
            path = data.download(entry, progress=lambda done, total: self._post(
                "progress", (f"Downloading {data.human_size(done)} of {data.human_size(total)}",
                             done / total if total else 0.0)), cancelled=self._cancel.is_set)
            return ("downloaded", (entry, path, then_send))

        self._run(f"Downloading {entry['filename']}...", "download", job)

    def download_region(self):
        """Fetches only the drawn region of a tiled map, saves it and opens it to send."""
        entry = self.entry
        if entry is None or self._busy() or not self.crop.active():
            if entry is not None and not self.crop.active():
                self.log("Draw a region smaller than the map first.", level="warn")
            return
        bounds = self.crop.bounds

        def job():
            dem_path, info = data.remote_crop(
                entry, bounds, cancelled=self._cancel.is_set,
                progress=lambda fraction: self._post(
                    "progress", ("Fetching the region's tiles", fraction)))
            return ("region", (dem_path, info))

        row, col, rows, cols = bounds
        self._run(f"Downloading a {cols} x {rows} px region...", "region", job)

    def send_to_unreal(self):
        """Opens the map in Send to Unreal: the region if one is drawn, else the whole file."""
        if self.entry is None:
            return
        if self.crop.active():
            self.download_region()
        else:
            self.download(then_send=True)

    def _open_in_unreal_view(self, path):
        """Hands `path` to Send to Unreal, with the crop on when it is too big to send whole."""
        # Read from the file itself: the preview may have been a picture (NAC
        # DTMs) whose shape says nothing about the map's.
        shape = data.map_shape(path)
        too_big = shape is not None and max(shape) > crop_logic.AUTO_IMPORT_MAX
        if hasattr(self.app, "open_in_landscape"):
            self.app.open_in_landscape(path, crop=too_big)

    def stop(self):
        """Asks the running transfer to stop at its next chunk or tile."""
        if self._busy():
            self._cancel.set()
            self.stop_button.state(["disabled"])
            self.progress.note("Stopping...")

    def open_folder(self):
        """Opens the selected map's download folder (input_maps/ when none is selected)."""
        target = (os.path.dirname(data.local_path(self.entry)) if self.entry
                  else os.path.dirname(data.cache_dir()))
        os.makedirs(target, exist_ok=True)
        try:
            os.startfile(target)  # noqa: S606 - Windows shell open
        except AttributeError:  # pragma: no cover - non-Windows fallback
            subprocess.Popen(["xdg-open", target])

    def _update_buttons(self):
        """Enables the actions that make sense for the selection, and Stop while busy."""
        busy = self._busy()
        has_entry = self.entry is not None
        for button in (self.download_button, self.send_button):
            button.state(["!disabled"] if has_entry and not busy else ["disabled"])
        self.stop_button.state(["!disabled"] if busy else ["disabled"])
        self.source_link.state(["!disabled"] if has_entry else ["disabled"])
        self.crop.update_states(busy=busy)

    # ------------------------------------------------------------- worker
    def _busy(self):
        """True while a list, download or region transfer is running."""
        return self.worker is not None and self.worker.is_alive()

    def _run(self, label, kind, work):
        """
        Runs `work` on a worker thread; it returns the (kind, payload) message
        that _handle acts on when it is done. One transfer runs at a time.
        """
        self._cancel.clear()
        self.progress.start(label, kind, 1.0)
        self.log(label)

        def job():
            try:
                self._post(*work())
            except Cancelled:
                self._post("cancelled", None)
            except Exception as exc:
                # A network or file problem: a readable line in the log, with the
                # full traceback kept on the console for debugging.
                traceback.print_exc()
                self._post("error", f"{type(exc).__name__}: {exc}")
            finally:
                self._post("done", None)

        self.worker = threading.Thread(target=job, daemon=True)
        self.worker.start()
        self._update_buttons()

    def _post(self, kind, payload):
        self._queue.put((kind, payload))

    def _drain(self):
        """Main-thread pump: every Tk call for this view happens here."""
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self._drain)

    def _current(self, token):
        """True when a preview result belongs to the map still selected."""
        return token == self._select_token

    def _handle(self, kind, payload):
        """Acts on one message from a worker thread (main thread only)."""
        if kind == "details":
            token, details = payload
            self._update_entry(details)
            if self._current(token) and self.entry and self.entry["id"] == details["id"]:
                self.entry = details
                self._show_details()
        elif kind == "preview_file" and self._current(payload[0]):
            self.view.show_path(payload[1])
        elif kind == "preview_array" and self._current(payload[0]):
            _token, details, array, shape, resolution = payload
            if resolution and not details.get("resolution_m"):
                self.entry = dict(details, resolution_m=resolution)
            self.crop.set_available(True)
            self.view.show_array(details.get("title") or details["name"], array, shape)
            rows, cols = shape
            self.log(f"Preview from the server: {cols} x {rows} px at full resolution.", level="ok")
        elif kind == "preview_picture" and self._current(payload[0]):
            _token, details, path = payload
            self.view.show_picture(path, f"{details.get('title') or details['name']}\n"
                                         f"shaded relief from the archive (download to see the DEM)")
        elif kind == "preview_none" and self._current(payload[0]):
            details = payload[1]
            self.view.show_message(f"No preview before download\n"
                                   f"({data.human_size(details.get('size_bytes'))}: Download to see it)")
        elif kind == "preview_failed" and self._current(payload[0]):
            self.view.show_message("Could not load the preview")
            self.log(f"Preview failed: {payload[1]}", level="error")
        elif kind == "progress":
            self.progress.update(*payload)
        elif kind == "lroc_list":
            self.log(f"Listed {len(payload)} LROC NAC DTMs.", level="ok")
            self.reload_catalog()
        elif kind == "downloaded":
            entry, path, then_send = payload
            self.log(f"Downloaded: {path}", level="ok")
            self._update_entry(entry)
            if self.entry and self.entry["id"] == entry["id"]:
                self._show_details()
                self.view.show_path(path)
            if then_send:
                self._open_in_unreal_view(path)
        elif kind == "region":
            dem_path, info = payload
            note = f", {info['missing_fraction']:.1%} without data" if info["missing_fraction"] else ""
            self.log(f"Region saved: {dem_path}{note}", level="ok")
            if hasattr(self.app, "open_in_landscape"):
                self.app.open_in_landscape(dem_path, crop=False)
        elif kind == "cancelled":
            self.log("Stopped. A whole-file download resumes where it stopped next time.", level="warn")
            self.progress.finish("cancelled")
        elif kind == "error":
            self.log(payload, level="error")
            self.progress.finish("failed")
        elif kind == "done":
            self.progress.finish()
            self._update_buttons()

    def log(self, text, level=None):
        """Appends a line to the view's activity log."""
        self._log.log(text, level)

    def _on_close(self):
        """Back to the home screen; a running transfer is stopped (after asking)."""
        if self._busy():
            if not ask_confirmation(
                    self, "Transfer running",
                    "A download is still running.\n\nStop it and leave? A whole-file "
                    "download resumes where it stopped next time.", ok_label="Stop and leave"):
                return
            self._cancel.set()
        self.app.show_home()

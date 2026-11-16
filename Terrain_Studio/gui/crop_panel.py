"""
The crop group shared by the Send to Unreal and Lunar Datasets views: a
toggle that shows a rectangle on the map, its bounds in pixels, snapping to a
Landscape size and a read-out of the Landscape it makes. The maths is in
crop_logic; the host view supplies the map view, the resolution and its own
action buttons.
"""

import tkinter as tk
from tkinter import ttk

from gui import crop_logic
from gui.widgets import Tooltip


class CropPanel(ttk.Labelframe):
    """
    Crop controls bound to a MapView.

    The host sets `view` once its map view exists, passes `map_loaded` as the
    view's `on_loaded`, calls `reset()` when it switches maps and
    `refresh_readout()` when the resolution changes. `bounds` is the selection
    in full-resolution pixels (row, col, rows, cols) and `active()` tells
    whether a region smaller than the map is selected.
    """

    def __init__(self, parent, get_resolution, toggle_text, toggle_tip,
                 buttons=(), wraplength=340):
        """
        `get_resolution` returns the map's m/px (or None) for the read-out;
        `toggle_text` and `toggle_tip` label the on/off checkbox; `buttons` are
        the host's own (text, command, tooltip) actions, enabled with the crop.
        """
        super().__init__(parent, text="Crop", style="Group.TLabelframe", padding=8)
        self.view = None
        self.map_shape = None
        self.bounds = None
        self._get_resolution = get_resolution
        self.columnconfigure(1, weight=1)
        self.columnconfigure(3, weight=1)

        self.enabled_var = tk.BooleanVar(value=False)
        self._check = ttk.Checkbutton(self, text=toggle_text, variable=self.enabled_var,
                                      command=self._toggle)
        self._check.grid(row=0, column=0, columnspan=4, sticky="w")
        Tooltip(self._check, toggle_tip)

        self.vars = {key: tk.StringVar() for key in ("x", "y", "width", "height")}
        self._widgets = []
        fields = (("x", "X (px)"), ("y", "Y (px)"), ("width", "Width (px)"), ("height", "Height (px)"))
        for index, (key, label) in enumerate(fields):
            row, column = 1 + index // 2, (index % 2) * 2
            ttk.Label(self, text=label, font=("Segoe UI", 8)).grid(
                row=row, column=column, sticky="w", pady=(6, 0), padx=(0 if column == 0 else 10, 0))
            entry = ttk.Entry(self, textvariable=self.vars[key], width=8,
                              justify="right", font=("Consolas", 9))
            entry.grid(row=row, column=column + 1, sticky="w", padx=(6, 0), pady=(6, 0))
            entry.bind("<Return>", self._apply_typed)
            entry.bind("<FocusOut>", self._apply_typed)
            self._widgets.append(entry)

        self.snap_var = tk.BooleanVar(value=True)
        snap = ttk.Checkbutton(self, text="Snap to a Landscape size (no padding)",
                               variable=self.snap_var, command=self._resnap)
        snap.grid(row=3, column=0, columnspan=4, sticky="w", pady=(6, 0))
        Tooltip(snap, "Shrinks the region to a size Unreal builds exactly (a whole\n"
                      "number of Landscape components), so no edge is padded.")
        self._widgets.append(snap)

        self.readout_var = tk.StringVar()
        self.readout = ttk.Label(self, textvariable=self.readout_var, style="Readout.TLabel",
                                 justify="left", wraplength=wraplength)
        self.readout.grid(row=4, column=0, columnspan=4, sticky="w", pady=(6, 0))

        row_frame = ttk.Frame(self)
        row_frame.grid(row=5, column=0, columnspan=4, sticky="w", pady=(6, 0))
        whole = ttk.Button(row_frame, text="Whole map", command=self.select_whole_map)
        whole.pack(side="left")
        self._widgets.append(whole)
        # The host's own actions (Save crop, Download region...), enabled with the crop.
        self.buttons = {}
        for text, command, tip in buttons:
            button = ttk.Button(row_frame, text=text, command=command)
            button.pack(side="left", padx=(6, 0))
            if tip:
                Tooltip(button, tip)
            self.buttons[text] = button
            self._widgets.append(button)
        self.update_states()

    # ---------------------------------------------------------- host side
    @property
    def enabled(self):
        """True while the crop checkbox is ticked."""
        return bool(self.enabled_var.get())

    def active(self):
        """True when cropping is on and a region smaller than the map is selected."""
        return (self.enabled and self.bounds is not None and self.map_shape is not None
                and tuple(self.bounds) != crop_logic.whole_map(self.map_shape))

    def reset(self):
        """A new map is coming: forget the old one's shape and selection."""
        self.map_shape = None
        self.bounds = None
        for var in self.vars.values():
            var.set("")             # the old map's numbers mean nothing for the new one
        self.update_states()
        self.refresh_readout()

    def map_loaded(self, shape):
        """The view drew a map of `shape`: place the rectangle if cropping is on."""
        self.map_shape = tuple(shape)
        if self.enabled and self.view is not None:
            self.set_bounds(crop_logic.default_crop(self.map_shape))
            self.view.enable_crop(self._on_drawn, self.bounds)
        self.update_states()
        self.refresh_readout()

    def set_enabled(self, enabled):
        """Turns cropping on or off from the host, as the checkbox would."""
        self.enabled_var.set(bool(enabled))
        self._toggle()

    def set_available(self, available):
        """Greys the whole group out (and turns cropping off) for a map it cannot crop."""
        if not available and self.enabled:
            self.set_enabled(False)
        self._check.state(["!disabled"] if available else ["disabled"])
        self.update_states()

    def update_states(self, busy=False):
        """Entries and buttons are live only while cropping is on, a map is drawn and nothing runs."""
        live = self.enabled and self.map_shape is not None and not busy
        for widget in self._widgets:
            widget.state(["!disabled"] if live else ["disabled"])

    def refresh_readout(self):
        """What the selection (or the whole map) gives in Unreal."""
        if self.map_shape is None:
            self.readout_var.set("")
            return
        bounds = (self.bounds if self.enabled and self.bounds
                  else crop_logic.whole_map(self.map_shape))
        summary = crop_logic.describe_crop(bounds, self._get_resolution() or 1.0, self.map_shape)
        self.readout_var.set("\n".join(summary["lines"]))
        # Amber when Unreal cannot take it as is, so it is seen before acting.
        self.readout.configure(
            foreground="#b8860b" if not (summary["fits"] and summary["auto_import"]) else "")

    # ------------------------------------------------------------ events
    def _toggle(self):
        """The checkbox changed: show or hide the rectangle on the map."""
        if self.view is not None:
            if self.enabled and self.map_shape is not None:
                if self.bounds is None:
                    self.set_bounds(crop_logic.default_crop(self.map_shape))
                self.view.enable_crop(self._on_drawn, self.bounds)
            elif not self.enabled:
                self.view.disable_crop()
        self.update_states()
        self.refresh_readout()

    def _on_drawn(self, bounds):
        """The user drew or adjusted the rectangle on the map."""
        self.set_bounds(bounds)

    def _apply_typed(self, _event=None):
        """Uses the typed X, Y, width and height, once they are all numbers."""
        if not self.enabled or self.map_shape is None:
            return
        try:
            values = {key: float(var.get().replace(",", "")) for key, var in self.vars.items()}
        except ValueError:
            self.set_bounds(self.bounds)          # restore the last valid values
            return
        bounds = crop_logic.clamp_bounds(values["y"], values["x"], values["height"],
                                         values["width"], self.map_shape)
        if tuple(bounds) != tuple(self.bounds or ()):
            self.set_bounds(bounds)

    def _resnap(self):
        """Re-applies the bounds, so ticking 'Snap' snaps them at once."""
        if self.bounds is not None:
            self.set_bounds(self.bounds)

    def select_whole_map(self):
        """Selects the entire map (not snapped: the map is what it is)."""
        if self.map_shape is not None:
            self.set_bounds(crop_logic.whole_map(self.map_shape), snap=False)

    def set_bounds(self, bounds, snap=None):
        """
        Takes `bounds` as the selection: snapped if asked, then shown in the
        fields, on the map and in the read-out.
        """
        if bounds is None or self.map_shape is None:
            return
        if self.snap_var.get() if snap is None else snap:
            bounds = crop_logic.snap_bounds(bounds, self.map_shape)
        self.bounds = tuple(bounds)
        row, col, rows, cols = self.bounds
        for key, value in (("x", col), ("y", row), ("width", cols), ("height", rows)):
            self.vars[key].set(str(value))
        if self.view is not None:
            self.view.set_crop_bounds(self.bounds)
        self.refresh_readout()

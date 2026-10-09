"""
Shared Tk widgets and dialogs: tooltips, the progress panel with learned time
estimates, message dialogs, parameter rows, the activity log and view headers.
"""

import ctypes
import json
import os
import sys
import textwrap
import logging
import time

import tkinter as tk
from tkinter import ttk
import numpy as np

log = logging.getLogger(__name__)


# =========================================================================
# SMALL UI HELPERS
# =========================================================================

def enable_dpi_awareness():
    """
    Opts the process into per-monitor DPI awareness on Windows.

    Without awareness a scaled display (125%/150%) hands Tk a virtualized
    resolution and bitmap-upscales the window, blurring it. With only *system*
    awareness the window is still bitmap-stretched when dragged to a monitor with
    a different scale factor. Per-Monitor-v2 keeps every monitor crisp; the app
    then rescales its widgets on the DPI-change events Windows sends (see
    TerrainStudio._apply_dpi_change). Falls back to older modes on old Windows.
    """
    if sys.platform != "win32":
        return
    user32 = ctypes.windll.user32
    # Per-Monitor-Aware v2 (Windows 10 1703+). An older Windows lacks the call:
    # each failure falls through to the next, older mode.
    try:
        user32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception as exc:
        log.debug("Per-monitor-v2 DPI awareness unavailable: %s", exc)
    # Per-Monitor aware (Windows 8.1+); HRESULT S_OK == 0.
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return
    except Exception as exc:
        log.debug("Per-monitor DPI awareness unavailable: %s", exc)
    # System DPI aware, then the legacy call.
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception as exc:
        log.debug("System DPI awareness unavailable: %s", exc)
        try:
            user32.SetProcessDPIAware()
        except Exception as exc:
            log.warning("Could not enable DPI awareness; the window may look blurry "
                        "on a scaled display: %s", exc)


class Tooltip:
    """Lightweight hover tooltip for any Tk widget."""

    def __init__(self, widget, text, delay=450):
        self.widget = widget
        self.text = text
        self.delay = delay
        self._after_id = None
        self._window = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self._after_id = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._after_id:
            self.widget.after_cancel(self._after_id)
            self._after_id = None

    def _show(self):
        if self._window or not self.text:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self._window = tk.Toplevel(self.widget)
        self._window.wm_overrideredirect(True)
        self._window.wm_geometry(f"+{x}+{y}")
        tk.Label(
            self._window, text=self.text, justify="left",
            background="#232323", foreground="#e8e8e8", relief="solid",
            borderwidth=1, font=("Segoe UI", 8), padx=8, pady=5,
        ).pack()

    def _hide(self, _event=None):
        self._cancel()
        if self._window:
            self._window.destroy()
            self._window = None


# =========================================================================
# PROGRESS WITH TIME ESTIMATES
# =========================================================================

# Seconds per unit of work of each job kind, learned from finished runs and
# kept between sessions, so jobs that cannot report progress still get an
# estimate.
DURATIONS_PATH = os.path.join(os.path.expanduser("~"), ".lunar_terrain_studio", "durations.json")


_durations = None


def _job_rates():
    global _durations
    if _durations is None:
        try:
            with open(DURATIONS_PATH) as handle:
                _durations = {k: float(v) for k, v in json.load(handle).items()}
        except (OSError, ValueError, AttributeError):
            _durations = {}
    return _durations


def _record_job_rate(key, seconds_per_unit):
    rates = _job_rates()
    old = rates.get(key)
    rates[key] = seconds_per_unit if old is None else 0.5 * old + 0.5 * seconds_per_unit
    try:
        os.makedirs(os.path.dirname(DURATIONS_PATH), exist_ok=True)
        with open(DURATIONS_PATH, "w") as handle:
            json.dump(rates, handle, indent=1)
    except OSError:
        pass                                # The estimate is a convenience only


def format_duration(seconds):
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min {seconds % 60:02d} s"
    return f"{seconds // 3600} h {(seconds % 3600) // 60:02d} min"


class ProgressPanel(ttk.Frame):
    """
    Status line and progress bar for a view's background jobs, with the
    elapsed time and an estimate of the time left. Jobs that report progress
    are estimated from their rate; the others animate the bar and are estimated
    from how long the same kind of job took before, per unit of `work`.
    """

    TICK_MS = 500

    def __init__(self, parent):
        super().__init__(parent)
        self.text_var = tk.StringVar(value="Idle")
        ttk.Label(self, textvariable=self.text_var, style="Readout.TLabel").pack(anchor="w")
        self.bar = ttk.Progressbar(self, mode="determinate", maximum=1.0)
        self.bar.pack(fill="x", pady=(2, 0))
        self._job = None
        self._tick_job = None
        self._idle_job = None

    @property
    def running(self):
        return self._job is not None

    def start(self, label, key=None, work=1.0):
        """Begins a job; `work` scales the learned duration of `key` (e.g. megapixels)."""
        if self._idle_job:
            self.after_cancel(self._idle_job)
            self._idle_job = None
        self._job = {"message": label, "key": key or label, "work": max(float(work), 1e-6),
                     "start": time.monotonic(), "fraction": None, "eta": None}
        self.bar.configure(mode="indeterminate")
        self.bar.start(15)
        self._render()
        if self._tick_job is None:
            self._tick_job = self.after(self.TICK_MS, self._tick)

    def update(self, message=None, fraction=None):
        """Reports progress; starts a job implicitly if none is running."""
        if self._job is None:
            self.start(message or "Working...")
        if message:
            self._job["message"] = message
        if fraction is not None and fraction > 0:
            if self._job["fraction"] is None:
                self.bar.stop()
                self.bar.configure(mode="determinate")
            self._job["fraction"] = min(max(float(fraction), self._job["fraction"] or 0.0), 1.0)
            self.bar.configure(value=self._job["fraction"])
        self._render()

    def note(self, text):
        if self._job is not None:
            self._job["message"] = text
            self._render()

    def finish(self, outcome="done"):
        """Ends the job: 'done' (its duration is learned), 'failed' or 'cancelled'."""
        job, self._job = self._job, None
        if job is None:
            return
        elapsed = time.monotonic() - job["start"]
        if outcome == "done" and elapsed > 0.2:
            _record_job_rate(job["key"], elapsed / job["work"])
        self.bar.stop()
        self.bar.configure(mode="determinate", value=0)
        verb = {"done": "Done in", "failed": "Failed after", "cancelled": "Cancelled after"}[outcome]
        self.text_var.set(f"{job['message'].rstrip('.')}: {verb.lower()} {format_duration(elapsed)}")
        self._idle_job = self.after(6000, lambda: self.text_var.set("Idle"))

    def _tick(self):
        self._tick_job = None
        if self._job is not None and self.winfo_exists():
            self._render()
            self._tick_job = self.after(self.TICK_MS, self._tick)

    def _remaining(self, elapsed):
        job = self._job
        learned = _job_rates().get(job["key"])
        learned_left = learned * job["work"] - elapsed if learned is not None else None
        fraction = job["fraction"]
        if fraction and fraction >= 0.05 and elapsed >= 1.0:
            from_rate = elapsed * (1 - fraction) / fraction
            if learned_left is not None and learned_left > 0 and fraction < 0.5:
                weight = (fraction - 0.05) / 0.45          # Trust the rate more as it firms up
                return weight * from_rate + (1 - weight) * learned_left
            return from_rate
        if learned_left is not None:
            return learned_left
        return None

    def _render(self):
        job = self._job
        if job is None:
            return
        elapsed = time.monotonic() - job["start"]
        parts = [job["message"].rstrip(".")]
        if job["fraction"] is not None:
            parts.append(f"{job['fraction'] * 100:.0f}%")
        parts.append(f"{format_duration(elapsed)} elapsed")
        left = self._remaining(elapsed)
        if left is not None:
            # Smooth the estimate so it does not jump with every update.
            job["eta"] = left if job["eta"] is None else 0.7 * job["eta"] + 0.3 * left
            if job["eta"] > 0.5:
                parts.append(f"about {format_duration(job['eta'])} left")
            else:
                parts.append("finishing (longer than last time)" if left < -2 else "almost done")
        elif elapsed > 2:
            parts.append("no estimate yet (first run)")
        self.text_var.set("   ·   ".join(parts))


class MessageDialog(tk.Toplevel):
    """
    Modal dialog for long, pre-formatted messages.

    Replaces tkinter's messagebox, whose platform dialog re-wraps text at its own
    width and clips long paths. Text is shown verbatim in a monospace widget
    sized to the content, selectable, and scrollable when taller than the screen.
    """

    # Content bounds in character cells, so a short message stays a small dialog
    # and a long one stops growing before it runs off the screen.
    MIN_WIDTH, MAX_WIDTH = 46, 104
    MIN_HEIGHT, MAX_HEIGHT = 3, 26

    def __init__(self, parent, title, message, ok_label="OK", cancel_label=None):
        super().__init__(parent)
        self.result = False
        self.title(title)
        self.transient(parent)
        self.resizable(True, True)

        body = ttk.Frame(self, padding=(14, 12, 14, 8))
        body.pack(fill="both", expand=True)
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        font = ("Consolas", 9)
        lines = message.splitlines() or [""]
        width = min(max(max(len(line) for line in lines), self.MIN_WIDTH), self.MAX_WIDTH)

        self._body = body
        self._scrollbar = None
        text = tk.Text(body, wrap="word", width=width, height=self.MIN_HEIGHT, font=font,
                       relief="flat", padx=6, pady=4, cursor="arrow",
                       background=self.cget("background"),
                       highlightthickness=0, borderwidth=0)
        text.grid(row=0, column=0, sticky="nsew")
        text.insert("1.0", message)
        self._text = text

        # Size from an estimate now so the dialog opens at roughly the right
        # size, then let _fit_to_content correct it against Tk's own layout.
        self._apply_height(self._wrapped_rows(lines, width))

        # Read-only, but still selectable: 'disabled' would block copying, so the
        # edit keys are swallowed instead.
        text.bind("<Key>", self._swallow_key)

        buttons = ttk.Frame(self, padding=(14, 0, 14, 12))
        buttons.pack(fill="x")
        ok = ttk.Button(buttons, text=ok_label, command=self._accept)
        if cancel_label:
            ttk.Button(buttons, text=cancel_label,
                       command=self._dismiss).pack(side="right")
            ok.pack(side="right", padx=(0, 6))
        else:
            ok.pack(side="right")

        self.bind("<Return>", self._accept)
        self.bind("<Escape>", self._dismiss)
        self.protocol("WM_DELETE_WINDOW", self._dismiss)

        self._centre_on(parent)
        ok.focus_set()
        self.grab_set()
        # Runs once the window is on screen and the text widget knows its real
        # width, which is when Tk can be asked how the message actually wrapped.
        self.after(0, self._fit_to_content)
        self.wait_window(self)

    @staticmethod
    def _wrapped_rows(lines, width):
        """
        Estimated rows the message occupies when word-wrapped to `width`.

        Accounts for words that do not fit the remainder of a row, which
        dividing line length by width underestimates.
        """
        rows = 0
        for line in lines:
            wrapped = textwrap.wrap(
                line, width, break_long_words=True, break_on_hyphens=False,
                drop_whitespace=False, replace_whitespace=False)
            rows += max(len(wrapped), 1)
        return rows

    def _apply_height(self, rows):
        """Sizes the text to `rows`, adding or removing the scrollbar to match."""
        height = min(max(rows, self.MIN_HEIGHT), self.MAX_HEIGHT)
        self._text.configure(height=height)

        if rows > height and self._scrollbar is None:
            self._scrollbar = ttk.Scrollbar(self._body, orient="vertical",
                                            command=self._text.yview)
            self._scrollbar.grid(row=0, column=1, sticky="ns")
            self._text.configure(yscrollcommand=self._scrollbar.set)
        elif rows <= height and self._scrollbar is not None:
            self._text.configure(yscrollcommand="")
            self._scrollbar.destroy()
            self._scrollbar = None

    def _fit_to_content(self):
        """
        Re-sizes against Tk's actual layout once the window is mapped.

        The textwrap estimate can differ from Tk by a row on long unbroken
        tokens; a row short without a scrollbar would hide text.
        """
        if not self.winfo_exists():
            return
        self.update_idletasks()
        counted = self._text.count("1.0", "end-1c", "displaylines")
        if counted is None:  # a single display line: Tk counts the gaps, not the rows
            rows = 1
        else:
            gaps = counted[0] if isinstance(counted, (tuple, list)) else counted
            rows = int(gaps) + 1
        if rows != int(self._text.cget("height")) or self._scrollbar is not None:
            self._apply_height(rows)

    # Keys that must still reach the Text widget: copy, select-all, and moving
    # the insertion point. Everything else would edit the message.
    _ALLOWED_CTRL_KEYS = frozenset({"c", "C", "a", "A", "Insert"})
    _NAVIGATION_KEYS = frozenset({"Left", "Right", "Up", "Down",
                                  "Home", "End", "Prior", "Next"})

    @classmethod
    def _swallow_key(cls, event):
        """Blocks edits while leaving the copy and navigation bindings intact."""
        control_held = bool(event.state & 0x4)
        if control_held and event.keysym in cls._ALLOWED_CTRL_KEYS:
            return None
        if not control_held and event.keysym in cls._NAVIGATION_KEYS:
            return None
        return "break"

    def _centre_on(self, parent):
        self.update_idletasks()
        width, height = self.winfo_reqwidth(), self.winfo_reqheight()
        if parent is not None and parent.winfo_viewable():
            x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - height) // 3
        else:
            x = (self.winfo_screenwidth() - width) // 2
            y = (self.winfo_screenheight() - height) // 3
        # Keep the whole dialog on screen even when the parent sits near an edge.
        x = max(0, min(x, self.winfo_screenwidth() - width))
        y = max(0, min(y, self.winfo_screenheight() - height))
        self.geometry(f"+{x}+{y}")

    def _accept(self, _event=None):
        self.result = True
        self.destroy()

    def _dismiss(self, _event=None):
        self.result = False
        self.destroy()


def show_message(parent, title, message):
    """Information dialog: one dismiss button."""
    MessageDialog(parent, title, message, ok_label="Close")


def ask_confirmation(parent, title, message, ok_label="Continue"):
    """Confirmation dialog. Returns True only if the user accepted."""
    return MessageDialog(parent, title, message,
                         ok_label=ok_label, cancel_label="Cancel").result


def maximize_window(window):
    """
    Maximizes a window across platforms, kept movable rather than borderless.

    Windows takes state("zoomed"), many Linux WMs only the -zoomed attribute;
    anything else falls back to filling the screen.
    """
    for attempt in (lambda: window.state("zoomed"),
                    lambda: window.attributes("-zoomed", True)):
        try:
            attempt()
            return
        except tk.TclError:
            continue
    window.geometry(f"{window.winfo_screenwidth()}x{window.winfo_screenheight()}+0+0")


def format_path_block(path, width=84, indent="    "):
    """
    Lays a long path over several lines, broken at its separators.

    Word wrapping breaks a path at the spaces inside folder names, splitting it
    mid-component and dropping the indent. Breaking at the separators keeps each
    line a whole path component.
    """
    parts = path.replace("/", os.sep).split(os.sep)
    # Keep each separator on the component before it, so no line starts with one.
    pieces = [p + os.sep for p in parts[:-1]] + parts[-1:]

    lines, current = [], indent
    hanging = indent + "  "
    for piece in pieces:
        candidate = current + piece
        if len(candidate) > width and current.strip():
            lines.append(current)
            current = hanging + piece
        else:
            current = candidate
    lines.append(current)
    return "\n".join(lines)


class ParamRow:
    """
    A single parameter control: label, slider, and a numeric entry kept in sync.

    Logarithmic parameters store log10(value) on the slider while the entry
    always shows the real value, so densities spanning several decades stay
    controllable with a single drag.
    """

    def __init__(self, parent, spec, on_change):
        self.key, label, self.minimum, self.maximum, self.kind, tooltip = spec
        self.on_change = on_change
        self._syncing = False
        # Authoritative value. The slider position is a lossy view of it (a log
        # slider cannot round-trip 8e-4 exactly), so typed values are kept here
        # verbatim and only the widget reads from the transformed position.
        self.value = float(self.minimum)

        frame = ttk.Frame(parent)
        frame.pack(fill="x", pady=(2, 4))
        frame.columnconfigure(0, weight=1)

        name_label = ttk.Label(frame, text=label, font=("Segoe UI", 8))
        name_label.grid(row=0, column=0, sticky="w")
        Tooltip(name_label, tooltip)

        self.entry_var = tk.StringVar()
        self.entry = ttk.Entry(frame, textvariable=self.entry_var, width=11,
                               justify="right", font=("Consolas", 9))
        self.entry.grid(row=0, column=1, sticky="e")
        self.entry.bind("<Return>", self._commit_entry)
        self.entry.bind("<FocusOut>", self._commit_entry)

        self.slider_var = tk.DoubleVar()
        self.slider = ttk.Scale(
            frame, from_=self._to_slider(self.minimum), to=self._to_slider(self.maximum),
            variable=self.slider_var, command=self._slider_moved, orient="horizontal",
        )
        self.slider.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(1, 0))
        Tooltip(self.slider, tooltip)

    # --- value transforms -------------------------------------------------
    def _to_slider(self, value):
        if self.kind == 'log':
            return float(np.log10(max(value, 1e-12)))
        return float(value)

    def _from_slider(self, value):
        if self.kind == 'log':
            return float(10.0 ** value)
        if self.kind == 'int':
            return int(round(value))
        return float(value)

    def _format(self, value):
        if self.kind == 'int':
            return str(int(round(value)))
        if self.kind == 'log' and value < 0.01:
            return f"{value:.2e}"
        return f"{value:.4g}"

    # --- events -----------------------------------------------------------
    def _slider_moved(self, _raw):
        if self._syncing:
            return
        self._syncing = True
        self.value = self._from_slider(self.slider_var.get())
        self.entry_var.set(self._format(self.value))
        self._syncing = False
        self.on_change()

    def _commit_entry(self, _event=None):
        if self._syncing:
            return
        try:
            value = float(self.entry_var.get())
        except ValueError:
            self.set(self.get())  # Revert to the last valid value
            return
        self.set(value)
        self.on_change()

    # --- accessors --------------------------------------------------------
    def get(self):
        return int(round(self.value)) if self.kind == 'int' else self.value

    def set(self, value):
        # Values outside the slider range are clamped for the widget, but the
        # stored value and the entry keep showing what the config actually holds.
        self.value = float(value)
        clamped = min(max(self.value, self.minimum), self.maximum)
        self._syncing = True
        self.slider_var.set(self._to_slider(clamped))
        self.entry_var.set(self._format(self.value))
        self._syncing = False


class ActivityLog(ttk.Frame):
    """The dark log strip the studio uses, as a reusable widget."""

    def __init__(self, parent, height=7, title="Activity log"):
        super().__init__(parent, padding=(8, 4, 8, 8))
        ttk.Label(self, text=title, font=("Segoe UI Semibold", 10)).pack(anchor="w")
        self.text = tk.Text(self, height=height, font=("Consolas", 8), wrap="word",
                            relief="solid", borderwidth=1, background="#1e1e1e",
                            foreground="#d4d4d4", insertbackground="#d4d4d4")
        self.text.pack(fill="both", expand=True, pady=(4, 0))
        self.text.tag_configure("warn", foreground="#e6c07b")
        self.text.tag_configure("error", foreground="#e06c75")
        self.text.tag_configure("ok", foreground="#98c379")
        self.text.configure(state="disabled")

    def log(self, message, level=None):
        self.text.configure(state="normal")
        self.text.insert("end", message.rstrip() + "\n", level or ())
        self.text.see("end")
        self.text.configure(state="disabled")


class ViewHeader(ttk.Frame):
    """
    Header shared by the full-window views: Home button, title, subtitle.
    """

    def __init__(self, parent, title, on_home, subtitle=""):
        super().__init__(parent)
        ttk.Button(self, text="⌂ Home", width=9,
                   command=on_home).pack(side="left")
        ttk.Label(self, text=title,
                  font=("Segoe UI Semibold", 11)).pack(side="left", padx=(8, 0))
        self.subtitle_var = tk.StringVar(value=subtitle)
        ttk.Label(self, textvariable=self.subtitle_var,
                  style="HomeHint.TLabel").pack(side="left", padx=(10, 0))

    def set_subtitle(self, text):
        self.subtitle_var.set(text)

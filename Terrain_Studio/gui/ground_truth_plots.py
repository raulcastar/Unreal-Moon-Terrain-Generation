"""Ground Truth drawing: the relief with the crater mask, on a given figure. No Tk."""

import numpy as np
from matplotlib.colors import LightSource
from matplotlib.patches import Patch

from gui import plot_common

CRATER_RGBA = (1.0, 0.69, 0.0)
RIM_RGBA = (0.23, 0.63, 1.0)

# A centred message in place of the view (loading, failures).
draw_message = plot_common.draw_message


def draw_crater_mask(figure, dem, resolution, labels=None, ids=None, rim_shown=False,
                     crater_count=None):
    """
    Hillshade of `dem` (metres, `resolution` m/px) with the crater labels
    (crater_map.render_labels at one cell per DEM cell) over it, when given.
    """
    figure.clear()
    axes = figure.add_subplot(111)
    finite = np.where(np.isfinite(dem), dem, np.nanmin(dem))
    relief = LightSource(azdeg=315, altdeg=45).hillshade(
        finite, vert_exag=(0.1 * max(dem.shape)) / max(float(np.ptp(finite)), 1e-6))
    extent = (-0.5 * resolution, (dem.shape[1] - 0.5) * resolution,
              (dem.shape[0] - 0.5) * resolution, -0.5 * resolution)
    axes.imshow(relief, cmap="gray", extent=extent, vmin=0, vmax=1)
    handles = []
    title = "Crater mask"
    if labels is not None and ids is not None:
        overlay = np.zeros(labels.shape + (4,))
        overlay[labels == ids["crater"]] = CRATER_RGBA + (0.45,)
        overlay[(labels == ids["rim"]) & (ids["rim"] > 0)] = RIM_RGBA + (0.45,)
        axes.imshow(overlay, extent=extent, interpolation="nearest")
        handles += [Patch(facecolor=CRATER_RGBA + (0.6,), label=f"Crater ({ids['crater']})")]
        if ids["rim"] and rim_shown:
            handles.append(Patch(facecolor=RIM_RGBA + (0.6,), label=f"Rim ({ids['rim']})"))
        title += f"  -  {crater_count} craters"
    if handles:
        axes.legend(handles=handles, loc="upper right", fontsize=7, framealpha=0.9)
    axes.set_title(title, fontsize=9)
    axes.set_xlabel("x (m)")
    axes.set_ylabel("y (m)")

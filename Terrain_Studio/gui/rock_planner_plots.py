"""Rock Field & Rover Paths drawing, on a given Matplotlib figure. No Tk."""

import matplotlib
import numpy as np
from matplotlib.colors import LightSource
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

from gui import plot_common
from gui.rock_planner_logic import route_status

# A centred message in place of the view (loading, failures).
draw_message = plot_common.draw_message


def draw_plan(figure, result):
    """The hillshade with the crater, corridor, site and rock overlays."""
    figure.clear()
    axes = figure.add_subplot(111)

    plan = result['plan_elevation']
    res_row, res_col = result['plan_res']
    relief = max(float(np.ptp(plan)), 1e-6)
    light = LightSource(azdeg=315, altdeg=45)
    vert_exag = (0.1 * max(plan.shape)) / relief
    shaded = light.shade(plan, cmap=matplotlib.colormaps["gray"],
                         blend_mode="soft", vert_exag=vert_exag)
    axes.imshow(shaded, origin="upper", interpolation="bilinear")

    # Craters: impassable and off-limits for rocks. Fill value 0.0 so the
    # winter colormap paints them blue (its 1.0 end is green, which would
    # clash with the corridors).
    crater = result['crater_mask']
    axes.imshow(np.where(crater, 0.0, np.nan), origin="upper",
                cmap="winter", alpha=0.45, vmin=0, vmax=1)

    # Guaranteed corridors.
    for rr, cc in result['paths']:
        axes.plot(cc, rr, color="#39d353", linewidth=1.4, alpha=0.9)

    # Markers: crater centres, i.e. the Voronoi sites.
    marker_label = result.get('marker_label', 'Waypoint')
    if result.get('markers'):
        mr = [r for r, _ in result['markers']]
        mc = [c for _, c in result['markers']]
        axes.plot(mc, mr, "o", color="#58a6ff", markersize=5,
                  markeredgecolor="white", linewidth=0, label=marker_label)

    # Rocks, sized by footprint radius (points^2 area at the display scale).
    rocks = result['rocks_px']
    if len(rocks):
        axes.scatter(rocks[:, 1], rocks[:, 0], s=np.maximum(2, (rocks[:, 2] * 1.6) ** 2),
                     facecolor="#d9a066", edgecolor="#3a2a17", linewidths=0.2,
                     alpha=0.8, label="Rocks")

    rows, cols = result['plan_shape']
    axes.set_title(
        f"Rock field & Voronoi rover corridors  -  {result['rock_count']} rocks, "
        f"{result['drivable_frac'] * 100:.0f}% drivable, {route_status(result)}\n"
        f"craters cover {result['crater_frac'] * 100:.0f}%   |   "
        f"planning grid {cols} x {rows} px  ({res_col:.2f} m/px)", fontsize=9)
    axes.set_xlabel("planning pixels")
    axes.set_ylabel("planning pixels")

    legend_handles = [
        Line2D([0], [0], color="#39d353", lw=2, label="Rover corridor"),
        Line2D([0], [0], marker="o", color="#58a6ff", lw=0,
               markeredgecolor="white", label=marker_label),
        Patch(facecolor="#d9a066", edgecolor="#3a2a17", label="Rock"),
        Patch(facecolor="blue", alpha=0.45, label="Crater"),
    ]
    axes.legend(handles=legend_handles, loc="upper right", fontsize=7,
                framealpha=0.9)

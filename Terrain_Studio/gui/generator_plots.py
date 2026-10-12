"""
Generate view drawing: the shaded relief, the 3D surface and the slope
analysis, drawn on a given Matplotlib figure. No Tk.
"""

import matplotlib
import numpy as np
from matplotlib.cm import ScalarMappable
from matplotlib.colors import LightSource, Normalize
from matplotlib.ticker import MaxNLocator

from gui.generator_logic import slope_degrees


def draw_relief(figure, elevation, extent, resolution, cmap, azimuth, altitude, exaggeration, title):
    """Sun-lit shaded relief with an elevation colour bar."""
    figure.clear()
    axes = figure.add_subplot(111)

    light = LightSource(azdeg=azimuth, altdeg=altitude)
    shaded = light.shade(
        elevation, cmap=matplotlib.colormaps[cmap],
        blend_mode="soft", vert_exag=exaggeration,
        dx=resolution, dy=resolution)

    axes.imshow(shaded, extent=extent, origin="lower", interpolation="bilinear")
    axes.set_title(f"Sun-lit shaded relief - {title}", fontsize=10)
    axes.set_xlabel("metres")
    axes.set_ylabel("metres")
    axes.set_aspect("equal")

    # The shaded image carries lighting rather than scalar values, so the
    # colourbar is driven by a standalone mappable over the elevation range.
    mappable = ScalarMappable(
        norm=Normalize(vmin=float(np.min(elevation)), vmax=float(np.max(elevation))),
        cmap=cmap)
    figure.colorbar(mappable, ax=axes, shrink=0.8, label="Elevation (m)")


def surface_exaggeration(sub, extent, z_box=0.35):
    """
    The vertical exaggeration that fits the relief of `sub` into a box z_box
    high relative to the map's longest side: terrain relief is metres over
    kilometres, so a true-scale box would look flat.
    """
    span = max(extent[1] - extent[0], extent[3] - extent[2])
    relief = max(float(np.ptp(sub)), 1e-6)
    return (z_box * span) / relief


def draw_surface(figure, elevation, extent, resolution, cmap, azimuth, altitude, title,
                 mesh_detail, drag_detail):
    """
    3D surface, its mesh capped at `mesh_detail` points per side. Returns
    (fine, coarse) surface artists when a coarser copy (`drag_detail`) was drawn
    for smooth rotation, else None.
    """
    figure.clear()
    axes = figure.add_subplot(111, projection="3d")

    rows, cols = elevation.shape
    # Cap the mesh density so large previews stay interactive
    step = max(1, int(np.ceil(max(rows, cols) / mesh_detail)))
    sub = elevation[::step, ::step]
    sub_res = resolution * step

    span_x = extent[1] - extent[0]
    span_y = extent[3] - extent[2]
    span = max(span_x, span_y)
    relief = max(float(np.ptp(sub)), 1e-6)

    # Fit the Z box to a readable height and report the implied exaggeration
    # in the title instead of hiding it.
    z_box = 0.35
    implied_exaggeration = surface_exaggeration(sub, extent, z_box)

    light = LightSource(azdeg=azimuth, altdeg=altitude)

    def plot_mesh(mesh, mesh_res):
        """Draws one level of detail and returns its collection."""
        mesh_x = np.linspace(extent[0], extent[1], mesh.shape[1])
        mesh_y = np.linspace(extent[2], extent[3], mesh.shape[0])
        mesh_gx, mesh_gy = np.meshgrid(mesh_x, mesh_y)
        colors = light.shade(
            mesh, cmap=matplotlib.colormaps[cmap],
            blend_mode="soft", vert_exag=implied_exaggeration,
            dx=mesh_res, dy=mesh_res)
        # rcount/ccount match the mesh so Matplotlib does not downsample the
        # grid out of alignment with the pre-computed face colours.
        return axes.plot_surface(
            mesh_gx, mesh_gy, mesh, facecolors=colors, shade=False,
            linewidth=0, antialiased=False,
            rcount=mesh.shape[0], ccount=mesh.shape[1])

    fine_surface = plot_mesh(sub, sub_res)

    # A coarse copy, drawn once and swapped in during a drag to keep rotation
    # responsive.
    artists = None
    drag_step = max(1, int(np.ceil(max(rows, cols) / drag_detail)))
    if drag_step > step:
        coarse_surface = plot_mesh(elevation[::drag_step, ::drag_step], resolution * drag_step)
        coarse_surface.set_visible(False)
        artists = (fine_surface, coarse_surface)

    axes.set_title(
        f"3D surface - {title}\n"
        f"vertical exaggeration x{implied_exaggeration:,.0f} (auto-fit)",
        fontsize=10)
    axes.set_xlabel("metres")
    axes.set_ylabel("metres")
    axes.set_zlabel("elevation (m)")
    axes.view_init(elev=32, azim=-125)
    axes.set_zlim(float(np.min(sub)) - relief * 0.15,
                  float(np.max(sub)) + relief * 0.15)
    axes.zaxis.set_major_locator(MaxNLocator(5))
    axes.set_box_aspect((span_x / span, span_y / span, z_box))
    figure.subplots_adjust(left=0.0, right=0.97, top=0.93, bottom=0.02)
    return artists


def draw_slope(figure, elevation, extent, resolution, title):
    """Slope magnitude map over the elevation histogram."""
    figure.clear()
    slope = slope_degrees(elevation, resolution)

    # The map carries the detail, so it gets two thirds of the frame.
    grid = figure.add_gridspec(3, 1)
    axes = figure.add_subplot(grid[0:2, 0])
    image = axes.imshow(slope, extent=extent, origin="lower",
                        cmap="inferno", interpolation="bilinear",
                        vmax=max(1.0, np.percentile(slope, 99.5)))
    axes.set_title(f"Slope magnitude - {title}", fontsize=10)
    axes.set_xlabel("metres")
    axes.set_ylabel("metres")
    axes.set_aspect("equal")
    figure.colorbar(image, ax=axes, shrink=0.85, label="Slope (deg)")

    histogram_axes = figure.add_subplot(grid[2, 0])
    histogram_axes.hist(elevation.ravel(), bins=120, color="#4a7ebb")
    histogram_axes.set_title("Elevation distribution", fontsize=10)
    histogram_axes.set_xlabel("Elevation (m)")
    histogram_axes.set_ylabel("Pixels")

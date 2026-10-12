"""Sun & Shadows drawing: the map with its sky dome, and the lit 3D surface. No Tk."""

import math

import numpy as np
from matplotlib.ticker import MaxNLocator

from gui import plot_common
from gui.backend import sun_shadows
from gui.sun_logic import SUN_3D_CELLS

# A centred message in place of the view (loading, failures).
draw_message = plot_common.draw_message


def build_map(figure, shape, resolution, unreal_view):
    """
    The map, sky dome and read-out axes for a preview of `shape` cells at
    `resolution` m/px. Returns the artists the page updates: axes, image, sky,
    info, sun_line, sun_dot, text.
    """
    rows, cols = shape
    figure.clear()
    grid = figure.add_gridspec(2, 2, width_ratios=[3.3, 1], height_ratios=[1, 1.1])
    axes = figure.add_subplot(grid[:, 0])
    sky = figure.add_subplot(grid[0, 1], projection="polar")
    info = figure.add_subplot(grid[1, 1])
    info.axis("off")

    extent = (-0.5 * resolution, (cols - 0.5) * resolution, (rows - 0.5) * resolution, -0.5 * resolution)
    image = axes.imshow(np.zeros((rows, cols, 3)), extent=extent, origin="upper",
                        interpolation="antialiased")
    if unreal_view:
        axes.invert_xaxis()        # Columns run towards -X
        axes.set_xlabel("x (m) along the DEM columns   |   Unreal +X to the right")
        axes.set_ylabel("y (m) along the DEM rows   |   Unreal +Y down")
    else:
        axes.set_xlabel("x (m) along the DEM columns   |   Unreal -X to the right")
        axes.set_ylabel("y (m) along the DEM rows   |   Unreal +Y down")
    axes.set_facecolor("black")

    # The dome is mirrored with the map, so each azimuth sits on the same side.
    sky.set_theta_zero_location("N")
    sky.set_theta_direction(1 if unreal_view else -1)
    sky.set_rlim(0, 90)
    sky.set_rticks([30, 60])
    sky.set_yticklabels(["60°", "30°"], fontsize=7, color="#777777")
    sky.set_xticks(np.radians([0, 90, 180, 270]))
    sky.set_xticklabels(["-Y", "-X", "+Y", "+X"], fontsize=8)
    sky.set_facecolor("#0b1020")
    sky.grid(color="#3a4660", linewidth=0.6)
    sky.set_title("Sky - click or drag the sun", fontsize=8)
    sun_line, = sky.plot([], [], color="#ffd34d", linewidth=1, linestyle=":")
    sun_dot, = sky.plot([], [], "o", color="#ffd34d", markersize=11,
                        markeredgecolor="white")
    text = info.text(0.0, 1.0, "", va="top", ha="left", family="monospace",
                     fontsize=8, transform=info.transAxes)
    return {"axes": axes, "image": image, "sky": sky, "info": info,
            "sun_line": sun_line, "sun_dot": sun_dot, "text": text}


def place_sun(artists, azimuth, elevation):
    """Moves the sun on the sky dome."""
    theta, radius = math.radians(azimuth), 90.0 - elevation
    artists["sun_dot"].set_data([theta], [radius])
    artists["sun_line"].set_data([theta, theta], [0.0, radius])


def draw_probe(axes, x0, y0, tip, height):
    """The reference object and its shadow; returns the artists drawn."""
    length = math.hypot(tip[0] - x0, tip[1] - y0)
    line, = axes.plot([x0, tip[0]], [y0, tip[1]], color="#ff9f1c", linewidth=2.2,
                      solid_capstyle="round")
    base, = axes.plot([x0], [y0], "^", color="white", markeredgecolor="black",
                      markersize=8)
    end, = axes.plot([tip[0]], [tip[1]], "o", color="#ff9f1c", markersize=4)
    label = axes.annotate(f"{height:g} m object: {length:.2f} m shadow",
                          (tip[0], tip[1]), xytext=(6, 6), textcoords="offset points",
                          fontsize=8, color="white",
                          bbox=dict(boxstyle="round,pad=0.2", fc="#000000aa", ec="none"))
    return [line, base, end, label]


def draw_3d(figure, dem, resolution, rgb, azimuth, elevation):
    """Lit 3D surface (colours `rgb`, the map's display image) with the incoming sunlight."""
    stride = max(1, int(math.ceil(max(dem.shape) / SUN_3D_CELLS)))
    z = dem[::stride, ::stride]
    z = np.where(np.isfinite(z), z, np.nanmin(z))
    colours = rgb[::stride, ::stride]
    rows, cols = z.shape
    step = resolution * stride
    xs, ys = np.meshgrid(np.arange(cols) * step, np.arange(rows) * step)

    figure.clear()
    # Draw in call order so the sun ray stays above the surface.
    axes = figure.add_subplot(111, projection="3d", computed_zorder=False)
    axes.plot_surface(xs, ys, z, facecolors=colours, rstride=1, cstride=1,
                      shade=False, linewidth=0, antialiased=False)
    width, depth = cols * step, rows * step
    relief = max(float(np.ptp(z)), 1e-6)
    # Flat maps are exaggerated to a relief of at least 8% of their width.
    exaggeration = max(1.0, 0.08 * max(width, depth) / relief)

    # Sun ray to the map centre, at its true angle before exaggeration.
    toward_sun = sun_shadows.sun_vector(azimuth, elevation)
    centre = np.array([width / 2, depth / 2, float(np.nanmax(z))])
    start = centre + toward_sun * 0.45 * max(width, depth)
    axes.plot(*zip(start, centre), color="#ffd34d", linewidth=2.5)
    axes.scatter(*start, color="#ffd34d", s=90, edgecolor="white", depthshade=False)

    z_low, z_high = float(np.nanmin(z)), max(float(np.nanmax(z)), start[2])
    axes.set_xlim(0, width)
    axes.set_ylim(depth, 0)        # Rows grow towards the viewer, as on the map
    axes.set_zlim(z_low, z_high)
    axes.set_box_aspect((width, depth, (z_high - z_low) * exaggeration))
    axes.zaxis.set_major_locator(MaxNLocator(4))
    axes.set_title(f"Sun from {azimuth:.1f} deg at {elevation:.1f} deg"
                   + (f"  -  heights x{exaggeration:.1f}" if exaggeration > 1.05 else ""),
                   fontsize=9)
    axes.set_xlabel("x (m)")
    axes.set_ylabel("y (m)")
    axes.set_zlabel("z (m)")
    axes.view_init(elev=35, azim=-60)

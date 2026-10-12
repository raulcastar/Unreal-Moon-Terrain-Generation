"""
Sun & Shadows logic: the sun's position and how it reads, the preview map and
its colours, the shadow statistics. No Tk, no Matplotlib figures.
"""

import math

import numpy as np
from matplotlib.colors import LightSource

SUN_PREVIEW_SIZES = ("500", "1000", "1500", "2000")

SUN_DISPLAY_MODES = ("Lunar render", "Lunar render, tinted shadows", "Shadows over relief")

# The Landscape mirrors the DEM (columns run towards -X): Unreal's Top view
# (+X right, +Y down) is the DEM image flipped left to right.
SUN_ORIENTATIONS = ("Unreal Top view (+X right, +Y down)", "DEM image (row 0 at the top)")

SUN_SHADOW_RGBA = (0.16, 0.35, 0.85, 0.55)     # Cast-shadow tint

SUN_3D_CELLS = 140                             # Longest side of the 3D mesh

# ACES filmic curve followed by display gamma, tabulated for lunar_tone.
SUN_TONE_X = np.linspace(0.0, 16.0, 4097)
SUN_TONE_Y = np.clip(SUN_TONE_X * (2.51 * SUN_TONE_X + 0.03)
                     / (SUN_TONE_X * (2.43 * SUN_TONE_X + 0.59) + 0.14), 0.0, 1.0) ** (1 / 2.2)
SUN_TONE_GROUND = 0.67                         # Display value of the median lit ground

MIN_ELEVATION_DEG = 0.5


def lunar_tone(light):
    """
    Maps direct light (0..1) to display values the way Unreal renders it:
    filmic curve, exposure normalised to the median lit ground (as auto-exposure
    does). Grazing light falls to black, as in the engine.
    """
    light = np.nan_to_num(light)
    lit = light[light > 0]
    median = float(np.median(lit)) if lit.size else 1.0
    gain = np.interp(SUN_TONE_GROUND, SUN_TONE_Y, SUN_TONE_X) / max(median, 1e-6)
    return np.interp(light * gain, SUN_TONE_X, SUN_TONE_Y)


# ------------------------------------------------------------------ the sun

def clamp_sun(azimuth, elevation):
    """(azimuth in [0, 360), elevation in [0.5, 90]) degrees."""
    return azimuth % 360.0, min(max(elevation, MIN_ELEVATION_DEG), 90.0)


def parse_positive(text, fallback, minimum=1e-3):
    """A positive number from a field (at least `minimum`), `fallback` if not a number."""
    try:
        return max(minimum, float(text))
    except ValueError:
        return fallback


def shadow_direction(azimuth):
    """Compass direction shadows point to (deg): away from the sun."""
    return (azimuth + 180.0) % 360.0


def unreal_light_rotation(azimuth, elevation):
    """(pitch, yaw) of Unreal's Directional Light for a sun at azimuth/elevation."""
    return -elevation, math.degrees(math.atan2(math.cos(math.radians(azimuth)),
                                               math.sin(math.radians(azimuth))))


def sun_info_text(azimuth, elevation, height, length):
    pitch, yaw = unreal_light_rotation(azimuth, elevation)
    return (f"Light from {azimuth:.1f} deg, {elevation:.1f} deg up: a {height:g} m object "
            f"casts a {length:.2f} m shadow towards {shadow_direction(azimuth):.0f} deg.\n"
            f"Unreal light rotation: pitch {pitch:.1f}, yaw {yaw:.1f}.")


def sun_from_shadow_fields(direction_text, length_text, height):
    """
    (azimuth, elevation) that casts a shadow `length` long in `direction` from
    an object `height` tall; or an error message (str) for invalid fields.
    """
    try:
        direction, length = float(direction_text), float(length_text)
    except ValueError:
        return "Shadow direction and length must be numbers."
    if length <= 0:
        return "The shadow length must be positive."
    return direction + 180.0, math.degrees(math.atan2(height, length))


def probe_tip(x0, y0, azimuth, length):
    """Map position (m) of the tip of a shadow `length` long cast from (x0, y0)."""
    a = math.radians(azimuth)
    return x0 - math.sin(a) * length, y0 + math.cos(a) * length


def parse_sun_settings(intensity_text, disc_text):
    """(intensity in lux, sun disc in deg), or an error message (str)."""
    try:
        intensity, disc = float(intensity_text), float(disc_text)
    except ValueError:
        return "Intensity and sun disc must be numbers."
    if intensity <= 0 or disc < 0:
        return "Intensity must be positive and the sun disc not negative."
    return intensity, disc


def unreal_sun_log(found):
    """The log line after reading the sun from Unreal (send_sun.read_sun_from_unreal)."""
    where = "running game" if found.get("pie") else "level"
    return (f"Read '{found['label']}' from the {where}: azimuth "
            f"{found['azimuth']:.1f}, elevation {found['elevation']:.1f} deg, "
            f"{found['intensity']:.0f} lux.")


# -------------------------------------------------------------------- the map

def reduce_dem(dem, size, resolution):
    """(DEM reduced so its longest side is at most `size`, metres per cell)."""
    stride = max(1, int(math.ceil(max(dem.shape) / size)))
    return dem[::stride, ::stride], resolution * stride


def relief_shading(dem):
    """Grey hillshade (0.25..0.85) of a DEM, missing cells filled with its minimum."""
    finite = np.where(np.isfinite(dem), dem, np.nanmin(dem))
    relief = LightSource(azdeg=315, altdeg=45).hillshade(
        finite, vert_exag=(0.1 * max(dem.shape)) / max(float(np.ptp(finite)), 1e-6))
    return 0.25 + 0.6 * relief


def display_rgb(light, shadow, relief, mode):
    """RGB image of the map for one of SUN_DISPLAY_MODES."""
    base = relief if mode == SUN_DISPLAY_MODES[2] else lunar_tone(light)
    rgb = np.repeat(base[..., None], 3, axis=2)
    if mode != SUN_DISPLAY_MODES[0]:
        tint, alpha = np.array(SUN_SHADOW_RGBA[:3]), SUN_SHADOW_RGBA[3]
        rgb[shadow] = rgb[shadow] * (1 - alpha) + tint * alpha
    return np.clip(rgb, 0.0, 1.0)


def shadow_shares(dem, shadow, light):
    """(% of the map in cast shadow, % facing away from the sun) over the valid cells."""
    valid = np.isfinite(dem)
    cells = max(int(valid.sum()), 1)
    cast = 100.0 * (shadow & valid).sum() / cells
    dark = 100.0 * ((light <= 0) & ~shadow & valid).sum() / cells
    return cast, dark


def map_title(azimuth, elevation, cast):
    return (f"Sun from {azimuth:.1f} deg at {elevation:.1f} deg  -  "
            f"{cast:.1f}% of the map in cast shadow")


def readout_text(azimuth, elevation, cast, dark, shape, resolution):
    """The text panel beside the map."""
    return (f"Sun\n  azimuth   {azimuth:6.1f} deg\n  elevation {elevation:6.1f} deg\n\n"
            f"Sun lies towards Unreal\n  X {-math.sin(math.radians(azimuth)):+5.2f}  "
            f"Y {-math.cos(math.radians(azimuth)):+5.2f}\n"
            f"Shadows point {(azimuth + 180) % 360:5.1f} deg\n"
            f"  length  {1 / math.tan(math.radians(elevation)):6.2f} x height\n\n"
            f"Map\n  cast shadow  {cast:5.1f} %\n  facing away  {dark:5.1f} %\n\n"
            f"Preview {shape[1]} x {shape[0]} px\n  at {resolution:.3g} m/px")

"""
Sun Dispatch
------------
Sends the sun position chosen in the studio to a running Unreal Editor over
Remote Execution, where unreal_sun points a lunar Directional Light at it, and
reads the level's current sun back.
"""

import json

from unreal_connection import run_in_unreal, unreal_call

SUN_JSON_PREFIX = "SUN_JSON "   # must match unreal_sun.SUN_JSON_PREFIX


def _sun_call(call):
    """Unreal-side Python that runs `call` on unreal_sun."""
    return unreal_call(call, "unreal_level_saver", "unreal_sun")


def build_command(azimuth_deg, elevation_deg, intensity_lux, source_angle_deg, save_level):
    args = ", ".join([f"{float(azimuth_deg)!r}", f"{float(elevation_deg)!r}",
                      f"intensity_lux={float(intensity_lux)!r}",
                      f"source_angle_deg={float(source_angle_deg)!r}",
                      f"save_level={bool(save_level)}"])
    return _sun_call(f"set_sun({args})")


def send_sun_to_unreal(azimuth_deg, elevation_deg, intensity_lux=120000.0,
                       source_angle_deg=0.53, save_level=True, log=print,
                       discovery_seconds=2.0):
    """Sets the lunar sun in a running Unreal Editor. Returns True on success."""
    command = build_command(azimuth_deg, elevation_deg, intensity_lux,
                            source_angle_deg, save_level)
    return run_in_unreal(command, log, "Setting the sun in Unreal", discovery_seconds)


def read_sun_from_unreal(log=print, discovery_seconds=2.0):
    """
    Returns the level's sun as a dict (label, azimuth, elevation, intensity,
    source_angle, pie), or None if it could not be read.
    """
    found = {}

    def relay(text, level=None):
        if text.startswith(SUN_JSON_PREFIX):
            found.update(json.loads(text[len(SUN_JSON_PREFIX):]))
        else:
            log(text, level)

    if not run_in_unreal(_sun_call("get_sun()"), relay, "Reading the sun from Unreal",
                         discovery_seconds):
        return None
    if "error" in found:
        log(f"[-] {found['error']}", "error")
        return None
    return found or None

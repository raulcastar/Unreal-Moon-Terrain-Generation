"""
Crater Mask Dispatch
--------------------
Sends a crater map (<name>_craterMask.png) to a running Unreal Editor over
Remote Execution, where unreal_ground_truth makes the rover's object mask show
craters, rims and shadows on the Landscape, and sets up the mask camera.
"""

import json

from unreal_connection import run_in_unreal, unreal_call

CAMERAS_JSON_PREFIX = "CAMERAS_JSON "   # Must match unreal_ground_truth.CAMERAS_JSON_PREFIX


def _crater_call(call):
    """Unreal-side Python that runs `call` on unreal_ground_truth."""
    return unreal_call(call, "unreal_level_saver", "unreal_rocks", "unreal_ground_truth")


def send_crater_mask_to_unreal(png_path, width, height, resolution, landscape_label="",
                               landscape_width=0, supersample=1, camera_actor="",
                               camera_capture="", save_level=False, shadows=True,
                               rover_shadow=True, sun=True, sun_glare_deg=1.1,
                               wheel_tracks=True, track_style=None, log=print,
                               discovery_seconds=2.0):
    """
    Imports the crater map, updates the object-mask material (craters and, with
    `shadows`, the shadow mask in the same frame; with `rover_shadow`, the
    rover's own shadow flagged too) and sets up the mask camera.
    Returns True on success.
    """
    args = ", ".join([repr(png_path.replace("\\", "/")), str(int(width)), str(int(height)),
                      f"resolution={float(resolution)!r}",
                      f"landscape_label={landscape_label!r}",
                      f"landscape_width={int(landscape_width)}",
                      f"supersample={int(supersample)}",
                      f"camera_actor={camera_actor!r}",
                      f"camera_capture={camera_capture!r}",
                      f"save_level={bool(save_level)}",
                      f"shadows={bool(shadows)}",
                      f"rover_shadow={bool(rover_shadow)}",
                      f"sun={bool(sun)}",
                      f"sun_glare_deg={float(sun_glare_deg)!r}",
                      f"wheel_tracks={bool(wheel_tracks)}",
                      f"track_style={dict(track_style or {})!r}"])
    return run_in_unreal(_crater_call(f"apply_crater_mask({args})"), log,
                         "Adding crater ground truth in Unreal", discovery_seconds)


def send_track_style(track_style, log=print, discovery_seconds=2.0):
    """
    Rebuilds only the wheel-track decal material with a new tread look (pattern,
    lug spacing, relief, darkness). Returns True on success.
    """
    code = unreal_call(f"apply_track_style({dict(track_style or {})!r})", "unreal_track_setup")
    return run_in_unreal(code, log, "Updating the wheel-track look in Unreal", discovery_seconds)


def list_mask_cameras(log=print, discovery_seconds=2.0):
    """
    Scene-capture cameras in the level, as {"pie", "actors": [{"actor",
    "captures", "with_mask", "streamer_mask"}]}, or None if Unreal did not answer.
    """
    found = {}

    def relay(text, level=None):
        if text.startswith(CAMERAS_JSON_PREFIX):
            found.update(json.loads(text[len(CAMERAS_JSON_PREFIX):]))
        else:
            log(text, level)

    if not run_in_unreal(_crater_call("list_mask_cameras()"), relay,
                         "Listing cameras in Unreal", discovery_seconds):
        return None
    return found or None


def remove_crater_mask_from_unreal(log=print, discovery_seconds=2.0):
    """Restores the original object-mask material. Returns True on success."""
    return run_in_unreal(_crater_call("remove_crater_mask()"), log,
                         "Restoring the object-mask material", discovery_seconds)

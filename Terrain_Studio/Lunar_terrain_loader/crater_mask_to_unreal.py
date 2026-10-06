"""
Crater Mask Dispatch
--------------------
Sends a crater map (<name>_craterMask.png) to a running Unreal Editor over
Remote Execution, where unreal_crater_mask makes the rover's object mask show
craters, rims and shadows on the Landscape, and sets up the mask camera.
"""

import json

from rocks_to_unreal import BASE_DIRECTORY, run_in_unreal

CAMERAS_JSON_PREFIX = "CAMERAS_JSON "   # Must match unreal_crater_mask.CAMERAS_JSON_PREFIX


def _crater_call(call):
    """Builds Unreal-side Python that imports unreal_crater_mask and prints the result of `call`."""
    return (
        f"import sys, importlib\n"
        f"if {BASE_DIRECTORY!r} not in sys.path: sys.path.append({BASE_DIRECTORY!r})\n"
        f"import unreal_level_saver\n"
        f"importlib.reload(unreal_level_saver)\n"
        f"import unreal_rock_placer\n"
        f"importlib.reload(unreal_rock_placer)\n"
        f"import unreal_crater_mask\n"
        f"importlib.reload(unreal_crater_mask)\n"
        f"print(unreal_crater_mask.{call})\n"
    )


def send_crater_mask_to_unreal(png_path, width, height, resolution, landscape_label="",
                               landscape_width=0, supersample=1, camera_actor="",
                               camera_capture="", save_level=False, shadows=True,
                               rover_shadow=True, sun=True, sun_glare_deg=1.1,
                               wheel_tracks=True, log=print, discovery_seconds=2.0):
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
                      f"wheel_tracks={bool(wheel_tracks)}"])
    return run_in_unreal(_crater_call(f"apply_crater_mask({args})"), log,
                         "Adding crater ground truth in Unreal", discovery_seconds)


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

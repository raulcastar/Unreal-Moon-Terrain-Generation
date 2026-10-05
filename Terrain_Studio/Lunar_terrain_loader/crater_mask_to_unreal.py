"""
Crater Mask Dispatch
--------------------
Sends a crater map (<name>_craterMask.png) to a running Unreal Editor over
Remote Execution, where unreal_crater_mask makes the rover's object mask show
craters and rims on the Landscape.
"""

from rocks_to_unreal import BASE_DIRECTORY, run_in_unreal


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
                               landscape_width=0, supersample=1, log=print,
                               discovery_seconds=2.0):
    """Imports the crater map and updates the object-mask material. Returns True on success."""
    args = ", ".join([repr(png_path.replace("\\", "/")), str(int(width)), str(int(height)),
                      f"resolution={float(resolution)!r}",
                      f"landscape_label={landscape_label!r}",
                      f"landscape_width={int(landscape_width)}",
                      f"supersample={int(supersample)}"])
    return run_in_unreal(_crater_call(f"apply_crater_mask({args})"), log,
                         "Adding crater ground truth in Unreal", discovery_seconds)


def remove_crater_mask_from_unreal(log=print, discovery_seconds=2.0):
    """Restores the original object-mask material. Returns True on success."""
    return run_in_unreal(_crater_call("remove_crater_mask()"), log,
                         "Restoring the object-mask material", discovery_seconds)

"""
Terrain Studio startup script for the Unreal project
-----------------------------------------------------
Copy to <UnrealProject>/Content/Python/init_unreal.py. Unreal runs it when the
editor starts, and it starts the rover-shadow watcher: it keeps the
<actor>_RoverShadowCapture helper attached to its mask camera, in the level and
in a running game (see "Rover shadow" in the README). Without it the watcher
only starts once the studio sends masks or presses Find.

The watcher code lives in Terrain Studio (Lunar_terrain_loader); set LOADER_DIR
below, or the TERRAIN_STUDIO_LOADER environment variable, to that folder. If it
is missing, the editor starts normally and the log says so.
"""

import os
import sys

import unreal

LOADER_DIR = (os.environ.get("TERRAIN_STUDIO_LOADER")
              or r"C:\Users\raul.castilla\OneDrive - University of Luxembourg\Desktop"
                 r"\Git Projects\Unreal-Moon-Terrain-Generation\Terrain_Studio\Lunar_terrain_loader")


def _start_rover_shadow_watch():
    if not os.path.isfile(os.path.join(LOADER_DIR, "unreal_crater_mask.py")):
        unreal.log_warning(f"Terrain Studio: {LOADER_DIR} not found; rover-shadow watcher not started.")
        return
    if LOADER_DIR not in sys.path:
        sys.path.append(LOADER_DIR)
    try:
        import unreal_crater_mask
        unreal_crater_mask.install_rover_shadow_watch()
        unreal.log("Terrain Studio: rover-shadow watcher started.")
    except Exception as error:      # Never block the editor's startup
        unreal.log_warning(f"Terrain Studio: rover-shadow watcher not started ({error}).")


_start_rover_shadow_watch()

"""
Terrain Studio startup script for the Unreal project
-----------------------------------------------------
Lives at <UnrealProject>/Content/Python/init_unreal.py; Unreal runs it when the
editor opens. It starts the Terrain Studio editor watcher, which keeps the
rover-shadow helper attached to its mask camera, points the sun mask at the
sun, and lays wheel tracks during Play.

The watcher's modules are in Content/Python/TerrainStudio, inside the project,
so it needs neither Terrain Studio nor its repository. Terrain Studio refreshes
this folder and this script whenever it sends anything to Unreal
(unreal_link/runtime_sync.py); their sources are
Terrain_Studio/unreal_runtime, which holds only the watcher. If the folder is missing,
the script falls back to the TERRAIN_STUDIO_RUNTIME environment variable (a
Terrain_Studio folder) or the repository below; if none is found, the editor
starts normally and the Output Log says so.
"""

import os
import sys

import unreal

RUNTIME_FOLDER = "TerrainStudio"
REPOSITORY_STUDIO = (r"C:\Users\raul.castilla\OneDrive - University of Luxembourg\Desktop"
                     r"\Git Projects\Unreal-Moon-Terrain-Generation\Terrain_Studio")


def _runtime_directories():
    """
    Folders holding the watcher's modules, the project's own copy first, else the
    repository's unreal_runtime. None if not found.
    """
    project_python = os.path.join(unreal.Paths.convert_relative_path_to_full(
        unreal.Paths.project_content_dir()), "Python")
    copy = os.path.join(project_python, RUNTIME_FOLDER)
    if os.path.isfile(os.path.join(copy, "unreal_watcher.py")):
        return [copy]
    for studio in (os.environ.get("TERRAIN_STUDIO_RUNTIME", ""), REPOSITORY_STUDIO):
        runtime = os.path.join(studio, "unreal_runtime")
        if studio and os.path.isfile(os.path.join(runtime, "unreal_watcher.py")):
            return [runtime]
    return None


def _start_watcher():
    folders = _runtime_directories()
    if folders is None:
        unreal.log_warning("Terrain Studio: watcher modules not found; watcher not started.")
        return
    for folder in reversed(folders):    # Before any other copy of the same modules
        if folder not in sys.path:
            sys.path.insert(0, folder)
    try:
        import unreal_watcher
        unreal_watcher.install_watcher()
        unreal.log(f"Terrain Studio: watcher started (from {folders[0]}).")
    except Exception as error:          # Never block the editor's startup
        unreal.log_warning(f"Terrain Studio: watcher not started ({error}).")


_start_watcher()

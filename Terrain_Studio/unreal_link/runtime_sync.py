"""
Unreal Runtime Sync
-------------------
Keeps the Unreal project's own copy of the editor watcher up to date. The
watcher (rover-shadow helper attachment, sun mask, wheel tracks) runs
inside the Unreal Editor from <Project>/Content/Python: init_unreal.py starts it
when the editor opens, and the modules it needs live in
Content/Python/TerrainStudio. The project therefore works without Terrain
Studio or this repository.

Source: Terrain_Studio/unreal_runtime/, which holds only the watcher
(unreal_watcher, unreal_wheel_tracks) and init_unreal.py. It
imports nothing from the studio; the studio's Unreal-side commands in
unreal_commands/ use it, never the other way round.

with_runtime_sync(command) prefixes a remote-execution command with Unreal-side
code that copies those files into the project when they differ and puts the
project's copy first on Unreal's module path, so every action the studio sends
to Unreal (map, rocks, masks, sun) runs the same, just-updated code as the
watcher. A failed copy is reported and never stops the action.
"""

import os

LINK_DIRECTORY = os.path.dirname(os.path.abspath(__file__)).replace("\\", "/")
RUNTIME_SOURCE = os.path.join(os.path.dirname(LINK_DIRECTORY), "unreal_runtime").replace("\\", "/")
RUNTIME_FOLDER = "TerrainStudio"        # Under <Project>/Content/Python
WATCHER_MODULES = ("unreal_watcher.py", "unreal_wheel_tracks.py")
# Studio modules that run in Unreal (unreal_commands/ and rock_models), under
# their current and earlier names: never kept in the project copy, and reloaded
# from the studio if a stale or moved copy is still loaded.
STUDIO_MODULES = ("unreal_ground_truth.py", "unreal_track_setup.py", "unreal_rocks.py",
                  "unreal_landscape.py", "unreal_sun.py", "unreal_level_saver.py", "rock_models.py",
                  "unreal_crater_mask.py", "unreal_rock_placer.py", "unreal_terrain_generator.py",
                  "mask_classes.py")
STARTUP_SCRIPT = "init_unreal.py"

_SYNC_CODE = r'''
def _terrain_studio_sync():
    import os, shutil, sys, unreal
    python_dir = os.path.join(unreal.Paths.convert_relative_path_to_full(
        unreal.Paths.project_content_dir()), "Python")
    runtime_dir = os.path.join(python_dir, {folder!r})
    os.makedirs(runtime_dir, exist_ok=True)
    pairs = [(os.path.join({runtime_source!r}, name), os.path.join(runtime_dir, name))
             for name in {watcher!r}]
    pairs.append((os.path.join({runtime_source!r}, {startup!r}), os.path.join(python_dir, {startup!r})))
    changed = []
    for source, target in pairs:
        if not os.path.isfile(source):
            continue
        with open(source, "rb") as handle:
            data = handle.read()
        if os.path.isfile(target):
            with open(target, "rb") as handle:
                if handle.read() == data:
                    continue
        shutil.copyfile(source, target)
        changed.append(os.path.relpath(target, python_dir).replace(os.sep, "/"))
    removed = []
    for name in os.listdir(runtime_dir):              # Only the watcher belongs here
        if name.endswith(".py") and name not in {watcher!r}:
            os.remove(os.path.join(runtime_dir, name))
            removed.append(name)
    if changed:
        unreal.log("Terrain Studio: updated Content/Python/" + ", ".join(changed))
    if removed:
        unreal.log("Terrain Studio: removed from Content/Python/" + {folder!r} + ": " + ", ".join(removed))
    # The project's copy first, so the watcher and the studio's commands share it;
    # forget copies loaded from elsewhere (e.g. a moved file).
    if runtime_dir in sys.path:
        sys.path.remove(runtime_dir)
    sys.path.insert(0, runtime_dir)
    here = os.path.normcase(os.path.abspath(runtime_dir))
    for name in {watcher!r} + {studio!r}:
        module = sys.modules.get(name[:-3])
        if module is None:
            continue
        origin = os.path.normcase(os.path.abspath(getattr(module, "__file__", "") or ""))
        from_copy = os.path.dirname(origin) == here
        updated = any(entry.endswith("/" + name) for entry in changed)   # Loaded before this update
        if (name in {watcher!r}) != from_copy or updated or not os.path.isfile(origin):
            del sys.modules[name[:-3]]
try:
    _terrain_studio_sync()
except Exception as _error:
    import unreal
    unreal.log_warning(f"Terrain Studio: could not update Content/Python ({{_error}})")
'''


def sync_code():
    """Unreal-side Python that refreshes the project's copy of the runtime files."""
    return _SYNC_CODE.format(folder=RUNTIME_FOLDER, runtime_source=RUNTIME_SOURCE,
                             watcher=WATCHER_MODULES, studio=STUDIO_MODULES,
                             startup=STARTUP_SCRIPT)


def with_runtime_sync(command):
    """`command`, preceded by the runtime sync."""
    return sync_code() + "\n" + command

# Editor watcher

A small Python function runs inside the Unreal Editor on every frame. It:

- points the rover's shadow map at the rover from the sun;
- keeps `MPC_Sun` pointing at the sun (the sun's id in the object mask);
- during Play, lays the wheel tracks.

It only reads the rover and moves its own objects. It is editor Python, so it
works in the editor and Play In Editor, not in a packaged game.

## Where it lives

In the Unreal project, independent of Terrain Studio:

| File | Role |
| --- | --- |
| `<Project>/Content/Python/init_unreal.py` | Unreal runs it when the editor opens; it starts the watcher |
| `<Project>/Content/Python/TerrainStudio/*.py` | The watcher's modules: `unreal_watcher`, `unreal_wheel_tracks` |

Sources in this repository, with the watcher and the studio kept apart:

| Folder | Files | Role |
| --- | --- | --- |
| `Terrain_Studio/unreal_runtime/` | `init_unreal.py`, `unreal_watcher.py`, `unreal_wheel_tracks.py` | Only the watcher's per-frame work. Copied into the project; imports nothing from the studio |
| `Terrain_Studio/unreal_commands/` | `unreal_ground_truth.py`, `unreal_track_setup.py`, ... | The studio's Unreal-side commands, run when it sends: mask material, cameras, rover-shadow helper, track decals. Uses the watcher's helpers, never the other way round |

## Kept up to date

Every time Terrain Studio sends anything to Unreal (map, rocks, masks, sun), it
first copies these files into the project when they differ
(`unreal_link/runtime_sync.py`) and removes anything else from
`Content/Python/TerrainStudio`. It puts the project's copy first on Unreal's
module path and reloads the modules it updated, so the studio's commands and the
watcher run the same code; the Output Log shows "Terrain Studio: updated
Content/Python/...". Unchanged files are left alone.

**If the folder is missing** (the studio has not sent anything to this project
yet), the startup script falls back to the `TERRAIN_STUDIO_RUNTIME` environment
variable, set to a `Terrain_Studio` folder. If neither is found, the editor
starts normally and the Output Log says the watcher was not started; the next
send from the studio installs it.

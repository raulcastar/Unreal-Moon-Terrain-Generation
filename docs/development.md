# Development

## Tests

The generator (terrain, craters, rocks) and the studio's logic have unit tests
in `tests/`, on small synthetic maps; they run in a few seconds:

```sh
py -m pip install -r requirements-dev.txt
py -m pytest
```

They check, among others, that no rock lands in a crater or on the rover
corridors, that rocks never overlap, that rock sizes follow the lunar
size-frequency law, the rock height cap, crater detection on known craters, and
the crater-mask labels. Every test writes into its own temporary folder: the
studio's output paths are redirected there, and the run fails if anything in the
real `outputs/` folder changed. Tests never reach a running Unreal Editor.

## Project layout

The studio's code is split by where it runs:

```
Terrain_Studio/
  terrain_studio.py              Starts the studio (gui.app.main)
  gui/                           The studio's window (Tk)
    app.py                       Main window: home screen, view switching, DPI scaling
    generator_page.py            Generate view: layout, events, background jobs
    generator_logic.py             its parameter schema, seeds, read-outs (no Tk)
    generator_plots.py             its relief, 3D surface and slope drawing (Matplotlib)
    landscape_page.py            Send to Unreal view: DEM to Landscape
    rock_planner_page.py         Rock Planner view: layout, events, jobs
    rock_planner_logic.py          its schema, input parsing, read-outs (no Tk)
    rock_planner_plots.py          its plan drawing
    ground_truth_page.py         Ground Truth view: crater map, mask ids, mask camera, tracks
    ground_truth_logic.py          its mask ids and classes, crater source, track look, cameras
    ground_truth_plots.py          its crater-mask drawing
    sun_page.py                  Sun view: layout, events, jobs
    sun_logic.py                   its sun position, tone curve, shadow read-outs (no Tk)
    sun_plots.py                   its map, sky dome and 3D drawing
    plot_common.py               Drawing helpers shared by the views
    map_views.py                 Shaded-relief map view, map viewer and map browser
    widgets.py                   Tooltips, progress panel, dialogs, parameter rows, log
    dem_files.py                 Finding and loading DEMs; the files each step writes
    paths.py                     Repository folders (config, input_maps, outputs)
    backend.py                   Imports generator/ and unreal_link/ for the views
  generator/                     Terrain maths, no Unreal (shared by the GUI and the CLI)
    terrain_core.py              Terrain generation
    generate_cli.py              Command-line generator
    rock_field.py                Rock placement and corridor planning
    crater_map.py                Crater detection and the crater label map
    sun_shadows.py               Sun direction and cast shadows over a DEM
  unreal_link/                   Runs in the studio; talks to a running Unreal Editor
    unreal_connection.py         Sends a command to Unreal (run_in_unreal, unreal_call)
    remote_execution.py          Unreal's Remote Execution protocol (client, by Epic Games)
    send_terrain.py              DEM to Landscape (also a command-line tool)
    send_rocks.py                Rock field
    send_masks.py                Ground truth: crater map, mask camera, tracks
    send_sun.py                  Sun
    heightmap_files.py           DEM reading, 16-bit heightmap, Landscape sizing
    rock_models.py               Rock model sizes (also used inside Unreal)
    mask_classes.py              Object-mask classes from config/mask_classes.json (also used inside Unreal)
    runtime_sync.py              Copies unreal_runtime/ into the Unreal project
  unreal_commands/               Runs inside Unreal when the studio sends
    unreal_landscape.py          Builds the Landscape
    unreal_rocks.py              Imports rock models and places the rocks
    unreal_ground_truth.py       Mask material, crater map, cameras, rover-shadow helper, sun mask
    unreal_track_setup.py        Track decal pool and material
    unreal_sun.py                Lunar Directional Light
    unreal_level_saver.py        Saves the level
  unreal_runtime/                The editor watcher only (copied to <Project>/Content/Python)
    init_unreal.py               Unreal startup script: starts the watcher
    unreal_watcher.py            Per-frame: rover-shadow helper, sun direction
    unreal_wheel_tracks.py       Per-frame: lays the wheel tracks during Play
mcp/                             MCP server for AI agents
config/                          Terrain presets, rocks/ presets, mask_classes.json
input_maps/  input_rocks/  outputs/
tests/
docs/
```

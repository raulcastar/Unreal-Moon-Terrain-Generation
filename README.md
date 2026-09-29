# Unreal Moon Terrain Generation

Procedural lunar Digital Elevation Model (DEM) generation, previewing, and import
into Unreal Engine as a scaled Landscape actor.

Everything lives under `Terrain_Studio/`, which has three parts:

| Component | Purpose |
| --- | --- |
| `terrain_studio.py` | Graphical front-end: set parameters, preview the surface, generate, and push to Unreal. |
| `Procedural_generator/` | The procedural simulation itself, plus a CLI for batch/headless runs. |
| `Lunar_terrain_loader/` | Converts any DEM (procedural, `.npy`, `.csv`, GeoTIFF) into Unreal's 16-bit `.r16` heightmap and spawns the Landscape actor over Remote Execution. |

Paths below are written from the repository root, so the two engines appear as
`Terrain_Studio/Procedural_generator/` and `Terrain_Studio/Lunar_terrain_loader/`.

Three folders at the root carry the data. `config/` is tracked; the other two
are not, so nobody commits a gigabyte of DEMs by accident:

| Folder | Contents |
| --- | --- |
| `config/` | Terrain presets as YAML, shared by the GUI and the CLI. |
| `input_maps/` | Raw source DEMs you bring in (GeoTIFF, `.npy`, `.csv`). |
| `outputs/` | Everything the studio produces, in one subfolder per map: the generated DEM, its soil and rock-field CSVs, and the Unreal `.r16` heightmap. |

---

## Quick start

Windows:

```bat
run_terrain_studio.bat
```

Linux / macOS:

```sh
chmod +x run_terrain_studio.sh   # once
./run_terrain_studio.sh
```

Or directly, on any platform:

```sh
python Terrain_Studio/terrain_studio.py
```

Requirements: `numpy`, `scipy`, `pandas`, `pyyaml`, `matplotlib` (and `tifffile`
only if you import GeoTIFF DEMs). Tkinter ships with Python (on Debian/Ubuntu
install it with `sudo apt install python3-tk`).

**Column widths** — the three columns are separated by drag handles: one between
the parameters and the preview, one between the preview and the readouts. Drag
either to give a wide DEM more preview, or the readouts more room. Only the
preview absorbs a window resize, so the two side columns keep the width you set.
Both side columns scroll when their content is taller than the window, and the
export panel's text rewraps to the width you give it, so a wider column is also
a shorter one.

The studio opens maximized. On Windows it is per-monitor DPI aware: dragging the
window between monitors with different display scaling keeps it crisp and
rescales the UI to the new monitor (preserving your parameters, preview, and
selected map).

---

## Lunar Terrain Studio

**Home screen** — on launch the studio opens on a landing screen laid out as
the pipeline it is: three numbered stages, then the map library. A status line
reports how many maps are in `input_maps/` and `outputs/` and which one the
stages would act on, so the next step is visible rather than guessed.

1. *Create a Terrain* — set parameters, preview, and export a procedural DEM.
2. *Add Rocks & Rover Paths* (optional) — scatter rocks across the drivable
   ground while keeping a connected corridor network the rover can always
   drive. Writes an instance list, not a new DEM.
3. *Send to Unreal* — convert an elevation map to a `.r16` heightmap and spawn
   the Landscape actor. It does not open the parameter studio: converting an
   existing DEM does not use any of those controls.

*Browse Maps* is the library rather than a stage, and whatever you leave
selected becomes the current map for stages 2 and 3. It opens the map browser: the project's maps listed on
  the left, the selected one rendered on the right. Moving the selection
  redraws the view, so a folder can be read through in one pass instead of
  reopening a file picker per map.

  The list covers `input_maps/` and `outputs/` one level
  deep, grouped by folder, and both are always shown — an `outputs/` that does
  not exist yet is listed as such rather than left out, so exports are never
  somewhere you cannot see. The divider between the list and the view is
  draggable. *Add folder…* brings in any other directory, *Refresh* picks up
  maps written since it opened, and *Include masks and other rasters* adds the
  `_classMap` / `_soilMap` files that are hidden by default (fine to look at,
  never to import). Loading runs in the background with a "Loading…" indicator,
  so large maps never freeze the window, and moving quickly through the list
  loads only the map you land on rather than every one you pass.
Each choice replaces the home screen inside the same window rather than
opening one of its own, so the app stays a single place with a single
taskbar entry and the window title names the view you are in. Every view
carries a `⌂ Home` button that returns here, and its state is kept, so
leaving and coming back does not reset it.

### Send a Map to Unreal

A standalone window holding only what the export reads:

- **Source map** — *Browse* drops down the DEMs already in `input_maps/` and
  `outputs/`, grouped by folder, since that is where almost every map is. Below
  the list it can open the file picker aimed at either folder, or anywhere else,
  for maps the list does not reach.
  Companion rasters that share those folders and extensions but are not terrain
  (`_classMap`, `_soilMap`, `_obstacleMap`, `_rocks`, `_roverPaths`, and the
  loader's `_converted` intermediates) are left off the list, so a class raster
  cannot be imported as a landscape by mistake.
  The chosen map is rendered in the window straight away, so confirming you
  picked the right surface takes no extra click; the divider between the
  controls and the map is draggable.
- **Resolution (m/px)** — the ground distance one pixel covers, which sets the
  landscape's XY scale. GeoTIFFs carry their own and ignore it.
- **Dispatch to running Unreal Editor** — unticked, the heightmap is written but
  no editor is contacted.
- **Writes to** — the exact folder the files will land in, updated as soon as a
  map is picked, so there is no guessing afterwards, with a button that opens
  it. Like the dialogs, it breaks the path at its separators and re-flows as
  you widen the column.
- A log and progress bar, and *Stop*, which cancels between the conversion and
  the network dispatch.

When it finishes, a dialog gives the `.r16` path and the manual import steps.
Paths in the dialogs are broken across lines at their own separators rather
than word-wrapped, which a folder like `OneDrive - University of Luxembourg`
would otherwise split mid-path.
The same export is still available inside the studio, next to the generator
controls, where it follows on from a surface you just generated; both run the
same pipeline.

The studio itself covers the whole pipeline.

**Parameters (left)** — every simulation parameter as a slider plus typed entry,
grouped by stage, each with a hover tooltip. Presets are the same YAML files the
CLI reads (`config/`), so anything saved here runs headless.

**Preview (centre)** — a reduced-resolution render of the same pipeline, updated
as you drag, in three tabs:

- **Shaded Relief** — hillshade with adjustable sun azimuth and elevation.
- **3D Surface** — shaded mesh. Relief is metres over kilometres, so the view
  auto-fits its vertical exaggeration and reports the factor in the title. The
  mesh drops to coarse detail while dragging and restores on release.
- **Slope Analysis** — slope magnitude in degrees plus the elevation histogram.

Crater radii and the erosion sigma scale with the map, and the preview reuses the
full-resolution crater count, so it is representative of the final export. After
**Generate Full Resolution**, the panes show the real exported array.

**Determinism** — leave the seed blank and one is rolled, written to the field,
and reused for preview and export. Type a seed to reproduce a surface.

**Readouts (right)** — live surface statistics (relief, mean/P99/max slope,
crater count) and the resulting Unreal Landscape topology (section size,
component grid, vertex count, world size). The topology panel warns before you
generate if the map exceeds the 50×50 component ceiling of a single Landscape.

**Export & Unreal (right)** — the two-step output workflow:

1. *Generate Full Resolution*. The panel lists the filenames it will write,
   extensions included, under the base filename field and updates as you type,
   so the result is visible before the run rather than inferred from a stem.
   Confirming shows the same list with a description of each file, then runs.
   Each run gets its own subfolder `outputs/<name>/`. The `.csv` pair only
   appears when `EXPORT_CSV` is on in the loaded preset.
2. *Send to Unreal* needs an **elevation map** in metres, as `.npy`, `.csv`,
   `.txt`, `.tif` or `.tiff` - usually Step 1's `<name>_elevationMap.npy`. The
   companion rasters written beside it (`_classMap`, `_soilMap`, `_obstacleMap`,
   `_rocks`, `_roverPaths`) share those extensions but are not heights, so the
   *Browse* drop-down leaves them off the list and picking one by hand raises a
   warning under the field rather than silently building terrain out of mask
   values.

   It converts that map to Unreal's scalar space, writes
   the `.r16` / `.json` / `.txt` files to `outputs/<name>/` beside the DEM
   they came from, then
   spawns the Landscape actor in a running editor. A dialog reports the `.r16`
   path and the manual import steps. Untick *Dispatch to running Unreal Editor*
   to only write the files.

   The *Source map* defaults to the last full-resolution generation; *Browse*
   drops down the DEMs already in `input_maps/` and `outputs/`, then offers the
   file picker aimed at either folder, or anywhere else, for maps the list does
   not reach (either way it previews the pick). Resolution comes from the Map
   Geometry field (GeoTIFFs use their embedded
   scale). To just look at a map without sending it, use *View an Existing Map*
   on the home screen.

*Stop* cancels a running generation or Unreal export at its next checkpoint;
partial files may remain and should be regenerated.

Unreal needs the **Python Editor Script Plugin** with **Remote Execution**
enabled in Project Settings. Applying the heightmap to the spawned Landscape is a
manual step, described by the editor dialog (Landscape Mode → Manage →
right-click → Import).

---

## Rock Field & Rover Paths

*Place Rocks & Paths* on the home screen opens a planner that decides where to
put rocks as obstacles on an elevation map, while guaranteeing the rover keeps a
drivable route. The guarantee is structural, not statistical:

1. **Craters** are the impassable hazard — closed depressions at least a set
   depth deep, found by a priority-flood sink fill (so crater bowls are caught,
   not ordinary valleys). The DEM is reflection-padded first so the map border
   acts as a no-flow wall, which means craters straddling the edges are detected
   too, not missed as if they drained off the map. Craters are impassable *and*
   excluded from rock placement; everything else is drivable.
2. The road network follows the **Voronoi diagram of the craters**: each crater
   is a Voronoi *site*, and the roads are the Voronoi *edges* between them —
   every edge is equidistant from the two craters it separates, so the rover
   drives down the middle of the gaps. Pieces broken by a crater are bridged into
   one connected network. (With fewer than two craters there is no diagram, so no
   roads are drawn.)
3. Those corridors, widened to the path width, are protected — no rock is ever
   placed within half the path width of them. The lane therefore stays open no
   matter how densely rocks are scattered elsewhere.

Load any `.npy` / `.csv` / `.tif` DEM (or let it inherit the last generated map),
set the path width, the crater depth threshold, and the rock population
(coverage — capped at 0.05 — plus min/max scale and height ratio). The preview
overlays craters (blue), the corridors (green) and their crater sites, and the
rocks sized by footprint, with a legend; the log reports the rock count, the
drivable and crater fractions, and whether the road network is one connected
route. Planning runs on a grid capped at 512 px, so it stays fast on large DEMs
and scales the results back to world metres.

*Load DEM* drops down the maps already in `input_maps/` and `outputs/`, with
the file picker aimed at either folder below the list, exactly as the export
panels do. Loading a map also names the export after it, so the rock field is
written into that map's own output folder rather than into whatever folder the
base filename was last left pointing at.

The parameter panel scrolls, with **⌂ Home**, **Update Preview**, **Stop**, and
**Generate & Export** pinned in view at all times, and a drag handle sets how
much width it takes from the plan. *Stop* cancels a long plan at
its next checkpoint while keeping the DEM loaded, so you can retune and retry.

The Export panel lists the filenames it will write, extensions included, and
updates as you type or toggle the obstacle map. *Generate & Export* writes them
into the same `outputs/<name>/` folder as the DEM:

The panel names each file's columns on screen; the units are here. `x_m` and
`y_m` are metres east and north of the map corner, with no Z — read the height off the DEM.
`scale` is the uniform mesh scale, the radius in metres of a 1 m base mesh, and
`height_m` is `scale x ROCK_HEIGHT_RATIO`. Planning runs on a grid capped at
512 px, so `x_m`/`y_m` are exact while `row`/`col` carry that coarser step.

| File | Contents |
| --- | --- |
| `<name>_rocks.csv` | One row per rock: `id, row, col, x_m, y_m, scale, height_m` — an instance list to spawn as static meshes in Unreal. |
| `<name>_roverPaths.csv` | The rover corridor polylines: `path_id, x_m, y_m`. |
| `<name>_obstacleMap.csv` | Optional full-resolution `0/1` rock-footprint raster (a mask aligned to the DEM). |

---

## Command line

The generator remains fully usable without the GUI:

```bat
cd Terrain_Studio\Procedural_generator
py procedural_landscape_gen.py -c ..\..\config\lunar_1m.yaml
py procedural_landscape_gen.py -c ..\..\config\lunar_high_valleys_1m.yaml -n my_terrain --no-preview
```

| Flag | Meaning |
| --- | --- |
| `-c, --config` | YAML configuration (created with defaults if missing). |
| `-o, --output-dir` | Destination directory (default `<repo root>/outputs`). |
| `-n, --name` | Base filename for the exported elevation map. |
| `--no-preview` | Skip the Matplotlib window, for batch runs. |

Importing an existing DEM (GeoTIFF, `.npy`, `.csv`) into Unreal without the GUI:

```bat
cd Terrain_Studio\Lunar_terrain_loader
py main.py ..\..\input_maps\Lunar_LROnac_Haworth_1m.tif ..\..\outputs\Haworth 1.0
```

---

## Simulation parameters

| Key | Meaning |
| --- | --- |
| `ROWS`, `COLS` | DEM grid size in pixels. |
| `RESOLUTION_M_PER_PX` | Ground metres per pixel; drives the Unreal XY scale. |
| `BASE_ELEVATION` | Datum in metres. Kept above zero because the Unreal converter treats exact zeros as no-data. |
| `RANDOM_SEED` | `null` for a new surface each run, or an integer to lock it. |
| `MACRO_ELEVATION_MIN` / `MAX` | Bounds of the total hill height differential; the actual value is drawn between them. |
| `EROSION_BLUR_DIVISOR` | Smoothing sigma is `max(rows, cols) / divisor`. Lower = smoother and more eroded. |
| `MICRO_ROUGHNESS` | High-frequency dithering amplitude; prevents 16-bit quantization banding in-engine. |
| `CRATER_DENSITY` | Impacts per pixel of area — total craters are `rows × cols × density`. |
| `CRATER_MIN_RADIUS_PCT` / `MAX` | Crater radii as a fraction of the largest map dimension. Radii are cubically biased towards the minimum. |
| `CRATER_DEPTH_RATIO` | Bowl depth relative to radius (real lunar craters sit near 0.2). |
| `CRATER_RIM_HEIGHT_RATIO` | Ejecta rim height relative to bowl depth. |
| `EXPORT_CSV`, `EXPORT_NPY` | Output formats. |

Craters are placed with collision rejection so bowls and rims never overlap, and
placement gives up after ten attempts per crater — at high densities fewer than
the target are placed, which the studio reports.

**Performance.** The macro terrain is built and eroded on a grid capped at
1024 px, then upsampled. Its blur radius scales with the map, so a direct build
is roughly O(N³) — a 10000×10000 map took ~45 minutes for that step alone, versus
seconds with the cap. Maps at or below the cap are built natively and are
bit-for-bit identical to the direct method. Crater placement and file writing
still scale with the map, so use *Stop* if a large run runs long.

---

## Layout

```
Terrain_Studio/
  terrain_studio.py              GUI: parameters, preview, export, Unreal dispatch
  Procedural_generator/
    terrain_core.py              Generation engine (shared by GUI and CLI)
    rock_field.py                Rock placement & rover-corridor planning engine
    procedural_landscape_gen.py  CLI front-end
  Lunar_terrain_loader/
    main.py                      DEM -> Unreal orchestration
    terrain_files_generator.py   DEM reading, 16-bit conversion, component solving
    unreal_terrain_generator.py  Runs inside Unreal; spawns the Landscape actor
    unreal_api.py                Remote Execution client
config/*.yaml                    Terrain presets
input_maps/                      Raw source DEMs (untracked)
outputs/<name>/                  Everything generated for one map (untracked)
run_terrain_studio.bat           Windows launcher
run_terrain_studio.sh            Linux / macOS launcher
```

The studio finds both engines relative to its own file, and both engines find
`config/`, `input_maps/` and `outputs/` relative to the repository root, so
`Terrain_Studio/` can be moved or renamed as a whole without editing any path.

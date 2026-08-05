# Unreal Moon Terrain Generation

Procedural lunar Digital Elevation Model (DEM) generation, previewing, and import
into Unreal Engine as a scaled Landscape actor.

The project has three parts:

| Component | Purpose |
| --- | --- |
| `Terrain_Studio/` | Graphical front-end: set parameters, preview the surface, generate, and push to Unreal. |
| `Procedural_generator/` | The procedural simulation itself, plus a CLI for batch/headless runs. |
| `Lunar_terrain_loader/` | Converts any DEM (procedural, `.npy`, `.csv`, GeoTIFF) into Unreal's 16-bit `.r16` heightmap and spawns the Landscape actor over Remote Execution. |

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

The studio opens maximized. On Windows it is per-monitor DPI aware: dragging the
window between monitors with different display scaling keeps it crisp and
rescales the UI to the new monitor (preserving your parameters, preview, and
selected map).

---

## Lunar Terrain Studio

**Home screen** — on launch the studio opens on a landing screen with three
choices:

- *Generate New Terrain* — opens the studio to set parameters, preview, and export.
- *View an Existing Map* — opens a file picker and shows the chosen `.npy` /
  `.csv` / `.tif` DEM in a preview window: the shaded relief on top and an
  activity log below (like the studio). Loading runs in the background with a
  "Loading…" indicator, so large maps never freeze the window.
- *Place Rocks & Paths* — scatters rocks as obstacles across a DEM while keeping
  a connected corridor network the rover can always drive (see below).
- *Send a Map to Unreal* — opens the studio and prompts for a DEM to convert.

The `⌂ Home` button at the top of the parameter column returns here anytime.

The studio itself covers the whole pipeline.

**Parameters (left)** — every simulation parameter as a slider plus typed entry,
grouped by stage, each with a hover tooltip. Presets are the same YAML files the
CLI reads (`Procedural_generator/config/`), so anything saved here runs headless.

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

1. *Generate Full Resolution* first shows a dialog listing the output files
   (`<name>_elevationMap.npy`, `<name>_elevationMap.csv`, `<name>_soilMap.csv`)
   and their target folder; on confirm it runs and writes them. Each run gets its
   own subfolder `Procedural_generator/outputs/<name>/`.
2. *Send to Unreal* converts the **source map** to Unreal's scalar space, writes
   the `.r16` / `.json` / `.txt` files to `Lunar_terrain_loader/outputs/`, then
   spawns the Landscape actor in a running editor. A dialog reports the `.r16`
   path and the manual import steps. Untick *Dispatch to running Unreal Editor*
   to only write the files.

   The *Source map* defaults to the last full-resolution generation; use
   *Browse* to pick any `.npy` / `.csv` / `.tif` DEM instead (which also previews
   it). Resolution comes from the Map Geometry field (GeoTIFFs use their embedded
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

The parameter panel scrolls, with **⌂ Home**, **Update Preview**, **Stop**, and
**Generate & Export** pinned in view at all times. *Stop* cancels a long plan at
its next checkpoint while keeping the DEM loaded, so you can retune and retry.

*Generate & Export* writes, into the same `Procedural_generator/outputs/<name>/`
folder as the DEM:

| File | Contents |
| --- | --- |
| `<name>_rocks.csv` | One row per rock: `id, row, col, x_m, y_m, scale, height_m` — an instance list to spawn as static meshes in Unreal (`scale` is the uniform mesh scale). |
| `<name>_roverPaths.csv` | The rover corridor polylines: `path_id, x_m, y_m`. |
| `<name>_obstacleMap.csv` | Optional full-resolution `0/1` rock-footprint raster (a mask aligned to the DEM). |

---

## Command line

The generator remains fully usable without the GUI:

```bat
cd Procedural_generator
py procedural_landscape_gen.py -c config/lunar_1m.yaml
py procedural_landscape_gen.py -c config/lunar_high_valleys_1m.yaml -n my_terrain --no-preview
```

| Flag | Meaning |
| --- | --- |
| `-c, --config` | YAML configuration (created with defaults if missing). |
| `-o, --output-dir` | Destination directory (default `outputs`). |
| `-n, --name` | Base filename for the exported elevation map. |
| `--no-preview` | Skip the Matplotlib window, for batch runs. |

Importing an existing DEM (GeoTIFF, `.npy`, `.csv`) into Unreal without the GUI:

```bat
cd Lunar_terrain_loader
py main.py maps\Lunar_LROnac_Haworth_1m.tif outputs 1.0
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
  terrain_studio.py            GUI: parameters, preview, export, Unreal dispatch
Procedural_generator/
  terrain_core.py              Generation engine (shared by GUI and CLI)
  rock_field.py                Rock placement & rover-corridor planning engine
  procedural_landscape_gen.py  CLI front-end
  config/*.yaml                Presets
  outputs/<name>/              Generated DEMs, one subfolder per run
Lunar_terrain_loader/
  main.py                      DEM -> Unreal orchestration
  terrain_files_generator.py   DEM reading, 16-bit conversion, component solving
  unreal_terrain_generator.py  Runs inside Unreal; spawns the Landscape actor
  unreal_api.py                Remote Execution client
  maps/                        Source DEMs
  outputs/                     .r16 heightmaps and metadata
run_terrain_studio.bat         Windows launcher
run_terrain_studio.sh          Linux / macOS launcher
```

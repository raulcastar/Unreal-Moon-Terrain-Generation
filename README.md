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

```bat
run_terrain_studio.bat
```

or

```bat
py Terrain_Studio\terrain_studio.py
```

Requirements: `numpy`, `scipy`, `pandas`, `pyyaml`, `matplotlib` (and `tifffile`
only if you import GeoTIFF DEMs). Tkinter ships with Python.

---

## Lunar Terrain Studio

A single window covering the whole pipeline.

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

1. *Generate Full Resolution* writes `<name>_elevationMap.npy`,
   `<name>_elevationMap.csv`, and `<name>_soilMap_edges_0.csv` to
   `Procedural_generator/outputs/`.
2. *Send to Unreal* converts that DEM to Unreal's scalar space, writes the
   `.r16` / `.json` / `.txt` files to `Lunar_terrain_loader/outputs/`, then
   spawns the Landscape actor in a running editor. Untick *Dispatch to running
   Unreal Editor* to only write the files.

*Stop* cancels a running generation or Unreal export at its next checkpoint;
partial files may remain and should be regenerated.

Unreal needs the **Python Editor Script Plugin** with **Remote Execution**
enabled in Project Settings. Applying the heightmap to the spawned Landscape is a
manual step, described by the editor dialog (Landscape Mode → Manage →
right-click → Import).

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
  procedural_landscape_gen.py  CLI front-end
  config/*.yaml                Presets
  outputs/                     Generated DEMs
Lunar_terrain_loader/
  main.py                      DEM -> Unreal orchestration
  terrain_files_generator.py   DEM reading, 16-bit conversion, component solving
  unreal_terrain_generator.py  Runs inside Unreal; spawns the Landscape actor
  unreal_api.py                Remote Execution client
  maps/                        Source DEMs
  outputs/                     .r16 heightmaps and metadata
run_terrain_studio.bat
```

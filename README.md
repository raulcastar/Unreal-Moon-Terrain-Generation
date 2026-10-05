# Unreal Moon Terrain Generation

Generate procedural lunar elevation maps (DEMs), place rocks with guaranteed rover
corridors, and import the result into Unreal Engine as a scaled Landscape.

<p align="center"><img src="docs/images/home.png" width="720" alt="Home screen"></p>

## Quick start

```sh
run_terrain_studio.bat                 # Windows
./run_terrain_studio.sh                # Linux / macOS (chmod +x once)
python Terrain_Studio/terrain_studio.py  # any platform
```

Requires `numpy`, `scipy`, `pandas`, `pyyaml` and `matplotlib`; `tifffile` only for
GeoTIFF input. Tkinter ships with Python (`sudo apt install python3-tk` on Debian/Ubuntu).

The home screen lays out the four workflow stages in order, plus a map library.
Every view opens in the same window and has a **⌂ Home** button to return.

## 1. Create a terrain

![Creating a terrain](docs/images/create-terrain.gif)

- **Parameters** (left): every setting as a slider with a typed entry and tooltip.
  Presets are the YAML files in `config/`, shared with the CLI.
- **Preview** (centre): shaded relief, 3D surface and slope analysis, updated as
  you drag. A fixed seed reproduces a surface; a blank seed rolls a new one.

  ![Preview tabs: shaded relief, 3D surface, slope analysis](docs/images/create-terrain-tabs.gif)
- **Readouts** (right): surface statistics and the resulting Unreal Landscape
  topology, which warns if the map exceeds a single Landscape's 50×50 components.
- **Generate Full Resolution** writes the DEM to `outputs/<name>/`; the panel
  lists the exact filenames before you run.

Column widths are adjustable with the drag handles.

## 2. Add rocks and rover paths (optional)

![Rock field planner](docs/images/rock-planner.png)

Scatters rocks across the drivable ground while guaranteeing a connected route:

1. **Craters** — closed depressions deeper than a threshold, found by
   priority-flood sink filling — are impassable and kept free of rocks.
2. **Corridors** follow the Voronoi diagram of the crater centres, so the rover
   drives down the middle of the gaps between them.
3. **Rocks** are never placed within half the path width of a corridor.

Planning runs on a grid capped at 512 px and is scaled back to metres. Loading a
DEM names the export after it, so the files land beside that map:

| File | Contents |
| --- | --- |
| `<name>_rocks.csv` | One row per rock: `id, row, col, x_m, y_m, scale, height_m` |
| `<name>_roverPaths.csv` | Corridor polylines: `path_id, x_m, y_m` |
| `<name>_obstacleMap.csv` | Optional 0/1 rock-footprint raster at full DEM resolution |

`x_m` and `y_m` are metres east and north of the map corner (no Z — take it from
the DEM). `scale` is the uniform mesh scale, i.e. the radius in metres of a 1 m
base mesh; `height_m` is `scale × ROCK_HEIGHT_RATIO`.

## 3. Send to Unreal

![Send to Unreal](docs/images/send-to-unreal.png)

Converts an elevation map to Unreal's 16-bit `.r16` heightmap, spawns the
Landscape actor in a running editor, applies the heightmap to it and saves the
level. The heightmap is written to `outputs/<name>/`. Untick *Dispatch to
running Unreal Editor* to only write the files (a dialog then gives the path and
the manual import steps), or *Save the level afterwards* to leave saving to you.

**Unreal setup:** enable the *Python Editor Script Plugin* and *Remote Execution*
in Project Settings. Landscapes larger than 4097 × 4097 vertices, or an
automatic import that fails, fall back to the manual import (Landscape Mode →
Manage → right-click → Import), with the steps shown in the editor.

## 4. Set the sun and shadows (optional)

Places the sun so shadows fall where you want them and sets the matching light in
Unreal. The sun is set in one of three ways:

- the azimuth and elevation sliders;
- clicking the sky dome;
- drawing a shadow on the map: click where an object of a given height stands and
  drag to where its shadow should end.

Azimuth is measured clockwise from the top of the DEM image: 0 points to Unreal
−Y and 90 to −X. The Landscape mirrors the DEM, since columns run towards −X, so by
default the map is shown as Unreal's Top view shows it (+X right, +Y down). The
sky dome is labelled with Unreal's axes.

The map shows the terrain lit by that sun, with no sky light, so shadows are black.
Optionally, cast shadows are tinted, which tells them apart from slopes facing away.
A second tab shows a lit 3D view. Shadows are cast from the DEM, which matches the
Landscape: line traces in Unreal agree with them at 97–100% of points, and every
mismatch lies on a shadow edge.

**Send Sun to Unreal** updates the light labelled `LunarSun`, or the level's only
Directional Light, and spawns `LunarSun` if there is neither. It applies lunar
settings:

- white light with no atmospheric reddening;
- a 0.53° sun disc;
- movable, with dynamic shadows reaching at least 200 m;
- intensity set in the panel.

During Play In Editor, only the running game changes; send the sun again after
stopping it to keep it in the level. **Read Sun from Unreal** loads the level's
current sun into the sliders.

## Browse maps

![Browsing maps](docs/images/browse-maps.gif)

Lists every map in `input_maps/` and `outputs/`; selecting one renders it as a
shaded relief or, in the second tab, a rotatable 3D surface. The selected map
becomes the current map for stages 2 and 3. Class, soil and rock rasters are
hidden by default, since they share extensions but are not heights.

## Folders

| Folder | Contents | Tracked |
| --- | --- | --- |
| `config/` | Terrain presets (YAML) | yes |
| `input_maps/` | Source DEMs: `.npy`, `.csv`, `.txt`, `.tif` | no |
| `outputs/<name>/` | Everything generated for one map | no |

## Command line

Generate a terrain without the GUI:

```sh
cd Terrain_Studio/Procedural_generator
python procedural_landscape_gen.py -c ../../config/lunar_1m.yaml -n my_terrain --no-preview
```

| Flag | Meaning |
| --- | --- |
| `-c, --config` | YAML preset (created with defaults if missing) |
| `-o, --output-dir` | Destination (default `<repo>/outputs`) |
| `-n, --name` | Base filename |
| `--no-preview` | Skip the Matplotlib window |

Convert a DEM to a heightmap and send it to Unreal:

```sh
cd Terrain_Studio/Lunar_terrain_loader
python main.py <map> [output_dir] [resolution_m_per_px]
```

## Parameters

| Key | Meaning |
| --- | --- |
| `ROWS`, `COLS` | DEM size in pixels |
| `RESOLUTION_M_PER_PX` | Ground metres per pixel; sets the Unreal XY scale |
| `BASE_ELEVATION` | Datum in metres; kept above zero, as zero is treated as no-data |
| `RANDOM_SEED` | `null` for a new surface each run, or an integer to reproduce one |
| `MACRO_ELEVATION_MIN` / `MAX` | Range for the total hill relief |
| `EROSION_BLUR_DIVISOR` | Smoothing sigma is `max(rows, cols) / divisor`; lower is smoother |
| `MICRO_ROUGHNESS` | High-frequency noise; prevents 16-bit terracing in Unreal |
| `CRATER_DENSITY` | Craters per pixel of area |
| `CRATER_MIN_RADIUS_PCT` / `MAX` | Crater radii as a fraction of the largest map dimension |
| `CRATER_DEPTH_RATIO` | Bowl depth relative to radius (lunar craters are near 0.2) |
| `CRATER_RIM_HEIGHT_RATIO` | Rim height relative to bowl depth |
| `EXPORT_CSV`, `EXPORT_NPY` | Output formats |

Craters never overlap; at high densities fewer than the target may fit, which the
studio reports. Large maps build their macro terrain on a 1024 px grid and
upsample it, keeping a 10000×10000 map to seconds rather than tens of minutes.

## Project layout

```
Terrain_Studio/
  terrain_studio.py              GUI
  Procedural_generator/
    terrain_core.py              Terrain generation (shared by GUI and CLI)
    rock_field.py                Rock placement and corridor planning
    procedural_landscape_gen.py  CLI
  Lunar_terrain_loader/
    main.py                      DEM to Unreal pipeline
    terrain_files_generator.py   DEM reading, 16-bit conversion, Landscape sizing
    unreal_terrain_generator.py  Runs inside Unreal; spawns the Landscape
    unreal_api.py                Remote Execution client
config/  input_maps/  outputs/
docs/images/                     README screenshots
```

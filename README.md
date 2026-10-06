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

The home screen lays out the four workflow steps in order: create and send a
terrain, place rocks, mask craters, position the sun. A library holds the map
browser and a standalone sender.
Every view opens in the same window and has a **⌂ Home** button to return.

## 1. Create a terrain and send it to Unreal

![Creating a terrain](docs/images/create-terrain.gif)

- **Parameters** (left): every setting as a slider with a typed entry and tooltip.
  Presets are the YAML files in `config/`, shared with the CLI.
- **Preview** (centre): shaded relief, 3D surface and slope analysis, updated as
  you drag. A fixed seed reproduces a surface; a blank seed rolls a new one.

  ![Preview tabs: shaded relief, 3D surface, slope analysis](docs/images/create-terrain-tabs.gif)
- **Readouts** (right): surface statistics and the resulting Unreal Landscape
  topology, which warns if the map exceeds a single Landscape's 50×50 components.
- **Generate Full Resolution** writes the DEM to `outputs/<name>/`; the panel
  lists the exact filenames before you run. It also writes `<name>_craters.csv`,
  every crater it placed (`id, row, col, radius_px, x_m, y_m, radius_m, depth_m,
  rim_height_m, ejecta_radius_m`), which step 3 uses as exact crater ground truth.

Column widths are adjustable with the drag handles.

**Send to Unreal**, in the same panel, takes the generated map (or any other you
pick):

Converts an elevation map to Unreal's 16-bit `.r16` heightmap, spawns the
Landscape actor in a running editor, applies the heightmap to it and saves the
level. The heightmap is written to `outputs/<name>/`. Untick *Dispatch to
running Unreal Editor* to only write the files (a dialog then gives the path and
the manual import steps), or *Save the level afterwards* to leave saving to you.

Every new Landscape is set up for the simulator:

- it gets the project's `/Game/Landscape/Landscape_Material`;
- it is tagged for the rover's object mask (Render CustomDepth on, stencil 10,
  `landscape` in the GNC mask labels).

If that material is missing, a warning is logged and the engine's default
material stays. Both defaults are at the top of `unreal_terrain_generator.py`.

**Unreal setup:** enable the *Python Editor Script Plugin* and *Remote Execution*
in Project Settings. Landscapes larger than 4097 × 4097 vertices, or an
automatic import that fails, fall back to the manual import (Landscape Mode →
Manage → right-click → Import), with the steps shown in the editor.

## 2. Place rocks and rover paths (optional)

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

**Place Rocks in Unreal** puts the exported rocks on the Landscape and tags each one
for the rover's object mask (`navcam/central/object_mask`) as a boulder or a small
rock. The tags are the *Object mask ids*: by default 20 for boulders and 21 for
small rocks, split at a 0.15 m radius. **Update Mask Ids Only** re-tags rocks
already placed without moving them.

## 3. Crater masks (optional)

Shows craters in the rover's object mask, alongside the rocks:

1. **Find Craters** maps them on the DEM and shows them on the map preview.
2. **Mask Camera in Unreal** picks the scene capture that renders the mask. The
   list is read from the open level, and the default is the rover's
   `Navcam_Mask`.
3. **Send Crater Mask to Unreal** does three things:
   - exports the crater map;
   - rebuilds `PPM_ObjectMask` so the Landscape shows the craters;
   - sets up the chosen camera: it gets `PPM_ObjectMask` and, if its actor has a
     StereoCamStreamer, becomes the streamed mask camera.

   **Restore Original Mask Material** undoes the material change.
4. **GNC Labels** lists the `mask_labels` that
   `GNC-Moon-Simulator/component_stereo_depth/config/params.yaml` needs, for the
   rock and crater ids together, ready to copy.

Where the craters come from:

- **Terrains generated in the studio** come with `<name>_craters.csv`, and step 3
  uses it by default (*Exact, from the generator*). Centres and radii are the
  ones the generator stamped: the crater is the bowl inside the rim crest, at the
  crater radius, and the generator's ejecta rim reaches 1.5 × the radius (rim
  width 0.5).
- **Other maps** are *Detected in the DEM*, as follows. On generated terrain,
  detection sizes large craters (radius ≥ 10 px) within 3%, but makes small ones
  (5–10 px) about 40% too small. That is the reason to prefer the generator's
  list when there is one.

How detection works:

- It starts from the closed depressions the rock planner treats as impassable.
- Each crater is the circle fitted through its rim crest, so it covers the whole
  bowl even on sloping ground.
- Shallow or elongated basins are rejected.

The crater map is `<name>_craterMask.png` in the map's output folder, with a JSON
beside it. Its pixel values are mask ids:

| Value | Meaning |
| --- | --- |
| `0` | Not a crater |
| `30` | Crater: ground inside the rim crest |
| `31` | Crater rim: a ring outside the crest, 0.2 × the radius wide by default |

The map uses 4 cells per DEM cell (25 cm on a 1 m map), capped at 8192 px.

How the mask is drawn: the mask camera's material looks at each Landscape pixel
(stencil 10), takes the world point it shows, and reads the crater map there. So:

- rocks and the rover keep their own ids;
- craters hidden behind terrain stay hidden;
- the original material is kept as `PPM_ObjectMask_Original`;
- the mask material, its backup and the crater maps all live in
  `/Game/GroundTruth_Masks`;
- it also works during Play In Editor, although camera changes made then last
  only until it stops.

## 4. Position the sun (optional)

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

## Library: browse maps

![Browsing maps](docs/images/browse-maps.gif)

Lists every map in `input_maps/` and `outputs/`; selecting one renders it as a
shaded relief or, in the second tab, a rotatable 3D surface. The selected map
becomes the current map for the other steps. Class, soil and rock rasters are
hidden by default, since they share extensions but are not heights.

## Library: send an existing map

A standalone version of step 1's **Send to Unreal**, for any elevation map
already on disk.

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

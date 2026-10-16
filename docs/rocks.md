# 2. Rocks and rover paths

![Rock field planner](images/rock-planner.png)

Scatters rocks across the drivable ground while guaranteeing a connected route.

## How the plan is made

1. **Craters**: closed depressions deeper than a threshold, found by
   priority-flood sink filling, are impassable. Rocks also stay out of every
   crater bowl up to its rim crest (plus one planning cell), using the same
   crater circles as the [crater map](masks.md), so no rock lands on a crater
   id. The circles are fitted on a grid of up to 1024 px, so small craters are
   kept clear too.
2. **Corridors** follow the Voronoi diagram of the crater centres, so the rover
   drives down the middle of the gaps between them.
3. **Rocks** are never placed within half the path width of a corridor. Their
   sizes follow the lunar size-frequency law (most rocks small, big boulders
   rare).

Planning runs on a grid capped at 512 px and is scaled back to metres.

**Exact craters.** For a map generated in the studio, **Crater Hazards → Exact,
from the generator** (chosen automatically when `<map>_craters.csv` is next to
the DEM) uses the generator's own crater list instead of detecting craters. The
craters at least *Min crater depth* deep are impassable and are the corridors'
sites; every crater's bowl, up to its rim, is kept free of rocks. Detection can
miss craters: on a 1000 × 1000 map with 400 craters it found 186, and the plan
put 409 rocks inside real crater rims, against 0 with the exact list.

## Presets

On the Plan tab, from `config/rocks/*.yaml` (**Save as…** adds your own). Each
file explains its values.

| Preset | Coverage | Rock radius | Rocks per km² | Time to place in Unreal |
|---|---|---|---|---|
| `lunar_mare` (default) | 0.6% | 0.2–2.0 m | ~11,000 | ~3 min |
| `lunar_highlands` | 1.5% | 0.2–2.5 m | ~27,000 | ~7 min |
| `blocky_ejecta` | 4% | 0.2–3.0 m | ~69,000 | ~17 min |
| `quick_test` | 0.1% | 0.3–2.0 m | ~900 | seconds |

The mare coverage follows LRO Diviner's rock abundance: about 0.4–0.5% of the
Moon's surface is covered by rocks of about 1 m or larger (Bandfield et al.,
2011), scaled to the smaller rocks counted here. The minimum radius, 0.2 m
(0.14 m tall), matches the GNC planner's 0.15 m obstacle height.

Each preset also sets `BOULDER_MIN_RADIUS_M`, the object-mask split when placing:
rocks at least this radius are `boulder` (id 9), smaller ones `small_rock` (id
10). It is 0.5 m (rocks of 1 m and up, the size Diviner counts), which tags
about 10% of a lunar_mare field as boulders. It is the **Boulder if radius ≥**
field on the Export & Unreal tab; changing it does not require a new export.

## Export

Loading a DEM names the export after it, so the files land beside that map:

| File | Contents |
| --- | --- |
| `<name>_rocks.csv` | One row per rock: `id, row, col, x_m, y_m, scale, height_m` |
| `<name>_roverPaths.csv` | Corridor polylines: `path_id, x_m, y_m` |
| `<name>_obstacleMap.csv` | Optional 0/1 rock-footprint raster at full DEM resolution |

`x_m` and `y_m` are metres east and north of the map corner (no Z: take it from
the DEM). `scale` is the uniform mesh scale, i.e. the radius in metres of a 1 m
base mesh; `height_m` is `scale × ROCK_HEIGHT_RATIO`.

Each export also saves the settings it was made with
(`<name>_rocks_settings.json`). A line above the buttons says whether the file
still matches the controls, or lists what changed.

## Place in Unreal

**Place Rocks in Unreal** puts the exported rocks on the Landscape and tags each
one for the rover's object mask (`navcam/central/object_mask`) as a boulder or a
small rock (ids 9 and 10 by default, split at `BOULDER_MIN_RADIUS_M`). If the
export is out of date, or missing, the studio offers **Export, then Place**.
**Update Mask Ids Only** re-tags rocks already placed without moving them.

The rock models are the Poly Haven glTF rocks in `input_rocks/` (or any folder
of `.gltf`/`.glb`/`.obj`/`.fbx` models); the studio measures them and gives
each rock one of the models closest to its size, so they are scaled little.

A field is placed as one actor, `<name>_rocks`, with one instanced static mesh
component per rock model and mask id, instead of one actor per rock. Every view
in Unreal (the editor and each rover camera and sensor) handles actors one by
one, so ten thousand rock actors limited the frame rate: instancing a 10,338-rock
field raised Play from 14 to 20 frames/s with every camera streaming. Instances
keep their own position, rotation, scale and collision. Rocks placed with
physics simulation stay one actor each. A field placed as individual actors can
be converted in place, keeping every rock where it is:
`send_rocks.send_instance_to_unreal("<name>")` from `Terrain_Studio/unreal_link`.

"""
Unreal Engine Rock Placer
-------------------------
Executes natively within the Unreal Engine Python environment. Imports rock
meshes from a folder and places the rock field planned by the studio
(<name>_rocks.csv) on the Landscape.

Positions follow the Landscape spawned by unreal_terrain_generator. Its
heightmap is mirrored in X, so at the default spawn position a rock at
(x_m, y_m) in the DEM frame sits at Unreal X = -x_m * 100, Y = y_m * 100.
When the Landscape is found in the level, positions are taken from its actual
transform instead, so a moved or rescaled Landscape keeps its rocks aligned.
Heights come from a line trace onto the surface, so the heightmap must be
imported into the Landscape before placing rocks.

Only the standard library is used: Unreal's embedded Python has no numpy.
"""

import csv
import os
import random

import unreal

from rock_models import (GLTF_EXTENSIONS, MESH_EXTENSIONS,  # noqa: F401
                         find_model_files, fitting_models)
from unreal_level_saver import save_current_level

ROCK_ASSET_FOLDER = "/Game/LunarRocks"
ROCK_ACTOR_FOLDER = "LunarRocks"
TEXTURE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".tga", ".bmp")

CM_PER_METER = 100.0
TRACE_REACH_CM = 1.0e7      # traces run from +100 km to -100 km
SINK_FRACTION = 0.2         # rocks are buried by this fraction of their height
PHYSICS_MASS_KG = 500.0

# Custom Depth-Stencil ids for the GNC simulator's object mask; they must match
# mask_labels in GNC-Moon-Simulator/component_stereo_depth/config/params.yaml.
BOULDER_STENCIL = 20
SMALL_ROCK_STENCIL = 21
BOULDER_MIN_RADIUS_M = 0.15


# =========================================================================
# ROCK MODELS
# =========================================================================

def _asset_name(text):
    """A valid asset/folder name: letters, digits and underscores only."""
    return "".join(c if c.isalnum() or c == "_" else "_" for c in text)


def _run_import(source_path, destination_path, destination_name=None, options=None):
    task = unreal.AssetImportTask()
    task.set_editor_property("automated", True)
    task.set_editor_property("save", True)
    task.set_editor_property("replace_existing", True)
    task.set_editor_property("filename", source_path)
    task.set_editor_property("destination_path", destination_path)
    if destination_name:
        task.set_editor_property("destination_name", destination_name)
    if options is not None:
        task.set_editor_property("options", options)
    try:
        task.set_editor_property("async_", False)   # Interchange imports are async by default
    except Exception:
        pass
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    # Read the result from the task: the asset registry only lists new assets
    # on a later tick, so listing the folder now would find nothing.
    try:
        return [str(p) for p in task.get_editor_property("imported_object_paths")]
    except Exception:
        return []


def _assets_in(folder, cls):
    """Loaded assets of class `cls` under `folder`."""
    if not unreal.EditorAssetLibrary.does_directory_exist(folder):
        return []
    found = []
    for path in unreal.EditorAssetLibrary.list_assets(folder, recursive=True,
                                                      include_folder=False):
        asset = unreal.load_asset(path.split(".")[0])
        if isinstance(asset, cls):
            found.append(asset)
    return found


def _usable(meshes):
    """
    Meshes with real geometry. An import can leave an empty static mesh (zero
    bounds); scaling one to a planned footprint would blow it up to an absurd
    size, so empty meshes are dropped. Mesh-description APIs are not used to
    check this: on an empty mesh they crash the editor.
    """
    usable = []
    for mesh in meshes:
        extent = mesh.get_bounds().box_extent
        if max(extent.x, extent.y, extent.z) > 1e-2:
            usable.append(mesh)
    return usable


def _pick_lod0(meshes):
    """
    The mesh to place when a file holds one mesh per LOD: the one named LOD0,
    otherwise the one with the most vertices.
    """
    if len(meshes) < 2:
        return meshes[0] if meshes else None
    named = [m for m in meshes if "lod0" in m.get_name().lower()]
    if named:
        return named[0]
    subsystem = unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)
    try:
        return max(meshes, key=lambda mesh: subsystem.get_number_verts(mesh, 0))
    except Exception:
        return meshes[0]


def simple_shape_count(mesh):
    """
    Simple collision shapes on `mesh`, read from its aggregate geometry.
    StaticMeshEditorSubsystem.get_simple_collision_count does not count the
    convex hulls added by convex decomposition in UE 5.7.
    """
    body = mesh.get_editor_property("body_setup")
    if body is None:
        return 0
    geom = body.get_editor_property("agg_geom")
    return sum(len(geom.get_editor_property(kind))
               for kind in ("convex_elems", "box_elems", "sphere_elems", "sphyl_elems"))


def ensure_simple_collision(mesh):
    """
    Gives `mesh` a convex-hull simple collision if it has none.

    Chaos physics (the rover's body and wheels) collides with simple collision;
    glTF imports come without any, so the rocks would only block traces.
    Returns True when collision was added.
    """
    if simple_shape_count(mesh) > 0:
        return False
    subsystem = unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)
    try:
        subsystem.set_convex_decomposition_collisions(mesh, 1, 32, 100000)
    except Exception as exc:
        unreal.log_warning(f"[-] Convex hull failed for {mesh.get_name()}: {exc}")
    if simple_shape_count(mesh) == 0:
        subsystem.add_simple_collisions(mesh, unreal.ScriptCollisionShapeType.NDOP26)
    unreal.EditorAssetLibrary.save_loaded_asset(mesh)
    return True


def _loaded(paths, cls):
    assets = (unreal.load_asset(path.split(".")[0]) for path in paths)
    return [a for a in assets if isinstance(a, cls)]


def _mesh_import_options(is_obj):
    """Static-mesh import settings for .obj/.fbx: Nanite and simple collision."""
    mesh_data = unreal.FbxStaticMeshImportData()
    mesh_data.set_editor_property("build_nanite", True)
    mesh_data.set_editor_property("auto_generate_collision", True)

    options = unreal.FbxImportUI()
    options.set_editor_property("import_mesh", True)
    options.set_editor_property("import_textures", False)
    options.set_editor_property("import_materials", False)
    options.set_editor_property("import_as_skeletal", False)
    options.set_editor_property("static_mesh_import_data", mesh_data)
    options.set_editor_property("is_obj_import", is_obj)
    return options


def _material_from_texture(texture, folder, material_name):
    """Material with the texture as base colour and a rough, non-metallic surface."""
    material_path = f"{folder}/{material_name}"
    if unreal.EditorAssetLibrary.does_asset_exist(material_path):
        return unreal.load_asset(material_path)

    tools = unreal.AssetToolsHelpers.get_asset_tools()
    material = tools.create_asset(material_name, folder, unreal.Material,
                                  unreal.MaterialFactoryNew())
    library = unreal.MaterialEditingLibrary

    sample = library.create_material_expression(
        material, unreal.MaterialExpressionTextureSample, -500, 0)
    sample.set_editor_property("texture", texture)
    library.connect_material_property(sample, "RGB", unreal.MaterialProperty.MP_BASE_COLOR)

    for value, prop, y in ((0.0, unreal.MaterialProperty.MP_METALLIC, 250),
                           (0.1, unreal.MaterialProperty.MP_SPECULAR, 350),
                           (0.9, unreal.MaterialProperty.MP_ROUGHNESS, 450)):
        constant = library.create_material_expression(
            material, unreal.MaterialExpressionConstant, -300, y)
        constant.set_editor_property("r", value)
        library.connect_material_property(constant, "", prop)

    material.set_editor_property("cast_ray_traced_shadows", True)
    library.recompile_material(material)
    unreal.EditorAssetLibrary.save_asset(material.get_path_name())
    return material


def _matching_texture(mesh_path):
    """
    Texture beside an .obj/.fbx: same stem first, then one whose stem starts
    with the mesh stem (rock_01 -> rock_01_albedo), then a lone texture.
    """
    folder, stem = os.path.dirname(mesh_path), os.path.splitext(os.path.basename(mesh_path))[0]
    textures = sorted(f for f in os.listdir(folder) if f.lower().endswith(TEXTURE_EXTENSIONS))
    for candidate in textures:
        if os.path.splitext(candidate)[0].lower() == stem.lower():
            return os.path.join(folder, candidate)
    for candidate in textures:
        if os.path.splitext(candidate)[0].lower().startswith(stem.lower()):
            return os.path.join(folder, candidate)
    return os.path.join(folder, textures[0]) if len(textures) == 1 else None


def _import_model(model_path):
    """
    Imports one rock model into its own folder under ROCK_ASSET_FOLDER and
    returns its static mesh. glTF brings its own materials; .obj/.fbx get the
    matching texture. Models already imported are reused.
    """
    stem = os.path.splitext(os.path.basename(model_path))[0]
    folder = f"{ROCK_ASSET_FOLDER}/{_asset_name(stem)}"

    # Reuse a previous import only if it has geometry; an empty one is redone.
    meshes = _usable(_assets_in(folder, unreal.StaticMesh))
    if meshes:
        return _pick_lod0(meshes)

    if model_path.lower().endswith(GLTF_EXTENSIONS):
        meshes = _usable(_loaded(_run_import(model_path, folder), unreal.StaticMesh))
    else:
        meshes = _usable(_loaded(
            _run_import(model_path, folder, f"SM_{_asset_name(stem)}",
                        _mesh_import_options(model_path.lower().endswith(".obj"))),
            unreal.StaticMesh))
        texture_path = _matching_texture(model_path)
        if texture_path and meshes:
            textures = _loaded(_run_import(texture_path, folder, f"T_{_asset_name(stem)}"),
                               unreal.Texture)
            if textures:
                meshes[0].set_material(0, _material_from_texture(
                    textures[0], folder, f"M_{_asset_name(stem)}"))
    return _pick_lod0(meshes)


def import_rock_models(models_folder):
    """
    Imports the rock models in `models_folder` and returns
    [(mesh, footprint_radius_cm, bottom_z_cm, height_cm)].
    """
    paths = find_model_files(models_folder)
    if not paths:
        raise ValueError(f"No .obj, .fbx, .gltf or .glb rock models found in {models_folder}")

    models = []
    for path in paths:
        mesh = _import_model(path)
        if mesh is None:
            unreal.log_warning(f"[-] Could not import {os.path.basename(path)}; skipped.")
            continue
        if ensure_simple_collision(mesh):
            unreal.log(f"[*] Added convex collision to {mesh.get_name()}.")
        bounds = mesh.get_bounds()
        extent, origin = bounds.box_extent, bounds.origin
        radius_cm = max(extent.x, extent.y, 1e-3)
        models.append((mesh, radius_cm, origin.z - extent.z, 2.0 * extent.z))
        unreal.log(f"[*] Rock model {os.path.basename(path)} -> {mesh.get_name()}: "
                   f"footprint radius {radius_cm:.1f} cm")
    if not models:
        raise ValueError("None of the rock models could be imported.")
    return models


# =========================================================================
# LANDSCAPE FRAME
# =========================================================================

def _level_actors():
    subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    return subsystem.get_all_level_actors()


def find_landscape(label):
    """The Landscape labelled `label`, or the only Landscape in the level."""
    landscapes = [a for a in _level_actors() if isinstance(a, unreal.LandscapeProxy)]
    for actor in landscapes:
        if label and actor.get_actor_label() == label:
            return actor
    if len(landscapes) == 1:
        if label:
            unreal.log_warning(f"[-] No Landscape labelled '{label}'; using the only "
                               f"Landscape, '{landscapes[0].get_actor_label()}'.")
        return landscapes[0]
    return None


def landscape_frame(landscape, landscape_width, resolution):
    """
    (x0, y0, step_x, step_y): world position in cm of DEM cell (0, 0) and the
    world step per DEM cell. DEM columns run towards -X (mirrored heightmap).
    """
    if landscape is None:
        step = resolution * CM_PER_METER
        return 0.0, 0.0, -step, step

    location = landscape.get_actor_location()
    scale = landscape.get_actor_scale3d()
    if landscape_width > 1:
        max_x = location.x + (landscape_width - 1) * scale.x
    else:
        origin, extent = landscape.get_actor_bounds(False)
        max_x = origin.x + extent.x
    return max_x, location.y, -scale.x, scale.y


def rock_world_xy(x_m, y_m, resolution, frame):
    """World (X, Y) in cm of a rock at (x_m, y_m) metres in the DEM frame."""
    x0, y0, step_x, step_y = frame
    return x0 + (x_m / resolution) * step_x, y0 + (y_m / resolution) * step_y


def _surface_z(world, x, y):
    """Height of the first surface below (x, y), or None if the trace misses."""
    hit = unreal.SystemLibrary.line_trace_single(
        world, unreal.Vector(x, y, TRACE_REACH_CM), unreal.Vector(x, y, -TRACE_REACH_CM),
        unreal.TraceTypeQuery.TRACE_TYPE_QUERY1, True, [],
        unreal.DrawDebugTrace.NONE, True)
    if isinstance(hit, tuple):          # (hit_found, HitResult) on some versions
        if not hit or not hit[0]:
            return None
        hit = hit[-1]
    if hit is None:
        return None
    fields = hit.to_tuple()             # (blocking_hit, initial_overlap, time,
    return fields[4].z if fields[0] else None   #  distance, location, ...)


# =========================================================================
# PLACEMENT
# =========================================================================

def _read_rocks(rocks_csv):
    with open(rocks_csv, newline="") as handle:
        return [{k: float(v) for k, v in row.items()} for row in csv.DictReader(handle)]


def rock_stencil(radius_m, boulder_stencil=BOULDER_STENCIL,
                 small_rock_stencil=SMALL_ROCK_STENCIL,
                 boulder_min_radius_m=BOULDER_MIN_RADIUS_M):
    """Stencil id for a rock of footprint radius `radius_m`."""
    return boulder_stencil if radius_m >= boulder_min_radius_m else small_rock_stencil


def _apply_stencil(actor, stencil_id):
    component = actor.static_mesh_component
    component.set_editor_property("render_custom_depth", True)
    component.set_editor_property("custom_depth_stencil_value", int(stencil_id))


def set_rock_stencils(group, boulder_stencil=BOULDER_STENCIL,
                      small_rock_stencil=SMALL_ROCK_STENCIL,
                      boulder_min_radius_m=BOULDER_MIN_RADIUS_M, save_level=False):
    """
    Tags already placed rocks of `group` in the Custom Depth-Stencil buffer,
    sizing each from its actor scale, then saves the level if `save_level`.
    Returns {stencil_id: count}.
    """
    tag = f"LunarRocks_{group}"
    radius_cm = {}
    counts = {}
    for actor in _level_actors():
        if tag not in [str(t) for t in actor.tags]:
            continue
        mesh = actor.static_mesh_component.static_mesh
        if mesh.get_name() not in radius_cm:
            extent = mesh.get_bounds().box_extent
            radius_cm[mesh.get_name()] = max(extent.x, extent.y)
        radius_m = actor.get_actor_scale3d().x * radius_cm[mesh.get_name()] / CM_PER_METER
        stencil_id = rock_stencil(radius_m, boulder_stencil, small_rock_stencil,
                                  boulder_min_radius_m)
        _apply_stencil(actor, stencil_id)
        counts[stencil_id] = counts.get(stencil_id, 0) + 1
    if save_level and counts:
        unreal.log(f"[*] {save_current_level()}")
    return counts


def clear_rocks(group):
    """Deletes the rocks a previous run placed for `group`."""
    tag = f"LunarRocks_{group}"
    old = [a for a in _level_actors() if tag in [str(t) for t in a.tags]]
    if old:
        unreal.get_editor_subsystem(unreal.EditorActorSubsystem).destroy_actors(old)
    return len(old)


def place_rocks(rocks_csv, models_folder, resolution=1.0, landscape_label="",
                landscape_width=0, seed=1, simulate_physics=False,
                boulder_stencil=BOULDER_STENCIL, small_rock_stencil=SMALL_ROCK_STENCIL,
                boulder_min_radius_m=BOULDER_MIN_RADIUS_M, save_level=False):
    """
    Places every rock in `rocks_csv` on the Landscape. Rocks from a previous
    run of the same rock field are removed first. With `save_level`, the
    imported rock assets and the level are saved afterwards. Returns a
    summary line.
    """
    if not resolution > 0:
        raise ValueError(f"Resolution must be positive, got {resolution}.")
    # Models imported during Play In Editor come out as empty meshes, and actors
    # spawned then belong to the play session and vanish when it stops.
    level_editor = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    if level_editor is not None and level_editor.is_in_play_in_editor():
        raise RuntimeError("Unreal is in Play In Editor: stop playing, then place the rocks.")
    if unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world() is None:
        raise RuntimeError("No level is open in the Unreal Editor.")

    group = os.path.basename(rocks_csv).replace("_rocks.csv", "")
    rocks = _read_rocks(rocks_csv)
    models = import_rock_models(models_folder)

    landscape = find_landscape(landscape_label)
    frame = landscape_frame(landscape, int(landscape_width), resolution)
    if landscape is None:
        unreal.log_warning("[-] No Landscape found; using the default spawn position.")
    removed = clear_rocks(group)

    world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    fallback_z = landscape.get_actor_location().z if landscape else 0.0
    actors = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    mobility = (unreal.ComponentMobility.MOVABLE if simulate_physics
                else unreal.ComponentMobility.STATIC)
    tag = unreal.Name(f"LunarRocks_{group}")

    # Trace every position before spawning, so no trace lands on a new rock.
    positions, misses = [], 0
    with unreal.ScopedSlowTask(len(rocks), "Tracing rock positions") as task:
        task.make_dialog(True)
        for rock in rocks:
            if task.should_cancel():
                return "Cancelled while tracing."
            task.enter_progress_frame(1)
            x, y = rock_world_xy(rock["x_m"], rock["y_m"], resolution, frame)
            z = _surface_z(world, x, y)
            if z is None:
                misses += 1
                z = fallback_z
            positions.append((x, y, z))

    model_radii = [radius for _mesh, radius, _bottom, _height in models]
    placed = 0
    stencil_counts = {}
    with unreal.ScopedSlowTask(len(rocks), f"Placing {len(rocks)} rocks") as task:
        task.make_dialog(True)
        for rock, (x, y, z) in zip(rocks, positions):
            if task.should_cancel():
                break
            task.enter_progress_frame(1)
            rng = random.Random(seed * 1000003 + int(rock["id"]))
            # A model close to the planned size, so it keeps roughly its
            # modelled size instead of being blown up or shrunk.
            choices = fitting_models(model_radii, rock["scale"] * CM_PER_METER)
            mesh, radius_cm, bottom_cm, height_cm = models[rng.choice(choices)]

            # Scale the mesh so its footprint matches the planned rock radius,
            # then rest its base on the surface and bury it slightly.
            factor = rock["scale"] * CM_PER_METER / radius_cm
            z_pivot = z - bottom_cm * factor - SINK_FRACTION * height_cm * factor

            actor = actors.spawn_actor_from_object(
                mesh, unreal.Vector(x, y, z_pivot),
                unreal.Rotator(0.0, 0.0, rng.uniform(0.0, 360.0)))
            if actor is None:
                continue
            actor.set_actor_scale3d(unreal.Vector(factor, factor, factor))
            actor.set_actor_label(f"{group}_rock_{int(rock['id'])}")
            actor.set_folder_path(f"{ROCK_ACTOR_FOLDER}/{group}")
            actor.tags = [tag]
            actor.set_mobility(mobility)
            stencil_id = rock_stencil(rock["scale"], boulder_stencil, small_rock_stencil,
                                      boulder_min_radius_m)
            _apply_stencil(actor, stencil_id)
            stencil_counts[stencil_id] = stencil_counts.get(stencil_id, 0) + 1
            if simulate_physics:
                component = actor.get_component_by_class(unreal.PrimitiveComponent)
                component.set_simulate_physics(True)
                component.set_mass_override_in_kg(mass_in_kg=PHYSICS_MASS_KG)
            placed += 1

    summary = (f"Placed {placed} of {len(rocks)} rocks from {len(models)} model(s)"
               f" in outliner folder {ROCK_ACTOR_FOLDER}/{group}; stencil ids "
               + ", ".join(f"{k}: {v}" for k, v in sorted(stencil_counts.items())))
    if removed:
        summary += f"; removed {removed} from the previous run"
    if misses:
        summary += (f"; {misses} found no surface below them and were set at the "
                    f"Landscape's base height (is the heightmap imported?)")
    if save_level and placed:
        # The imported meshes and their collision edits, then the level itself.
        unreal.EditorAssetLibrary.save_directory(ROCK_ASSET_FOLDER, only_if_is_dirty=True,
                                                 recursive=True)
        summary += "; " + save_current_level().rstrip(".")
    unreal.log(f"[+] {summary}.")
    return summary

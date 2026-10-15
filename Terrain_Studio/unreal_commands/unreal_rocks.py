"""
Unreal Engine Rock Placer
-------------------------
Executes natively within the Unreal Engine Python environment. Imports rock
meshes from a folder and places the rock field planned by the studio
(<name>_rocks.csv) on the Landscape.

A field is placed as ONE actor (<name>_rocks) holding one instanced static mesh
component per (rock mesh, stencil id), not one actor per rock: every renderer
view (the editor, each navcam and sensor capture) culls and submits actors one
by one, and ten thousand rock actors made that the frame-rate bottleneck.
Instances keep their own transform and collision; the stencil id is per
component. Rocks that simulate physics stay individual actors. instance_rocks()
converts a field placed as individual actors by earlier versions.

Positions follow the Landscape spawned by unreal_landscape, whose
heightmap is mirrored in X: at the default spawn position a rock at (x_m, y_m)
in the DEM frame lies at X = -x_m * 100, Y = y_m * 100 (cm). When the Landscape
is present, its actual transform is used, so moved or rescaled Landscapes stay
aligned. Heights come from line traces, so the heightmap must be applied first.

Standard library only: Unreal's embedded Python has no numpy.
"""

import csv
import os
import random

import unreal

import mask_classes
from rock_models import (GLTF_EXTENSIONS, GLTF_PLACEHOLDER_TEXTURES,  # noqa: F401
                         GLTF_TEXTURE_SLOTS, MESH_EXTENSIONS, find_model_files,
                         fitting_models, instance_groups, texture_for_slot)
from unreal_level_saver import save_current_level

ROCK_ASSET_FOLDER = "/Game/LunarRocks"
ROCK_ACTOR_FOLDER = "LunarRocks"
TEXTURE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".tga", ".bmp")

CM_PER_METER = 100.0
TRACE_REACH_CM = 1.0e7      # traces run from +100 km to -100 km
SINK_FRACTION = 0.2         # rocks are buried by this fraction of their height
PHYSICS_MASS_KG = 500.0

# Custom Depth-Stencil ids of the rocks in the object mask (config/mask_classes.json;
# the GNC simulator reads its copy of that file).
BOULDER_STENCIL = mask_classes.id_of("boulder")
SMALL_ROCK_STENCIL = mask_classes.id_of("small_rock")
BOULDER_MIN_RADIUS_M = 0.5   # as rock_field.DEFAULTS (the studio passes its setting)


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
    except Exception as exc:     # engines without Interchange have no such property
        unreal.log(f"[*] AssetImportTask has no async_ property ({exc}); importing as the engine defaults.")
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    # The asset registry lists new assets only on a later tick, so the result
    # is read from the task itself.
    try:
        return [str(p) for p in task.get_editor_property("imported_object_paths")]
    except Exception:
        return []


def _assets_in(folder, cls):
    """Returns the loaded assets of class `cls` under `folder`."""
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
    Filters out empty static meshes (zero bounds), which a failed import can
    leave behind. Bounds are used because mesh-description APIs crash the
    editor on empty meshes.
    """
    usable = []
    for mesh in meshes:
        extent = mesh.get_bounds().box_extent
        if max(extent.x, extent.y, extent.z) > 1e-2:
            usable.append(mesh)
    return usable


def _pick_lod0(meshes):
    """Selects the mesh to place from a per-LOD import: LOD0 by name, else the densest."""
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
    Counts the simple collision shapes in the mesh's aggregate geometry.
    get_simple_collision_count omits convex-decomposition hulls in UE 5.7.
    """
    body = mesh.get_editor_property("body_setup")
    if body is None:
        return 0
    geom = body.get_editor_property("agg_geom")
    return sum(len(geom.get_editor_property(kind))
               for kind in ("convex_elems", "box_elems", "sphere_elems", "sphyl_elems"))


def ensure_simple_collision(mesh):
    """
    Adds a convex-hull simple collision if the mesh has none (glTF imports lack
    it, and Chaos physics only collides with simple collision). Returns True if
    collision was added.
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
    """Returns .obj/.fbx static-mesh import options with Nanite and simple collision."""
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
    """Creates (or reuses) a rough, non-metallic material using the texture as base colour."""
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
    Finds the texture for an .obj/.fbx: same stem, then a stem prefixed by the
    mesh stem (rock_01 -> rock_01_albedo), then the folder's only texture.
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


def _repair_gltf_materials(folder):
    """
    Fills the glTF material slots that Unreal's importer left on an engine
    placeholder (a white base colour, a flat normal) with the model's own
    imported textures, matched by name. Unreal leaves them empty when one
    material is shared by several meshes, as in a file holding its LODs: the
    rock then renders plain white. Returns how many slots were filled.
    """
    textures = {t.get_name(): t for t in _assets_in(folder, unreal.Texture2D)}
    if not textures:
        return 0
    library = unreal.MaterialEditingLibrary
    filled = 0
    for material in _assets_in(folder, unreal.MaterialInstanceConstant):
        changed = False
        names = [str(n) for n in library.get_texture_parameter_names(material)]
        for slot, _endings in GLTF_TEXTURE_SLOTS:
            if slot not in names:
                continue
            current = library.get_material_instance_texture_parameter_value(material, slot)
            if current is not None and current.get_name() not in GLTF_PLACEHOLDER_TEXTURES:
                continue
            match = texture_for_slot(slot, sorted(textures))
            if match:
                library.set_material_instance_texture_parameter_value(material, slot, textures[match])
                unreal.log(f"[*] {material.get_name()}: {slot} was empty after import; set to {match}.")
                changed = True
                filled += 1
        if changed:
            library.update_material_instance(material)
            unreal.EditorAssetLibrary.save_loaded_asset(material, only_if_is_dirty=False)
    return filled


def _import_model(model_path):
    """
    Imports a rock model into its own folder under ROCK_ASSET_FOLDER and returns
    its static mesh, reusing a previous import. glTF keeps its materials;
    .obj/.fbx receive the matching texture.
    """
    stem = os.path.splitext(os.path.basename(model_path))[0]
    folder = f"{ROCK_ASSET_FOLDER}/{_asset_name(stem)}"

    # Reuse a previous import unless it is empty (repairing its glTF materials).
    meshes = _usable(_assets_in(folder, unreal.StaticMesh))
    if meshes:
        if model_path.lower().endswith(GLTF_EXTENSIONS):
            _repair_gltf_materials(folder)
        return _pick_lod0(meshes)

    if model_path.lower().endswith(GLTF_EXTENSIONS):
        meshes = _usable(_loaded(_run_import(model_path, folder), unreal.StaticMesh))
        _repair_gltf_materials(folder)
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
    """Imports all models in `models_folder`; returns [(mesh, footprint_radius_cm, bottom_z_cm, height_cm)]."""
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
    """Returns the Landscape labelled `label`, or the only Landscape in the level."""
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
    Returns (x0, y0, step_x, step_y): the world position (cm) of DEM cell (0, 0)
    and the step per cell. Columns run towards -X (mirrored heightmap).
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
    """Converts DEM-frame metres (x_m, y_m) to world (X, Y) in cm."""
    x0, y0, step_x, step_y = frame
    return x0 + (x_m / resolution) * step_x, y0 + (y_m / resolution) * step_y


def _surface_z(world, x, y):
    """Returns the height of the first surface below (x, y), or None if the trace misses."""
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
    """Returns the stencil id for a rock of footprint radius `radius_m`."""
    return boulder_stencil if radius_m >= boulder_min_radius_m else small_rock_stencil


def _apply_stencil(component, stencil_id):
    component.set_editor_property("render_custom_depth", True)
    component.set_editor_property("custom_depth_stencil_value", int(stencil_id))


# =========================================================================
# INSTANCED FIELDS
# =========================================================================

def _group_actors(group):
    tag = f"LunarRocks_{group}"
    return [a for a in _level_actors() if tag in [str(t) for t in a.tags]]


def _instanced_components(actor):
    return list(actor.get_components_by_class(unreal.InstancedStaticMeshComponent))


def _root_handle(actor):
    """The subobject handle of the actor's root component."""
    subobjects = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
    data = unreal.SubobjectDataBlueprintFunctionLibrary
    for handle in subobjects.k2_gather_subobject_data_for_instance(actor):
        if data.get_object(data.get_data(handle)) == actor.root_component:
            return handle
    raise RuntimeError(f"{actor.get_actor_label()} has no root component.")


def _add_instanced_component(actor, root, mesh, stencil_id):
    """Adds an InstancedStaticMeshComponent for `mesh` with stencil `stencil_id`."""
    subobjects = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
    data = unreal.SubobjectDataBlueprintFunctionLibrary
    handle, failure = subobjects.add_new_subobject(unreal.AddNewSubobjectParams(
        parent_handle=root, new_class=unreal.InstancedStaticMeshComponent,
        blueprint_context=None))
    if str(failure):
        raise RuntimeError(f"Could not add a rock component: {failure}")
    subobjects.rename_subobject(
        handle, unreal.Text(f"Rocks_{_asset_name(mesh.get_name())}_s{int(stencil_id)}"))
    component = data.get_object(data.get_data(handle))
    component.set_editor_property("static_mesh", mesh)
    component.set_mobility(unreal.ComponentMobility.STATIC)
    component.set_collision_profile_name("BlockAll")
    _apply_stencil(component, stencil_id)
    return component


def build_instanced_field(group, rocks):
    """
    Places `rocks` [(mesh, stencil_id, world Transform)] as one actor labelled
    <group>_rocks, tagged and foldered like the individual rocks were, with one
    InstancedStaticMeshComponent per (mesh, stencil id). Returns
    ({stencil_id: count}, number of components).
    """
    meshes = {mesh.get_path_name(): mesh for mesh, _stencil, _transform in rocks}
    groups = instance_groups((mesh.get_path_name(), stencil_id, transform)
                             for mesh, stencil_id, transform in rocks)
    actors = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    actor = actors.spawn_actor_from_class(unreal.Actor, unreal.Vector(0.0, 0.0, 0.0))
    actor.set_actor_label(f"{group}_rocks")
    actor.set_folder_path(f"{ROCK_ACTOR_FOLDER}/{group}")
    actor.tags = [unreal.Name(f"LunarRocks_{group}")]
    # Static, like the rock actors were (cached shadows); a component cannot
    # be more static than its parent, so the root first.
    actor.root_component.set_mobility(unreal.ComponentMobility.STATIC)
    root = _root_handle(actor)
    counts = {}
    for (mesh_path, stencil_id), transforms in groups:
        component = _add_instanced_component(actor, root, meshes[mesh_path], stencil_id)
        component.add_instances(transforms, False, True, False)    # world space, no navmesh rebuild
        counts[stencil_id] = counts.get(stencil_id, 0) + len(transforms)
    return counts, len(groups)


def _field_instances(actor):
    """[(mesh, stencil_id, world Transform)] of every instance of a field actor."""
    rocks = []
    for component in _instanced_components(actor):
        mesh = component.static_mesh
        stencil_id = component.get_editor_property("custom_depth_stencil_value")
        for index in range(component.get_instance_count()):
            rocks.append((mesh, stencil_id, component.get_instance_transform(index, True)))
    return rocks


def _simulates_physics(actor):
    return actor.static_mesh_component.get_editor_property("body_instance").get_editor_property(
        "simulate_physics")


def instance_rocks(group, save_level=False):
    """
    Converts the rocks of `group` placed as individual actors (by earlier
    versions) into one instanced field actor with the same meshes, transforms,
    stencil ids and collision, then deletes those actors. Rocks that simulate
    physics stay actors. Returns a summary line.
    """
    level_editor = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    if level_editor is not None and level_editor.is_in_play_in_editor():
        raise RuntimeError("Unreal is in Play In Editor: stop playing, then convert the rocks.")
    singles = [a for a in _group_actors(group)
               if isinstance(a, unreal.StaticMeshActor) and not _simulates_physics(a)]
    if not singles:
        return f"No individual rock actors in field {group}; nothing to convert."
    rocks = [(a.static_mesh_component.static_mesh,
              a.static_mesh_component.get_editor_property("custom_depth_stencil_value"),
              a.get_actor_transform()) for a in singles]
    counts, components = build_instanced_field(group, rocks)
    unreal.get_editor_subsystem(unreal.EditorActorSubsystem).destroy_actors(singles)
    summary = (f"Converted {len(singles)} rock actors of {group} into actor {group}_rocks "
               f"with {components} instanced components; stencil ids "
               + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    if save_level:
        summary += "; " + save_current_level().rstrip(".")
    unreal.log(f"[+] {summary}.")
    return summary


def set_rock_stencils(group, boulder_stencil=BOULDER_STENCIL,
                      small_rock_stencil=SMALL_ROCK_STENCIL,
                      boulder_min_radius_m=BOULDER_MIN_RADIUS_M, save_level=False):
    """
    Re-tags the placed rocks of `group` with Custom Depth-Stencil ids, sized
    from their scale, and optionally saves the level. An instanced field is
    rebuilt with its instances regrouped by the new ids. Returns {stencil_id: count}.
    """
    radius_cm = {}

    def stencil_for(mesh, scale_x):
        if mesh.get_name() not in radius_cm:
            extent = mesh.get_bounds().box_extent
            radius_cm[mesh.get_name()] = max(extent.x, extent.y)
        return rock_stencil(scale_x * radius_cm[mesh.get_name()] / CM_PER_METER,
                            boulder_stencil, small_rock_stencil, boulder_min_radius_m)

    counts, fields, regrouped = {}, [], []
    for actor in _group_actors(group):
        if _instanced_components(actor):
            fields.append(actor)
            for mesh, _old, transform in _field_instances(actor):
                regrouped.append((mesh, stencil_for(mesh, transform.scale3d.x), transform))
            continue
        if not isinstance(actor, unreal.StaticMeshActor):
            continue
        stencil_id = stencil_for(actor.static_mesh_component.static_mesh,
                                 actor.get_actor_scale3d().x)
        _apply_stencil(actor.static_mesh_component, stencil_id)
        counts[stencil_id] = counts.get(stencil_id, 0) + 1
    if fields:
        unreal.get_editor_subsystem(unreal.EditorActorSubsystem).destroy_actors(fields)
        field_counts, _components = build_instanced_field(group, regrouped)
        for stencil_id, count in field_counts.items():
            counts[stencil_id] = counts.get(stencil_id, 0) + count
    if save_level and counts:
        unreal.log(f"[*] {save_current_level()}")
    return counts


def clear_rocks(group):
    """Deletes the rocks placed by a previous run for `group`; returns how many rocks."""
    old = _group_actors(group)
    rocks = sum(sum(c.get_instance_count() for c in _instanced_components(a)) or 1 for a in old)
    if old:
        unreal.get_editor_subsystem(unreal.EditorActorSubsystem).destroy_actors(old)
    return rocks


def place_rocks(rocks_csv, models_folder, resolution=1.0, landscape_label="",
                landscape_width=0, seed=1, simulate_physics=False,
                boulder_stencil=BOULDER_STENCIL, small_rock_stencil=SMALL_ROCK_STENCIL,
                boulder_min_radius_m=BOULDER_MIN_RADIUS_M, save_level=False):
    """
    Places every rock in `rocks_csv` on the Landscape, replacing those from a
    previous run of the same field: as one instanced field actor, or as one
    actor per rock when the rocks simulate physics. With `save_level`, the rock
    assets and the level are saved afterwards. Returns a summary line.
    """
    if not resolution > 0:
        raise ValueError(f"Resolution must be positive, got {resolution}.")
    # During Play In Editor imports produce empty meshes and spawned actors
    # belong to the transient play world.
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
    tag = unreal.Name(f"LunarRocks_{group}")

    # Trace all positions before spawning so traces never hit new rocks.
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
    instanced = []              # (mesh, stencil_id, Transform) for the field actor
    with unreal.ScopedSlowTask(len(rocks), f"Placing {len(rocks)} rocks") as task:
        task.make_dialog(True)
        for rock, (x, y, z) in zip(rocks, positions):
            if task.should_cancel():
                break
            task.enter_progress_frame(1)
            rng = random.Random(seed * 1000003 + int(rock["id"]))
            # Prefer models close to the planned size to limit scaling.
            choices = fitting_models(model_radii, rock["scale"] * CM_PER_METER)
            mesh, radius_cm, bottom_cm, height_cm = models[rng.choice(choices)]

            # Scale to the planned footprint, rest on the surface, then sink slightly.
            # The planned height (height_m) is a maximum: a model taller than
            # that for its footprint is flattened to it; squatter ones keep
            # their proportions.
            factor = rock["scale"] * CM_PER_METER / radius_cm
            z_factor = factor
            if rock.get("height_m", 0.0) > 0.0 and height_cm > 0.0:
                z_factor = min(factor, rock["height_m"] * CM_PER_METER / height_cm)
            z_pivot = z - bottom_cm * z_factor - SINK_FRACTION * height_cm * z_factor
            location = unreal.Vector(x, y, z_pivot)
            rotation = unreal.Rotator(0.0, 0.0, rng.uniform(0.0, 360.0))
            scale = unreal.Vector(factor, factor, z_factor)
            stencil_id = rock_stencil(rock["scale"], boulder_stencil, small_rock_stencil,
                                      boulder_min_radius_m)

            if not simulate_physics:
                instanced.append((mesh, stencil_id, unreal.Transform(location, rotation, scale)))
                placed += 1
                continue

            # Instances cannot simulate physics: one movable actor per rock.
            actor = actors.spawn_actor_from_object(mesh, location, rotation)
            if actor is None:
                continue
            actor.set_actor_scale3d(scale)
            actor.set_actor_label(f"{group}_rock_{int(rock['id'])}")
            actor.set_folder_path(f"{ROCK_ACTOR_FOLDER}/{group}")
            actor.tags = [tag]
            actor.set_mobility(unreal.ComponentMobility.MOVABLE)
            _apply_stencil(actor.static_mesh_component, stencil_id)
            stencil_counts[stencil_id] = stencil_counts.get(stencil_id, 0) + 1
            component = actor.get_component_by_class(unreal.PrimitiveComponent)
            component.set_simulate_physics(True)
            component.set_mass_override_in_kg(mass_in_kg=PHYSICS_MASS_KG)
            placed += 1

    where = f"outliner folder {ROCK_ACTOR_FOLDER}/{group}"
    if instanced:
        stencil_counts, components = build_instanced_field(group, instanced)
        where = f"actor {group}_rocks ({components} instanced components) in {where}"
    summary = (f"Placed {placed} of {len(rocks)} rocks from {len(models)} model(s)"
               f" in {where}; stencil ids "
               + ", ".join(f"{k}: {v}" for k, v in sorted(stencil_counts.items())))
    if removed:
        summary += f"; removed {removed} from the previous run"
    if misses:
        summary += (f"; {misses} found no surface below them and were set at the "
                    f"Landscape's base height (is the heightmap imported?)")
    if save_level and placed:
        # Save the rock assets (including collision edits), then the level.
        unreal.EditorAssetLibrary.save_directory(ROCK_ASSET_FOLDER, only_if_is_dirty=True,
                                                 recursive=True)
        summary += "; " + save_current_level().rstrip(".")
    unreal.log(f"[+] {summary}.")
    return summary

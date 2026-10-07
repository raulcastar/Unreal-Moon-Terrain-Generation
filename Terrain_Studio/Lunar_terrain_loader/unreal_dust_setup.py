"""
Unreal Wheel Dust: setup (Terrain Studio)
-----------------------------------------
Executes natively within the Unreal Engine Python environment, called by the
studio when it sends masks. Readies the Niagara wheel dust: the soft sprite
material M_LunarDustSprite, and the system writing the object-mask stencil
DUST_ID. The editor watcher's unreal_wheel_dust runs the emitters during Play.

For lunar dust (no air) set the system to Gravity Force (0, 0, -162) cm/s^2, no
Drag, launch velocity backwards and up, about one second of life, and this
sprite; its Spawn Rate multiplied by User.DustAmount.
"""

import unreal

from unreal_wheel_dust import DUST_SYSTEM

GROUND_TRUTH_FOLDER = "/Game/GroundTruth_Masks"
DUST_SPRITE_MATERIAL = f"{GROUND_TRUTH_FOLDER}/M_LunarDustSprite"
OLD_DUST_LABEL = "WheelDust"             # The earlier instanced-grain version: removed
OLD_GRAIN_MATERIAL = f"{GROUND_TRUTH_FOLDER}/M_LunarDustGrain"
DUST_ID = 70
REGOLITH_COLOUR = unreal.LinearColor(0.13, 0.128, 0.124, 1.0)
SPRITE_STRENGTH = 0.95                   # Sprite opacity at its centre (times the particle's alpha)
SPRITE_OPACITY_CODE = r"""// Soft round puff: opaque-ish centre fading to a clear edge, times the
// particle's own alpha (Niagara colour / fade over life).
float r = length(UV - 0.5) * 2.0;
return saturate(1.0 - r * r) * Strength * Alpha;"""


def _sprite_material():
    """Soft, round, translucent regolith-coloured sprite for the dust particles."""
    lib = unreal.MaterialEditingLibrary
    material = unreal.load_asset(DUST_SPRITE_MATERIAL)
    if material is None:
        material = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
            DUST_SPRITE_MATERIAL.rsplit("/", 1)[-1], GROUND_TRUTH_FOLDER, unreal.Material,
            unreal.MaterialFactoryNew())
    lib.delete_all_material_expressions(material)
    material.set_editor_property("blend_mode", unreal.BlendMode.BLEND_TRANSLUCENT)
    material.set_editor_property("translucency_lighting_mode",
                                 unreal.TranslucencyLightingMode.TLM_SURFACE_PER_PIXEL_LIGHTING)   # Lit by the sun directly
    material.set_editor_property("allow_translucent_custom_depth_writes", True)   # Object mask
    material.set_editor_property("used_with_niagara_sprites", True)

    particle = lib.create_material_expression(material, unreal.MaterialExpressionParticleColor, -700, 0)
    colour = lib.create_material_expression(material, unreal.MaterialExpressionConstant3Vector, -700, -120)
    colour.set_editor_property("constant", REGOLITH_COLOUR)
    tint = lib.create_material_expression(material, unreal.MaterialExpressionMultiply, -400, -80)
    lib.connect_material_expressions(colour, "", tint, "A")
    lib.connect_material_expressions(particle, "RGB", tint, "B")
    lib.connect_material_property(tint, "", unreal.MaterialProperty.MP_BASE_COLOR)

    uv = lib.create_material_expression(material, unreal.MaterialExpressionTextureCoordinate, -700, 200)
    strength = lib.create_material_expression(material, unreal.MaterialExpressionScalarParameter, -700, 280)
    strength.set_editor_property("parameter_name", "Strength")
    strength.set_editor_property("default_value", SPRITE_STRENGTH)
    opacity = lib.create_material_expression(material, unreal.MaterialExpressionCustom, -400, 200)
    opacity.set_editor_property("code", SPRITE_OPACITY_CODE)
    opacity.set_editor_property("output_type", unreal.CustomMaterialOutputType.CMOT_FLOAT1)
    entries = []
    for name in ("UV", "Strength", "Alpha"):
        entry = unreal.CustomInput()
        entry.set_editor_property("input_name", name)
        entries.append(entry)
    opacity.set_editor_property("inputs", entries)
    lib.connect_material_expressions(uv, "", opacity, "UV")
    lib.connect_material_expressions(strength, "", opacity, "Strength")
    lib.connect_material_expressions(particle, "A", opacity, "Alpha")
    lib.connect_material_property(opacity, "", unreal.MaterialProperty.MP_OPACITY)
    lib.recompile_material(material)
    unreal.EditorAssetLibrary.save_loaded_asset(material)
    return material


def remove_old_dust(world=None):
    """Removes the earlier instanced-grain dust (actor and material). Returns how many actors."""
    world = world or unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    actors = [a for a in unreal.GameplayStatics.get_all_actors_of_class(world, unreal.Actor)
              if a.get_actor_label().endswith("_" + OLD_DUST_LABEL)]
    for actor in actors:
        actor.destroy_actor()
    if unreal.EditorAssetLibrary.does_asset_exist(OLD_GRAIN_MATERIAL):
        unreal.EditorAssetLibrary.delete_asset(OLD_GRAIN_MATERIAL)
    return len(actors)


def remove_wheel_dust(world=None):
    """Nothing to remove from the level any more (emitters live only in the game); clears the old version."""
    return remove_old_dust(world)


def setup_wheel_dust(rover):
    """
    Readies the dust for `rover`'s wheels: the sprite material, and the system
    writing the dust stencil. Editor only. Returns (ready, summary line).
    """
    remove_old_dust()
    system = unreal.load_asset(DUST_SYSTEM)
    if system is None:
        return False, f"Wheel dust: off, {DUST_SYSTEM} not found."
    _sprite_material()
    system.set_editor_property("render_custom_depth", True)
    system.set_editor_property("custom_depth_stencil_value", DUST_ID)
    unreal.EditorAssetLibrary.save_loaded_asset(system)
    return True, (f"Wheel dust: {DUST_SYSTEM.rsplit('/', 1)[-1]} at each wheel of "
                  f"{rover.get_actor_label()} while it rolls (dust is id {DUST_ID} in the object "
                  f"mask); sprite material {DUST_SPRITE_MATERIAL}.")

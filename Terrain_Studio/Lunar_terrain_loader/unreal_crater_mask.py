"""
Unreal Crater Ground Truth
--------------------------
Executes natively within the Unreal Engine Python environment. Imports the
studio's crater map (<name>_craterMask.png, cell values = mask ids) and builds
the object-mask material so landscape pixels take the crater map's id:

    stencil == landscape id  and  crater map cell > 0   ->   that cell's id
    otherwise                                          ->   the stencil id

The material keeps the original colouring (a stable colour hashed from each
id), so rocks, the rover and bare ground are unchanged. The crater map is read
with an exact texel fetch at the pixel's world position, using the same DEM
frame as the rock placer (columns run towards -X); it may hold several cells
per DEM cell (CraterSupersample).
"""

import unreal

from unreal_rock_placer import find_landscape, landscape_frame

MASK_MATERIAL = "/Game/Blueprints/PPM_ObjectMask"
BACKUP_MATERIAL = "/Game/Blueprints/PPM_ObjectMask_Original"
TEXTURE_FOLDER = "/Game/GroundTruth"
LANDSCAPE_STENCIL = 10

# Custom node: crater lookup on landscape pixels, then the original id colouring.
MASK_CODE = r"""float v = In;                                         // Stencil value 0..255
if (abs(v - LandscapeStencil) < 0.5)
{
    // Landscape pixel: the crater map's id at this point, if any. DEM cell c
    // spans map cells [c * S, (c + 1) * S), centred on vertex c.
    float2 dem = float2((Pos.x - CraterOriginX) / CraterStepX,
                        (Pos.y - CraterOriginY) / CraterStepY);
    int2 cell = int2(floor((dem + 0.5) * CraterSupersample));
    if (cell.x >= 0 && cell.y >= 0 && cell.x < CraterWidth && cell.y < CraterHeight)
    {
        float id = round(Crater.Load(int3(cell, 0)).r * 255.0);
        if (id > 0.5) v = id;
    }
}
if (v < 0.5) return float3(0, 0, 0);                  // Untagged pixels are black
float3 c;
c.r = frac(sin(v * 12.9898) * 43758.5453);
c.g = frac(sin(v * 78.2330) * 43758.5453);
c.b = frac(sin(v * 37.7190) * 43758.5453);
return c;                                             // Stable distinct colour per id"""

SCALAR_INPUTS = ("LandscapeStencil", "CraterOriginX", "CraterOriginY", "CraterStepX",
                 "CraterStepY", "CraterWidth", "CraterHeight", "CraterSupersample")


def import_mask_texture(png_path, name):
    """Imports the crater map as an exact (uncompressed, unfiltered, no mips) texture."""
    task = unreal.AssetImportTask()
    task.set_editor_property("filename", png_path)
    task.set_editor_property("destination_path", TEXTURE_FOLDER)
    task.set_editor_property("destination_name", name)
    task.set_editor_property("replace_existing", True)
    task.set_editor_property("automated", True)
    task.set_editor_property("save", False)
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])

    texture = unreal.load_asset(f"{TEXTURE_FOLDER}/{name}")
    if texture is None:
        raise RuntimeError(f"Could not import {png_path}")
    for prop, value in (
            ("compression_settings", unreal.TextureCompressionSettings.TC_GRAYSCALE),
            ("srgb", False),
            ("filter", unreal.TextureFilter.TF_NEAREST),
            ("mip_gen_settings", unreal.TextureMipGenSettings.TMGS_NO_MIPMAPS),
            ("never_stream", True),
            ("address_x", unreal.TextureAddress.TA_CLAMP),
            ("address_y", unreal.TextureAddress.TA_CLAMP)):
        texture.set_editor_property(prop, value)
    unreal.EditorAssetLibrary.save_loaded_asset(texture)
    return texture


def _backup_material():
    if not unreal.EditorAssetLibrary.does_asset_exist(BACKUP_MATERIAL):
        unreal.EditorAssetLibrary.duplicate_asset(MASK_MATERIAL, BACKUP_MATERIAL)


def _build_mask_graph(material, texture, values):
    """Rebuilds the mask material: stencil + world position + crater map -> colour."""
    lib = unreal.MaterialEditingLibrary
    lib.delete_all_material_expressions(material)

    stencil = lib.create_material_expression(material, unreal.MaterialExpressionSceneTexture, -900, 0)
    stencil.set_editor_property("scene_texture_id", unreal.SceneTextureId.PPI_CUSTOM_STENCIL)
    red = lib.create_material_expression(material, unreal.MaterialExpressionComponentMask, -650, 0)
    for channel, keep in (("r", True), ("g", False), ("b", False), ("a", False)):
        red.set_editor_property(channel, keep)
    lib.connect_material_expressions(stencil, "Color", red, "")

    position = lib.create_material_expression(material, unreal.MaterialExpressionWorldPosition, -650, 120)
    crater = lib.create_material_expression(
        material, unreal.MaterialExpressionTextureObjectParameter, -650, 220)
    crater.set_editor_property("parameter_name", "CraterMask")
    crater.set_editor_property("texture", texture)

    custom = lib.create_material_expression(material, unreal.MaterialExpressionCustom, -300, 0)
    custom.set_editor_property("code", MASK_CODE)
    custom.set_editor_property("description", "ObjectMaskColour")
    custom.set_editor_property("output_type", unreal.CustomMaterialOutputType.CMOT_FLOAT3)
    names = ("In", "Pos", "Crater") + SCALAR_INPUTS
    inputs = []
    for name in names:
        entry = unreal.CustomInput()
        entry.set_editor_property("input_name", name)
        inputs.append(entry)
    custom.set_editor_property("inputs", inputs)

    lib.connect_material_expressions(red, "", custom, "In")
    lib.connect_material_expressions(position, "", custom, "Pos")
    lib.connect_material_expressions(crater, "", custom, "Crater")
    for row, name in enumerate(SCALAR_INPUTS):
        scalar = lib.create_material_expression(
            material, unreal.MaterialExpressionScalarParameter, -650, 360 + 70 * row)
        scalar.set_editor_property("parameter_name", name)
        scalar.set_editor_property("default_value", float(values[name]))
        lib.connect_material_expressions(scalar, "", custom, name)
    lib.connect_material_property(custom, "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)

    lib.recompile_material(material)
    unreal.EditorAssetLibrary.save_loaded_asset(material)


def apply_crater_mask(png_path, width, height, resolution=1.0, landscape_label="",
                      landscape_width=0, landscape_stencil=LANDSCAPE_STENCIL, supersample=1):
    """
    Imports the crater map and makes the object mask show it on the Landscape.
    Returns a summary line.
    """
    landscape = find_landscape(landscape_label)
    if landscape is None:
        return "[-] No Landscape found to align the crater map with (is Play In Editor running?)."
    x0, y0, step_x, step_y = landscape_frame(landscape, landscape_width, resolution)

    stem = png_path.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    texture = import_mask_texture(png_path, f"T_{stem}")
    material = unreal.load_asset(MASK_MATERIAL)
    if material is None:
        return f"[-] {MASK_MATERIAL} not found: set up the object mask first (StereoCamStreamer README)."
    _backup_material()
    _build_mask_graph(material, texture, {
        "LandscapeStencil": landscape_stencil, "CraterOriginX": x0, "CraterOriginY": y0,
        "CraterStepX": step_x, "CraterStepY": step_y,
        "CraterWidth": width, "CraterHeight": height, "CraterSupersample": supersample})
    return (f"Crater ground truth on '{landscape.get_actor_label()}': {texture.get_path_name()} "
            f"({width} x {height}, {supersample} per DEM cell) in {MASK_MATERIAL}. "
            f"Backup: {BACKUP_MATERIAL}.")


def remove_crater_mask():
    """Restores the object-mask material saved before the crater map was added."""
    if not unreal.EditorAssetLibrary.does_asset_exist(BACKUP_MATERIAL):
        return "Nothing to restore: no backup of the object-mask material."
    unreal.EditorAssetLibrary.delete_asset(MASK_MATERIAL)
    unreal.EditorAssetLibrary.rename_asset(BACKUP_MATERIAL, MASK_MATERIAL)
    return f"Restored the original {MASK_MATERIAL}."

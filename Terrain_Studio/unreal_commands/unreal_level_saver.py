"""
Unreal Level Saver
------------------
Runs inside the Unreal Editor. Saves the open
level after the studio modifies it (Landscape spawn, rock placement).
"""

import unreal


def save_current_level():
    """Saves the open level without any dialog. Returns a status line for the log."""
    level_editor = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    if level_editor.is_in_play_in_editor():
        return "Level not saved: Play In Editor is running."
    world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    if world is None:
        return "Level not saved: no level is open."

    package = world.get_outermost().get_path_name()
    # Untitled levels live under /Temp/ and would require a Save As dialog.
    if package.startswith("/Temp/"):
        return ("Level not saved: it is untitled. Save it once with File > Save "
                "Current Level As; later runs then save it automatically.")
    if not level_editor.save_current_level():
        return f"Could not save level {package}; save it from the editor."

    # World Partition levels store actors in external packages.
    leftover = unreal.EditorLoadingAndSavingUtils.get_dirty_map_packages()
    if leftover:
        unreal.EditorLoadingAndSavingUtils.save_packages(leftover, True)
    return f"Saved level {package}."

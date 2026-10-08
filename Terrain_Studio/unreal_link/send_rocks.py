"""
Rock Field Dispatch
-------------------
Sends a planned rock field (<name>_rocks.csv) to a running Unreal Editor over
Remote Execution, where unreal_rocks imports the rock models and places
the rocks on the Landscape.
"""

from unreal_connection import run_in_unreal, unreal_call


def _placer_call(call):
    """Unreal-side Python that runs `call` on unreal_rocks."""
    return unreal_call(call, "unreal_level_saver", "unreal_rocks")


def build_command(rocks_csv, models_folder, resolution, landscape_label,
                  landscape_width, seed, simulate_physics,
                  boulder_stencil=20, small_rock_stencil=21, boulder_min_radius_m=0.15,
                  save_level=True):
    """Builds the placement command; paths are quoted with repr()."""
    args = ", ".join([
        repr(rocks_csv.replace("\\", "/")),
        repr(models_folder.replace("\\", "/")),
        f"resolution={float(resolution)!r}",
        f"landscape_label={landscape_label!r}",
        f"landscape_width={int(landscape_width)}",
        f"seed={int(seed)}",
        f"simulate_physics={bool(simulate_physics)}",
        f"boulder_stencil={int(boulder_stencil)}",
        f"small_rock_stencil={int(small_rock_stencil)}",
        f"boulder_min_radius_m={float(boulder_min_radius_m)!r}",
        f"save_level={bool(save_level)}",
    ])
    return _placer_call(f"place_rocks({args})")


def build_stencil_command(group, boulder_stencil, small_rock_stencil, boulder_min_radius_m,
                          save_level=True):
    """Builds the command that re-tags placed rocks without moving them."""
    args = ", ".join([repr(group), f"boulder_stencil={int(boulder_stencil)}",
                      f"small_rock_stencil={int(small_rock_stencil)}",
                      f"boulder_min_radius_m={float(boulder_min_radius_m)!r}",
                      f"save_level={bool(save_level)}"])
    return _placer_call(f"set_rock_stencils({args})")


def send_rocks_to_unreal(rocks_csv, models_folder, resolution, landscape_label="",
                         landscape_width=0, seed=1, simulate_physics=False,
                         boulder_stencil=20, small_rock_stencil=21,
                         boulder_min_radius_m=0.15, save_level=True, log=print,
                         discovery_seconds=2.0):
    """
    Places the rock field in a running Unreal Editor and optionally saves the
    level. Returns True on success.
    """
    command = build_command(rocks_csv, models_folder, resolution, landscape_label,
                            landscape_width, seed, simulate_physics,
                            boulder_stencil, small_rock_stencil, boulder_min_radius_m,
                            save_level)
    return run_in_unreal(command, log, "Placing rocks in Unreal; this can take a while",
                         discovery_seconds)


def send_stencils_to_unreal(group, boulder_stencil=20, small_rock_stencil=21,
                            boulder_min_radius_m=0.15, save_level=True, log=print,
                            discovery_seconds=2.0):
    """
    Re-tags the placed rocks of `group` with new stencil ids and optionally
    saves the level. Returns True on success.
    """
    command = build_stencil_command(group, boulder_stencil, small_rock_stencil,
                                    boulder_min_radius_m, save_level)
    return run_in_unreal(command, log, "Updating rock stencils in Unreal",
                         discovery_seconds)

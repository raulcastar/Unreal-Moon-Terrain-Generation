"""
The studio's back end: the generator modules (generator/) and the senders
that talk to Unreal (unreal_link/). The senders are optional: without their
dependencies the studio still generates and previews terrain.
"""

from gui import paths  # noqa: F401  (puts generator/ and unreal_link/ on the import path)

import terrain_core  # noqa: E402
import rock_field    # noqa: E402
import sun_shadows   # noqa: E402
import crater_map    # noqa: E402

try:
    import heightmap_files
    import send_terrain
    import send_rocks
    import rock_models
    import send_sun
    import send_masks
    LOADER_AVAILABLE = True
    LOADER_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - environment dependent
    heightmap_files = send_terrain = send_rocks = rock_models = send_sun = send_masks = None
    LOADER_AVAILABLE = False
    LOADER_IMPORT_ERROR = exc

__all__ = ["terrain_core", "rock_field", "sun_shadows", "crater_map", "heightmap_files",
           "send_terrain", "send_rocks", "rock_models", "send_sun", "send_masks",
           "LOADER_AVAILABLE", "LOADER_IMPORT_ERROR", "paths"]

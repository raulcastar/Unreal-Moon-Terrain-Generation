"""Ground Truth logic (gui.ground_truth_logic) and drawing (gui.ground_truth_plots)."""

import json
import types

import numpy as np
import pytest
from matplotlib.figure import Figure

import mask_classes
from gui import ground_truth_logic as logic
from gui import ground_truth_plots as plots

IDS = dict(logic.DEFAULT_MASK_IDS)


def test_mask_ids_default_and_shared():
    assert logic.mask_ids(types.SimpleNamespace()) == logic.DEFAULT_MASK_IDS
    shared = dict(IDS, boulder=20)
    assert logic.mask_ids(types.SimpleNamespace(mask_ids=shared))["boulder"] == 20


@pytest.mark.parametrize("crater, rim, ok", [
    ("6", "7", True), ("6", "0", True), ("0", "7", False), ("6", "6", False),
    ("6", "256", False), ("x", "7", False)])
def test_parse_crater_ids(crater, rim, ok):
    ids = logic.parse_crater_ids({"boulder": 30}, crater, rim)
    assert (ids is not None) == ok
    if ok:
        assert ids["crater"] == int(crater) and ids["boulder"] == 30


def test_effective_classes_follow_the_chosen_ids():
    ids = dict(IDS, crater=40, rim=41, boulder=42, small_rock=43)
    classes = {c["name"]: c["id"] for c in logic.effective_classes(ids)}
    assert (classes["crater"], classes["crater_rim"], classes["boulder"], classes["small_rock"]) == \
        (40, 41, 42, 43)
    ordered = [c["id"] for c in logic.effective_classes(ids)]
    assert ordered == sorted(ordered)
    no_rim = {c["name"] for c in logic.effective_classes(dict(ids, rim=0))}
    assert "crater_rim" not in no_rim
    one_rock = {c["name"] for c in logic.effective_classes(dict(ids, small_rock=42))}
    assert "small_rock" not in one_rock and "boulder" in one_rock


def test_class_lines_and_gnc_status():
    line = logic.class_lines([{"id": 9, "name": "boulder", "group": "rock", "rgb": [238, 244, 242]}])[0]
    assert line.startswith("  9  boulder") and "rgb(238, 244, 242)" in line
    assert "not found" in logic.gnc_status(None)
    assert logic.gnc_status("") == "GNC copy matches."
    assert logic.gnc_status("9: x").endswith("9: x")


def test_classes_document(tmp_path):
    path = tmp_path / "classes.json"
    path.write_text(json.dumps({"about": ["x"], "classes": []}))
    text = logic.classes_document(str(path), [{"id": 1, "name": "sky"}])
    assert json.loads(text) == {"about": ["x"], "classes": [{"id": 1, "name": "sky"}]}
    assert text.endswith("\n")
    real = logic.classes_document(mask_classes.CLASSES_FILE, mask_classes.classes())
    assert json.loads(real)["classes"] == mask_classes.classes()


def test_shadow_lines_and_glare():
    assert logic.shadow_id_lines(False, True) == ["Shadow feed off."]
    on = logic.shadow_id_lines(True, True)
    assert len(on) == 6 and on[5].startswith("5  rover shadow")
    off = logic.shadow_id_lines(True, False)
    assert off[-1] == "(5 rover shadow: off)" and len(off) == 6
    assert logic.parse_glare_deg("2.5") == 2.5
    assert logic.parse_glare_deg("-1") == 0.0 and logic.parse_glare_deg("") == 0.0


def test_crater_list_and_source(tmp_path):
    dem = tmp_path / "map_a_elevationMap.npy"
    assert logic.crater_list_for(str(dem), "map_a") is None
    (tmp_path / "map_a_craters.csv").write_text("id,row,col,radius\n")
    assert logic.crater_list_for(str(dem), "map_a").endswith("map_a_craters.csv")
    assert logic.crater_source_info(True, True, "map_a_craters.csv") == \
        "Generated here: using map_a_craters.csv."
    assert "detecting anyway" in logic.crater_source_info(True, False)
    assert "No generator crater list" in logic.crater_source_info(False, False)
    assert logic.crater_summary(12, 3.456, False) == (
        "12 craters (detected), 3.5% of the map.", "12 craters detected.")


def test_crater_mask_meta(tmp_path):
    png, meta = logic.load_crater_mask_meta(str(tmp_path), "map_a")
    assert png.endswith("map_a_craterMask.png") and meta is None
    (tmp_path / "map_a_craterMask.json").write_text(json.dumps({"width": 8}))
    assert logic.load_crater_mask_meta(str(tmp_path), "map_a")[1] == {"width": 8}


def test_track_style_and_preview():
    style = logic.track_style("Chevron", "4", "1.234", "0.5")
    assert style == {"pattern": "chevron", "period_cm": 4.0, "relief": 1.23, "shade": 0.5}
    assert logic.track_style("unknown", "", 1, 1)["pattern"] == "straight"
    assert logic.track_style("unknown", "", 1, 1)["period_cm"] == 3.0
    assert logic.track_style_info("Chevron", style) == "Chevron, lugs every 4 cm, relief 1.2, shade 0.50"
    smooth = logic.track_preview_shapes(dict(style, pattern="smooth"), 180)
    assert len(smooth) == 1 and smooth[0][0] == "rect"
    chevrons = logic.track_preview_shapes(style, 180)
    assert len(chevrons) > 3 and all(kind == "polygon" for kind, _c, _col in chevrons[1:])
    blocks = logic.track_preview_shapes(dict(style, pattern="blocks"), 180)
    assert (len(blocks) - 1) % 2 == 0                       # two staggered blocks per period


def test_cameras():
    assert logic.parse_camera(" Leo_Raph0 / Navcam_Mask ") == ("Leo_Raph0", "Navcam_Mask")
    assert logic.parse_camera("nothing") == ("", "")
    found = {"actors": [{"actor": "Leo_Raph0", "captures": ["Navcam_Mask", "RearCam"],
                         "streamer_mask": "Navcam_Mask"}], "pie": True}
    options, streamed, info = logic.camera_choices(found)
    assert options == ["Leo_Raph0 / Navcam_Mask", "Leo_Raph0 / RearCam"]
    assert streamed == "Leo_Raph0 / Navcam_Mask" and "Play In Editor" in info
    assert logic.camera_choices({"actors": []})[1] is None
    # Any rover's mask capture is the default, not one rover's label
    assert logic.default_camera(["Yutu_Rover0 / Rear_Mask", "Yutu_Rover0 / Navcam_Mask"]) == "Yutu_Rover0 / Navcam_Mask"
    assert logic.default_camera(["Probe / Capture"]) is None


def test_crater_mask_drawing():
    dem = np.random.default_rng(0).normal(size=(40, 50)).cumsum(axis=0)
    labels = np.zeros((40, 50), np.uint8)
    labels[10:20, 10:20] = IDS["crater"]
    labels[8:10, 10:20] = IDS["rim"]
    figure = Figure()
    plots.draw_crater_mask(figure, dem, 1.0, labels, IDS, True, 1)
    figure.canvas.draw()
    assert figure.axes[0].get_title() == "Crater mask  -  1 craters"
    assert len(figure.axes[0].get_legend().get_texts()) == 2
    plots.draw_crater_mask(figure, dem, 1.0)
    assert figure.axes[0].get_title() == "Crater mask"

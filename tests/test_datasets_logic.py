"""
Lunar datasets (gui.datasets_logic) and remote GeoTIFF reading (gui.remote_tiff).

No test reaches the internet: a local HTTP server with range support stands in
for the NASA servers, and input_maps/ is redirected to a temporary folder.
"""

import http.server
import json
import os
import re
import threading

import numpy as np
import pytest

from gui import datasets_logic as data
from gui import paths
from gui.remote_tiff import Cancelled, RemoteTiff, fetch_range, remote_size


@pytest.fixture(autouse=True)
def input_maps(tmp_path, monkeypatch):
    """input_maps/ redirected, so downloads and caches never land in the real one."""
    folder = tmp_path / "input_maps"
    folder.mkdir()
    monkeypatch.setattr(paths, "INPUT_MAPS_DIR", str(folder))
    return folder


class _RangeHandler(http.server.SimpleHTTPRequestHandler):
    """Static files with HTTP Range support (SimpleHTTPRequestHandler has none)."""

    def log_message(self, *_args):
        pass

    def _send(self, body):
        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            self.send_error(404)
            return
        size = os.path.getsize(path)
        start, end = 0, size - 1
        match = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
        if match:
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else size - 1
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        if body:
            with open(path, "rb") as handle:
                handle.seek(start)
                self.wfile.write(handle.read(end - start + 1))

    def do_GET(self):
        self._send(True)

    def do_HEAD(self):
        self._send(False)


@pytest.fixture
def server(tmp_path):
    """(folder, base URL) of a local range-capable file server."""
    folder = tmp_path / "served"
    folder.mkdir()
    handler = lambda *a, **k: _RangeHandler(*a, directory=str(folder), **k)  # noqa: E731
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield folder, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


# ------------------------------------------------------------ catalogue
def test_catalogue_has_every_source_and_complete_entries():
    sources, entries, fetched = data.load_catalog()
    assert set(data.SOURCE_ORDER) <= set(sources)
    assert fetched is None and not any(e["source"] == "lroc_nac" for e in entries)  # nothing cached yet
    for entry in entries:
        assert entry["url"].startswith("https://") and entry["filename"] in entry["url"]
        assert entry["resolution_m"] > 0 and entry["size_bytes"] > 0
    assert sum(e["source"] == "lola_sites" for e in entries) == 27
    assert all(e["tiled"] for e in entries if e["source"] == "lola_mosaics")


def test_lroc_entries_from_ode_and_their_links():
    payload = {"ODEResults": {"Products": {"Product": [
        {"pdsid": "sdp.nac_dtm.apollo15.nac_dtm_apollo15"},
        {"pdsid": "sdp.nac_dtm.a12lmas.nac_dtm_a12lmas"},
        {"pdsid": "sdp.nac_pho.something.else"}]}}}
    entries = data.lroc_entries_from_ode(payload)
    assert [e["name"] for e in entries] == ["A12LMAS", "APOLLO15"]
    apollo = entries[1]
    assert apollo["url"].endswith("/DATA/SDP/NAC_DTM/APOLLO15/NAC_DTM_APOLLO15.TIF")
    assert apollo["preview_url"].endswith("/NAC_DTM_APOLLO15_SHADE.BROWSE.PNG")
    assert apollo["readme_url"].endswith("/NAC_DTM_APOLLO15_README.TXT")
    # A single product comes back as a dict, not a list.
    single = {"ODEResults": {"Products": {"Product": {"pdsid": "sdp.nac_dtm.x1.nac_dtm_x1"}}}}
    assert [e["name"] for e in data.lroc_entries_from_ode(single)] == ["X1"]


README = """PROJECT NAME
  Apollo 15 Landing Site

- Post spacing in DTM is 2.0 m.
- DTM extents:
   Min. Lat.: 25.5921918
   Min. Lon.: 3.50268023
   Max. Lat.: 26.53608486
   Max. Lon.: 3.69012653

KEY FEATURES
- Anthropogenic
- Mare
- Rille

REFERENCE
"""


def test_parse_lroc_readme():
    details = data.parse_lroc_readme(README)
    assert details["title"] == "Apollo 15 Landing Site"
    assert details["resolution_m"] == 2.0
    assert details["extent"]["max_lat"] == pytest.approx(26.53608486)
    assert details["features"] == ["Anthropogenic", "Mare", "Rille"]
    assert details["region"].startswith("26.06°")


def test_filter_entries():
    _sources, entries, _ = data.load_catalog()
    shackleton = data.filter_entries(entries, None, "shackleton rim")
    assert [e["name"] for e in shackleton] == ["Site04"]
    assert len(data.filter_entries(entries, "lola_mosaics")) == 8
    assert data.filter_entries(entries, "lola_sites", "no such place") == []


def test_human_size():
    assert data.human_size(None) == "?"
    assert data.human_size(950) == "950 B"
    assert data.human_size(64_033_681) == "64.0 MB"
    assert data.human_size(5_116_709_568) == "5.1 GB"


# ------------------------------------------------------------- network
def _entry(base, name, size, **extra):
    return dict({"id": f"test_{name}", "source": "lola_sites", "name": name, "title": name,
                 "url": f"{base}/{name}", "filename": name, "size_bytes": size,
                 "resolution_m": 5.0, "tiled": False}, **extra)


def test_download_resumes_a_partial_file(server):
    folder, base = server
    payload = os.urandom(3_500_000)
    (folder / "site.tif").write_bytes(payload)
    entry = _entry(base, "site.tif", len(payload))

    # A previous attempt stopped part-way.
    target = data.local_path(entry)
    os.makedirs(os.path.dirname(target))
    with open(target + ".part", "wb") as handle:
        handle.write(payload[:1_200_000])

    seen = []
    path = data.download(entry, progress=lambda done, total: seen.append(done))
    assert open(path, "rb").read() == payload
    assert seen[0] > 1_200_000                      # it continued, it did not restart
    assert not os.path.exists(target + ".part")
    assert data.is_downloaded(entry)
    assert path.startswith(str(paths.INPUT_MAPS_DIR))


def test_download_can_be_stopped_and_keeps_the_part(server):
    folder, base = server
    (folder / "big.tif").write_bytes(os.urandom(4_000_000))
    entry = _entry(base, "big.tif", 4_000_000)
    calls = []
    with pytest.raises(Cancelled):
        data.download(entry, progress=lambda d, t: calls.append(d), cancelled=lambda: len(calls) >= 1)
    assert not data.is_downloaded(entry)
    assert os.path.getsize(data.local_path(entry) + ".part") > 0


def test_lroc_details_are_read_once_and_cached(server):
    folder, base = server
    (folder / "README.TXT").write_text(README)
    (folder / "DTM.TIF").write_bytes(b"x" * 1234)
    entry = {"id": "lroc_apollo15", "source": "lroc_nac", "name": "APOLLO15", "title": "APOLLO15",
             "url": f"{base}/DTM.TIF", "readme_url": f"{base}/README.TXT", "filename": "DTM.TIF"}
    details = data.entry_details(entry)
    assert details["title"] == "Apollo 15 Landing Site" and details["size_bytes"] == 1234
    os.remove(folder / "README.TXT")                 # the cache now answers
    assert data.entry_details(entry)["resolution_m"] == 2.0


tifffile = pytest.importorskip("tifffile")
pytest.importorskip("imagecodecs")


def _tiled_geotiff(path, data_array, scale=10.0):
    """A small cloud-optimised-style GeoTIFF: tiled, deflate + float predictor, pixel scale."""
    tifffile.imwrite(path, data_array, tile=(64, 64), compression="zlib", predictor=True,
                     extratags=[(33550, "d", 3, (scale, scale, 0.0), True)])


def test_remote_tiff_reads_windows_by_tiles(server):
    folder, base = server
    rows, cols = 300, 420
    dem = (np.add.outer(np.arange(rows), np.arange(cols) * 0.5)).astype(np.float32)
    _tiled_geotiff(folder / "mosaic.tif", dem)

    with RemoteTiff(f"{base}/mosaic.tif") as tiff:
        assert tiff.shape == (rows, cols) and tiff.tiled and tiff.resolution == 10.0
        for bounds in [(0, 0, 64, 64), (70, 33, 150, 200), (236, 356, 64, 64), (0, 0, rows, cols)]:
            row, col, r, c = bounds
            np.testing.assert_array_equal(tiff.read_window(bounds), dem[row:row + r, col:col + c])
        preview, level = tiff.read_preview(max_edge=100)
        assert level == 0 and preview.shape == (rows, cols)


def test_remote_crop_saves_a_crop_map(server):
    folder, base = server
    dem = np.random.default_rng(0).normal(1000, 50, (256, 320)).astype(np.float32)
    _tiled_geotiff(folder / "LDEM_TEST.TIF", dem, scale=20.0)
    entry = _entry(base, "LDEM_TEST.TIF", os.path.getsize(folder / "LDEM_TEST.TIF"),
                   source="lola_mosaics", tiled=True, resolution_m=20.0)
    fractions = []
    dem_path, info = data.remote_crop(entry, (10, 20, 127, 190), progress=fractions.append)
    np.testing.assert_array_equal(np.load(dem_path), dem[10:137, 20:210])
    assert info["source"] == entry["url"] and info["resolution_m_per_px"] == 20.0
    assert os.path.basename(dem_path) == "LDEM_TEST_crop_x20_y10_190x127.npy"
    assert dem_path.startswith(str(paths.OUTPUT_ROOT)) and fractions[-1] == 1.0


def test_range_helpers(server):
    folder, base = server
    (folder / "f.bin").write_bytes(bytes(range(256)) * 10)
    assert remote_size(f"{base}/f.bin") == 2560
    assert fetch_range(f"{base}/f.bin", 250, 260) == bytes(range(250, 256)) + bytes(range(0, 5))

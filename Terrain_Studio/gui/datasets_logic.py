"""
Public lunar DEM datasets: the catalogue, details, previews, downloads and
remote crops behind the Lunar Datasets view (no Tk).

Three sources (config/lunar_datasets.json):

    lroc_nac      LROC NAC stereo DTMs, 1-5 m/px, ~670 sites. Listed live from
                  the PDS Orbital Data Explorer (one ~0.5 MB query, cached);
                  each has a shaded-relief browse PNG and a README.
    lola_sites    LOLA 5 m/px south-pole landing sites, fixed list.
    lola_mosaics  LOLA south-pole mosaics, 10-240 m/px, fixed list. These are
                  cloud-optimised GeoTIFFs (up to 5 GB): previewed from an
                  overview and cropped remotely (remote_tiff).

Downloads go to input_maps/<source>/, so they also appear in the studio's map
lists; small cached files (catalogue, details, previews) go to
input_maps/.dataset_cache/. Paths are read at call time, so tests can redirect
them.
"""

import datetime
import json
import os
import re
import urllib.parse
import urllib.request

import numpy as np

from gui import crop_logic, paths
from gui.remote_tiff import (USER_AGENT, Cancelled, RemoteTiff, http_request,
                             remote_size)


SOURCE_ORDER = ("lroc_nac", "lola_sites", "lola_mosaics")


# PDS Orbital Data Explorer REST API: product search for the LROC NAC DTMs
# (product type SDNDTM). results=p returns product names only, not their file
# lists, which keeps the answer to ~0.5 MB; the limit is well above the ~670
# products, so one request lists them all.
ODE_URL = "https://oderest.rsl.wustl.edu/live2/"
ODE_LROC_QUERY = {"query": "product", "results": "p", "output": "JSON", "target": "moon",
                  "ihid": "LRO", "iid": "LROC", "pt": "SDNDTM", "limit": "2000"}


# Where the LROC RDR volume is served. Every NAC DTM follows one layout under
# it, so the product list alone gives the file, preview and README links.
LROC_BASE = ("https://pds.mcp.nasa.gov/data/store/img/lunar_reconnaissance_orbiter/pds4/"
             "lroc/lro-l-lroc-5-rdr/LROLRC_2001")


# A NAC DTM's product id, e.g. sdp.nac_dtm.apollo15.nac_dtm_apollo15; the
# group is the site folder name.
_LROC_PDSID = re.compile(r"^sdp\.nac_dtm\.([a-z0-9_]+)\.nac_dtm_\1$")


# Bytes read per step of a download: large enough to keep the transfer fast,
# small enough for progress and Stop to react within a moment.
DOWNLOAD_CHUNK = 1 << 20


# ------------------------------------------------------------- folders
def catalog_file():
    """The catalogue of sources and fixed entries."""
    return os.path.join(paths.CONFIG_DIR, "lunar_datasets.json")


def cache_dir():
    """Where the NAC DTM list, details and previews are kept between sessions."""
    return os.path.join(paths.INPUT_MAPS_DIR, ".dataset_cache")


def local_path(entry):
    """Where `entry` is (or will be) downloaded."""
    return os.path.join(paths.INPUT_MAPS_DIR, entry["source"], entry["filename"])


def is_downloaded(entry):
    """True when the file is complete on disk (its size matches, when known)."""
    path = local_path(entry)
    if not os.path.isfile(path):
        return False
    size = entry.get("size_bytes")
    return not size or os.path.getsize(path) == size


def map_shape(path):
    """
    (rows, cols) of a DEM file read from its header alone, or None.

    Deciding whether a downloaded map needs cropping must not load hundreds of
    megabytes, and a GeoTIFF or .npy header gives the shape directly.
    """
    try:
        if path.lower().endswith((".tif", ".tiff")):
            import tifffile
            with tifffile.TiffFile(path) as tif:
                return tuple(tif.pages[0].shape[:2])
        if path.lower().endswith(".npy"):
            return tuple(np.load(path, mmap_mode="r").shape[:2])
    except Exception:
        return None
    return None


def human_size(size):
    """`size` bytes as a short readable string, or '?' when unknown."""
    if not size:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1000 or unit == "GB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1000.0


# ----------------------------------------------------------- catalogue
def load_sources():
    """The source descriptions and fixed entries from config/lunar_datasets.json."""
    with open(catalog_file(), encoding="utf-8") as handle:
        return json.load(handle)["sources"]


def lroc_entry(site):
    """A catalogue entry for the NAC DTM of `site` (its upper-case folder name)."""
    folder = f"{LROC_BASE}/DATA/SDP/NAC_DTM/{site}"
    return {
        "id": f"lroc_{site.lower()}", "source": "lroc_nac", "name": site,
        "title": site, "region": None, "resolution_m": None, "size_bytes": None,
        "url": f"{folder}/NAC_DTM_{site}.TIF", "filename": f"NAC_DTM_{site}.TIF",
        "preview_url": f"{LROC_BASE}/EXTRAS/BROWSE/NAC_DTM/{site}/NAC_DTM_{site}_SHADE.BROWSE.PNG",
        "readme_url": f"{folder}/NAC_DTM_{site}_README.TXT",
        "tiled": False}


def lroc_entries_from_ode(payload):
    """NAC DTM entries from an ODE product-search answer, sorted by site name."""
    products = (payload.get("ODEResults", {}).get("Products") or {}).get("Product", [])
    if isinstance(products, dict):
        products = [products]
    sites = set()
    for product in products:
        match = _LROC_PDSID.match(product.get("pdsid", ""))
        if match:
            sites.add(match.group(1).upper())
    return [lroc_entry(site) for site in sorted(sites)]


def fetch_lroc_entries(timeout=120):
    """Lists the NAC DTMs from ODE (network) and caches the list; returns the entries."""
    url = ODE_URL + "?" + urllib.parse.urlencode(ODE_LROC_QUERY)
    payload = json.loads(http_request(url, timeout=timeout).decode("utf-8"))
    entries = lroc_entries_from_ode(payload)
    if not entries:
        raise OSError("The PDS search answered with no NAC DTMs; try again later.")
    os.makedirs(cache_dir(), exist_ok=True)
    with open(os.path.join(cache_dir(), "lroc_catalog.json"), "w", encoding="utf-8") as handle:
        json.dump({"fetched": datetime.datetime.now().isoformat(timespec="seconds"),
                   "sites": [entry["name"] for entry in entries]}, handle)
    return entries


def cached_lroc_entries():
    """(entries, fetched date) from the cached NAC DTM list, or ([], None)."""
    try:
        with open(os.path.join(cache_dir(), "lroc_catalog.json"), encoding="utf-8") as handle:
            cached = json.load(handle)
    except (OSError, ValueError):
        return [], None
    return [lroc_entry(site) for site in cached.get("sites", [])], cached.get("fetched")


def load_catalog():
    """
    (sources, entries, lroc_fetched): the fixed entries plus the cached NAC
    DTM list, each entry tagged with its source.
    """
    sources = load_sources()
    lroc_entries, lroc_fetched = cached_lroc_entries()
    entries = []
    for key in SOURCE_ORDER:
        source = sources.get(key, {})
        if source.get("live"):
            entries += lroc_entries        # listed from the PDS, not from the file
        else:
            entries += [dict(entry, source=key) for entry in source.get("entries", [])]
    return sources, entries, lroc_fetched


def filter_entries(entries, source=None, text=""):
    """The entries of `source` (all when None) whose name, title or region contain `text`."""
    words = text.lower().split()
    found = []
    for entry in entries:
        if source and entry["source"] != source:
            continue
        haystack = " ".join(str(entry.get(key) or "") for key in ("name", "title", "region")).lower()
        if all(word in haystack for word in words):
            found.append(entry)
    return found


# ------------------------------------------------------------- details
def parse_lroc_readme(text):
    """
    What a NAC DTM README says about the map: its project name, post spacing
    (m/px), latitude/longitude extent and key features.
    """
    details = {}
    title = re.search(r"PROJECT NAME\s*\n\s*(.+)", text)
    if title:
        details["title"] = title.group(1).strip()
    spacing = re.search(r"Post spacing in DTM is\s*([\d.]+)\s*m", text, re.IGNORECASE)
    if spacing:
        details["resolution_m"] = float(spacing.group(1))
    extent = {}
    for key, label in (("min_lat", "Min. Lat."), ("min_lon", "Min. Lon."),
                       ("max_lat", "Max. Lat."), ("max_lon", "Max. Lon.")):
        match = re.search(re.escape(label) + r":\s*(-?[\d.]+)", text)
        if match:
            extent[key] = float(match.group(1))
    if len(extent) == 4:
        details["extent"] = extent
        details["region"] = (f"{(extent['min_lat'] + extent['max_lat']) / 2:.2f}°, "
                             f"{(extent['min_lon'] + extent['max_lon']) / 2:.2f}°E")
    features = re.search(r"KEY FEATURES\s*\n((?:\s*-\s*.+\n?)+)", text)
    if features:
        details["features"] = [line.strip()[1:].strip()
                               for line in features.group(1).splitlines() if line.strip()]
    return details


def entry_details(entry, refresh=False, timeout=30):
    """
    `entry` with what the servers add: for a NAC DTM, its README (title,
    resolution, extent, features) and file size, cached after the first look.
    Other entries already carry everything and are returned unchanged.
    """
    if entry["source"] != "lroc_nac":
        return dict(entry)
    cache = os.path.join(cache_dir(), f"{entry['id']}.json")
    if not refresh:
        try:
            with open(cache, encoding="utf-8") as handle:
                return dict(entry, **json.load(handle))
        except (OSError, ValueError):
            pass
    readme = http_request(entry["readme_url"], timeout=timeout).decode("utf-8", "replace")
    details = parse_lroc_readme(readme)
    details["size_bytes"] = remote_size(entry["url"], timeout=timeout)
    os.makedirs(cache_dir(), exist_ok=True)
    with open(cache, "w", encoding="utf-8") as handle:
        json.dump(details, handle)
    return dict(entry, **details)


def preview_image(entry, timeout=60):
    """
    The path of a ready-made preview picture for `entry` (downloaded once and
    cached), or None when the source has none.
    """
    url = entry.get("preview_url")
    if not url:
        return None
    path = os.path.join(cache_dir(), f"{entry['id']}_preview.png")
    if not os.path.isfile(path):
        data = http_request(url, timeout=timeout)
        os.makedirs(cache_dir(), exist_ok=True)
        with open(path + ".part", "wb") as handle:
            handle.write(data)
        os.replace(path + ".part", path)
    return path


def remote_preview(entry, progress=None, cancelled=None):
    """
    (overview array, full-resolution shape, resolution m/px) of a tiled
    entry, read from the server without downloading the file.
    """
    with RemoteTiff(entry["url"], entry.get("size_bytes")) as tiff:
        array, _level = tiff.read_preview(progress=progress, cancelled=cancelled)
        return array, tiff.shape, tiff.resolution or entry.get("resolution_m")


def remote_crop(entry, bounds, progress=None, cancelled=None):
    """
    Fetches the `bounds` window (full-resolution pixels) of a tiled entry and
    saves it as a crop map; returns (DEM path, info) as crop_logic.write_crop.
    """
    with RemoteTiff(entry["url"], entry.get("size_bytes")) as tiff:
        window = tiff.read_window(bounds, 0, progress, cancelled)
        resolution = tiff.resolution or entry.get("resolution_m")
    return crop_logic.write_crop(entry["filename"], bounds, window, resolution,
                                 source=entry["url"])


# ------------------------------------------------------------ download
def download(entry, progress=None, cancelled=None, timeout=60):
    """
    Downloads `entry` to input_maps/<source>/ and returns the path.

    The file is written as <name>.part and renamed when complete, so a half
    file is never taken for a map. A stopped or failed download resumes from
    the .part next time, when the server supports ranges (both NASA servers do).
    `progress(done_bytes, total_bytes)` is called as it goes.
    """
    path = local_path(entry)
    if is_downloaded(entry):
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    part = path + ".part"
    done = os.path.getsize(part) if os.path.isfile(part) else 0
    total = entry.get("size_bytes") or remote_size(entry["url"], timeout=timeout)
    if total and done > total:
        os.remove(part)
        done = 0

    headers = {"User-Agent": USER_AGENT}
    if done:
        headers["Range"] = f"bytes={done}-"
    request = urllib.request.Request(entry["url"], headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        # 200 instead of 206: the server ignored the range, so start over.
        mode = "ab" if done and response.status == 206 else "wb"
        if mode == "wb":
            done = 0
        with open(part, mode) as handle:
            while True:
                if cancelled and cancelled():
                    raise Cancelled()
                chunk = response.read(DOWNLOAD_CHUNK)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    if total and done != total:
        raise OSError(f"The download stopped at {human_size(done)} of {human_size(total)}; "
                      f"run it again to resume.")
    os.replace(part, path)
    return path

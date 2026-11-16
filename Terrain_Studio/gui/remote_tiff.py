"""
Reading parts of a large GeoTIFF over HTTP without downloading it (no Tk).

Some lunar mosaics are gigabytes (LOLA's 10 m/px south-pole DEM is 5.1 GB),
far more than one Unreal Landscape needs. When they are Cloud-Optimized
GeoTIFFs (tiled, with reduced-resolution overviews), the studio can fetch a
preview from the smallest overview and, for the region the user draws, only
the tiles under it, with HTTP range requests.

The TIFF structure is parsed by tifffile through a seekable HTTP file; tile
bytes are then fetched directly (adjacent tiles merged into one request, a few
requests at a time) and decoded with the page's own codec.
"""

import io
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import numpy as np


# A generic client name: no user or project details go to the servers.
USER_AGENT = "LunarTerrainStudio/1.0"


class Cancelled(Exception):
    """The user stopped a transfer."""


def http_request(url, headers=None, method="GET", timeout=60, retries=5):
    """
    The response body of `url` as bytes (its headers for a HEAD request),
    retrying dropped connections and server errors.

    The NASA servers sometimes reset a connection mid-transfer or answer 503
    under load, so a few retries with a growing pause turn a failed preview
    into a slow one.
    """
    headers = dict(headers or {})
    headers.setdefault("User-Agent", USER_AGENT)
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers=headers, method=method)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read() if method != "HEAD" else response.headers
        except urllib.error.HTTPError as error:
            # A client error (404, 403) is a real answer: retrying will not help.
            if error.code < 500 or attempt == retries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))
        except OSError:
            if attempt == retries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))


def remote_size(url, timeout=30):
    """The size of the file at `url` in bytes, or None when the server does not say."""
    headers = http_request(url, method="HEAD", timeout=timeout)
    length = headers.get("Content-Length")
    return int(length) if length and length.isdigit() else None


def fetch_range(url, start, end, timeout=60):
    """Bytes start..end (inclusive) of `url`."""
    data = http_request(url, headers={"Range": f"bytes={start}-{end}"}, timeout=timeout)
    if len(data) != end - start + 1:
        raise OSError(f"The server sent {len(data)} bytes for a {end - start + 1}-byte range; "
                      f"it may not support partial downloads.")
    return data


class HttpRangeFile(io.RawIOBase):
    """
    A read-only, seekable file over HTTP range requests, for tifffile to parse
    the TIFF structure. Reads are served from 256 KB blocks, cached, so the
    many small reads of a TIFF header cost one or two requests.
    """

    BLOCK = 1 << 18

    def __init__(self, url, size=None):
        super().__init__()
        self.url = url
        self.size = size if size is not None else remote_size(url)
        if not self.size:
            raise OSError(f"Cannot read the size of {url}.")
        self.position = 0
        self._blocks = {}

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=io.SEEK_SET):
        if whence == io.SEEK_SET:
            self.position = offset
        elif whence == io.SEEK_CUR:
            self.position += offset
        else:
            self.position = self.size + offset
        return self.position

    def _block(self, index):
        if index not in self._blocks:
            start = index * self.BLOCK
            end = min(start + self.BLOCK, self.size) - 1
            self._blocks[index] = fetch_range(self.url, start, end)
        return self._blocks[index]

    def read(self, size=-1):
        if size is None or size < 0:
            size = self.size - self.position
        size = max(0, min(size, self.size - self.position))
        out = bytearray()
        while size > 0:
            index, offset = divmod(self.position, self.BLOCK)
            chunk = self._block(index)[offset:offset + size]
            out += chunk
            self.position += len(chunk)
            size -= len(chunk)
        return bytes(out)

    def readinto(self, buffer):
        data = self.read(len(buffer))
        buffer[:len(data)] = data
        return len(data)


class RemoteTiff:
    """
    A GeoTIFF on a web server: its levels, pixel scale and NoData, a preview
    from an overview, and any window of any level, fetched tile by tile.
    """

    # Two tiles further apart than this are fetched in separate requests;
    # closer ones share a request, since one more round trip costs more than
    # the bytes in between.
    MERGE_GAP = 64 * 1024

    PARALLEL_REQUESTS = 4

    def __init__(self, url, size=None):
        import tifffile
        self.url = url
        self._file = HttpRangeFile(url, size)
        self._tif = tifffile.TiffFile(self._file)
        series = self._tif.series[0]
        self._levels = [level.keyframe for level in series.levels]
        page = self._levels[0]
        self.shape = tuple(page.shape[:2])
        self.tiled = bool(page.is_tiled)
        tags = page.tags
        scale = tags["ModelPixelScaleTag"].value if "ModelPixelScaleTag" in tags else None
        self.resolution = float(scale[0]) if scale and scale[0] > 0 else None
        self.nodata = None
        if "GDAL_NODATA" in tags:
            try:
                self.nodata = float(str(tags["GDAL_NODATA"].value).strip().strip("\x00"))
            except ValueError:
                pass

    @property
    def level_shapes(self):
        return [tuple(page.shape[:2]) for page in self._levels]

    def close(self):
        self._tif.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def preview_level(self, max_edge=1200):
        """The coarsest level still at least `max_edge` px on its long side (or the finest)."""
        best = 0
        for index, shape in enumerate(self.level_shapes):
            if max(shape) >= max_edge:
                best = index
        return best

    def read_preview(self, max_edge=1200, progress=None, cancelled=None):
        """(array, level index) of a whole overview, for the map preview."""
        level = self.preview_level(max_edge)
        rows, cols = self.level_shapes[level]
        return self.read_window((0, 0, rows, cols), level, progress, cancelled), level

    def read_window(self, bounds, level=0, progress=None, cancelled=None):
        """
        The (row, col, rows, cols) window of `level` as float32, NoData as NaN,
        fetching only the tiles that overlap it.
        """
        if not self.tiled:
            raise ValueError("This GeoTIFF is not tiled: download it whole instead.")
        page = self._levels[level]
        row, col, rows, cols = (int(v) for v in bounds)
        tile_rows, tile_cols = page.tilelength, page.tilewidth
        tiles_across = -(-page.imagewidth // tile_cols)
        first_ty, last_ty = row // tile_rows, (row + rows - 1) // tile_rows
        first_tx, last_tx = col // tile_cols, (col + cols - 1) // tile_cols

        # One request per run of tiles that sit next to each other in the file.
        runs = []
        for ty in range(first_ty, last_ty + 1):
            run = []
            for tx in range(first_tx, last_tx + 1):
                index = ty * tiles_across + tx
                offset, count = page.dataoffsets[index], page.databytecounts[index]
                if count == 0:
                    continue           # an empty tile: left as NoData
                # Tiles stored out of order, or far apart, start a new request.
                gap = offset - (run[-1][1] + run[-1][2]) if run else 0
                if run and (gap < 0 or gap > self.MERGE_GAP):
                    runs.append(run)
                    run = []
                run.append((index, offset, count, ty, tx))
            if run:
                runs.append(run)

        out = np.full((rows, cols), np.nan, dtype=np.float32)
        total = len(runs) or 1

        def fetch(run):
            """Downloads one run of adjacent tiles (worker thread)."""
            if cancelled and cancelled():
                raise Cancelled()
            start = run[0][1]
            end = run[-1][1] + run[-1][2] - 1
            return run, start, fetch_range(self.url, start, end)

        # Runs download in parallel; each is decoded and copied into `out` as
        # it arrives (pool.map keeps the order, which does not matter here).
        with ThreadPoolExecutor(self.PARALLEL_REQUESTS) as pool:
            for done, (run, start, data) in enumerate(pool.map(fetch, runs), 1):
                for index, offset, count, ty, tx in run:
                    # decode returns (pixels, indices, shape); only the pixels matter.
                    tile = page.decode(data[offset - start:offset - start + count], index)[0]
                    tile = np.asarray(tile, dtype=np.float32).reshape(tile_rows, tile_cols)
                    # Copy the part of the tile that falls inside the window.
                    top, left = ty * tile_rows, tx * tile_cols
                    y0, y1 = max(row, top), min(row + rows, top + tile_rows)
                    x0, x1 = max(col, left), min(col + cols, left + tile_cols)
                    out[y0 - row:y1 - row, x0 - col:x1 - col] = \
                        tile[y0 - top:y1 - top, x0 - left:x1 - left]
                if progress:
                    progress(done / total)
                if cancelled and cancelled():
                    raise Cancelled()

        # NoData as NaN, matched as in dem_files._nodata_to_nan (a NaN NoData,
        # as LOLA uses, needs nothing).
        if self.nodata is not None and np.isfinite(self.nodata):
            out[out == np.float32(self.nodata)] = np.nan
            if abs(self.nodata) > 1e30:
                out[np.abs(out) > 1e30] = np.nan
        return out

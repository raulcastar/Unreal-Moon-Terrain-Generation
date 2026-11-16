# Lunar datasets

**Library → Lunar Datasets** brings real lunar elevation maps into the studio:
browse public datasets, preview a map, download it (whole or only a region),
and send it to Unreal. No account or key is needed; everything comes from
NASA's public servers.

## Sources

| Source | Maps | Resolution | File size | Preview before download | Region only |
|---|---|---|---|---|---|
| **LROC NAC DTMs** | ~670 stereo terrain models (Apollo sites, craters, rilles, volcanic and polar features) | 1-5 m/px | ~50-300 MB | yes: the archive's shaded relief | no: download, then crop in Send to Unreal |
| **LOLA south-pole landing sites** | 27 candidate landing sites (Shackleton rim, Connecting ridge, Malapert, de Gerlache...) | 5 m/px | 40-140 MB | after download | no: download, then crop in Send to Unreal |
| **LOLA south-pole mosaics** | the south polar region from 60°S, 75°S, 80°S or 83°S to the pole | 10-240 m/px | 0.2-5.1 GB | yes: read from the file's own overview | **yes**: only the tiles under the region are fetched |

The LROC list (one ~0.5 MB query to NASA's PDS Orbital Data Explorer) is
loaded with **Load NAC DTM list** and kept, so it is needed once. The LOLA
entries are listed in [`config/lunar_datasets.json`](../config/lunar_datasets.json),
with each source's credit and reference.

## Workflow

1. Pick a map in the catalogue. **Search** filters by name, title and region.
   The details give its location, resolution, size, features and credit, and
   the preview appears on the right.
2. Then either:
   - **Download**: the whole file goes to `input_maps/<source>/` (for example
     `input_maps/lroc_nac/NAC_DTM_APOLLO15.TIF`), so it also appears in
     **Browse Maps** and the map menus. Files over 1 GB ask first. A stopped or
     failed download resumes where it stopped next time.
   - **Download only a region** (mosaics): tick it, draw the region on the
     preview (the same crop controls as in [Send to Unreal](terrain.md#crop-a-region-of-a-large-map),
     with snapping to a Landscape size and the read-out), then **Download
     region**. Only that region is fetched: 1009 × 1009 px of the 80 m/px
     mosaic takes a few seconds instead of a 190 MB download. It is saved like
     any crop, to `outputs/<map>_crop_x<X>_y<Y>_<W>x<H>/`, and opens in Send to
     Unreal.
3. **Send to Unreal** opens the map in the Send to Unreal view (downloading it
   first if needed). A map too large for one Landscape opens with the crop
   turned on, so a region can be chosen there.

GeoTIFFs keep their own resolution: Send to Unreal fills in *Resolution* from
the file, and a crop records it in its `_crop.json`.

## Notes

- The mosaics use compressed GeoTIFFs, which need the `imagecodecs` package
  (in `requirements.txt`).
- Previews and details are cached in `input_maps/.dataset_cache/`; delete that
  folder to fetch them again.
- LROC DTMs can carry processing artefacts (boxes, seams, faceted areas); each
  DTM's README, linked from **About this source**, describes them.

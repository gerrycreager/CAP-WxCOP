# FAA VFR Sectional Charts — self-hosted raster source

## Credit

This entire fetch mechanism — the `external-api.faa.gov` geoname lookup,
parsing the returned XML for the current edition's zip URL, and the
zip/TIFF/TFW/HTM structure it unpacks into — was documented and shared
directly by:

**Charlie Jackson, Capt, PCR-WA-068** (<charles.jackson@wawg.cap.gov>),
creator of **[capgrids.com](https://capgrids.com)**.

Charlie built and already runs this pipeline in production for
capgrids.com, including the daily cron update-check this project's
cron job is modeled on. Without his write-up, this project would have
started from "there's no public FAA chart API" (which was this app's
prior, incorrect assumption — see `project_nasr_sectional_rendering_idea`
in Claude's session memory for that history). Thank you, Charlie.

## What's here

`raw/<ChartName>/` — one directory per FAA sectional chart, each
containing the extracted `.tif` (georeferenced GeoTIFF, FAA's real
chart raster — NAD83 Lambert Conformal Conic, per-chart projection
parameters embedded in the file itself), `.tfw` (world file, redundant
with the embedded GeoTIFF tags but kept for tools that want it), and
`.htm` (FAA's own chart metadata).

`manifest.json` — tracks each chart's last-seen `edition_date` so the
fetch script can tell "unchanged" from "needs re-fetch" without
re-downloading anything.

55 charts total: 37 CONUS + 15 Alaska + 1 Hawaii + 2 Caribbean (which
cover Puerto Rico/US Virgin Islands — there's no sectional literally
named "Puerto Rico-Virgin Islands" in FAA's current catalog). No
Guam/Mariana Islands sectional exists in FAA's distribution to fetch.

This directory is intentionally **not** committed to git (`data/` is
large, binary, and fully re-derivable from FAA's servers at any time —
see `.gitignore`). This README is the exception, checked in on its
own so the credit above is visible on GitHub.

## Pipeline

1. `scripts/fetch_sectional_charts.py` — fetch/update-check (this doc's
   subject). Run once for the initial backfill, then daily via cron
   for updates (see that script's crontab entry).
2. Reprojection + tiling (EPSG:3857, palette-aware — GDAL warns that
   naive resampling on indexed-color pixels blends palette index
   numbers, not real colors, so this step expands to RGB or uses
   nearest-neighbor resampling, not a plain `gdalwarp` default) into
   the existing MapCache instance already serving MRMS/satellite
   tiles on this host.
3. Served as a shared WMTS layer CAP_WxCOP (EWMC, Flight Ops Impacts)
   and AvTrack all point at — one tile source, several consuming apps.
   (Deliberately not added to Cadet Weather Impacts, per Gerry.)

## Fixed: legend/marginalia bleed at chart overlaps

FAA's own digital-chart product documentation confirms why this
happened and that cropping it out is the officially-sanctioned fix,
not a workaround:

> "An Aeronautical Raster Chart is a digital image of an FAA VFR
> Chart. All information that is part of the paper chart is included
> in the file. The area inside the neat line is georeferenced to the
> surface of the earth. **Only the main body of the chart is
> accurately georeferenced.**"

Each chart's raster includes its own legend/title panel baked into
the same pixel grid as the real map — technically inside the same
affine transform, but not "accurately georeferenced" per FAA's own
statement above, i.e. not real cartographic data. Left uncropped, the
VRT mosaic would pick whichever chart's panel happened to be listed
last wherever charts overlap, showing a legend box instead of terrain
in a thin band near some chart boundaries.

`scripts/crop_sectional_margins.py` detects and removes this panel
on **all four edges** (the neatline is a rectangle, not just a west-
edge legend column — margins measured 2026-11-21 across all 55
charts: left 6.7%-17.2%, top 0%-7.1%, right 0%, bottom up to ~9% of
image size, so this isn't a fixed crop on any side) before
reprojection. Detection: downsample, then independently scan inward
from each of the four edges for where a sustained run of "page
background" rows/columns (bright + low-saturation, i.e.
white/cream/light-blue) gives way to dense chart content — that's
the real chart's neatline on that side. Per FAA's own documentation
(quoted above), the neatline is the trim line: only what's inside it
is accurately georeferenced. Validated visually against charts with
very different backgrounds (tan terrain, mountainous Alaska,
blue-ocean Caribbean) before being applied pipeline-wide. Runs as a
step before `warp_sectional_charts.sh`, which reads from `cropped/`
rather than `raw/`.

No known residual limitation from this crop as of 2026-11-22 — all
four edges are now trimmed per chart, not just the west legend
column.

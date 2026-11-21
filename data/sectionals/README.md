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

## Known limitation: legend/marginalia bleed at chart overlaps

Adjacent FAA charts physically overlap, and each chart's raster
includes its own legend/marginalia box baked into the image (not
cropped to pure terrain). The VRT mosaic picks whichever chart is
listed last wherever charts overlap, so a thin band near some chart
boundaries can show a neighboring chart's legend box instead of
terrain. This is cosmetic, not a correctness bug — verified the core
chart content (interior of any single chart) renders cleanly at full
color fidelity. A real fix would need per-chart cutline polygons
clipping each raster to its true mapped extent before mosaicking;
not attempted yet (FAA doesn't publish exact crop boundaries, so
this would need either image-based legend detection or manually
sourced crop geometry). Revisit if this proves visually disruptive
in practice.

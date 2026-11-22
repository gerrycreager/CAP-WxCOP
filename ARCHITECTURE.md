# WxCOP — Architecture

This is the conceptual map for a contributor: how data gets in, where it
lives, and how it becomes the pages at `/CAP_WxCOP/*`. For "how do I deploy
a change" see `README.md`; this doc is "how is the system put together."

## Three-host layout

WxCOP runs across three hosts, split by role:

- **r815** — the public-facing relay. Apache + mod_wsgi (this app), plus the
  MapServer/MapCache stack that serves radar, satellite, and the self-hosted
  FAA sectional charts as map tiles. Does no heavy data processing itself.
- **data1** — ingest and rendering. Unidata LDM receives MRMS radar and other
  NOAAPort feeds continuously; `pqact` rules pipe matching products into
  renderer scripts (e.g. `mrms_tile_renderer.py`) that turn GRIB2 into
  Leaflet-ready PNG tile pyramids.
- **data2** — PostgreSQL/PostGIS. All structured observation and model data
  (METAR/TAF, soundings, WPC fronts, NHC tropical tracks, NASR aviation
  features, MADIS surface obs) lives here, queried live by the Flask app.

The Flask app itself (`app.py` and its blueprints) runs on r815 and talks to
data2's Postgres for anything structured, and to r815's own MapServer/
MapCache for anything raster-tiled.

## Data flow, by source

```
NOAAPort/MRMS  --LDM-->  data1 (pqact + renderer scripts)  --tiles-->  r815 MapCache
FAA chart API  --cron -->  self-hosted raster pipeline (crop/warp/mosaic)  -->  r815 MapCache
NOAA REST APIs (WPC fronts, NHC tropical, NASR) --cron poll-->  data2 Postgres
HRRR/RRFS model output  --cron fetch-->  data2 / local cache  -->  Flask API  -->  frontend
```

## Subsystems

**MRMS radar.** LDM → `pqact` PIPE rules → `mrms_tile_renderer.py` → PNG tile
pyramid on disk, served by MapCache. Composite reflectivity, MESH hail size,
lightning probability, azimuthal shear. 2-minute render cycle, 30-frame
retention for animation.

**Self-hosted FAA VFR sectional charts.** 55 current-edition charts (CONUS +
AK + HI + Caribbean/PR-VI) fetched directly from `external-api.faa.gov`
(mechanism documented in `data/sectionals/README.md`, credited there to
Charlie Jackson/capgrids.com), cropped to the FAA-defined neatline on all
four edges (only the area inside a chart's printed neatline is accurately
georeferenced — FAA's own documentation), reprojected to EPSG:3857, mosaicked
with GDAL into one VRT, and served through MapServer + MapCache as a WMTS
tileset. Daily cron re-checks each chart's edition date and only re-fetches
on an actual update. Same tile source is reused by AvTrack (a separate app,
same host) and Flight Ops Impacts — one tile source, multiple consumers.

**NASR aviation-detail layer.** Navaids, fixes, airports, heliports, and
runways from FAA's NASR data, served as real vector geometry with no zoom
ceiling (the sectional raster's own practical detail limit doesn't apply
here) via `/api/nasr/*`.

**WPC fronts + NHC tropical overlays.** Poll NOAA's public ArcGIS REST
services directly over HTTPS (not LDM) for WPC surface fronts/pressure
centers and NHC tropical cyclone points/tracks/cones. Rendered with real NWS
front symbology (cold/warm/stationary/occluded pips), not plain colored
lines.

**HRRR/RRFS forecast tools.** Wind constraint maps, smoke forecast overlay,
on-demand soundings (async job + polling, F00–F48), and TAF generation —
the latter two feed mission-specific tooling (a mission's Mission Essential
Forecast tool, and an automated fetch→timeline→PDF→email pipeline for
missions that want one generated on schedule).

**VFR/IFR stoplight.** Converts live METAR + forecast ceiling/visibility
into a go/no-go indicator against CAP's actual release-authority tiers
(FRO/SFRO/CAP-DO) from CAPR 70-10, with per-Wing threshold overrides.

## Pages

| Page | Audience | What it shows |
|---|---|---|
| Enhanced Weather Map (EWMC) | Full operational picture | Everything above, all togglable layers |
| Flight Ops Impacts | Mission planning | Stoplight, wind/smoke forecast, sectional/NASR, TFRs |
| Cadet Weather Impacts | Cadet-facing | A deliberately reduced subset of the above |
| Wind map | Wind-specific | HRRR/RRFS wind constraint analysis only |
| MRMS radar | Radar-specific | Standalone radar animation |

## Why Flask + LDM + PostGIS, not something more "standard"

LDM is the existing, already-trusted mechanism CAP/NOAA partners use for
real-time NOAAPort data — there was no reason to reinvent that ingest path.
PostGIS gives spatial queries (nearest station, feature-in-polygon, etc.)
for free on data that's inherently geographic. Flask blueprints keep each
subsystem (radar, weather, wind, NASR, fronts/tropical) as an independently
testable/deployable unit behind one WSGI app, which is what let this grow
from "a radar page" to the current feature set without a rewrite.

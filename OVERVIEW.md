# WxCOP — what it is and why it exists

WxCOP (Weather Common Operating Picture) is a weather map built specifically
for Civil Air Patrol aviation and search-and-rescue mission planning — not a
general-purpose weather app pressed into service for CAP.

## The problem

A mission pilot or Incident Commander deciding whether a sortie is flyable
today needs several things that don't come bundled together anywhere else:
live radar, current METARs/TAFs translated into a VFR/IFR go/no-go call
against CAP's actual release-authority rules (CAPR 70-10), the real FAA
sectional chart for the area (not a generic basemap), wind/ceiling/visibility
forecasts for the next several hours, and active TFRs, fronts, and tropical
systems — all on one map, without juggling five different government
websites mid-briefing.

## What it does

- **Live weather picture**: METARs/TAFs with flight-category coloring,
  station plots (wind barb, temp/dewpoint, ceiling, visibility, gust),
  NEXRAD/MRMS radar (reflectivity, hail, lightning, rotation), WPC surface
  fronts and pressure centers with real NWS symbology, NHC tropical cyclone
  tracks/cones, active TFRs.
- **Real FAA charts, not a stand-in**: all 55 current-edition VFR sectional
  charts (CONUS, Alaska, Hawaii, Caribbean/Puerto Rico), self-hosted and
  kept current automatically, plus a NASR-sourced layer of navaids, fixes,
  airports, and runways with no zoom-level cutoff.
- **Forecast tools for mission planning**: HRRR/RRFS-driven wind constraint
  maps, smoke forecasts, on-demand soundings, and a VFR/IFR stoplight
  indicator wired to CAP's actual release-authority thresholds (FRO / SFRO /
  CAP-DO tiers).
- **Mission-specific products**: a TAF generation tool mission staff use
  directly (feeds Mission Essential Forecast documents), and an automated
  MEF pipeline (fetch → timeline → PDF → email) for missions that want one
  on a schedule rather than built by hand each time.

## Who uses it, and how it fits together

Wing/mission staff and aircrew use it directly as a web map — no install,
just a browser. It also serves as backing data for other CAP tools: the
VFR sectional/NASR layers it self-hosts are reused by **AvTrack** (CAP's
ADS-B aircraft tracking app, a separate project) as its own map basemaps,
and its TAF/forecast data feeds mission tooling like the KOMN MEF generator.
There's a simplified "Cadet Weather Impacts" page for cadet-facing use that
deliberately leaves out some of the denser operational layers.

## How it's built (briefly — see `README.md` for deployment/ops detail, and `ARCHITECTURE.md` for the full technical picture)

A Flask web app on top of a continuous data-ingest pipeline: Unidata LDM
pulls MRMS radar and other NOAAPort feeds in real time, scheduled jobs pull
HRRR/RRFS model output and FAA chart updates, and everything lands in
PostGIS for the app to query and render as map layers.

#!/var/www/cap_winds_app/venv/bin/python3
"""
ingest_nam_oconus.py — CAP WxCOP OCONUS Airport Impacts Ingest
===================================================================
Reads OCONUS grib2 data and ingests airport weather impacts into
observations.airport_wx_impacts on data2, for AK/HI/PR-Caribbean.

RRFS->NAM migration (2026-09-21): NOAA is retiring NAM (SCN 26-48, same
notice driving this app's broader HRRR->RRFS migration -- see
/root/.claude/plans/rosy-wobbling-music.md, "Phase 5"). DATA_SOURCE below
(env var NAM_OCONUS_SOURCE) defaults to 'rrfs'; 'nam' remains a full
fallback with the original code path completely unchanged.

Real mechanism (corrected -- the previous version of this docstring
described a PostGIS-raster design that was never actually implemented):
uses `rasterio` (Python GDAL bindings) to open the grib2 file directly and
read band arrays with `src.read()`; `rasterio.warp.transform` handles the
CRS-aware reprojection (AK's polar stereographic, RRFS's Lambert Conformal)
and an inverse affine transform converts projected coords to pixel row/col.
No PostGIS raster table exists anywhere in this pipeline. No KDTree either
-- one point lookup per airport, not a full-grid regrid.

Domains (NAM path):
    AK    — awak3d   → airports with iso_region='US-AK' or station_id~'^PA'
    HI    — afwahi   → airports with iso_region='US-HI'
    CARIB — afwaca   → airports with iso_region IN ('US-PR','US-VI')
NAM band map (verified against nam.t00z.awak3d00.tm00.grib2, hardcoded band
index -- fragile/NAM-specific, see RRFS_WANTED_FIELDS below for why the
RRFS path matches by GRIB_ELEMENT name instead):
    14  REFC  entire atm   dBZ      composite reflectivity (decoded, NEVER
                                     used downstream -- confirmed 2026-09-21,
                                     not in build_impact_row/INSERT_SQL/the
                                     live schema/the API -- dropped entirely
                                     from the RRFS field list below, not
                                     ported forward as dead weight)
    15  VIS   surface      m        visibility
    21  GUST  surface      m/s      wind gust
    296 TMP   2m AGL       degC     temperature
    298 DPT   2m AGL       degC     dewpoint
    299 RH    2m AGL       %        relative humidity (fallback for dpt)
    300 UGRD  10m AGL      m/s      U-wind component
    301 VGRD  10m AGL      m/s      V-wind component
    306 PRATE surface      kg/m2/s  precipitation rate
    335 CAPE  surface      J/kg     CAPE
    350 HGT   ceiling      gpm      cloud ceiling height (clear-sky fill
                                     value clamped >50000ft -> 30000ft)

RRFS domains: 'carib' maps to RRFS's 'pr' domain (RRFS has no separate VI
domain per SCN 26-48) -- geographic grid-extent coverage of PR-domain VI
airports (TIST/TISX) is UNVERIFIED, confirm during testing, don't assume
it's fine by analogy to ak/hi. RRFS's dedicated CEIL field (meters, real
physical values even for clear sky -- confirmed 2026-09-21, e.g. 11885m at
one AK point) numerically survives the *same* `*3.28084` + `>50000ft` clamp
formula NAM's HGT-based ceiling hack uses -- confirmed no code change
needed there, the existing arithmetic works for both sources unmodified.

Usage:
    ingest_nam_oconus.py --domain ak --cycle 12 --fhour 0      # NAM path (NAM_OCONUS_SOURCE=nam)
    ingest_nam_oconus.py --domain hi --all-fhours              # RRFS path (default): --cycle ignored,
                                                                # resolves the latest ready RRFS cycle itself
    ingest_nam_oconus.py --domain carib --cycle 00 --fhour 0

Called by fetch_nam_oconus.py after each successful file download for the
NAM path; for the RRFS path (default), fetch is inline via
rrfs_wind_fetch.py's byte-range fetch-and-cache, no separate fetch step.

Run on: data1 (192.168.0.61) — local disk (LDM-fed, NOT actually NFS
        despite what an earlier version of this docstring claimed --
        /LDM on data1 is a symlink to a local LVM volume) for the NAM path;
        connects to data2 PostgreSQL for upsert either way.
"""

import os
import sys
import math
import logging
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

import rasterio
import rasterio.crs
from rasterio.warp import transform as warp_transform
import numpy as np
import psycopg2
from psycopg2.extras import execute_values

import rrfs_wind_fetch as rwf

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DB_DSN   = "dbname=avwx_data user=avwx_user host=192.168.0.60"
NAM_BASE = Path('/LDM/models/nam')

LOG_FILE = '/var/log/ingest_nam_oconus.log'

DATA_SOURCE = os.environ.get('NAM_OCONUS_SOURCE', 'rrfs')  # 'rrfs' (default) or 'nam'

# RRFS domain/resolution mapping -- see module docstring for the 'carib'->'pr'
# note and the unverified VI-coverage caveat.
RRFS_DOMAIN     = {'ak': 'ak', 'hi': 'hi', 'carib': 'pr'}
RRFS_RESOLUTION = {'ak': '3km', 'hi': '2p5km', 'carib': '2p5km'}
RRFS_MODEL_SOURCE = {'ak': 'RRFS_AK', 'hi': 'RRFS_HI', 'carib': 'RRFS_PR'}

# RRFS 2dfld field wanted-set for the byte-range fetch. REFC deliberately
# dropped (decoded but never used downstream -- see module docstring).
RRFS_WANTED_FIELDS = {
    ('VIS', 'surface'), ('GUST', 'surface'),
    ('TMP', '2 m above ground'), ('DPT', '2 m above ground'), ('RH', '2 m above ground'),
    ('UGRD', '10 m above ground'), ('VGRD', '10 m above ground'),
    ('PRATE', 'surface'), ('CAPE', 'surface'),
    ('HGT', 'cloud ceiling'),
}
# Real bug found+fixed 2026-09-23: the module docstring's claim that
# GRIB_ELEMENT=CEIL is a clean, reliable field (no known cfgrib/pygrib-style
# issues) turned out to be only half true -- GDAL DOES resolve it cleanly,
# but the field's own VALUES are a genuine RRFS artifact. At KBAK, 00z
# 2026-09-23, CEIL alternated 1795->3468->1798->3346ft hour to hour while
# low cloud cover sat at a rock-steady 100% the whole time -- physically
# impossible for a real ceiling under an unbroken overcast layer.
# GRIB_ELEMENT=HGT at typeOfLevel='cloud ceiling' (same file, same hours)
# was smooth: 1650->1644->1644->1854ft. Switched to HGT; see
# HGT_SHORT_NAME_KEYS below for why this needs its own disambiguation.
# Same bug, same fix, also applied to rrfs_model_taf.py and
# mission_taf_extract.py. NOT YET DEPLOYED to data1 (this script's actual
# production host) -- confirm before copying over, per the cross-host
# deploy discipline used throughout the RRFS migration.
HGT_SHORT_NAME_KEYS = {
    '0-CEIL': 'ceil',
}

# Band numbers — verified against nam.t00z.awak3d00.tm00.grib2
BANDS = {
    'refc':  14,   # Composite reflectivity (dBZ)
    'vis':   15,   # Surface visibility (m)
    'gust':  21,   # Surface wind gust (m/s)
    'tmp':   296,  # 2m temperature (degC)
    'dpt':   298,  # 2m dewpoint (degC)
    'rh':    299,  # 2m relative humidity (%)
    'ugrd':  300,  # 10m U-wind (m/s)
    'vgrd':  301,  # 10m V-wind (m/s)
    'prate': 306,  # Precip rate (kg/m2/s)
    'cape':  335,  # Surface CAPE (J/kg)
    'ceil':  350,  # Cloud ceiling height (gpm)
}

# model_source tag stored in DB — used for priority/display
MODEL_SOURCE = {
    'ak':    'NAM_AK',
    'hi':    'NAM_HI',
    'carib': 'NAM_CARIB',
}

SOURCE_PRIORITY = {
    'ak':    3,
    'hi':    3,
    'carib': 3,
}

# Airport region filters per domain
REGION_FILTER = {
    'ak':    "iso_region = 'US-AK' OR station_id ~ '^PA'",
    'hi':    "iso_region = 'US-HI'",
    'carib': "iso_region IN ('PR-U-A','VI-SC','VI-ST','BQ-BO','BQ-SE') OR station_id LIKE 'TJ%' OR station_id LIKE 'TI%'",
}

# Filename templates
FILE_TEMPLATE = {
    'ak':    'nam.t{cycle:02d}z.awak3d{fhour:02d}.tm00.grib2',
    'hi':    'nam.t{cycle:02d}z.afwahi{fhour:02d}.tm00.grib2',
    'carib': 'nam.t{cycle:02d}z.afwaca{fhour:02d}.tm00.grib2',
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s [ingest_nam_oconus] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger('ingest_nam_oconus')

# ---------------------------------------------------------------------------
# Unit conversions
# ---------------------------------------------------------------------------
MPS_TO_KTS = 1.94384

def ms_to_kts(v):
    return None if v is None else v * MPS_TO_KTS

def prate_to_mmhr(v):
    """kg/m2/s → mm/hr"""
    return None if v is None else v * 3600.0

def vis_to_sm(v):
    """meters → statute miles"""
    return None if v is None else v / 1609.344

def dpt_from_rh_tmp(rh, tmp_c):
    """Magnus formula dewpoint from RH% and T°C"""
    if rh is None or tmp_c is None or rh <= 0:
        return None
    a, b = 17.625, 243.04
    alpha = math.log(rh / 100.0) + (a * tmp_c) / (b + tmp_c)
    return (b * alpha) / (a - alpha)

def wind_dir(u, v):
    """U/V m/s → meteorological wind direction (degrees)"""
    if u is None or v is None:
        return None
    deg = math.degrees(math.atan2(-u, -v)) % 360
    return int(round(deg))

def wind_speed(u, v):
    """U/V m/s → speed in knots"""
    if u is None or v is None:
        return None
    return math.sqrt(u*u + v*v) * MPS_TO_KTS

def vfr_color(ceil_ft, vis_sm):
    """CAPR 70-1 VFR flight category color."""
    if ceil_ft is None or vis_sm is None:
        return 'UNKNOWN', None
    if ceil_ft < 500 or vis_sm < 1:
        return 'RED', ('ceil' if ceil_ft < 500 else 'vis')
    if ceil_ft < 1000 or vis_sm < 3:
        return 'RED', ('ceil' if ceil_ft < 1000 else 'vis')
    if ceil_ft < 3000 or vis_sm < 5:
        return 'YELLOW', ('ceil' if ceil_ft < 3000 else 'vis')
    return 'GREEN', None

def wind_color(speed_kts):
    """CAPR 70-1 wind stoplight."""
    if speed_kts is None:
        return 'UNKNOWN'
    if speed_kts > 30:
        return 'RED'
    if speed_kts > 20:
        return 'YELLOW'
    return 'GREEN'

# ---------------------------------------------------------------------------
# Raster reading
# ---------------------------------------------------------------------------

def safe_val(arr, row, col):
    """Extract scalar from array, return None if nodata/nan/extreme."""
    try:
        v = float(arr[row, col])
        if math.isnan(v) or math.isinf(v) or abs(v) > 1e15:
            return None
        return v
    except Exception:
        return None

def load_bands(grib_path: Path) -> dict:
    """
    Open grib2 file and read all required bands into numpy arrays.
    Returns dict of band_name → 2D numpy array.
    """
    arrays = {}
    try:
        with rasterio.open(grib_path) as src:
            for name, band_num in BANDS.items():
                if band_num <= src.count:
                    arrays[name] = src.read(band_num).astype(np.float32)
                else:
                    log.warning(f'Band {band_num} ({name}) not found in {grib_path.name}')
                    arrays[name] = None
            arrays['_transform'] = src.transform
            arrays['_crs']       = src.crs
            arrays['_shape']     = (src.height, src.width)
    except Exception as e:
        log.error(f'Error reading {grib_path}: {e}')
        return {}
    return arrays

def load_rrfs_bands(grib_path: Path) -> dict:
    """
    Same shape/return value as load_bands() (keyed by the same lowercase
    field names -- vis/gust/tmp/dpt/rh/ugrd/vgrd/prate/cape/ceil -- so
    build_impact_row() works completely unchanged regardless of source),
    but for an RRFS 2dfld curated subset. Matches fields by GRIB_ELEMENT
    tag instead of NAM's hardcoded band index, since RRFS's message
    ordering is unrelated to NAM's (confirmed 2026-09-21: RRFS puts REFC
    at message 1, not band 14 -- a fixed-index table would be wrong).
    GDAL's GRIB driver was confirmed (2026-09-21) to resolve every field
    this pipeline needs cleanly, including REFC/CEIL which have known
    issues under cfgrib/pygrib -- not relevant here since this path never
    uses either library.
    """
    arrays = {}
    try:
        with rasterio.open(grib_path) as src:
            for i in range(1, src.count + 1):
                tags = src.tags(i)
                elem = (tags.get('GRIB_ELEMENT') or '').lower()
                if elem == 'hgt':
                    # Ambiguous by itself -- RRFS's 2dfld has several
                    # distinct HGT fields; disambiguate via GRIB_SHORT_NAME.
                    key = HGT_SHORT_NAME_KEYS.get(tags.get('GRIB_SHORT_NAME'))
                    if key is None:
                        continue
                else:
                    key = elem
                if key and key not in arrays:  # first match wins on any dup
                    arrays[key] = src.read(i).astype(np.float32)
            arrays['_transform'] = src.transform
            arrays['_crs']       = src.crs
            arrays['_shape']     = (src.height, src.width)
    except Exception as e:
        log.error(f'Error reading RRFS {grib_path}: {e}')
        return {}
    return arrays

def airport_grid_coords(airports: list, raster_crs, transform, shape) -> list:
    """
    Project airport lat/lon points into raster pixel coordinates.
    Uses rasterio.warp.transform for CRS-aware projection — handles
    AK polar stereographic and regular lat/lon grids identically.

    airports: list of (airport_id, station_id, lon, lat, runway_hdg)
    Returns: list of (airport_id, station_id, row, col, runway_hdg)
             with row/col as integer pixel indices, or None if outside grid.
    """
    if not airports:
        return []

    lons = [a[2] for a in airports]
    lats = [a[3] for a in airports]
    grid_h, grid_w = shape

    # Project from WGS84 to raster CRS
    try:
        xs, ys = warp_transform('EPSG:4326', raster_crs, lons, lats)
    except Exception as e:
        log.error(f'CRS transform failed: {e}')
        return []

    results = []
    inv = ~transform
    for i, (x, y) in enumerate(zip(xs, ys)):
        col_f, row_f = inv * (x, y)
        col = int(round(col_f))
        row = int(round(row_f))
        if 0 <= row < grid_h and 0 <= col < grid_w:
            results.append((airports[i][0], airports[i][1], row, col, airports[i][4]))
        # Out-of-grid airports silently skipped
    return results

# ---------------------------------------------------------------------------
# Airport lookup
# ---------------------------------------------------------------------------

def get_airports(cur, domain: str) -> list:
    """
    Fetch airports for domain from DB.
    Returns list of (airport_id, station_id, lon, lat, best_runway_hdg).
    """
    where = REGION_FILTER[domain]
    cur.execute(f"""
        SELECT
            a.id,
            a.station_id,
            ST_X(a.location) AS lon,
            ST_Y(a.location) AS lat,
            (SELECT CAST(r.le_heading_degt AS smallint)
             FROM observations.runways r
             WHERE r.airport_id = a.id
               AND r.le_length_ft = (
                   SELECT MAX(r2.le_length_ft) FROM observations.runways r2
                   WHERE r2.airport_id = a.id)
             LIMIT 1) AS best_runway_hdg
        FROM observations.airports a
        WHERE a.location IS NOT NULL
          AND ({where})
        ORDER BY a.station_id
    """)
    return cur.fetchall()

# ---------------------------------------------------------------------------
# Impact row building
# ---------------------------------------------------------------------------

def build_impact_row(airport_id, station_id, runway_hdg,
                     bands, row, col,
                     model_source, model_run, valid_time, fhour,
                     priority):
    """Extract variables from band arrays at (row,col) and build DB row."""

    def b(name):
        arr = bands.get(name)
        return safe_val(arr, row, col) if arr is not None else None

    u_ms   = b('ugrd')
    v_ms   = b('vgrd')
    tmp_c  = b('tmp')
    dpt_c  = b('dpt')
    rh_pct = b('rh')
    vis_m  = b('vis')
    gust_ms = b('gust')
    ceil_gpm = b('ceil')
    prate  = b('prate')
    cape   = b('cape')

    # Derive dewpoint from RH if direct DPT is missing
    if dpt_c is None and rh_pct is not None and tmp_c is not None:
        dpt_c = dpt_from_rh_tmp(rh_pct, tmp_c)

    # Wind
    wdir   = wind_dir(u_ms, v_ms)
    wspd_kts = wind_speed(u_ms, v_ms)
    wgust_kts = ms_to_kts(gust_ms)

    # Ceiling: gpm → ft (1 gpm ≈ 3.28084 ft)
    ceil_ft = int(ceil_gpm * 3.28084) if ceil_gpm is not None else None
    # Cap unrealistically high ceilings (clear sky = 20000 gpm fill value)
    # Clear sky → treat as unlimited ceiling (99999 ft) for VFR purposes
    if ceil_ft is not None and ceil_ft > 50000:
        ceil_ft = 30000

    # Visibility m → SM, cap at 10SM
    vis_sm = min(vis_to_sm(vis_m), 10.0) if vis_m is not None else None

    # Flight category
    vfr_col, vfr_worst = vfr_color(ceil_ft, vis_sm)

    # IFR color (same logic, different thresholds stored separately)
    ifr_col = vfr_col  # reuse for now
    ifr_worst = vfr_worst

    # Precip rate mm/hr
    precip_mmhr = prate_to_mmhr(prate)

    # Temperatures to Fahrenheit for legacy columns
    tmp_f  = (tmp_c  * 9/5 + 32) if tmp_c  is not None else None
    dpt_f  = (dpt_c  * 9/5 + 32) if dpt_c  is not None else None

    # Heat index (Rothfusz, valid T >= 80°F, RH >= 40%)
    heat_index_f = None
    if tmp_f is not None and tmp_f >= 80 and rh_pct is not None and rh_pct >= 40:
        T, R = tmp_f, rh_pct
        hi = (-42.379 + 2.04901523*T + 10.14333127*R
              - 0.22475541*T*R - 6.83783e-3*T*T
              - 5.481717e-2*R*R + 1.22874e-3*T*T*R
              + 8.5282e-4*T*R*R - 1.99e-6*T*T*R*R)
        heat_index_f = hi

    # Wind chill (NWS formula, valid T <= 50°F, wind >= 3 mph)
    wind_chill_f = None
    if tmp_f is not None and wspd_kts is not None:
        wspd_mph = wspd_kts * 1.15078
        if tmp_f <= 50 and wspd_mph >= 3:
            wind_chill_f = (35.74 + 0.6215*tmp_f
                           - 35.75*(wspd_mph**0.16)
                           + 0.4275*tmp_f*(wspd_mph**0.16))

    # Crosswind (simplified — uses runway heading if available)
    crosswind_kts = None
    if wdir is not None and wspd_kts is not None and runway_hdg is not None:
        angle = math.radians(abs(wdir - runway_hdg) % 360)
        if angle > math.pi:
            angle = 2*math.pi - angle
        crosswind_kts = abs(wspd_kts * math.sin(angle))

    return (
        airport_id, station_id, model_source, model_run, valid_time,
        fhour,
        ceil_ft, int(vis_m) if vis_m is not None else None,
        wdir, wspd_kts, wgust_kts,
        tmp_c, dpt_c, tmp_f,
        heat_index_f, wind_chill_f, crosswind_kts,
        runway_hdg,
        vfr_col, vfr_worst, ifr_col, ifr_worst,
        None,      # tstm_prob — populated by LAMP for CONUS, NULL for NAM OCONUS
        priority,
    )

# ---------------------------------------------------------------------------
# DB upsert
# ---------------------------------------------------------------------------
INSERT_SQL = """
    INSERT INTO observations.airport_wx_impacts (
        airport_id, station_id, model_source, model_run, valid_time,
        forecast_hour,
        ceil_ft, vis_m,
        wind_dir, wind_speed_kts, wind_gust_kts,
        tmp_c, dpt_c, tmp_f,
        heat_index_f, wind_chill_f, crosswind_kts,
        best_runway_hdg,
        vfr_color, vfr_worst_param, ifr_color, ifr_worst_param,
        tstm_prob,
        source_priority
    ) VALUES %s
    ON CONFLICT (airport_id, model_run, forecast_hour)
    DO UPDATE SET
        model_source    = EXCLUDED.model_source,
        valid_time      = EXCLUDED.valid_time,
        ceil_ft         = EXCLUDED.ceil_ft,
        vis_m           = EXCLUDED.vis_m,
        wind_dir        = EXCLUDED.wind_dir,
        wind_speed_kts  = EXCLUDED.wind_speed_kts,
        wind_gust_kts   = EXCLUDED.wind_gust_kts,
        tmp_c           = EXCLUDED.tmp_c,
        dpt_c           = EXCLUDED.dpt_c,
        tmp_f           = EXCLUDED.tmp_f,
        heat_index_f    = EXCLUDED.heat_index_f,
        wind_chill_f    = EXCLUDED.wind_chill_f,
        crosswind_kts   = EXCLUDED.crosswind_kts,
        best_runway_hdg = EXCLUDED.best_runway_hdg,
        vfr_color       = EXCLUDED.vfr_color,
        vfr_worst_param = EXCLUDED.vfr_worst_param,
        ifr_color       = EXCLUDED.ifr_color,
        ifr_worst_param = EXCLUDED.ifr_worst_param,
        tstm_prob       = EXCLUDED.tstm_prob,
        source_priority = EXCLUDED.source_priority,
        ingested_at     = NOW()
    WHERE EXCLUDED.source_priority <= observations.airport_wx_impacts.source_priority
"""

def upsert_impacts(cur, rows):
    if not rows:
        return 0
    execute_values(cur, INSERT_SQL, rows)
    return cur.rowcount

# ---------------------------------------------------------------------------
# Main ingest for one file
# ---------------------------------------------------------------------------

def _ingest_common(bands, domain, model_source, model_run, valid_time, fhour, priority) -> bool:
    """Shared tail end of ingest_file()/ingest_file_rrfs() -- airport lookup,
    pixel projection, row-building, upsert. Source-agnostic: bands just
    needs the standard lowercase keys either loader produces."""
    raster_crs = bands['_crs']
    transform  = bands['_transform']
    shape      = bands['_shape']

    try:
        conn = psycopg2.connect(DB_DSN)
        conn.autocommit = False
        cur = conn.cursor()
    except Exception as e:
        log.error(f'DB connection failed: {e}')
        return False

    try:
        airports = get_airports(cur, domain)
        if not airports:
            log.warning(f'No airports found for domain {domain}')
            conn.close()
            return True

        log.info(f'Processing {len(airports)} airports for {domain}')

        grid_pts = airport_grid_coords(airports, raster_crs, transform, shape)
        log.info(f'{len(grid_pts)} airports within grid bounds')

        rows = []
        for airport_id, station_id, row, col, runway_hdg in grid_pts:
            impact = build_impact_row(
                airport_id, station_id, runway_hdg,
                bands, row, col,
                model_source, model_run, valid_time, fhour,
                priority
            )
            rows.append(impact)

        n = upsert_impacts(cur, rows)
        conn.commit()
        log.info(f'Upserted {n} rows for {domain} {model_source} F{fhour:02d}')
        conn.close()
        return True

    except Exception as e:
        log.error(f'Ingest error: {e}', exc_info=True)
        conn.rollback()
        conn.close()
        return False


def ingest_file(domain: str, cycle: int, fhour: int) -> bool:
    """Ingest one NAM OCONUS grib2 file for given domain/cycle/fhour."""

    filename = FILE_TEMPLATE[domain].format(cycle=cycle, fhour=fhour)
    grib_path = NAM_BASE / domain / filename

    if not grib_path.exists():
        log.error(f'File not found: {grib_path}')
        return False

    log.info(f'Ingesting {filename} ({grib_path.stat().st_size/1e6:.1f} MB)')

    # Determine model_run datetime from filename cycle
    # Use today's date — if cycle is in the future, use yesterday
    now_utc = datetime.now(timezone.utc)
    run_dt = now_utc.replace(hour=cycle, minute=0, second=0, microsecond=0)
    if run_dt > now_utc:
        run_dt -= timedelta(days=1)
    model_run  = run_dt
    valid_time = run_dt + timedelta(hours=fhour)

    bands = load_bands(grib_path)
    if not bands:
        return False

    return _ingest_common(bands, domain, MODEL_SOURCE[domain], model_run,
                           valid_time, fhour, SOURCE_PRIORITY[domain])


def fetch_rrfs_file(domain: str, date: str, cycle: str, fhour: int):
    """Fetch-and-cache one RRFS 2dfld curated subset for this domain/fhour
    via rrfs_wind_fetch.py's existing byte-range technique. Returns the
    local path, or None if not posted."""
    return rwf.fetch_and_cache(
        '2dfld', date, cycle, fhour, RRFS_WANTED_FIELDS, tag='namoconus',
        domain=RRFS_DOMAIN[domain], resolution=RRFS_RESOLUTION[domain],
    )


def ingest_file_rrfs(domain: str, date: str, cycle: str, fhour: int) -> bool:
    """RRFS counterpart to ingest_file() -- same shape, different source."""
    grib_path = fetch_rrfs_file(domain, date, cycle, fhour)
    if not grib_path:
        log.warning(f'RRFS not available for {domain} {date} {cycle}z F{fhour:03d}')
        return False

    model_run  = datetime.strptime(f'{date}{cycle}', '%Y%m%d%H').replace(tzinfo=timezone.utc)
    valid_time = model_run + timedelta(hours=fhour)

    bands = load_rrfs_bands(grib_path)
    if not bands:
        return False

    return _ingest_common(bands, domain, RRFS_MODEL_SOURCE[domain], model_run,
                           valid_time, fhour, SOURCE_PRIORITY[domain])

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

MAX_FHOUR = {'ak': 42, 'hi': 27, 'carib': 27}  # same ceiling for both sources -- see module docstring


def main():
    ap = argparse.ArgumentParser(description='Ingest OCONUS airport impacts (RRFS default, NAM fallback)')
    ap.add_argument('--domain', required=True, choices=['ak','hi','carib'],
                    help='Domain: ak, hi, or carib')
    ap.add_argument('--cycle', required=True, type=int, choices=[0,6,12,18],
                    help='Cycle hour (0/6/12/18) -- NAM path only; ignored for '
                         'RRFS (resolves the latest ready cycle itself), kept '
                         'required so the existing cron invocation needs no change')
    ap.add_argument('--fhour', required=False, type=int, default=0,
                    help='Forecast hour')
    ap.add_argument('--all-fhours', action='store_true',
                    help='Ingest all available forecast hours for domain/cycle')
    args = ap.parse_args()

    log.info(f'DATA_SOURCE={DATA_SOURCE} domain={args.domain}')

    if DATA_SOURCE == 'rrfs':
        date, cycle = rwf.find_ready_cycle(
            product='2dfld', domain=RRFS_DOMAIN[args.domain],
            resolution=RRFS_RESOLUTION[args.domain],
        )
        if not date:
            log.error(f'No ready RRFS cycle found for domain {args.domain} within lookback window')
            sys.exit(1)
        log.info(f'RRFS cycle: {date} {cycle}z')

        if args.all_fhours:
            # RRFS only extends past F18 on the 4 synoptic cycles (00/06/12/18Z)
            # -- an hourly off-cycle run (e.g. 21z) genuinely has nothing beyond
            # F18. Cap this run's ceiling to what the resolved cycle actually
            # has rather than looping into guaranteed "not posted" warnings
            # for every non-synoptic cycle (most of them).
            max_fh = MAX_FHOUR[args.domain]
            if cycle not in ('00', '06', '12', '18'):
                max_fh = min(max_fh, 18)
            ok = fail = 0
            for fh in range(0, max_fh + 1):  # hourly steps -- RRFS is hourly-native, unlike NAM's 3-hourly
                if ingest_file_rrfs(args.domain, date, cycle, fh):
                    ok += 1
                else:
                    fail += 1
            log.info(f'Batch complete: {ok} OK, {fail} failed')
            sys.exit(1 if fail > 0 else 0)
        else:
            success = ingest_file_rrfs(args.domain, date, cycle, args.fhour)
            sys.exit(0 if success else 1)
    else:
        if args.all_fhours:
            max_fh = MAX_FHOUR[args.domain]
            ok = fail = 0
            for fh in range(0, max_fh + 1, 3):
                if ingest_file(args.domain, args.cycle, fh):
                    ok += 1
                else:
                    fail += 1
            log.info(f'Batch complete: {ok} OK, {fail} failed')
            sys.exit(1 if fail > 0 else 0)
        else:
            success = ingest_file(args.domain, args.cycle, args.fhour)
            sys.exit(0 if success else 1)

if __name__ == '__main__':
    main()

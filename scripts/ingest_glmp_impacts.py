#!/usr/bin/env python3
"""
ingest_glmp_impacts.py — Extract GLMP forecasts for all qualifying CAP airfields
and evaluate CAPR 70-1 weather impacts stoplights.

Data sources:
  CONUS GLMP  — K airports + T airports (Caribbean/PR/USVI)
  Alaska GLMP — PA* airports within Alaska domain
  GFS 0.25°   — PH* (Hawaii), PG* (Guam) fallback

Airport filter: has_paved_runway=true AND longest_runway_ft >= 2500
Station prefix: K, T, P (US and territories only)

CAPR 70-1 thresholds evaluated:
  VFR Fixed Wing: wind, crosswind, ceiling, visibility, temp cold/hot,
                  wind chill, heat index
  IFR Fixed Wing: wind, crosswind, ceiling, visibility

Output table: observations.airport_wx_impacts (data2/PostgreSQL)

Run on data1 every 30 min (after fetch_glmp.py):
  2,32 * * * * /var/www/cap_winds_app/venv/bin/python3 \
    /var/www/cap_winds_app/scripts/ingest_glmp_impacts.py \
    >> /var/log/glmp_impacts.log 2>&1
"""

import os
import sys
import math
import fcntl
import logging
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pygrib
import psycopg2
import psycopg2.extras
from scipy.spatial import cKDTree

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
GLMP_BASE   = Path('/LDM/models/glmp')
GFS_BASE    = Path('/LDM/models/gfs/0p25')
LOCKFILE    = '/var/lock/ingest_glmp_impacts.lock'
DB_DSN      = os.environ.get('DB_DSN',
                             'dbname=avwx_data user=avwx_user host=192.168.0.60')

# GLMP grid domain bounds (from grid inspection)
CONUS_LAT_MIN, CONUS_LAT_MAX =  20.19,  52.81
CONUS_LON_MIN, CONUS_LON_MAX = -130.10, -60.89
AK_LAT_MIN                   =  50.0
AK_LON_MIN, AK_LON_MAX       = -180.0, -129.0

# GLMP variable files and their pygrib shortName/typeOfLevel
GLMP_VARS = {
    'fcsts_cig':  ('ceil',   'cloudCeiling',      0),
    'fcsts_t':    ('2t',     'heightAboveGround',  2),
    'fcsts_td':   ('2d',     'heightAboveGround',  2),
    'fcsts_vis':  ('vis',    'surface',            0),
    'fcsts_wdir': ('10wdir', 'heightAboveGround', 10),
    'fcsts_wgst': ('i10fg',  'heightAboveGround', 10),
    'fcsts_wspd': ('10si',   'heightAboveGround', 10),
}

# How many recent cycles to look back when finding latest complete cycle
CYCLE_LOOKBACK = 6   # 6 × 30min = 3 hours

# ---------------------------------------------------------------------------
# CAPR 70-1 Threshold evaluation
# ---------------------------------------------------------------------------
def color_wind(kts):
    if kts is None:       return 'UNKNOWN'
    if kts < 25:          return 'GREEN'
    if kts < 30:          return 'YELLOW'
    return 'RED'

def color_xwind_vfr(kts):
    if kts is None:       return 'UNKNOWN'
    if kts < 8:           return 'GREEN'
    if kts < 15:          return 'YELLOW'
    return 'RED'

def color_xwind_ifr(kts):
    if kts is None:       return 'UNKNOWN'
    if kts < 8:           return 'GREEN'
    if kts < 13:          return 'YELLOW'
    return 'RED'

def color_ceil_vfr(ft):
    if ft is None:        return 'UNKNOWN'
    if ft < 0 or ft == 99999: return 'GREEN'   # unlimited/clear
    if ft > 2000:         return 'GREEN'
    if ft >= 500:         return 'YELLOW'
    return 'RED'

def color_ceil_ifr(ft):
    if ft is None:        return 'UNKNOWN'
    if ft < 0 or ft == 99999: return 'GREEN'   # unlimited/clear
    if ft > 800:          return 'GREEN'
    if ft >= 500:         return 'YELLOW'
    return 'RED'

def color_vis(m):
    """Visibility in metres."""
    if m is None:         return 'UNKNOWN'
    if m > 3200:          return 'GREEN'
    if m >= 1600:         return 'YELLOW'
    return 'RED'

def color_temp_cold(f):
    if f is None:         return 'UNKNOWN'
    if f >= 20:           return 'GREEN'
    if f >= 10:           return 'YELLOW'
    return 'RED'

def color_temp_hot(f):
    if f is None:         return 'UNKNOWN'
    if f < 90:            return 'GREEN'
    if f <= 104:          return 'YELLOW'
    return 'RED'

def color_wind_chill(f):
    if f is None:         return 'UNKNOWN'
    if f > 40:            return 'GREEN'
    if f >= 22:           return 'YELLOW'
    return 'RED'

def color_heat_index(f):
    if f is None:         return 'UNKNOWN'
    if f <= 90:           return 'GREEN'
    if f <= 101:          return 'YELLOW'
    return 'RED'

COLOR_ORDER = {'RED': 0, 'YELLOW': 1, 'GREEN': 2, 'UNKNOWN': 3}

def worst_color(*colors):
    return min(colors, key=lambda c: COLOR_ORDER.get(c, 3))

def worst_param(params_dict):
    """Return name of worst parameter."""
    worst = 'GREEN'
    worst_name = None
    for name, color in params_dict.items():
        if COLOR_ORDER.get(color, 3) < COLOR_ORDER.get(worst, 3):
            worst = color
            worst_name = name
    return worst_name

# ---------------------------------------------------------------------------
# Derived fields
# ---------------------------------------------------------------------------
def kelvin_to_c(k):
    return k - 273.15 if k is not None else None

def c_to_f(c):
    return c * 9/5 + 32 if c is not None else None

def ms_to_kts(ms):
    return ms * 1.94384 if ms is not None else None

def heat_index_f(tmp_f, rh_pct):
    """NWS Rothfusz — only valid when tmp_f >= 80°F."""
    if tmp_f is None or tmp_f < 80 or rh_pct is None:
        return None
    hi = (-42.379 + 2.04901523*tmp_f + 10.14333127*rh_pct
          - 0.22475541*tmp_f*rh_pct - 0.00683783*tmp_f**2
          - 0.05481717*rh_pct**2 + 0.00122874*tmp_f**2*rh_pct
          + 0.00085282*tmp_f*rh_pct**2 - 0.00000199*tmp_f**2*rh_pct**2)
    return hi

def wind_chill_f(tmp_f, wind_mph):
    """NWS wind chill — only valid when tmp_f <= 50°F and wind >= 3 mph."""
    if tmp_f is None or tmp_f > 50 or wind_mph is None or wind_mph < 3:
        return None
    return (35.74 + 0.6215*tmp_f - 35.75*(wind_mph**0.16)
            + 0.4275*tmp_f*(wind_mph**0.16))

def relative_humidity(tmp_c, dpt_c):
    """Magnus formula RH %."""
    if tmp_c is None or dpt_c is None:
        return None
    return 100 * math.exp((17.625*dpt_c)/(243.04+dpt_c) -
                          (17.625*tmp_c)/(243.04+tmp_c))

def crosswind(wind_dir_deg, wind_speed_kts, runway_heading_deg):
    """Crosswind component in knots."""
    if any(v is None for v in [wind_dir_deg, wind_speed_kts, runway_heading_deg]):
        return None
    angle = math.radians(wind_dir_deg - runway_heading_deg)
    return abs(wind_speed_kts * math.sin(angle))

def best_runway_crosswind(wind_dir, wind_speed_kts, runway_headings):
    """
    Find runway end with minimum crosswind. Returns (min_xwind, best_heading).
    runway_headings: list of (le_heading, he_heading) tuples, either may be None.
    """
    if not runway_headings or wind_dir is None or wind_speed_kts is None:
        return None, None
    best_xw = None
    best_hdg = None
    for le_hdg, he_hdg in runway_headings:
        for hdg in [le_hdg, he_hdg]:
            if hdg is None:
                continue
            # Also check reciprocal if only one end stored
            xw = crosswind(wind_dir, wind_speed_kts, hdg)
            if xw is not None and (best_xw is None or xw < best_xw):
                best_xw = xw
                best_hdg = hdg
    return best_xw, best_hdg

# ---------------------------------------------------------------------------
# Grid assignment
# ---------------------------------------------------------------------------
def assign_grid(lat, lon):
    """
    Assign airport to GLMP grid sector based on coordinates.
    Returns 'co', 'ak', 'gfs', or None (if outside all known grids).
    """
    if lon > 180:
        lon -= 360
    if (CONUS_LAT_MIN <= lat <= CONUS_LAT_MAX and
            CONUS_LON_MIN <= lon <= CONUS_LON_MAX):
        return 'co'
    if lat >= AK_LAT_MIN and AK_LON_MIN <= lon <= AK_LON_MAX:
        return 'ak'
    # Hawaii (~19-22°N, -155 to -160°W) and Guam (~13°N, 144°E)
    return 'gfs'

# ---------------------------------------------------------------------------
# GLMP file discovery
# ---------------------------------------------------------------------------
def find_latest_glmp_cycle(sector):
    """
    Find most recent complete GLMP cycle for a given sector ('co' or 'ak').
    Returns (cycle_dt, file_dict) where file_dict maps var_name -> Path,
    or (None, None) if not found.
    """
    now_utc = datetime.now(timezone.utc)
    # Round down to nearest 30-min boundary
    minute = (now_utc.minute // 30) * 30
    candidate = now_utc.replace(minute=minute, second=0, microsecond=0)

    for i in range(CYCLE_LOOKBACK * 2):  # 30-min steps
        cycle_dt  = candidate - timedelta(minutes=30*i)
        date_str  = cycle_dt.strftime('%Y%m%d')
        cycle_str = cycle_dt.strftime('%H%Mz')
        date_dir  = GLMP_BASE / date_str

        if not date_dir.exists():
            continue

        # Check all 7 variable files are present
        files = {}
        complete = True
        for var in GLMP_VARS:
            fname = f'glmp_{date_str}_{cycle_str}_{sector}_{var}.grib2'
            fpath = date_dir / fname
            if not fpath.exists() or fpath.stat().st_size == 0:
                complete = False
                break
            files[var] = fpath

        if complete:
            log.info(f'Latest complete GLMP {sector.upper()} cycle: '
                     f'{date_str} {cycle_str}')
            return cycle_dt, files

    log.warning(f'No complete GLMP {sector.upper()} cycle found in last '
                f'{CYCLE_LOOKBACK} cycles')
    return None, None

# ---------------------------------------------------------------------------
# GLMP grid extraction
# ---------------------------------------------------------------------------
def load_glmp_grids(file_dict):
    """
    Load all GLMP variable files for one sector/cycle.
    Returns dict: var_name -> {step: numpy_array_flat}
    Also returns (tree, lats_shape) for spatial lookup.
    """
    grids  = {}
    tree   = None
    lshape = None

    for var, fpath in file_dict.items():
        shortname, type_of_level, level = GLMP_VARS[var]
        try:
            grbs = pygrib.open(str(fpath))
            msgs = grbs.select(shortName=shortname,
                               typeOfLevel=type_of_level,
                               level=level)
            var_grids = {}
            for msg in msgs:
                step = msg.stepRange
                if '-' in str(step):
                    # Accumulated field — use end step
                    step = int(str(step).split('-')[-1])
                else:
                    step = int(step)
                var_grids[step] = msg.values.ravel()

                # Build KDTree from first message
                if tree is None:
                    lats, lons = msg.latlons()
                    lons = np.where(lons > 180, lons - 360, lons)
                    pts  = np.column_stack([lats.ravel(), lons.ravel()])
                    tree = cKDTree(pts)
                    lshape = lats.shape

            grids[var] = var_grids
            grbs.close()
        except Exception as e:
            log.warning(f'Failed to load {var} from {fpath.name}: {e}')
            grids[var] = {}

    return grids, tree, lshape

def extract_at_index(grids, idx, step):
    """Extract all variable values at grid index for a given forecast step."""
    result = {}
    for var, step_dict in grids.items():
        val = step_dict.get(step)
        if val is not None:
            try:
                v = float(val[idx])
                result[var] = None if (v > 9e20 or np.isnan(v)) else v
            except (ValueError, TypeError):
                result[var] = None
        else:
            result[var] = None
    return result

# ---------------------------------------------------------------------------
# GFS fallback extraction (for Hawaii and Guam)
# ---------------------------------------------------------------------------
def find_latest_gfs_file(fhour):
    """Find the latest GFS file for a given forecast hour."""
    now_utc = datetime.now(timezone.utc)
    for hours_back in range(0, 25, 6):
        candidate  = now_utc - timedelta(hours=hours_back)
        cycle_hour = (candidate.hour // 6) * 6
        cycle_dt   = candidate.replace(hour=cycle_hour, minute=0,
                                       second=0, microsecond=0)
        date_str   = cycle_dt.strftime('%Y%m%d')
        hour_str   = cycle_dt.strftime('%H')
        fpath = GFS_BASE / date_str / \
                f'gfs_0p25_{date_str}_{hour_str}z_f{fhour:03d}.grib2'
        if fpath.exists() and fpath.stat().st_size > 0:
            return fpath, cycle_dt
    return None, None

# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS observations.airport_wx_impacts (
    id              SERIAL PRIMARY KEY,
    airport_id      INTEGER NOT NULL
                        REFERENCES observations.airports(id) ON DELETE CASCADE,
    station_id      VARCHAR(10) NOT NULL,
    model_source    VARCHAR(10) NOT NULL,  -- 'GLMP_CO', 'GLMP_AK', 'GFS'
    model_run       TIMESTAMPTZ NOT NULL,
    valid_time      TIMESTAMPTZ NOT NULL,
    forecast_hour   SMALLINT NOT NULL,
    -- Raw values
    ceil_ft         INTEGER,
    vis_m           INTEGER,
    wind_dir        SMALLINT,
    wind_speed_kts  REAL,
    wind_gust_kts   REAL,
    tmp_c           REAL,
    dpt_c           REAL,
    -- Derived
    tmp_f           REAL,
    heat_index_f    REAL,
    wind_chill_f    REAL,
    crosswind_kts   REAL,
    best_runway_hdg SMALLINT,
    -- VFR stoplight
    vfr_color       VARCHAR(10),
    vfr_worst_param VARCHAR(30),
    -- IFR stoplight
    ifr_color       VARCHAR(10),
    ifr_worst_param VARCHAR(30),
    ingested_at     TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (airport_id, model_run, forecast_hour)
);
CREATE INDEX IF NOT EXISTS airport_wx_impacts_airport_id_idx
    ON observations.airport_wx_impacts (airport_id);
CREATE INDEX IF NOT EXISTS airport_wx_impacts_valid_time_idx
    ON observations.airport_wx_impacts (valid_time);
CREATE INDEX IF NOT EXISTS airport_wx_impacts_station_id_idx
    ON observations.airport_wx_impacts (station_id);
"""

def get_airports(conn):
    """Fetch qualifying airports with runway headings and Wing ICL overrides."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT
                a.id,
                a.station_id,
                ST_Y(a.location) as lat,
                ST_X(a.location) as lon,
                a.elevation_ft,
                array_agg(
                    ARRAY[r.le_heading_degt, r.he_heading_degt]
                ) as runway_headings,
                wm.wing_id,
                wm.region_code,
                -- Wind ICL overrides (NULL = use national default)
                MAX(CASE WHEN wi.parameter = 'wind_vfr_yellow'
                    AND (wi.expires IS NULL OR wi.expires >= CURRENT_DATE)
                    THEN wi.threshold END) as icl_wind_vfr_yellow,
                MAX(CASE WHEN wi.parameter = 'wind_vfr_red'
                    AND (wi.expires IS NULL OR wi.expires >= CURRENT_DATE)
                    THEN wi.threshold END) as icl_wind_vfr_red,
                -- Crosswind VFR ICL overrides
                MAX(CASE WHEN wi.parameter = 'crosswind_vfr_yellow'
                    AND (wi.expires IS NULL OR wi.expires >= CURRENT_DATE)
                    THEN wi.threshold END) as icl_xwind_vfr_yellow,
                MAX(CASE WHEN wi.parameter = 'crosswind_vfr_red'
                    AND (wi.expires IS NULL OR wi.expires >= CURRENT_DATE)
                    THEN wi.threshold END) as icl_xwind_vfr_red,
                -- Crosswind IFR ICL overrides
                MAX(CASE WHEN wi.parameter = 'crosswind_ifr_yellow'
                    AND (wi.expires IS NULL OR wi.expires >= CURRENT_DATE)
                    THEN wi.threshold END) as icl_xwind_ifr_yellow,
                MAX(CASE WHEN wi.parameter = 'crosswind_ifr_red'
                    AND (wi.expires IS NULL OR wi.expires >= CURRENT_DATE)
                    THEN wi.threshold END) as icl_xwind_ifr_red
            FROM observations.airports a
            JOIN observations.runways r ON r.airport_id = a.id
            LEFT JOIN observations.wing_map wm ON wm.iso_region = a.iso_region
            LEFT JOIN observations.wing_icl wi ON wi.wing_id = wm.wing_id
            WHERE a.has_paved_runway = true
              AND a.longest_runway_ft >= 2500
              AND a.station_id ~ '^[KTP]'
              AND a.location IS NOT NULL
            GROUP BY a.id, a.station_id, a.location, a.elevation_ft,
                     wm.wing_id, wm.region_code
            ORDER BY a.station_id
        """)
        airports = cur.fetchall()
    conn.commit()  # release transaction immediately after read
    log.info(f'Loaded {len(airports)} qualifying airports with runway data')
    return airports

def upsert_impacts(conn, records):
    """Upsert a batch of impact records."""
    if not records:
        return 0
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO observations.airport_wx_impacts (
                airport_id, station_id, model_source, model_run, valid_time,
                forecast_hour, ceil_ft, vis_m, wind_dir, wind_speed_kts,
                wind_gust_kts, tmp_c, dpt_c, tmp_f, heat_index_f,
                wind_chill_f, crosswind_kts, best_runway_hdg,
                vfr_color, vfr_worst_param, ifr_color, ifr_worst_param,
                ingested_at
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
                ingested_at     = EXCLUDED.ingested_at
        """, [
            (r['airport_id'], r['station_id'], r['model_source'],
             r['model_run'], r['valid_time'], r['forecast_hour'],
             r['ceil_ft'], r['vis_m'], r['wind_dir'], r['wind_speed_kts'],
             r['wind_gust_kts'], r['tmp_c'], r['dpt_c'], r['tmp_f'],
             r['heat_index_f'], r['wind_chill_f'], r['crosswind_kts'],
             r['best_runway_hdg'], r['vfr_color'], r['vfr_worst_param'],
             r['ifr_color'], r['ifr_worst_param'], datetime.now(timezone.utc))
            for r in records
        ], page_size=500)
    conn.commit()
    return len(records)

# ---------------------------------------------------------------------------
# Stoplight evaluation
# ---------------------------------------------------------------------------
def evaluate_stoplights(raw, xwind_kts, icl=None):
    """
    Evaluate VFR and IFR stoplights from raw extracted values.
    raw: dict with keys matching GLMP_VARS plus derived fields.
    xwind_kts: crosswind component in knots.
    icl: dict of Wing ICL threshold overrides (None = use national defaults).
         Keys: wind_vfr_yellow, wind_vfr_red,
               crosswind_vfr_yellow, crosswind_vfr_red,
               crosswind_ifr_yellow, crosswind_ifr_red
    Returns dict with vfr_color, vfr_worst_param, ifr_color, ifr_worst_param.
    """
    if icl is None:
        icl = {}

    ceil_ft       = raw.get('ceil_ft')
    vis_m         = raw.get('vis_m')
    wind_kts      = raw.get('wind_speed_kts')
    wind_gust_kts = raw.get('wind_gust_kts')
    tmp_f         = raw.get('tmp_f')
    hi_f          = raw.get('heat_index_f')
    wc_f          = raw.get('wind_chill_f')

    # Use gust for wind limit check if higher than sustained
    wind_for_limit = max(
        v for v in [wind_kts, wind_gust_kts] if v is not None
    ) if any(v is not None for v in [wind_kts, wind_gust_kts]) else None

    # Apply ICL overrides — fall back to national defaults if not set
    # Wind thresholds
    wind_y = icl.get('wind_vfr_yellow') or 25.0
    wind_r = icl.get('wind_vfr_red')    or 30.0

    def color_wind_icl(kts):
        if kts is None: return 'UNKNOWN'
        if kts < wind_y: return 'GREEN'
        if kts < wind_r: return 'YELLOW'
        return 'RED'

    # Crosswind VFR thresholds
    xw_vfr_y = icl.get('crosswind_vfr_yellow') or 8.0
    xw_vfr_r = icl.get('crosswind_vfr_red')    or 15.0

    def color_xwind_vfr_icl(kts):
        if kts is None: return 'UNKNOWN'
        if kts < xw_vfr_y: return 'GREEN'
        if kts < xw_vfr_r: return 'YELLOW'
        return 'RED'

    # Crosswind IFR thresholds
    xw_ifr_y = icl.get('crosswind_ifr_yellow') or 8.0
    xw_ifr_r = icl.get('crosswind_ifr_red')    or 13.0

    def color_xwind_ifr_icl(kts):
        if kts is None: return 'UNKNOWN'
        if kts < xw_ifr_y: return 'GREEN'
        if kts < xw_ifr_r: return 'YELLOW'
        return 'RED'

    # VFR parameters
    vfr_params = {
        'wind':       color_wind_icl(wind_for_limit),
        'crosswind':  color_xwind_vfr_icl(xwind_kts),
        'ceiling':    color_ceil_vfr(ceil_ft),
        'visibility': color_vis(vis_m),
        'temp_cold':  color_temp_cold(tmp_f),
        'temp_hot':   color_temp_hot(tmp_f),
        'wind_chill': color_wind_chill(wc_f),
        'heat_index': color_heat_index(hi_f),
    }

    # IFR parameters
    ifr_params = {
        'wind':       color_wind_icl(wind_for_limit),
        'crosswind':  color_xwind_ifr_icl(xwind_kts),
        'ceiling':    color_ceil_ifr(ceil_ft),
        'visibility': color_vis(vis_m),
    }

    vfr_color = worst_color(*vfr_params.values())
    ifr_color = worst_color(*ifr_params.values())

    return {
        'vfr_color':       vfr_color,
        'vfr_worst_param': worst_param(vfr_params),
        'ifr_color':       ifr_color,
        'ifr_worst_param': worst_param(ifr_params),
    }

# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------
def process_sector(airports, sector, conn, now_utc):
    """Process all airports in a GLMP sector grid."""
    cycle_dt, file_dict = find_latest_glmp_cycle(sector)
    if cycle_dt is None:
        log.warning(f'No GLMP {sector.upper()} cycle available — skipping')
        return 0

    # Check if already ingested
    sector_airports = [a for a in airports
                       if assign_grid(a['lat'], a['lon']) == sector]
    if not sector_airports:
        log.info(f'No airports assigned to {sector.upper()} grid')
        return 0

    log.info(f'Processing {len(sector_airports)} airports on '
             f'GLMP {sector.upper()} grid')

    # Load all variable grids
    grids, tree, lshape = load_glmp_grids(file_dict)
    if tree is None:
        log.error(f'Failed to build KDTree for {sector.upper()} grid')
        return 0

    # Pre-compute grid indices for all sector airports (vectorized)
    coords = np.array([[a['lat'], a['lon']] for a in sector_airports])
    _, indices = tree.query(coords)

    # Get available forecast steps
    ref_var = next(iter(grids))
    steps   = sorted(grids[ref_var].keys())
    log.info(f'  Forecast steps available: F{steps[0]:03d}-F{steps[-1]:03d} '
             f'({len(steps)} hours)')

    records = []
    for step in steps:
        valid_time = cycle_dt + timedelta(hours=step)
        for i, apt in enumerate(sector_airports):
            idx  = indices[i]
            raw  = extract_at_index(grids, idx, step)

            # Unit conversions
            cig_raw    = raw.get('fcsts_cig')
            ceil_ft    = round(cig_raw * 3.28084) \
                         if cig_raw is not None else None
            # Negative ceiling = unlimited/clear sky — store as 99999
            if ceil_ft is not None and ceil_ft < 0:
                ceil_ft = 99999
            vis_raw    = raw.get('fcsts_vis')
            vis_m      = round(vis_raw) \
                         if vis_raw is not None else None
            wind_kts   = ms_to_kts(raw.get('fcsts_wspd'))
            gust_kts   = ms_to_kts(raw.get('fcsts_wgst'))
            wind_dir   = round(raw.get('fcsts_wdir')) \
                         if raw.get('fcsts_wdir') is not None else None
            tmp_c      = kelvin_to_c(raw.get('fcsts_t'))
            dpt_c      = kelvin_to_c(raw.get('fcsts_td'))
            tmp_f_val  = c_to_f(tmp_c)

            # Derived
            rh         = relative_humidity(tmp_c, dpt_c)
            hi_f_val   = heat_index_f(tmp_f_val, rh)
            wind_mph   = wind_kts * 1.15078 if wind_kts is not None else None
            wc_f_val   = wind_chill_f(tmp_f_val, wind_mph)

            # Crosswind
            rwy_hdgs   = [(r[0], r[1]) for r in apt['runway_headings']
                          if r is not None]
            xwind, best_hdg = best_runway_crosswind(wind_dir, wind_kts, rwy_hdgs)

            derived = {
                'ceil_ft':      ceil_ft,
                'vis_m':        vis_m,
                'wind_speed_kts': wind_kts,
                'wind_gust_kts':  gust_kts,
                'tmp_f':        tmp_f_val,
                'heat_index_f': hi_f_val,
                'wind_chill_f': wc_f_val,
            }
            # Build ICL override dict for this airport's wing
            icl = {
                'wind_vfr_yellow':      apt.get('icl_wind_vfr_yellow'),
                'wind_vfr_red':         apt.get('icl_wind_vfr_red'),
                'crosswind_vfr_yellow': apt.get('icl_xwind_vfr_yellow'),
                'crosswind_vfr_red':    apt.get('icl_xwind_vfr_red'),
                'crosswind_ifr_yellow': apt.get('icl_xwind_ifr_yellow'),
                'crosswind_ifr_red':    apt.get('icl_xwind_ifr_red'),
            }
            stoplights = evaluate_stoplights(derived, xwind, icl)

            records.append({
                'airport_id':    apt['id'],
                'station_id':    apt['station_id'],
                'model_source':  f'GLMP_{sector.upper()}',
                'model_run':     cycle_dt,
                'valid_time':    valid_time,
                'forecast_hour': step,
                'ceil_ft':       ceil_ft,
                'vis_m':         vis_m,
                'wind_dir':      wind_dir,
                'wind_speed_kts': round(wind_kts, 1) if wind_kts else None,
                'wind_gust_kts': round(gust_kts, 1) if gust_kts else None,
                'tmp_c':         round(tmp_c, 1) if tmp_c is not None else None,
                'dpt_c':         round(dpt_c, 1) if dpt_c is not None else None,
                'tmp_f':         round(tmp_f_val, 1) if tmp_f_val is not None else None,
                'heat_index_f':  round(hi_f_val, 1) if hi_f_val is not None else None,
                'wind_chill_f':  round(wc_f_val, 1) if wc_f_val is not None else None,
                'crosswind_kts': round(xwind, 1) if xwind is not None else None,
                'best_runway_hdg': round(best_hdg) if best_hdg is not None else None,
                **stoplights,
            })

        # Batch upsert per forecast hour
        if records:
            n = upsert_impacts(conn, records)
            if step % 6 == 0:
                log.info(f'  F{step:03d}: {n} records upserted')
            records = []

    return len(sector_airports) * len(steps)


def main():
    parser = argparse.ArgumentParser(
        description='Extract GLMP forecasts and evaluate CAPR 70-1 impacts')
    parser.add_argument('-v', '--verbose', action='store_true')
    parser.add_argument('--init-db', action='store_true',
                        help='Create DB table and exit')
    args = parser.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    # Lock file — prevent overlapping runs
    lock_fd = open(LOCKFILE, 'w')
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.warning('Another instance is running — exiting')
        return

    now_utc = datetime.now(timezone.utc)
    log.info('=' * 60)
    log.info(f'GLMP impacts ingest started: {now_utc.strftime("%Y-%m-%d %H:%MZ")}')

    conn = psycopg2.connect(DB_DSN)
    conn.autocommit = False

    try:
        # Create table if needed
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
        conn.commit()

        if args.init_db:
            log.info('DB table created/verified — exiting (--init-db)')
            return

        # Load airports
        airports = get_airports(conn)
        if not airports:
            log.error('No qualifying airports found')
            return

        total = 0

        # Process CONUS grid
        total += process_sector(airports, 'co', conn, now_utc)

        # Process Alaska grid (if files available)
        total += process_sector(airports, 'ak', conn, now_utc)

        # TODO: GFS fallback for PH*/PG* when needed
        # (low airport count, low priority until GLMP AK files confirmed)

        log.info(f'Total records processed: {total}')

    except Exception as e:
        conn.rollback()
        log.error(f'Fatal error: {e}', exc_info=True)
        raise
    finally:
        conn.close()
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()

    log.info('GLMP impacts ingest complete')
    log.info('=' * 60)


if __name__ == '__main__':
    main()

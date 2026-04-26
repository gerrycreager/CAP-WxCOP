#!/usr/bin/env python3
"""
ingest_glmp_impacts.py v2 — CAPR 70-1 Weather Impacts ingest from new NOMADS GLMP format.

Reads the new glmp.tHH30z.fcsts_*.grib2 files (one file per variable, all 25 hours)
and glmp.tHH30z.hr00_*.grib2 (analysis fields) from /LDM/models/glmp/YYYYMMDD/.

Variables ingested:
  fcsts_cig     — ceiling height (metres, -1=unlimited)
  fcsts_cigp3   — prob(ceil < 304.8m / 1000ft)    [VFR No-Go threshold]
  fcsts_vis     — visibility (metres)
  fcsts_visp2   — prob(vis < 1609m / 1SM)         [IFR No-Go threshold]
  fcsts_wspd    — wind speed (m/s → kts)
  fcsts_wdir    — wind direction (10s of degrees)
  fcsts_wgst    — wind gust (m/s → kts)
  fcsts_t       — 2m temperature (K → °F)
  fcsts_td      — 2m dewpoint (K → °F)

CAPF 70-1A Stoplight thresholds applied per airport runway heading:
  VFR: wind, crosswind, ceiling, visibility, temp cold, temp hot, wind chill
  IFR: wind, crosswind, ceiling, visibility

Wing ICL overrides applied (more conservative only).

Airport filter: has_paved_runway AND longest_runway_ft >= 2500

Output: observations.airport_wx_impacts (upsert)

Runs on data1 hourly, 15 min after fetch_glmp.py completes.
Cron: 0 * * * * (top of hour, fetch runs at :45 previous hour)
"""

import os
import sys
import re
import logging
import fcntl
import numpy as np
from datetime import datetime, timezone, timedelta
from pathlib import Path

import cfgrib
import psycopg2
from psycopg2.extras import execute_values
from scipy.spatial import KDTree

# ── Configuration ──────────────────────────────────────────────────────────────
GLMP_DIR        = Path('/LDM/models/glmp')
MAX_FORECAST_HR = 25
LOCKFILE        = '/home/ldm/var/run/ingest_glmp_impacts.lock'
CFGRIB_IDX_DIR  = '/tmp/cfgrib_glmp_idx'

DB_HOST = '192.168.0.60'
DB_NAME = 'avwx_data'
DB_USER = 'avwx_user'
DB_PASS = 'avwx_pass'

LOG_FILE = '/home/ldm/var/logs/glmp_impacts.log'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s,%(msecs)03d - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger(__name__)

# ── Unit conversions ───────────────────────────────────────────────────────────
def k_to_f(k):
    return (k - 273.15) * 9/5 + 32 if k is not None and k > 0 else None

def ms_to_kts(ms):
    return ms * 1.94384 if ms is not None and ms >= 0 else None

def m_to_ft(m):
    return m * 3.28084 if m is not None and m > 0 else None

def wdir_decode(raw):
    """Wind direction is in 10s of degrees in GLMP."""
    if raw is None or np.isnan(raw): return None
    return int(round(raw * 10)) % 360

def heat_index_f(tmp_f, dpt_f):
    """Simple Rothfusz heat index."""
    if tmp_f is None or dpt_f is None or tmp_f < 80: return None
    rh = 100 * (112 - 0.1*tmp_f + dpt_f) / (112 + 0.9*tmp_f)
    rh = max(0, min(100, rh))
    hi = (-42.379 + 2.04901523*tmp_f + 10.14333127*rh
          - 0.22475541*tmp_f*rh - 0.00683783*tmp_f**2
          - 0.05481717*rh**2 + 0.00122874*tmp_f**2*rh
          + 0.00085282*tmp_f*rh**2 - 0.00000199*tmp_f**2*rh**2)
    return hi if hi > tmp_f else None

def wind_chill_f(tmp_f, wind_kts):
    """NWS wind chill formula."""
    if tmp_f is None or wind_kts is None or tmp_f > 50 or wind_kts < 3:
        return None
    wind_mph = wind_kts * 1.15078
    return (35.74 + 0.6215*tmp_f - 35.75*(wind_mph**0.16)
            + 0.4275*tmp_f*(wind_mph**0.16))

def crosswind_kts(wind_spd_kts, wind_dir_deg, rwy_hdg_deg):
    """Crosswind component for a runway."""
    if any(v is None for v in [wind_spd_kts, wind_dir_deg, rwy_hdg_deg]):
        return None
    import math
    angle = math.radians(abs(wind_dir_deg - rwy_hdg_deg))
    return abs(wind_spd_kts * math.sin(angle))

# ── CAPR 70-1 / CAPF 70-1A Stoplight thresholds ───────────────────────────────
COLOR_ORDER = {'RED': 0, 'YELLOW': 1, 'GREEN': 2, 'UNKNOWN': 3}

def worst_color(*colors):
    return min(colors, key=lambda c: COLOR_ORDER.get(c, 3))

def worst_param_name(params_dict):
    worst = 'GREEN'; name = None
    for k, v in params_dict.items():
        if COLOR_ORDER.get(v, 3) < COLOR_ORDER.get(worst, 3):
            worst = v; name = k
    return name

def source_priority(src):
    return {'GLMP_CO': 1, 'GLMP_AK': 1, 'GLMP_HI': 1, 'GLMP_PR': 1,
            'HRRR': 2, 'AIGFS': 3, 'LAMP': 4}.get(src, 9)

def compute_colors(ceil_ft, vis_m, wind_kts, xwind_kts, tmp_f, hi_f, wc_f, icl):
    """
    Compute VFR/IFR stoplights per CAPR 70-1 / CAPF 70-1A.
    icl dict has optional override thresholds (more conservative only).
    """
    # ── ICL-adjusted thresholds ───────────────────────────────────────────
    # Wind
    wind_y  = icl.get('wind_vfr_yellow') or 21.0
    wind_r  = icl.get('wind_vfr_red')    or 30.0
    # Crosswind VFR
    xw_vfr_y = icl.get('crosswind_vfr_yellow') or 8.0
    xw_vfr_r = icl.get('crosswind_vfr_red')    or 15.0
    # Crosswind IFR
    xw_ifr_y = icl.get('crosswind_ifr_yellow') or 8.0
    xw_ifr_r = icl.get('crosswind_ifr_red')    or 13.0
    # Ceiling VFR: GREEN>2000ft, YELLOW 1000-2000ft, RED<1000ft
    ceil_vfr_y = icl.get('ceil_vfr_yellow') or 2000.0
    ceil_vfr_r = icl.get('ceil_vfr_red')    or 1000.0
    # Ceiling IFR: GREEN>800ft, YELLOW 500-800ft, RED<500ft
    ceil_ifr_y = 800.0
    ceil_ifr_r = 500.0
    # Visibility VFR: GREEN>3SM, YELLOW 1-3SM, RED<1SM
    SM = 1609.344
    vis_vfr_y = (icl.get('vis_vfr_yellow') or 3.0) * SM
    vis_vfr_r = (icl.get('vis_vfr_red')    or 1.0) * SM
    # Visibility IFR: GREEN>1SM, YELLOW 0.5-1SM, RED<0.5SM
    vis_ifr_y = 1.0 * SM
    vis_ifr_r = 0.5 * SM
    # Cold temp
    tmp_cold_y = icl.get('temp_cold_yellow') or 20.0
    tmp_cold_r = icl.get('temp_cold_red')    or -10.0
    # Hot temp (not ICL-overridable per CAPR 70-1)
    tmp_hot_y  = 90.0
    tmp_hot_r  = 104.0
    # Wind chill (CAPF 70-1A)
    wc_y = 22.0
    wc_r = 0.0

    def c_wind(kts):
        if kts is None: return 'UNKNOWN'
        return 'RED' if kts > wind_r else 'YELLOW' if kts > wind_y else 'GREEN'

    def c_xwind_vfr(kts):
        if kts is None: return 'UNKNOWN'
        return 'RED' if kts > xw_vfr_r else 'YELLOW' if kts > xw_vfr_y else 'GREEN'

    def c_xwind_ifr(kts):
        if kts is None: return 'UNKNOWN'
        return 'RED' if kts > xw_ifr_r else 'YELLOW' if kts > xw_ifr_y else 'GREEN'

    def c_ceil_vfr(ft):
        if ft is None: return 'UNKNOWN'
        if ft < 0 or ft > 12000: return 'GREEN'   # -1 = unlimited
        return 'RED' if ft < ceil_vfr_r else 'YELLOW' if ft <= ceil_vfr_y else 'GREEN'

    def c_ceil_ifr(ft):
        if ft is None: return 'UNKNOWN'
        if ft < 0 or ft > 12000: return 'GREEN'
        return 'RED' if ft < ceil_ifr_r else 'YELLOW' if ft <= ceil_ifr_y else 'GREEN'

    def c_vis_vfr(m):
        if m is None: return 'UNKNOWN'
        return 'RED' if m < vis_vfr_r else 'YELLOW' if m <= vis_vfr_y else 'GREEN'

    def c_vis_ifr(m):
        if m is None: return 'UNKNOWN'
        return 'RED' if m < vis_ifr_r else 'YELLOW' if m <= vis_ifr_y else 'GREEN'

    def c_tmp_cold(f):
        if f is None: return 'UNKNOWN'
        return 'RED' if f < tmp_cold_r else 'YELLOW' if f <= tmp_cold_y else 'GREEN'

    def c_tmp_hot(f):
        if f is None: return 'UNKNOWN'
        return 'RED' if f > tmp_hot_r else 'YELLOW' if f >= tmp_hot_y else 'GREEN'

    def c_wind_chill(f):
        if f is None: return 'GREEN'   # no chill = no restriction
        return 'RED' if f < wc_r else 'YELLOW' if f <= wc_y else 'GREEN'

    vfr_params = {
        'wind':       c_wind(wind_kts),
        'crosswind':  c_xwind_vfr(xwind_kts),
        'ceiling':    c_ceil_vfr(ceil_ft),
        'visibility': c_vis_vfr(vis_m),
        'temp_cold':  c_tmp_cold(tmp_f),
        'temp_hot':   c_tmp_hot(tmp_f),
        'wind_chill': c_wind_chill(wc_f),
        # heat_index excluded from VFR stoplight per CAPR 70-1
    }
    ifr_params = {
        'wind':       c_wind(wind_kts),
        'crosswind':  c_xwind_ifr(xwind_kts),
        'ceiling':    c_ceil_ifr(ceil_ft),
        'visibility': c_vis_ifr(vis_m),
    }

    return {
        'vfr_color':       worst_color(*vfr_params.values()),
        'vfr_worst_param': worst_param_name(vfr_params),
        'ifr_color':       worst_color(*ifr_params.values()),
        'ifr_worst_param': worst_param_name(ifr_params),
    }

# ── Database ───────────────────────────────────────────────────────────────────
def get_conn():
    return psycopg2.connect(host=DB_HOST, dbname=DB_NAME,
                            user=DB_USER, password=DB_PASS)

def load_airports(conn):
    """Load airports with runways and Wing ICL overrides."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                a.id, a.station_id,
                ST_X(a.location::geometry) AS lon,
                ST_Y(a.location::geometry) AS lat,
                a.is_military,
                r.le_heading_degt, r.he_heading_degt,
                r.le_length_ft,
                -- Wing ICL overrides
                MAX(CASE WHEN wi.parameter='wind_vfr_yellow'      THEN wi.threshold END) as wind_vfr_y,
                MAX(CASE WHEN wi.parameter='wind_vfr_red'         THEN wi.threshold END) as wind_vfr_r,
                MAX(CASE WHEN wi.parameter='crosswind_vfr_yellow' THEN wi.threshold END) as xw_vfr_y,
                MAX(CASE WHEN wi.parameter='crosswind_vfr_red'    THEN wi.threshold END) as xw_vfr_r,
                MAX(CASE WHEN wi.parameter='crosswind_ifr_yellow' THEN wi.threshold END) as xw_ifr_y,
                MAX(CASE WHEN wi.parameter='crosswind_ifr_red'    THEN wi.threshold END) as xw_ifr_r,
                MAX(CASE WHEN wi.parameter='ceil_vfr_yellow'      THEN wi.threshold END) as ceil_vfr_y,
                MAX(CASE WHEN wi.parameter='ceil_vfr_red'         THEN wi.threshold END) as ceil_vfr_r,
                MAX(CASE WHEN wi.parameter='vis_vfr_yellow'       THEN wi.threshold END) as vis_vfr_y,
                MAX(CASE WHEN wi.parameter='vis_vfr_red'          THEN wi.threshold END) as vis_vfr_r,
                MAX(CASE WHEN wi.parameter='temp_cold_yellow'     THEN wi.threshold END) as tmp_cold_y,
                MAX(CASE WHEN wi.parameter='temp_cold_red'        THEN wi.threshold END) as tmp_cold_r
            FROM observations.airports a
            JOIN observations.runways r ON r.airport_id = a.id
            LEFT JOIN observations.wing_map wm ON wm.iso_region = a.iso_region
            LEFT JOIN observations.wing_icl wi ON (
                wi.wing_id = wm.wing_id OR wi.wing_id = wm.region_code
            ) AND (wi.expires IS NULL OR wi.expires > NOW())
            WHERE a.has_paved_runway = true
              AND r.le_length_ft >= 2500
              AND r.surface NOT ILIKE '%water%'
              AND r.closed = false
              AND a.location IS NOT NULL
            GROUP BY a.id, a.station_id, a.location, a.is_military,
                     r.le_heading_degt, r.he_heading_degt, r.le_length_ft
            ORDER BY a.station_id
        """)
        rows = cur.fetchall()
    conn.commit()
    log.info(f"Loaded {len(rows)} qualifying airport/runway combinations")
    return rows

# ── GLMP grid / KDTree ─────────────────────────────────────────────────────────
def build_kdtree_from_file(grib_file):
    """Build KDTree from a GLMP grib2 file's lat/lon grid."""
    os.makedirs(CFGRIB_IDX_DIR, exist_ok=True)
    idx_path = f"{CFGRIB_IDX_DIR}/{Path(grib_file).name}.idx"
    try:
        ds = cfgrib.open_dataset(str(grib_file), engine='cfgrib',
                                 backend_kwargs={'squeeze': True,
                                                 'indexing': {'filename': idx_path}})
    except Exception:
        ds = cfgrib.open_dataset(str(grib_file), engine='cfgrib',
                                 backend_kwargs={'squeeze': True})

    lats = ds['latitude'].values
    lons = ds['longitude'].values
    lons = np.where(lons > 180, lons - 360, lons)
    ds.close()

    flat_lats = lats.flatten()
    flat_lons = lons.flatten()
    tree = KDTree(np.column_stack([flat_lats, flat_lons]))
    log.info(f"KDTree built: {lats.shape[0]}×{lats.shape[1]} = {lats.size:,} points")
    return tree, flat_lats, flat_lons, lats.shape

def get_airport_grid_indices(tree, airports):
    """Map each airport to nearest grid point."""
    coords = np.array([(lat, lon) for _, _, lon, lat, *_ in airports])
    _, indices = tree.query(coords, workers=-1)
    return indices

def read_grib_all_hours(grib_file, n_hours=MAX_FORECAST_HR):
    """
    Read a fcsts_*.grib2 file containing all forecast hours.
    Returns numpy array shape (n_hours, n_grid_points).
    """
    os.makedirs(CFGRIB_IDX_DIR, exist_ok=True)
    idx_path = f"{CFGRIB_IDX_DIR}/{Path(grib_file).name}.idx"
    try:
        ds = cfgrib.open_dataset(str(grib_file), engine='cfgrib',
                                 backend_kwargs={'squeeze': False,
                                                 'indexing': {'filename': idx_path}})
    except Exception:
        ds = cfgrib.open_dataset(str(grib_file), engine='cfgrib',
                                 backend_kwargs={'squeeze': False})

    var = list(ds.data_vars)[0]
    data = ds[var].values  # may be (1,25,1,ny,nx) or (ny,nx) etc.
    ds.close()

    # Squeeze out all singleton dimensions
    data = np.squeeze(data)

    # After squeeze: should be (n_steps, ny, nx) or (ny, nx)
    if data.ndim == 2:
        data = data[np.newaxis, :]   # add step dim

    # Now (n_steps, ny, nx)
    n_steps = data.shape[0]
    ny      = data.shape[1]
    nx      = data.shape[2]
    use = min(n_hours, n_steps)
    return data[:use].reshape(use, ny * nx)

def read_grib_single(grib_file):
    """Read a single-field grib2 file (hr00_* analysis). Returns flat array."""
    os.makedirs(CFGRIB_IDX_DIR, exist_ok=True)
    idx_path = f"{CFGRIB_IDX_DIR}/{Path(grib_file).name}.idx"
    try:
        ds = cfgrib.open_dataset(str(grib_file), engine='cfgrib',
                                 backend_kwargs={'squeeze': True,
                                                 'indexing': {'filename': idx_path}})
    except Exception:
        ds = cfgrib.open_dataset(str(grib_file), engine='cfgrib',
                                 backend_kwargs={'squeeze': True})
    var = list(ds.data_vars)[0]
    data = ds[var].values.flatten()
    ds.close()
    return data

# ── Find latest GLMP cycle ─────────────────────────────────────────────────────
def find_latest_cycle(sector='co'):
    """Find latest cycle with all required files present.
    Filename pattern: glmp_tHH30z_fcsts_VAR.g.SECTOR.grib2
    e.g. glmp_t1930z_fcsts_cig.g.co.grib2
    Cycles run at :30 past each hour.
    """
    now = datetime.now(timezone.utc)
    required_vars = [f'fcsts_cig.g.{sector}.grib2',
                     f'fcsts_wspd.g.{sector}.grib2']

    for delta in range(6):
        candidate = now - timedelta(hours=delta)
        date_str  = candidate.strftime('%Y%m%d')
        cstr      = f"t{candidate.hour:02d}30z"   # e.g. t1930z
        date_dir  = GLMP_DIR / date_str

        if not date_dir.exists():
            continue

        all_present = all(
            (date_dir / f"glmp_{cstr}_{var}").exists()
            for var in required_vars
        )
        if all_present:
            cycle_dt = candidate.replace(minute=0, second=0, microsecond=0)
            return date_dir, cycle_dt, cstr, date_str, f'GLMP_{sector.upper()}'

    return None, None, None, None, None

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    # Lock
    os.makedirs(os.path.dirname(LOCKFILE), exist_ok=True)
    lock_fd = open(LOCKFILE, 'w')
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        log.warning("Another instance is running — exiting")
        sys.exit(0)

    log.info("=" * 60)
    log.info(f"GLMP impacts ingest started: "
             f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%MZ')}")
    t0 = datetime.now()

    conn = get_conn()
    airports = load_airports(conn)
    if not airports:
        log.error("No qualifying airports found")
        conn.close()
        sys.exit(1)

    total = 0

    for sector_code, sector_name in [('co', 'GLMP_CO'), ('ak', 'GLMP_AK')]:
        date_dir, cycle_dt, cstr, date_str, src = find_latest_cycle(sector_code)
        if not date_dir:
            log.warning(f"No complete {sector_name} cycle found — skipping")
            continue

        log.info(f"{sector_name}: cycle {cstr} in {date_dir}")

        def f(name):
            # Pattern: glmp_tHH30z_fcsts_VAR.g.SECTOR.grib2
            return date_dir / f"glmp_{cstr}_{name}.g.{sector_code}.grib2"

        # Build KDTree from ceiling file
        cig_file = f('fcsts_cig')
        if not cig_file.exists():
            log.warning(f"Missing {cig_file} — skipping {sector_name}")
            continue

        tree, flat_lats, flat_lons, shape = build_kdtree_from_file(cig_file)
        indices = get_airport_grid_indices(tree, airports)

        # Read all variables
        def read_var(name, multi=True):
            fp = f(name)
            if not fp.exists():
                log.warning(f"Missing {fp.name}")
                return None
            try:
                return read_grib_all_hours(fp) if multi else read_grib_single(fp)
            except Exception as e:
                log.warning(f"Error reading {fp.name}: {e}")
                return None

        cig_data  = read_var('fcsts_cig')   # metres, -1=unlimited
        vis_data  = read_var('fcsts_vis')   # metres
        wspd_data = read_var('fcsts_wspd')  # m/s
        wdir_data = read_var('fcsts_wdir')  # 10s of degrees
        wgst_data = read_var('fcsts_wgst')  # m/s
        t_data    = read_var('fcsts_t')     # Kelvin
        td_data   = read_var('fcsts_td')    # Kelvin

        if cig_data is None or vis_data is None or wspd_data is None:
            log.error(f"Missing critical files for {sector_name} — skipping")
            continue

        # This replaces the "Process each forecast hour" block
        # ── Pre-extract airport arrays from the airports list ─────────────────────────
        n_apts = len(airports)
        apt_ids   = np.array([a[0] for a in airports], dtype=np.int64)
        apt_sids  = [a[1] for a in airports]
        apt_le_hdg = np.array([a[5] if a[5] is not None else np.nan for a in airports])
        apt_he_hdg = np.array([a[6] if a[6] is not None else np.nan for a in airports])

        # ICL thresholds — per airport arrays
        def icl_arr(idx, default):
            return np.array([a[idx] if a[idx] is not None else default for a in airports])

        icl_wind_y   = icl_arr(8,  21.0)
        icl_wind_r   = icl_arr(9,  30.0)
        icl_xw_vfr_y = icl_arr(10,  8.0)
        icl_xw_vfr_r = icl_arr(11, 15.0)
        icl_xw_ifr_y = icl_arr(12,  8.0)
        icl_xw_ifr_r = icl_arr(13, 13.0)
        icl_ceil_y   = icl_arr(14, 2000.0)
        icl_ceil_r   = icl_arr(15, 1000.0)
        icl_vis_y    = icl_arr(16, 3.0) * 1609.344
        icl_vis_r    = icl_arr(17, 1.0) * 1609.344
        icl_tc_y     = icl_arr(18, 20.0)
        icl_tc_r     = icl_arr(19, -10.0)

        spriority = source_priority(src)
        now_utc = datetime.now(timezone.utc)

        # ── Process each forecast hour using vectorized numpy ─────────────────────────
        for fhr_idx in range(min(MAX_FORECAST_HR, cig_data.shape[0])):
            fhr = fhr_idx + 1
            valid_time = cycle_dt + timedelta(hours=fhr)

            # Extract all airport values in one array index operation
            cig_v  = cig_data[fhr_idx, indices].astype(float)
            vis_v  = vis_data[fhr_idx, indices].astype(float)
            wspd_v = wspd_data[fhr_idx, indices].astype(float) if wspd_data is not None else np.full(n_apts, np.nan)
            wdir_v = wdir_data[fhr_idx, indices].astype(float) if wdir_data is not None else np.full(n_apts, np.nan)
            wgst_v = wgst_data[fhr_idx, indices].astype(float) if wgst_data is not None else np.full(n_apts, np.nan)
            t_v    = t_data[fhr_idx, indices].astype(float)    if t_data    is not None else np.full(n_apts, np.nan)
            td_v   = td_data[fhr_idx, indices].astype(float)   if td_data   is not None else np.full(n_apts, np.nan)

            # Convert — vectorized
            # Ceiling: NaN or <0 → 99999 (unlimited)
            ceil_ft = np.where(np.isnan(cig_v) | (cig_v < 0), 99999.0, cig_v * 3.28084)
            ceil_ft = np.where(ceil_ft > 50000, 99999.0, ceil_ft)

            # Visibility: NaN → 99999
            vis_m = np.where(np.isnan(vis_v), 99999.0, vis_v)

            # Wind: m/s → kts
            wind_kts = np.where(np.isnan(wspd_v), np.nan, wspd_v * 1.94384)
            gust_kts = np.where(np.isnan(wgst_v), np.nan, wgst_v * 1.94384)

            # Wind direction: 10s of degrees → degrees
            wdir_deg = np.where(np.isnan(wdir_v), np.nan, (wdir_v * 10) % 360)

            # Temperature: K → F
            tmp_f = np.where(np.isnan(t_v),  np.nan, (t_v  - 273.15) * 9/5 + 32)
            dpt_f = np.where(np.isnan(td_v), np.nan, (td_v - 273.15) * 9/5 + 32)

            # Best runway: primary=max headwind, secondary=min crosswind
            def xwind_vec(hdg_arr):
                return np.abs(wind_kts * np.sin(np.deg2rad(wdir_deg - hdg_arr)))
            def headwind_vec(hdg_arr):
                return wind_kts * np.cos(np.deg2rad(wdir_deg - hdg_arr))
            nan_le = np.isnan(apt_le_hdg) | np.isnan(wind_kts)
            nan_he = np.isnan(apt_he_hdg) | np.isnan(wind_kts)
            xw_le = np.where(nan_le, np.nan, xwind_vec(apt_le_hdg))
            xw_he = np.where(nan_he, np.nan, xwind_vec(apt_he_hdg))
            hw_le = np.where(nan_le, np.nan, headwind_vec(apt_le_hdg))
            hw_he = np.where(nan_he, np.nan, headwind_vec(apt_he_hdg))
            # Use he_end if: more headwind, or equal HW and less crosswind
            hw_diff = np.where(~nan_le & ~nan_he, hw_he - hw_le, np.nan)
            use_he = (
                (~nan_he & (np.nan_to_num(hw_diff) > 0.5)) |
                (~nan_he & ~nan_le & (np.abs(np.nan_to_num(hw_diff)) <= 0.5) & (xw_he < xw_le)) |
                (nan_le & ~nan_he)
            )
            xw = np.where(use_he, xw_he, xw_le)
            xw = np.where(np.isnan(xw), np.fmin(xw_le, xw_he), xw)
            best_hdg = np.where(use_he, apt_he_hdg, apt_le_hdg)
            # Wind chill (NWS formula) — vectorized
            wind_mph = wind_kts * 1.15078
            wc_valid = (tmp_f <= 50) & (wind_mph >= 3) & ~np.isnan(tmp_f) & ~np.isnan(wind_kts)
            wc_f = np.where(wc_valid,
                            35.74 + 0.6215*tmp_f - 35.75*(wind_mph**0.16) + 0.4275*tmp_f*(wind_mph**0.16),
                            np.nan)

            # Heat index (simplified Rothfusz) — vectorized
            rh = 100 * (112 - 0.1*tmp_f + dpt_f) / (112 + 0.9*tmp_f)
            rh = np.clip(rh, 0, 100)
            hi_raw = (-42.379 + 2.04901523*tmp_f + 10.14333127*rh
                      - 0.22475541*tmp_f*rh - 0.00683783*tmp_f**2
                      - 0.05481717*rh**2 + 0.00122874*tmp_f**2*rh
                      + 0.00085282*tmp_f*rh**2 - 0.00000199*tmp_f**2*rh**2)
            hi_valid = (tmp_f >= 80) & ~np.isnan(tmp_f) & ~np.isnan(dpt_f) & (hi_raw > tmp_f)
            hi_f = np.where(hi_valid, hi_raw, np.nan)

            # ── Vectorized stoplight colors ───────────────────────────────────────
            # Using integer encoding: RED=0, YELLOW=1, GREEN=2, UNKNOWN=3
            RED=0; YELLOW=1; GREEN=2; UNK=3

            def c_wind(kts):
                r = np.full(n_apts, GREEN)
                r = np.where(kts > icl_wind_y, YELLOW, r)
                r = np.where(kts > icl_wind_r, RED, r)
                r = np.where(np.isnan(kts), UNK, r)
                return r

            def c_xwind_vfr(kts):
                r = np.full(n_apts, GREEN)
                r = np.where(kts > icl_xw_vfr_y, YELLOW, r)
                r = np.where(kts > icl_xw_vfr_r, RED, r)
                r = np.where(np.isnan(kts), UNK, r)
                return r

            def c_xwind_ifr(kts):
                r = np.full(n_apts, GREEN)
                r = np.where(kts > icl_xw_ifr_y, YELLOW, r)
                r = np.where(kts > icl_xw_ifr_r, RED, r)
                r = np.where(np.isnan(kts), UNK, r)
                return r

            def c_ceil_vfr(ft):
                r = np.full(n_apts, GREEN)
                r = np.where(ft <= icl_ceil_y, YELLOW, r)
                r = np.where(ft < icl_ceil_r, RED, r)
                r = np.where(ft >= 99999, GREEN, r)   # unlimited = clear
                return r

            def c_ceil_ifr(ft):
                r = np.full(n_apts, GREEN)
                r = np.where(ft <= 800, YELLOW, r)
                r = np.where(ft < 500, RED, r)
                r = np.where(ft >= 99999, GREEN, r)
                return r

            def c_vis_vfr(m):
                r = np.full(n_apts, GREEN)
                r = np.where(m <= icl_vis_y, YELLOW, r)
                r = np.where(m < icl_vis_r, RED, r)
                r = np.where(m >= 99999, GREEN, r)
                return r

            def c_vis_ifr(m):
                sm = 1609.344
                r = np.full(n_apts, GREEN)
                r = np.where(m <= 1.0*sm, YELLOW, r)
                r = np.where(m < 0.5*sm, RED, r)
                r = np.where(m >= 99999, GREEN, r)
                return r

            def c_tmp_cold(f):
                r = np.full(n_apts, GREEN)
                r = np.where(f <= icl_tc_y, YELLOW, r)
                r = np.where(f < icl_tc_r, RED, r)
                r = np.where(np.isnan(f), UNK, r)
                return r

            def c_tmp_hot(f):
                r = np.full(n_apts, GREEN)
                r = np.where(f >= 90, YELLOW, r)
                r = np.where(f > 104, RED, r)
                r = np.where(np.isnan(f), UNK, r)
                return r

            def c_wc(f):
                r = np.full(n_apts, GREEN)
                r = np.where(f <= 22, YELLOW, r)
                r = np.where(f < 0, RED, r)
                r = np.where(np.isnan(f), GREEN, r)  # no chill = no restriction
                return r

            # VFR params array: shape (n_params, n_apts)
            vfr_stack = np.stack([
                c_wind(wind_kts),
                c_xwind_vfr(xw),
                c_ceil_vfr(ceil_ft),
                c_vis_vfr(vis_m),
                c_tmp_cold(tmp_f),
                c_tmp_hot(tmp_f),
                c_wc(wc_f),
            ])
            VFR_NAMES = ['wind','crosswind','ceiling','visibility','temp_cold','temp_hot','wind_chill']

            ifr_stack = np.stack([
                c_wind(wind_kts),
                c_xwind_ifr(xw),
                c_ceil_ifr(ceil_ft),
                c_vis_ifr(vis_m),
            ])
            IFR_NAMES = ['wind','crosswind','ceiling','visibility']

            # Worst color = minimum value per airport
            vfr_worst_idx = np.argmin(vfr_stack, axis=0)
            ifr_worst_idx = np.argmin(ifr_stack, axis=0)
            vfr_color_int = vfr_stack[vfr_worst_idx, np.arange(n_apts)]
            ifr_color_int = ifr_stack[ifr_worst_idx, np.arange(n_apts)]

            INT_TO_COLOR = {RED:'RED', YELLOW:'YELLOW', GREEN:'GREEN', UNK:'UNKNOWN'}

            # ── Build rows ─────────────────────────────────────────────────────────
            # Deduplicate by airport: keep min crosswind row
            # Since we computed one row per airport already (not per runway),
            # we already have best runway selected above. No dedup needed.
            rows = []
            for i in range(n_apts):
                aid = int(apt_ids[i])
                sid = apt_sids[i]

                cf  = ceil_ft[i]
                vm  = vis_m[i]
                wk  = wind_kts[i]
                wd  = wdir_deg[i]
                gk  = gust_kts[i]
                tf  = tmp_f[i]
                df  = dpt_f[i]
                hf  = hi_f[i]
                wf  = wc_f[i]
                xwv = xw[i]
                bh  = best_hdg[i]

                def fn(v):  # float or None
                    return None if (v is None or (isinstance(v, float) and np.isnan(v))) else float(v)
                def rn(v, d=1):
                    fv = fn(v)
                    return round(fv, d) if fv is not None else None
                def inn(v):
                    fv = fn(v)
                    return int(fv) if fv is not None else None

                vc = INT_TO_COLOR[int(vfr_color_int[i])]
                ic = INT_TO_COLOR[int(ifr_color_int[i])]
                vw = VFR_NAMES[int(vfr_worst_idx[i])] if vc != 'GREEN' else None
                iw = IFR_NAMES[int(ifr_worst_idx[i])] if ic != 'GREEN' else None

                rows.append((
                    aid, sid, src, cycle_dt, valid_time, fhr,
                    inn(cf) if cf != 99999.0 else None,
                    inn(vm) if vm != 99999.0 else None,
                    rn(wk), inn(wd), rn(gk),
                    rn(tf), rn(df), rn(tf),
                    rn(hf), rn(wf),
                    rn(xwv),
                    inn(bh),
                    vc, vw, ic, iw,
                    spriority,
                    now_utc
                ))


            with conn.cursor() as cur:
                # Deduplicate by airport_id — keep min crosswind row
                seen = {}
                for row in rows:
                    key = row[0]  # airport_id
                    if key not in seen:
                        seen[key] = row
                    else:
                        xw_new = row[16] if row[16] is not None else 9999
                        xw_old = seen[key][16] if seen[key][16] is not None else 9999
                        if xw_new < xw_old:
                            seen[key] = row
                rows = list(seen.values())

                execute_values(cur, """
                    INSERT INTO observations.airport_wx_impacts
                        (airport_id, station_id, model_source, model_run,
                         valid_time, forecast_hour,
                         ceil_ft, vis_m, wind_speed_kts, wind_dir, wind_gust_kts,
                         tmp_c, dpt_c, tmp_f, heat_index_f, wind_chill_f,
                         crosswind_kts, best_runway_hdg,
                         vfr_color, vfr_worst_param, ifr_color, ifr_worst_param,
                         source_priority, ingested_at)
                    VALUES %s
                    ON CONFLICT (airport_id, model_run, forecast_hour)
                    DO UPDATE SET
                        vfr_color       = EXCLUDED.vfr_color,
                        vfr_worst_param = EXCLUDED.vfr_worst_param,
                        ifr_color       = EXCLUDED.ifr_color,
                        ifr_worst_param = EXCLUDED.ifr_worst_param,
                        ceil_ft         = EXCLUDED.ceil_ft,
                        vis_m           = EXCLUDED.vis_m,
                        wind_speed_kts  = EXCLUDED.wind_speed_kts,
                        wind_dir        = EXCLUDED.wind_dir,
                        wind_gust_kts   = EXCLUDED.wind_gust_kts,
                        tmp_f           = EXCLUDED.tmp_f,
                        heat_index_f    = EXCLUDED.heat_index_f,
                        wind_chill_f    = EXCLUDED.wind_chill_f,
                        crosswind_kts   = EXCLUDED.crosswind_kts,
                        best_runway_hdg = EXCLUDED.best_runway_hdg,
                        source_priority = EXCLUDED.source_priority,
                        ingested_at     = EXCLUDED.ingested_at
                """, rows)
            conn.commit()
            total += len(rows)
            log.info(f"  {sector_name} F{fhr:03d}: {len(rows)} upserted "
                     f"(valid {valid_time.strftime('%H:%MZ')})")

    elapsed = (datetime.now() - t0).total_seconds()
    log.info(f"Total: {total} records in {elapsed:.0f}s")

    # Scour old GLMP_CO records — keep only latest 2 cycles
    try:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM observations.airport_wx_impacts
                WHERE model_source LIKE 'GLMP%'
                  AND model_run < (
                      SELECT MAX(model_run) - INTERVAL '2 hours'
                      FROM observations.airport_wx_impacts
                      WHERE model_source LIKE 'GLMP%'
                  )
            """)
            log.info(f"Scoured {cur.rowcount} old GLMP records")
        conn.commit()
    except Exception as e:
        log.warning(f"Scour failed: {e}")

    log.info("GLMP impacts ingest complete")
    log.info("=" * 60)

    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    lock_fd.close()
    conn.close()

if __name__ == '__main__':
    main()

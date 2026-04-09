#!/var/www/cap_winds_app/venv/bin/python3
"""
ingest_model_site_wx.py — HRRR point extraction for cadet sites
================================================================
Reads HRRR sfc grib2 files from LDM ingest, extracts meteorological
fields at each active cadet site using nearest-grid-point lookup,
computes derived fields (heat index, wind chill, WBGT), and writes
F00-F24 per site per model run to observations.model_site_wx.

Run as www-data cron on data2 every 15 minutes:
  */15 * * * * /var/www/cap_winds_app/venv/bin/python3 \
    /var/www/cap_winds_app/scripts/ingest_model_site_wx.py \
    >> /var/log/model_site_wx_ingest.log 2>&1

HRRR sfc file path pattern (from LDM pqact_ngrid.conf):
  /LDM/models/hrrr/hrrr.YYYYMMDD/HHz/hrrr.tHHz.wrfsfcfFFF.grib2
  where HH = cycle hour (00-23), FF = forecast hour (00-24)

HRRR fields extracted:
  TMP:2 m above ground      -> tmp_c
  DPT:2 m above ground      -> dpt_c
  UGRD:10 m above ground    -> u_ms (for wind_speed/dir)
  VGRD:10 m above ground    -> v_ms
  GUST:surface              -> wind_gust_kts
  PRATE:surface             -> precip_rate_mmhr
  APCP:surface              -> precip_mm
  CRAIN/CSNOW/CICEP/CFRZR   -> precip_type
  CAPE:surface              -> cape_jkg
  DSWRF:surface             -> dswrf_wm2 (for WBGT)
  HGT:cloud base            -> ceil_ft (where available)

Derived:
  wind_speed_kts, wind_dir  from U/V components
  heat_index_c              NWS Rothfusz (tmp >= 27C only)
  wind_chill_c              NWS (tmp <= 10C, wind >= 5 mph only)
  wbgt_c                    Liljegren outdoor WBGT
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

sys.path.insert(0, '/var/www/cap_winds_app')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HRRR_BASE   = Path('/LDM/models/hrrr')
LOCKFILE    = '/var/lock/ingest_model_site_wx.lock'

# HRRR CONUS domain bounds (approximate)
HRRR_LAT_MIN, HRRR_LAT_MAX =  21.0,  53.0
HRRR_LON_MIN, HRRR_LON_MAX = -134.0, -60.0

def in_hrrr_domain(lat, lon):
    """Return True if site is within HRRR CONUS domain."""
    return (HRRR_LAT_MIN <= lat <= HRRR_LAT_MAX and
            HRRR_LON_MIN <= lon <= HRRR_LON_MAX)

# AIGFS — AI Global Forecast System (OCONUS model of record)
# Single sfc file per cycle containing all forecast hours at 6-hr steps
# File: /LDM/models/AIGFS/AIGFS_YYYYMMDD_HH00_sfc.grib2
AIGFS_BASE       = Path('/LDM/models/AIGFS')
AIGFS_FCST_HOURS = list(range(0, 49, 6))    # F000-F048 at 6-hr steps (48hr buffer)
AIGFS_LOOKBACK   = 4                          # look back up to 4 cycles (24 hrs)
DB_DSN      = os.environ.get('DB_DSN',
                             'dbname=avwx_data user=avwx_user host=192.168.0.60')

# GFS 0.25° — global model for OCONUS personnel weather (full variable set)
# File: /LDM/models/gfs/0p25/YYYYMMDD/gfs_0p25_YYYYMMDD_HHz_fFFF.grib2
GFS_BASE        = Path('/LDM/models/gfs/0p25')
GFS_CYCLES      = [0, 6, 12, 18]          # 4 cycles/day
GFS_LOOKBACK    = 4                        # look back up to 4 cycles (24 hrs)
GFS_FCST_HOURS  = list(range(0, 49, 3))   # F000-F048 at 3-hr steps (hourly when available)

# Forecast hours to ingest (F00 = analysis, F01-F24 = forecast)
FCST_HOURS  = list(range(0, 19))   # F00-F18: HRRR hourly cycles go to F18

# How many model cycles to look back when searching for latest
CYCLE_LOOKBACK_HRS = 6

# Minimum age of grib file before processing (seconds) — avoid partial writes
FILE_STABILITY_SECS = 30

# ---------------------------------------------------------------------------
# Physical constants / derived field computations
# ---------------------------------------------------------------------------

def kelvin_to_c(k):
    return round(k - 273.15, 2) if k is not None else None


def ms_to_kts(ms):
    return round(ms * 1.94384, 1) if ms is not None else None


def uv_to_speed_dir(u_ms, v_ms):
    """Convert U/V wind components (m/s) to speed (kts) and direction (deg)."""
    if u_ms is None or v_ms is None:
        return None, None
    speed_ms  = math.sqrt(u_ms**2 + v_ms**2)
    speed_kts = ms_to_kts(speed_ms)
    # Meteorological wind direction: from which wind is blowing
    direction = (270 - math.degrees(math.atan2(v_ms, u_ms))) % 360
    return speed_kts, round(direction)


def heat_index_c(tmp_c, dpt_c):
    """NWS Rothfusz heat index. Only valid when tmp >= 27°C."""
    if tmp_c is None or tmp_c < 27 or dpt_c is None:
        return None
    rh = min(max(math.exp((17.625*dpt_c)/(243.04+dpt_c) -
                          (17.625*tmp_c)/(243.04+tmp_c)), 0), 1) * 100
    tf = tmp_c * 9/5 + 32
    hi = (-42.379 + 2.04901523*tf + 10.14333127*rh
          - 0.22475541*tf*rh - 0.00683783*tf**2
          - 0.05481717*rh**2 + 0.00122874*tf**2*rh
          + 0.00085282*tf*rh**2 - 0.00000199*tf**2*rh**2)
    return round((hi - 32) * 5/9, 2)


def wind_chill_c(tmp_c, wind_speed_kts):
    """NWS wind chill. Only valid when tmp <= 10°C and wind >= 5 mph."""
    if tmp_c is None or tmp_c > 10:
        return None
    wind_mph = (wind_speed_kts or 0) * 1.15078
    if wind_mph < 5:
        return None
    tf = tmp_c * 9/5 + 32
    wc = (35.74 + 0.6215*tf - 35.75*(wind_mph**0.16)
          + 0.4275*tf*(wind_mph**0.16))
    return round((wc - 32) * 5/9, 2)


def wbgt_liljegren(tmp_c, dpt_c, dswrf_wm2, wind_speed_ms=None):
    """
    Outdoor WBGT via Liljegren method (USARIEM standard).
    Simplified version using globe temp approximation.

    Reference: Liljegren et al. (2008), J. Occup. Environ. Hyg.

    Args:
        tmp_c:       2m air temperature (°C)
        dpt_c:       2m dewpoint (°C)
        dswrf_wm2:   downwelling shortwave radiation (W/m²)
        wind_speed_ms: 2m wind speed (m/s), optional

    Returns: WBGT °C or None
    """
    if tmp_c is None or dpt_c is None:
        return None

    # Natural wet bulb temperature (psychrometric approximation)
    # Using August-Roche-Magnus for RH
    alpha = (17.625 * dpt_c) / (243.04 + dpt_c)
    beta  = (17.625 * tmp_c) / (243.04 + tmp_c)
    rh    = min(max(math.exp(alpha - beta), 0.0), 1.0)

    # Stull psychrometric wet bulb (°C) - accurate within 1°C
    tw = (tmp_c * math.atan(0.151977 * (rh*100 + 8.313659)**0.5)
          + math.atan(tmp_c + rh*100)
          - math.atan(rh*100 - 1.676331)
          + 0.00391838 * (rh*100)**1.5 * math.atan(0.023101 * rh*100)
          - 4.686035)

    # Globe temperature approximation
    # Without radiation: tg ≈ ta
    # With radiation: tg increases ~2°C per 100 W/m² (empirical)
    if dswrf_wm2 is not None and dswrf_wm2 > 0:
        # Wind effect: higher wind reduces globe temp
        wind_factor = 1.0
        if wind_speed_ms is not None and wind_speed_ms > 0.5:
            wind_factor = max(0.5, 1.0 - 0.02 * wind_speed_ms)
        tg = tmp_c + (dswrf_wm2 / 500.0) * 10.0 * wind_factor
    else:
        tg = tmp_c

    # WBGT outdoor = 0.7*Tw + 0.2*Tg + 0.1*Ta
    wbgt = 0.7 * tw + 0.2 * tg + 0.1 * tmp_c
    return round(wbgt, 2)


def parse_precip_type(grbs_by_shortname, idx):
    """
    Determine precip type from categorical grib2 fields.
    CRAIN, CSNOW, CICEP, CFRZR — value=1 means category is present.
    Returns: 'rain', 'snow', 'sleet', 'fzra', or None
    """
    def get_val(name):
        msg = grbs_by_shortname.get(name)
        if msg is None:
            return 0
        try:
            return int(msg.values.flat[idx])
        except Exception:
            return 0

    if get_val('CFRZR') == 1:
        return 'fzra'
    if get_val('CSNOW') == 1:
        return 'snow'
    if get_val('CICEP') == 1:
        return 'sleet'
    if get_val('CRAIN') == 1:
        return 'rain'
    return None


# ---------------------------------------------------------------------------
# HRRR file discovery
# ---------------------------------------------------------------------------

def find_latest_hrrr_cycle():
    """
    Find the most recent HRRR cycle that is complete — both F00 and the
    last forecast hour (F18 for hourly cycles) are present and stable.
    This prevents the planning map from showing a partial new cycle while
    the previous complete cycle is still the best available data.

    Searches HRRR_BASE/YYYYMMDD/HHz/ directories, newest first.
    Returns (cycle_dt, cycle_dir) or (None, None).
    """
    last_fhour = max(FCST_HOURS)          # 18 for hourly HRRR cycles
    now_utc    = datetime.now(timezone.utc)
    for hours_back in range(CYCLE_LOOKBACK_HRS + 1):
        candidate = now_utc - timedelta(hours=hours_back)
        date_str  = candidate.strftime('%Y%m%d')
        hour_str  = candidate.strftime('%H')
        cycle_dir = HRRR_BASE / f'hrrr.{date_str}' / f'{hour_str}z'
        f00       = cycle_dir / f'hrrr.t{hour_str}z.wrfsfcf000.grib2'
        flast     = cycle_dir / f'hrrr.t{hour_str}z.wrfsfcf{last_fhour:03d}.grib2'
        if not (f00.exists() and f00.stat().st_size > 0):
            continue
        if not (flast.exists() and flast.stat().st_size > 0):
            log.info(f"HRRR hrrr.{date_str}/{hour_str}z: F00 present but "
                     f"F{last_fhour:02d} not yet complete — skipping, "
                     f"trying previous cycle")
            continue
        age = now_utc.timestamp() - flast.stat().st_mtime
        if age < FILE_STABILITY_SECS:
            log.info(f"HRRR hrrr.{date_str}/{hour_str}z: F{last_fhour:02d} "
                     f"too recent ({age:.0f}s) — waiting for stability")
            continue
        log.info(f"Latest complete HRRR cycle: hrrr.{date_str}/{hour_str}z")
        return candidate.replace(minute=0, second=0, microsecond=0), cycle_dir
    log.warning(f"No complete HRRR cycle found in last {CYCLE_LOOKBACK_HRS} hours")
    return None, None


def hrrr_file(cycle_dir, cycle_hour_str, fhour):
    """Return path to HRRR sfc grib2 file for a given forecast hour."""
    fname = f'hrrr.t{cycle_hour_str}z.wrfsfcf{fhour:03d}.grib2'
    return cycle_dir / fname


# ---------------------------------------------------------------------------
# AIGFS file discovery and field loading
# ---------------------------------------------------------------------------

# AIGFS sfc fields available (subset of HRRR — no dewpoint, CAPE, DSWRF, ceil)
AIGFS_FIELD_SPECS = [
    ('2t',  'heightAboveGround', 2,  'TMP 2m'),
    ('10u', 'heightAboveGround', 10, 'UGRD 10m'),
    ('10v', 'heightAboveGround', 10, 'VGRD 10m'),
    ('tp',  'surface',           0,  'APCP surface'),
]


def find_latest_aigfs_cycle():
    """
    Find the most recent complete AIGFS cycle.
    AIGFS cycles run at 00/06/12/18Z. Each cycle produces a single sfc grib2
    file containing all forecast hours (F000-F384 at 6-hr steps).
    We require F000 and F120 both present and stable before using the cycle.

    Returns (cycle_dt, aigfs_path) or (None, None).
    """
    now_utc    = datetime.now(timezone.utc)
    # AIGFS runs at 00/06/12/18Z — check last AIGFS_LOOKBACK cycles
    for hours_back in range(0, AIGFS_LOOKBACK * 6 + 1, 6):
        candidate = now_utc - timedelta(hours=hours_back)
        # Round down to nearest 6-hour cycle
        cycle_hour = (candidate.hour // 6) * 6
        cycle_dt   = candidate.replace(hour=cycle_hour, minute=0,
                                       second=0, microsecond=0)
        date_str   = cycle_dt.strftime('%Y%m%d')
        hour_str   = cycle_dt.strftime('%H')
        fpath      = AIGFS_BASE / f'AIGFS_{date_str}_{hour_str}00_sfc.grib2'
        if not (fpath.exists() and fpath.stat().st_size > 0):
            continue
        age = now_utc.timestamp() - fpath.stat().st_mtime
        if age < FILE_STABILITY_SECS:
            log.info(f"AIGFS {fpath.name}: too recent ({age:.0f}s) — waiting")
            continue
        log.info(f"Latest complete AIGFS cycle: {fpath.name}")
        return cycle_dt, fpath
    log.warning(f"No complete AIGFS cycle found in last {AIGFS_LOOKBACK*6} hours")
    return None, None


def load_aigfs_fields_for_hour(grib_path, fhour):
    """
    Load AIGFS sfc fields for a specific forecast hour from the multi-hour file.
    Returns dict: shortName -> grib message (or None).
    """
    fields = {}
    try:
        grbs = pygrib.open(str(grib_path))
        for shortname, level_type, level, desc in AIGFS_FIELD_SPECS:
            try:
                msgs = grbs.select(shortName=shortname,
                                   typeOfLevel=level_type,
                                   level=level,
                                   endStep=fhour)
                fields[shortname] = msgs[0] if msgs else None
            except Exception:
                fields[shortname] = None
        grbs.close()
    except Exception as e:
        log.error(f"Failed to open AIGFS {grib_path}: {e}")
    return fields


def ingest_aigfs_cycle(conn, cycle_dt, aigfs_path, sites, verbose=False):
    """
    Ingest AIGFS forecast hours for OCONUS sites.
    AIGFS is the model of record for all OCONUS sites (AK, HI, PR, GU).
    Single file contains all forecast hours at 6-hr steps.
    Available fields: tmp, wind (u/v), precip — no dewpoint/CAPE/DSWRF/ceil.
    """
    model_name  = 'AIGFS'
    now_utc     = datetime.now(timezone.utc)
    total_inserted = 0

    # Build grid tree from F000
    log.info(f"Building AIGFS grid tree from {aigfs_path.name}")
    f000_fields = load_aigfs_fields_for_hour(aigfs_path, 0)
    ref_msg = next((v for v in f000_fields.values() if v is not None), None)
    if ref_msg is None:
        log.error("No usable fields in AIGFS F000")
        return 0

    tree, grid_shape = build_kdtree(ref_msg)

    # Pre-compute grid indices for all OCONUS sites
    site_indices = {}
    for site in sites:
        idx = site_grid_index(tree, grid_shape, site['lat'], site['lon'])
        site_indices[site['id']] = idx
        if verbose:
            log.info(f"  AIGFS site {site['site_name']}: grid index {idx}")

    # Process each 6-hour forecast step
    for fhour in AIGFS_FCST_HOURS:
        valid_time = cycle_dt + timedelta(hours=fhour)
        fields     = load_aigfs_fields_for_hour(aigfs_path, fhour)

        if not any(v is not None for v in fields.values()):
            if verbose:
                log.info(f"  AIGFS F{fhour:03d}: no fields available, skipping")
            continue

        records = []
        for site in sites:
            idx = site_indices[site['id']]

            def val(key, scale=1.0, offset=0.0):
                msg = fields.get(key)
                if msg is None:
                    return None
                try:
                    v = float(msg.values.flat[idx])
                    if v > 9e20:
                        return None
                    return v * scale + offset
                except Exception:
                    return None

            tmp_k  = val('2t')
            u_ms   = val('10u')
            v_ms   = val('10v')
            apcp_m = val('tp')

            tmp_c_val     = kelvin_to_c(tmp_k)
            wspd_kts, wdir = uv_to_speed_dir(u_ms, v_ms)
            wc_c          = wind_chill_c(tmp_c_val, wspd_kts)
            precip_mm_val = round(apcp_m * 1000.0, 2) if apcp_m is not None else None

            records.append({
                'site_id':          site['id'],
                'model_name':       model_name,
                'model_run':        cycle_dt,
                'valid_time':       valid_time,
                'forecast_hour':    fhour,
                'wind_dir':         wdir,
                'wind_speed_kts':   wspd_kts,
                'wind_gust_kts':    None,       # not in AIGFS sfc
                'tmp_c':            tmp_c_val,
                'dpt_c':            None,       # not in AIGFS sfc
                'heat_index_c':     None,
                'wind_chill_c':     wc_c,
                'precip_mm':        precip_mm_val,
                'precip_rate_mmhr': None,       # AIGFS has accumulated tp only
                'precip_type':      None,
                'dswrf_wm2':        None,
                'wbgt_c':           None,       # requires dewpoint
                'cape_jkg':         None,
                'ceil_ft':          None,
                'ingested_at':      now_utc,
            })

        n = upsert_site_wx(conn, records)
        total_inserted += n
        if verbose:
            log.info(f"  AIGFS F{fhour:03d}: {n} site records upserted "
                     f"(valid {valid_time.strftime('%Y-%m-%d %H:%MZ')})")

    log.info(f"AIGFS cycle {cycle_dt.strftime('%Y-%m-%d %HZ')}: "
             f"{total_inserted} total records upserted across "
             f"{len(sites)} OCONUS sites")
    return total_inserted



# ---------------------------------------------------------------------------
# GFS file discovery and ingest
# ---------------------------------------------------------------------------

def find_latest_gfs_cycle():
    """
    Find the most recent GFS 0.25° cycle with F000 and F048 both present.
    GFS cycles run at 00/06/12/18Z. Files land ~3.5 hours after cycle time.
    Returns (cycle_dt, cycle_date_dir) or (None, None).
    """
    now_utc = datetime.now(timezone.utc)
    for hours_back in range(0, GFS_LOOKBACK * 6 + 1, 6):
        candidate  = now_utc - timedelta(hours=hours_back)
        cycle_hour = (candidate.hour // 6) * 6
        cycle_dt   = candidate.replace(hour=cycle_hour, minute=0,
                                       second=0, microsecond=0)
        date_str   = cycle_dt.strftime('%Y%m%d')
        hour_str   = cycle_dt.strftime('%H')
        date_dir   = GFS_BASE / date_str
        f000 = date_dir / f'gfs_0p25_{date_str}_{hour_str}z_f000.grib2'
        f048 = date_dir / f'gfs_0p25_{date_str}_{hour_str}z_f048.grib2'
        if not (f000.exists() and f000.stat().st_size > 0):
            continue
        if not (f048.exists() and f048.stat().st_size > 0):
            log.info(f"GFS {date_str}/{hour_str}Z: F000 present but F048 not yet "
                     f"complete — trying previous cycle")
            continue
        age = now_utc.timestamp() - f048.stat().st_mtime
        if age < FILE_STABILITY_SECS:
            log.info(f"GFS {date_str}/{hour_str}Z: F048 too recent ({age:.0f}s) — waiting")
            continue
        log.info(f"Latest complete GFS cycle: {date_str}/{hour_str}Z")
        return cycle_dt, date_dir
    log.warning(f"No complete GFS cycle found in last {GFS_LOOKBACK*6} hours")
    return None, None


def gfs_file(date_dir, cycle_dt, fhour):
    """Return path to GFS 0.25° grib2 file for a given forecast hour."""
    date_str  = cycle_dt.strftime('%Y%m%d')
    hour_str  = cycle_dt.strftime('%H')
    fname     = f'gfs_0p25_{date_str}_{hour_str}z_f{fhour:03d}.grib2'
    return date_dir / fname


def ingest_gfs_cycle(conn, cycle_dt, date_dir, sites, verbose=False):
    """
    Ingest GFS 0.25° forecast hours for OCONUS sites.
    GFS is used for OCONUS personnel weather because it has the full variable
    set (TMP, DPT, UGRD, VGRD, APCP, CAPE, DSWRF) unlike AIGFS which only
    has TMP and wind.
    """
    model_name     = 'GFS'
    now_utc        = datetime.now(timezone.utc)
    total_inserted = 0

    # Build grid tree from F000
    f000_path = gfs_file(date_dir, cycle_dt, 0)
    if not f000_path.exists():
        log.error(f"GFS F000 not found: {f000_path}")
        return 0

    log.info(f"Building GFS grid tree from {f000_path.name}")
    f000_fields = load_grib_fields(f000_path)
    ref_msg = next((v for v in f000_fields.values() if v is not None), None)
    if ref_msg is None:
        log.error("No usable fields in GFS F000")
        return 0

    tree, grid_shape = build_kdtree(ref_msg)

    # Pre-compute grid indices for all OCONUS sites
    site_indices = {}
    for site in sites:
        idx = site_grid_index(tree, grid_shape, site['lat'], site['lon'])
        site_indices[site['id']] = idx
        if verbose:
            log.info(f"  GFS site {site['site_name']}: grid index {idx}")

    # Process each forecast step
    for fhour in GFS_FCST_HOURS:
        fpath = gfs_file(date_dir, cycle_dt, fhour)
        if not fpath.exists():
            if verbose:
                log.info(f"  GFS F{fhour:03d}: file not available, skipping")
            continue

        age = now_utc.timestamp() - fpath.stat().st_mtime
        if age < FILE_STABILITY_SECS:
            log.info(f"  GFS F{fhour:03d}: file still being written, skipping")
            continue

        valid_time = cycle_dt + timedelta(hours=fhour)
        fields     = load_grib_fields(fpath)
        records    = []

        for site in sites:
            idx = site_indices[site['id']]

            def val(key, scale=1.0, offset=0.0):
                msg = fields.get(key)
                if msg is None:
                    return None
                try:
                    v = float(msg.values.flat[idx])
                    if v > 9e20:
                        return None
                    return v * scale + offset
                except Exception:
                    return None

            # Raw extractions — same shortnames as HRRR
            tmp_k     = val('2t')
            dpt_k     = val('2d')
            u_ms      = val('10u')
            v_ms      = val('10v')
            gust_ms   = val('gust')
            prate_kgs = val('prate')
            apcp_m    = val('tp')
            cape      = val('cape')
            dswrf     = val('dswrf')
            cb_m      = val('cloudbase')

            # Unit conversions
            tmp_c_val    = kelvin_to_c(tmp_k)
            dpt_c_val    = kelvin_to_c(dpt_k)
            wspd_kts, wdir = uv_to_speed_dir(u_ms, v_ms)
            gust_kts     = ms_to_kts(gust_ms)
            prate_mmhr   = round(prate_kgs * 3600.0, 3) if prate_kgs is not None else None
            precip_mm_val = round(apcp_m * 1000.0, 2) if apcp_m is not None else None
            cape_val     = round(cape, 1) if cape is not None else None
            dswrf_val    = round(dswrf, 1) if dswrf is not None else None
            ceil_ft      = round(cb_m * 3.28084) if cb_m is not None else None

            # Precip type
            ptype = parse_precip_type(fields, idx)

            # Derived thermal fields
            hi_c  = heat_index_c(tmp_c_val, dpt_c_val)
            wc_c  = wind_chill_c(tmp_c_val, wspd_kts)
            u_ms_val = u_ms or 0
            v_ms_val = v_ms or 0
            ws_ms = math.sqrt(u_ms_val**2 + v_ms_val**2)
            wbgt  = wbgt_liljegren(tmp_c_val, dpt_c_val, dswrf_val, ws_ms)

            records.append({
                'site_id':          site['id'],
                'model_name':       model_name,
                'model_run':        cycle_dt,
                'valid_time':       valid_time,
                'forecast_hour':    fhour,
                'wind_dir':         wdir,
                'wind_speed_kts':   wspd_kts,
                'wind_gust_kts':    gust_kts,
                'tmp_c':            tmp_c_val,
                'dpt_c':            dpt_c_val,
                'heat_index_c':     hi_c,
                'wind_chill_c':     wc_c,
                'precip_mm':        precip_mm_val,
                'precip_rate_mmhr': prate_mmhr,
                'precip_type':      ptype,
                'dswrf_wm2':        dswrf_val,
                'wbgt_c':           wbgt,
                'cape_jkg':         cape_val,
                'ceil_ft':          ceil_ft,
                'ingested_at':      now_utc,
            })

        n = upsert_site_wx(conn, records)
        total_inserted += n
        if verbose:
            log.info(f"  GFS F{fhour:03d}: {n} site records upserted "
                     f"(valid {valid_time.strftime('%Y-%m-%d %H:%MZ')})")

    log.info(f"GFS cycle {cycle_dt.strftime('%Y-%m-%d %HZ')}: "
             f"{total_inserted} total records upserted across "
             f"{len(sites)} OCONUS sites")
    return total_inserted

# ---------------------------------------------------------------------------
# Grid index lookup
# ---------------------------------------------------------------------------

def build_kdtree(grb_msg):
    """Build a cKDTree from grib2 lat/lon grid for fast nearest-point lookup."""
    lats, lons = grb_msg.latlons()
    # Normalize lons to -180..180
    lons = np.where(lons > 180, lons - 360, lons)
    pts  = np.column_stack([lats.ravel(), lons.ravel()])
    return cKDTree(pts), lats.shape


def site_grid_index(tree, grid_shape, site_lat, site_lon):
    """Return flat grid index of nearest grid point to (site_lat, site_lon)."""
    _, idx = tree.query([site_lat, site_lon])
    return idx


# ---------------------------------------------------------------------------
# GRIB field extraction
# ---------------------------------------------------------------------------

FIELD_SPECS = [
    # (shortName or custom key, typeOfLevel, level, description)
    ('2t',    'heightAboveGround', 2,    'TMP 2m'),
    ('2d',    'heightAboveGround', 2,    'DPT 2m'),
    ('10u',   'heightAboveGround', 10,   'UGRD 10m'),
    ('10v',   'heightAboveGround', 10,   'VGRD 10m'),
    ('gust',  'surface',           0,    'GUST surface'),
    ('prate', 'surface',           0,    'PRATE surface'),
    ('tp',    'surface',           0,    'APCP surface'),
    ('cape',  'surface',           0,    'CAPE surface'),
    ('dswrf', 'surface',           0,    'DSWRF surface (HRRR)'),
    ('sdswrf','surface',           0,    'DSWRF surface (GFS)'),
    ('crain', 'surface',           0,    'CRAIN surface'),
    ('csnow', 'surface',           0,    'CSNOW surface'),
    ('cicep', 'surface',           0,    'CICEP surface'),
    ('cfrzr', 'surface',           0,    'CFRZR surface'),
]

# Cloud base height (not always present in all HRRR versions)
CEIL_SPEC = ('gh', 'cloudBase', 0, 'HGT cloud base')


def load_grib_fields(grib_path):
    """
    Load all needed grib2 fields from an HRRR sfc file.
    Returns dict: key -> grib message (or None if not found).
    """
    fields = {}
    try:
        grbs = pygrib.open(str(grib_path))
        for shortname, level_type, level, desc in FIELD_SPECS:
            try:
                msgs = grbs.select(shortName=shortname,
                                   typeOfLevel=level_type,
                                   level=level)
                if msgs:
                    fields[shortname] = msgs[0]
            except Exception:
                fields[shortname] = None

        # Cloud base (optional)
        try:
            msgs = grbs.select(shortName=CEIL_SPEC[0],
                               typeOfLevel=CEIL_SPEC[1])
            fields['cloudbase'] = msgs[0] if msgs else None
        except Exception:
            fields['cloudbase'] = None

        # Normalize GFS sdswrf -> dswrf key for unified downstream access
        if 'sdswrf' in fields and fields['sdswrf'] is not None:
            if fields.get('dswrf') is None:
                fields['dswrf'] = fields['sdswrf']

        grbs.close()
    except Exception as e:
        log.error(f"Failed to open {grib_path}: {e}")

    return fields


# ---------------------------------------------------------------------------
# DB operations
# ---------------------------------------------------------------------------

def get_active_sites(conn):
    """Fetch all active cadet sites."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT id, site_name, lat, lon, elevation_ft
            FROM observations.cadet_sites
            WHERE is_active = TRUE
            ORDER BY id
        """)
        return cur.fetchall()


def cycle_already_ingested(conn, model_run_dt, site_id):
    """Check if a model run has already been fully ingested for a site."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM observations.model_site_wx
            WHERE site_id = %s
              AND model_run = %s
              AND forecast_hour = 24
        """, (site_id, model_run_dt))
        return cur.fetchone()[0] > 0


def upsert_site_wx(conn, records):
    """
    Upsert a list of model_site_wx records.
    records: list of dicts with all model_site_wx columns.
    """
    if not records:
        return 0
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO observations.model_site_wx (
                site_id, model_name, model_run, valid_time, forecast_hour,
                wind_dir, wind_speed_kts, wind_gust_kts,
                tmp_c, dpt_c, heat_index_c, wind_chill_c,
                precip_mm, precip_rate_mmhr, precip_type,
                dswrf_wm2, wbgt_c, cape_jkg, ceil_ft,
                ingested_at
            ) VALUES %s
            ON CONFLICT (site_id, model_run, forecast_hour)
            DO UPDATE SET
                wind_dir         = EXCLUDED.wind_dir,
                wind_speed_kts   = EXCLUDED.wind_speed_kts,
                wind_gust_kts    = EXCLUDED.wind_gust_kts,
                tmp_c            = EXCLUDED.tmp_c,
                dpt_c            = EXCLUDED.dpt_c,
                heat_index_c     = EXCLUDED.heat_index_c,
                wind_chill_c     = EXCLUDED.wind_chill_c,
                precip_mm        = EXCLUDED.precip_mm,
                precip_rate_mmhr = EXCLUDED.precip_rate_mmhr,
                precip_type      = EXCLUDED.precip_type,
                dswrf_wm2        = EXCLUDED.dswrf_wm2,
                wbgt_c           = EXCLUDED.wbgt_c,
                cape_jkg         = EXCLUDED.cape_jkg,
                ceil_ft          = EXCLUDED.ceil_ft,
                ingested_at      = EXCLUDED.ingested_at
        """, [
            (r['site_id'], r['model_name'], r['model_run'], r['valid_time'],
             r['forecast_hour'], r['wind_dir'], r['wind_speed_kts'],
             r['wind_gust_kts'], r['tmp_c'], r['dpt_c'],
             r['heat_index_c'], r['wind_chill_c'],
             r['precip_mm'], r['precip_rate_mmhr'], r['precip_type'],
             r['dswrf_wm2'], r['wbgt_c'], r['cape_jkg'], r['ceil_ft'],
             r['ingested_at'])
            for r in records
        ])
    conn.commit()
    return len(records)


# ---------------------------------------------------------------------------
# Main ingest logic
# ---------------------------------------------------------------------------

def ingest_cycle(conn, cycle_dt, cycle_dir, sites, verbose=False):
    """Ingest all forecast hours for one HRRR cycle."""
    cycle_hour_str = cycle_dt.strftime('%H')
    model_name     = 'HRRR'
    now_utc        = datetime.now(timezone.utc)
    total_inserted = 0

    # Build grid tree from F00 (all hours share the same grid)
    f00_path = hrrr_file(cycle_dir, cycle_hour_str, 0)
    if not f00_path.exists():
        log.error(f"F00 not found: {f00_path}")
        return 0

    log.info(f"Building grid tree from {f00_path.name}")
    f00_fields = load_grib_fields(f00_path)
    ref_msg = next((v for v in f00_fields.values() if v is not None), None)
    if ref_msg is None:
        log.error("No usable fields in F00")
        return 0

    tree, grid_shape = build_kdtree(ref_msg)

    # Pre-compute grid indices for all sites
    site_indices = {}
    for site in sites:
        idx = site_grid_index(tree, grid_shape, site['lat'], site['lon'])
        site_indices[site['id']] = idx
        if verbose:
            log.info(f"  Site {site['site_name']}: grid index {idx}")

    # Process each forecast hour
    for fhour in FCST_HOURS:
        fpath = hrrr_file(cycle_dir, cycle_hour_str, fhour)
        if not fpath.exists():
            if verbose:
                log.info(f"  F{fhour:02d}: file not yet available, skipping")
            continue

        # Skip unstable files
        age = now_utc.timestamp() - fpath.stat().st_mtime
        if age < FILE_STABILITY_SECS:
            log.info(f"  F{fhour:02d}: file still being written, skipping")
            continue

        valid_time = cycle_dt + timedelta(hours=fhour)
        fields     = load_grib_fields(fpath)
        records    = []

        for site in sites:
            idx = site_indices[site['id']]

            def val(key, scale=1.0, offset=0.0):
                msg = fields.get(key)
                if msg is None:
                    return None
                try:
                    v = float(msg.values.flat[idx])
                    if v > 9e20:   # grib2 missing value
                        return None
                    return v * scale + offset
                except Exception:
                    return None

            # Raw extractions
            tmp_k    = val('2t')
            dpt_k    = val('2d')
            u_ms     = val('10u')
            v_ms     = val('10v')
            gust_ms  = val('gust')
            prate_kgs = val('prate')  # kg/m²/s = mm/s
            apcp_m   = val('tp')     # m accumulated
            cape     = val('cape')
            dswrf    = val('dswrf')

            # Ceiling
            cb_m = val('cloudbase')
            ceil_ft = round(cb_m * 3.28084) if cb_m is not None else None

            # Unit conversions
            tmp_c_val  = kelvin_to_c(tmp_k)
            dpt_c_val  = kelvin_to_c(dpt_k)
            wspd_kts, wdir = uv_to_speed_dir(u_ms, v_ms)
            gust_kts   = ms_to_kts(gust_ms)
            prate_mmhr = round(prate_kgs * 3600.0, 3) if prate_kgs is not None else None
            precip_mm_val = round(apcp_m * 1000.0, 2) if apcp_m is not None else None
            cape_val   = round(cape, 1) if cape is not None else None
            dswrf_val  = round(dswrf, 1) if dswrf is not None else None

            # Precip type
            ptype = parse_precip_type(fields, idx)

            # Derived thermal fields
            hi_c = heat_index_c(tmp_c_val, dpt_c_val)
            wc_c = wind_chill_c(tmp_c_val, wspd_kts)
            u_ms_val = u_ms or 0
            v_ms_val = v_ms or 0
            ws_ms = math.sqrt(u_ms_val**2 + v_ms_val**2)
            wbgt  = wbgt_liljegren(tmp_c_val, dpt_c_val, dswrf_val, ws_ms)

            records.append({
                'site_id':        site['id'],
                'model_name':     model_name,
                'model_run':      cycle_dt,
                'valid_time':     valid_time,
                'forecast_hour':  fhour,
                'wind_dir':       wdir,
                'wind_speed_kts': wspd_kts,
                'wind_gust_kts':  gust_kts,
                'tmp_c':          tmp_c_val,
                'dpt_c':          dpt_c_val,
                'heat_index_c':   hi_c,
                'wind_chill_c':   wc_c,
                'precip_mm':      precip_mm_val,
                'precip_rate_mmhr': prate_mmhr,
                'precip_type':    ptype,
                'dswrf_wm2':      dswrf_val,
                'wbgt_c':         wbgt,
                'cape_jkg':       cape_val,
                'ceil_ft':        ceil_ft,
                'ingested_at':    now_utc,
            })

        n = upsert_site_wx(conn, records)
        total_inserted += n
        if verbose:
            log.info(f"  F{fhour:02d}: {n} site records upserted "
                     f"(valid {valid_time.strftime('%H:%MZ')})")

    log.info(f"Cycle {cycle_dt.strftime('%Y-%m-%d %HZ')}: "
             f"{total_inserted} total records upserted across "
             f"{len(sites)} sites")
    return total_inserted


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Ingest HRRR (CONUS) and GFS (OCONUS) data for cadet sites')
    parser.add_argument('-v', '--verbose', action='store_true')
    parser.add_argument('--cycle', help='Force HRRR cycle YYYYMMDD_HH (e.g. 20260327_18)')
    parser.add_argument('--gfs-cycle', help='Force GFS cycle for OCONUS sites YYYYMMDD_HH (e.g. 20260408_12)')
    parser.add_argument('--aigfs-cycle', help='(Deprecated) Force AIGFS cycle YYYYMMDD_HH — use --gfs-cycle instead')
    args = parser.parse_args()

    # Lockfile — prevent concurrent cron runs
    lock_fd = open(LOCKFILE, 'w')
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        log.warning("Another ingest instance is running — exiting")
        return

    log.info("=" * 60)
    log.info(f"Model site wx ingest started: "
             f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}")

    conn = None
    try:
        conn = psycopg2.connect(DB_DSN)

        all_sites = get_active_sites(conn)
        log.info(f"Active cadet sites: {len(all_sites)}")
        if not all_sites:
            log.warning("No active cadet sites found — nothing to ingest")
            return

        # Split sites by model domain
        conus_sites  = [s for s in all_sites if in_hrrr_domain(s['lat'], s['lon'])]
        oconus_sites = [s for s in all_sites if not in_hrrr_domain(s['lat'], s['lon'])]

        log.info(f"  CONUS (HRRR): {len(conus_sites)} sites — "
                 f"{[s['site_name'] for s in conus_sites]}")
        log.info(f"  OCONUS (GFS): {len(oconus_sites)} sites — "
                 f"{[s['site_name'] for s in oconus_sites]}")

        # ── HRRR ingest for CONUS sites ───────────────────────────────────────
        if conus_sites:
            if args.cycle:
                dt = datetime.strptime(args.cycle, '%Y%m%d_%H').replace(
                    tzinfo=timezone.utc)
                date_str  = dt.strftime('%Y%m%d')
                hour_str  = dt.strftime('%H')
                cycle_dir = HRRR_BASE / f'hrrr.{date_str}' / f'{hour_str}z'
                if not cycle_dir.exists():
                    log.error(f"HRRR cycle directory not found: {cycle_dir}")
                else:
                    ingest_cycle(conn, dt, cycle_dir, conus_sites,
                                 verbose=args.verbose)
            else:
                cycle_dt, cycle_dir = find_latest_hrrr_cycle()
                if cycle_dt is None:
                    log.error("No complete HRRR cycle available")
                else:
                    ingest_cycle(conn, cycle_dt, cycle_dir, conus_sites,
                                 verbose=args.verbose)

        # ── GFS ingest for OCONUS sites ───────────────────────────────────────
        # GFS used for OCONUS because it has full variable set for personnel
        # impacts (TMP, DPT, UGRD, VGRD, APCP, CAPE, DSWRF, WBGT).
        # AIGFS retained for aviation wind constraints but not personnel wx.
        if oconus_sites:
            if args.gfs_cycle:
                dt = datetime.strptime(args.gfs_cycle, '%Y%m%d_%H').replace(
                    tzinfo=timezone.utc)
                date_dir = GFS_BASE / dt.strftime('%Y%m%d')
                if not date_dir.exists():
                    log.error(f"GFS date directory not found: {date_dir}")
                else:
                    ingest_gfs_cycle(conn, dt, date_dir, oconus_sites,
                                     verbose=args.verbose)
            else:
                gfs_dt, gfs_date_dir = find_latest_gfs_cycle()
                if gfs_dt is None:
                    log.error("No complete GFS cycle available for OCONUS sites")
                else:
                    ingest_gfs_cycle(conn, gfs_dt, gfs_date_dir, oconus_sites,
                                     verbose=args.verbose)

    except Exception as e:
        log.error(f"Ingest failed: {e}", exc_info=True)
    finally:
        if conn:
            conn.close()
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()

    log.info("=" * 60)


if __name__ == '__main__':
    main()

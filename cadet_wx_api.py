"""
cadet_wx_api.py — CAP WxCOP Cadet Weather API Blueprint
========================================================
Provides JSON endpoints for the Cadet Weather COP and Planning maps.

Endpoints:
  GET /api/cadet_wx/sites
  GET /api/cadet_wx/current
  GET /api/cadet_wx/forecast?hour=1-24
  GET /api/cadet_wx/site/<site_id>/forecast

Stoplight logic — CAPR 60-2 / DAFMAN 91-203:
  Each category -> GREEN / YELLOW / RED
  Site color = worst single category (RED > YELLOW > GREEN)

Changelog vs original:
  - Fixed METAR field names (IEM->production schema):
      tmpf->temp_c, dwpf->dewpoint_c, sknt->wind_speed_kts,
      gust->wind_gust_kts; precip from present_weather ARRAY
  - Fixed WWA query: phenomena (not phenom), end_time (not expires)
  - WWA query uses end_time > NOW() for reliability over is_active flag
"""

import math
import logging
from datetime import datetime, timezone, timedelta

from flask import Blueprint, jsonify, request
import psycopg2
import psycopg2.extras

log = logging.getLogger(__name__)

cadet_wx_bp = Blueprint('cadet_wx', __name__)

# ---------------------------------------------------------------------------
# Stoplight thresholds — CAPR 60-2 / DAFMAN 91-203
# ---------------------------------------------------------------------------

def heat_stress_color(heat_index_c, tmp_c):
    """CAPR 60-2 §2.6.13 Table 2.2 — Heat Stress."""
    hi_f = None
    if heat_index_c is not None:
        hi_f = heat_index_c * 9/5 + 32
    elif tmp_c is not None:
        hi_f = tmp_c * 9/5 + 32
    if hi_f is None:
        return 'UNKNOWN'
    if hi_f <= 90:
        return 'GREEN'   # Normal / Low
    elif hi_f <= 103:
        return 'YELLOW'  # Moderate
    else:
        return 'RED'     # High / Extreme (>103°F)


def cold_stress_color(wind_chill_c, tmp_c):
    """CAPR 60-2 §2.6.14 Table 2.3 — Cold Stress."""
    wc_f = None
    if wind_chill_c is not None:
        wc_f = wind_chill_c * 9/5 + 32
    elif tmp_c is not None:
        wc_f = tmp_c * 9/5 + 32
    if wc_f is None:
        return 'UNKNOWN'
    if wc_f > 40:
        return 'GREEN'   # Normal
    elif wc_f >= 21:
        return 'GREEN'   # Low
    elif wc_f >= 0:
        return 'YELLOW'  # Medium
    else:
        return 'RED'     # High / Extreme (<0°F)


def lightning_color(within_5nm, within_10nm):
    """DAFMAN 91-203 §3.2-3.3 — GLM lightning proximity."""
    if within_5nm:
        return 'RED'     # WARNING — shelter immediately
    elif within_10nm:
        return 'YELLOW'  # WATCH — prepare to shelter
    return 'GREEN'


def surface_wind_color(wind_speed_kts, wind_gust_kts):
    """Surface wind stoplight."""
    if wind_speed_kts is None:
        return 'UNKNOWN'
    gust = wind_gust_kts or 0
    if wind_speed_kts > 25 or gust > 35:
        return 'RED'
    elif wind_speed_kts >= 15 or gust >= 25:
        return 'YELLOW'
    return 'GREEN'


def precip_color(precip_rate_mmhr, precip_type=None):
    """Precipitation stoplight."""
    if precip_type in ('fzra', 'hail'):
        return 'RED'
    if precip_type in ('snow', 'sleet'):
        return 'YELLOW'
    if precip_rate_mmhr is None:
        return 'GREEN'
    rate_inhr = precip_rate_mmhr / 25.4
    if rate_inhr > 0.3:
        return 'RED'
    elif rate_inhr >= 0.1:
        return 'YELLOW'
    return 'GREEN'


def wwa_color(wwa_phenoms):
    """
    NWS Watch/Warning/Advisory stoplight.
    wwa_phenoms: list of (phenomena, significance) tuples.
    """
    RED_CODES    = {('TO','W'),('SV','W'),('FF','W'),('EW','W'),('BZ','W'),('WS','W')}
    YELLOW_CODES = {('TO','A'),('SV','A'),('WI','Y'),('WS','A')}
    color = 'GREEN'
    for phenom, sig in (wwa_phenoms or []):
        code = (phenom, sig)
        if code in RED_CODES:
            return 'RED'
        if code in YELLOW_CODES:
            color = 'YELLOW'
    return color


def composite_color(colors):
    """Worst of all category colors."""
    if 'RED' in colors:   return 'RED'
    if 'YELLOW' in colors: return 'YELLOW'
    if 'GREEN' in colors:  return 'GREEN'
    return 'UNKNOWN'


# ---------------------------------------------------------------------------
# METAR parsing helpers — production schema field names
# ---------------------------------------------------------------------------

def parse_present_weather(present_weather):
    """
    Extract precip type from observations.metar present_weather ARRAY.
    Column is an array of strings like ["-RA", "BR"] or PostgreSQL array.
    Returns: 'fzra', 'hail', 'snow', 'sleet', 'rain', or None
    """
    if not present_weather:
        return None
    # Join all elements into one string for pattern matching
    if isinstance(present_weather, (list, tuple)):
        text = ' '.join(str(x) for x in present_weather).upper()
    else:
        text = str(present_weather).upper()

    if 'FZRA' in text or 'FZDZ' in text:
        return 'fzra'
    if 'GR' in text or 'GS' in text:
        return 'hail'
    if 'SN' in text or 'SG' in text:
        return 'snow'
    if 'PL' in text or 'IC' in text:
        return 'sleet'
    if 'RA' in text or 'DZ' in text:
        return 'rain'
    return None


def precip_type_from_raw(raw_text):
    """Fallback: parse raw METAR text for precip type."""
    if not raw_text:
        return None
    groups = raw_text.upper().split()
    for g in groups:
        if 'FZRA' in g or 'FZDZ' in g:
            return 'fzra'
        if any(x in g for x in ['+GR', 'GR ', '-GR', 'GS']):
            return 'hail'
        if 'SN' in g or 'SG' in g:
            return 'snow'
        if 'PL' in g or 'IC' in g:
            return 'sleet'
        if 'RA' in g or 'DZ' in g:
            return 'rain'
    return None


def metar_heat_index(tmp_c, dpt_c):
    """NWS heat index (Rothfusz). Only valid when tmp >= 27°C."""
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


def metar_wind_chill(tmp_c, wind_speed_kts):
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


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _open_db():
    import os
    dsn = os.environ.get('DB_DSN',
                         'dbname=avwx_data user=avwx_user host=192.168.0.60')
    return psycopg2.connect(dsn)


def _cursor(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


# ---------------------------------------------------------------------------
# WWA lookup — active polygons covering a point
# Uses end_time > NOW() rather than is_active flag (more reliable)
# Column is 'phenomena' not 'phenom'
# ---------------------------------------------------------------------------

def get_wwa_for_point(cur, lat, lon):
    """
    Return list of (phenomena, significance) for active NWS WWA polygons
    containing the given point.
    """
    cur.execute("""
        SELECT phenomena, significance
        FROM observations.wwa
        WHERE end_time > NOW()
          AND ST_Contains(
                geom,
                ST_SetSRID(ST_MakePoint(%s, %s), 4326)
              )
    """, (lon, lat))
    rows = cur.fetchall()
    return [(r['phenomena'], r['significance']) for r in rows]


# ---------------------------------------------------------------------------
# GLM lightning proximity — DAFMAN 91-203 §3.2-3.3
# ---------------------------------------------------------------------------

GLM_RED_NM    = 5.0
GLM_YELLOW_NM = 10.0
GLM_WINDOW_MIN = 30
NM_TO_DEG = 1.0 / 60.0


def get_glm_lightning(cur, lat, lon):
    """
    Query glm_flashes for recent flashes near (lat, lon).
    Returns (within_5nm, within_10nm, nearest_nm, last_flash_time, flash_count)
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=GLM_WINDOW_MIN)
    lat_deg = GLM_YELLOW_NM * NM_TO_DEG
    lon_deg = GLM_YELLOW_NM * NM_TO_DEG / max(math.cos(math.radians(lat)), 0.01)

    cur.execute("""
        SELECT lat, lon, flash_time
        FROM observations.glm_flashes
        WHERE flash_time >= %s
          AND lat BETWEEN %s AND %s
          AND lon BETWEEN %s AND %s
        ORDER BY flash_time DESC
    """, (cutoff,
          lat - lat_deg, lat + lat_deg,
          lon - lon_deg, lon + lon_deg))
    rows = cur.fetchall()

    if not rows:
        return False, False, None, None, 0

    within_5nm = False
    within_10nm = False
    nearest_nm = None
    last_time = None
    count = 0

    for row in rows:
        dlat = (row['lat'] - lat) * 60.0
        dlon = (row['lon'] - lon) * 60.0 * math.cos(math.radians(lat))
        dist_nm = math.sqrt(dlat**2 + dlon**2)
        if dist_nm <= GLM_YELLOW_NM:
            count += 1
            if nearest_nm is None or dist_nm < nearest_nm:
                nearest_nm = dist_nm
            ft = row['flash_time']
            if ft.tzinfo is None:
                ft = ft.replace(tzinfo=timezone.utc)
            if last_time is None or ft > last_time:
                last_time = ft
            if dist_nm <= GLM_RED_NM:
                within_5nm = True
            within_10nm = True

    return within_5nm, within_10nm, nearest_nm, last_time, count


def glm_available(cur):
    """Check whether glm_flashes has data from the last 10 minutes."""
    try:
        cur.execute("""
            SELECT 1 FROM observations.glm_flashes
            WHERE flash_time > NOW() - INTERVAL '10 minutes'
            LIMIT 1
        """)
        return cur.fetchone() is not None
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Current conditions — METAR vs model F00 recency logic
# ---------------------------------------------------------------------------

def build_current_stoplight(cur, site):
    """
    Evaluate stoplight using the more recent of METAR or model F00.
    Uses production schema field names throughout.
    """
    site_id = site['id']
    lat     = site['lat']
    lon     = site['lon']
    wx_sta  = site.get('wx_station_override') or site.get('station_id')

    metar_data = None
    model_data = None
    metar_time = None
    model_time = None

    # --- Fetch latest METAR (production schema field names) ---
    if wx_sta:
        cur.execute("""
            SELECT observation_time,
                   temp_c, dewpoint_c,
                   wind_speed_kts, wind_gust_kts,
                   present_weather, raw_text
            FROM observations.metar
            WHERE station_id = %s
              AND observation_time > NOW() - INTERVAL '3 hours'
            ORDER BY observation_time DESC
            LIMIT 1
        """, (wx_sta,))
        row = cur.fetchone()
        if row:
            metar_time = row['observation_time']
            tmp_c  = row['temp_c']
            dpt_c  = row['dewpoint_c']
            wspd   = row['wind_speed_kts']
            gust   = row['wind_gust_kts']
            # Precip type from present_weather array, fallback to raw_text
            ptype  = parse_present_weather(row['present_weather'])
            if ptype is None:
                ptype = precip_type_from_raw(row['raw_text'])
            # Precip rate not available in METAR schema — use None
            # (type alone drives the stoplight for observed conditions)
            prate = None
            hi_c  = metar_heat_index(tmp_c, dpt_c)
            wc_c  = metar_wind_chill(tmp_c, wspd)
            metar_data = dict(
                tmp_c=tmp_c, dpt_c=dpt_c,
                wind_speed_kts=wspd, wind_gust_kts=gust,
                precip_rate_mmhr=prate, precip_type=ptype,
                heat_index_c=hi_c, wind_chill_c=wc_c,
                wbgt_c=None,  # not available from METAR
                observation_time=metar_time,
            )

    # --- Fetch latest model F00 ---
    cur.execute("""
        SELECT valid_time, wind_speed_kts, wind_gust_kts, wind_dir,
               tmp_c, dpt_c, heat_index_c, wind_chill_c,
               precip_rate_mmhr, precip_type, wbgt_c, cape_jkg, ceil_ft
        FROM observations.model_site_wx
        WHERE site_id = %s
          AND forecast_hour = 0
          AND valid_time > NOW() - INTERVAL '3 hours'
        ORDER BY model_run DESC, valid_time DESC
        LIMIT 1
    """, (site_id,))
    row = cur.fetchone()
    if row:
        model_time = row['valid_time']
        model_data = dict(row)

    # --- Choose more recent source ---
    if metar_time and model_time:
        mt = metar_time if metar_time.tzinfo else metar_time.replace(tzinfo=timezone.utc)
        mo = model_time if model_time.tzinfo else model_time.replace(tzinfo=timezone.utc)
        use_data, source, obs_time = (
            (metar_data, 'METAR', metar_time) if mt >= mo
            else (model_data, 'MODEL_F00', model_time)
        )
    elif metar_data:
        use_data, source, obs_time = metar_data, 'METAR', metar_time
    elif model_data:
        use_data, source, obs_time = model_data, 'MODEL_F00', model_time
    else:
        return _no_data_result(site_id, site['site_name'], lat, lon)
    wwa_phenoms = get_wwa_for_point(cur, lat, lon)

    # --- GLM lightning (with WWA proxy fallback) ---
    use_glm = glm_available(cur)
    if use_glm:
        within_5nm, within_10nm, nearest_nm, last_flash_time, flash_count = \
            get_glm_lightning(cur, lat, lon)
        glm_data = {
            'within_5nm':        within_5nm,
            'within_10nm':       within_10nm,
            'nearest_nm':        round(nearest_nm, 2) if nearest_nm is not None else None,
            'last_flash_time':   last_flash_time.isoformat() if last_flash_time else None,
            'flash_count_30min': flash_count,
            'source':            'GLM',
        }
        ltg_color = lightning_color(within_5nm, within_10nm)
    else:
        # Proxy: Tornado/Severe Wx warning → RED, watch → YELLOW
        has_warning = any(sig == 'W' for ph, sig in wwa_phenoms if ph in ('TO','SV'))
        has_watch   = any(sig == 'A' for ph, sig in wwa_phenoms if ph in ('TO','SV'))
        ltg_color = lightning_color(has_warning, has_watch)
        glm_data = {
            'source': 'WWA_PROXY',
            'within_5nm': False, 'within_10nm': False,
            'nearest_nm': None, 'last_flash_time': None, 'flash_count_30min': 0,
        }

    # --- Stoplight ---
    cats = {
        'heat_stress':   heat_stress_color(use_data.get('heat_index_c'), use_data.get('tmp_c')),
        'cold_stress':   cold_stress_color(use_data.get('wind_chill_c'), use_data.get('tmp_c')),
        'lightning':     ltg_color,
        'surface_wind':  surface_wind_color(use_data.get('wind_speed_kts'), use_data.get('wind_gust_kts')),
        'precipitation': precip_color(use_data.get('precip_rate_mmhr'), use_data.get('precip_type')),
        'severe_wx':     wwa_color(wwa_phenoms),
    }

    return {
        'site_id':     site_id,
        'site_name':   site['site_name'],
        'unit':        site.get('unit'),
        'site_type':   site['site_type'],
        'lat':         lat,
        'lon':         lon,
        'cap_region':  site.get('cap_region'),
        'color':       composite_color(list(cats.values())),
        'categories':  cats,
        'data_source': source,
        'obs_time':    obs_time.isoformat() if obs_time else None,
        'conditions': {
            'tmp_c':            use_data.get('tmp_c'),
            'dpt_c':            use_data.get('dpt_c'),
            'heat_index_c':     use_data.get('heat_index_c'),
            'wind_chill_c':     use_data.get('wind_chill_c'),
            'wind_speed_kts':   use_data.get('wind_speed_kts'),
            'wind_gust_kts':    use_data.get('wind_gust_kts'),
            'wind_dir':         use_data.get('wind_dir'),
            'precip_rate_mmhr': use_data.get('precip_rate_mmhr'),
            'precip_type':      use_data.get('precip_type'),
            'wbgt_c':           use_data.get('wbgt_c'),
            'cape_jkg':         use_data.get('cape_jkg'),
            'ceil_ft':          use_data.get('ceil_ft'),
        },
        'wwa':       [{'phenom': p, 'significance': s} for p, s in wwa_phenoms],
        'lightning': glm_data,
    }


def _no_data_result(site_id, site_name, lat=None, lon=None):
    return {
        'site_id':     site_id,
        'site_name':   site_name,
        'color':       'UNKNOWN',
        'categories':  {},
        'data_source': None,
        'obs_time':    None,
        'conditions':  {},
        'wwa':         [],
        'lightning':   None,
        'lat':         lat,
        'lon':         lon,
    }


# ---------------------------------------------------------------------------
# Forecast stoplight — model_site_wx F01-F24
# ---------------------------------------------------------------------------

def build_forecast_stoplight(cur, site, forecast_hour):
    """Evaluate stoplight for a single site at a specific forecast hour."""
    site_id = site['id']
    lat     = site['lat']
    lon     = site['lon']

    cur.execute("""
        SELECT forecast_hour, valid_time,
               wind_speed_kts, wind_gust_kts, wind_dir,
               tmp_c, dpt_c, heat_index_c, wind_chill_c,
               precip_mm, precip_rate_mmhr, precip_type,
               dswrf_wm2, wbgt_c, cape_jkg, ceil_ft
        FROM observations.model_site_wx
        WHERE site_id = %s
          AND forecast_hour BETWEEN 1 AND 24
          AND model_run = (
              SELECT MAX(model_run)
              FROM observations.model_site_wx
              WHERE site_id = %s AND forecast_hour = 1
          )
        ORDER BY forecast_hour
    """, (site_id, site_id))
    all_hours = cur.fetchall()

    if not all_hours:
        return _no_data_result(site_id, site['site_name'], lat, lon)

    wwa_phenoms = get_wwa_for_point(cur, lat, lon)
    has_warning = any(sig == 'W' for ph, sig in wwa_phenoms if ph in ('TO','SV'))
    has_watch   = any(sig == 'A' for ph, sig in wwa_phenoms if ph in ('TO','SV'))

    table = []
    for row in all_hours:
        # For forecast hours, GLM not available — use CAPE as thunderstorm proxy
        # High CAPE (>1000 J/kg) with precip → elevated lightning risk
        cape = row['cape_jkg'] or 0
        if cape > 2000:
            ltg_fcst = 'RED'
        elif cape > 500:
            ltg_fcst = 'YELLOW'
        else:
            # Fall back to current WWA
            ltg_fcst = lightning_color(has_warning, has_watch)

        cats = {
            'heat_stress':   heat_stress_color(row['heat_index_c'], row['tmp_c']),
            'cold_stress':   cold_stress_color(row['wind_chill_c'], row['tmp_c']),
            'lightning':     ltg_fcst,
            'surface_wind':  surface_wind_color(row['wind_speed_kts'], row['wind_gust_kts']),
            'precipitation': precip_color(row['precip_rate_mmhr'], row['precip_type']),
            'severe_wx':     wwa_color(wwa_phenoms),
        }
        table.append({
            'forecast_hour':    row['forecast_hour'],
            'valid_time':       row['valid_time'].isoformat() if row['valid_time'] else None,
            'color':            composite_color(list(cats.values())),
            'categories':       cats,
            'wind_dir':         row['wind_dir'],
            'wind_speed_kts':   row['wind_speed_kts'],
            'wind_gust_kts':    row['wind_gust_kts'],
            'tmp_c':            row['tmp_c'],
            'dpt_c':            row['dpt_c'],
            'heat_index_c':     row['heat_index_c'],
            'wind_chill_c':     row['wind_chill_c'],
            'precip_rate_mmhr': row['precip_rate_mmhr'],
            'precip_type':      row['precip_type'],
            'wbgt_c':           row['wbgt_c'],
            'cape_jkg':         row['cape_jkg'],
            'ceil_ft':          row['ceil_ft'],
        })

    target = next((r for r in table if r['forecast_hour'] == forecast_hour), table[0])

    return {
        'site_id':        site_id,
        'site_name':      site['site_name'],
        'unit':           site.get('unit'),
        'site_type':      site['site_type'],
        'lat':            lat,
        'lon':            lon,
        'cap_region':     site.get('cap_region'),
        'color':          target['color'],
        'categories':     target['categories'],
        'forecast_hour':  forecast_hour,
        'valid_time':     target['valid_time'],
        'conditions': {
            'tmp_c':            target['tmp_c'],
            'dpt_c':            target['dpt_c'],
            'heat_index_c':     target['heat_index_c'],
            'wind_chill_c':     target['wind_chill_c'],
            'wind_speed_kts':   target['wind_speed_kts'],
            'wind_gust_kts':    target['wind_gust_kts'],
            'wind_dir':         target['wind_dir'],
            'precip_rate_mmhr': target['precip_rate_mmhr'],
            'precip_type':      target['precip_type'],
            'wbgt_c':           target['wbgt_c'],
            'cape_jkg':         target['cape_jkg'],
            'ceil_ft':          target['ceil_ft'],
        },
        'forecast_table': table,
    }


# ---------------------------------------------------------------------------
# Flask endpoints
# ---------------------------------------------------------------------------

def _site_to_dict(row: dict) -> dict:
    """Map DB column names to template-friendly field names."""
    return {
        'site_id':       row['id'],
        'name':          row['site_name'],
        'short_name':    row.get('unit'),
        'cap_region':    row.get('cap_region'),
        'nearest_icao':  (row.get('wx_station_override') or
                          row.get('station_id') or '').strip() or None,
        'station_id':    (row.get('station_id') or '').strip() or None,
        'lat':           row.get('lat'),
        'lon':           row.get('lon'),
        'elevation_ft':  row.get('elevation_ft'),
        'activity_type': row.get('site_type'),
        'description':   row.get('description'),
        'is_active':     row.get('is_active', True),
    }


@cadet_wx_bp.route('/api/cadet_wx/sites', methods=['GET'])
def get_sites():
    try:
        conn = _open_db()
        with _cursor(conn) as cur:
            cur.execute("""
                SELECT id, site_name, unit, site_type, station_id,
                       wx_station_override, lat, lon, elevation_ft,
                       description, cap_region, is_active
                FROM observations.cadet_sites
                WHERE is_active = TRUE
                ORDER BY cap_region, site_name
            """)
            sites = [_site_to_dict(dict(r)) for r in cur.fetchall()]
        conn.close()
        return jsonify({'sites': sites, 'count': len(sites)})
    except Exception as e:
        log.error(f"GET /api/cadet_wx/sites: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@cadet_wx_bp.route('/api/cadet_wx/nearest_metar')
def nearest_metar():
    """
    Find nearest reporting airport to a lat/lon.
    Query params: lat, lon, limit (default 5)
    Returns list of nearest airports with current METAR conditions.
    """
    try:
        lat   = request.args.get('lat', type=float)
        lon   = request.args.get('lon', type=float)
        limit = request.args.get('limit', default=5, type=int)
        if lat is None or lon is None:
            return jsonify({'error': 'lat and lon required'}), 400
        limit = min(max(limit, 1), 10)

        conn = _open_db()
        with _cursor(conn) as cur:
            # PostGIS nearest-neighbor using <-> distance operator with GiST index
            cur.execute("""
                SELECT
                    a.station_id,
                    a.name,
                    ST_Y(a.location::geometry) AS lat,
                    ST_X(a.location::geometry) AS lon,
                    a.elevation_ft,
                    a.is_military,
                    ROUND(
                        ST_Distance(
                            a.location::geography,
                            ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography
                        ) / 1852.0
                    ) AS dist_nm,
                    m.temp_c,
                    m.dewpoint_c,
                    m.wind_speed_kts,
                    m.wind_gust_kts,
                    m.wind_dir,
                    m.observation_time
                FROM observations.airports a
                LEFT JOIN LATERAL (
                    SELECT temp_c, dewpoint_c, wind_speed_kts,
                           wind_gust_kts, wind_dir, observation_time
                    FROM observations.metar
                    WHERE station_id = a.station_id
                      AND observation_time > NOW() - INTERVAL '2 hours'
                    ORDER BY observation_time DESC
                    LIMIT 1
                ) m ON TRUE
                WHERE EXISTS (
                    SELECT 1 FROM observations.metar m2
                    WHERE m2.station_id = a.station_id
                      AND m2.observation_time > NOW() - INTERVAL '2 hours'
                )
                ORDER BY a.location::geography <->
                         ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography
                LIMIT %s
            """, (lon, lat, lon, lat, limit))
            rows = [dict(r) for r in cur.fetchall()]
        conn.close()

        # Serialize datetime
        for r in rows:
            if r.get('observation_time'):
                r['observation_time'] = r['observation_time'].isoformat()

        return jsonify({'stations': rows, 'count': len(rows),
                        'query': {'lat': lat, 'lon': lon}})
    except Exception as e:
        log.error(f"/api/cadet_wx/nearest_metar: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@cadet_wx_bp.route('/api/cadet_wx/current')
def get_current():
    """Current conditions stoplight for all active cadet sites."""
    try:
        conn = _open_db()
        with _cursor(conn) as cur:
            cur.execute("""
                SELECT id, site_name, unit, site_type, station_id,
                       wx_station_override, lat, lon, cap_region
                FROM observations.cadet_sites
                WHERE is_active = TRUE
                ORDER BY id
            """)
            sites = [dict(r) for r in cur.fetchall()]
            results = []
            for site in sites:
                try:
                    results.append(build_current_stoplight(cur, site))
                except Exception as e:
                    log.error(f"Current stoplight site {site['id']}: {e}", exc_info=True)
                    results.append(_no_data_result(site['id'], site['site_name'], site.get('lat'), site.get('lon')))
        conn.close()
        return jsonify({
            'sites':     results,
            'count':     len(results),
            'generated': datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        log.error(f"/api/cadet_wx/current: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@cadet_wx_bp.route('/api/cadet_wx/forecast')
def get_forecast():
    """Forecast stoplight for all active cadet sites at a specific hour."""
    try:
        hour = max(1, min(24, int(request.args.get('hour', 6))))
    except (ValueError, TypeError):
        hour = 6
    try:
        conn = _open_db()
        with _cursor(conn) as cur:
            cur.execute("""
                SELECT id, site_name, unit, site_type, station_id,
                       wx_station_override, lat, lon, cap_region
                FROM observations.cadet_sites
                WHERE is_active = TRUE
                ORDER BY id
            """)
            sites = [dict(r) for r in cur.fetchall()]
            results = []
            for site in sites:
                try:
                    results.append(build_forecast_stoplight(cur, site, hour))
                except Exception as e:
                    log.error(f"Forecast stoplight site {site['id']}: {e}", exc_info=True)
                    results.append(_no_data_result(site['id'], site['site_name'], site.get('lat'), site.get('lon')))
        conn.close()
        return jsonify({
            'sites':         results,
            'count':         len(results),
            'forecast_hour': hour,
            'generated':     datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        log.error(f"/api/cadet_wx/forecast: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@cadet_wx_bp.route('/api/cadet_wx/site/<int:site_id>/forecast')
def get_site_forecast(site_id):
    """Full 24-hour forecast table for a single site."""
    try:
        conn = _open_db()
        with _cursor(conn) as cur:
            cur.execute("""
                SELECT id, site_name, unit, site_type, station_id,
                       wx_station_override, lat, lon, cap_region
                FROM observations.cadet_sites
                WHERE id = %s
            """, (site_id,))
            site = cur.fetchone()
            if not site:
                return jsonify({'error': 'Site not found'}), 404
            result = build_forecast_stoplight(cur, dict(site), 1)
        conn.close()
        return jsonify({
            'site_id':        site_id,
            'site_name':      result['site_name'],
            'forecast_table': result.get('forecast_table', []),
            'generated':      datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        log.error(f"/api/cadet_wx/site/{site_id}/forecast: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


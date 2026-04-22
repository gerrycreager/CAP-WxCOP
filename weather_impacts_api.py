"""
weather_impacts_api.py — Flask API for CAPR 70-1 Weather Impacts stoplight data.

Routes:
  GET /api/weather-impacts/airports       Map display — all airports for a given hour
  GET /api/weather-impacts/station/<id>   Detail panel — full 25-hour forecast
  GET /api/weather-impacts/available-hours
  GET /api/weather-impacts/status
"""

import os
import logging
from datetime import datetime, timezone
from flask import Blueprint, jsonify, request

log = logging.getLogger(__name__)
weather_impacts_api = Blueprint('weather_impacts_api', __name__)

# ── DB connection ──────────────────────────────────────────────────────────────
def get_connection():
    import psycopg2
    dsn = os.environ.get('DB_DSN',
                         'dbname=avwx_data user=avwx_user host=192.168.0.60')
    return psycopg2.connect(dsn)

# ── Helpers ────────────────────────────────────────────────────────────────────
def parse_bounds(s):
    if not s:
        return None
    try:
        parts = [float(x) for x in s.split(',')]
        return tuple(parts) if len(parts) == 4 else None
    except (ValueError, TypeError):
        return None

def color_label(c):
    return {'GREEN': 'Go', 'YELLOW': 'Caution — Marginal',
            'RED': 'No-Go / Req. Auth.'}.get(c, 'Unknown')

def fmt_ceil(ft):
    if ft is None: return None
    return 'Unlimited' if ft == 99999 else f'{ft:,} ft'

def fmt_vis(m):
    if m is None: return None
    return f'{m/1609.34:.1f} SM'

# Source priority: GLMP > HRRR > AIGFS > LAMP > other
SOURCE_PRIORITY = """
    CASE model_source
      WHEN 'GLMP_CO'  THEN 1
      WHEN 'GLMP_AK'  THEN 1
      WHEN 'GLMP_HI'  THEN 1
      WHEN 'GLMP_PR'  THEN 1
      WHEN 'HRRR'     THEN 2
      WHEN 'AIGFS'    THEN 3
      WHEN 'LAMP'     THEN 4
      ELSE 5
    END
"""

# ── GET /airports ──────────────────────────────────────────────────────────────
@weather_impacts_api.route('/airports', methods=['GET'])
def get_impacts_airports():
    try:
        hour   = int(request.args.get('hour', 1))
        op     = request.args.get('op', 'vfr').lower()
        bounds = parse_bounds(request.args.get('bounds'))
        limit  = min(int(request.args.get('limit', 5000)), 10000)

        min_rwy = int(request.args.get('min_rwy', 0))
        if op not in ('vfr', 'ifr'):
            return jsonify({'error': 'op must be vfr or ifr'}), 400
        if not (1 <= hour <= 25):
            return jsonify({'error': 'hour must be 1-25'}), 400

        color_col = 'wi.vfr_color' if op == 'vfr' else 'wi.ifr_color'
        worst_col = 'wi.vfr_worst_param' if op == 'vfr' else 'wi.ifr_worst_param'

        conn = get_connection()
        cur  = conn.cursor()

        bounds_sql = ''
        params = [hour]
        if bounds:
            w, s, e, n = bounds
            bounds_sql = "AND ST_Y(a.location) BETWEEN %s AND %s AND ST_X(a.location) BETWEEN %s AND %s"
            params += [s, n, w, e]
        if min_rwy > 0:
            bounds_sql += f' AND (a.is_military OR a.longest_runway_ft >= {int(min_rwy)})'
        params += [limit]

        # Use DISTINCT ON to get best source per airport for this forecast hour.
        # DISTINCT ON picks the first row per airport_id when sorted by priority + model_run DESC.
        cur.execute(f"""
            SELECT
                wi.station_id,
                ST_Y(a.location)    AS lat,
                ST_X(a.location)    AS lon,
                a.name,
                a.is_military,
                a.longest_runway_ft,
                wi.forecast_hour,
                wi.valid_time,
                wi.model_source,
                wi.ceil_ft,
                wi.vis_m,
                wi.wind_speed_kts,
                wi.wind_dir,
                wi.wind_gust_kts,
                wi.crosswind_kts,
                wi.best_runway_hdg,
                wi.tmp_f,
                wi.heat_index_f,
                wi.wind_chill_f,
                {color_col}         AS color,
                {worst_col}         AS worst_param,
                wi.tstm_prob,
                wi.tstm_color,
                wi.model_run
            FROM (
                SELECT DISTINCT ON (airport_id)
                    *
                FROM observations.airport_wx_impacts
                WHERE forecast_hour = %s
                ORDER BY airport_id,
                    {SOURCE_PRIORITY},
                    model_run DESC
            ) wi
            JOIN observations.airports a ON a.id = wi.airport_id
            {bounds_sql}
            ORDER BY wi.station_id
            LIMIT %s
        """, params)

        rows = cur.fetchall()
        cur.close()
        conn.close()

        airports = []
        model_run = None
        for row in rows:
            (station_id, lat, lon, name, is_military, longest_rwy,
             fhour, valid_time, model_source,
             ceil_ft, vis_m, wind_kts, wind_dir, wind_gust,
             xwind, best_hdg, tmp_f, hi_f, wc_f,
             color, worst_param, tstm_prob, tstm_color, mr) = row

            if model_run is None and mr:
                model_run = mr.strftime('%Y-%m-%dT%H:%MZ')

            airports.append({
                'station_id':     station_id,
                'lat':            float(lat) if lat else None,
                'lon':            float(lon) if lon else None,
                'name':           name,
                'is_military':    bool(is_military),
                'longest_rwy_ft': longest_rwy,
                'forecast_hour':  fhour,
                'valid_time':     valid_time.strftime('%Y-%m-%dT%H:%MZ') if valid_time else None,
                'model_source':   model_source,
                'color':          color or 'UNKNOWN',
                'color_label':    color_label(color),
                'worst_param':    worst_param,
                'ceil_ft':        ceil_ft,
                'ceil_display':   fmt_ceil(ceil_ft),
                'vis_m':          vis_m,
                'vis_display':    fmt_vis(vis_m),
                'tstm_prob':      int(tstm_prob) if tstm_prob is not None else None,
                'tstm_color':     tstm_color,
                'wind_speed_kts': float(wind_kts) if wind_kts is not None else None,
                'wind_dir':       wind_dir,
                'wind_gust_kts':  float(wind_gust) if wind_gust is not None else None,
                'crosswind_kts':  float(xwind) if xwind is not None else None,
                'best_runway_hdg': best_hdg,
                'tmp_f':          float(tmp_f) if tmp_f is not None else None,
                'heat_index_f':   float(hi_f) if hi_f is not None else None,
                'wind_chill_f':   float(wc_f) if wc_f is not None else None,
            })

        return jsonify({
            'airports':      airports,
            'count':         len(airports),
            'forecast_hour': hour,
            'operation':     op.upper(),
            'model_run':     model_run,
            'query_time':    datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%MZ'),
        })

    except Exception as e:
        log.exception('Error in get_impacts_airports')
        return jsonify({'error': str(e)}), 500


# ── GET /station/<id> ──────────────────────────────────────────────────────────
@weather_impacts_api.route('/station/<station_id>', methods=['GET'])
def get_impacts_station(station_id):
    try:
        station_id = station_id.upper()
        op         = request.args.get('op', 'vfr').lower()

        if op not in ('vfr', 'ifr'):
            return jsonify({'error': 'op must be vfr or ifr'}), 400

        conn = get_connection()
        cur  = conn.cursor()

        # For station detail: use best source (priority order), latest run of that source
        cur.execute("""
            SELECT
                wi.forecast_hour,
                wi.valid_time,
                wi.model_source,
                wi.ceil_ft,
                wi.vis_m,
                wi.wind_speed_kts,
                wi.wind_dir,
                wi.wind_gust_kts,
                wi.crosswind_kts,
                wi.best_runway_hdg,
                wi.tmp_f,
                wi.heat_index_f,
                wi.wind_chill_f,
                wi.vfr_color,
                wi.vfr_worst_param,
                wi.ifr_color,
                wi.ifr_worst_param,
                wi.tstm_prob,
                wi.tstm_color,
                wi.model_run,
                a.name,
                ST_Y(a.location) AS lat,
                ST_X(a.location) AS lon,
                a.is_military,
                a.elevation_ft,
                a.longest_runway_ft
            FROM (
                SELECT DISTINCT ON (forecast_hour)
                    *
                FROM observations.airport_wx_impacts
                WHERE station_id = %s
                ORDER BY forecast_hour,
                    CASE model_source
                      WHEN 'GLMP_CO' THEN 1 WHEN 'GLMP_AK' THEN 1
                      WHEN 'GLMP_HI' THEN 1 WHEN 'GLMP_PR' THEN 1
                      WHEN 'HRRR'    THEN 2 WHEN 'AIGFS'   THEN 3
                      WHEN 'LAMP'    THEN 4 ELSE 5
                    END,
                    model_run DESC
            ) wi
            JOIN observations.airports a ON a.station_id = %s
            ORDER BY wi.forecast_hour
        """, (station_id, station_id))

        rows = cur.fetchall()
        cur.close()
        conn.close()

        if not rows:
            return jsonify({'error': f'No data for station {station_id}'}), 404

        first = rows[0]
        station = {
            'station_id':     station_id,
            'name':           first[20],
            'lat':            float(first[21]) if first[21] else None,
            'lon':            float(first[22]) if first[22] else None,
            'is_military':    bool(first[23]),
            'elevation_ft':   first[24],
            'longest_rwy_ft': first[25],
            'model_run':      first[19].strftime('%Y-%m-%dT%H:%MZ') if first[19] else None,
        }

        forecast = []
        for row in rows:
            (fhour, valid_time, model_source,
             ceil_ft, vis_m, wind_kts, wind_dir, wind_gust,
             xwind, best_hdg, tmp_f, hi_f, wc_f,
             vfr_color, vfr_worst, ifr_color, ifr_worst,
             tstm_prob, tstm_color, model_run, *_) = row

            color = vfr_color if op == 'vfr' else ifr_color
            worst = vfr_worst if op == 'vfr' else ifr_worst

            forecast.append({
                'forecast_hour':  fhour,
                'valid_time':     valid_time.strftime('%Y-%m-%dT%H:%MZ') if valid_time else None,
                'model_source':   model_source,
                'color':          color or 'UNKNOWN',
                'color_label':    color_label(color),
                'worst_param':    worst,
                'vfr_color':      vfr_color,
                'ifr_color':      ifr_color,
                'ceil_ft':        ceil_ft,
                'ceil_display':   fmt_ceil(ceil_ft),
                'vis_m':          vis_m,
                'vis_display':    fmt_vis(vis_m),
                'wind_speed_kts': float(wind_kts) if wind_kts is not None else None,
                'wind_dir':       wind_dir,
                'wind_gust_kts':  float(wind_gust) if wind_gust is not None else None,
                'crosswind_kts':  float(xwind) if xwind is not None else None,
                'best_runway_hdg': best_hdg,
                'tmp_f':          float(tmp_f) if tmp_f is not None else None,
                'heat_index_f':   float(hi_f) if hi_f is not None else None,
                'wind_chill_f':   float(wc_f) if wc_f is not None else None,
                'tstm_prob':      int(tstm_prob) if tstm_prob is not None else None,
                'tstm_color':     tstm_color,
            })

        return jsonify({
            'station':   station,
            'operation': op.upper(),
            'forecast':  forecast,
            'count':     len(forecast),
        })

    except Exception as e:
        log.exception('Error in get_impacts_station')
        return jsonify({'error': str(e)}), 500


# ── GET /available-hours ───────────────────────────────────────────────────────
@weather_impacts_api.route('/available-hours', methods=['GET'])
def get_available_hours():
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT
                MAX(model_run),
                MIN(forecast_hour),
                MAX(forecast_hour),
                COUNT(DISTINCT forecast_hour),
                COUNT(DISTINCT station_id),
                array_agg(DISTINCT forecast_hour ORDER BY forecast_hour)
            FROM observations.airport_wx_impacts
            WHERE model_run = (SELECT MAX(model_run) FROM observations.airport_wx_impacts)
        """)
        row = cur.fetchone()
        cur.close()
        conn.close()

        if not row or not row[0]:
            return jsonify({'error': 'No weather impacts data available'}), 404

        mr, min_h, max_h, hcount, acount, hours = row
        return jsonify({
            'model_run':     mr.strftime('%Y-%m-%dT%H:%MZ'),
            'min_hour':      min_h,
            'max_hour':      max_h,
            'hour_count':    hcount,
            'airport_count': acount,
            'hours':         hours,
        })

    except Exception as e:
        log.exception('Error in get_available_hours')
        return jsonify({'error': str(e)}), 500


# ── GET /status ────────────────────────────────────────────────────────────────
@weather_impacts_api.route('/status', methods=['GET'])
def get_status():
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT
                MAX(model_run),
                MAX(ingested_at),
                COUNT(*),
                COUNT(DISTINCT station_id),
                COUNT(DISTINCT forecast_hour)
            FROM observations.airport_wx_impacts
        """)
        row = cur.fetchone()
        cur.close()
        conn.close()

        lr, li, total, airports, hours = row
        now = datetime.now(timezone.utc)
        age = round((now - lr).total_seconds() / 60) if lr else None

        return jsonify({
            'latest_model_run':  lr.strftime('%Y-%m-%dT%H:%MZ') if lr else None,
            'last_ingested_at':  li.strftime('%Y-%m-%dT%H:%MZ') if li else None,
            'model_run_age_min': age,
            'total_records':     total,
            'airport_count':     airports,
            'forecast_hours':    hours,
            'status':            'current' if age and age < 60 else 'stale',
        })

    except Exception as e:
        log.exception('Error in get_status')
        return jsonify({'error': str(e)}), 500

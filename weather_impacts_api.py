"""
weather_impacts_api.py — Flask API for CAPR 70-1 Weather Impacts stoplight data.

Serves pre-computed VFR/IFR stoplights from observations.airport_wx_impacts,
populated by ingest_glmp_impacts.py from NOMADS GLMP gridded forecasts.

Routes:
  GET /api/weather-impacts/airports
      Returns all qualifying airports with current stoplight for a given
      forecast hour and operation type (VFR/IFR).
      Query params:
        hour=N          forecast hour 1-25 (default: 1)
        op=vfr|ifr      operation type (default: vfr)
        bounds=W,S,E,N  optional viewport filter (decimal degrees)
        limit=N         max airports returned (default: 5000)

  GET /api/weather-impacts/station/<station_id>
      Returns full 25-hour forecast table for one airport.
      Query params:
        op=vfr|ifr      operation type (default: vfr)

  GET /api/weather-impacts/available-hours
      Returns available forecast hours and model run time for current cycle.

  GET /api/weather-impacts/status
      Returns pipeline status — last model run, record count, etc.
"""

import os
import logging
from datetime import datetime, timezone
from flask import Blueprint, jsonify, request

log = logging.getLogger(__name__)

weather_impacts_api = Blueprint('weather_impacts_api', __name__)

# ---------------------------------------------------------------------------
# DB connection
# ---------------------------------------------------------------------------
def get_connection():
    import psycopg2
    import psycopg2.extras
    dsn = os.environ.get('DB_DSN',
                         'dbname=avwx_data user=avwx_user host=192.168.0.60')
    return psycopg2.connect(dsn)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def parse_bounds(bounds_str):
    """Parse 'W,S,E,N' bounds string. Returns (west, south, east, north) or None."""
    if not bounds_str:
        return None
    try:
        parts = [float(x) for x in bounds_str.split(',')]
        if len(parts) != 4:
            return None
        return tuple(parts)
    except (ValueError, TypeError):
        return None


def color_label(color):
    labels = {
        'GREEN':   'Go',
        'YELLOW':  'Caution — Marginal',
        'RED':     'No-Go / Req. Auth.',
    }
    return labels.get(color, 'Unknown')


def fmt_ceil(ft):
    if ft is None:
        return None
    if ft == 99999:
        return 'Unlimited'
    return f'{ft:,} ft'


def fmt_vis(m):
    if m is None:
        return None
    sm = m / 1609.34
    return f'{sm:.1f} SM'


# ---------------------------------------------------------------------------
# GET /api/weather-impacts/airports
# ---------------------------------------------------------------------------
@weather_impacts_api.route('/airports', methods=['GET'])
def get_impacts_airports():
    """
    Return all airports with stoplight for a given forecast hour.
    This is the primary endpoint for the map display.
    """
    try:
        hour    = int(request.args.get('hour', 1))
        op      = request.args.get('op', 'vfr').lower()
        bounds  = parse_bounds(request.args.get('bounds'))
        limit   = min(int(request.args.get('limit', 5000)), 10000)

        if op not in ('vfr', 'ifr'):
            return jsonify({'error': 'op must be vfr or ifr'}), 400
        if not (1 <= hour <= 25):
            return jsonify({'error': 'hour must be 1-25'}), 400

        color_col  = 'wi.vfr_color'  if op == 'vfr' else 'wi.ifr_color'
        worst_col  = 'wi.vfr_worst_param' if op == 'vfr' else 'wi.ifr_worst_param'

        conn = get_connection()
        cur  = conn.cursor()

        # Build bounds filter
        bounds_sql = ''
        params = [hour]
        if bounds:
            west, south, east, north = bounds
            bounds_sql = """
                AND ST_Y(a.location) BETWEEN %s AND %s
                AND ST_X(a.location) BETWEEN %s AND %s
            """
            params += [south, north, west, east]
        params += [limit]

        cur.execute(f"""
            SELECT
                wi.station_id,
                ST_Y(a.location)        AS lat,
                ST_X(a.location)        AS lon,
                a.name                  AS airport_name,
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
                {color_col}             AS color,
                {worst_col}             AS worst_param,
                wi.model_run
            FROM observations.airport_wx_impacts wi
            JOIN observations.airports a ON a.id = wi.airport_id
            WHERE wi.forecast_hour = %s
              AND wi.model_run = (
                  SELECT MAX(model_run)
                  FROM observations.airport_wx_impacts
              )
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
             color, worst_param, mr) = row

            if model_run is None and mr:
                model_run = mr.strftime('%Y-%m-%dT%H:%MZ')

            airports.append({
                'station_id':    station_id,
                'lat':           float(lat) if lat else None,
                'lon':           float(lon) if lon else None,
                'name':          name,
                'is_military':   bool(is_military),
                'longest_rwy_ft': longest_rwy,
                'forecast_hour': fhour,
                'valid_time':    valid_time.strftime('%Y-%m-%dT%H:%MZ') if valid_time else None,
                'model_source':  model_source,
                'color':         color or 'UNKNOWN',
                'color_label':   color_label(color),
                'worst_param':   worst_param,
                # Raw values
                'ceil_ft':       ceil_ft,
                'ceil_display':  fmt_ceil(ceil_ft),
                'vis_m':         vis_m,
                'vis_display':   fmt_vis(vis_m),
                'wind_speed_kts': float(wind_kts) if wind_kts is not None else None,
                'wind_dir':      wind_dir,
                'wind_gust_kts': float(wind_gust) if wind_gust is not None else None,
                'crosswind_kts': float(xwind) if xwind is not None else None,
                'best_runway_hdg': best_hdg,
                'tmp_f':         float(tmp_f) if tmp_f is not None else None,
                'heat_index_f':  float(hi_f) if hi_f is not None else None,
                'wind_chill_f':  float(wc_f) if wc_f is not None else None,
            })

        return jsonify({
            'airports':    airports,
            'count':       len(airports),
            'forecast_hour': hour,
            'operation':   op.upper(),
            'model_run':   model_run,
            'query_time':  datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%MZ'),
        })

    except Exception as e:
        log.exception('Error in get_impacts_airports')
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# GET /api/weather-impacts/station/<station_id>
# ---------------------------------------------------------------------------
@weather_impacts_api.route('/station/<station_id>', methods=['GET'])
def get_impacts_station(station_id):
    """
    Return full 25-hour forecast table for one airport.
    Used by the right-side detail panel when an airport is clicked.
    """
    try:
        station_id = station_id.upper()
        op         = request.args.get('op', 'vfr').lower()

        if op not in ('vfr', 'ifr'):
            return jsonify({'error': 'op must be vfr or ifr'}), 400

        color_col = 'wi.vfr_color'  if op == 'vfr' else 'wi.ifr_color'
        worst_col = 'wi.vfr_worst_param' if op == 'vfr' else 'wi.ifr_worst_param'

        conn = get_connection()
        cur  = conn.cursor()

        cur.execute(f"""
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
                wi.model_run,
                a.name,
                ST_Y(a.location) AS lat,
                ST_X(a.location) AS lon,
                a.is_military,
                a.elevation_ft,
                a.longest_runway_ft
            FROM observations.airport_wx_impacts wi
            JOIN observations.airports a ON a.id = wi.airport_id
            WHERE wi.station_id = %s
              AND wi.model_run = (
                  SELECT MAX(model_run)
                  FROM observations.airport_wx_impacts
                  WHERE station_id = %s
              )
            ORDER BY wi.forecast_hour
        """, (station_id, station_id))

        rows = cur.fetchall()
        cur.close()
        conn.close()

        if not rows:
            return jsonify({'error': f'No data for station {station_id}'}), 404

        # Station info from first row
        first = rows[0]
        station = {
            'station_id':    station_id,
            'name':          first[18],
            'lat':           float(first[19]) if first[19] else None,
            'lon':           float(first[20]) if first[20] else None,
            'is_military':   bool(first[21]),
            'elevation_ft':  first[22],
            'longest_rwy_ft': first[23],
            'model_run':     first[17].strftime('%Y-%m-%dT%H:%MZ') if first[17] else None,
        }

        forecast = []
        for row in rows:
            (fhour, valid_time, model_source,
             ceil_ft, vis_m, wind_kts, wind_dir, wind_gust,
             xwind, best_hdg, tmp_f, hi_f, wc_f,
             vfr_color, vfr_worst, ifr_color, ifr_worst,
             model_run, *_) = row

            color      = vfr_color if op == 'vfr' else ifr_color
            worst      = vfr_worst if op == 'vfr' else ifr_worst

            forecast.append({
                'forecast_hour':  fhour,
                'valid_time':     valid_time.strftime('%Y-%m-%dT%H:%MZ') if valid_time else None,
                'model_source':   model_source,
                'color':          color or 'UNKNOWN',
                'color_label':    color_label(color),
                'worst_param':    worst,
                'vfr_color':      vfr_color,
                'ifr_color':      ifr_color,
                # Raw values
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


# ---------------------------------------------------------------------------
# GET /api/weather-impacts/available-hours
# ---------------------------------------------------------------------------
@weather_impacts_api.route('/available-hours', methods=['GET'])
def get_available_hours():
    """Return available forecast hours and model run time."""
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT
                MAX(model_run)          AS model_run,
                MIN(forecast_hour)      AS min_hour,
                MAX(forecast_hour)      AS max_hour,
                COUNT(DISTINCT forecast_hour) AS hour_count,
                COUNT(DISTINCT station_id)    AS airport_count,
                array_agg(DISTINCT forecast_hour ORDER BY forecast_hour) AS hours
            FROM observations.airport_wx_impacts
            WHERE model_run = (SELECT MAX(model_run)
                               FROM observations.airport_wx_impacts)
        """)
        row = cur.fetchone()
        cur.close()
        conn.close()

        if not row or not row[0]:
            return jsonify({'error': 'No weather impacts data available'}), 404

        model_run, min_h, max_h, hour_count, apt_count, hours = row
        return jsonify({
            'model_run':     model_run.strftime('%Y-%m-%dT%H:%MZ'),
            'min_hour':      min_h,
            'max_hour':      max_h,
            'hour_count':    hour_count,
            'airport_count': apt_count,
            'hours':         hours,
        })

    except Exception as e:
        log.exception('Error in get_available_hours')
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# GET /api/weather-impacts/status
# ---------------------------------------------------------------------------
@weather_impacts_api.route('/status', methods=['GET'])
def get_status():
    """Pipeline status — last model run, record count, age."""
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT
                MAX(model_run)      AS latest_run,
                MAX(ingested_at)    AS last_ingest,
                COUNT(*)            AS total_records,
                COUNT(DISTINCT station_id) AS airports,
                COUNT(DISTINCT forecast_hour) AS forecast_hours
            FROM observations.airport_wx_impacts
        """)
        row = cur.fetchone()
        cur.close()
        conn.close()

        latest_run, last_ingest, total, airports, hours = row
        now = datetime.now(timezone.utc)
        age_min = round((now - latest_run).total_seconds() / 60) if latest_run else None

        return jsonify({
            'latest_model_run':  latest_run.strftime('%Y-%m-%dT%H:%MZ') if latest_run else None,
            'last_ingested_at':  last_ingest.strftime('%Y-%m-%dT%H:%MZ') if last_ingest else None,
            'model_run_age_min': age_min,
            'total_records':     total,
            'airport_count':     airports,
            'forecast_hours':    hours,
            'status':            'current' if age_min and age_min < 60 else 'stale',
        })

    except Exception as e:
        log.exception('Error in get_status')
        return jsonify({'error': str(e)}), 500

"""
Weather API - Complete Working Version
Uses PostGIS geometry functions to extract lat/lon from location column
Table schema: observations.metar and observations.airports both use PostGIS 'location' column
"""

import sys
import json
sys.path.insert(0, '/var/www/cap_winds_app')
from db_config import get_connection
from datetime import datetime, timedelta
from flask import Blueprint, jsonify, request

weather_api = Blueprint('weather_api', __name__, url_prefix='/api/weather')

@weather_api.route('/metar/recent', methods=['GET'])
def get_recent_metar():
    """Get recent METAR observations within bounding box"""
    try:
        # Get bounding box parameters
        bounds_param = request.args.get('bounds', '')
        limit = int(request.args.get('limit', 500))
        
        if not bounds_param:
            return jsonify({'error': 'bounds parameter required'}), 400
            
        try:
            bounds = list(map(float, bounds_param.split(',')))
            if len(bounds) != 4:
                raise ValueError()
            west, south, east, north = bounds
        except (ValueError, TypeError):
            return jsonify({'error': 'Invalid bounds format. Expected: west,south,east,north'}), 400
        
        # Validate bounds
        if not (-180 <= west <= 180 and -180 <= east <= 180 and 
                -90 <= south <= 90 and -90 <= north <= 90):
            return jsonify({'error': 'Bounds out of valid range'}), 400
            
        if west >= east or south >= north:
            return jsonify({'error': 'Invalid bounds: west >= east or south >= north'}), 400

        conn = get_connection()
        cur = conn.cursor()
        
        # FINAL CORRECTED QUERY - Using PostGIS ST_Y/ST_X functions for lat/lon
        query = """
            WITH recent_observations AS (
                SELECT DISTINCT ON (m.station_id)
                    m.station_id,
                    ST_Y(m.location) as latitude,
                    ST_X(m.location) as longitude,
                    m.observation_time,
                    m.temp_c,
                    m.dewpoint_c,
                    m.wind_dir,
                    m.wind_speed_kts,
                    m.wind_gust_kts,
                    m.altimeter_hg,
                    m.visibility_sm,
                    m.present_weather,
                    m.sky_conditions,
                    m.flight_category,
                    m.raw_text,
                    m.is_speci,
                    a.name as airport_name,
                    a.iso_region as municipality,
                    a.is_military,
                    CASE 
                        WHEN a.is_major_hub THEN 'large_airport'
                        WHEN a.longest_runway_ft >= 8000 THEN 'medium_airport'
                        ELSE 'small_airport'
                    END as airport_type
                FROM observations.metar m
                LEFT JOIN observations.airports a ON m.station_id = a.station_id
                WHERE ST_Y(m.location) BETWEEN %s AND %s
                  AND ST_X(m.location) BETWEEN %s AND %s
                  AND m.observation_time >= NOW() - INTERVAL '2 hours'
                ORDER BY m.station_id, m.observation_time DESC
            )
            SELECT 
                station_id,
                latitude,
                longitude,
                observation_time,
                temp_c,
                dewpoint_c,
                wind_dir,
                wind_speed_kts,
                wind_gust_kts,
                altimeter_hg,
                visibility_sm,
                present_weather,
                sky_conditions,
                flight_category,
                raw_text,
                is_speci,
                airport_name,
                municipality,
                is_military,
                airport_type
            FROM recent_observations
            ORDER BY observation_time DESC
            LIMIT %s
        """
        
        cur.execute(query, (south, north, west, east, limit))
        rows = cur.fetchall()
        
        metars = []
        for row in rows:
            # Parse present weather JSON if it exists
            present_weather = []
            if row[11]:  # present_weather column
                try:
                    present_weather = json.loads(row[11]) if isinstance(row[11], str) else row[11]
                    if not isinstance(present_weather, list):
                        present_weather = []
                except (json.JSONDecodeError, TypeError):
                    present_weather = []
            
            # Parse sky conditions JSON if it exists  
            sky_conditions = []
            if row[12]:  # sky_conditions column
                try:
                    sky_conditions = json.loads(row[12]) if isinstance(row[12], str) else row[12]
                    if not isinstance(sky_conditions, list):
                        sky_conditions = []
                except (json.JSONDecodeError, TypeError):
                    sky_conditions = []
                    
            # METAR data structure
            metar_data = {
                'station_id': row[0],
                'latitude': float(row[1]) if row[1] is not None else None,
                'longitude': float(row[2]) if row[2] is not None else None,
                'observation_time': row[3].isoformat() if row[3] else None,
                'temp_c': float(row[4]) if row[4] is not None else None,
                'dewpoint_c': float(row[5]) if row[5] is not None else None,
                'wind_dir': int(row[6]) if row[6] is not None else None,
                'wind_speed_kts': int(row[7]) if row[7] is not None else None,
                'wind_gust_kts': int(row[8]) if row[8] is not None else None,
                'altimeter_hg': float(row[9]) if row[9] is not None else None,
                'visibility_sm': float(row[10]) if row[10] is not None else None,
                'present_weather': present_weather,
                'sky_conditions': sky_conditions,
                'flight_category': row[13],
                'raw_text': row[14],
                'is_speci': row[15] if row[15] is not None else False,
                'airport_name': row[16],
                'municipality': row[17],
                'is_military': row[18] if row[18] is not None else False,
                'airport_type': row[19]
            }
            
            metars.append(metar_data)
        
        cur.close()
        conn.close()
        
        # Get latest observation time for reference
        latest_obs = None
        if metars:
            latest_obs = max(m['observation_time'] for m in metars if m['observation_time'])
            if isinstance(latest_obs, str):
                latest_obs = datetime.fromisoformat(latest_obs.replace('Z', '+00:00'))
        
        return jsonify({
            'metars': metars,
            'latest_observation': latest_obs.isoformat() if latest_obs else None,
            'count': len(metars),
            'bounds': {
                'west': west,
                'south': south, 
                'east': east,
                'north': north
            },
            'query_time': datetime.utcnow().isoformat()
        })
        
    except Exception as e:
        import traceback
        error_details = {
            'error': str(e),
            'type': type(e).__name__,
            'traceback': traceback.format_exc()
        }
        return jsonify(error_details), 500

@weather_api.route('/stations', methods=['GET'])
def get_stations():
    """Get airport information"""
    try:
        bounds_param = request.args.get('bounds', '')
        if not bounds_param:
            return jsonify({'error': 'bounds parameter required'}), 400
            
        try:
            bounds = list(map(float, bounds_param.split(',')))
            if len(bounds) != 4:
                raise ValueError()
            west, south, east, north = bounds
        except (ValueError, TypeError):
            return jsonify({'error': 'Invalid bounds format. Expected: west,south,east,north'}), 400

        conn = get_connection()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT station_id, name, iso_region as municipality, 
                   ST_Y(location) as latitude, ST_X(location) as longitude,
                   elevation_ft, is_military, 
                   CASE 
                       WHEN is_major_hub THEN 'large_airport'
                       WHEN longest_runway_ft >= 8000 THEN 'medium_airport'
                       ELSE 'small_airport'
                   END as airport_type,
                   has_paved_runway, longest_runway_ft
            FROM observations.airports
            WHERE ST_Y(location) BETWEEN %s AND %s
              AND ST_X(location) BETWEEN %s AND %s
              AND has_paved_runway = true
              AND longest_runway_ft >= 2500
            ORDER BY is_military DESC, longest_runway_ft DESC, station_id
        """, (south, north, west, east))
        
        stations = []
        for row in cur.fetchall():
            stations.append({
                'station_id': row[0],
                'name': row[1],
                'municipality': row[2], 
                'latitude': float(row[3]) if row[3] else None,
                'longitude': float(row[4]) if row[4] else None,
                'elevation_ft': row[5],
                'is_military': row[6] if row[6] is not None else False,
                'airport_type': row[7],
                'has_paved_runway': row[8] if row[8] is not None else False,
                'longest_runway_ft': row[9]
            })
        
        cur.close()
        conn.close()
        
        return jsonify({
            'stations': stations,
            'count': len(stations)
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@weather_api.route('/station/<station_id>', methods=['GET'])
def get_station_detail(station_id):
    """Get detailed weather information for a specific station"""
    try:
        station_id = station_id.upper()
        
        conn = get_connection()
        cur = conn.cursor()
        
        # Get recent METARs for this station (last 6 hours)
        cur.execute("""
            SELECT 
                m.station_id,
                ST_Y(m.location) as latitude,
                ST_X(m.location) as longitude,
                m.observation_time,
                m.temp_c,
                m.dewpoint_c,
                m.wind_dir,
                m.wind_speed_kts,
                m.wind_gust_kts,
                m.altimeter_hg,
                m.visibility_sm,
                m.present_weather,
                m.sky_conditions,
                m.flight_category,
                m.raw_text,
                m.is_speci,
                a.name as airport_name,
                a.iso_region as municipality,
                a.is_military,
                a.elevation_ft,
                CASE 
                    WHEN a.is_major_hub THEN 'large_airport'
                    WHEN a.longest_runway_ft >= 8000 THEN 'medium_airport'
                    ELSE 'small_airport'
                END as airport_type
            FROM observations.metar m
            LEFT JOIN observations.airports a ON m.station_id = a.station_id
            WHERE m.station_id = %s
              AND m.observation_time >= NOW() - INTERVAL '6 hours'
            ORDER BY m.observation_time DESC
            LIMIT 10
        """, (station_id,))
        
        metar_rows = cur.fetchall()
        
        if not metar_rows:
            cur.close()
            conn.close()
            return jsonify({'error': f'No recent data found for station {station_id}'}), 404
        
        # Get TAF data if available
        taf_data = None
        try:
            cur.execute("""
                SELECT raw_text, valid_from, valid_to, issue_time
                FROM observations.taf
                WHERE station_id = %s
                  AND valid_to >= NOW()
                ORDER BY issue_time DESC
                LIMIT 1
            """, (station_id,))
            
            taf_row = cur.fetchone()
            if taf_row:
                taf_data = {
                    'raw_text': taf_row[0],
                    'valid_from': taf_row[1].isoformat() if taf_row[1] else None,
                    'valid_to': taf_row[2].isoformat() if taf_row[2] else None,
                    'issue_time': taf_row[3].isoformat() if taf_row[3] else None
                }
        except Exception:
            taf_data = None
        
        # Process METAR data
        metars = []
        station_info = None
        
        for row in metar_rows:
            # Parse JSON fields safely
            present_weather = []
            if row[11]:
                try:
                    present_weather = json.loads(row[11]) if isinstance(row[11], str) else row[11]
                    if not isinstance(present_weather, list):
                        present_weather = []
                except (json.JSONDecodeError, TypeError):
                    present_weather = []
            
            sky_conditions = []
            if row[12]:
                try:
                    sky_conditions = json.loads(row[12]) if isinstance(row[12], str) else row[12]
                    if not isinstance(sky_conditions, list):
                        sky_conditions = []
                except (json.JSONDecodeError, TypeError):
                    sky_conditions = []
            
            metar_data = {
                'observation_time': row[3].isoformat() if row[3] else None,
                'temp_c': float(row[4]) if row[4] is not None else None,
                'dewpoint_c': float(row[5]) if row[5] is not None else None,
                'wind_dir': int(row[6]) if row[6] is not None else None,
                'wind_speed_kts': int(row[7]) if row[7] is not None else None,
                'wind_gust_kts': int(row[8]) if row[8] is not None else None,
                'altimeter_hg': float(row[9]) if row[9] is not None else None,
                'visibility_sm': float(row[10]) if row[10] is not None else None,
                'present_weather': present_weather,
                'sky_conditions': sky_conditions,
                'flight_category': row[13],
                'raw_text': row[14],
                'is_speci': row[15] if row[15] is not None else False
            }
            
            metars.append(metar_data)
            
            # Set station info from first row
            if not station_info:
                station_info = {
                    'station_id': row[0],
                    'latitude': float(row[1]) if row[1] is not None else None,
                    'longitude': float(row[2]) if row[2] is not None else None,
                    'airport_name': row[16],
                    'municipality': row[17],
                    'is_military': row[18] if row[18] is not None else False,
                    'elevation_ft': row[19],
                    'airport_type': row[20]
                }
        
        cur.close()
        conn.close()
        
        return jsonify({
            'station': station_info,
            'metars': metars,
            'taf': taf_data,
            'metar_count': len(metars),
            'query_time': datetime.utcnow().isoformat()
        })
        
    except Exception as e:
        import traceback
        return jsonify({
            'error': str(e),
            'type': type(e).__name__,
            'traceback': traceback.format_exc()
        }), 500

@weather_api.route('/station/<station_id>/history', methods=['GET'])
def get_station_history(station_id):
    """Get extended METAR history for a station"""
    try:
        station_id = station_id.upper()
        hours = int(request.args.get('hours', 24))  # Default 24 hours
        
        # Limit to reasonable range
        if hours > 168:  # 1 week max
            hours = 168
        
        conn = get_connection()
        cur = conn.cursor()
        
        # Fixed SQL interval syntax - PostgreSQL string concatenation
        cur.execute("""
            SELECT 
                observation_time, temp_c, dewpoint_c, wind_dir, wind_speed_kts,
                wind_gust_kts, altimeter_hg, visibility_sm, flight_category,
                raw_text, is_speci, present_weather, sky_conditions
            FROM observations.metar
            WHERE station_id = %s
              AND observation_time >= NOW() - INTERVAL '%s hours'
            ORDER BY observation_time DESC
        """, (station_id, str(hours)))
        
        history = []
        for row in cur.fetchall():
            # Parse JSON fields
            present_weather = []
            if row[11]:
                try:
                    present_weather = json.loads(row[11]) if isinstance(row[11], str) else row[11]
                    if not isinstance(present_weather, list):
                        present_weather = []
                except (json.JSONDecodeError, TypeError):
                    present_weather = []
            
            sky_conditions = []
            if row[12]:
                try:
                    sky_conditions = json.loads(row[12]) if isinstance(row[12], str) else row[12]
                    if not isinstance(sky_conditions, list):
                        sky_conditions = []
                except (json.JSONDecodeError, TypeError):
                    sky_conditions = []
            
            history.append({
                'observation_time': row[0].isoformat() if row[0] else None,
                'temp_c': float(row[1]) if row[1] is not None else None,
                'dewpoint_c': float(row[2]) if row[2] is not None else None,
                'wind_dir': int(row[3]) if row[3] is not None else None,
                'wind_speed_kts': int(row[4]) if row[4] is not None else None,
                'wind_gust_kts': int(row[5]) if row[5] is not None else None,
                'altimeter_hg': float(row[6]) if row[6] is not None else None,
                'visibility_sm': float(row[7]) if row[7] is not None else None,
                'flight_category': row[8],
                'raw_text': row[9],
                'is_speci': row[10] if row[10] is not None else False,
                'present_weather': present_weather,
                'sky_conditions': sky_conditions
            })
        
        cur.close()
        conn.close()
        
        return jsonify({
            'station_id': station_id,
            'history': history,
            'hours_requested': hours,
            'count': len(history),
            'query_time': datetime.utcnow().isoformat()
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@weather_api.route('/wind-constraints', methods=['GET'])
def get_wind_constraints():
    """Get wind constraint analysis for airports"""
    try:
        bounds_param = request.args.get('bounds', '')
        if not bounds_param:
            return jsonify({'error': 'bounds parameter required'}), 400

        try:
            bounds = list(map(float, bounds_param.split(',')))
            if len(bounds) != 4:
                raise ValueError()
            west, south, east, north = bounds
        except (ValueError, TypeError):
            return jsonify({'error': 'Invalid bounds format'}), 400

        conn = get_connection()
        cur = conn.cursor()
        
        # Check if wind_constraints table exists, if not return empty result
        cur.execute("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables 
                WHERE table_schema = 'observations' 
                  AND table_name = 'wind_constraints'
            )
        """)
        
        if not cur.fetchone()[0]:
            cur.close()
            conn.close()
            return jsonify({'wind_constraints': [], 'count': 0, 'note': 'Wind constraints table not available'})
        
        cur.execute("""
            SELECT w.station_id, w.constraint_level, w.wind_speed_kts, 
                   w.wind_dir, w.valid_time,
                   a.name, ST_Y(a.location) as lat, ST_X(a.location) as lon
            FROM observations.wind_constraints w
            LEFT JOIN observations.airports a ON w.station_id = a.station_id
            WHERE w.valid_time >= NOW() - INTERVAL '3 hours'
              AND ST_Y(a.location) BETWEEN %s AND %s
              AND ST_X(a.location) BETWEEN %s AND %s
            ORDER BY w.station_id, w.valid_time DESC
        """, (south, north, west, east))
        
        constraints = []
        for row in cur.fetchall():
            constraints.append({
                'station_id': row[0],
                'constraint_level': row[1],
                'wind_speed_kts': row[2],
                'wind_dir': row[3],
                'valid_time': row[4].isoformat() if row[4] else None,
                'airport_name': row[5],
                'latitude': float(row[6]) if row[6] else None,
                'longitude': float(row[7]) if row[7] else None
            })
        
        cur.close()
        conn.close()
        
        return jsonify({'wind_constraints': constraints, 'count': len(constraints)})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@weather_api.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint for weather API"""
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        # Test METAR table
        cur.execute("SELECT COUNT(*) FROM observations.metar WHERE observation_time >= NOW() - INTERVAL '1 hour'")
        recent_metars = cur.fetchone()[0]
        
        # Test airports table  
        cur.execute("SELECT COUNT(*) FROM observations.airports")
        total_airports = cur.fetchone()[0]
        
        cur.close()
        conn.close()
        
        return jsonify({
            'status': 'healthy',
            'database': 'connected',
            'recent_metars': recent_metars,
            'total_airports': total_airports,
            'timestamp': datetime.utcnow().isoformat()
        })
        
    except Exception as e:
        return jsonify({
            'status': 'unhealthy', 
            'error': str(e),
            'timestamp': datetime.utcnow().isoformat()
        }), 500

@weather_api.route('/no-metar-airports', methods=['GET'])
def get_no_metar_airports():
    """
    Return airports with paved runways > 2500 ft that have had no METAR
    observation in the past 2 hours.  These are shown on the weather map
    as white/hollow dots to indicate the airfield exists but has no wx data.

    Optional query params:
      bounds=west,south,east,north  (default: CONUS)
      limit=N                       (default: 5000)
    """
    try:
        bounds_param = request.args.get('bounds', '-125,24,-66,50')
        limit        = int(request.args.get('limit', 5000))

        try:
            bounds = list(map(float, bounds_param.split(',')))
            if len(bounds) != 4:
                raise ValueError()
            west, south, east, north = bounds
        except (ValueError, TypeError):
            return jsonify({'error': 'Invalid bounds format. Expected: west,south,east,north'}), 400

        conn = get_connection()
        cur  = conn.cursor()

        # LEFT JOIN metar — keep only airports with no recent observation.
        # is_military airports are included regardless of runway flag so
        # military fields without METARs still appear.
        cur.execute("""
            SELECT
                a.station_id,
                ST_Y(a.location::geometry) AS latitude,
                ST_X(a.location::geometry) AS longitude,
                a.name AS airport_name,
                a.iso_region,
                a.is_military,
                a.longest_runway_ft,
                CASE
                    WHEN a.is_major_hub              THEN 'large_airport'
                    WHEN a.longest_runway_ft >= 8000 THEN 'medium_airport'
                    ELSE                                  'small_airport'
                END AS airport_type
            FROM observations.airports a
            LEFT JOIN (
                SELECT DISTINCT ON (station_id) station_id
                FROM observations.metar
                WHERE observation_time >= NOW() - INTERVAL '2 hours'
            ) recent ON a.station_id = recent.station_id
            WHERE recent.station_id IS NULL        -- no recent METAR
              AND (
                  (a.has_paved_runway = true AND a.longest_runway_ft > 2500)
                  OR a.is_military = true
              )
              AND a.location IS NOT NULL
              AND ST_Y(a.location::geometry) BETWEEN %s AND %s
              AND ST_X(a.location::geometry) BETWEEN %s AND %s
            ORDER BY a.is_military DESC, a.longest_runway_ft DESC NULLS LAST
            LIMIT %s
        """, (south, north, west, east, limit))

        airports = []
        for row in cur.fetchall():
            airports.append({
                'station_id':        row[0],
                'latitude':          float(row[1]) if row[1] is not None else None,
                'longitude':         float(row[2]) if row[2] is not None else None,
                'airport_name':      row[3],
                'municipality':      row[4],
                'is_military':       bool(row[5]) if row[5] is not None else False,
                'longest_runway_ft': int(row[6]) if row[6] else None,
                'airport_type':      row[7],
                'no_metar':          True,
            })

        cur.close()
        conn.close()

        return jsonify({
            'airports': airports,
            'count':    len(airports),
            'query_time': datetime.utcnow().isoformat(),
        })

    except Exception as e:
        import traceback
        return jsonify({
            'error':     str(e),
            'type':      type(e).__name__,
            'traceback': traceback.format_exc(),
        }), 500


# Backwards compatibility routes
@weather_api.route('/metar', methods=['GET'])
def get_metar_compat():
    """Compatibility route for /metar (redirects to /metar/recent)"""
    return get_recent_metar()


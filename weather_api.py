"""
Weather Data API Blueprint
Provides METAR and TAF data via REST API
"""
from flask import Blueprint, jsonify, request
import sys
import json
sys.path.insert(0, '/var/www/cap_winds_app')
from db_config import get_connection
from datetime import datetime, timedelta
from taf_decoder import decode_taf, format_taf_for_display
from runway_analysis import analyze_runways_for_wind, format_runway_analysis_html

weather_api = Blueprint('weather_api', __name__, url_prefix='/api/weather')


def calculate_distance(lat1, lon1, lat2, lon2):
    """Calculate distance in nautical miles using Haversine formula"""
    from math import radians, cos, sin, asin, sqrt
    
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    c = 2 * asin(sqrt(a))
    r_nm = 3440.065  # Earth radius in nautical miles
    return c * r_nm


# =============================================================================
# NEW: COMBINED STATION ENDPOINT (for weather_station.html)
# =============================================================================

@weather_api.route('/station/<station_id>')
def get_station_weather(station_id):
    """
    Get complete weather for a station (METAR + TAF + Runway Analysis)
    Used by weather_station.html
    
    Query parameters:
    - radius: Include stations within N nm (default: 0 = single station)
    
    Returns:
        {
          "stations": [
            {
              "station_id": "KMCO",
              "distance_nm": 0,
              "observations": [...],  # METAR/SPECI with context
              "taf": {...},           # TAF data
              "runway_analysis_html": "..."
            }
          ]
        }
    """
    try:
        station_id = station_id.upper().strip()
        radius_nm = request.args.get('radius', 0, type=float)
        
        if len(station_id) != 4 or not station_id.isalpha():
            return jsonify({'error': 'Invalid station ID'}), 400
        
        conn = get_connection()
        cur = conn.cursor()
        
        # Get primary station location
        cur.execute("""
            SELECT DISTINCT ON (station_id)
                station_id, 
                ST_Y(location) as latitude,
                ST_X(location) as longitude
            FROM observations.metar
            WHERE station_id = %s
            AND location IS NOT NULL
            ORDER BY station_id, observation_time DESC
        """, (station_id,))
        
        primary_station = cur.fetchone()
        
        if not primary_station:
            cur.close()
            conn.close()
            return jsonify({'error': 'Station not found'}), 404
        
        # Get stations within radius
        stations_to_fetch = [station_id]
        station_distances = {station_id: 0}
        
        if radius_nm > 0:
            cur.execute("""
                SELECT DISTINCT ON (station_id)
                    station_id,
                    ST_Y(location) as latitude,
                    ST_X(location) as longitude
                FROM observations.metar
                WHERE station_id != %s
                AND location IS NOT NULL
                AND observation_time > NOW() - INTERVAL '6 hours'
                ORDER BY station_id, observation_time DESC
            """, (station_id,))
            
            nearby = cur.fetchall()
            
            for stn in nearby:
                dist = calculate_distance(
                    primary_station[1], primary_station[2],  # primary lat, lon
                    stn[1], stn[2]  # nearby lat, lon
                )
                
                if dist <= radius_nm:
                    stations_to_fetch.append(stn[0])
                    station_distances[stn[0]] = round(dist, 1)
        
        # Get weather for all stations
        result = {'stations': []}
        
        for stn_id in stations_to_fetch:
            # Get METAR observations (last 3 hours)
            cur.execute("""
                SELECT 
                    station_id,
                    observation_time,
                    raw_text,
                    temp_c,
                    dewpoint_c,
                    wind_dir,
                    wind_speed_kts,
                    wind_gust_kts,
                    visibility_sm,
                    altimeter_hg,
                    flight_category,
                    sky_conditions,
                    present_weather,
                    ST_Y(location) as latitude,
                    ST_X(location) as longitude,
                    is_speci
                FROM observations.metar
                WHERE station_id = %s
                AND observation_time > NOW() - INTERVAL '3 hours'
                ORDER BY observation_time DESC
            """, (stn_id,))
            
            metars = cur.fetchall()
            
            if not metars:
                continue
            
            # Build observations list
            observations = []
            
            for i, metar in enumerate(metars):
                raw = metar[2]
                obs_type = 'SPECI' if (metar[15] or 'SPECI' in raw) else 'METAR'
                
                obs = {
                    'type': obs_type,
                    'station_id': metar[0],
                    'observation_time': metar[1].isoformat() if metar[1] else None,
                    'raw_text': metar[2],
                    'temp_c': metar[3],
                    'dewpoint_c': metar[4],
                    'wind_dir': metar[5],
                    'wind_speed_kts': metar[6],
                    'wind_gust_kts': metar[7],
                    'visibility_sm': metar[8],
                    'altimeter_hg': metar[9],
                    'flight_category': metar[10],
                    'sky_conditions': metar[11],
                    'present_weather': metar[12],
                    'latitude': metar[13],
                    'longitude': metar[14]
                }
                
                # Add context for SPECIs (previous observations)
                if obs_type == 'SPECI' and i < len(metars) - 1:
                    obs['context'] = []
                    for ctx_metar in metars[i+1:min(i+3, len(metars))]:
                        ctx_raw = ctx_metar[2]
                        ctx_type = 'SPECI' if (ctx_metar[15] or 'SPECI' in ctx_raw) else 'METAR'
                        obs['context'].append({
                            'type': ctx_type,
                            'time': ctx_metar[1].isoformat() if ctx_metar[1] else None,
                            'raw_text': ctx_metar[2],
                            'flight_category': ctx_metar[10]
                        })
                
                observations.append(obs)
            
            # Get TAF
            cur.execute("""
                SELECT 
                    station_id,
                    issue_time,
                    valid_from,
                    valid_to,
                    raw_text
                FROM observations.taf
                WHERE station_id = %s
                AND valid_to > NOW()
                ORDER BY issue_time DESC
                LIMIT 1
            """, (stn_id,))
            
            taf_row = cur.fetchone()
            taf_data = None
            
            if taf_row:
                # Decode TAF
                try:
                    decoded = decode_taf(taf_row[4])
                    decoded_html = format_taf_for_display(decoded) if decoded else None
                except:
                    decoded = None
                    decoded_html = None
                
                taf_data = {
                    'station_id': taf_row[0],
                    'issue_time': taf_row[1].isoformat() if taf_row[1] else None,
                    'valid_from': taf_row[2].isoformat() if taf_row[2] else None,
                    'valid_to': taf_row[3].isoformat() if taf_row[3] else None,
                    'raw_text': taf_row[4],
                    'decoded': decoded,
                    'decoded_html': decoded_html
                }
            
            # Get runway analysis for most recent observation
            runway_analysis_html = None
            if observations:
                latest = observations[0]
                try:
                    analysis = analyze_runways_for_wind(
                        stn_id,
                        latest['wind_dir'],
                        latest['wind_speed_kts'],
                        latest['wind_gust_kts']
                    )
                    runway_analysis_html = format_runway_analysis_html(analysis)
                except:
                    runway_analysis_html = None
            
            # Add station to results
            result['stations'].append({
                'station_id': stn_id,
                'distance_nm': station_distances[stn_id],
                'observations': observations,
                'taf': taf_data,
                'runway_analysis_html': runway_analysis_html
            })
        
        cur.close()
        conn.close()
        
        return jsonify(result)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# =============================================================================
# NEW: SIMPLE TAF ENDPOINT (for weather_map.html popups)
# =============================================================================

@weather_api.route('/taf/<station_id>')
def get_taf_simple(station_id):
    """
    Get current TAF for a station (simple version for map popups)
    Used by weather_map.html
    
    Returns:
        {
          "station_id": "KMCO",
          "issue_time": "2026-01-19T14:00:00",
          "valid_from": "2026-01-19T15:00:00",
          "valid_to": "2026-01-20T15:00:00",
          "raw_text": "TAF KMCO ...",
          "age_minutes": 25
        }
    """
    try:
        station_id = station_id.upper().strip()
        
        if len(station_id) != 4 or not station_id.isalpha():
            return jsonify({'error': 'Invalid station ID'}), 400
        
        conn = get_connection()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT 
                station_id,
                issue_time,
                valid_from,
                valid_to,
                raw_text,
                created_at
            FROM observations.taf
            WHERE station_id = %s
            AND valid_to > NOW()
            ORDER BY issue_time DESC
            LIMIT 1
        """, (station_id,))
        
        row = cur.fetchone()
        cur.close()
        conn.close()
        
        if not row:
            return jsonify({'error': 'No TAF found'}), 404
        
        age_minutes = None
        if row[1]:
            age_minutes = int((datetime.utcnow() - row[1]).total_seconds() / 60)
        
        return jsonify({
            'station_id': row[0],
            'issue_time': row[1].isoformat() if row[1] else None,
            'valid_from': row[2].isoformat() if row[2] else None,
            'valid_to': row[3].isoformat() if row[3] else None,
            'raw_text': row[4],
            'created_at': row[5].isoformat() if row[5] else None,
            'age_minutes': age_minutes
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# =============================================================================
# EXISTING ENDPOINTS (unchanged below)
# =============================================================================

@weather_api.route('/metar/recent')
def get_recent_metars():
    """
    Get recent METAR observations
    
    Query parameters:
    - max_age: Maximum age in hours (default: 2)
    - limit: Maximum number of results (default: 500, max: 5000)
    - flight_category: Filter by VFR/MVFR/IFR/LIFR (optional)
    - bounds: Bounding box as "west,south,east,north" (optional)
    - center: Center point as "lat,lon" with radius in nm (optional)
    - radius: Radius in nautical miles (default: 100, requires center)
    """
    try:
        # Parse parameters
        max_age_hours = int(request.args.get('max_age', 2))
        limit = min(int(request.args.get('limit', 500)), 5000)
        flight_category = request.args.get('flight_category', '').upper()
        bounds = request.args.get('bounds')
        center = request.args.get('center')
        radius_nm = float(request.args.get('radius', 100))
        
        conn = get_connection()
        cur = conn.cursor()
        
        # Build query
        query = """
            SELECT 
                station_id,
                observation_time,
                raw_text,
                temp_c,
                dewpoint_c,
                wind_dir,
                wind_speed_kts,
                wind_gust_kts,
                visibility_sm,
                altimeter_hg,
                flight_category,
                sky_conditions,
                present_weather,
                ST_X(location) as longitude,
                ST_Y(location) as latitude,
                is_speci
            FROM observations.metar
            WHERE observation_time > NOW() - INTERVAL '%s hours'
        """
        params = [max_age_hours]
        
        # Add flight category filter
        if flight_category and flight_category in ['VFR', 'MVFR', 'IFR', 'LIFR']:
            query += " AND flight_category = %s"
            params.append(flight_category)
        
        # Add spatial filter
        if bounds:
            # Bounding box filter
            west, south, east, north = map(float, bounds.split(','))
            
            # Check for International Date Line crossing (west > east)
            if west > east:
                # Query spans the date line - split into two boxes
                query += """
                    AND (
                        location && ST_MakeEnvelope(%s, %s, 180, %s, 4326)
                        OR
                        location && ST_MakeEnvelope(-180, %s, %s, %s, 4326)
                    )
                """
                params.extend([west, south, north, south, east, north])
            else:
                # Normal bounding box (doesn't cross date line)
                query += """
                    AND location && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
                """
                params.extend([west, south, east, north])
        
        elif center:
            # Radius filter
            lat, lon = map(float, center.split(','))
            radius_meters = radius_nm * 1852  # Convert nm to meters
            query += """
                AND ST_DWithin(
                    location::geography,
                    ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                    %s
                )
            """
            params.extend([lon, lat, radius_meters])
        
        # Get most recent observation per station using DISTINCT ON
        query = f"""
            WITH filtered AS (
                {query}
            )
            SELECT DISTINCT ON (station_id)
                station_id, observation_time, raw_text, temp_c, dewpoint_c,
                wind_dir, wind_speed_kts, wind_gust_kts, visibility_sm, altimeter_hg,
                flight_category, sky_conditions, present_weather, longitude, latitude, is_speci
            FROM filtered
            ORDER BY station_id, observation_time DESC
            LIMIT %s
        """
        params.append(limit)
        
        cur.execute(query, params)
        
        metars = []
        for row in cur.fetchall():
            metars.append({
                'station_id': row[0],
                'observation_time': row[1].isoformat() if row[1] else None,
                'raw_text': row[2],
                'temp_c': row[3],
                'dewpoint_c': row[4],
                'wind_dir': row[5],
                'wind_speed_kts': row[6],
                'wind_gust_kts': row[7],
                'visibility_sm': row[8],
                'altimeter_hg': row[9],
                'flight_category': row[10],
                'sky_conditions': row[11],
                'present_weather': row[12],
                'longitude': row[13],
                'latitude': row[14],
                'is_speci': row[15]
            })
        
        cur.close()
        conn.close()
        
        return jsonify({
            'count': len(metars),
            'metars': metars,
            'timestamp': datetime.utcnow().isoformat()
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@weather_api.route('/metar/station/<station_id>')
def get_station_metar(station_id):
    """
    Get recent METARs for a specific station
    
    Query parameters:
    - hours: Number of hours to look back (default: 24)
    """
    try:
        hours = int(request.args.get('hours', 24))
        station_id = station_id.upper()
        
        conn = get_connection()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT 
                station_id,
                observation_time,
                raw_text,
                temp_c,
                dewpoint_c,
                wind_dir,
                wind_speed_kts,
                wind_gust_kts,
                visibility_sm,
                altimeter_hg,
                flight_category,
                sky_conditions,
                present_weather,
                ST_X(location) as longitude,
                ST_Y(location) as latitude,
                is_speci
            FROM observations.metar
            WHERE station_id = %s
              AND observation_time > NOW() - INTERVAL '%s hours'
            ORDER BY observation_time DESC
        """, (station_id, hours))
        
        metars = []
        for row in cur.fetchall():
            metars.append({
                'station_id': row[0],
                'observation_time': row[1].isoformat() if row[1] else None,
                'raw_text': row[2],
                'temp_c': row[3],
                'dewpoint_c': row[4],
                'wind_dir': row[5],
                'wind_speed_kts': row[6],
                'wind_gust_kts': row[7],
                'visibility_sm': row[8],
                'altimeter_hg': row[9],
                'flight_category': row[10],
                'sky_conditions': row[11],
                'present_weather': row[12],
                'longitude': row[13],
                'latitude': row[14],
                'is_speci': row[15]
            })
        
        cur.close()
        conn.close()
        
        if not metars:
            return jsonify({'error': 'Station not found or no recent data'}), 404
        
        # Add runway analysis for the most recent METAR
        runway_analysis = None
        runway_analysis_html = None
        if metars:
            latest = metars[0]
            try:
                analysis = analyze_runways_for_wind(
                    station_id,
                    latest['wind_dir'],
                    latest['wind_speed_kts'],
                    latest['wind_gust_kts']
                )
                runway_analysis = analysis
                runway_analysis_html = format_runway_analysis_html(analysis)
            except:
                pass
        
        return jsonify({
            'station_id': station_id,
            'count': len(metars),
            'metars': metars,
            'runway_analysis': runway_analysis,
            'runway_analysis_html': runway_analysis_html,
            'timestamp': datetime.utcnow().isoformat()
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@weather_api.route('/taf/station/<station_id>')
def get_station_taf(station_id):
    """
    Get current TAF for a station with decoding
    
    Returns TAF with decoded weather information
    """
    try:
        station_id = station_id.upper()
        
        conn = get_connection()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT 
                station_id,
                issue_time,
                valid_from,
                valid_to,
                raw_text,
                ST_X(location) as longitude,
                ST_Y(location) as latitude
            FROM observations.taf
            WHERE station_id = %s
              AND valid_to > NOW()
            ORDER BY issue_time DESC
            LIMIT 1
        """, (station_id,))
        
        row = cur.fetchone()
        
        cur.close()
        conn.close()
        
        if not row:
            return jsonify({'error': 'No TAF found for station'}), 404
        
        # Decode the TAF
        try:
            decoded = decode_taf(row[4])
            decoded_html = format_taf_for_display(decoded) if decoded else None
        except:
            decoded = None
            decoded_html = None
        
        taf = {
            'station_id': row[0],
            'issue_time': row[1].isoformat() if row[1] else None,
            'valid_from': row[2].isoformat() if row[2] else None,
            'valid_to': row[3].isoformat() if row[3] else None,
            'raw_text': row[4],
            'decoded': decoded,
            'decoded_html': decoded_html,
            'longitude': row[5],
            'latitude': row[6]
        }
        
        return jsonify(taf)
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@weather_api.route('/stations/search')
def search_stations():
    """
    Search for stations by identifier or name
    
    Query parameters:
    - q: Search query (station ID or partial name)
    - limit: Maximum results (default: 20)
    """
    try:
        query = request.args.get('q', '').upper()
        limit = min(int(request.args.get('limit', 20)), 100)
        
        if len(query) < 2:
            return jsonify({'error': 'Query must be at least 2 characters'}), 400
        
        conn = get_connection()
        cur = conn.cursor()
        
        # Search in METAR stations (most recent observation)
        cur.execute("""
            WITH latest AS (
                SELECT DISTINCT ON (station_id)
                    station_id,
                    observation_time,
                    flight_category,
                    ST_X(location) as longitude,
                    ST_Y(location) as latitude
                FROM observations.metar
                WHERE station_id LIKE %s
                  AND observation_time > NOW() - INTERVAL '6 hours'
                ORDER BY station_id, observation_time DESC
            )
            SELECT * FROM latest
            ORDER BY station_id
            LIMIT %s
        """, (f'{query}%', limit))
        
        stations = []
        for row in cur.fetchall():
            stations.append({
                'station_id': row[0],
                'last_observation': row[1].isoformat() if row[1] else None,
                'flight_category': row[2],
                'longitude': row[3],
                'latitude': row[4]
            })
        
        cur.close()
        conn.close()
        
        return jsonify({
            'query': query,
            'count': len(stations),
            'stations': stations
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@weather_api.route('/stats')
def get_stats():
    """
    Get database statistics
    """
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        # METAR stats
        cur.execute("""
            SELECT 
                COUNT(*) as total,
                COUNT(DISTINCT station_id) as stations,
                MAX(observation_time) as latest,
                COUNT(CASE WHEN flight_category = 'VFR' THEN 1 END) as vfr,
                COUNT(CASE WHEN flight_category = 'MVFR' THEN 1 END) as mvfr,
                COUNT(CASE WHEN flight_category = 'IFR' THEN 1 END) as ifr,
                COUNT(CASE WHEN flight_category = 'LIFR' THEN 1 END) as lifr
            FROM observations.metar
            WHERE observation_time > NOW() - INTERVAL '2 hours'
        """)
        
        metar_row = cur.fetchone()
        
        # TAF stats
        cur.execute("""
            SELECT 
                COUNT(*) as total,
                COUNT(DISTINCT station_id) as stations,
                MAX(issue_time) as latest
            FROM observations.taf
            WHERE valid_to > NOW()
        """)
        
        taf_row = cur.fetchone()
        
        cur.close()
        conn.close()
        
        return jsonify({
            'metar': {
                'total_recent': metar_row[0],
                'stations': metar_row[1],
                'latest': metar_row[2].isoformat() if metar_row[2] else None,
                'by_category': {
                    'VFR': metar_row[3],
                    'MVFR': metar_row[4],
                    'IFR': metar_row[5],
                    'LIFR': metar_row[6]
                }
            },
            'taf': {
                'total_active': taf_row[0],
                'stations': taf_row[1],
                'latest': taf_row[2].isoformat() if taf_row[2] else None
            },
            'timestamp': datetime.utcnow().isoformat()
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@weather_api.route('/tfr/active')
def get_active_tfrs():
    """
    Get all active TFRs

    Query parameters:
    - bounds: Bounding box "west,south,east,north" (optional)
    """
    try:
        bounds = request.args.get('bounds')

        conn = get_connection()
        cur = conn.cursor()

        query = """
            SELECT
                tfr_number,
                notam_id,
                effective_start,
                effective_end,
                facility,
                city,
                state,
                type,
                ST_AsGeoJSON(geometry) as geometry,
                lower_altitude_ft,
                upper_altitude_ft,
                raw_data
            FROM observations.tfr
            WHERE active = TRUE
              AND effective_end > NOW()
        """

        params = []

        # Add spatial filter if bounds provided
        if bounds:
            west, south, east, north = map(float, bounds.split(','))
            query += """
                AND ST_Intersects(
                    geometry,
                    ST_MakeEnvelope(%s, %s, %s, %s, 4326)
                )
            """
            params.extend([west, south, east, north])

        query += " ORDER BY effective_start DESC"

        cur.execute(query, params)

        tfrs = []
        for row in cur.fetchall():
            tfrs.append({
                'tfr_number': row[0],
                'notam_id': row[1],
                'effective_start': row[2].isoformat() if row[2] else None,
                'effective_end': row[3].isoformat() if row[3] else None,
                'facility': row[4],
                'city': row[5],
                'state': row[6],
                'type': row[7],
                'geometry': json.loads(row[8]) if row[8] else None,
                'lower_altitude_ft': row[9],
                'upper_altitude_ft': row[10],
                'raw_data': row[11]
            })

        cur.close()
        conn.close()

        return jsonify({
            'count': len(tfrs),
            'tfrs': tfrs,
            'timestamp': datetime.utcnow().isoformat()
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


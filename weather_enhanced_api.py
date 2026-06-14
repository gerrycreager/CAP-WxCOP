"""
Enhanced Weather API - New Separate File
Creates /api/weather-enhanced endpoints with military prioritization
Keeps original weather_api.py completely intact
"""

import sys
import json
sys.path.insert(0, '/var/www/cap_winds_app')
from db_config import get_connection
from datetime import datetime, timedelta
from flask import Blueprint, jsonify, request

# New blueprint with different name to avoid conflicts
weather_enhanced_api = Blueprint('weather_enhanced_api', __name__, url_prefix='/api/weather-enhanced')

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

@weather_enhanced_api.route('/metar/recent', methods=['GET'])
def get_recent_metar_enhanced():
    """
    Enhanced METAR endpoint with military prioritization and increased station limit
    URL: /api/weather-enhanced/metar/recent
    Prioritizes: Military -> Major -> Regional -> Small airports
    Default limit increased to 2500 stations
    """
    try:
        # Get bounding box parameters
        bounds_param = request.args.get('bounds', '')
        limit = int(request.args.get('limit', 2500))  # Increased from 500 to 2500
        
        if not bounds_param:
            return jsonify({'error': 'bounds parameter required'}), 400
            
        try:
            bounds = list(map(float, bounds_param.split(',')))
            if len(bounds) != 4:
                raise ValueError()
            west, south, east, north = bounds
        except (ValueError, TypeError):
            return jsonify({'error': 'Invalid bounds format. Expected: west,south,east,north'}), 400
        
        # Clamp lat to valid range
        south = max(-90.0, min(90.0, south))
        north = max(-90.0, min(90.0, north))
        if south >= north:
            return jsonify({'error': 'Invalid bounds: south >= north'}), 400

        # Normalize longitude to [-180, 180]
        # Leaflet reports e.g. -209 when panned west past antimeridian
        while west < -180.0: west += 360.0
        while west >  180.0: west -= 360.0
        while east < -180.0: east += 360.0
        while east >  180.0: east -= 360.0
        # After normalization, west > east means view crosses antimeridian
        antimeridian = (west > east)

        conn = get_connection()
        cur = conn.cursor()
        
        # Enhanced query with military prioritization
        query = """
            WITH prioritized_stations AS (
                SELECT DISTINCT ON (m.station_id)
                    m.station_id,
                    ST_Y(m.location::geometry) AS latitude,
                    ST_X(m.location::geometry) AS longitude,
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
                    -- Military prioritization logic
                    CASE
                        WHEN a.is_military = true        THEN 1
                        WHEN a.is_major_hub = true       THEN 2
                        WHEN a.longest_runway_ft >= 5000 THEN 3
                        WHEN a.longest_runway_ft >= 2500 THEN 4
                        ELSE 5
                    END as priority,
                    a.name as airport_name,
                    a.iso_region,
                    a.is_military,
                    a.longest_runway_ft,
                    CASE
                        WHEN a.is_major_hub = true       THEN 'large_airport'
                        WHEN a.longest_runway_ft >= 8000 THEN 'medium_airport'
                        ELSE                                  'small_airport'
                    END as airport_type
                FROM observations.metar m
                LEFT JOIN observations.airports a ON m.station_id = a.station_id
                WHERE ST_Intersects(
                        m.location::geometry,
                        {geom}
                      )
                  AND m.observation_time >= NOW() - INTERVAL '2 hours'
                  AND m.location IS NOT NULL
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
                iso_region,
                is_military,
                longest_runway_ft,
                airport_type,
                priority
            FROM prioritized_stations
            ORDER BY
                priority ASC,
                is_military DESC NULLS LAST,
                longest_runway_ft DESC NULLS LAST,
                observation_time DESC
            LIMIT %s
        """
        
        # Build geometry inline in SQL — cannot use %s for PostGIS functions
        if antimeridian:
            geom = (f'ST_Union('
                    f'ST_MakeEnvelope(-180,{south},{east},{north},4326),'
                    f'ST_MakeEnvelope({west},{south},180,{north},4326))')
        else:
            geom = f'ST_MakeEnvelope({west},{south},{east},{north},4326)'
        cur.execute(query.format(geom=geom), (limit,))
        rows = cur.fetchall()
        
        metars = []
        for row in rows:
            # present_weather is text[] — psycopg2 returns it as a Python list directly
            present_weather = row[11] if isinstance(row[11], list) else []
            
            # Parse sky conditions JSON if it exists  
            sky_conditions = []
            if row[12]:  # sky_conditions column
                try:
                    sky_conditions = json.loads(row[12]) if isinstance(row[12], str) else row[12]
                    if not isinstance(sky_conditions, list):
                        sky_conditions = []
                except (json.JSONDecodeError, TypeError):
                    sky_conditions = []
                    
            # Enhanced METAR data with labels and military status
            metar_data = {
                'station_id': row[0],
                'latitude': float(row[1]) if row[1] is not None else None,
                'longitude': float(row[2]) if row[2] is not None else None,
                'observation_time': row[3].strftime('%Y-%m-%dT%H:%M:%SZ') if row[3] else None,
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
                
                # Enhanced labeling information
                'airport_name':      row[16],
                'municipality':      row[17],
                'is_military':       bool(row[18]) if row[18] is not None else False,
                'longest_runway_ft': int(row[19]) if row[19] is not None else None,
                'airport_type':      row[20],
                'priority':          row[21],

                # Display label for map
                'display_label':  row[0] + (' (MIL)' if row[18] else ''),
                'label_priority': 'military' if row[18] else 'civilian'
            }
            
            metars.append(metar_data)
        
        cur.close()
        conn.close()
        
        # Get latest model run time for reference
        latest_run = None
        if metars:
            latest_run = max(m['observation_time'] for m in metars if m['observation_time'])
            if isinstance(latest_run, str):
                latest_run = datetime.fromisoformat(latest_run.replace('Z', '+00:00'))
        
        return jsonify({
            'metars': metars,
            'model_run': latest_run.isoformat() if latest_run else None,
            'count': len(metars),
            'bounds': {
                'west': west,
                'south': south, 
                'east': east,
                'north': north
            },
            'limit_applied': limit,
            'military_count': len([m for m in metars if m['is_military']]),
            'station_types': {
                'military': len([m for m in metars if m['is_military']]),
                'large_airport': len([m for m in metars if m['airport_type'] == 'large_airport']),
                'medium_airport': len([m for m in metars if m['airport_type'] == 'medium_airport']),
                'small_airport': len([m for m in metars if m['airport_type'] == 'small_airport'])
            }
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@weather_enhanced_api.route('/stations/priorities', methods=['GET'])
def get_station_priorities():
    """Get station priority information for enhanced map display"""
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        # Get comprehensive station priority data
        query = """
            SELECT
                a.station_id,
                a.name,
                a.iso_region,
                ST_Y(a.location::geometry) AS latitude,
                ST_X(a.location::geometry) AS longitude,
                a.is_military,
                a.longest_runway_ft,
                CASE
                    WHEN a.is_military = true        THEN 1
                    WHEN a.is_major_hub = true       THEN 2
                    WHEN a.longest_runway_ft >= 5000 THEN 3
                    WHEN a.longest_runway_ft >= 2500 THEN 4
                    ELSE 5
                END as priority
            FROM observations.airports a
            WHERE a.station_id IS NOT NULL
              AND a.location IS NOT NULL
            ORDER BY priority ASC, a.is_military DESC NULLS LAST
        """
        
        cur.execute(query)
        rows = cur.fetchall()
        
        stations = []
        for row in rows:
            stations.append({
                'station_id':        row[0],
                'name':              row[1],
                'municipality':      row[2],
                'latitude':          float(row[3]) if row[3] is not None else None,
                'longitude':         float(row[4]) if row[4] is not None else None,
                'is_military':       bool(row[5]) if row[5] is not None else False,
                'longest_runway_ft': int(row[6]) if row[6] is not None else None,
                'priority':          row[7],
                'display_label':     row[0] + (' (MIL)' if row[5] else ''),
                'label_class':       'military' if row[5] else 'civilian'
            })
        
        cur.close()
        conn.close()
        
        return jsonify({
            'stations': stations,
            'count': len(stations),
            'priority_counts': {
                'military': len([s for s in stations if s['is_military']]),
                'large_airport': len([s for s in stations if s['airport_type'] == 'large_airport']),
                'medium_airport': len([s for s in stations if s['airport_type'] == 'medium_airport']),
                'small_airport': len([s for s in stations if s['airport_type'] == 'small_airport'])
            }
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Reuse existing TFR/NDA endpoints from original weather_api if they exist
# or create new enhanced versions here

@weather_enhanced_api.route('/nda/active', methods=['GET'])
def get_active_nda():
    """Get active National Defense Airspace areas for enhanced map"""
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT global_id, name, city, state, type_code, local_type,
                   wkhr_code, wkhr_rmk, ST_AsGeoJSON(geometry) as geometry
            FROM observations.national_defense_airspace 
            WHERE active = TRUE 
            ORDER BY state, name
        """)
        
        nda_areas = []
        for row in cur.fetchall():
            nda_areas.append({
                'id': row[0],
                'name': row[1], 
                'city': row[2],
                'state': row[3],
                'type_code': row[4],
                'local_type': row[5],
                'work_hours_code': row[6],
                'work_hours_remark': row[7],
                'geometry': json.loads(row[8]) if row[8] else None
            })
        
        cur.close()
        conn.close()
        
        return jsonify({
            'nda_areas': nda_areas,
            'count': len(nda_areas),
            'generated_at': datetime.utcnow().isoformat() + 'Z',
            'source': 'FAA/ESRI National Defense Airspace',
            'note': 'For situational awareness only - not for official flight planning'
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@weather_enhanced_api.route('/stadium-tfrs', methods=['GET']) 
def get_stadium_tfrs():
    """Get Stadium TFR locations for enhanced mission planning"""
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT global_id, name, city, state, status_code,
                   latitude, longitude, ST_AsGeoJSON(geometry) as geometry,
                   ST_AsGeoJSON(buffer_3nm) as buffer_3nm_geom
            FROM observations.stadium_tfrs 
            WHERE active = TRUE 
            ORDER BY state, name
        """)
        
        stadiums = []
        for row in cur.fetchall():
            stadiums.append({
                'id': row[0],
                'name': row[1], 
                'city': row[2],
                'state': row[3],
                'status': row[4],
                'latitude': float(row[5]) if row[5] else None,
                'longitude': float(row[6]) if row[6] else None,
                'geometry': json.loads(row[7]) if row[7] else None,
                'tfr_area': json.loads(row[8]) if row[8] else None  # 3NM buffer for display
            })
        
        cur.close()
        conn.close()
        
        return jsonify({
            'stadium_tfrs': stadiums,
            'count': len(stadiums),
            'generated_at': datetime.utcnow().isoformat() + 'Z',
            'note': 'Stadium locations where TFRs may be activated during events - for situational awareness only'
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# =============================================================================
# GLM Lightning Flash API
# =============================================================================

@weather_enhanced_api.route('/glm/flashes', methods=['GET'])
def get_glm_flashes():
    """
    Return GLM lightning flashes as GeoJSON FeatureCollection.

    Query params:
      minutes  : lookback window in minutes (default 20, max 60)
      min_lat, max_lat, min_lon, max_lon : optional bbox filter
    """
    try:
        minutes = min(int(request.args.get('minutes', 20)), 60)
        min_lat = request.args.get('min_lat', type=float)
        max_lat = request.args.get('max_lat', type=float)
        min_lon = request.args.get('min_lon', type=float)
        max_lon = request.args.get('max_lon', type=float)

        cutoff = datetime.utcnow() - timedelta(minutes=minutes)

        conn = get_connection()
        cur  = conn.cursor()

        # Base query — bbox filter optional
        if all(v is not None for v in [min_lat, max_lat, min_lon, max_lon]):
            cur.execute("""
                SELECT flash_time, lat, lon, flash_energy, satellite
                FROM observations.glm_flashes
                WHERE flash_time >= %s
                  AND lat  BETWEEN %s AND %s
                  AND lon  BETWEEN %s AND %s
                  AND flash_quality_flag = 0
                  AND flash_energy >= 1e-14
                ORDER BY flash_time DESC
                LIMIT 50000
            """, (cutoff, min_lat, max_lat, min_lon, max_lon))
        else:
            cur.execute("""
                SELECT flash_time, lat, lon, flash_energy, satellite
                FROM observations.glm_flashes
                WHERE flash_time >= %s
                  AND flash_quality_flag = 0
                  AND flash_energy >= 1e-14
                ORDER BY flash_time DESC
                LIMIT 50000
            """, (cutoff,))

        rows = cur.fetchall()
        cur.close()
        conn.close()

        now = datetime.utcnow()

        features = []
        for flash_time, lat, lon, flash_energy, satellite in rows:
            # Age in minutes (flash_time may be tz-aware)
            ft = flash_time.replace(tzinfo=None) if flash_time.tzinfo else flash_time
            age_min = (now - ft).total_seconds() / 60.0

            # Age bucket: 0=red (<5 min), 1=yellow (5-15 min), 2=green (>15 min)
            if age_min < 5:
                age_bucket = 0
            elif age_min < 15:
                age_bucket = 1
            else:
                age_bucket = 2

            features.append({
                'type': 'Feature',
                'geometry': {
                    'type': 'Point',
                    'coordinates': [lon, lat]
                },
                'properties': {
                    'flash_time': ft.strftime('%Y-%m-%dT%H:%M:%SZ'),
                    'age_min':    round(age_min, 1),
                    'age_bucket': age_bucket,   # 0=red,1=yellow,2=green
                    'energy':     float(flash_energy) if flash_energy else None,
                    'satellite':  satellite.strip() if satellite else None,
                }
            })

        return jsonify({
            'type':        'FeatureCollection',
            'features':    features,
            'count':       len(features),
            'minutes':     minutes,
            'generated_at': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── WWA (Watch/Warning/Advisory) active polygons ──────────────────────────────
# NWS standard colors by phenomena.significance
WWA_COLORS = {
    ('TO', 'W'): '#FF0000',  # Tornado Warning — Red
    ('SV', 'W'): '#FFA500',  # Severe Thunderstorm Warning — Orange
    ('FF', 'W'): '#8B0000',  # Flash Flood Warning — Dark Red
    ('FL', 'W'): '#00FF00',  # Flood Warning — Lime
    ('EW', 'W'): '#FF8C00',  # Extreme Wind Warning — Dark Orange
    ('HW', 'W'): '#DAA520',  # High Wind Warning — Goldenrod
    ('BZ', 'W'): '#FF4500',  # Blizzard Warning — Orange Red
    ('WS', 'W'): '#FF69B4',  # Winter Storm Warning — Hot Pink
    ('IS', 'W'): '#8B008B',  # Ice Storm Warning — Dark Magenta
    ('DS', 'W'): '#FFE4C4',  # Dust Storm Warning — Bisque
    ('WC', 'W'): '#B0C4DE',  # Wind Chill Warning — Light Steel Blue
    ('EC', 'W'): '#B0C4DE',  # Extreme Cold Warning — Light Steel Blue
    ('HT', 'W'): '#C71585',  # Excessive Heat Warning — Medium Violet Red
    ('TO', 'A'): '#FFFF00',  # Tornado Watch — Yellow
    ('SV', 'A'): '#DB7093',  # Severe Thunderstorm Watch — Pale Violet Red
    ('FF', 'A'): '#32CD32',  # Flash Flood Watch — Lime Green
    ('FL', 'A'): '#2E8B57',  # Flood Watch — Sea Green
    ('HW', 'A'): '#B8860B',  # High Wind Watch — Dark Goldenrod
    ('WS', 'A'): '#4682B4',  # Winter Storm Watch — Steel Blue
    ('HT', 'A'): '#FF7F50',  # Excessive Heat Watch — Coral
    ('EC', 'A'): '#5F9EA0',  # Extreme Cold Watch — Cadet Blue
}

WWA_LABELS = {
    ('TO', 'W'): 'Tornado Warning',
    ('SV', 'W'): 'Severe Thunderstorm Warning',
    ('FF', 'W'): 'Flash Flood Warning',
    ('FL', 'W'): 'Flood Warning',
    ('EW', 'W'): 'Extreme Wind Warning',
    ('HW', 'W'): 'High Wind Warning',
    ('BZ', 'W'): 'Blizzard Warning',
    ('WS', 'W'): 'Winter Storm Warning',
    ('IS', 'W'): 'Ice Storm Warning',
    ('DS', 'W'): 'Dust Storm Warning',
    ('WC', 'W'): 'Wind Chill Warning',
    ('EC', 'W'): 'Extreme Cold Warning',
    ('HT', 'W'): 'Excessive Heat Warning',
    ('TO', 'A'): 'Tornado Watch',
    ('SV', 'A'): 'Severe Thunderstorm Watch',
    ('FF', 'A'): 'Flash Flood Watch',
    ('FL', 'A'): 'Flood Watch',
    ('HW', 'A'): 'High Wind Watch',
    ('WS', 'A'): 'Winter Storm Watch',
    ('HT', 'A'): 'Excessive Heat Watch',
    ('EC', 'A'): 'Extreme Cold Watch',
}


@weather_enhanced_api.route('/wwa/active', methods=['GET'])
def get_active_wwa():
    """
    Return active WWA polygons as GeoJSON FeatureCollection.
    Only returns records with non-null geometry (storm-based warnings).
    County-based watches (no polygon) are excluded — use UGC lookup separately.
    Query params:
      bounds: west,south,east,north (optional — if omitted returns all active)
    """
    try:
        bounds_param = request.args.get('bounds', '')
        conn = get_connection()
        cur  = conn.cursor()

        if bounds_param:
            try:
                west, south, east, north = map(float, bounds_param.split(','))
                south = max(-90.0, min(90.0, south))
                north = max(-90.0, min(90.0, north))
                while west < -180.0: west += 360.0
                while east < -180.0: east += 360.0
                while east >  180.0: east -= 360.0
                if west > east:
                    geom = (f'ST_Union('
                            f'ST_MakeEnvelope(-180,{south},{east},{north},4326),'
                            f'ST_MakeEnvelope({west},{south},180,{north},4326))')
                else:
                    geom = f'ST_MakeEnvelope({west},{south},{east},{north},4326)'
                where_extra = f'AND ST_Intersects(w.geom, {geom})'
            except (ValueError, TypeError):
                where_extra = ''
        else:
            where_extra = ''

        cur.execute(f"""
            SELECT
                w.id,
                w.wfo,
                w.phenomena,
                w.significance,
                w.event_number,
                w.begin_time,
                w.end_time,
                w.headline,
                w.ugc_zones,
                ST_AsGeoJSON(w.geom) AS geom_json,
                w.raw_segment
            FROM observations.wwa w
            WHERE w.is_active = TRUE
              AND w.geom IS NOT NULL
              AND (w.end_time IS NULL OR w.end_time > NOW())
              {where_extra}
            ORDER BY
                -- Warnings before watches, more severe first
                CASE w.significance WHEN 'W' THEN 0 WHEN 'A' THEN 1 ELSE 2 END,
                CASE w.phenomena
                    WHEN 'TO' THEN 0 WHEN 'SV' THEN 1 WHEN 'EW' THEN 2
                    WHEN 'FF' THEN 3 WHEN 'FL' THEN 4 ELSE 5
                END
        """)

        rows = cur.fetchall()
        cur.close()
        conn.close()

        now = datetime.utcnow()
        features = []
        for row in rows:
            (wid, wfo, ph, sig, etn, begin_time, end_time,
             headline, ugc_zones, geom_json, raw_seg) = row

            if not geom_json:
                continue

            key   = (ph.strip(), sig.strip())
            color = WWA_COLORS.get(key, '#AAAAAA')
            label = WWA_LABELS.get(key, f'{ph.strip()}.{sig.strip()}')

            # Extract storm motion from raw_segment if present
            storm_motion = None
            if raw_seg and 'STORM_MOTION:' in raw_seg:
                import re
                m = re.search(
                    r'STORM_MOTION:\s*(\d+)DEG\s+(\d+)KT(?:\s+LOC\s+([\d.-]+),([\d.-]+))?',
                    raw_seg)
                if m:
                    storm_motion = {
                        'deg':     int(m.group(1)),
                        'kts':     int(m.group(2)),
                        'loc_lat': float(m.group(3)) if m.group(3) else None,
                        'loc_lon': float(m.group(4)) if m.group(4) else None,
                    }

            features.append({
                'type': 'Feature',
                'geometry': json.loads(geom_json),
                'properties': {
                    'id':           wid,
                    'wfo':          wfo.strip(),
                    'phenomena':    ph.strip(),
                    'significance': sig.strip(),
                    'etn':          etn,
                    'label':        label,
                    'color':        color,
                    'begin_time':   begin_time.strftime('%Y-%m-%dT%H:%M:%SZ') if begin_time else None,
                    'end_time':     end_time.strftime('%Y-%m-%dT%H:%M:%SZ') if end_time else None,
                    'headline':     headline,
                    'ugc_zones':    ugc_zones,
                    'storm_motion': storm_motion,
                }
            })

        return jsonify({
            'type':         'FeatureCollection',
            'features':     features,
            'count':        len(features),
            'generated_at': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

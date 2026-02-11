"""
Weather Display Pages
Station detail view and interactive map
"""
from flask import Blueprint, render_template, request, jsonify
import sys
sys.path.insert(0, '/var/www/cap_winds_app')
from db_config import get_connection
from datetime import datetime, timedelta
from taf_decoder import decode_taf, format_taf_for_display
from runway_analysis import analyze_runways_for_wind, format_runway_analysis_html

weather_pages = Blueprint('weather_pages', __name__, url_prefix='/weather')

@weather_pages.route('/')
def index():
    """Weather home page - redirect to station search"""
    return render_template('weather_index.html')

@weather_pages.route('/station')
def station_view():
    """Station detail view page"""
    return render_template('weather_station.html')

@weather_pages.route('/map')
def map_view():
    """Interactive weather map page"""
    return render_template('weather_map.html')

@weather_pages.route('/api/station/<station_id>')
def get_station_data(station_id):
    """
    Get detailed station data with METAR/SPECI context
    
    Query parameters:
    - radius: Include stations within radius (nm), default: 0 (single station only)
    - hours: Hours of history, default: 24
    """
    try:
        station_id = station_id.upper()
        radius_nm = float(request.args.get('radius', 0))
        hours = int(request.args.get('hours', 24))
        
        conn = get_connection()
        cur = conn.cursor()
        
        # Get the center station coordinates
        cur.execute("""
            SELECT DISTINCT ON (station_id)
                station_id,
                ST_X(location) as longitude,
                ST_Y(location) as latitude
            FROM observations.metar
            WHERE station_id = %s
            ORDER BY station_id, observation_time DESC
            LIMIT 1
        """, (station_id,))
        
        center_station = cur.fetchone()
        if not center_station:
            return jsonify({'error': 'Station not found'}), 404
        
        center_lon = center_station[1]
        center_lat = center_station[2]
        
        # Build station list
        if radius_nm > 0:
            # Get stations within radius
            radius_meters = radius_nm * 1852
            cur.execute("""
                SELECT DISTINCT station_id,
                       ST_X(location) as longitude,
                       ST_Y(location) as latitude,
                       ST_Distance(
                           location::geography,
                           ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography
                       ) / 1852 as distance_nm
                FROM observations.metar
                WHERE ST_DWithin(
                    location::geography,
                    ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                    %s
                )
                AND observation_time > NOW() - INTERVAL '%s hours'
                ORDER BY distance_nm
            """, (center_lon, center_lat, center_lon, center_lat, radius_meters, hours))
            
            stations = cur.fetchall()
        else:
            # Single station only
            stations = [(station_id, center_lon, center_lat, 0.0)]
        
        # Get observations for each station
        results = []
        for stn_id, stn_lon, stn_lat, distance in stations:
            # Get all observations (METAR and SPECI) for this station
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
                    is_speci
                FROM observations.metar
                WHERE station_id = %s
                  AND observation_time > NOW() - INTERVAL '%s hours'
                ORDER BY observation_time DESC
            """, (stn_id, hours))
            
            all_obs = cur.fetchall()
            
            # Separate METARs and SPECIs
            metars = [obs for obs in all_obs if not obs[13]]  # is_speci = False
            specis = [obs for obs in all_obs if obs[13]]      # is_speci = True
            
            # Take last 6 METARs
            recent_metars = metars[:6]
            
            # Format observations with context
            observations = []
            
            # Add regular METARs
            for obs in recent_metars:
                observations.append({
                    'type': 'METAR',
                    'observation_time': obs[1].isoformat() if obs[1] else None,
                    'raw_text': obs[2],
                    'temp_c': obs[3],
                    'dewpoint_c': obs[4],
                    'wind_dir': obs[5],
                    'wind_speed_kts': obs[6],
                    'wind_gust_kts': obs[7],
                    'visibility_sm': obs[8],
                    'altimeter_hg': obs[9],
                    'flight_category': obs[10],
                    'sky_conditions': obs[11],
                    'present_weather': obs[12],
                    'context': None  # Regular METAR has no special context
                })
            
            # Add SPECIs with context (preceding METAR + intermediate SPECIs)
            for speci in specis:
                speci_time = speci[1]
                
                # Find preceding METAR
                preceding_metar = None
                for m in metars:
                    if m[1] < speci_time:
                        preceding_metar = m
                        break
                
                # Find intermediate SPECIs between preceding METAR and this SPECI
                intermediate_specis = []
                if preceding_metar:
                    for s in specis:
                        if preceding_metar[1] < s[1] < speci_time:
                            intermediate_specis.append(s)
                
                # Build context
                context = []
                if preceding_metar:
                    context.append({
                        'type': 'METAR',
                        'time': preceding_metar[1].isoformat(),
                        'raw_text': preceding_metar[2],
                        'flight_category': preceding_metar[10]
                    })
                
                for int_speci in sorted(intermediate_specis, key=lambda x: x[1]):
                    context.append({
                        'type': 'SPECI',
                        'time': int_speci[1].isoformat(),
                        'raw_text': int_speci[2],
                        'flight_category': int_speci[10]
                    })
                
                observations.append({
                    'type': 'SPECI',
                    'observation_time': speci[1].isoformat() if speci[1] else None,
                    'raw_text': speci[2],
                    'temp_c': speci[3],
                    'dewpoint_c': speci[4],
                    'wind_dir': speci[5],
                    'wind_speed_kts': speci[6],
                    'wind_gust_kts': speci[7],
                    'visibility_sm': speci[8],
                    'altimeter_hg': speci[9],
                    'flight_category': speci[10],
                    'sky_conditions': speci[11],
                    'present_weather': speci[12],
                    'context': context if context else None
                })
            
            # Sort all observations by time (most recent first)
            observations.sort(key=lambda x: x['observation_time'], reverse=True)
            
            # Get TAF if available
            cur.execute("""
                SELECT raw_text, issue_time, valid_from, valid_to
                FROM observations.taf
                WHERE station_id = %s
                  AND valid_to > NOW()
                ORDER BY issue_time DESC
                LIMIT 1
            """, (stn_id,))
            
            taf_row = cur.fetchone()
            taf = None
            if taf_row:
                # Decode the TAF
                decoded = decode_taf(taf_row[0])
                decoded_html = format_taf_for_display(decoded) if decoded else None
                
                taf = {
                    'raw_text': taf_row[0],
                    'issue_time': taf_row[1].isoformat() if taf_row[1] else None,
                    'valid_from': taf_row[2].isoformat() if taf_row[2] else None,
                    'valid_to': taf_row[3].isoformat() if taf_row[3] else None,
                    'decoded': decoded,
                    'decoded_html': decoded_html
                }
            
            # Get runway analysis for most recent observation
            runway_analysis = None
            runway_analysis_html = None
            if observations:
                # Find the most recent METAR (not SPECI)
                latest_metar = next((obs for obs in observations if obs['type'] == 'METAR'), observations[0] if observations else None)
                if latest_metar:
                    analysis = analyze_runways_for_wind(
                        stn_id,
                        latest_metar.get('wind_dir'),
                        latest_metar.get('wind_speed_kts'),
                        latest_metar.get('wind_gust_kts')
                    )
                    runway_analysis = analysis
                    runway_analysis_html = format_runway_analysis_html(analysis)
            
            results.append({
                'station_id': stn_id,
                'longitude': stn_lon,
                'latitude': stn_lat,
                'distance_nm': round(distance, 1) if distance > 0 else 0,
                'observations': observations,
                'taf': taf,
                'runway_analysis': runway_analysis,
                'runway_analysis_html': runway_analysis_html
            })
        
        cur.close()
        conn.close()
        
        return jsonify({
            'center_station': station_id,
            'center_coords': [center_lat, center_lon],
            'radius_nm': radius_nm,
            'station_count': len(results),
            'stations': results,
            'timestamp': datetime.utcnow().isoformat()
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


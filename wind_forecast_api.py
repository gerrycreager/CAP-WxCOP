#!/var/www/cap_winds_app/venv/bin/python3
"""
Wind Forecast API with Label Priority
Adds priority-based labeling for maps
"""

import sys
sys.path.insert(0, '/var/www/cap_winds_app')

from flask import Blueprint, jsonify, request
from datetime import datetime, timedelta
from db_config import get_connection
import logging
import re

wind_forecast_api = Blueprint('wind_forecast_api', __name__)
log = logging.getLogger(__name__)

# Civil Air Patrol Region bounding boxes
REGION_BOUNDS = {
    'CONUS':     {'west': -125, 'south': 24,  'east': -66,  'north': 50},
    'NCR':       {'west': -104, 'south': 37,  'east': -89,  'north': 49},
    'GLR':       {'west': -93,  'south': 37,  'east': -80,  'north': 49},
    'MAR':       {'west': -84,  'south': 32,  'east': -75,  'north': 40},
    'NER':       {'west': -80,  'south': 39,  'east': -66,  'north': 48},
    'SER':       {'west': -92,  'south': 24,  'east': -79,  'north': 37},
    'SWR':       {'west': -115, 'south': 25,  'east': -89,  'north': 37},
    'RMR':       {'west': -117, 'south': 37,  'east': -102, 'north': 49},
    'PCR':       {'west': -125, 'south': 32,  'east': -114, 'north': 49},
    'AK':        {'west': -180, 'south': 51,  'east': -130, 'north': 72},
    'HI':        {'west': -161, 'south': 18,  'east': -154, 'north': 23},
    'CARIBBEAN': {'west': -80,  'south': 17,  'east': -64,  'north': 27},
}


def get_label_priority(name, runway_ft, station_id):
    """
    Determine label priority for map display
    Returns: 1 (military), 2 (major), 3 (medium), 4 (small/no label)
    """
    if not name:
        return 4
    
    name_upper = name.upper()
    
    # Priority 1: Military installations
    military_keywords = [
        'AIR FORCE BASE', 'AFB', 'NAVAL', 'NAVY', 'MARINE', 'MCAS',
        'AIR STATION', 'NAS', 'AIR GUARD', 'ANG', 'MILITARY',
        'ARMY', 'COAST GUARD', 'SPACE FORCE'
    ]
    if any(keyword in name_upper for keyword in military_keywords):
        return 1
    
    # Priority 2: Major airports (long runways >= 10,000 ft)
    if runway_ft and runway_ft >= 10000:
        return 2
    
    # Priority 2: International airports (by name)
    if 'INTERNATIONAL' in name_upper:
        return 2
    
    # Priority 3: Medium airports (6,000 - 10,000 ft runways)
    if runway_ft and runway_ft >= 6000:
        return 3
    
    # Priority 4: Small airports (don't label)
    return 4


def get_wind_forecasts_in_bounds(west, south, east, north, limit=5000):
    """
    Query model_wind_forecasts with JOIN to airports table
    Includes label priority for smart map labeling
    """
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        # Get latest model run
        cur.execute("""
            SELECT DISTINCT model_run 
            FROM observations.model_wind_forecasts 
            ORDER BY model_run DESC 
            LIMIT 1
        """)
        
        result = cur.fetchone()
        if not result:
            cur.close()
            conn.close()
            log.warning("No model runs found in database")
            return []
        
        latest_run = result[0]
        log.info(f"Using model run: {latest_run}")
        
        # Query WITH JOIN to airports table
        query = """
        SELECT 
            mwf.station_id,
            ST_X(mwf.location::geometry) as lon,
            ST_Y(mwf.location::geometry) as lat,
            MAX(mwf.wind_speed_kts) as max_wind_kts,
            MAX(mwf.wind_gust_kts) as max_gust_kts,
            MIN(mwf.wind_speed_kts) as min_wind_kts,
            mwf.model_name,
            mwf.wind_category,
            a.name as airport_name,
            a.longest_runway_ft,
            a.is_military
        FROM observations.model_wind_forecasts mwf
        INNER JOIN observations.airports a ON mwf.station_id = a.station_id
        WHERE mwf.model_run = %s
            AND ST_X(mwf.location::geometry) BETWEEN %s AND %s
            AND ST_Y(mwf.location::geometry) BETWEEN %s AND %s
            AND mwf.forecast_hour <= 12
        GROUP BY mwf.station_id, mwf.location, mwf.model_name, mwf.wind_category, a.name, a.longest_runway_ft, a.is_military
        ORDER BY a.is_military DESC, MAX(mwf.wind_speed_kts) DESC
        LIMIT %s
        """
        
        cur.execute(query, (latest_run, west, east, south, north, limit))
        
        airports = []
        for row in cur.fetchall():
            station_id = row[0]
            name = row[8] or station_id
            runway_ft = int(row[9]) if row[9] else None
            is_military = bool(row[10]) if row[10] is not None else False

            # get_label_priority uses name/runway keywords; override to 1 if DB flags military
            label_priority = get_label_priority(name, runway_ft, station_id)
            if is_military:
                label_priority = 1

            airports.append({
                'station_id': station_id,
                'lon': float(row[1]) if row[1] else 0,
                'lat': float(row[2]) if row[2] else 0,
                'max_wind_kts': int(row[3]) if row[3] else 0,
                'max_gust_kts': int(row[4]) if row[4] else None,
                'min_wind_kts': int(row[5]) if row[5] else 0,
                'max_wind_time': None,
                'max_gust_time': None,
                'model': row[6] or 'HRRR',
                'category': row[7] or 'NORMAL',
                'name': name,
                'longest_runway_ft': runway_ft,
                'is_military': is_military,
                'label_priority': label_priority,
                'type': 'airport'
            })
        
        cur.close()
        conn.close()
        
        log.info(f"Query returned {len(airports)} airports in bounds [{west},{south},{east},{north}]")
        return airports
        
    except Exception as e:
        log.error(f"Error querying wind forecasts: {e}", exc_info=True)
        try:
            cur.close()
            conn.close()
        except:
            pass
        return []


def generate_wind_grid(airports, bounds, grid_resolution=50):
    """
    Generate interpolated wind speed grid from airport point data
    
    Args:
        airports: List of airport dicts with lat, lon, max_wind_kts
        bounds: Dict with west, east, south, north
        grid_resolution: Number of grid points in each direction
    
    Returns:
        Dict with grid data for contour rendering
    """
    from scipy.interpolate import griddata
    import numpy as np
    
    if len(airports) < 3:
        return None
    
    lons = np.array([a['lon'] for a in airports])
    lats = np.array([a['lat'] for a in airports])
    winds = np.array([a['max_wind_kts'] for a in airports])
    
    lon_range = np.linspace(bounds['west'], bounds['east'], grid_resolution)
    lat_range = np.linspace(bounds['south'], bounds['north'], grid_resolution)
    grid_lon, grid_lat = np.meshgrid(lon_range, lat_range)
    
    try:
        grid_wind = griddata((lons, lats), winds, (grid_lon, grid_lat), method='cubic', fill_value=np.nan)
    except:
        grid_wind = griddata((lons, lats), winds, (grid_lon, grid_lat), method='linear', fill_value=np.nan)
    
    mask = np.isnan(grid_wind)
    if mask.any():
        grid_wind_nearest = griddata((lons, lats), winds, (grid_lon, grid_lat), method='nearest')
        grid_wind[mask] = grid_wind_nearest[mask]
    
    wind_levels = [5, 10, 15, 20, 25, 30, 35, 40]
    
    return {
        'lons': lon_range.tolist(),
        'lats': lat_range.tolist(),
        'winds': grid_wind.tolist(),
        'levels': wind_levels,
        'min_wind': float(np.nanmin(grid_wind)),
        'max_wind': float(np.nanmax(grid_wind))
    }

@wind_forecast_api.route('/region/<region_code>')
def get_region_forecasts(region_code):
    """Get wind forecasts for a CAP region"""
    region_code = region_code.upper()
    
    if region_code not in REGION_BOUNDS:
        return jsonify({'error': 'Invalid region code'}), 400
    
    bounds = REGION_BOUNDS[region_code]
    airports = get_wind_forecasts_in_bounds(
        bounds['west'], bounds['south'], 
        bounds['east'], bounds['north']
    )
    
    # Generate wind grid for contours
    wind_grid = None
    if len(airports) >= 3:
        try:
            wind_grid = generate_wind_grid(airports, bounds, grid_resolution=40)
        except Exception as e:
            log.warning(f"Failed to generate wind grid: {e}")
    
    response = {
        'region': region_code,
        'bounds': bounds,
        'count': len(airports),
        'airports': airports,
        'timestamp': datetime.utcnow().isoformat()
    }
    
    if wind_grid:
        response['wind_grid'] = wind_grid
    
    return jsonify(response)
@wind_forecast_api.route('/state/<state_code>')
def get_state_forecasts(state_code):
    """Get wind forecasts for a US state"""
    state_code = state_code.upper()
    
    # State bounding boxes (simplified - you may want more precise bounds)
    state_bounds = {
            'AL': {'west': -88.5, 'south': 30.1, 'east': -84.8, 'north': 35.1},
        'AK': {'west': -180, 'south': 51, 'east': -129, 'north': 72},
        'AZ': {'west': -114.8, 'south': 31.3, 'east': -109.0, 'north': 37.1},
        'AR': {'west': -94.6, 'south': 33.0, 'east': -89.6, 'north': 36.5},
        'CA': {'west': -124.5, 'south': 32.5, 'east': -114.1, 'north': 42.1},
        'CO': {'west': -109.1, 'south': 36.9, 'east': -102.0, 'north': 41.1},
        'CT': {'west': -73.8, 'south': 40.9, 'east': -71.8, 'north': 42.1},
        'DE': {'west': -75.8, 'south': 38.4, 'east': -75.0, 'north': 39.9},
        'FL': {'west': -87.7, 'south': 24.4, 'east': -80.0, 'north': 31.1},
        'GA': {'west': -85.6, 'south': 30.3, 'east': -80.8, 'north': 35.1},
        'HI': {'west': -160.3, 'south': 18.9, 'east': -154.7, 'north': 22.3},
        'ID': {'west': -117.3, 'south': 41.9, 'east': -111.0, 'north': 49.1},
        'IL': {'west': -91.5, 'south': 36.9, 'east': -87.5, 'north': 42.6},
        'IN': {'west': -88.1, 'south': 37.7, 'east': -84.8, 'north': 41.8},
        'IA': {'west': -96.7, 'south': 40.3, 'east': -90.1, 'north': 43.6},
        'KS': {'west': -102.1, 'south': 36.9, 'east': -94.6, 'north': 40.1},
        'KY': {'west': -89.6, 'south': 36.5, 'east': -81.9, 'north': 39.2},
        'LA': {'west': -94.1, 'south': 28.9, 'east': -88.8, 'north': 33.1},
        'ME': {'west': -71.1, 'south': 43.0, 'east': -66.9, 'north': 47.5},
        'MD': {'west': -79.5, 'south': 37.9, 'east': -75.0, 'north': 39.8},
        'MA': {'west': -73.5, 'south': 41.2, 'east': -69.9, 'north': 42.9},
        'MI': {'west': -90.5, 'south': 41.6, 'east': -82.1, 'north': 48.3},
        'MN': {'west': -97.3, 'south': 43.5, 'east': -89.5, 'north': 49.4},
        'MS': {'west': -91.7, 'south': 30.1, 'east': -88.1, 'north': 35.1},
        'MO': {'west': -95.8, 'south': 35.9, 'east': -89.1, 'north': 40.7},
        'MT': {'west': -116.1, 'south': 44.3, 'east': -104.0, 'north': 49.1},
        'NE': {'west': -104.1, 'south': 39.9, 'east': -95.3, 'north': 43.1},
        'NV': {'west': -120.1, 'south': 35.0, 'east': -114.0, 'north': 42.1},
        'NH': {'west': -72.6, 'south': 42.7, 'east': -70.6, 'north': 45.4},
        'NJ': {'west': -75.6, 'south': 38.9, 'east': -73.9, 'north': 41.4},
        'NM': {'west': -109.1, 'south': 31.3, 'east': -103.0, 'north': 37.1},
        'NY': {'west': -79.8, 'south': 40.5, 'east': -71.8, 'north': 45.1},
        'NC': {'west': -84.4, 'south': 33.8, 'east': -75.4, 'north': 36.6},
        'ND': {'west': -104.1, 'south': 45.9, 'east': -96.5, 'north': 49.1},
        'OH': {'west': -84.9, 'south': 38.4, 'east': -80.5, 'north': 42.0},
        'OK': {'west': -103.1, 'south': 33.6, 'east': -94.4, 'north': 37.1},
        'OR': {'west': -124.7, 'south': 41.9, 'east': -116.5, 'north': 46.3},
        'PA': {'west': -80.6, 'south': 39.7, 'east': -74.7, 'north': 42.3},
        'RI': {'west': -71.9, 'south': 41.1, 'east': -71.1, 'north': 42.1},
        'SC': {'west': -83.4, 'south': 32.0, 'east': -78.5, 'north': 35.3},
        'SD': {'west': -104.1, 'south': 42.5, 'east': -96.4, 'north': 45.9},
        'TN': {'west': -90.4, 'south': 34.9, 'east': -81.6, 'north': 36.7},
        'TX': {'west': -106.7, 'south': 25.8, 'east': -93.5, 'north': 36.6},
        'UT': {'west': -114.1, 'south': 37.0, 'east': -109.0, 'north': 42.1},
        'VT': {'west': -73.5, 'south': 42.7, 'east': -71.5, 'north': 45.1},
        'VA': {'west': -83.8, 'south': 36.5, 'east': -75.2, 'north': 39.5},
        'WA': {'west': -124.9, 'south': 45.5, 'east': -116.9, 'north': 49.1},
        'WV': {'west': -82.7, 'south': 37.2, 'east': -77.7, 'north': 40.7},
        'WI': {'west': -92.9, 'south': 42.5, 'east': -86.2, 'north': 47.3},
        'WY': {'west': -111.1, 'south': 41.0, 'east': -104.0, 'north': 45.1},
        # Territories
        'PR': {'west': -67.3, 'south': 17.9, 'east': -65.2, 'north': 18.6},
        'VI': {'west': -65.1, 'south': 17.6, 'east': -64.5, 'north': 18.5},
        'GU': {'west': 144.6, 'south': 13.2, 'east': 145.0, 'north': 13.7},
    }
    
    if state_code not in state_bounds:
        return jsonify({'error': 'State not supported or bounds not defined'}), 400
    
    bounds = state_bounds[state_code]
    airports = get_wind_forecasts_in_bounds(
        bounds['west'], bounds['south'], 
        bounds['east'], bounds['north']
    )
    
    # Generate wind grid for contours
    wind_grid = None
    if len(airports) >= 3:
        try:
            wind_grid = generate_wind_grid(airports, bounds, grid_resolution=40)
        except Exception as e:
            log.warning(f"Failed to generate wind grid: {e}")
    
    response = {
        'state': state_code,
        'bounds': bounds,
        'count': len(airports),
        'airports': airports,
        'timestamp': datetime.utcnow().isoformat()
    }
    
    if wind_grid:
        response['wind_grid'] = wind_grid
    
    return jsonify(response)

@wind_forecast_api.route('/conus')
def get_conus_forecasts():
    """Get wind forecasts for entire CONUS"""
    bounds = REGION_BOUNDS['CONUS']
    airports = get_wind_forecasts_in_bounds(
        bounds['west'], bounds['south'], 
        bounds['east'], bounds['north']
    )
    
    # Generate wind grid for contours
    wind_grid = None
    if len(airports) >= 3:
        try:
            wind_grid = generate_wind_grid(airports, bounds, grid_resolution=50)
        except Exception as e:
            log.warning(f"Failed to generate wind grid: {e}")
    
    response = {
        'region': 'CONUS',
        'bounds': bounds,
        'count': len(airports),
        'airports': airports,
        'timestamp': datetime.utcnow().isoformat()
    }
    
    if wind_grid:
        response['wind_grid'] = wind_grid
    
    return jsonify(response)
    """Get wind forecasts for custom bounding box"""
    try:
        west = float(request.args.get('west'))
        south = float(request.args.get('south'))
        east = float(request.args.get('east'))
        north = float(request.args.get('north'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid bounds parameters'}), 400
    
    airports = get_wind_forecasts_in_bounds(west, south, east, north)
    
    return jsonify({
        'bounds': {'west': west, 'south': south, 'east': east, 'north': north},
        'count': len(airports),
        'airports': airports,
        'timestamp': datetime.utcnow().isoformat()
    })


@wind_forecast_api.route('/status')
def get_status():
    """Get API status"""
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        # Get latest model run
        cur.execute("""
            SELECT model_run, COUNT(*) as forecast_count
            FROM observations.model_wind_forecasts
            GROUP BY model_run
            ORDER BY model_run DESC
            LIMIT 1
        """)
        
        result = cur.fetchone()
        if result:
            latest_run, forecast_count = result
            status = 'operational'
        else:
            latest_run = None
            forecast_count = 0
            status = 'no_data'
        
        cur.close()
        conn.close()
        
        return jsonify({
            'status': status,
            'latest_model_run': latest_run.isoformat() if latest_run else None,
            'forecast_count': forecast_count,
            'timestamp': datetime.utcnow().isoformat()
        })
        
    except Exception as e:
        return jsonify({
            'status': 'error',
            'error': str(e),
            'timestamp': datetime.utcnow().isoformat()
        }), 500


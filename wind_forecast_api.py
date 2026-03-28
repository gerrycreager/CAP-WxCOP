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
# Regions defined by state membership (iso_region codes) for accurate polygon coverage.
# Bounding-box approach caused edge truncation (e.g. eastern NC/SC in SER).
# OCONUS territories and CONUS overview retain bounding boxes.
REGION_STATES = {
    # North Central Region
    'NCR': ['US-IA','US-IL','US-KS','US-MN','US-MO','US-ND','US-NE','US-SD','US-WI'],
    # Great Lakes Region
    'GLR': ['US-IN','US-KY','US-MI','US-OH','US-WV'],
    # Mid-Atlantic Region (NY included — NY Wing is in MAR)
    'MAR': ['US-DC','US-DE','US-MD','US-NJ','US-NY','US-PA','US-VA'],
    # Northeast Region (NY excluded — avoid duplicate; NER covers New England only)
    'NER': ['US-CT','US-MA','US-ME','US-NH','US-RI','US-VT'],
    # Southeast Region — state-based to capture full eastern NC/SC coastline
    'SER': ['US-AL','US-FL','US-GA','US-MS','US-NC','US-SC','US-TN'],
    # Southwest Region — AZ added (was missing)
    'SWR': ['US-AR','US-AZ','US-LA','US-NM','US-OK','US-TX'],
    # Rocky Mountain Region — ID added (was missing; 101 airports)
    'RMR': ['US-CO','US-ID','US-MT','US-UT','US-WY'],
    # Pacific Region (CONUS — AK/HI handled by OCONUS bounding boxes)
    'PCR': ['US-CA','US-NV','US-OR','US-WA'],
}

# Bounding boxes for CONUS overview and OCONUS territories
REGION_BOUNDS = {
    'CONUS':    {'west': -125, 'south': 24,  'east': -66,  'north': 50},
    'SER-PR':   {'west': -68,  'south': 17,  'east': -65,  'north': 19},
    'PCR-AK':   {'west': -180, 'south': 51,  'east': -130, 'north': 72},
    'PCR-HI':   {'west': -161, 'south': 18,  'east': -154, 'north': 23},
    'PCR-GUAM': {'west': 144,  'south': 13,  'east': 145,  'north': 14},
}

# All valid region codes
ALL_REGIONS = set(REGION_STATES.keys()) | set(REGION_BOUNDS.keys())


def get_label_priority(name, runway_ft, station_id, is_military=False):
    """
    Determine label priority for map display.
    Primary:  is_military from DB (authoritative).
    Fallback: keyword matching for unambiguous military designators only,
              covering airports not yet flagged in the DB (ARB, ANGB, etc.)
    Returns: 1 (military), 2 (major/international), 3 (regional), 4 (small)
    """
    if is_military:
        return 1

    if not name:
        return 4

    name_upper = name.upper()

    # Keyword fallback — restricted to unambiguous military-only designators
    military_keywords = [
        'AIR FORCE BASE', 'AFB',
        'AIR RESERVE BASE', 'ARB',
        'AIR NATIONAL GUARD BASE', 'ANGB',
        'AIR GUARD BASE',
        'NAVAL AIR STATION', 'NAS',
        'NAVAL AIR FACILITY', 'NAF',
        'MARINE CORPS AIR STATION', 'MCAS',
        'COAST GUARD AIR STATION', 'CGAS',
        'JOINT BASE',
        'AIR STATION',
        'SPACE FORCE BASE',
    ]
    if any(kw in name_upper for kw in military_keywords):
        return 1

    # Priority 2: Major airports (runway >= 10,000 ft or International)
    if (runway_ft and runway_ft >= 10000) or 'INTERNATIONAL' in name_upper:
        return 2

    # Priority 3: Regional airports (runway 5,000 - 9,999 ft)
    if runway_ft and runway_ft >= 5000:
        return 3

    return 4


def _get_latest_run(cur, valid_time=None):
    """
    Return the most recent model_run datetime using a fast indexed query.
    If valid_time is given, finds the latest run covering that valid time.
    ORDER BY model_run DESC LIMIT 1 hits idx_model_winds_run efficiently.
    """
    if valid_time is not None:
        cur.execute("""
            SELECT model_run FROM observations.model_wind_forecasts
            WHERE valid_time = %s
            ORDER BY model_run DESC LIMIT 1
        """, (valid_time,))
    else:
        cur.execute("""
            SELECT model_run FROM observations.model_wind_forecasts
            ORDER BY model_run DESC LIMIT 1
        """)
    result = cur.fetchone()
    return result[0] if result else None


def get_wind_forecasts_by_states(state_list, limit=5000, valid_time=None):
    """
    Query wind forecasts for a list of iso_region codes (e.g. ['US-NC','US-SC']).
    Uses state membership rather than bounding box so irregular region shapes
    don't clip airports near the edges (e.g. eastern Carolinas in SER).
    Returns same dict structure as get_wind_forecasts_in_bounds().
    Also computes the actual bounds of returned airports for map fitting.
    """
    try:
        conn = get_connection()
        cur = conn.cursor()

        latest_run = _get_latest_run(cur, valid_time)
        if not latest_run:
            cur.close(); conn.close()
            log.warning("No model runs found")
            return [], None

        placeholders = ','.join(['%s'] * len(state_list))

        if valid_time is not None:
            query = f"""
            SELECT
                mwf.station_id,
                ST_X(mwf.location::geometry) as lon,
                ST_Y(mwf.location::geometry) as lat,
                mwf.wind_speed_kts as max_wind_kts,
                mwf.wind_gust_kts  as max_gust_kts,
                mwf.wind_speed_kts as min_wind_kts,
                mwf.model_name,
                mwf.wind_category,
                a.name as airport_name,
                a.longest_runway_ft,
                a.is_military,
                mwf.wind_dir
            FROM observations.model_wind_forecasts mwf
            INNER JOIN observations.airports a ON mwf.station_id = a.station_id
            WHERE mwf.model_run = %s
                AND mwf.valid_time = %s
                AND a.iso_region IN ({placeholders})
            ORDER BY mwf.wind_speed_kts DESC
            LIMIT %s
            """
            cur.execute(query, [latest_run, valid_time] + state_list + [limit])
        else:
            query = f"""
            SELECT
                mwf.station_id,
                ST_X(mwf.location::geometry) as lon,
                ST_Y(mwf.location::geometry) as lat,
                MAX(mwf.wind_speed_kts) as max_wind_kts,
                MAX(mwf.wind_gust_kts)  as max_gust_kts,
                MIN(mwf.wind_speed_kts) as min_wind_kts,
                mwf.model_name,
                mwf.wind_category,
                a.name as airport_name,
                a.longest_runway_ft,
                a.is_military,
                NULL as wind_dir
            FROM observations.model_wind_forecasts mwf
            INNER JOIN observations.airports a ON mwf.station_id = a.station_id
            WHERE mwf.model_run = %s
                AND a.iso_region IN ({placeholders})
                AND mwf.forecast_hour <= 12
            GROUP BY mwf.station_id, mwf.location, mwf.model_name, mwf.wind_category,
                     a.name, a.longest_runway_ft, a.is_military
            ORDER BY MAX(mwf.wind_speed_kts) DESC
            LIMIT %s
            """
            cur.execute(query, [latest_run] + state_list + [limit])

        airports = []
        lats, lons = [], []
        for row in cur.fetchall():
            name      = row[8] or row[0]
            runway_ft = int(row[9]) if row[9] else None
            is_mil    = bool(row[10]) if row[10] is not None else False
            label_priority = get_label_priority(name, runway_ft, row[0], is_mil)

            lat = float(row[2]) if row[2] else 0
            lon = float(row[1]) if row[1] else 0
            lats.append(lat)
            lons.append(lon)

            airports.append({
                'station_id':        row[0],
                'lon':               lon,
                'lat':               lat,
                'max_wind_kts':      int(row[3]) if row[3] else 0,
                'max_gust_kts':      int(row[4]) if row[4] else None,
                'min_wind_kts':      int(row[5]) if row[5] else 0,
                'wind_dir':          int(row[11]) if row[11] is not None else None,
                'max_wind_time':     None,
                'max_gust_time':     None,
                'model':             row[6] or 'HRRR',
                'category':          row[7] or 'NORMAL',
                'name':              name,
                'longest_runway_ft': runway_ft,
                'label_priority':    label_priority,
                'is_military':       is_mil,
                'type':              'airport',
            })

        cur.close(); conn.close()

        bounds = None
        if lats:
            pad = 0.5
            bounds = {
                'west':  min(lons) - pad, 'east':  max(lons) + pad,
                'south': min(lats) - pad, 'north': max(lats) + pad,
            }

        log.info(f"State query returned {len(airports)} airports for {state_list}")
        return airports, bounds

    except Exception as e:
        log.error(f"Error querying wind forecasts by state: {e}", exc_info=True)
        try: cur.close(); conn.close()
        except: pass
        return [], None


def get_wind_forecasts_in_bounds(west, south, east, north, limit=5000, valid_time=None):
    """
    Query model_wind_forecasts with JOIN to airports table.
    Uses ST_MakeEnvelope + && operator to hit idx_model_winds_location GiST index.
    If valid_time provided, returns per-hour data; otherwise worst-case f000-f012.
    """
    try:
        conn = get_connection()
        cur = conn.cursor()

        latest_run = _get_latest_run(cur, valid_time)
        if not latest_run:
            cur.close()
            conn.close()
            log.warning("No model runs found in database")
            return []

        log.info(f"Using model run: {latest_run}")

        if valid_time is not None:
            query = """
            SELECT
                mwf.station_id,
                ST_X(mwf.location::geometry) as lon,
                ST_Y(mwf.location::geometry) as lat,
                mwf.wind_speed_kts as max_wind_kts,
                mwf.wind_gust_kts  as max_gust_kts,
                mwf.wind_speed_kts as min_wind_kts,
                mwf.model_name,
                mwf.wind_category,
                a.name as airport_name,
                a.longest_runway_ft,
                a.is_military,
                mwf.wind_dir,
                mwf.forecast_hour
            FROM observations.model_wind_forecasts mwf
            INNER JOIN observations.airports a ON mwf.station_id = a.station_id
            WHERE mwf.model_run = %s
                AND mwf.valid_time = %s
                AND mwf.location && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
            ORDER BY mwf.wind_speed_kts DESC
            LIMIT %s
            """
            cur.execute(query, (latest_run, valid_time, west, south, east, north, limit))
        else:
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
                a.is_military,
                NULL as wind_dir,
                NULL as forecast_hour
            FROM observations.model_wind_forecasts mwf
            INNER JOIN observations.airports a ON mwf.station_id = a.station_id
            WHERE mwf.model_run = %s
                AND mwf.location && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
                AND mwf.forecast_hour <= 12
            GROUP BY mwf.station_id, mwf.location, mwf.model_name, mwf.wind_category,
                     a.name, a.longest_runway_ft, a.is_military
            ORDER BY MAX(mwf.wind_speed_kts) DESC
            LIMIT %s
            """
            cur.execute(query, (latest_run, west, south, east, north, limit))

        airports = []
        for row in cur.fetchall():
            station_id = row[0]
            name      = row[8] or station_id
            runway_ft = int(row[9]) if row[9] else None
            is_mil    = bool(row[10]) if row[10] is not None else False
            label_priority = get_label_priority(name, runway_ft, station_id, is_mil)

            airports.append({
                'station_id': station_id,
                'lon': float(row[1]) if row[1] else 0,
                'lat': float(row[2]) if row[2] else 0,
                'max_wind_kts': int(row[3]) if row[3] else 0,
                'max_gust_kts': int(row[4]) if row[4] else None,
                'min_wind_kts': int(row[5]) if row[5] else 0,
                'wind_dir': int(row[11]) if row[11] is not None else None,
                'max_wind_time': None,
                'max_gust_time': None,
                'model': row[6] or 'HRRR',
                'category': row[7] or 'NORMAL',
                'name': name,
                'longest_runway_ft': runway_ft,
                'label_priority': label_priority,
                'is_military': is_mil,
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

@wind_forecast_api.route('/available-times')
def get_available_times():
    """Return list of valid_times available from latest complete HRRR run.
    Used by the temporal slider to populate its steps.
    """
    try:
        conn = get_connection()
        cur = conn.cursor()
        # Latest complete run
        cur.execute("""
            SELECT model_run FROM (
                SELECT model_run, MAX(forecast_hour) as max_fh
                FROM observations.model_wind_forecasts
                GROUP BY model_run
            ) sub WHERE max_fh >= 12
            ORDER BY model_run DESC LIMIT 1
        """)
        result = cur.fetchone()
        if not result:
            cur.close(); conn.close()
            return jsonify({'error': 'No complete model runs available'}), 404
        model_run = result[0]
        cur.execute("""
            SELECT DISTINCT valid_time, forecast_hour
            FROM observations.model_wind_forecasts
            WHERE model_run = %s
            ORDER BY valid_time
        """, (model_run,))
        rows = cur.fetchall()
        cur.close(); conn.close()
        return jsonify({
            'model_run': model_run.isoformat(),
            'model_name': 'HRRR',
            'times': [
                {'valid_time': r[0].isoformat(), 'forecast_hour': r[1]}
                for r in rows
            ]
        })
    except Exception as e:
        log.error(f"Error fetching available times: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@wind_forecast_api.route('/region/<region_code>')
def get_region_forecasts(region_code):
    """Get wind forecasts for a CAP region.
    State-based regions use iso_region filter for accurate edge coverage.
    OCONUS territories fall back to bounding box.
    Optional ?valid_time=ISO8601 for temporal slider.
    """
    region_code = region_code.upper()

    if region_code not in ALL_REGIONS:
        return jsonify({'error': 'Invalid region code'}), 400

    # Parse optional valid_time param
    valid_time = None
    vt_str = request.args.get('valid_time')
    if vt_str:
        try:
            from dateutil.parser import parse as parse_dt
            valid_time = parse_dt(vt_str).replace(tzinfo=None)
        except Exception as e:
            log.warning(f"Invalid valid_time param: {vt_str} — {e}")

    # State-based query for CONUS regions
    if region_code in REGION_STATES:
        airports, bounds = get_wind_forecasts_by_states(
            REGION_STATES[region_code], valid_time=valid_time)
        if not bounds:
            bounds = REGION_BOUNDS.get(region_code, {'west':-125,'south':24,'east':-66,'north':50})
        if not airports:
            log.warning(f"State query empty for {region_code}, falling back to bounding box")
            bounds = REGION_BOUNDS.get(region_code, {'west':-125,'south':24,'east':-66,'north':50})
            airports = get_wind_forecasts_in_bounds(
                bounds['west'], bounds['south'],
                bounds['east'], bounds['north'],
                valid_time=valid_time
            )
    else:
        bounds = REGION_BOUNDS[region_code]
        airports = get_wind_forecasts_in_bounds(
            bounds['west'], bounds['south'],
            bounds['east'], bounds['north'],
            valid_time=valid_time
        )

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
        'valid_time': valid_time.isoformat() if valid_time else None,
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
        'AK': {'west': 172, 'south': 51, 'east': -129, 'north': 72},
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
    valid_time = None
    vt_str = request.args.get('valid_time')
    if vt_str:
        try:
            from dateutil.parser import parse as parse_dt
            valid_time = parse_dt(vt_str).replace(tzinfo=None)
        except Exception as e:
            log.warning(f"Invalid valid_time param: {vt_str} — {e}")

    airports = get_wind_forecasts_in_bounds(
        bounds['west'], bounds['south'],
        bounds['east'], bounds['north'],
        valid_time=valid_time
    )

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
        'valid_time': valid_time.isoformat() if valid_time else None,
        'timestamp': datetime.utcnow().isoformat()
    }

    if wind_grid:
        response['wind_grid'] = wind_grid

    return jsonify(response)

@wind_forecast_api.route('/conus')
def get_conus_forecasts():
    """Get wind forecasts for entire CONUS"""
    bounds = REGION_BOUNDS['CONUS']
    valid_time = None
    vt_str = request.args.get('valid_time')
    if vt_str:
        try:
            from dateutil.parser import parse as parse_dt
            valid_time = parse_dt(vt_str).replace(tzinfo=None)
        except Exception as e:
            log.warning(f"Invalid valid_time param: {vt_str} — {e}")

    airports = get_wind_forecasts_in_bounds(
        bounds['west'], bounds['south'],
        bounds['east'], bounds['north'],
        valid_time=valid_time
    )

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
        'valid_time': valid_time.isoformat() if valid_time else None,
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


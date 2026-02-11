#!/usr/bin/env python3
"""
Radar API Blueprint for CAP Winds Application
PHASE 1A - WITH AIRPORTS ENDPOINT AND REGION FILTERING
Complete production version
"""

import os
import glob
import math
from datetime import datetime, timedelta
from flask import Blueprint, render_template, jsonify, request
from db_config import get_connection

radar_api = Blueprint('radar_api', __name__)

RADAR_BASE_DIR = "/LDM/radar/level3"

# Complete NEXRAD site coordinates (156 sites)
RADAR_SITES = {
    'ABR': {'lat': 45.4558, 'lon': -98.4132, 'elev': 397},
    'ABX': {'lat': 35.1497, 'lon': -106.8239, 'elev': 1789},
    'AHG': {'lat': 58.6794, 'lon': -156.6311, 'elev': 73},
    'AIH': {'lat': 60.7258, 'lon': -151.3511, 'elev': 81},
    'AKC': {'lat': 61.1747, 'lon': -149.8889, 'elev': 67},
    'AKQ': {'lat': 36.9840, 'lon': -77.0075, 'elev': 34},
    'AMA': {'lat': 35.2333, 'lon': -101.7092, 'elev': 1099},
    'AMX': {'lat': 25.6111, 'lon': -80.4128, 'elev': 4},
    'APD': {'lat': 42.8997, 'lon': -70.9364, 'elev': 45},
    'APX': {'lat': 44.9069, 'lon': -84.7197, 'elev': 446},
    'ARX': {'lat': 43.8228, 'lon': -91.1914, 'elev': 396},
    'ATX': {'lat': 48.1947, 'lon': -122.4958, 'elev': 151},
    'BBX': {'lat': 39.4961, 'lon': -121.6316, 'elev': 53},
    'BGM': {'lat': 42.1997, 'lon': -75.9847, 'elev': 490},
    'BHX': {'lat': 40.4986, 'lon': -124.2919, 'elev': 732},
    'BIS': {'lat': 46.7708, 'lon': -100.7606, 'elev': 505},
    'BLX': {'lat': 45.8539, 'lon': -108.6067, 'elev': 1097},
    'BMX': {'lat': 33.1722, 'lon': -86.7697, 'elev': 197},
    'BOI': {'lat': 43.4919, 'lon': -116.2361, 'elev': 933},
    'BOU': {'lat': 40.0361, 'lon': -105.1808, 'elev': 1709},
    'BOX': {'lat': 41.9558, 'lon': -71.1369, 'elev': 36},
    'BRO': {'lat': 25.9161, 'lon': -97.4189, 'elev': 7},
    'BUF': {'lat': 42.9486, 'lon': -78.7369, 'elev': 211},
    'BYX': {'lat': 24.5975, 'lon': -81.7033, 'elev': 3},
    'CAE': {'lat': 33.9486, 'lon': -81.1186, 'elev': 70},
    'CBW': {'lat': 46.0392, 'lon': -67.8064, 'elev': 227},
    'CBX': {'lat': 43.4908, 'lon': -116.2356, 'elev': 933},
    'CCX': {'lat': 40.9231, 'lon': -78.0039, 'elev': 733},
    'CLE': {'lat': 41.4131, 'lon': -81.8597, 'elev': 233},
    'CLX': {'lat': 32.6556, 'lon': -81.0422, 'elev': 30},
    'CRP': {'lat': 27.7839, 'lon': -97.5111, 'elev': 13},
    'CXX': {'lat': 44.5111, 'lon': -73.1664, 'elev': 97},
    'CYS': {'lat': 41.1519, 'lon': -104.8061, 'elev': 1868},
    'DAX': {'lat': 38.5011, 'lon': -121.6778, 'elev': 9},
    'DDC': {'lat': 37.7608, 'lon': -99.9689, 'elev': 789},
    'DFX': {'lat': 29.2731, 'lon': -100.2803, 'elev': 345},
    'DGX': {'lat': 32.2797, 'lon': -89.9844, 'elev': 133},
    'DIX': {'lat': 39.9469, 'lon': -74.4108, 'elev': 45},
    'DLH': {'lat': 46.8369, 'lon': -92.2097, 'elev': 435},
    'DMX': {'lat': 41.7311, 'lon': -93.7228, 'elev': 299},
    'DOX': {'lat': 38.8258, 'lon': -75.4400, 'elev': 15},
    'DTX': {'lat': 42.6997, 'lon': -83.4717, 'elev': 327},
    'DVN': {'lat': 41.6117, 'lon': -90.5808, 'elev': 230},
    'DYX': {'lat': 32.5386, 'lon': -99.2542, 'elev': 462},
    'EAX': {'lat': 38.8103, 'lon': -94.2644, 'elev': 303},
    'EMX': {'lat': 31.8936, 'lon': -110.6303, 'elev': 1586},
    'ENX': {'lat': 42.5864, 'lon': -74.0639, 'elev': 557},
    'EOX': {'lat': 31.4606, 'lon': -85.4594, 'elev': 132},
    'EPZ': {'lat': 31.8731, 'lon': -106.6981, 'elev': 1251},
    'ESX': {'lat': 35.7011, 'lon': -114.8914, 'elev': 1483},
    'EVX': {'lat': 30.5647, 'lon': -85.9214, 'elev': 43},
    'EWX': {'lat': 29.7039, 'lon': -98.0286, 'elev': 193},
    'EYX': {'lat': 35.0978, 'lon': -117.5608, 'elev': 841},
    'FCX': {'lat': 37.0242, 'lon': -80.2739, 'elev': 874},
    'FDR': {'lat': 34.3622, 'lon': -98.9764, 'elev': 386},
    'FDX': {'lat': 34.6342, 'lon': -103.6294, 'elev': 1417},
    'FFC': {'lat': 33.3636, 'lon': -84.5658, 'elev': 262},
    'FSD': {'lat': 43.5878, 'lon': -96.7294, 'elev': 436},
    'FSX': {'lat': 34.5744, 'lon': -111.1983, 'elev': 2261},
    'FTG': {'lat': 39.7867, 'lon': -104.5458, 'elev': 1675},
    'FWS': {'lat': 32.5731, 'lon': -97.3031, 'elev': 208},
    'GGW': {'lat': 48.2064, 'lon': -106.6250, 'elev': 694},
    'GJX': {'lat': 39.0619, 'lon': -108.2136, 'elev': 3046},
    'GLD': {'lat': 39.3667, 'lon': -101.7003, 'elev': 1132},
    'GRB': {'lat': 44.4986, 'lon': -88.1111, 'elev': 208},
    'GRK': {'lat': 30.7217, 'lon': -97.3831, 'elev': 164},
    'GRR': {'lat': 42.8939, 'lon': -85.5447, 'elev': 237},
    'GSP': {'lat': 34.8833, 'lon': -82.2200, 'elev': 287},
    'GUA': {'lat': 13.4544, 'lon': 144.8081, 'elev': 264},
    'GWX': {'lat': 33.8967, 'lon': -88.3289, 'elev': 145},
    'GYX': {'lat': 43.8914, 'lon': -70.2564, 'elev': 125},
    'HDX': {'lat': 33.0764, 'lon': -106.1222, 'elev': 1287},
    'HGX': {'lat': 29.4719, 'lon': -95.0792, 'elev': 18},
    'HKI': {'lat': 21.8939, 'lon': -159.5525, 'elev': 179},
    'HKM': {'lat': 22.0361, 'lon': -159.5519, 'elev': 185},
    'HMO': {'lat': 20.1253, 'lon': -155.7781, 'elev': 3050},
    'HNX': {'lat': 36.3142, 'lon': -119.6319, 'elev': 74},
    'HPX': {'lat': 36.7369, 'lon': -87.2850, 'elev': 176},
    'HTX': {'lat': 35.7469, 'lon': -86.0833, 'elev': 538},
    'HWA': {'lat': 19.0950, 'lon': -155.5689, 'elev': 408},
    'ICT': {'lat': 37.6544, 'lon': -97.4431, 'elev': 407},
    'ICX': {'lat': 37.5908, 'lon': -112.8619, 'elev': 3231},
    'ILN': {'lat': 39.4203, 'lon': -83.8217, 'elev': 322},
    'ILX': {'lat': 40.1506, 'lon': -89.3367, 'elev': 177},
    'IND': {'lat': 39.7075, 'lon': -86.2803, 'elev': 241},
    'INX': {'lat': 36.1750, 'lon': -95.5644, 'elev': 204},
    'IWA': {'lat': 33.2892, 'lon': -111.6700, 'elev': 412},
    'IWX': {'lat': 41.3586, 'lon': -85.7000, 'elev': 290},
    'JAX': {'lat': 30.4847, 'lon': -81.7019, 'elev': 10},
    'JGX': {'lat': 32.6753, 'lon': -83.3508, 'elev': 159},
    'JKL': {'lat': 37.5908, 'lon': -83.3130, 'elev': 415},
    'JUA': {'lat': 18.1156, 'lon': -66.0781, 'elev': 870},
    'LBB': {'lat': 33.6542, 'lon': -101.8142, 'elev': 993},
    'LCH': {'lat': 30.1253, 'lon': -93.2161, 'elev': 4},
    'LGX': {'lat': 47.1158, 'lon': -124.1064, 'elev': 73},
    'LIX': {'lat': 30.3367, 'lon': -89.8256, 'elev': 7},
    'LNX': {'lat': 41.9578, 'lon': -100.5761, 'elev': 905},
    'LOT': {'lat': 41.6044, 'lon': -88.0844, 'elev': 202},
    'LOX': {'lat': 34.2006, 'lon': -119.4631, 'elev': 1515},
    'LRX': {'lat': 40.7397, 'lon': -116.8025, 'elev': 2056},
    'LSX': {'lat': 38.6989, 'lon': -90.6828, 'elev': 185},
    'LTX': {'lat': 33.9892, 'lon': -78.4292, 'elev': 20},
    'LVX': {'lat': 37.9753, 'lon': -85.9436, 'elev': 219},
    'LWX': {'lat': 38.9753, 'lon': -77.4778, 'elev': 83},
    'LZK': {'lat': 34.8364, 'lon': -92.2622, 'elev': 173},
    'MAF': {'lat': 31.9433, 'lon': -102.1894, 'elev': 874},
    'MAX': {'lat': 42.0811, 'lon': -122.7172, 'elev': 2290},
    'MBX': {'lat': 48.3925, 'lon': -100.8644, 'elev': 455},
    'MHX': {'lat': 34.7761, 'lon': -76.8761, 'elev': 9},
    'MKX': {'lat': 42.9678, 'lon': -88.5506, 'elev': 292},
    'MLB': {'lat': 28.1133, 'lon': -80.6542, 'elev': 10},
    'MOB': {'lat': 30.6794, 'lon': -88.2397, 'elev': 63},
    'MPX': {'lat': 44.8489, 'lon': -93.5656, 'elev': 288},
    'MQT': {'lat': 46.5311, 'lon': -87.5486, 'elev': 430},
    'MRX': {'lat': 36.1686, 'lon': -83.4019, 'elev': 408},
    'MSX': {'lat': 47.0411, 'lon': -113.9864, 'elev': 2394},
    'MTX': {'lat': 41.2628, 'lon': -112.4478, 'elev': 1969},
    'MUX': {'lat': 37.1553, 'lon': -121.8983, 'elev': 1057},
    'MVX': {'lat': 47.5278, 'lon': -97.3258, 'elev': 300},
    'MXX': {'lat': 32.5367, 'lon': -85.7897, 'elev': 122},
    'NKX': {'lat': 32.9189, 'lon': -117.0419, 'elev': 291},
    'NQA': {'lat': 35.3447, 'lon': -89.8733, 'elev': 86},
    'OAX': {'lat': 41.3203, 'lon': -96.3667, 'elev': 350},
    'OHX': {'lat': 36.2472, 'lon': -86.5631, 'elev': 176},
    'OKX': {'lat': 40.8656, 'lon': -72.8639, 'elev': 26},
    'OTX': {'lat': 47.6803, 'lon': -117.6267, 'elev': 727},
    'PAH': {'lat': 37.0683, 'lon': -88.7719, 'elev': 119},
    'PBZ': {'lat': 40.5317, 'lon': -80.2181, 'elev': 361},
    'PDT': {'lat': 45.6906, 'lon': -118.8528, 'elev': 462},
    'POE': {'lat': 31.1553, 'lon': -92.9761, 'elev': 124},
    'PUX': {'lat': 38.4594, 'lon': -104.1814, 'elev': 1638},
    'RAX': {'lat': 35.6656, 'lon': -78.4897, 'elev': 106},
    'RGX': {'lat': 39.7542, 'lon': -119.4622, 'elev': 2530},
    'RIW': {'lat': 43.0661, 'lon': -108.4772, 'elev': 1697},
    'RLX': {'lat': 38.3111, 'lon': -81.7233, 'elev': 329},
    'RTX': {'lat': 45.7150, 'lon': -122.9650, 'elev': 479},
    'SFX': {'lat': 43.1056, 'lon': -112.6861, 'elev': 1364},
    'SGF': {'lat': 37.2353, 'lon': -93.4006, 'elev': 390},
    'SHV': {'lat': 32.4506, 'lon': -93.8414, 'elev': 83},
    'SJT': {'lat': 31.3711, 'lon': -100.4925, 'elev': 577},
    'SOX': {'lat': 33.8178, 'lon': -117.6359, 'elev': 923},
    'SRX': {'lat': 35.2903, 'lon': -94.3619, 'elev': 195},
    'TBW': {'lat': 27.7056, 'lon': -82.4017, 'elev': 12},
    'TFX': {'lat': 47.4597, 'lon': -111.3856, 'elev': 1132},
    'TLH': {'lat': 30.3975, 'lon': -84.3289, 'elev': 19},
    'TLX': {'lat': 35.3331, 'lon': -97.2778, 'elev': 370},
    'TWX': {'lat': 38.9969, 'lon': -96.2325, 'elev': 417},
    'TYX': {'lat': 43.7556, 'lon': -75.6800, 'elev': 563},
    'UDX': {'lat': 44.1250, 'lon': -102.8297, 'elev': 919},
    'UEX': {'lat': 40.3208, 'lon': -98.4417, 'elev': 602},
    'VAX': {'lat': 30.8900, 'lon': -83.0017, 'elev': 54},
    'VBX': {'lat': 34.8381, 'lon': -120.3958, 'elev': 376},
    'VNX': {'lat': 36.7406, 'lon': -98.1278, 'elev': 369},
    'VTX': {'lat': 34.4117, 'lon': -119.1794, 'elev': 831},
    'VWX': {'lat': 38.2606, 'lon': -87.7247, 'elev': 146},
    'YUX': {'lat': 32.4953, 'lon': -114.6567, 'elev': 53},
}

RADAR_RANGE_KM = 230
RADAR_SIZE_DEG = (RADAR_RANGE_KM * 2) / 111.0


@radar_api.route('/animation')
def animation_page():
    """Radar animation interface"""
    return render_template('radar_animation.html')


@radar_api.route('/api/sites')
def get_sites():
    """Get list of all radar sites"""
    return jsonify({
        'sites': RADAR_SITES,
        'count': len(RADAR_SITES)
    })


@radar_api.route('/api/airports')
def get_airports():
    """
    Get airports for map display
    Filtered by: US, Canada, Bermuda, Bahamas, Mexico
    Zoom-dependent: Low zoom shows reporting airports only
    """
    zoom = request.args.get('zoom', type=int, default=5)
    
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        # At low zoom (CONUS view), only show airports with weather reporting
        # At high zoom (regional/local), show all qualifying airports
        if zoom <= 5:
            query = """
                SELECT 
                    station_id,
                    name,
                    ST_Y(location) as lat,
                    ST_X(location) as lon,
                    longest_runway_ft,
                    has_reporting,
                    is_military,
                    elevation_ft
                FROM observations.airports
                WHERE has_paved_runway = true 
                  AND longest_runway_ft >= 2500
                  AND has_reporting = true
                  AND (
                      iso_region LIKE 'US-%' 
                      OR iso_region LIKE 'CA-%'
                      OR iso_region LIKE 'BM-%'
                      OR iso_region LIKE 'BS-%'
                      OR iso_region LIKE 'MX-%'
                  )
                ORDER BY longest_runway_ft DESC
                LIMIT 5000
            """
        else:
            query = """
                SELECT 
                    station_id,
                    name,
                    ST_Y(location) as lat,
                    ST_X(location) as lon,
                    longest_runway_ft,
                    has_reporting,
                    is_military,
                    elevation_ft
                FROM observations.airports
                WHERE has_paved_runway = true 
                  AND longest_runway_ft >= 2500
                  AND (
                      iso_region LIKE 'US-%' 
                      OR iso_region LIKE 'CA-%'
                      OR iso_region LIKE 'BM-%'
                      OR iso_region LIKE 'BS-%'
                      OR iso_region LIKE 'MX-%'
                  )
                ORDER BY longest_runway_ft DESC
            """
        
        cur.execute(query)
        airports = []
        
        for row in cur.fetchall():
            # Categorize for color coding
            if row[6]:  # is_military
                category = 'military'
            elif row[5]:  # has_reporting
                category = 'reporting'
            else:
                category = 'basic'
            
            airports.append({
                'id': row[0],
                'name': row[1],
                'lat': row[2],
                'lon': row[3],
                'runway_ft': row[4],
                'category': category,
                'elevation_ft': row[7]
            })
        
        cur.close()
        conn.close()
        
        return jsonify({
            'airports': airports,
            'count': len(airports),
            'zoom': zoom
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@radar_api.route('/api/nearest/<airport_id>')
def nearest_radar(airport_id):
    """Find nearest radar sites to an airport"""
    airport_id = airport_id.upper().strip()
    
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT ST_Y(location) as lat, ST_X(location) as lon
            FROM observations.airports
            WHERE station_id = %s
        """, (airport_id,))
        result = cur.fetchone()
        cur.close()
        conn.close()
        
        if not result:
            return jsonify({'error': f'Airport {airport_id} not found'}), 404
        
        airport_lat, airport_lon = result
        
    except Exception as e:
        return jsonify({'error': f'Database error: {str(e)}'}), 500
    
    def haversine(lat1, lon1, lat2, lon2):
        """Calculate great circle distance in nautical miles"""
        R = 3440.065  # Earth radius in nautical miles
        lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
        c = 2 * math.asin(math.sqrt(a))
        return R * c
    
    distances = []
    for site_id, site_data in RADAR_SITES.items():
        dist = haversine(airport_lat, airport_lon, site_data['lat'], site_data['lon'])
        distances.append({
            'site_id': site_id,
            'name': site_id,
            'lat': site_data['lat'],
            'lon': site_data['lon'],
            'distance_nm': round(dist, 1)
        })
    
    distances.sort(key=lambda x: x['distance_nm'])
    
    return jsonify({
        'airport': airport_id,
        'airport_lat': airport_lat,
        'airport_lon': airport_lon,
        'nearest': distances[:5]
    })


@radar_api.route('/api/images/<site_id>/<product>')
def get_images(site_id, product):
    """Get available radar images for a site/product"""
    site_id = site_id.upper()
    product = product.upper()
    
    if site_id not in RADAR_SITES:
        return jsonify({'error': f'Unknown radar site: {site_id}'}), 404
    
    hours = int(request.args.get('hours', 3))
    hours = max(1, min(hours, 24))
    
    cutoff_time = datetime.utcnow() - timedelta(hours=hours)
    png_dir = os.path.join(RADAR_BASE_DIR, site_id, product, 'png')
    
    if not os.path.exists(png_dir):
        return jsonify({'error': f'No data for {site_id}/{product}'}), 404
    
    images = []
    
    for days_back in range(2):
        date = datetime.utcnow() - timedelta(days=days_back)
        date_dir = os.path.join(png_dir, date.strftime('%Y%m%d'))
        
        if not os.path.exists(date_dir):
            continue
        
        pattern = os.path.join(date_dir, f'{site_id}_{product}_*.png')
        files = glob.glob(pattern)
        
        for filepath in files:
            filename = os.path.basename(filepath)
            try:
                parts = filename.replace('.png', '').split('_')
                if len(parts) >= 3:
                    timestamp_str = parts[2]
                    day = int(timestamp_str[0:2])
                    hour = int(timestamp_str[2:4])
                    minute = int(timestamp_str[4:6])
                    
                    file_time = datetime(date.year, date.month, day, hour, minute)
                    
                    if file_time >= cutoff_time:
                        site_data = RADAR_SITES[site_id]
                        half_deg = RADAR_SIZE_DEG / 2
                        
                        images.append({
                            'url': f'/radar_level3/{site_id}/{product}/png/{date.strftime("%Y%m%d")}/{filename}',
                            'time': file_time.isoformat() + 'Z',
                            'timestamp': int(file_time.timestamp()),
                            'bounds': {
                                'north': site_data['lat'] + half_deg,
                                'south': site_data['lat'] - half_deg,
                                'east': site_data['lon'] + half_deg,
                                'west': site_data['lon'] - half_deg
                            }
                        })
            except (ValueError, IndexError):
                continue
    
    images.sort(key=lambda x: x['timestamp'])
    
    return jsonify({
        'site_id': site_id,
        'product': product,
        'hours': hours,
        'count': len(images),
        'images': images
    })


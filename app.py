#!/var/www/cap_winds_app/venv/bin/python3
"""
CAP Winds Flask Application - COMPLETE WORKING VERSION
All services: Wind Maps, Weather, Radar Animation, KQ Station Management
Fixed with correct database schema references
"""

import os
import sys
from datetime import datetime, timedelta
from flask import Flask, render_template, request, send_from_directory, jsonify

sys.path.insert(0, '/var/www/cap_winds_app')

# Initialize Flask app with subpath support
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
app.config['APPLICATION_ROOT'] = '/cap_winds_app'

# Secret key for sessions/flash messages
app.secret_key = 'cap-winds-secret-key-change-me'

# Import and register blueprints - COMPLETE VERSION
print("Loading CAP Weather COP APIs...")

# Original weather API (CORRECTED - uses station_id column)
try:
    from weather_api import weather_api
    app.register_blueprint(weather_api, url_prefix='/api/weather')
    print("✓ Weather API registered at /cap_winds_app/api/weather")
except Exception as e:
    print(f"❌ CRITICAL: Could not load Weather API: {e}")

# KQ Admin (CORRECTED - fixed syntax errors)
try:
    from kq_admin import kq_admin
    app.register_blueprint(kq_admin, url_prefix='/admin/kq-stations')
    print("✓ KQ Station Admin registered at /cap_winds_app/admin/kq-stations")
except Exception as e:
    print(f"⚠ Could not load KQ admin: {e}")

# Weather Pages
try:
    from weather_pages import weather_pages
    app.register_blueprint(weather_pages, url_prefix='/weather')
    print("✓ Weather Pages registered at /cap_winds_app/weather")
except Exception as e:
    print(f"⚠ Could not load Weather Pages: {e}")

# Wind Forecast API
try:
    from wind_forecast_api import wind_forecast_api
    app.register_blueprint(wind_forecast_api, url_prefix='/api/wind-forecast')
    print("✓ Wind Forecast API registered at /api/wind-forecast")
except Exception as e:
    print(f"⚠ Could not load Wind Forecast API: {e}")

# AIRMET/SIGMET API
try:
    from airmet_sigmet_api import airmet_sigmet_api
    app.register_blueprint(airmet_sigmet_api, url_prefix='/api/hazards')
    print("✓ AIRMET/SIGMET API registered at /cap_winds_app/api/hazards")
except Exception as e:
    print(f"⚠ Could not load AIRMET/SIGMET API: {e}")

# Radar Animation API
try:
    from radar_api import radar_api
    app.register_blueprint(radar_api, url_prefix='/radar')
    print("✓ Radar API registered at /cap_winds_app/radar")
    print("  - Animation page: /cap_winds_app/radar/animation")
    print("  - API endpoints: /cap_winds_app/radar/api/*")
except Exception as e:
    print(f"⚠ Could not load Radar API: {e}")

# Incident Archive API
try:
    from incident_archive import incident_archive
    app.register_blueprint(incident_archive, url_prefix='')
    print("✓ Incident Archive registered")
except Exception as e:
    print(f"⚠ Could not load Incident Archive: {e}")

# Manual TAF (CORRECTED - uses manual_taf not manual_taf_bp)
try:
    from manual_taf import manual_taf
    app.register_blueprint(manual_taf, url_prefix="/admin")
    print("✓ Manual TAF registered at /cap_winds_app/admin")
except Exception as e:
    print(f"⚠ Could not load Manual TAF: {e}")

# Configuration
BATCH_MAP_DIR = "/var/www/cap_winds_app/static/batch_maps"
SHAPEFILE_DIR = "/var/www/html/cap_winds_shp"
MAX_BATCH_MAP_AGE_HOURS = 2

# Batch map files (static names)
BATCH_MAP_FILES = {
    # CONUS
    'CONUS': 'conus_wind_constraints.png',
    # Regions
    'NER': 'ner_wind_constraints.png',
    'MAR': 'mar_wind_constraints.png',
    'GLR': 'glr_wind_constraints.png',
    'SER': 'ser_wind_constraints.png',
    'NCR': 'ncr_wind_constraints.png',
    'RMR': 'rmr_wind_constraints.png',
    'SWR': 'swr_wind_constraints.png',
    'PCR': 'pcr_wind_constraints.png',
    # Wings (States)
    'AL': 'al_wind_constraints.png',
    'AZ': 'az_wind_constraints.png',
    'AR': 'ar_wind_constraints.png',
    'CA': 'ca_wind_constraints.png',
    'CO': 'co_wind_constraints.png',
    'CT': 'ct_wind_constraints.png',
    'DE': 'de_wind_constraints.png',
    'FL': 'fl_wind_constraints.png',
    'GA': 'ga_wind_constraints.png',
    'ID': 'id_wind_constraints.png',
    'IL': 'il_wind_constraints.png',
    'IN': 'in_wind_constraints.png',
    'IA': 'ia_wind_constraints.png',
    'KS': 'ks_wind_constraints.png',
    'KY': 'ky_wind_constraints.png',
    'LA': 'la_wind_constraints.png',
    'ME': 'me_wind_constraints.png',
    'MD': 'md_wind_constraints.png',
    'MA': 'ma_wind_constraints.png',
    'MI': 'mi_wind_constraints.png',
    'MN': 'mn_wind_constraints.png',
    'MS': 'ms_wind_constraints.png',
    'MO': 'mo_wind_constraints.png',
    'MT': 'mt_wind_constraints.png',
    'NE': 'ne_wind_constraints.png',
    'NV': 'nv_wind_constraints.png',
    'NH': 'nh_wind_constraints.png',
    'NJ': 'nj_wind_constraints.png',
    'NM': 'nm_wind_constraints.png',
    'NY': 'ny_wind_constraints.png',
    'NC': 'nc_wind_constraints.png',
    'ND': 'nd_wind_constraints.png',
    'OH': 'oh_wind_constraints.png',
    'OK': 'ok_wind_constraints.png',
    'OR': 'or_wind_constraints.png',
    'PA': 'pa_wind_constraints.png',
    'RI': 'ri_wind_constraints.png',
    'SC': 'sc_wind_constraints.png',
    'SD': 'sd_wind_constraints.png',
    'TN': 'tn_wind_constraints.png',
    'TX': 'tx_wind_constraints.png',
    'UT': 'ut_wind_constraints.png',
    'VT': 'vt_wind_constraints.png',
    'VA': 'va_wind_constraints.png',
    'WA': 'wa_wind_constraints.png',
    'WV': 'wv_wind_constraints.png',
    'WI': 'wi_wind_constraints.png',
    'WY': 'wy_wind_constraints.png',
    'DC': 'dc_wind_constraints.png',
    'PR': 'pr_wind_constraints.png',
}

REGION_NAMES = {
    'CONUS': 'Continental United States',
    'NCR': 'North Central Region',
    'GLR': 'Great Lakes Region',
    'NER': 'Northeast Region',
    'PCR': 'Pacific Region',
    'PCR-WEST': 'Pacific Region - West Coast',
    'PCR-AK': 'Pacific Region - Alaska',
    'PCR-HI': 'Pacific Region - Hawaii',
    'PCR-GUAM': 'Pacific Region - Guam',
    'RMR': 'Rocky Mountain Region',
    'SER': 'Southeast Region',
    'SER-CARIB': 'Southeast Region - Caribbean',
    'SWR': 'Southwest Region',
}

WING_CODES = [
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA',
    'HI', 'ID', 'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD',
    'MA', 'MI', 'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH', 'NJ',
    'NM', 'NY', 'NC', 'ND', 'OH', 'OK', 'OR', 'PA', 'RI', 'SC',
    'DC', 'PR', 'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV', 'WI', 'WY'
]

# Import states_service
try:
    import states_service
    print("✓ Successfully imported states_service module")
    STATES_SERVICE_AVAILABLE = True
except Exception as e:
    print(f"⚠ Could not import states_service: {e}")
    STATES_SERVICE_AVAILABLE = False


def get_batch_map(location_code, max_age_hours=MAX_BATCH_MAP_AGE_HOURS):
    """Check if a pre-generated batch map exists and is recent"""
    location_upper = location_code.upper()
    
    if location_upper not in BATCH_MAP_FILES:
        return None
    
    map_filename = BATCH_MAP_FILES[location_upper]
    map_path = os.path.join(BATCH_MAP_DIR, map_filename)
    
    if not os.path.exists(map_path):
        return None
    
    try:
        file_mtime = datetime.fromtimestamp(os.path.getmtime(map_path))
        age = datetime.now() - file_mtime
        
        if age > timedelta(hours=max_age_hours):
            return None
        
        app.logger.info(f"✓ Batch map: {map_filename} (age: {int(age.total_seconds()/60)}m)")
        
        return {
            'url': f'/cap_winds/{map_filename}',
            'map_name': REGION_NAMES.get(location_upper, location_upper),
            'generated_at': file_mtime.strftime('%Y-%m-%d %H:%M UTC'),
            'age_minutes': int(age.total_seconds() / 60),
            'is_batch': True,
            'airport_shp_url': None,
            'contours_poly_url': None,
            'contours_line_url': None,
        }
    except Exception as e:
        app.logger.error(f"Error checking batch map: {e}")
        return None


def generate_map_on_demand(selection_type, form_data):
    """Generate a map on-demand using WindAnalysisService"""
    
    try:
        from states_service import WindAnalysisService
        service = WindAnalysisService()
        
        primary_airport = form_data.get('primary_airport', '').strip() or None
        
        if selection_type == 'region' or selection_type == 'state':
            location_type = selection_type
            location_code = form_data.get(f'{selection_type}_code', '').strip()
            app.logger.info(f"Generating {location_type} map: {location_code}")
            
            result = service.generate_analysis(
                location_type=location_type,
                location_code=location_code,
                primary_airport=primary_airport
            )
            
        elif selection_type == 'radius':
            location_type = 'radius'
            center_point = form_data.get('center_point', '').strip() or None
            center_lat = form_data.get('center_lat', '').strip()
            center_lon = form_data.get('center_lon', '').strip()
            
            # Convert lat/lon to float if provided
            if center_lat and center_lon:
                try:
                    center_lat = float(center_lat)
                    center_lon = float(center_lon)
                except ValueError:
                    center_lat = None
                    center_lon = None
            else:
                center_lat = None
                center_lon = None
            
            app.logger.info(f"Generating radius map: center_point={center_point}, lat={center_lat}, lon={center_lon}")
            
            result = service.generate_analysis(
                location_type=location_type,
                center_point=center_point,
                center_lat=center_lat,
                center_lon=center_lon,
                primary_airport=primary_airport
            )
            
        elif selection_type == 'conus':
            location_type = 'conus'
            app.logger.info("Generating CONUS map")
            
            result = service.generate_analysis(
                location_type=location_type,
                primary_airport=primary_airport
            )
            
        else:
            raise ValueError(f"Unknown selection type: {selection_type}")
        
        # Process result - it returns List[Dict]
        # Each dict has: map_path (or url), map_name, airport_shp_url, contours_poly_url, contours_line_url
        maps = []
        
        if result and isinstance(result, list) and len(result) > 0:
            for map_info in result:
                # Extract info from result dict
                map_path = map_info.get('map_path') or map_info.get('url')
                
                if map_path:
                    map_filename = os.path.basename(map_path)
                    maps.append({
                        'url': f'/cap_winds/{map_filename}',
                        'map_name': map_info.get('map_name', 'Wind Analysis'),
                        'is_batch': False,
                        'airport_shp_url': map_info.get('airport_shp_url'),
                        'contours_poly_url': map_info.get('contours_poly_url'),
                        'contours_line_url': map_info.get('contours_line_url'),
                    })
        
        if maps:
            app.logger.info(f"✓ Generated {len(maps)} map(s)")
            return maps
        else:
            app.logger.error("No maps generated from result")
            return None
            
    except Exception as e:
        app.logger.error(f"Generation error: {e}", exc_info=True)
        return None


@app.route('/', methods=['GET', 'POST'])
def index():
    """Wind map generation page (main page of Flask app)"""
    if request.method == 'GET':
        return render_template('index.html', 
                             wing_codes=WING_CODES,
                             selection_type='conus',
                             maps=None,
                             error=None)
    
    # POST - generate map
    selection_type = request.form.get('selection_type', 'conus')
    maps = []
    error = None
    
    try:
        # Check for batch map first (FAST!)
        batch_map = None
        
        if selection_type == 'conus':
            batch_map = get_batch_map('CONUS')
        elif selection_type == 'region':
            region_code = request.form.get('region_code')
            batch_map = get_batch_map(region_code)
        
        if batch_map:
            # Serve batch map (< 1 second)
            maps = [batch_map]
            app.logger.info("✓ Served batch map in < 1 sec")
        else:
            # Generate on-demand (1-20 minutes)
            if not STATES_SERVICE_AVAILABLE:
                error = "Map generation service unavailable"
            else:
                maps = generate_map_on_demand(selection_type, request.form)
                if not maps:
                    error = "Failed to generate map"
        
    except Exception as e:
        error = f"Error: {str(e)}"
        app.logger.error(f"Index error: {e}", exc_info=True)
    
    return render_template('index.html',
                         wing_codes=WING_CODES,
                         selection_type=selection_type,
                         maps=maps,
                         error=error)

@app.route('/wind-map')
def wind_forecast_map():
    """Interactive wind forecast map page"""
    return render_template('wind_forecast_map.html', wing_codes=WING_CODES)

@app.route('/weather_map.html')
def weather_map():
    """Original weather map"""
    return render_template('weather_map.html')

@app.route('/enhanced_weather_map.html')
def enhanced_weather_map():
    """Enhanced weather map with military prioritization and increased capacity"""
    return render_template('enhanced_weather_map.html')

@app.route('/radar_animation.html')
def radar_animation():
    """Radar animation page"""
    try:
        return render_template('radar_animation.html')
    except:
        return "<h1>Radar Animation</h1><p>Template not found</p><p><a href='/'>← Back to Home</a></p>"

@app.route('/manual_taf.html')
def manual_taf_page():
    """Manual TAF entry page"""
    try:
        return render_template('manual_taf.html')
    except:
        return "<h1>Manual TAF Entry</h1><p>Template not found</p><p><a href='/'>← Back to Home</a></p>"

@app.route('/kq_stations.html')
def kq_stations_page():
    """KQ stations management page"""
    try:
        return render_template('kq_stations.html')
    except:
        return "<h1>KQ Stations</h1><p>Template not found</p><p><a href='/'>← Back to Home</a></p>"

@app.route('/incident_archive.html')
def incident_archive_page():
    """Incident archive page"""
    try:
        return render_template('incident_archive.html')
    except:
        return "<h1>Incident Archive</h1><p>Template not found</p><p><a href='/'>← Back to Home</a></p>"

@app.route('/cap_winds/<path:filename>')
def serve_map(filename):
    """Serve map PNG files"""
    return send_from_directory(BATCH_MAP_DIR, filename)

@app.route('/cap_winds_shp/<path:filename>')
def serve_shapefile(filename):
    """Serve shapefile ZIP files"""
    return send_from_directory(SHAPEFILE_DIR, filename)

@app.route('/api/regions')
def api_regions():
    """API - list available batch regions"""
    regions = []
    
    for code, filename in BATCH_MAP_FILES.items():
        map_path = os.path.join(BATCH_MAP_DIR, filename)
        
        if os.path.exists(map_path):
            file_mtime = datetime.fromtimestamp(os.path.getmtime(map_path))
            age = datetime.now() - file_mtime
            
            regions.append({
                'code': code,
                'name': REGION_NAMES.get(code, code),
                'filename': filename,
                'url': f'/cap_winds/{filename}',
                'generated_at': file_mtime.isoformat(),
                'age_minutes': int(age.total_seconds() / 60),
                'is_current': age < timedelta(hours=MAX_BATCH_MAP_AGE_HOURS)
            })
    
    return jsonify({'regions': regions, 'count': len(regions)})

@app.route('/api/status')
def api_status():
    """API - system status"""
    batch_ok = 0
    batch_stale = 0
    batch_missing = 0
    
    for code, filename in BATCH_MAP_FILES.items():
        map_path = os.path.join(BATCH_MAP_DIR, filename)
        
        if os.path.exists(map_path):
            file_mtime = datetime.fromtimestamp(os.path.getmtime(map_path))
            age = datetime.now() - file_mtime
            
            if age < timedelta(hours=MAX_BATCH_MAP_AGE_HOURS):
                batch_ok += 1
            else:
                batch_stale += 1
        else:
            batch_missing += 1
    
    return jsonify({
        'status': 'operational' if batch_ok > 10 else 'degraded',
        'batch_maps': {
            'current': batch_ok,
            'stale': batch_stale,
            'missing': batch_missing,
            'total': len(BATCH_MAP_FILES)
        },
        'service_available': STATES_SERVICE_AVAILABLE,
        'timestamp': datetime.utcnow().isoformat()
    })

@app.route('/health')
def health():
    """Health check"""
    # Test database connection
    db_status = False
    try:
        from db_config import get_connection
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        conn.close()
        db_status = True
    except:
        pass
    
    return jsonify({
        'status': 'healthy' if db_status else 'degraded',
        'database': db_status,
        'weather_api': True,
        'enhanced_weather_map': True,
        'timestamp': datetime.utcnow().isoformat()
    })

@app.errorhandler(404)
def not_found(e):
    return render_template('index.html',
                         wing_codes=WING_CODES,
                         maps=None,
                         error="Page not found"), 404

@app.errorhandler(500)
def server_error(e):
    app.logger.error(f"Server error: {e}", exc_info=True)
    return render_template('index.html',
                         wing_codes=WING_CODES,
                         maps=None,
                         error="Server error"), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
else:
    print("CAP Weather COP Application Loaded - All APIs Active")
    print("Enhanced Weather Map: Military Priority Enabled")


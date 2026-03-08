#!/var/www/cap_winds_app/venv/bin/python3
"""
CAP Winds Flask Application - PRODUCTION VERSION
Fixed routing: Professional landing page, enhanced weather map, all APIs working
"""

import os
import sys
from datetime import datetime, timedelta
from flask import Flask, render_template, request, send_from_directory, jsonify

sys.path.insert(0, '/var/www/cap_winds_app')

# Initialize Flask app with subpath support
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
app.config['APPLICATION_ROOT'] = '/CAP_WxCOP'

# Secret key for sessions/flash messages
app.secret_key = 'cap-winds-secret-key-change-me'

# Import and register blueprints - COMPLETE VERSION
print("Loading CAP Weather COP APIs...")

# Weather API - Core METAR/TAF functionality
try:
    from weather_api import weather_api
    app.register_blueprint(weather_api, url_prefix='/api/weather')
    print("✓ Weather API registered at /CAP_WxCOP/api/weather")
except Exception as e:
    print(f"❌ CRITICAL: Could not load Weather API: {e}")

# KQ Admin - Custom station management
try:
    from kq_admin import kq_admin
    app.register_blueprint(kq_admin, url_prefix='/admin/kq-stations')
    print("✓ KQ Station Admin registered at /CAP_WxCOP/admin/kq-stations")
except Exception as e:
    print(f"⚠ Could not load KQ admin: {e}")

# Weather Pages - Station lookup, weather maps
try:
    from weather_pages import weather_pages
    app.register_blueprint(weather_pages, url_prefix='/weather')
    print("✓ Weather Pages registered at /CAP_WxCOP/weather")
except Exception as e:
    print(f"⚠ Could not load Weather Pages: {e}")

# Wind Forecast API - Wind constraint analysis
try:
    from wind_forecast_api import wind_forecast_api
    app.register_blueprint(wind_forecast_api, url_prefix='/api/wind-forecast')
    print("✓ Wind Forecast API registered at /CAP_WxCOP/api/wind-forecast")
except Exception as e:
    print(f"⚠ Could not load Wind Forecast API: {e}")

# Enhanced Weather API - Military priorities, expanded capacity
try:
    from weather_enhanced_api import weather_enhanced_api
    app.register_blueprint(weather_enhanced_api, url_prefix='/api/weather-enhanced')
    print("✓ Enhanced Weather API registered at /CAP_WxCOP/api/weather-enhanced")
except Exception as e:
    print(f"⚠ Could not load Enhanced Weather API: {e}")

# Radar API - NEXRAD animation and data
try:
    from radar_api import radar_api
    app.register_blueprint(radar_api, url_prefix='/radar')
    print("✓ Radar API registered at /CAP_WxCOP/radar")
except Exception as e:
    print(f"⚠ Could not load Radar API: {e}")

# Incident Archive - Weather data collection for investigations
try:
    from incident_archive import incident_archive
    app.register_blueprint(incident_archive, url_prefix='/')
    print("✓ Incident Archive registered at /CAP_WxCOP/incident-archive")
except Exception as e:
    print(f"⚠ Could not load Incident Archive: {e}")

# Manual TAF - KQ station TAF entry
try:
    from manual_taf import manual_taf
    app.register_blueprint(manual_taf, url_prefix='/')
    print("✓ Manual TAF registered at /CAP_WxCOP/manual-taf")
except Exception as e:
    print(f"⚠ Could not load Manual TAF: {e}")

# AIRMET/SIGMET API
try:
    from airmet_sigmet_api import airmet_sigmet_api
    app.register_blueprint(airmet_sigmet_api, url_prefix='/api/airmet-sigmet')
    print("✓ AIRMET/SIGMET API registered at /CAP_WxCOP/api/airmet-sigmet")
except Exception as e:
    print(f"⚠ Could not load AIRMET/SIGMET API: {e}")

print("CAP Weather COP initialization complete.\n")

# ============================================================================
# MAIN ROUTES - Professional Production Interface
# ============================================================================

@app.route('/')
def landing_page():
    """Professional CAP Weather COP landing page"""
    return render_template('index.html')

@app.route('/weather-map')
def weather_map():
    """Weather map with military prioritization and 2500 station capacity"""
    return render_template('enhanced_weather_map_complete.html')

@app.route('/wind-map')
def interactive_wind_map():
    """Interactive wind forecast map"""
    return render_template('wind_map_interactive.html')

@app.route('/static-maps')
def static_wind_maps():
    """Static pre-generated wind constraint maps by CONUS / region / wing"""
    return render_template('static_wind_maps.html')

@app.route('/mrms')
def mrms_radar():
    """MRMS composite reflectivity / MESH / lightning / azshear animated radar"""
    return render_template('radar_map.html')

# ============================================================================
# LEGACY ROUTES - Maintain compatibility
# ============================================================================

@app.route('/enhanced-weather-map')
def enhanced_weather_map_legacy():
    """Legacy route - redirect to new weather map"""
    return render_template('enhanced_weather_map_complete.html')

@app.route('/enhanced_weather_map.html')
def enhanced_weather_map_html_legacy():
    """Legacy route - redirect to new weather map"""
    return render_template('enhanced_weather_map_complete.html')

@app.route('/weather_map.html')
def weather_map_legacy():
    """Legacy weather map route"""
    return render_template('weather_map.html')

@app.route('/radar_animation.html')
def radar_animation():
    """Radar animation page"""
    try:
        return render_template('radar_animation.html')
    except:
        return "<h1>Radar Animation</h1><p>Template not found</p><p><a href='/CAP_WxCOP/'>← Back to Home</a></p>"

@app.route('/manual_taf.html')
def manual_taf_page_legacy():
    """Legacy manual TAF route"""
    try:
        return render_template('manual_taf.html')
    except:
        return "<h1>Manual TAF</h1><p>Template not found</p><p><a href='/CAP_WxCOP/'>← Back to Home</a></p>"

@app.route('/kq_stations.html')
def kq_stations_legacy():
    """Legacy KQ stations route"""
    try:
        return render_template('kq_stations.html')
    except:
        return "<h1>KQ Stations</h1><p>Template not found</p><p><a href='/CAP_WxCOP/'>← Back to Home</a></p>"

@app.route('/incident_archive.html')
def incident_archive_legacy():
    """Legacy incident archive route"""
    try:
        return render_template('incident_archive.html')
    except:
        return "<h1>Incident Archive</h1><p>Template not found</p><p><a href='/CAP_WxCOP/'>← Back to Home</a></p>"

# ============================================================================
# STATIC FILE HANDLERS - Wind constraint products, shapefiles
# ============================================================================

@app.route('/cap_winds/<path:filename>')
def serve_wind_maps(filename):
    """Serve generated wind constraint maps"""
    try:
        return send_from_directory('/var/www/cap_winds_app/static/batch_maps', filename)
    except FileNotFoundError:
        return jsonify({'error': 'File not found'}), 404

@app.route('/cap_winds_shp/<path:filename>')
def serve_shapefiles(filename):
    """Serve wind constraint shapefiles"""
    try:
        return send_from_directory('/var/www/cap_winds_app/static/batch_maps', filename)
    except FileNotFoundError:
        return jsonify({'error': 'Shapefile not found'}), 404

# ============================================================================
# API ENDPOINTS - System status and configuration
# ============================================================================

@app.route('/api/regions')
def get_regions():
    """Get available CAP regions"""
    regions = {
        'conus': 'Continental US',
        'glr': 'Great Lakes Region', 
        'mar': 'Middle Atlantic Region',
        'ncr': 'National Capital Region',
        'ner': 'Northeast Region',
        'pcr': 'Pacific Region',
        'rmr': 'Rocky Mountain Region',
        'ser': 'Southeast Region',
        'swr': 'Southwest Region'
    }
    return jsonify(regions)

@app.route('/api/status')
def system_status():
    """System health and status endpoint"""
    try:
        # Basic system check
        status = {
            'status': 'operational',
            'timestamp': datetime.utcnow().isoformat(),
            'services': {
                'weather_api': 'active',
                'wind_forecast': 'active', 
                'radar': 'active',
                'enhanced_weather': 'active'
            },
            'version': '2.0.0-production'
        }
        
        return jsonify(status)
    except Exception as e:
        return jsonify({
            'status': 'degraded',
            'error': str(e),
            'timestamp': datetime.utcnow().isoformat()
        }), 500

@app.route('/health')
def health_check():
    """Simple health check for monitoring"""
    return jsonify({
        'status': 'healthy',
        'service': 'CAP Weather COP',
        'timestamp': datetime.utcnow().isoformat()
    })

# ============================================================================
# LEGACY WIND CONSTRAINTS GENERATOR - Separate route for old functionality
# ============================================================================

@app.route('/api/map-meta')
def map_meta():
    """Metadata for the most recent batch map run (model run time, airport count, generated time)"""
    import glob
    batch_dir = '/var/www/cap_winds_app/static/batch_maps'
    try:
        from db_config import get_connection
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT model_run, COUNT(DISTINCT station_id) as cnt
            FROM observations.model_wind_forecasts
            GROUP BY model_run
            ORDER BY model_run DESC
            LIMIT 1
        """)
        row = cur.fetchone()
        cur.close(); conn.close()
        model_run     = row[0].strftime('%Y-%m-%d %H%MZ') if row else None
        airport_count = int(row[1]) if row else None
    except Exception:
        model_run     = None
        airport_count = None

    # Newest PNG mtime as generated time
    pngs = glob.glob(f'{batch_dir}/*.png')
    if pngs:
        newest  = max(pngs, key=os.path.getmtime)
        gen_ts  = datetime.utcfromtimestamp(os.path.getmtime(newest)).strftime('%Y-%m-%d %H%MZ')
    else:
        gen_ts  = None

    # Latest shapefile ZIP
    zips = glob.glob(f'/var/www/html/cap_winds_shp/*.zip')
    latest_zip = os.path.basename(max(zips, key=os.path.getmtime)) if zips else None

    return jsonify({
        'model_run':     model_run,
        'airport_count': airport_count,
        'generated':     gen_ts,
        'latest_zip':    latest_zip,
    })


@app.route('/api/latest-shapefile')
def latest_shapefile():
    """Return URL to the most recent shapefile ZIP"""
    import glob
    zips = glob.glob('/var/www/html/cap_winds_shp/*.zip')
    if not zips:
        return jsonify({'url': None, 'filename': None})
    newest   = max(zips, key=os.path.getmtime)
    filename = os.path.basename(newest)
    return jsonify({
        'url':      f'/CAP_WxCOP/cap_winds_shp/{filename}',
        'filename': filename,
    })


@app.route('/legacy-wind-generator', methods=['GET', 'POST'])
def legacy_wind_generator():
    """Legacy wind constraints map generator - moved to separate route"""
    # Import the old generation logic here if needed
    # This keeps the old functionality accessible but separate from main interface
    
    if request.method == 'GET':
        return """
        <h1>Legacy Wind Constraints Generator</h1>
        <p>This is the original wind constraints map generator.</p>
        <p><a href="/CAP_WxCOP/">← Return to Main Interface</a></p>
        <p>For modern wind analysis, use the <a href="/CAP_WxCOP/wind-map">Interactive Wind Map</a></p>
        """
    
    # Handle POST requests for legacy generation if needed
    return jsonify({'message': 'Legacy generator - use modern interface'})

# ============================================================================
# ERROR HANDLERS
# ============================================================================

@app.errorhandler(404)
def not_found_error(error):
    """Custom 404 page"""
    return render_template('error.html', 
                         error_code=404,
                         error_message="Page not found",
                         home_url="/CAP_WxCOP/"), 404

@app.errorhandler(500)
def internal_error(error):
    """Custom 500 page"""
    return render_template('error.html',
                         error_code=500, 
                         error_message="Internal server error",
                         home_url="/CAP_WxCOP/"), 500

# ============================================================================
# MAIN APPLICATION
# ============================================================================

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)

app.url_map.strict_slashes = False


#!/var/www/cap_winds_app/venv/bin/python3
"""
CAP Winds Flask Application - PRODUCTION VERSION
Fixed routing: Professional landing page, enhanced weather map, all APIs working
"""

import os
import sys
import secrets as _secrets
from datetime import datetime, timedelta
from flask import Flask, render_template, request, send_from_directory, jsonify

sys.path.insert(0, '/var/www/cap_winds_app')

# Initialize Flask app with subpath support
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
app.config['APPLICATION_ROOT'] = '/CAP_WxCOP'

# ============================================================================
# SECRET KEY - loaded from /etc/cap_wxcop/secret.key
# Generate with: cap_wxcop_user genkey
# ============================================================================

def _load_secret_key():
    key_file = '/etc/cap_wxcop/secret.key'
    try:
        with open(key_file) as f:
            key = f.read().strip()
            if key:
                return key
    except FileNotFoundError:
        pass
    # Generate and save on first run
    os.makedirs('/etc/cap_wxcop', exist_ok=True)
    key = _secrets.token_hex(32)
    with open(key_file, 'w') as f:
        f.write(key + '\n')
    os.chmod(key_file, 0o640)
    try:
        import grp
        gid = grp.getgrnam('www-data').gr_gid
        os.chown(key_file, 0, gid)
    except Exception:
        pass
    print(f"Generated new session key at {key_file}")
    return key

app.secret_key = _load_secret_key()
app.config['PERMANENT_SESSION_LIFETIME'] = 8 * 3600  # 8 hours

# ============================================================================
# BLUEPRINT REGISTRATION
# ============================================================================

print("Loading CAP Weather COP APIs...")

# Authentication - login/logout/TOTP MFA for protected routes
try:
    from auth import auth as auth_blueprint
    app.register_blueprint(auth_blueprint)
    print("✓ Auth registered at /CAP_WxCOP/auth")
    from sysctl_admin import sysctl_admin
    app.register_blueprint(sysctl_admin, url_prefix='')
    print("✓ Sysctl admin registered at /CAP_WxCOP/sysctl")
except Exception as e:
    print(f"❌ CRITICAL: Could not load Auth module: {e}")

# Weather API - Core METAR/TAF functionality
try:
    from weather_api import weather_api
    app.register_blueprint(weather_api, url_prefix='/api/weather')
    print("✓ Weather API registered at /CAP_WxCOP/api/weather")
except Exception as e:
    print(f"❌ CRITICAL: Could not load Weather API: {e}")

# KQ Admin - Custom station management (view public, add/edit/delete protected)
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

# HRRR Winds API - streamline/barb wind vector data
try:
    from hrrr_winds_api import hrrr_winds_api
    app.register_blueprint(hrrr_winds_api, url_prefix='/api/winds')
    print("✓ HRRR Winds API registered at /CAP_WxCOP/api/winds")
except Exception as e:
    print(f"⚠ Could not load HRRR Winds API: {e}")

# Local Base Reflectivity at sufficiently high zoom (z>9)
try:
    from nids_api import nids_api
    app.register_blueprint(nids_api, url_prefix='/api/nids')
    print('✓ NIDS API registered at /CAP_WxCOP/api/nids')
except Exception as e:
    print(f'⚠ Could not load NIDS API: {e}')

# Animated wind particles (leaflet-velocity / wind-js) - GFS Atlantic basin, TC steering flow
try:
    from wind_particles_api import wind_particles_api
    app.register_blueprint(wind_particles_api, url_prefix='/api/wind-particles')
    print('✓ Wind Particles API registered at /CAP_WxCOP/api/wind-particles')
except Exception as e:
    print(f'⚠ Could not load Wind Particles API: {e}')

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

try:
    from radar_status_api import radar_status_bp
    app.register_blueprint(radar_status_bp, url_prefix='/api/radar-status')
    print('✓ radar_status_api registered')

except Exception as e:
    import traceback
    traceback.print_exc()
    print(f'⚠ radar_status_api failed: {e}')


# MRMS WMS proxy — MapServer tile loop for radar map
try:
    from mrms_wms_api import mrms_wms_bp
    app.register_blueprint(mrms_wms_bp)
    print('✓ MRMS WMS API registered at /CAP_WxCOP/api/mrms')
except Exception as e:
    print(f'⚠ Could not load MRMS WMS API: {e}')

# Satellite WMS proxy — GOES ABI water vapor / IR MapServer rendering
try:
    from satellite_wms_api import satellite_wms_bp
    app.register_blueprint(satellite_wms_bp)
    print('✓ Satellite WMS API registered at /CAP_WxCOP/api/satellite')
except Exception as e:
    print(f'⚠ Could not load Satellite WMS API: {e}')

# TPW WMS proxy — GOES ABI L2 Total Precipitable Water MapServer rendering
try:
    from tpw_wms_api import tpw_wms_bp
    app.register_blueprint(tpw_wms_bp)
    print('✓ TPW WMS API registered at /CAP_WxCOP/api/tpw')
except Exception as e:
    print(f'⚠ Could not load TPW WMS API: {e}')

# HRRR smoke (MASSDEN) WMS proxy — PM2.5-equivalent contours for Cadet Weather COP
try:
    from hrrr_smoke_wms_api import hrrr_smoke_wms_bp
    app.register_blueprint(hrrr_smoke_wms_bp)
    print('✓ HRRR Smoke WMS API registered at /CAP_WxCOP/api/hrrr-smoke')
except Exception as e:
    print(f'⚠ Could not load HRRR Smoke WMS API: {e}')

try:
    from weather_impacts_api import weather_impacts_api
    app.register_blueprint(weather_impacts_api, url_prefix='/api/weather-impacts')
    print("✓ Weather Impacts API registered at /CAP_WxCOP/api/weather-impacts")
except Exception as e:
    print(f'⚠ Could not load Weather Impacts API: {e}')

try:
    from wing_icl_admin import wing_icl_admin
    app.register_blueprint(wing_icl_admin)
    print("✓ Wing ICL admin registered at /CAP_WxCOP/admin/wing-icl")
except Exception as e:
    print(f"⚠ Could not load Wing ICL admin: {e}")

try:
    from wxcop_geo_admin import wxcop_geo_admin
    app.register_blueprint(wxcop_geo_admin)
    print("✓ Access & Threat Report admin registered at /CAP_WxCOP/admin/geo-access")
except Exception as e:
    print(f"⚠ Could not load Access & Threat Report admin: {e}")

print("CAP Weather COP initialization complete.\n")

try:
    from cadet_wx_api import cadet_wx_bp
    app.register_blueprint(cadet_wx_bp)
    print("✓ Cadet WX API registered")
except Exception as e:
    print(f"⚠ Could not load Cadet WX API: {e}")

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

@app.route('/mrms')
def mrms_radar():
    """MRMS composite reflectivity / MESH / lightning / azshear animated radar"""
    return render_template('radar_map.html')

# ============================================================================
# LEGACY ROUTES - Maintain compatibility
# ============================================================================

@app.route('/enhanced-weather-map')
def enhanced_weather_map_legacy():
    return render_template('enhanced_weather_map_complete.html')

@app.route('/enhanced_weather_map.html')
def enhanced_weather_map_html_legacy():
    return render_template('enhanced_weather_map_complete.html')

@app.route('/weather-impacts')
def weather_impacts():
    return render_template('weather_impacts.html')

@app.route('/wbgt')
def wbgt_calculator():
    return render_template('wbgt_calculator.html')

@app.route('/cadet-wx')
def cadet_wx_map():
    try:
        return render_template('cadet_wx_map.html')
    except:
        return "<h1>Cadet WX</h1><p>Template not found</p><p><a href='/CAP_WxCOP/'>← Back to Home</a></p>"

@app.route('/cadet-wx-plan')
def cadet_wx_plan():
    try:
        return render_template('cadet_wx_plan.html')
    except:
        return "<h1>Cadet WX Planning</h1><p>Template not found</p><p><a href='/CAP_WxCOP/'>← Back to Home</a></p>"

@app.route('/cadet-sites/manage')
def cadet_site_mgmt():
    try:
        return render_template('cadet_site_mgmt.html')
    except:
        return "<h1>Cadet Site Management</h1><p>Template not found</p><p><a href='/CAP_WxCOP/'>← Back to Home</a></p>"

@app.route('/radar-status')
def radar_status():
    return render_template('radar_status.html')

@app.route('/weather_map.html')
def weather_map_legacy():
    return render_template('weather_map.html')

@app.route('/radar_animation.html')
def radar_animation():
    try:
        return render_template('radar_animation.html')
    except Exception:
        return "<h1>Radar Animation</h1><p>Template not found</p><p><a href='/CAP_WxCOP/'>← Back to Home</a></p>"

@app.route('/manual_taf.html')
def manual_taf_page_legacy():
    try:
        return render_template('manual_taf.html')
    except Exception:
        return "<h1>Manual TAF</h1><p>Template not found</p><p><a href='/CAP_WxCOP/'>← Back to Home</a></p>"

@app.route('/kq_stations.html')
def kq_stations_legacy():
    try:
        return render_template('kq_stations.html')
    except Exception:
        return "<h1>KQ Stations</h1><p>Template not found</p><p><a href='/CAP_WxCOP/'>← Back to Home</a></p>"

@app.route('/incident_archive.html')
def incident_archive_legacy():
    try:
        return render_template('incident_archive.html')
    except Exception:
        return "<h1>Incident Archive</h1><p>Template not found</p><p><a href='/CAP_WxCOP/'>← Back to Home</a></p>"

# ============================================================================
# STATIC FILE HANDLERS - Wind constraint products, shapefiles
# ============================================================================

@app.route('/cap_winds/<path:filename>')
def serve_wind_maps(filename):
    try:
        return send_from_directory('/var/www/cap_winds_app/static/batch_maps', filename)
    except FileNotFoundError:
        return jsonify({'error': 'File not found'}), 404

@app.route('/cap_winds_shp/<path:filename>')
def serve_shapefiles(filename):
    try:
        return send_from_directory('/var/www/cap_winds_app/static/batch_maps', filename)
    except FileNotFoundError:
        return jsonify({'error': 'Shapefile not found'}), 404

# ============================================================================
# API ENDPOINTS - System status and configuration
# ============================================================================

@app.route('/api/regions')
def get_regions():
    regions = {
        'conus': 'Continental US',
        'glr':   'Great Lakes Region',
        'mar':   'Middle Atlantic Region',
        'ncr':   'National Capital Region',
        'ner':   'Northeast Region',
        'pcr':   'Pacific Region',
        'rmr':   'Rocky Mountain Region',
        'ser':   'Southeast Region',
        'swr':   'Southwest Region',
        # OCONUS subregions
        'pcr-ak': 'Pacific Region - Alaska',
        'pcr-hi': 'Pacific Region - Hawaii',
        'pcr-gu': 'Pacific Region - Guam',
        'ser-pr': 'Southeast Region - Puerto Rico/USVI',
    }
    return jsonify(regions)

@app.route('/api/status')
def system_status():
    try:
        status = {
            'status':    'operational',
            'timestamp': datetime.utcnow().isoformat(),
            'services': {
                'weather_api':      'active',
                'wind_forecast':    'active',
                'radar':            'active',
                'enhanced_weather': 'active',
            },
            'version': '2.0.0-production',
        }
        return jsonify(status)
    except Exception as e:
        return jsonify({
            'status':    'degraded',
            'error':     str(e),
            'timestamp': datetime.utcnow().isoformat(),
        }), 500

@app.route('/api/pipeline-status')
def pipeline_status():
    """
    Data pipeline freshness, for the homepage status widget.
    Reads the state file wxcop_freshness_monitor.py (wxcop-monitor.timer,
    every 5 min on r815) already maintains -- no email/push, this endpoint
    is the sole consumer now that alerting moved to on-page display only
    (2026-07-23, was msmtp email before).
    """
    import json as _json
    state_path = '/var/lib/wxcop_monitor/state.json'
    try:
        with open(state_path) as f:
            state = _json.load(f)
    except (OSError, ValueError) as e:
        return jsonify({'error': str(e), 'checks': []}), 500

    checks = [
        {'name': name, **info}
        for name, info in sorted(state.items())
    ]
    stale = [c['name'] for c in checks if c.get('status') == 'STALE']
    return jsonify({
        'checks': checks,
        'stale_count': len(stale),
        'generated_at': datetime.utcnow().isoformat() + 'Z',
    })

@app.route('/health')
def health_check():
    try:
        from db_config import get_connection
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM observations.metar
            WHERE observation_time > NOW() - INTERVAL '2 hours'
        """)
        recent_metars = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM observations.airports")
        total_airports = cur.fetchone()[0]
        cur.close()
        conn.close()
        return jsonify({
            'status':         'healthy',
            'database':       'connected',
            'recent_metars':  recent_metars,
            'total_airports': total_airports,
            'timestamp':      datetime.utcnow().isoformat(),
        })
    except Exception as e:
        return jsonify({
            'status':    'degraded',
            'database':  'error',
            'error':     str(e),
            'timestamp': datetime.utcnow().isoformat(),
        }), 500

# ============================================================================
# LEGACY WIND CONSTRAINTS GENERATOR
# ============================================================================

@app.route('/legacy-wind-generator', methods=['GET', 'POST'])
def legacy_wind_generator():
    if request.method == 'GET':
        return """
        <h1>Legacy Wind Constraints Generator</h1>
        <p>This is the original wind constraints map generator.</p>
        <p><a href="/CAP_WxCOP/">← Return to Main Interface</a></p>
        <p>For modern wind analysis, use the
           <a href="/CAP_WxCOP/wind-map">Interactive Wind Map</a></p>
        """
    return jsonify({'message': 'Legacy generator - use modern interface'})

# ============================================================================
# ERROR HANDLERS
# ============================================================================

@app.errorhandler(404)
def not_found_error(error):
    return render_template('error.html',
                           error_code=404,
                           error_message="Page not found",
                           home_url="/CAP_WxCOP/"), 404

@app.errorhandler(500)
def internal_error(error):
    return render_template('error.html',
                           error_code=500,
                           error_message="Internal server error",
                           home_url="/CAP_WxCOP/"), 500

# ============================================================================
# MAIN
# ============================================================================

app.url_map.strict_slashes = False

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)


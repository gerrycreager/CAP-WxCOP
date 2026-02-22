#!/var/www/cap_winds_app/venv/bin/python3
"""
CAP Winds Flask Application - FINAL VERSION
Optimized for WindAnalysisService

Deploy:
  cp app_final.py /var/www/cap_winds_app/app.py
  systemctl restart apache2
"""

import os
import sys
from datetime import datetime, timedelta
from flask import Flask, render_template, request, send_from_directory, jsonify

sys.path.insert(0, '/var/www/cap_winds_app')

# Initialize Flask app
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

# Configuration
BATCH_MAP_DIR = "/var/www/html/cap_winds"
SHAPEFILE_DIR = "/var/www/html/cap_winds_shp"
MAX_BATCH_MAP_AGE_HOURS = 2

# Batch map files (static names)
BATCH_MAP_FILES = {
    'CONUS': 'conus.png',
    'NCR': 'ncr.png',
    'GLR': 'glr.png',
    'NER': 'ner.png',
    'PCR': 'pcr.png',
    'PCR-WEST': 'pcr-west.png',
    'PCR-AK': 'pcr-ak.png',
    'PCR-HI': 'pcr-hi.png',
    'PCR-GUAM': 'pcr-guam.png',
    'RMR': 'rmr.png',
    'SER': 'ser.png',
    'SER-CARIB': 'ser-carib.png',
    'SWR': 'swr.png',
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

STATE_CODES = [
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA',
    'HI', 'ID', 'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD',
    'MA', 'MI', 'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH', 'NJ',
    'NM', 'NY', 'NC', 'ND', 'OH', 'OK', 'OR', 'PA', 'RI', 'SC',
    'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV', 'WI', 'WY'
]

# Import states_service
try:
    import states_service
    app.logger.info("✓ Successfully imported states_service module")
    STATES_SERVICE_AVAILABLE = True
except Exception as e:
    app.logger.error(f"✗ Failed to import states_service: {e}")
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
        # Create service instance
        service = states_service.WindAnalysisService()
        
        # Determine parameters
        if selection_type == 'state':
            location = form_data.get('state_code')
            radius = None
        elif selection_type == 'region':
            location = form_data.get('region_code')
            radius = None
        elif selection_type == 'radius':
            center_point = form_data.get('center_point')
            center_lat = form_data.get('center_lat')
            center_lon = form_data.get('center_lon')
            
            if center_lat and center_lon:
                location = (float(center_lat), float(center_lon))
            else:
                location = center_point
            radius = 50
        elif selection_type == 'conus':
            location = 'CONUS'
            radius = None
        else:
            raise ValueError(f"Unknown selection type: {selection_type}")
        
        app.logger.info(f"Generating on-demand: {location}")
        
        # Call generate_analysis
        result = service.generate_analysis(
            location_or_coords=location,
            radius_nm=radius,
            output_path=BATCH_MAP_DIR,
            model="auto",
            output_format="png",
            include_shapefiles=True
        )
        
        # Process result
        if result and 'maps' in result and result['maps']:
            maps = []
            for map_path in result['maps']:
                map_filename = os.path.basename(map_path)
                maps.append({
                    'url': f'/cap_winds/{map_filename}',
                    'map_name': result.get('map_name', 'Wind Analysis'),
                    'is_batch': False,
                    'airport_shp_url': result.get('airport_shp_url'),
                    'contours_poly_url': result.get('contours_poly_url'),
                    'contours_line_url': result.get('contours_line_url'),
                })
            
            app.logger.info(f"✓ Generated {len(maps)} map(s)")
            return maps
        else:
            app.logger.error("No maps generated")
            return None
            
    except Exception as e:
        app.logger.error(f"Generation error: {e}", exc_info=True)
        return None


@app.route('/', methods=['GET', 'POST'])
def index():
    """Main page"""
    if request.method == 'GET':
        return render_template('index.html', 
                             state_codes=STATE_CODES,
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
                         state_codes=STATE_CODES,
                         selection_type=selection_type,
                         maps=maps,
                         error=error)


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
    return jsonify({'status': 'healthy'}), 200


@app.errorhandler(404)
def not_found(e):
    return render_template('index.html',
                         state_codes=STATE_CODES,
                         maps=None,
                         error="Page not found"), 404


@app.errorhandler(500)
def server_error(e):
    app.logger.error(f"Server error: {e}", exc_info=True)
    return render_template('index.html',
                         state_codes=STATE_CODES,
                         maps=None,
                         error="Server error"), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)



#!/var/www/cap_winds_app/venv/bin/python3
"""
CAP Winds Flask Application - COMPLETE VERSION
Serves aviation wind constraint maps with batch map optimization

Features:
- Checks pre-generated batch maps first (< 1 second)
- Falls back to on-demand generation for states/radius queries
- Serves static maps, shapefiles, and data files

Deploy:
  cp app.py /var/www/cap_winds_app/app.py
  systemctl restart apache2  # or your web server

For Apache with mod_wsgi, also create app.wsgi:
  See deployment instructions at end of file
"""

import os
import sys
from datetime import datetime, timedelta
from flask import Flask, render_template, request, send_from_directory, jsonify

# Add app directory to path
sys.path.insert(0, '/var/www/cap_winds_app')

# Import states service
try:
    from states_service import StatesService
except ImportError as e:
    print(f"ERROR: Cannot import StatesService: {e}")
    print("Make sure states_service.py is in /var/www/cap_winds_app/")
    sys.exit(1)

# Initialize Flask app
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max upload

# Configuration
BATCH_MAP_DIR = "/var/www/html/cap_winds"
SHAPEFILE_DIR = "/var/www/html/cap_winds_shp"
MAX_BATCH_MAP_AGE_HOURS = 2  # Serve batch maps up to 2 hours old

# Map region codes to batch-generated filenames
BATCH_MAP_FILES = {
    # CONUS
    'CONUS': 'conus.png',
    
    # Full regions
    'NCR': 'ncr.png',
    'GLR': 'glr.png',
    'NER': 'ner.png',
    'PCR': 'pcr.png',
    'RMR': 'rmr.png',
    'SER': 'ser.png',
    'SWR': 'swr.png',
    
    # Sub-regions (Pacific)
    'PCR-WEST': 'pcr-west.png',
    'PCR-AK': 'pcr-ak.png',
    'PCR-HI': 'pcr-hi.png',
    'PCR-GUAM': 'pcr-guam.png',
    
    # Sub-regions (Southeast)
    'SER-CARIB': 'ser-carib.png',
}

# Region display names
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

# US State codes for dropdown
STATE_CODES = [
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA',
    'HI', 'ID', 'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD',
    'MA', 'MI', 'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH', 'NJ',
    'NM', 'NY', 'NC', 'ND', 'OH', 'OK', 'OR', 'PA', 'RI', 'SC',
    'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV', 'WI', 'WY'
]


def get_batch_map(location_code, max_age_hours=MAX_BATCH_MAP_AGE_HOURS):
    """
    Check if a pre-generated batch map exists and is recent enough
    
    Args:
        location_code: Region code (e.g., 'CONUS', 'SWR', 'PCR-AK')
        max_age_hours: Maximum age in hours before map is considered stale
    
    Returns:
        dict with map info if found and recent, None otherwise
    """
    # Normalize location code
    location_upper = location_code.upper()
    
    # Check if this location has a batch map
    if location_upper not in BATCH_MAP_FILES:
        return None
    
    map_filename = BATCH_MAP_FILES[location_upper]
    map_path = os.path.join(BATCH_MAP_DIR, map_filename)
    
    # Check if file exists
    if not os.path.exists(map_path):
        app.logger.debug(f"Batch map not found: {map_path}")
        return None
    
    # Check file age
    try:
        file_mtime = datetime.fromtimestamp(os.path.getmtime(map_path))
        age = datetime.now() - file_mtime
        
        if age > timedelta(hours=max_age_hours):
            app.logger.info(f"Batch map too old ({age.total_seconds()/3600:.1f} hours): {map_filename}")
            return None
        
        # Map is recent! Return it
        app.logger.info(f"Serving batch map: {map_filename} (age: {int(age.total_seconds()/60)} minutes)")
        
        return {
            'url': f'/cap_winds/{map_filename}',
            'map_name': REGION_NAMES.get(location_upper, location_upper),
            'generated_at': file_mtime.strftime('%Y-%m-%d %H:%M UTC'),
            'age_minutes': int(age.total_seconds() / 60),
            'is_batch': True,
            # Note: Batch maps don't have individual shapefile URLs in simple overwrite mode
            # Shapefiles are in cap_winds_shp/ with various timestamped names
            'airport_shp_url': None,
            'contours_poly_url': None,
            'contours_line_url': None,
        }
        
    except Exception as e:
        app.logger.error(f"Error checking batch map: {e}")
        return None


def generate_map_on_demand(selection_type, form_data):
    """
    Generate a map on-demand using states_service
    
    Args:
        selection_type: Type of selection ('state', 'region', 'radius', 'conus')
        form_data: Form data from request
    
    Returns:
        list of map dicts, or None on error
    """
    try:
        service = StatesService()
        
        # Determine location and parameters based on selection type
        if selection_type == 'state':
            state_code = form_data.get('state_code')
            app.logger.info(f"Generating on-demand map for state: {state_code}")
            
            result = service.generate_analysis(
                location_or_coords=state_code,
                radius_nm=None,
                output_path=BATCH_MAP_DIR,
                model="auto",
                output_format="png",
                include_shapefiles=True
            )
            
        elif selection_type == 'region':
            region_code = form_data.get('region_code')
            app.logger.info(f"Generating on-demand map for region: {region_code}")
            
            result = service.generate_analysis(
                location_or_coords=region_code,
                radius_nm=None,
                output_path=BATCH_MAP_DIR,
                model="auto",
                output_format="png",
                include_shapefiles=True
            )
            
        elif selection_type == 'radius':
            center_point = form_data.get('center_point')
            center_lat = form_data.get('center_lat')
            center_lon = form_data.get('center_lon')
            
            # Use coordinates if provided, otherwise use airport code
            if center_lat and center_lon:
                location = (float(center_lat), float(center_lon))
                app.logger.info(f"Generating on-demand 50nm map for coordinates: {location}")
            else:
                location = center_point
                app.logger.info(f"Generating on-demand 50nm map for: {center_point}")
            
            result = service.generate_analysis(
                location_or_coords=location,
                radius_nm=50,
                output_path=BATCH_MAP_DIR,
                model="auto",
                output_format="png",
                include_shapefiles=True
            )
            
        elif selection_type == 'conus':
            app.logger.info("Generating on-demand CONUS map (batch map not available)")
            
            result = service.generate_analysis(
                location_or_coords='CONUS',
                radius_nm=None,
                output_path=BATCH_MAP_DIR,
                model="auto",
                output_format="png",
                include_shapefiles=True
            )
        
        else:
            app.logger.error(f"Unknown selection type: {selection_type}")
            return None
        
        # Process result and create map entries
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
            
            app.logger.info(f"Successfully generated {len(maps)} map(s)")
            return maps
        else:
            app.logger.error("Map generation returned no maps")
            return None
            
    except Exception as e:
        app.logger.error(f"Error generating map: {e}", exc_info=True)
        return None


@app.route('/', methods=['GET', 'POST'])
def index():
    """Main page - serves form and handles map generation requests"""
    
    if request.method == 'GET':
        # Show form
        return render_template('index.html', 
                             state_codes=STATE_CODES,
                             selection_type='conus',
                             maps=None,
                             error=None)
    
    # POST - user requested a map
    selection_type = request.form.get('selection_type', 'conus')
    primary_airport = request.form.get('primary_airport', '').strip()
    
    maps = []
    error = None
    
    try:
        # STEP 1: Check if this is a batch-generated region
        batch_map = None
        
        if selection_type == 'conus':
            batch_map = get_batch_map('CONUS')
            
        elif selection_type == 'region':
            region_code = request.form.get('region_code')
            batch_map = get_batch_map(region_code)
        
        # STEP 2: Serve batch map if available, otherwise generate on-demand
        if batch_map:
            # Use pre-generated batch map (FAST!)
            maps = [batch_map]
            app.logger.info(f"Served batch map in < 1 second")
            
        else:
            # No batch map available - generate on-demand (SLOW)
            maps = generate_map_on_demand(selection_type, request.form)
            
            if not maps:
                error = "Failed to generate map. Please try again or select a different region."
                app.logger.error(f"Map generation failed for {selection_type}")
        
    except Exception as e:
        error = f"Error processing request: {str(e)}"
        app.logger.error(f"Error in index route: {e}", exc_info=True)
    
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
    """API endpoint - list available batch-generated regions"""
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
    
    return jsonify({
        'regions': regions,
        'count': len(regions),
        'max_age_hours': MAX_BATCH_MAP_AGE_HOURS
    })


@app.route('/api/status')
def api_status():
    """API endpoint - system status"""
    batch_maps_ok = 0
    batch_maps_stale = 0
    batch_maps_missing = 0
    
    for code, filename in BATCH_MAP_FILES.items():
        map_path = os.path.join(BATCH_MAP_DIR, filename)
        
        if os.path.exists(map_path):
            file_mtime = datetime.fromtimestamp(os.path.getmtime(map_path))
            age = datetime.now() - file_mtime
            
            if age < timedelta(hours=MAX_BATCH_MAP_AGE_HOURS):
                batch_maps_ok += 1
            else:
                batch_maps_stale += 1
        else:
            batch_maps_missing += 1
    
    return jsonify({
        'status': 'operational' if batch_maps_ok > 10 else 'degraded',
        'batch_maps': {
            'current': batch_maps_ok,
            'stale': batch_maps_stale,
            'missing': batch_maps_missing,
            'total': len(BATCH_MAP_FILES)
        },
        'timestamp': datetime.utcnow().isoformat()
    })


@app.route('/health')
def health():
    """Health check endpoint for monitoring"""
    return jsonify({'status': 'healthy'}), 200


@app.errorhandler(404)
def not_found(e):
    """404 error handler"""
    return render_template('index.html',
                         state_codes=STATE_CODES,
                         selection_type='conus',
                         maps=None,
                         error="Page not found"), 404


@app.errorhandler(500)
def server_error(e):
    """500 error handler"""
    app.logger.error(f"Server error: {e}", exc_info=True)
    return render_template('index.html',
                         state_codes=STATE_CODES,
                         selection_type='conus',
                         maps=None,
                         error="Internal server error. Please try again."), 500


if __name__ == '__main__':
    # Development server
    app.run(host='0.0.0.0', port=5000, debug=True)


"""
=============================================================================
DEPLOYMENT INSTRUCTIONS
=============================================================================

1. Copy this file to your server:
   scp app.py user@server:/var/www/cap_winds_app/

2. Create WSGI file for Apache (app.wsgi):
   
   #!/var/www/cap_winds_app/venv/bin/python3
   import sys
   import logging
   
   # Set up logging
   logging.basicConfig(stream=sys.stderr, level=logging.INFO)
   
   # Add application directory to path
   sys.path.insert(0, '/var/www/cap_winds_app')
   
   # Import application
   from app import app as application
   
   # Log startup
   application.logger.info('CAP Winds Flask application started')

3. Apache virtual host configuration:
   
   <VirtualHost *:80>
       ServerName your-domain.com
       
       WSGIDaemonProcess cap_winds user=www-data group=www-data threads=5 \
           python-home=/var/www/cap_winds_app/venv \
           python-path=/var/www/cap_winds_app
       WSGIScriptAlias / /var/www/cap_winds_app/app.wsgi
       
       <Directory /var/www/cap_winds_app>
           WSGIProcessGroup cap_winds
           WSGIApplicationGroup %{GLOBAL}
           Require all granted
       </Directory>
       
       # Serve static files directly
       Alias /cap_winds /var/www/html/cap_winds
       Alias /cap_winds_shp /var/www/html/cap_winds_shp
       
       <Directory /var/www/html/cap_winds>
           Require all granted
       </Directory>
       
       <Directory /var/www/html/cap_winds_shp>
           Require all granted
       </Directory>
       
       ErrorLog ${APACHE_LOG_DIR}/cap_winds_error.log
       CustomLog ${APACHE_LOG_DIR}/cap_winds_access.log combined
   </VirtualHost>

4. Restart Apache:
   systemctl restart apache2

5. Test:
   curl http://localhost/
   curl http://localhost/api/status
   curl http://localhost/api/regions

=============================================================================
PERFORMANCE NOTES
=============================================================================

With batch map checking enabled:

BEFORE:
- CONUS request: 4-5 minutes (always generated)
- Regional request: 1-3 minutes (always generated)
- State request: 1-2 minutes (always generated)

AFTER:
- CONUS request: < 1 second (uses batch map)
- Regional request: < 1 second (uses batch map)
- State request: 1-2 minutes (on-demand generation)
- Radius request: 1-2 minutes (on-demand generation)

Expected improvement: 200-300x faster for 80-90% of requests

=============================================================================
"""

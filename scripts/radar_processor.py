"""
NEXRAD Level III Radar Processor using PyART
Converts NIDS files to georeferenced PNGs for web display

Features:
- PyART Level III reader
- Geographic projection (Plate Carrée for Leaflet)
- Transparent PNG generation
- Metadata (bounds) for Leaflet ImageOverlay
- Optimized for web display
"""

import os
import json
import numpy as np
from datetime import datetime
from pathlib import Path
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Import PyART
try:
    import pyart
    PYART_AVAILABLE = True
except ImportError:
    logger.error("PyART not installed. Install with: pip install arm-pyart")
    PYART_AVAILABLE = False

# Import plotting libraries
try:
    import matplotlib
    matplotlib.use('Agg')  # Non-interactive backend
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    logger.error("Matplotlib not installed")
    MATPLOTLIB_AVAILABLE = False

# Geographic projection
try:
    import cartopy.crs as ccrs
    CARTOPY_AVAILABLE = True
except ImportError:
    logger.warning("Cartopy not installed - using simple projection")
    CARTOPY_AVAILABLE = False


# Product-specific settings
PRODUCT_CONFIG = {
    'N0Q': {
        'name': 'Base Reflectivity',
        'field': 'reflectivity',
        'cmap': 'pyart_NWSRef',
        'vmin': -20,
        'vmax': 75,
        'units': 'dBZ'
    },
    'N0C': {
        'name': 'Correlation Coefficient',
        'field': 'cross_correlation_ratio',
        'cmap': 'pyart_RefDiff',
        'vmin': 0.5,
        'vmax': 1.0,
        'units': ''
    },
    'N0V': {
        'name': 'Base Velocity',
        'field': 'velocity',
        'cmap': 'pyart_BuDRd18',
        'vmin': -50,
        'vmax': 50,
        'units': 'knots'
    },
    'N0S': {
        'name': 'Storm Relative Velocity',
        'field': 'velocity',
        'cmap': 'pyart_BuDRd18',
        'vmin': -50,
        'vmax': 50,
        'units': 'knots'
    },
    'N0X': {
        'name': 'Differential Reflectivity',
        'field': 'differential_reflectivity',
        'cmap': 'pyart_RefDiff',
        'vmin': -5,
        'vmax': 5,
        'units': 'dB'
    },
    'N0K': {
        'name': 'Specific Differential Phase',
        'field': 'specific_differential_phase',
        'cmap': 'pyart_Theodore16',
        'vmin': -2,
        'vmax': 6,
        'units': 'deg/km'
    },
    'N1P': {
        'name': 'One-Hour Precipitation',
        'field': 'radar_estimated_rain_rate',
        'cmap': 'pyart_RRate11',
        'vmin': 0,
        'vmax': 5,
        'units': 'in'
    },
    'NTP': {
        'name': 'Storm Total Precipitation',
        'field': 'radar_estimated_rain_rate',
        'cmap': 'pyart_RRate11',
        'vmin': 0,
        'vmax': 10,
        'units': 'in'
    },
    'NCR': {
        'name': 'Composite Reflectivity',
        'field': 'reflectivity',
        'cmap': 'pyart_NWSRef',
        'vmin': -20,
        'vmax': 75,
        'units': 'dBZ'
    },
    'DHR': {
        'name': 'Digital Hybrid Reflectivity',
        'field': 'reflectivity',
        'cmap': 'pyart_NWSRef',
        'vmin': -20,
        'vmax': 75,
        'units': 'dBZ'
    },
    'EET': {
        'name': 'Enhanced Echo Tops',
        'field': 'echo_top',
        'cmap': 'pyart_StepSeq25',
        'vmin': 0,
        'vmax': 70,
        'units': 'kft'
    }
}


def check_dependencies():
    """Check if required libraries are available"""
    if not PYART_AVAILABLE:
        raise ImportError("PyART is required. Install with: pip install arm-pyart")
    if not MATPLOTLIB_AVAILABLE:
        raise ImportError("Matplotlib is required. Install with: pip install matplotlib")
    return True


def get_output_paths(nids_file, site_id, product):
    """
    Generate output paths for georeferenced PNG and metadata
    
    Args:
        nids_file: Path to input .nids file
        site_id: 4-letter site identifier
        product: Product code (e.g., N0Q)
        
    Returns:
        tuple: (png_path, json_path, output_dir)
    """
    # Parse input path: /LDM/radar/level3/{SITE}/{PRODUCT}/nids/YYYYMMDD/{SITE}_{PRODUCT}_HHMMSS.nids
    nids_path = Path(nids_file)
    
    # Get date directory and filename
    date_dir = nids_path.parent.name  # YYYYMMDD
    filename = nids_path.stem  # SITE_PRODUCT_HHMMSS
    
    # Build output directory: /LDM/radar/level3/{SITE}/{PRODUCT}/geo/YYYYMMDD/
    # nids_path structure: /LDM/radar/level3/SITE/PRODUCT/nids/YYYYMMDD/FILE.nids
    # parents[0] = YYYYMMDD, parents[1] = nids, parents[2] = PRODUCT directory
    base_dir = nids_path.parents[2]  # /LDM/radar/level3/SITE/PRODUCT
    output_dir = base_dir / 'geo' / date_dir
    
    # Create output directory if needed
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Output files
    png_path = output_dir / f"{filename}.png"
    json_path = output_dir / f"{filename}.json"
    
    return str(png_path), str(json_path), str(output_dir)


def read_radar_file(nids_file, product):
    """
    Read NEXRAD Level III file using PyART
    
    Args:
        nids_file: Path to .nids file
        product: Product code
        
    Returns:
        PyART radar object or None
    """
    try:
        radar = pyart.io.read_nexrad_level3(nids_file)
        logger.info(f"Successfully read {nids_file}")
        return radar
    except Exception as e:
        logger.error(f"Error reading {nids_file}: {e}")
        return None


def calculate_geographic_bounds(radar_lat, radar_lon, extent_degrees=2.5):
    """
    Calculate geographic bounds for radar imagery
    
    Args:
        radar_lat: Radar latitude
        radar_lon: Radar longitude
        extent_degrees: Extent in degrees (~150 nm for 2.5 degrees)
        
    Returns:
        tuple: (south, north, west, east)
    """
    south = radar_lat - extent_degrees
    north = radar_lat + extent_degrees
    west = radar_lon - extent_degrees
    east = radar_lon + extent_degrees
    
    # Ensure bounds stay within valid lat/lon ranges
    south = max(south, -90)
    north = min(north, 90)
    west = max(west, -180)
    east = min(east, 180)
    
    return south, north, west, east


def create_georeferenced_png(radar, product, output_png, site_lat, site_lon, 
                            image_size=800, extent_degrees=2.5):
    """
    Create georeferenced PNG from radar data
    
    Args:
        radar: PyART radar object
        product: Product code
        output_png: Output PNG file path
        site_lat: Radar site latitude
        site_lon: Radar site longitude
        image_size: Output image size in pixels
        extent_degrees: Geographic extent in degrees
        
    Returns:
        dict: Metadata with bounds and product info
    """
    # Get product configuration
    config = PRODUCT_CONFIG.get(product, PRODUCT_CONFIG['N0Q'])
    
    # Calculate geographic bounds
    south, north, west, east = calculate_geographic_bounds(site_lat, site_lon, extent_degrees)
    
    # Create figure with transparent background
    fig = plt.figure(figsize=(8, 8), dpi=100)
    ax = fig.add_subplot(111)
    
    # Get the first sweep (lowest elevation angle)
    try:
        # Try to get the field from radar
        field_name = config['field']
        
        # Check available fields
        available_fields = list(radar.fields.keys())
        logger.info(f"Available fields: {available_fields}")
        
        # Find matching field
        if field_name not in available_fields:
            # Try common alternatives
            alternatives = {
                'reflectivity': ['REF', 'DZ', 'DBZ', 'reflectivity'],
                'velocity': ['VEL', 'V', 'velocity'],
                'cross_correlation_ratio': ['RHO', 'RHOHV', 'cross_correlation_ratio'],
                'differential_reflectivity': ['ZDR', 'differential_reflectivity'],
                'specific_differential_phase': ['KDP', 'specific_differential_phase']
            }
            
            for alt in alternatives.get(field_name, []):
                if alt in available_fields:
                    field_name = alt
                    break
        
        if field_name not in radar.fields:
            logger.error(f"Field {field_name} not found in radar data")
            return None
        
        # Get data
        data = radar.fields[field_name]['data']
        
        # Get azimuth and range
        azimuth = radar.azimuth['data']
        range_km = radar.range['data'] / 1000.0  # Convert to km
        
        # Create coordinate grids
        az_rad = np.deg2rad(azimuth)
        r, th = np.meshgrid(range_km, az_rad)
        
        # Convert to Cartesian (relative to radar)
        x = r * np.sin(th)
        y = r * np.cos(th)
        
        # Convert to geographic coordinates
        # Approximate: 1 degree latitude ≈ 111 km, longitude varies with latitude
        km_per_deg_lat = 111.0
        km_per_deg_lon = 111.0 * np.cos(np.deg2rad(site_lat))
        
        lon_offset = x / km_per_deg_lon
        lat_offset = y / km_per_deg_lat
        
        lons = site_lon + lon_offset
        lats = site_lat + lat_offset
        
        # Get colormap
        try:
            cmap = pyart.graph.cm.get_colormap(config['cmap'])
        except:
            cmap = plt.get_cmap('viridis')
        
        # Plot data
        mesh = ax.pcolormesh(lons, lats, data, 
                           cmap=cmap,
                           vmin=config['vmin'],
                           vmax=config['vmax'],
                           shading='auto',
                           alpha=0.7)
        
        # Set extent
        ax.set_xlim(west, east)
        ax.set_ylim(south, north)
        
        # Remove axes
        ax.set_xticks([])
        ax.set_yticks([])
        ax.set_frame_on(False)
        
        # Make background transparent
        fig.patch.set_alpha(0.0)
        ax.patch.set_alpha(0.0)
        
        # Save with tight layout and transparency
        plt.savefig(output_png, 
                   bbox_inches='tight',
                   pad_inches=0,
                   transparent=True,
                   dpi=100)
        plt.close(fig)
        
        # Create metadata
        metadata = {
            'product': product,
            'product_name': config['name'],
            'site_lat': float(site_lat),
            'site_lon': float(site_lon),
            'bounds': {
                'south': float(south),
                'north': float(north),
                'west': float(west),
                'east': float(east)
            },
            'extent_degrees': extent_degrees,
            'image_size': image_size,
            'timestamp': datetime.utcnow().isoformat() + 'Z',
            'units': config['units'],
            'vmin': config['vmin'],
            'vmax': config['vmax']
        }
        
        logger.info(f"Created georeferenced PNG: {output_png}")
        return metadata
        
    except Exception as e:
        logger.error(f"Error creating PNG: {e}")
        import traceback
        traceback.print_exc()
        plt.close(fig)
        return None


def process_radar_image(site_id, product, nids_file, site_lat, site_lon, force=False):
    """
    Process a NEXRAD Level III file to georeferenced PNG
    
    Args:
        site_id: 4-letter radar site identifier
        product: Product code (e.g., N0Q)
        nids_file: Path to input .nids file
        site_lat: Radar site latitude
        site_lon: Radar site longitude
        force: Force reprocessing even if cached
        
    Returns:
        tuple: (png_path, metadata) or (None, None) on error
    """
    # Check dependencies
    try:
        check_dependencies()
    except ImportError as e:
        logger.error(str(e))
        return None, None
    
    # Get output paths
    png_path, json_path, output_dir = get_output_paths(nids_file, site_id, product)
    
    # Check if already processed (unless force=True)
    if not force and os.path.exists(png_path) and os.path.exists(json_path):
        # Check if PNG is newer than NIDS file
        nids_mtime = os.path.getmtime(nids_file)
        png_mtime = os.path.getmtime(png_path)
        
        if png_mtime > nids_mtime:
            logger.info(f"Using cached PNG: {png_path}")
            # Load and return metadata
            with open(json_path, 'r') as f:
                metadata = json.load(f)
            return png_path, metadata
    
    # Read radar file
    radar = read_radar_file(nids_file, product)
    if radar is None:
        return None, None
    
    # Create georeferenced PNG
    metadata = create_georeferenced_png(
        radar, product, png_path, 
        site_lat, site_lon,
        image_size=800,
        extent_degrees=2.5
    )
    
    if metadata is None:
        return None, None
    
    # Save metadata
    with open(json_path, 'w') as f:
        json.dump(metadata, f, indent=2)
    
    logger.info(f"Saved metadata: {json_path}")
    
    return png_path, metadata


def batch_process_site(site_id, product, site_lat, site_lon, hours=2):
    """
    Batch process recent radar files for a site
    
    Args:
        site_id: 4-letter radar site identifier
        product: Product code
        site_lat: Radar site latitude
        site_lon: Radar site longitude
        hours: Hours of data to process
        
    Returns:
        list: Processed PNG paths
    """
    import glob
    from datetime import datetime, timedelta
    
    # Find NIDS files
    base_path = f"/LDM/radar/level3/{site_id}/{product}/nids"
    
    if not os.path.exists(base_path):
        logger.error(f"Path not found: {base_path}")
        return []
    
    # Get recent files
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    nids_files = []
    
    for date_dir in sorted(os.listdir(base_path), reverse=True):
        date_path = os.path.join(base_path, date_dir)
        if not os.path.isdir(date_path):
            continue
        
        for filename in sorted(os.listdir(date_path), reverse=True):
            if not filename.endswith('.nids'):
                continue
            
            filepath = os.path.join(date_path, filename)
            mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
            
            if mtime >= cutoff:
                nids_files.append(filepath)
    
    logger.info(f"Found {len(nids_files)} NIDS files for {site_id} {product}")
    
    # Process each file
    processed = []
    for nids_file in nids_files:
        png_path, metadata = process_radar_image(
            site_id, product, nids_file, site_lat, site_lon, force=False
        )
        if png_path:
            processed.append(png_path)
    
    logger.info(f"Processed {len(processed)} files")
    return processed


if __name__ == '__main__':
    # Command-line interface for PQACT
    import sys
    
    if len(sys.argv) < 4:
        print("Usage: python radar_processor.py SITE PRODUCT NIDS_FILE")
        print("Example: python radar_processor.py HGX N0Q /LDM/radar/level3/HGX/N0Q/nids/20260121/HGX_N0Q_120000.nids")
        sys.exit(1)
    
    site = sys.argv[1].upper()
    product = sys.argv[2].upper()
    nids_file = sys.argv[3]
    
    # Look up site coordinates from radar_sites
    try:
        # Try importing from same directory first (scripts/)
        try:
            from radar_sites import get_site_info
        except ImportError:
            # Try importing from scripts package
            from scripts.radar_sites import get_site_info
        
        site_info = get_site_info(site)
        if not site_info:
            logger.error(f"Site {site} not found in database")
            sys.exit(1)
        
        lat = site_info['lat']
        lon = site_info['lon']
        logger.info(f"Processing {site} {product}: {lat}, {lon}")
        
    except ImportError as e:
        logger.error(f"Could not import radar_sites module: {e}")
        sys.exit(1)
    
    # Process the file
    png, meta = process_radar_image(site, product, nids_file, lat, lon, force=True)
    if png:
        logger.info(f"✓ Created: {png}")
        sys.exit(0)
    else:
        logger.error("✗ Failed to process")
        sys.exit(1)


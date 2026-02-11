#!/var/www/cap_winds_app/venv/bin/python3
"""
CAP Winds Static Map Generator - Database Version
Uses processed data from observations.model_wind_forecasts instead of reprocessing GRIB2 files

Key improvements:
1. Uses database data (no GRIB2 reprocessing)
2. "Wing" nomenclature instead of "State"
3. Puerto Rico Wing includes PR + USVI
4. Single comprehensive shapefile
5. Proper CONUS map bounds
6. Hourly map generation
"""

import sys
sys.path.insert(0, '/var/www/cap_winds_app')

import os
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import cartopy.crs as ccrs
import cartopy.feature as cfeature
import matplotlib.patheffects as path_effects

import geopandas as gpd
from shapely.geometry import Point
import fiona
from fiona.crs import from_epsg

from db_config import get_connection

# =====================================================================
# Configuration
# =====================================================================

class MapConfig:
    """Configuration for static map generation"""
    
    # Output directories
    WEB_OUTPUT_DIR = '/var/www/html/cap_winds'
    SHAPE_OUTPUT_DIR = '/var/www/html/cap_winds_shp'
    
    # Wind thresholds (CAPR 70-1)
    MAX_OPERATIONAL_WIND = 30  # kts
    CAUTION_WIND = 16  # kts (changed from 20 to match database)
    
    # Wing (State) boundaries
    WING_BOUNDARIES = {
        # CONUS Wings
        'AL': {'name': 'Alabama Wing', 'bounds': [-88.5, -84.8, 30.1, 35.1], 'region': 'SER'},
        'AZ': {'name': 'Arizona Wing', 'bounds': [-114.8, -109.0, 31.3, 37.1], 'region': 'SWR'},
        'AR': {'name': 'Arkansas Wing', 'bounds': [-94.6, -89.6, 33.0, 36.5], 'region': 'SWR'},
        'CA': {'name': 'California Wing', 'bounds': [-124.5, -114.1, 32.5, 42.1], 'region': 'PCR'},
        'CO': {'name': 'Colorado Wing', 'bounds': [-109.1, -102.0, 36.9, 41.1], 'region': 'RMR'},
        'CT': {'name': 'Connecticut Wing', 'bounds': [-73.8, -71.8, 40.9, 42.1], 'region': 'NER'},
        'DE': {'name': 'Delaware Wing', 'bounds': [-75.8, -75.0, 38.4, 39.9], 'region': 'MAR'},
        'FL': {'name': 'Florida Wing', 'bounds': [-87.7, -80.0, 24.4, 31.1], 'region': 'SER'},
        'GA': {'name': 'Georgia Wing', 'bounds': [-85.6, -80.8, 30.3, 35.1], 'region': 'SER'},
        'ID': {'name': 'Idaho Wing', 'bounds': [-117.3, -111.0, 41.9, 49.1], 'region': 'RMR'},
        'IL': {'name': 'Illinois Wing', 'bounds': [-91.5, -87.5, 36.9, 42.6], 'region': 'GLR'},
        'IN': {'name': 'Indiana Wing', 'bounds': [-88.1, -84.8, 37.7, 41.8], 'region': 'GLR'},
        'IA': {'name': 'Iowa Wing', 'bounds': [-96.7, -90.1, 40.3, 43.6], 'region': 'NCR'},
        'KS': {'name': 'Kansas Wing', 'bounds': [-102.1, -94.6, 36.9, 40.1], 'region': 'NCR'},
        'KY': {'name': 'Kentucky Wing', 'bounds': [-89.6, -81.9, 36.5, 39.2], 'region': 'GLR'},
        'LA': {'name': 'Louisiana Wing', 'bounds': [-94.1, -88.8, 28.9, 33.1], 'region': 'SWR'},
        'ME': {'name': 'Maine Wing', 'bounds': [-71.1, -66.9, 43.0, 47.5], 'region': 'NER'},
        'MD': {'name': 'Maryland Wing', 'bounds': [-79.5, -75.0, 37.9, 39.8], 'region': 'MAR'},
        'MA': {'name': 'Massachusetts Wing', 'bounds': [-73.5, -69.9, 41.2, 42.9], 'region': 'NER'},
        'MI': {'name': 'Michigan Wing', 'bounds': [-90.5, -82.1, 41.6, 48.3], 'region': 'GLR'},
        'MN': {'name': 'Minnesota Wing', 'bounds': [-97.3, -89.5, 43.5, 49.4], 'region': 'NCR'},
        'MS': {'name': 'Mississippi Wing', 'bounds': [-91.7, -88.1, 30.1, 35.1], 'region': 'SER'},
        'MO': {'name': 'Missouri Wing', 'bounds': [-95.8, -89.1, 35.9, 40.7], 'region': 'NCR'},
        'MT': {'name': 'Montana Wing', 'bounds': [-116.1, -104.0, 44.3, 49.1], 'region': 'RMR'},
        'NE': {'name': 'Nebraska Wing', 'bounds': [-104.1, -95.3, 39.9, 43.1], 'region': 'NCR'},
        'NV': {'name': 'Nevada Wing', 'bounds': [-120.1, -114.0, 35.0, 42.1], 'region': 'PCR'},
        'NH': {'name': 'New Hampshire Wing', 'bounds': [-72.6, -70.6, 42.7, 45.4], 'region': 'NER'},
        'NJ': {'name': 'New Jersey Wing', 'bounds': [-75.6, -73.9, 38.9, 41.4], 'region': 'MAR'},
        'NM': {'name': 'New Mexico Wing', 'bounds': [-109.1, -103.0, 31.3, 37.1], 'region': 'SWR'},
        'NY': {'name': 'New York Wing', 'bounds': [-79.8, -71.8, 40.5, 45.1], 'region': 'NER'},
        'NC': {'name': 'North Carolina Wing', 'bounds': [-84.4, -75.4, 33.8, 36.6], 'region': 'MAR'},
        'ND': {'name': 'North Dakota Wing', 'bounds': [-104.1, -96.5, 45.9, 49.1], 'region': 'NCR'},
        'OH': {'name': 'Ohio Wing', 'bounds': [-84.9, -80.5, 38.4, 42.0], 'region': 'GLR'},
        'OK': {'name': 'Oklahoma Wing', 'bounds': [-103.1, -94.4, 33.6, 37.1], 'region': 'SWR'},
        'OR': {'name': 'Oregon Wing', 'bounds': [-124.7, -116.5, 41.9, 46.3], 'region': 'PCR'},
        'PA': {'name': 'Pennsylvania Wing', 'bounds': [-80.6, -74.7, 39.7, 42.3], 'region': 'MAR'},
        'RI': {'name': 'Rhode Island Wing', 'bounds': [-71.9, -71.1, 41.1, 42.1], 'region': 'NER'},
        'SC': {'name': 'South Carolina Wing', 'bounds': [-83.4, -78.5, 32.0, 35.3], 'region': 'SER'},
        'SD': {'name': 'South Dakota Wing', 'bounds': [-104.1, -96.4, 42.5, 45.9], 'region': 'NCR'},
        'TN': {'name': 'Tennessee Wing', 'bounds': [-90.4, -81.6, 34.9, 36.7], 'region': 'SER'},
        'TX': {'name': 'Texas Wing', 'bounds': [-106.7, -93.5, 25.8, 36.6], 'region': 'SWR'},
        'UT': {'name': 'Utah Wing', 'bounds': [-114.1, -109.0, 37.0, 42.1], 'region': 'RMR'},
        'VT': {'name': 'Vermont Wing', 'bounds': [-73.5, -71.5, 42.7, 45.1], 'region': 'NER'},
        'VA': {'name': 'Virginia Wing', 'bounds': [-83.7, -75.2, 36.5, 39.5], 'region': 'MAR'},
        'WA': {'name': 'Washington Wing', 'bounds': [-124.9, -116.9, 45.5, 49.1], 'region': 'PCR'},
        'WV': {'name': 'West Virginia Wing', 'bounds': [-82.7, -77.7, 37.2, 40.7], 'region': 'MAR'},
        'WI': {'name': 'Wisconsin Wing', 'bounds': [-92.9, -86.2, 42.5, 47.3], 'region': 'GLR'},
        'WY': {'name': 'Wyoming Wing', 'bounds': [-111.1, -104.0, 40.9, 45.1], 'region': 'RMR'},
        
        # OCONUS Wings
        'AK': {'name': 'Alaska Wing', 'bounds': [172, -129, 51, 72], 'region': 'PCR', 'dateline_crossing': True},
        'HI': {'name': 'Hawaii Wing', 'bounds': [-160.3, -154.7, 18.9, 22.3], 'region': 'PCR'},
        'PR': {'name': 'Puerto Rico Wing', 'bounds': [-67.3, -64.6, 17.6, 18.6], 'region': 'SER'},  # Combined PR+USVI
        'GU': {'name': 'Guam Wing', 'bounds': [144.6, 144.9, 13.2, 13.7], 'region': 'PCR'},
    }
    
    # Comprehensive shapefile bounds (all US territories)
    SHAPEFILE_BOUNDS = {
        'west': 144.6,  # Guam
        'east': -64.6,  # USVI
        'south': 13.2,  # Guam
        'north': 72      # Alaska
    }
    
    # CONUS bounds (properly centered)
    CONUS_BOUNDS = {
        'west': -125,
        'east': -66,
        'south': 24,
        'north': 50
    }


def log(msg: str):
    """Simple logging"""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# =====================================================================
# Database Data Retrieval
# =====================================================================

def get_latest_model_run():
    """Get the most recent model run timestamp"""
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT DISTINCT model_run 
            FROM observations.model_wind_forecasts 
            ORDER BY model_run DESC 
            LIMIT 1
        """)
        
        result = cur.fetchone()
        cur.close()
        conn.close()
        
        if result:
            return result[0]
        return None
    except Exception as e:
        log(f"Error getting latest model run: {e}")
        return None


def get_wind_data_for_bounds(model_run, west, east, south, north):
    """
    Get wind forecast data from database for a bounding box
    Returns list of dicts with station_id, lat, lon, max_wind, category
    """
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        # Query aggregated wind data within bounds
        query = """
        SELECT 
            mwf.station_id,
            ST_Y(mwf.location::geometry) as lat,
            ST_X(mwf.location::geometry) as lon,
            MAX(mwf.wind_speed_kts) as max_wind_kts,
            MAX(mwf.wind_gust_kts) as max_gust_kts,
            mwf.wind_category,
            a.name as airport_name
        FROM observations.model_wind_forecasts mwf
        INNER JOIN observations.airports a ON mwf.station_id = a.station_id
        WHERE mwf.model_run = %s
            AND mwf.forecast_hour <= 12
            AND ST_X(mwf.location::geometry) BETWEEN %s AND %s
            AND ST_Y(mwf.location::geometry) BETWEEN %s AND %s
        GROUP BY mwf.station_id, mwf.location, mwf.wind_category, a.name
        """
        
        cur.execute(query, (model_run, west, east, south, north))
        
        data = []
        for row in cur.fetchall():
            data.append({
                'station_id': row[0],
                'lat': float(row[1]),
                'lon': float(row[2]),
                'max_wind': int(row[3]) if row[3] else 0,
                'max_gust': int(row[4]) if row[4] else None,
                'category': row[5] or 'NORMAL',
                'name': row[6] or row[0]
            })
        
        cur.close()
        conn.close()
        
        log(f"Retrieved {len(data)} airports from database")
        return data
        
    except Exception as e:
        log(f"Error querying wind data: {e}")
        return []


# =====================================================================
# Map Generation
# =====================================================================

def create_wind_map(wing_code, data, model_run, output_path):
    """
    Create static wind constraint map for a wing
    """
    # Handle special case for CONUS
    if wing_code == 'CONUS':
        bounds = [MapConfig.CONUS_BOUNDS['west'], MapConfig.CONUS_BOUNDS['east'],
                  MapConfig.CONUS_BOUNDS['south'], MapConfig.CONUS_BOUNDS['north']]
        title = "CAP Wind Constraints - CONUS"
    else:
        wing_info = MapConfig.WING_BOUNDARIES[wing_code]
        bounds = wing_info['bounds']
        title = f"CAP Wind Constraints - {wing_info['name']}"
    
    # Create figure
    fig = plt.figure(figsize=(12, 9))
    ax = plt.axes(projection=ccrs.PlateCarree())
    
    # Set extent with padding
    padding = 0.5
    ax.set_extent([
        bounds[0] - padding,
        bounds[1] + padding,
        bounds[2] - padding,
        bounds[3] + padding
    ], crs=ccrs.PlateCarree())
    
    # Add map features
    ax.add_feature(cfeature.STATES, linewidth=0.5, edgecolor='gray')
    ax.add_feature(cfeature.COASTLINE, linewidth=0.8)
    ax.add_feature(cfeature.BORDERS, linewidth=0.5, linestyle='--')
    ax.add_feature(cfeature.LAKES, alpha=0.3)
    ax.add_feature(cfeature.RIVERS, alpha=0.3)
    
    # Add gridlines
    gl = ax.gridlines(draw_labels=True, linewidth=0.5, color='gray', alpha=0.5, linestyle='--')
    gl.top_labels = False
    gl.right_labels = False
    
    # Plot wind data
    for airport in data:
        category = airport['category']
        
        # Determine color and size
        if category == 'NO-GO':
            color = '#ef4444'  # Red
            size = 100
            zorder = 3
        elif category == 'CAUTION':
            color = '#fbbf24'  # Yellow
            size = 80
            zorder = 2
        else:  # NORMAL
            color = '#4ade80'  # Green
            size = 60
            zorder = 1
        
        ax.scatter(
            airport['lon'], airport['lat'],
            c=color, s=size, alpha=0.7,
            edgecolors='black', linewidths=0.5,
            transform=ccrs.PlateCarree(),
            zorder=zorder
        )
    
    # Add title and metadata
    plt.title(title, fontsize=14, fontweight='bold', pad=20)
    
    # Add legend
    legend_elements = [
        plt.scatter([], [], c='#4ade80', s=60, edgecolors='black', label='Normal (< 16 kts)'),
        plt.scatter([], [], c='#fbbf24', s=80, edgecolors='black', label='Caution (16-29 kts)'),
        plt.scatter([], [], c='#ef4444', s=100, edgecolors='black', label='No-Go (≥ 30 kts)')
    ]
    ax.legend(handles=legend_elements, loc='lower left', frameon=True, fancybox=True)
    
    # Add timestamp
    timestamp_str = model_run.strftime('%Y-%m-%d %H%M UTC')
    plt.text(0.99, 0.01, f'Model Run: {timestamp_str}\nCAP Winds System',
             transform=ax.transAxes, fontsize=8, verticalalignment='bottom',
             horizontalalignment='right', bbox=dict(boxstyle='round', facecolor='white', alpha=0.8))
    
    # Add statistics
    normal_count = sum(1 for a in data if a['category'] == 'NORMAL')
    caution_count = sum(1 for a in data if a['category'] == 'CAUTION')
    nogo_count = sum(1 for a in data if a['category'] == 'NO-GO')
    
    stats_text = f"Airports: {len(data)}\nNormal: {normal_count} | Caution: {caution_count} | No-Go: {nogo_count}"
    plt.text(0.01, 0.99, stats_text,
             transform=ax.transAxes, fontsize=9, verticalalignment='top',
             bbox=dict(boxstyle='round', facecolor='white', alpha=0.8))
    
    # Save
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()
    
    log(f"Generated map: {output_path}")


# =====================================================================
# Shapefile Generation
# =====================================================================

def create_comprehensive_shapefile(model_run, output_dir):
    """
    Create single comprehensive shapefile covering all US territories
    """
    log("Creating comprehensive shapefile for all US territories...")
    
    bounds = MapConfig.SHAPEFILE_BOUNDS
    
    # Get data for entire US territory extent
    data = get_wind_data_for_bounds(
        model_run,
        bounds['west'], bounds['east'],
        bounds['south'], bounds['north']
    )
    
    if not data:
        log("No data available for shapefile generation")
        return False
    
    # Create GeoDataFrame
    geometry = [Point(d['lon'], d['lat']) for d in data]
    gdf = gpd.GeoDataFrame(data, geometry=geometry, crs="EPSG:4326")
    
    # Prepare output
    timestamp = model_run.strftime('%Y%m%d_%H%M')
    shapefile_name = f"cap_winds_all_territories_{timestamp}"
    shapefile_dir = os.path.join(output_dir, shapefile_name)
    os.makedirs(shapefile_dir, exist_ok=True)
    
    shapefile_path = os.path.join(shapefile_dir, f"{shapefile_name}.shp")
    
    # Write shapefile
    gdf.to_file(shapefile_path)
    
    # Create ZIP
    zip_path = f"{shapefile_dir}.zip"
    with zipfile.ZipFile(zip_path, 'w') as zipf:
        for file in Path(shapefile_dir).glob('*'):
            zipf.write(file, file.name)
    
    log(f"Shapefile created: {zip_path}")
    log(f"  Coverage: {len(data)} airports across all US territories")
    
    return True


# =====================================================================
# Main Generation Function
# =====================================================================

def generate_all_maps():
    """Generate all wind constraint maps"""
    log("=" * 70)
    log("CAP Wind Constraints Map Generation - Database Version")
    log("=" * 70)
    
    # Get latest model run
    model_run = get_latest_model_run()
    if not model_run:
        log("ERROR: No model run data available in database")
        return False
    
    log(f"Using model run: {model_run}")
    
    # Create output directories
    os.makedirs(MapConfig.WEB_OUTPUT_DIR, exist_ok=True)
    os.makedirs(MapConfig.SHAPE_OUTPUT_DIR, exist_ok=True)
    
    # Generate CONUS map
    log("\n--- Generating CONUS Map ---")
    conus_bounds = MapConfig.CONUS_BOUNDS
    conus_data = get_wind_data_for_bounds(
        model_run,
        conus_bounds['west'], conus_bounds['east'],
        conus_bounds['south'], conus_bounds['north']
    )
    
    if conus_data:
        conus_output = os.path.join(MapConfig.WEB_OUTPUT_DIR, 'conus_wind_constraints.png')
        create_wind_map('CONUS', conus_data, model_run, conus_output)
    else:
        log("WARNING: No CONUS data available")
    
    # Generate wing maps
    log("\n--- Generating Wing Maps ---")
    for wing_code, wing_info in MapConfig.WING_BOUNDARIES.items():
        if wing_code == 'CONUS':
            continue
        
        log(f"Generating {wing_info['name']}...")
        bounds = wing_info['bounds']
        
        data = get_wind_data_for_bounds(
            model_run,
            bounds[0], bounds[1], bounds[2], bounds[3]
        )
        
        if data:
            output_path = os.path.join(
                MapConfig.WEB_OUTPUT_DIR,
                f"{wing_code.lower()}_wind_constraints.png"
            )
            create_wind_map(wing_code, data, model_run, output_path)
        else:
            log(f"  No data for {wing_code}")
    
    # Generate comprehensive shapefile
    log("\n--- Generating Comprehensive Shapefile ---")
    create_comprehensive_shapefile(model_run, MapConfig.SHAPE_OUTPUT_DIR)
    
    log("\n" + "=" * 70)
    log("Map generation complete!")
    log("=" * 70)
    
    return True


if __name__ == '__main__':
    try:
        generate_all_maps()
    except Exception as e:
        log(f"FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

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
7. Region-level maps (NCR, GLR, MAR, NER, SER-CONUS, SWR, RMR, PCR-CONUS)
8. OCONUS subregion maps: PCR-AK, PCR-HI, PCR-GU, SER-PR
"""

import sys
sys.path.insert(0, '/var/www/cap_winds_app')

import os
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple, Optional

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
    WEB_OUTPUT_DIR   = '/var/www/html/cap_winds'
    SHAPE_OUTPUT_DIR = '/var/www/html/cap_winds_shp'

    # Wind thresholds (CAPR 70-1)
    MAX_OPERATIONAL_WIND = 30  # kts
    CAUTION_WIND         = 20  # kts

    # ------------------------------------------------------------------
    # Wing (State/Territory) boundaries
    # bounds format: [west, east, south, north]
    # ------------------------------------------------------------------
    WING_BOUNDARIES = {
        # CONUS Wings
        'AL': {'name': 'Alabama Wing',        'bounds': [-88.5, -84.8,  30.1,  35.1], 'region': 'SER'},
        'AZ': {'name': 'Arizona Wing',         'bounds': [-114.8,-109.0, 31.3,  37.1], 'region': 'SWR'},
        'AR': {'name': 'Arkansas Wing',        'bounds': [-94.6,  -89.6, 33.0,  36.5], 'region': 'SWR'},
        'CA': {'name': 'California Wing',      'bounds': [-124.5,-114.1, 32.5,  42.1], 'region': 'PCR'},
        'CO': {'name': 'Colorado Wing',        'bounds': [-109.1,-102.0, 36.9,  41.1], 'region': 'RMR'},
        'CT': {'name': 'Connecticut Wing',     'bounds': [-73.8,  -71.8, 40.9,  42.1], 'region': 'NER'},
        'DE': {'name': 'Delaware Wing',        'bounds': [-75.8,  -75.0, 38.4,  39.9], 'region': 'MAR'},
        'FL': {'name': 'Florida Wing',         'bounds': [-87.7,  -80.0, 24.4,  31.1], 'region': 'SER'},
        'GA': {'name': 'Georgia Wing',         'bounds': [-85.6,  -80.8, 30.3,  35.1], 'region': 'SER'},
        'ID': {'name': 'Idaho Wing',           'bounds': [-117.3,-111.0, 41.9,  49.1], 'region': 'RMR'},
        'IL': {'name': 'Illinois Wing',        'bounds': [-91.5,  -87.5, 36.9,  42.6], 'region': 'NCR'},
        'IN': {'name': 'Indiana Wing',         'bounds': [-88.1,  -84.8, 37.7,  41.8], 'region': 'GLR'},
        'IA': {'name': 'Iowa Wing',            'bounds': [-96.7,  -90.1, 40.3,  43.6], 'region': 'NCR'},
        'KS': {'name': 'Kansas Wing',          'bounds': [-102.1, -94.6, 36.9,  40.1], 'region': 'NCR'},
        'KY': {'name': 'Kentucky Wing',        'bounds': [-89.6,  -81.9, 36.5,  39.2], 'region': 'GLR'},
        'LA': {'name': 'Louisiana Wing',       'bounds': [-94.1,  -88.8, 28.9,  33.1], 'region': 'SWR'},
        'ME': {'name': 'Maine Wing',           'bounds': [-71.1,  -66.9, 43.0,  47.5], 'region': 'NER'},
        'MD': {'name': 'Maryland Wing',        'bounds': [-79.5,  -75.0, 37.9,  39.8], 'region': 'MAR'},
        'MA': {'name': 'Massachusetts Wing',   'bounds': [-73.5,  -69.9, 41.2,  42.9], 'region': 'NER'},
        'MI': {'name': 'Michigan Wing',        'bounds': [-90.5,  -82.1, 41.6,  48.3], 'region': 'GLR'},
        'MN': {'name': 'Minnesota Wing',       'bounds': [-97.3,  -89.5, 43.5,  49.4], 'region': 'NCR'},
        'MS': {'name': 'Mississippi Wing',     'bounds': [-91.7,  -88.1, 30.1,  35.1], 'region': 'SER'},
        'MO': {'name': 'Missouri Wing',        'bounds': [-95.8,  -89.1, 35.9,  40.7], 'region': 'NCR'},
        'MT': {'name': 'Montana Wing',         'bounds': [-116.1,-104.0, 44.3,  49.1], 'region': 'RMR'},
        'NE': {'name': 'Nebraska Wing',        'bounds': [-104.1, -95.3, 39.9,  43.1], 'region': 'NCR'},
        'NV': {'name': 'Nevada Wing',          'bounds': [-120.1,-114.0, 35.0,  42.1], 'region': 'PCR'},
        'NH': {'name': 'New Hampshire Wing',   'bounds': [-72.6,  -70.6, 42.7,  45.4], 'region': 'NER'},
        'NJ': {'name': 'New Jersey Wing',      'bounds': [-75.6,  -73.9, 38.9,  41.4], 'region': 'MAR'},
        'NM': {'name': 'New Mexico Wing',      'bounds': [-109.1,-103.0, 31.3,  37.1], 'region': 'SWR'},
        'NY': {'name': 'New York Wing',        'bounds': [-79.8,  -71.8, 40.5,  45.1], 'region': 'NER'},
        'NC': {'name': 'North Carolina Wing',  'bounds': [-84.4,  -75.4, 33.8,  36.6], 'region': 'SER'},
        'ND': {'name': 'North Dakota Wing',    'bounds': [-104.1, -96.5, 45.9,  49.1], 'region': 'NCR'},
        'OH': {'name': 'Ohio Wing',            'bounds': [-84.9,  -80.5, 38.4,  42.0], 'region': 'GLR'},
        'OK': {'name': 'Oklahoma Wing',        'bounds': [-103.1, -94.4, 33.6,  37.1], 'region': 'SWR'},
        'OR': {'name': 'Oregon Wing',          'bounds': [-124.7,-116.5, 41.9,  46.3], 'region': 'PCR'},
        'PA': {'name': 'Pennsylvania Wing',    'bounds': [-80.6,  -74.7, 39.7,  42.3], 'region': 'MAR'},
        'RI': {'name': 'Rhode Island Wing',    'bounds': [-71.9,  -71.1, 41.1,  42.1], 'region': 'NER'},
        'SC': {'name': 'South Carolina Wing',  'bounds': [-83.4,  -78.5, 32.0,  35.3], 'region': 'SER'},
        'SD': {'name': 'South Dakota Wing',    'bounds': [-104.1, -96.4, 42.5,  45.9], 'region': 'NCR'},
        'TN': {'name': 'Tennessee Wing',       'bounds': [-90.4,  -81.6, 34.9,  36.7], 'region': 'SER'},
        'TX': {'name': 'Texas Wing',           'bounds': [-106.7, -93.5, 25.8,  36.6], 'region': 'SWR'},
        'UT': {'name': 'Utah Wing',            'bounds': [-114.1,-109.0, 37.0,  42.1], 'region': 'RMR'},
        'VT': {'name': 'Vermont Wing',         'bounds': [-73.5,  -71.5, 42.7,  45.1], 'region': 'NER'},
        'VA': {'name': 'Virginia Wing',        'bounds': [-83.7,  -75.2, 36.5,  39.5], 'region': 'MAR'},
        'WA': {'name': 'Washington Wing',      'bounds': [-124.9,-116.9, 45.5,  49.1], 'region': 'PCR'},
        'WV': {'name': 'West Virginia Wing',   'bounds': [-82.7,  -77.7, 37.2,  40.7], 'region': 'MAR'},
        'WI': {'name': 'Wisconsin Wing',       'bounds': [-92.9,  -86.2, 42.5,  47.3], 'region': 'NCR'},
        'WY': {'name': 'Wyoming Wing',         'bounds': [-111.1,-104.0, 40.9,  45.1], 'region': 'RMR'},

        # OCONUS Wings
        # AK: dateline-crossing handled separately — bounds stored as [west_pos, east_neg, south, north]
        # but DB query uses the two-segment approach; map uses PlateCarree with wide extent.
        'AK': {'name': 'Alaska Wing',        'bounds': [-180.0,-129.0, 51.0, 72.0], 'region': 'PCR',
               'dateline_crossing': True},
        'HI': {'name': 'Hawaii Wing',        'bounds': [-160.3,-154.7, 18.9, 22.3], 'region': 'PCR'},
        'PR': {'name': 'Puerto Rico Wing',   'bounds': [-67.3,  -64.6, 17.6, 18.6], 'region': 'SER'},
        'GU': {'name': 'Guam Wing',          'bounds': [144.5,  145.1, 13.1, 13.8], 'region': 'PCR'},
    }

    # ------------------------------------------------------------------
    # Region-level maps
    # State-based regions use iso_region DB query (not bbox).
    # OCONUS subregions use bbox query.
    # bounds here are used only for the map extent (not the DB query).
    # bounds format: [west, east, south, north]
    # ------------------------------------------------------------------
    REGION_MAPS = {
        # CONUS regions (state-based DB query)
        'NCR':      {'name': 'North Central Region',       'states': ['US-IA','US-IL','US-KS','US-MN','US-MO','US-ND','US-NE','US-SD','US-WI'],
                     'bounds': [-104.1, -87.5, 36.9, 49.4]},
        'GLR':      {'name': 'Great Lakes Region',         'states': ['US-IN','US-KY','US-MI','US-OH','US-WV'],
                     'bounds': [-90.5,  -77.7, 36.5, 48.3]},
        'MAR':      {'name': 'Mid-Atlantic Region',        'states': ['US-DC','US-DE','US-MD','US-NJ','US-NY','US-PA','US-VA'],
                     'bounds': [-80.6,  -71.8, 37.9, 45.1]},
        'NER':      {'name': 'Northeast Region',           'states': ['US-CT','US-MA','US-ME','US-NH','US-RI','US-VT'],
                     'bounds': [-73.8,  -66.9, 40.9, 47.5]},
        'SER-CONUS':{'name': 'Southeast Region (CONUS)',   'states': ['US-AL','US-FL','US-GA','US-MS','US-NC','US-SC','US-TN'],
                     'bounds': [-91.7,  -75.4, 24.4, 36.7]},
        'SWR':      {'name': 'Southwest Region',           'states': ['US-AR','US-AZ','US-LA','US-NM','US-OK','US-TX'],
                     'bounds': [-114.8, -88.8, 25.8, 37.1]},
        'RMR':      {'name': 'Rocky Mountain Region',      'states': ['US-CO','US-ID','US-MT','US-UT','US-WY'],
                     'bounds': [-117.3,-102.0, 36.9, 49.1]},
        'PCR-CONUS':{'name': 'Pacific Region (CONUS)',     'states': ['US-CA','US-NV','US-OR','US-WA'],
                     'bounds': [-124.9,-114.0, 32.5, 49.1]},

        # OCONUS subregions (bbox DB query)
        'PCR-AK':   {'name': 'PCR — Alaska',               'states': None,
                     'bounds': [-180.0,-129.0, 51.0, 72.0], 'dateline_crossing': True},
        'PCR-HI':   {'name': 'PCR — Hawaii',               'states': None,
                     'bounds': [-160.3,-154.7, 18.9, 22.3]},
        'PCR-GU':   {'name': 'PCR — Guam',                 'states': None,
                     'bounds': [144.5,  145.1, 13.1, 13.8]},
        'SER-PR':   {'name': 'SER — Puerto Rico / Caribbean','states': None,
                     'bounds': [-67.3,  -64.6, 17.6, 18.6]},
    }

    # CONUS overview
    CONUS_BOUNDS = {'west': -125, 'east': -66, 'south': 24, 'north': 50}

    # Shapefile: broadest extent covering all US territories
    SHAPEFILE_BOUNDS = {'west': -180, 'east': -64.6, 'south': 13.1, 'north': 72}


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
        cur  = conn.cursor()
        cur.execute("""
            SELECT DISTINCT model_run
            FROM observations.model_wind_forecasts
            ORDER BY model_run DESC
            LIMIT 1
        """)
        result = cur.fetchone()
        cur.close(); conn.close()
        return result[0] if result else None
    except Exception as e:
        log(f"Error getting latest model run: {e}")
        return None


def get_wind_data_for_bounds(model_run, west, east, south, north):
    """
    Get wind forecast data from database for a bounding box.
    Returns list of dicts with station_id, lat, lon, max_wind, category.
    For AK (dateline crossing) call with west=-180, east=-129; the
    Eastern Aleutians (lon > 0) are handled by a separate OR clause.
    """
    try:
        conn = get_connection()
        cur  = conn.cursor()

        query = """
        SELECT
            mwf.station_id,
            ST_Y(mwf.location::geometry) AS lat,
            ST_X(mwf.location::geometry) AS lon,
            MAX(mwf.wind_speed_kts)      AS max_wind_kts,
            MAX(mwf.wind_gust_kts)       AS max_gust_kts,
            mwf.wind_category,
            a.name                       AS airport_name
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
                'lat':        float(row[1]),
                'lon':        float(row[2]),
                'max_wind':   int(row[3]) if row[3] else 0,
                'max_gust':   int(row[4]) if row[4] else None,
                'category':   row[5] or 'NORMAL',
                'name':       row[6] or row[0],
            })

        cur.close(); conn.close()
        log(f"  bbox query [{west},{south} → {east},{north}]: {len(data)} airports")
        return data

    except Exception as e:
        log(f"Error querying wind data: {e}")
        return []


def get_wind_data_for_states(model_run, state_list):
    """
    Get wind forecast data from database filtered by iso_region codes.
    Used for CONUS region maps to avoid clipping edge airports.
    """
    try:
        conn = get_connection()
        cur  = conn.cursor()

        placeholders = ','.join(['%s'] * len(state_list))
        query = f"""
        SELECT
            mwf.station_id,
            ST_Y(mwf.location::geometry) AS lat,
            ST_X(mwf.location::geometry) AS lon,
            MAX(mwf.wind_speed_kts)      AS max_wind_kts,
            MAX(mwf.wind_gust_kts)       AS max_gust_kts,
            mwf.wind_category,
            a.name                       AS airport_name
        FROM observations.model_wind_forecasts mwf
        INNER JOIN observations.airports a ON mwf.station_id = a.station_id
        WHERE mwf.model_run = %s
          AND mwf.forecast_hour <= 12
          AND a.iso_region IN ({placeholders})
        GROUP BY mwf.station_id, mwf.location, mwf.wind_category, a.name
        """

        cur.execute(query, [model_run] + state_list)

        data = []
        for row in cur.fetchall():
            data.append({
                'station_id': row[0],
                'lat':        float(row[1]),
                'lon':        float(row[2]),
                'max_wind':   int(row[3]) if row[3] else 0,
                'max_gust':   int(row[4]) if row[4] else None,
                'category':   row[5] or 'NORMAL',
                'name':       row[6] or row[0],
            })

        cur.close(); conn.close()
        log(f"  state query {state_list}: {len(data)} airports")
        return data

    except Exception as e:
        log(f"Error querying wind data by state: {e}")
        return []


# =====================================================================
# Map Generation
# =====================================================================

def _category_style(category):
    if category == 'NO-GO':
        return '#ef4444', 100, 3
    if category == 'CAUTION':
        return '#fbbf24', 80, 2
    return '#4ade80', 60, 1


def create_wind_map(title, bounds, data, model_run, output_path,
                    dateline_crossing=False):
    """
    Create a static wind constraint map.

    bounds: [west, east, south, north]
    dateline_crossing: if True, use a wider PlateCarree extent that
                       straddles the antimeridian (AK use case).
    """
    west, east, south, north = bounds
    pad = 0.5

    fig = plt.figure(figsize=(12, 9))

    if dateline_crossing:
        # Use a central-longitude projection centred on ~-155 so Alaska
        # renders without splitting across the antimeridian.
        proj = ccrs.PlateCarree(central_longitude=-155)
        ax   = plt.axes(projection=proj)
        ax.set_extent([-180, -129, 50, 73], crs=ccrs.PlateCarree())
    else:
        ax = plt.axes(projection=ccrs.PlateCarree())
        ax.set_extent([west - pad, east + pad, south - pad, north + pad],
                      crs=ccrs.PlateCarree())

    # Map features
    ax.add_feature(cfeature.STATES,    linewidth=0.5, edgecolor='gray')
    ax.add_feature(cfeature.COASTLINE, linewidth=0.8)
    ax.add_feature(cfeature.BORDERS,   linewidth=0.5, linestyle='--')
    ax.add_feature(cfeature.LAKES,     alpha=0.3)
    ax.add_feature(cfeature.RIVERS,    alpha=0.3)

    gl = ax.gridlines(draw_labels=True, linewidth=0.5,
                      color='gray', alpha=0.5, linestyle='--')
    gl.top_labels   = False
    gl.right_labels = False

    # Plot airports
    for airport in data:
        color, size, zorder = _category_style(airport['category'])
        ax.scatter(
            airport['lon'], airport['lat'],
            c=color, s=size, alpha=0.7,
            edgecolors='black', linewidths=0.5,
            transform=ccrs.PlateCarree(),
            zorder=zorder,
        )

    plt.title(title, fontsize=14, fontweight='bold', pad=20)

    # Legend
    legend_elements = [
        plt.scatter([], [], c='#4ade80', s=60, edgecolors='black', label='Normal (< 20 kts)'),
        plt.scatter([], [], c='#fbbf24', s=80, edgecolors='black', label='Caution (20–29 kts)'),
        plt.scatter([], [], c='#ef4444', s=100, edgecolors='black', label='No-Go (≥ 30 kts)'),
    ]
    ax.legend(handles=legend_elements, loc='lower left', frameon=True, fancybox=True)

    # Timestamp
    ts = model_run.strftime('%Y-%m-%d %H%MZ')
    plt.text(0.99, 0.01, f'Model Run: {ts}\nCAP WxCOP',
             transform=ax.transAxes, fontsize=8,
             verticalalignment='bottom', horizontalalignment='right',
             bbox=dict(boxstyle='round', facecolor='white', alpha=0.8))

    # Stats
    n  = sum(1 for a in data if a['category'] == 'NORMAL')
    c  = sum(1 for a in data if a['category'] == 'CAUTION')
    ng = sum(1 for a in data if a['category'] == 'NO-GO')
    plt.text(0.01, 0.99,
             f"Airports: {len(data)}\nNormal: {n} | Caution: {c} | No-Go: {ng}",
             transform=ax.transAxes, fontsize=9, verticalalignment='top',
             bbox=dict(boxstyle='round', facecolor='white', alpha=0.8))

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()
    log(f"  → {output_path}")


# =====================================================================
# Shapefile Generation
# =====================================================================

def create_comprehensive_shapefile(model_run, output_dir):
    """Create single comprehensive shapefile covering all US territories"""
    log("Creating comprehensive shapefile for all US territories...")

    # CONUS + Atlantic territories
    data = get_wind_data_for_bounds(
        model_run, -180, -64.6, 13.1, 72
    )
    # Guam (positive longitudes)
    data += get_wind_data_for_bounds(
        model_run, 144.5, 145.1, 13.1, 13.8
    )

    if not data:
        log("No data available for shapefile generation")
        return False

    geometry = [Point(d['lon'], d['lat']) for d in data]
    gdf = gpd.GeoDataFrame(data, geometry=geometry, crs="EPSG:4326")

    timestamp     = model_run.strftime('%Y%m%d_%H%M')
    shp_name      = f"cap_winds_all_territories_{timestamp}"
    shp_dir       = os.path.join(output_dir, shp_name)
    os.makedirs(shp_dir, exist_ok=True)

    shp_path = os.path.join(shp_dir, f"{shp_name}.shp")
    gdf.to_file(shp_path)

    zip_path = f"{shp_dir}.zip"
    with zipfile.ZipFile(zip_path, 'w') as zf:
        for f in Path(shp_dir).glob('*'):
            zf.write(f, f.name)

    log(f"Shapefile: {zip_path}  ({len(data)} airports)")
    return True


# =====================================================================
# Main Generation
# =====================================================================

def generate_all_maps():
    """Generate all wind constraint maps"""
    log("=" * 70)
    log("CAP Wind Constraints Map Generation")
    log("=" * 70)

    model_run = get_latest_model_run()
    if not model_run:
        log("ERROR: No model run data available in database")
        return False

    log(f"Model run: {model_run}")

    os.makedirs(MapConfig.WEB_OUTPUT_DIR,   exist_ok=True)
    os.makedirs(MapConfig.SHAPE_OUTPUT_DIR, exist_ok=True)

    # ------------------------------------------------------------------
    # 1. CONUS overview
    # ------------------------------------------------------------------
    log("\n--- CONUS ---")
    cb = MapConfig.CONUS_BOUNDS
    conus_data = get_wind_data_for_bounds(
        model_run, cb['west'], cb['east'], cb['south'], cb['north']
    )
    if conus_data:
        create_wind_map(
            "CAP Wind Constraints — CONUS",
            [cb['west'], cb['east'], cb['south'], cb['north']],
            conus_data, model_run,
            os.path.join(MapConfig.WEB_OUTPUT_DIR, 'conus_wind_constraints.png'),
        )
    else:
        log("  WARNING: no CONUS data")

    # ------------------------------------------------------------------
    # 2. Region maps
    # ------------------------------------------------------------------
    log("\n--- Region maps ---")
    for region_key, rcfg in MapConfig.REGION_MAPS.items():
        log(f"Region: {region_key} — {rcfg['name']}")
        b = rcfg['bounds']

        if rcfg['states']:
            # CONUS region — state-based query
            data = get_wind_data_for_states(model_run, rcfg['states'])
            if not data:
                log(f"  Fallback to bbox for {region_key}")
                data = get_wind_data_for_bounds(model_run, b[0], b[1], b[2], b[3])
        else:
            # OCONUS — bbox query
            data = get_wind_data_for_bounds(model_run, b[0], b[1], b[2], b[3])

        if data:
            fname = region_key.lower().replace('-', '-') + '_wind_constraints.png'
            create_wind_map(
                f"CAP Wind Constraints — {rcfg['name']}",
                b, data, model_run,
                os.path.join(MapConfig.WEB_OUTPUT_DIR, fname),
                dateline_crossing=rcfg.get('dateline_crossing', False),
            )
        else:
            log(f"  No data for {region_key} — skipping")

    # ------------------------------------------------------------------
    # 3. Wing (state/territory) maps
    # ------------------------------------------------------------------
    log("\n--- Wing maps ---")
    for wing_code, wcfg in MapConfig.WING_BOUNDARIES.items():
        log(f"Wing: {wing_code} — {wcfg['name']}")
        b = wcfg['bounds']
        data = get_wind_data_for_bounds(model_run, b[0], b[1], b[2], b[3])

        if data:
            fname = wing_code.lower() + '_wind_constraints.png'
            create_wind_map(
                f"CAP Wind Constraints — {wcfg['name']}",
                b, data, model_run,
                os.path.join(MapConfig.WEB_OUTPUT_DIR, fname),
                dateline_crossing=wcfg.get('dateline_crossing', False),
            )
        else:
            log(f"  No data for {wing_code} — skipping")

    # ------------------------------------------------------------------
    # 4. Shapefile
    # ------------------------------------------------------------------
    log("\n--- Shapefile ---")
    create_comprehensive_shapefile(model_run, MapConfig.SHAPE_OUTPUT_DIR)

    log("\n" + "=" * 70)
    log("Map generation complete")
    log("=" * 70)
    return True


if __name__ == '__main__':
    try:
        generate_all_maps()
    except Exception as e:
        log(f"FATAL: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


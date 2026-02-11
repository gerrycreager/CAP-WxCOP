"""
CAP Winds Map Generator - Complete v6 with OCONUS Support
Version: 2026-01-01 v6

Features:
- State, Region, and CONUS wind analysis maps
- 50nm radius capability for airports/CAP Grid points
- 12-hour forecast period (hours 0-12)
- CONUS: HRRR model (3km resolution, hourly)
- OCONUS (AK/HI/PR/VI/GU): GFS model (0.25°, 6-hourly)
- US Government data sources only (NOAA HRRR/GFS via LDM)
- Shapefile exports with ZIP packaging
- Airport wind interpolation and classification

INSTRUCTIONS:
1. Requires gfs_manager.py in same directory
2. Set MODEL_ROOT environment variable to /LDM/models/hrrr
3. Ensure LDM is providing both HRRR (NGRID) and GFS (CONDUIT) data
4. Restart Apache: sudo systemctl restart apache2

Dependencies:
- numpy, pandas, requests, xarray, scipy
- matplotlib, cartopy
- geopandas, shapely, fiona
- cfgrib (for GRIB2 reading)
"""

import os
import sys
import getpass
import traceback
import zipfile
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional, Callable
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from math import radians, cos, sin, asin, sqrt

import numpy as np
import pandas as pd
import requests
import xarray as xr
from scipy.interpolate import RegularGridInterpolator

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import cartopy.crs as ccrs
import cartopy.feature as cfeature
import matplotlib.patheffects as path_effects

import geopandas as gpd
from shapely.geometry import Point, Polygon, LineString, mapping as geom_mapping
import fiona
from fiona.crs import from_epsg

# GFS data manager for OCONUS coverage (AK, HI, PR, VI, GU)
from gfs_manager import GFSDataManager


# =====================================================================
# Configuration
# =====================================================================

class Config:
    """Centralized configuration management"""
    
    def __init__(self):
        self.APP_DIR = os.path.dirname(os.path.abspath(__file__))
        self.APP_CACHE_DIR = os.path.join(self.APP_DIR, ".cache")
        self.APP_CONFIG_DIR = os.path.join(self.APP_DIR, ".config")
        
        self.MODEL_ROOT = os.getenv('MODEL_ROOT', '/var/www/cap_winds_app/model_data')
        self.WEB_OUTPUT_DIR = os.getenv('WEB_OUTPUT_DIR', '/var/www/html/cap_winds')
        self.SHAPE_OUTPUT_DIR = os.getenv('SHAPE_OUTPUT_DIR', '/var/www/html/cap_winds_shp')
        
        self.MAX_OPERATIONAL_WIND = 30  # kts
        self.CAUTION_WIND = 20  # kts
        self.FORECAST_HOURS = range(0, 13)  # 12-hour forecast period (hours 0-12)
        self.MIN_RUNWAY_LENGTH = 2500  # feet
        
        # US Government data sources
        self.HRRR_BASE_URL = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com"  # NOAA HRRR model
        self.AIRPORT_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
        self.RUNWAY_URL = "https://davidmegginson.github.io/ourairports-data/runways.csv"
        
        # Initialize directories
        os.makedirs(self.APP_CACHE_DIR, exist_ok=True)
        os.makedirs(self.APP_CONFIG_DIR, exist_ok=True)
        os.makedirs(self.WEB_OUTPUT_DIR, exist_ok=True)
        os.makedirs(self.SHAPE_OUTPUT_DIR, exist_ok=True)
        
        # Set environment variables
        os.environ.setdefault("MPLCONFIGDIR", os.path.join(self.APP_CONFIG_DIR, "matplotlib"))
        os.environ.setdefault("CARTOPY_CACHE_DIR", os.path.join(self.APP_CACHE_DIR, "cartopy"))
        
        # Geographic boundaries
        self.STATE_BOUNDARIES = self._init_state_boundaries()
        self.CAP_REGIONS = self._init_cap_regions()
        self.CONUS_BOUNDS = [-125, -65, 24, 50]
        
    @staticmethod
    def _init_state_boundaries() -> Dict:
        """Initialize state boundary definitions"""
        return {
            'AL': {'name': 'Alabama', 'bounds': [-88.5, -84.8, 30.1, 35.1], 'region': 'SER'},
            'AK': {'name': 'Alaska', 'bounds': [-180, -129, 51, 72], 'region': 'PCR'},
            'AZ': {'name': 'Arizona', 'bounds': [-114.8, -109.0, 31.3, 37.1], 'region': 'SWR'},
            'AR': {'name': 'Arkansas', 'bounds': [-94.6, -89.6, 33.0, 36.5], 'region': 'SWR'},
            'CA': {'name': 'California', 'bounds': [-124.5, -114.1, 32.5, 42.1], 'region': 'PCR'},
            'CO': {'name': 'Colorado', 'bounds': [-109.1, -102.0, 36.9, 41.1], 'region': 'RMR'},
            'CT': {'name': 'Connecticut', 'bounds': [-73.8, -71.8, 40.9, 42.1], 'region': 'NER'},
            'DE': {'name': 'Delaware', 'bounds': [-75.8, -75.0, 38.4, 39.9], 'region': 'MAR'},
            'FL': {'name': 'Florida', 'bounds': [-87.7, -80.0, 24.4, 31.1], 'region': 'SER'},
            'GA': {'name': 'Georgia', 'bounds': [-85.6, -80.8, 30.3, 35.1], 'region': 'SER'},
            'HI': {'name': 'Hawaii', 'bounds': [-160.3, -154.7, 18.9, 22.3], 'region': 'PCR'},
            'ID': {'name': 'Idaho', 'bounds': [-117.3, -111.0, 41.9, 49.1], 'region': 'RMR'},
            'IL': {'name': 'Illinois', 'bounds': [-91.5, -87.5, 36.9, 42.6], 'region': 'GLR'},
            'IN': {'name': 'Indiana', 'bounds': [-88.1, -84.8, 37.7, 41.8], 'region': 'GLR'},
            'IA': {'name': 'Iowa', 'bounds': [-96.7, -90.1, 40.3, 43.6], 'region': 'NCR'},
            'KS': {'name': 'Kansas', 'bounds': [-102.1, -94.6, 36.9, 40.1], 'region': 'NCR'},
            'KY': {'name': 'Kentucky', 'bounds': [-89.6, -81.9, 36.5, 39.2], 'region': 'GLR'},
            'LA': {'name': 'Louisiana', 'bounds': [-94.1, -88.8, 28.9, 33.1], 'region': 'SWR'},
            'ME': {'name': 'Maine', 'bounds': [-71.1, -66.9, 43.0, 47.5], 'region': 'NER'},
            'MD': {'name': 'Maryland', 'bounds': [-79.5, -75.0, 37.9, 39.8], 'region': 'MAR'},
            'MA': {'name': 'Massachusetts', 'bounds': [-73.5, -69.9, 41.2, 42.9], 'region': 'NER'},
            'MI': {'name': 'Michigan', 'bounds': [-90.5, -82.1, 41.6, 48.3], 'region': 'GLR'},
            'MN': {'name': 'Minnesota', 'bounds': [-97.3, -89.5, 43.5, 49.4], 'region': 'NCR'},
            'MS': {'name': 'Mississippi', 'bounds': [-91.7, -88.1, 30.1, 35.1], 'region': 'SER'},
            'MO': {'name': 'Missouri', 'bounds': [-95.8, -89.1, 35.9, 40.7], 'region': 'NCR'},
            'MT': {'name': 'Montana', 'bounds': [-116.1, -104.0, 44.3, 49.1], 'region': 'RMR'},
            'NE': {'name': 'Nebraska', 'bounds': [-104.1, -95.3, 39.9, 43.1], 'region': 'NCR'},
            'NV': {'name': 'Nevada', 'bounds': [-120.1, -114.0, 35.0, 42.1], 'region': 'PCR'},
            'NH': {'name': 'New Hampshire', 'bounds': [-72.6, -70.6, 42.7, 45.4], 'region': 'NER'},
            'NJ': {'name': 'New Jersey', 'bounds': [-75.6, -73.9, 38.9, 41.4], 'region': 'MAR'},
            'NM': {'name': 'New Mexico', 'bounds': [-109.1, -103.0, 31.3, 37.1], 'region': 'SWR'},
            'NY': {'name': 'New York', 'bounds': [-79.8, -71.8, 40.5, 45.1], 'region': 'NER'},
            'NC': {'name': 'North Carolina', 'bounds': [-84.4, -75.4, 33.8, 36.6], 'region': 'MAR'},
            'ND': {'name': 'North Dakota', 'bounds': [-104.1, -96.5, 45.9, 49.1], 'region': 'NCR'},
            'OH': {'name': 'Ohio', 'bounds': [-84.9, -80.5, 38.4, 42.0], 'region': 'GLR'},
            'OK': {'name': 'Oklahoma', 'bounds': [-103.1, -94.4, 33.6, 37.1], 'region': 'SWR'},
            'OR': {'name': 'Oregon', 'bounds': [-124.7, -116.5, 41.9, 46.3], 'region': 'PCR'},
            'PA': {'name': 'Pennsylvania', 'bounds': [-80.6, -74.7, 39.7, 42.3], 'region': 'MAR'},
            'RI': {'name': 'Rhode Island', 'bounds': [-71.9, -71.1, 41.1, 42.1], 'region': 'NER'},
            'SC': {'name': 'South Carolina', 'bounds': [-83.4, -78.5, 32.0, 35.3], 'region': 'SER'},
            'SD': {'name': 'South Dakota', 'bounds': [-104.1, -96.4, 42.5, 45.9], 'region': 'NCR'},
            'TN': {'name': 'Tennessee', 'bounds': [-90.4, -81.6, 34.9, 36.7], 'region': 'SER'},
            'TX': {'name': 'Texas', 'bounds': [-106.7, -93.5, 25.8, 36.6], 'region': 'SWR'},
            'UT': {'name': 'Utah', 'bounds': [-114.1, -109.0, 37.0, 42.1], 'region': 'RMR'},
            'VT': {'name': 'Vermont', 'bounds': [-73.5, -71.5, 42.7, 45.1], 'region': 'NER'},
            'VA': {'name': 'Virginia', 'bounds': [-83.7, -75.2, 36.5, 39.5], 'region': 'MAR'},
            'WA': {'name': 'Washington', 'bounds': [-124.9, -116.9, 45.5, 49.1], 'region': 'PCR'},
            'WV': {'name': 'West Virginia', 'bounds': [-82.7, -77.7, 37.2, 40.7], 'region': 'MAR'},
            'WI': {'name': 'Wisconsin', 'bounds': [-92.9, -86.2, 42.5, 47.3], 'region': 'GLR'},
            'WY': {'name': 'Wyoming', 'bounds': [-111.1, -104.0, 40.9, 45.1], 'region': 'RMR'},
            'PR': {'name': 'Puerto Rico', 'bounds': [-67.3, -65.2, 17.9, 18.6], 'region': 'SER'},
            'VI': {'name': 'US Virgin Islands', 'bounds': [-65.1, -64.6, 17.6, 18.5], 'region': 'SER'},
            'GU': {'name': 'Guam', 'bounds': [144.6, 144.9, 13.2, 13.7], 'region': 'PCR'},
        }
    
    @staticmethod
    def _init_cap_regions() -> Dict:
        """Initialize CAP region definitions"""
        return {
            'NER': {'name': 'Northeast Region', 'states': ['CT', 'ME', 'MA', 'NH', 'NY', 'RI', 'VT']},
            'MAR': {'name': 'Mid-Atlantic Region', 'states': ['DE', 'MD', 'NJ', 'NC', 'PA', 'VA', 'WV']},
            'SER': {'name': 'Southeast Region', 'states': ['AL', 'FL', 'GA', 'MS', 'SC', 'TN', 'PR', 'VI']},
            'SER-CONUS': {'name': 'Southeast Region - CONUS', 'states': ['AL', 'FL', 'GA', 'MS', 'SC', 'TN']},
            'SER-CARIB': {'name': 'Southeast Region - Caribbean', 'states': ['PR', 'VI']},
            'GLR': {'name': 'Great Lakes Region', 'states': ['IL', 'IN', 'KY', 'MI', 'OH', 'WI']},
            'NCR': {'name': 'North Central Region', 'states': ['IA', 'KS', 'MN', 'MO', 'ND', 'NE', 'SD']},
            'RMR': {'name': 'Rocky Mountain Region', 'states': ['CO', 'ID', 'MT', 'UT', 'WY']},
            'SWR': {'name': 'Southwest Region', 'states': ['AR', 'AZ', 'LA', 'NM', 'OK', 'TX']},
            'PCR': {'name': 'Pacific Region', 'states': ['AK', 'CA', 'HI', 'NV', 'OR', 'WA', 'GU']},
            'PCR-WEST': {'name': 'Pacific Region - West Coast', 'states': ['CA', 'NV', 'OR', 'WA']},
            'PCR-HI': {'name': 'Pacific Region - Hawaii', 'states': ['HI']},
            'PCR-AK': {'name': 'Pacific Region - Alaska', 'states': ['AK']},
            'PCR-GUAM': {'name': 'Pacific Region - Guam', 'states': ['GU']},
        }


# =====================================================================
# Logging Utility
# =====================================================================

def _log(msg: str) -> None:
    """Thread-safe logging to stderr"""
    sys.stderr.write(f"[CAP_WINDS] {msg}\n")
    sys.stderr.flush()


# =====================================================================
# Distance Calculation Utilities
# =====================================================================

def haversine_distance(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """
    Calculate the great circle distance between two points in nautical miles
    
    Args:
        lon1, lat1: Coordinates of first point
        lon2, lat2: Coordinates of second point
        
    Returns:
        Distance in nautical miles
    """
    # Convert to radians
    lon1, lat1, lon2, lat2 = map(radians, [lon1, lat1, lon2, lat2])
    
    # Haversine formula
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    c = 2 * asin(sqrt(a))
    
    # Radius of earth in kilometers / 1.852 to get nautical miles
    km = 6371 * c
    nm = km / 1.852
    
    return nm


def calculate_radius_bounds(center_lat: float, center_lon: float, radius_nm: float) -> List[float]:
    """
    Calculate bounding box for a radius around a center point
    
    Args:
        center_lat: Center latitude
        center_lon: Center longitude
        radius_nm: Radius in nautical miles
        
    Returns:
        Bounding box [min_lon, max_lon, min_lat, max_lat]
    """
    # Convert radius to degrees (approximate)
    # 1 nautical mile = 1.852 km
    # At equator: 1 degree latitude ≈ 111 km
    km_radius = radius_nm * 1.852
    lat_delta = km_radius / 111.0
    
    # Longitude delta varies with latitude
    lon_delta = km_radius / (111.0 * abs(cos(radians(center_lat))))
    
    return [
        center_lon - lon_delta,  # min_lon
        center_lon + lon_delta,  # max_lon
        center_lat - lat_delta,  # min_lat
        center_lat + lat_delta,  # max_lat
    ]


# =====================================================================
# Data Classes
# =====================================================================

@dataclass
class WindData:
    """Container for HRRR wind analysis results"""
    max_wind_kts: np.ndarray
    lats: np.ndarray
    lons: np.ndarray
    cycle_date: str
    cycle_hour: str
    forecast_hours: range
    init_time: datetime
    
    
@dataclass
class MapSpec:
    """Specification for map generation"""
    location_name: str
    location_type: str
    location_code: str
    bounds: List[float]
    is_conus: bool
    primary_airport: Optional[str]
    dtg: str
    shape_prefix: str


# =====================================================================
# HRRR Data Management
# =====================================================================

class HRRRDataManager:
    """Manages HRRR forecast data retrieval and processing"""
    
    def __init__(self, config: Config):
        self.config = config
        
    def get_current_cycle(self) -> Tuple[datetime, datetime, str, str]:
        """Get current HRRR cycle information"""
        now_utc = datetime.utcnow()
        cycle_time = now_utc - timedelta(hours=2)
        cycle_date = cycle_time.strftime("%Y%m%d")
        cycle_hour = cycle_time.strftime("%H")
        return now_utc, cycle_time, cycle_date, cycle_hour
    
    def find_local_cycle_dir(self, cycle_date: str, cycle_hour: str) -> Optional[str]:
        """Find local HRRR cycle directory if it exists"""
        target_dir = os.path.join(self.config.MODEL_ROOT, f"hrrr.{cycle_date}", f"{cycle_hour}z")
        if os.path.isdir(target_dir):
            return target_dir
        
        if not os.path.isdir(self.config.MODEL_ROOT):
            return None
        
        try:
            for d in sorted(os.listdir(self.config.MODEL_ROOT), reverse=True):
                if not d.startswith("hrrr."):
                    continue
                date_dir = os.path.join(self.config.MODEL_ROOT, d)
                if not os.path.isdir(date_dir):
                    continue
                for hdir in sorted(os.listdir(date_dir), reverse=True):
                    if hdir.endswith("z"):
                        cand = os.path.join(date_dir, hdir)
                        if os.path.isdir(cand):
                            return cand
        except Exception as e:
            _log(f"Error finding local HRRR cycle: {e}")
        
        return None
    
    def download_grib_file(self, hour: int, cycle_date: str, cycle_hour: str, 
                          local_dir: str, base_url: Optional[str] = None) -> Optional[str]:
        """Download a single GRIB2 file"""
        os.makedirs(local_dir, exist_ok=True)
        fhour = f"{hour:02d}"
        filename = f"hrrr.t{cycle_hour}z.wrfsfcf{fhour}.grib2"
        local_path = os.path.join(local_dir, filename)
        
        # Use existing file if available
        if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
            _log(f"Using cached GRIB: {filename}")
            return local_path
        
        # Construct URL
        if base_url is None:
            base_url = f"{self.config.HRRR_BASE_URL}/hrrr.{cycle_date}/conus"
        url = f"{base_url}/{filename}"
        
        _log(f"Downloading GRIB: {filename}")
        try:
            r = requests.get(url, stream=True, timeout=120)
            r.raise_for_status()
            tmp = local_path + ".part"
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(8192):
                    if chunk:
                        f.write(chunk)
            os.replace(tmp, local_path)
            return local_path
        except Exception as e:
            _log(f"Failed to download {filename}: {e}")
            return None
    
    def download_grib_files_parallel(self, forecast_hours: range, cycle_date: str,
                                    cycle_hour: str, local_dir: str,
                                    base_url: Optional[str] = None,
                                    max_workers: int = 5) -> List[Optional[str]]:
        """Download multiple GRIB files in parallel"""
        _log(f"Downloading {len(forecast_hours)} GRIB files with {max_workers} workers")
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    self.download_grib_file, fh, cycle_date, cycle_hour, local_dir, base_url
                ): fh for fh in forecast_hours
            }
            
            results = {}
            for future in as_completed(futures):
                fh = futures[future]
                try:
                    results[fh] = future.result()
                except Exception as e:
                    _log(f"Error downloading forecast hour {fh}: {e}")
                    results[fh] = None
        
        # Return in original order
        return [results[fh] for fh in forecast_hours]
    
    def extract_wind_fields(self, grib_file: str) -> Tuple:
        """Extract wind fields from GRIB2 file"""
        if grib_file is None:
            return (None,) * 6
        
        try:
            # Open wind dataset (10m winds)
            ds_wind = xr.open_dataset(
                grib_file,
                engine="cfgrib",
                backend_kwargs={"filter_by_keys": {"typeOfLevel": "heightAboveGround", "level": 10}},
            )
            
            # Open gust dataset
            ds_gust = xr.open_dataset(
                grib_file,
                engine="cfgrib",
                backend_kwargs={"filter_by_keys": {"typeOfLevel": "surface", "stepType": "max"}},
            )
            
            # Find variable names
            wind_vars = list(ds_wind.data_vars)
            gust_vars = list(ds_gust.data_vars)
            uvar = next((v for v in wind_vars if "u" in v.lower()), None)
            vvar = next((v for v in wind_vars if "v" in v.lower()), None)
            gvar = next((v for v in gust_vars if "gust" in v.lower()), None)
            
            if uvar is None or vvar is None:
                _log(f"Wind components not found in {grib_file}")
                return (None,) * 6
            
            u10 = ds_wind[uvar]
            v10 = ds_wind[vvar]
            gust10 = ds_gust[gvar] if gvar else None
            time = pd.to_datetime(u10["time"].values)
            
            # Extract coordinates
            if "latitude" in u10.coords:
                lats = u10["latitude"].values
                lons = u10["longitude"].values
            elif "lat" in u10.coords:
                lats = u10["lat"].values
                lons = u10["lon"].values
            else:
                _log(f"Coordinates not found in {grib_file}")
                return (None,) * 6
            
            return time, lats, lons, u10.values, v10.values, (gust10.values if gust10 is not None else None)
            
        except Exception as e:
            _log(f"Error extracting wind fields from {grib_file}: {e}")
            _log(traceback.format_exc())
            return (None,) * 6
    
    def get_wind_analysis(self, forecast_hours: range = None,
                         parallel: bool = True) -> Optional[WindData]:
        """
        Get maximum wind analysis across forecast period
        
        Args:
            forecast_hours: Range of forecast hours to analyze
            parallel: Whether to download files in parallel
            
        Returns:
            WindData object with analysis results
        """
        if forecast_hours is None:
            forecast_hours = self.config.FORECAST_HOURS
        
        # Try current cycle, then fall back to previous cycles
        # HRRR runs hourly, try up to 6 hours back
        for hours_back in range(2, 9):  # Start at 2 (current logic), go to 8
            now_utc = datetime.utcnow()
            cycle_time = now_utc - timedelta(hours=hours_back)
            cycle_date = cycle_time.strftime("%Y%m%d")
            cycle_hour = cycle_time.strftime("%H")
            
            if hours_back == 2:
                _log(f"Attempting HRRR cycle: {cycle_date} {cycle_hour}Z")
            else:
                _log(f"Falling back to HRRR cycle: {cycle_date} {cycle_hour}Z (hours_back={hours_back})")
            
            # Determine data source
            local_cycle_dir = self.find_local_cycle_dir(cycle_date, cycle_hour)
            if local_cycle_dir:
                _log(f"SUCCESS: Using local HRRR data: {local_cycle_dir}")
                local_dir = local_cycle_dir
                base_url = None
            else:
                _log(f"No local HRRR data for {cycle_date} {cycle_hour}Z, trying remote source")
                local_dir = os.path.join(self.config.APP_DIR, "hrrr_data")
                base_url = f"{self.config.HRRR_BASE_URL}/hrrr.{cycle_date}/conus"
            
            # Download GRIB files
            if parallel:
                grib_files = self.download_grib_files_parallel(
                    forecast_hours, cycle_date, cycle_hour, local_dir, base_url
                )
            else:
                grib_files = [
                    self.download_grib_file(fh, cycle_date, cycle_hour, local_dir, base_url)
                    for fh in forecast_hours
                ]
            
            # Extract wind fields
            wind_speeds = []
            wind_gusts = []
            lats = None
            lons = None
            
            for grib_file in grib_files:
                if grib_file is None:
                    continue
                
                time, lats_tmp, lons_tmp, u10, v10, gust10 = self.extract_wind_fields(grib_file)
                if time is None:
                    continue
                
                if lats is None:
                    lats = lats_tmp
                    lons = lons_tmp
                
                wspd = np.sqrt(u10 ** 2 + v10 ** 2)
                wind_speeds.append(wspd)
                
                if gust10 is not None:
                    wind_gusts.append(gust10)
            
            # Check if we got enough data
            if len(wind_speeds) >= 3:  # Need at least 3 files for analysis
                _log(f"SUCCESS: Got {len(wind_speeds)} HRRR wind fields for cycle {cycle_date} {cycle_hour}Z")
                break
            else:
                _log(f"Insufficient HRRR data for cycle {cycle_date} {cycle_hour}Z "
                     f"(got {len(wind_speeds)} files, need at least 3), trying previous cycle")
                continue
        else:
            # No valid cycles found
            raise RuntimeError(
                "No valid HRRR data retrieved after checking 7 cycles (6 hours back). "
                "Check LDM NGRID feed and pqact_ngrid.conf configuration."
            )
        
        # Calculate maximum winds
        wind_speeds = np.array(wind_speeds)
        if len(wind_gusts) > 0:
            wind_gusts = np.array(wind_gusts)
            max_wind_total = np.maximum(
                np.nanmax(wind_speeds, axis=0),
                np.nanmax(wind_gusts, axis=0)
            )
        else:
            max_wind_total = np.nanmax(wind_speeds, axis=0)
        
        # Convert to knots
        max_wind_kts = max_wind_total * 1.944
        
        return WindData(
            max_wind_kts=max_wind_kts,
            lats=lats,
            lons=lons,
            cycle_date=cycle_date,
            cycle_hour=cycle_hour,
            forecast_hours=forecast_hours,
            init_time=cycle_time
        )


# =====================================================================
# Airport Management
# =====================================================================

class AirportManager:
    """Manages airport data retrieval and filtering"""
    
    def __init__(self, config: Config):
        self.config = config
        self._airport_cache = None
        self._runway_cache = None
    
    def _load_airport_data(self) -> pd.DataFrame:
        """Load airport data with caching"""
        if self._airport_cache is None:
            _log("Downloading airport data...")
            self._airport_cache = pd.read_csv(self.config.AIRPORT_URL)
        return self._airport_cache
    
    def _load_runway_data(self) -> pd.DataFrame:
        """Load runway data with caching"""
        if self._runway_cache is None:
            _log("Downloading runway data...")
            self._runway_cache = pd.read_csv(self.config.RUNWAY_URL)
        return self._runway_cache
    
    def lookup_airport_coordinates(self, airport_code: str) -> Optional[Tuple[float, float, str]]:
        """
        Look up airport coordinates by ICAO, GPS, or IATA code
        
        Args:
            airport_code: Airport identifier (ICAO, GPS code, or IATA)
            
        Returns:
            Tuple of (latitude, longitude, full_name) or None if not found
        """
        df_airports = self._load_airport_data()
        airport_code = airport_code.upper().strip()
        
        # Try matching against different identifier types
        matches = df_airports[
            (df_airports["gps_code"] == airport_code) |
            (df_airports["ident"] == airport_code) |
            (df_airports["iata_code"] == airport_code)
        ]
        
        if len(matches) > 0:
            row = matches.iloc[0]
            return (
                row["latitude_deg"],
                row["longitude_deg"],
                row["name"]
            )
        
        return None
    
    def filter_airports_by_radius(self, center_lat: float, center_lon: float,
                                  radius_nm: float) -> pd.DataFrame:
        """
        Filter airports within radius of a center point
        
        Args:
            center_lat: Center point latitude
            center_lon: Center point longitude
            radius_nm: Radius in nautical miles
            
        Returns:
            Filtered DataFrame of airports
        """
        df_airports = self._load_airport_data()
        
        # Filter to US and US territories (PR, VI, GU use their own ISO codes)
        filtered = df_airports[
            (
                (df_airports["iso_country"] == "US") |
                (df_airports["iso_country"].isin(["PR", "VI", "GU"]))
            ) &
            (df_airports["type"].isin(["large_airport", "medium_airport", "small_airport"]))
        ].copy()
        
        # Calculate distance for each airport
        distances = []
        for _, row in filtered.iterrows():
            dist = haversine_distance(
                center_lon, center_lat,
                row["longitude_deg"], row["latitude_deg"]
            )
            distances.append(dist)
        
        filtered["distance_nm"] = distances
        
        # Filter to within radius
        filtered = filtered[filtered["distance_nm"] <= radius_nm].copy()
        
        _log(f"Found {len(filtered)} airports within {radius_nm}nm of ({center_lat:.4f}, {center_lon:.4f})")
        
        return filtered
    
    def filter_airports_by_location(self, location_type: str, location_code: str,
                                    center_coords: Optional[Tuple[float, float]] = None) -> pd.DataFrame:
        """Filter airports by location type and code"""
        
        if location_type == "radius":
            if center_coords is None:
                raise ValueError("center_coords required for radius location type")
            center_lat, center_lon = center_coords
            filtered = self.filter_airports_by_radius(center_lat, center_lon, 50.0)
            
        elif location_type == "state":
            df_airports = self._load_airport_data()
            
            # US territories (PR, VI, GU) use their own ISO country codes
            # not "US" like the 50 states
            if location_code in ["PR", "VI", "GU"]:
                filtered = df_airports[
                    (df_airports["iso_country"] == location_code)
                ].copy()
            else:
                filtered = df_airports[
                    (df_airports["iso_country"] == "US") &
                    (df_airports["iso_region"] == f"US-{location_code}")
                ].copy()
            
        elif location_type == "conus":
            df_airports = self._load_airport_data()
            conus_states = [
                code for code in self.config.STATE_BOUNDARIES.keys()
                if code not in ["AK", "HI", "PR", "VI", "GU"]
            ]
            conus_state_codes = [f"US-{s}" for s in conus_states]
            filtered = df_airports[
                (df_airports["iso_country"] == "US") &
                (df_airports["iso_region"].isin(conus_state_codes))
            ].copy()
            
        elif location_type == "region":
            df_airports = self._load_airport_data()
            region_states = self.config.CAP_REGIONS[location_code]["states"]
            
            # Separate US states from territories
            us_states = [s for s in region_states if s not in ["PR", "VI", "GU"]]
            territories = [s for s in region_states if s in ["PR", "VI", "GU"]]
            
            # Build filter conditions
            conditions = []
            
            # Add US states filter
            if us_states:
                us_state_codes = [f"US-{s}" for s in us_states]
                conditions.append(
                    (df_airports["iso_country"] == "US") &
                    (df_airports["iso_region"].isin(us_state_codes))
                )
            
            # Add territories filter (they use their own ISO country codes)
            if territories:
                conditions.append(
                    df_airports["iso_country"].isin(territories)
                )
            
            # Combine conditions with OR
            if len(conditions) == 1:
                filtered = df_airports[conditions[0]].copy()
            elif len(conditions) > 1:
                combined = conditions[0]
                for cond in conditions[1:]:
                    combined = combined | cond
                filtered = df_airports[combined].copy()
            else:
                # No valid states/territories
                filtered = df_airports[df_airports["iso_country"] == "INVALID"].copy()
        else:
            raise ValueError(f"Unknown location_type: {location_type}")
        
        # Filter by airport type (except radius which already filtered)
        if location_type != "radius":
            filtered = filtered[
                filtered["type"].isin(["large_airport", "medium_airport", "small_airport"])
            ]
        
        _log(f"Airports before runway filter: {len(filtered)}")
        return filtered
    
    def filter_by_runway_requirements(self, airports_df: pd.DataFrame) -> pd.DataFrame:
        """Filter airports by runway requirements"""
        dfrunways = self._load_runway_data()
        
        paved_surfaces = ["ASP", "ASPH", "CON", "CONC", "concrete", "asphalt"]
        paved_runways = dfrunways[
            (dfrunways["surface"].isin(paved_surfaces)) &
            (dfrunways["length_ft"] >= self.config.MIN_RUNWAY_LENGTH)
        ]
        
        qualifying_ids = paved_runways["airport_ident"].unique()
        filtered = airports_df[airports_df["ident"].isin(qualifying_ids)].copy()
        
        _log(f"Airports with paved runways ≥{self.config.MIN_RUNWAY_LENGTH}ft: {len(filtered)}")
        return filtered
    
    def interpolate_winds(self, airports_df: pd.DataFrame, wind_data: WindData) -> pd.DataFrame:
        """Interpolate wind values to airport locations"""
        if airports_df is None or len(airports_df) == 0:
            return airports_df
        
        # Prepare interpolator
        if wind_data.lats.ndim == 2:
            lat_1d = wind_data.lats[:, 0]
            lon_1d = wind_data.lons[0, :]
        else:
            lat_1d = wind_data.lats
            lon_1d = wind_data.lons
        
        interp = RegularGridInterpolator(
            (lat_1d, lon_1d),
            wind_data.max_wind_kts,
            bounds_error=False,
            fill_value=np.nan,
        )
        
        # Interpolate winds at each airport
        winds = []
        for _, row in airports_df.iterrows():
            lat = row["latitude_deg"]
            lon = row["longitude_deg"]
            
            # Handle longitude wrapping
            if lon < 0 and lon_1d.min() > 0:
                lon += 360
            elif lon > 180 and lon_1d.max() < 180:
                lon -= 360
            
            try:
                winds.append(interp((lat, lon)))
            except Exception:
                winds.append(np.nan)
        
        airports_df["max_wind_kts"] = winds
        return airports_df
    
    def classify_airport_status(self, airports_df: pd.DataFrame) -> pd.DataFrame:
        """Classify airport operational status based on wind speeds"""
        # Handle empty dataframe or missing max_wind_kts column
        if len(airports_df) == 0:
            _log("No airports to classify (empty dataframe)")
            return airports_df
        
        if "max_wind_kts" not in airports_df.columns:
            _log("WARNING: max_wind_kts column missing, cannot classify airports")
            airports_df["status"] = "Unknown"
            return airports_df
        
        airports_df["status"] = "Normal"
        airports_df.loc[
            airports_df["max_wind_kts"] >= self.config.CAUTION_WIND, "status"
        ] = "Caution"
        airports_df.loc[
            airports_df["max_wind_kts"] >= self.config.MAX_OPERATIONAL_WIND, "status"
        ] = "Out of Limits"
        
        _log(
            f"Airport status - "
            f"Normal: {len(airports_df[airports_df['status']=='Normal'])}, "
            f"Caution: {len(airports_df[airports_df['status']=='Caution'])}, "
            f"Out of Limits: {len(airports_df[airports_df['status']=='Out of Limits'])}"
        )
        
        return airports_df
    
    def get_filtered_airports(self, location_type: str, location_code: str,
                            wind_data: WindData,
                            center_coords: Optional[Tuple[float, float]] = None) -> pd.DataFrame:
        """
        Complete airport filtering and wind interpolation pipeline
        
        Returns:
            DataFrame with filtered airports and wind data
        """
        airports = self.filter_airports_by_location(location_type, location_code, center_coords)
        airports = self.filter_by_runway_requirements(airports)
        airports = self.interpolate_winds(airports, wind_data)
        airports = self.classify_airport_status(airports)
        return airports


# =====================================================================
# Airport Labeling
# =====================================================================

class AirportLabeler:
    """Handles airport priority calculation and label selection"""
    
    MILITARY_KEYWORDS = [
        "AFB", "AIR FORCE BASE", "AIR FORCE", "NAVAL", "NAS ",
        "MCAS", "ARMY", "MILITARY", "JOINT BASE", "AIR STATION",
        "ANGB", "AIR NATIONAL GUARD", "COAST GUARD", "USAF",
        "USAFE", "SPACE FORCE", "SPACEPORT", "NATIONAL GUARD",
        "RESERVE", "FIELD AAF", "ARMY AIRFIELD", "NAVY",
        "MARINE CORPS",
    ]
    
    @classmethod
    def calculate_priorities(cls, airports_df: pd.DataFrame,
                           primary_airport: Optional[str] = None) -> pd.DataFrame:
        """Calculate display priority for each airport"""
        airports_df = airports_df.copy()
        airports_df["priority"] = 4  # Default: small airport
        
        # Medium airports
        airports_df.loc[airports_df["type"] == "medium_airport", "priority"] = 3
        
        # Large airports
        airports_df.loc[airports_df["type"] == "large_airport", "priority"] = 2
        
        # Military installations
        for idx, row in airports_df.iterrows():
            name_upper = str(row["name"]).upper()
            keywords_upper = str(row.get("keywords", "")).upper()
            combined = name_upper + " " + keywords_upper
            
            for keyword in cls.MILITARY_KEYWORDS:
                if keyword in combined:
                    airports_df.at[idx, "priority"] = 1
                    break
        
        # Primary airport (highest priority)
        if primary_airport:
            primary_matches = airports_df[
                (airports_df["gps_code"] == primary_airport) |
                (airports_df["ident"] == primary_airport) |
                (airports_df["iata_code"] == primary_airport)
            ]
            if len(primary_matches) > 0:
                airports_df.loc[primary_matches.index, "priority"] = 0
        
        return airports_df
    
    @staticmethod
    def determine_max_labels(total_airports: int, is_conus: bool,
                           num_high_priority: int) -> int:
        """Determine maximum number of labels to display"""
        if is_conus:
            # CONUS map: 70% of high-priority airports, minimum 30
            target = int(num_high_priority * 0.7)
            return max(30, target)
        
        # State/region maps: scale with total airports
        if total_airports <= 10:
            max_labels = total_airports
        elif total_airports <= 20:
            max_labels = total_airports
        elif total_airports <= 30:
            max_labels = 25
        elif total_airports <= 50:
            max_labels = 35
        elif total_airports <= 100:
            max_labels = 50
        else:
            max_labels = 60
        
        # Ensure all high-priority airports are labeled
        return max(max_labels, num_high_priority)
    
    @staticmethod
    def get_label_style(priority: int) -> Dict:
        """Get label styling based on priority"""
        styles = {
            0: {"fontsize": 9, "fontweight": "bold", "color": "darkred"},     # Primary
            1: {"fontsize": 8, "fontweight": "bold", "color": "darkblue"},   # Military
            2: {"fontsize": 8, "fontweight": "bold", "color": "black"},      # Large
            3: {"fontsize": 7, "fontweight": "normal", "color": "black"},    # Medium
            4: {"fontsize": 7, "fontweight": "normal", "color": "dimgray"},  # Small
        }
        return styles.get(priority, styles[4])


# =====================================================================
# Shapefile Export
# =====================================================================

class ShapefileExporter:
    """Handles shapefile export operations"""
    
    @staticmethod
    def _get_shapefile_components(base_path: str) -> List[str]:
        """Get all component files for a shapefile"""
        base = os.path.splitext(base_path)[0]
        extensions = ['.shp', '.shx', '.dbf', '.prj', '.cpg']
        components = []
        
        for ext in extensions:
            component = base + ext
            if os.path.exists(component):
                components.append(component)
        
        return components
    
    @staticmethod
    def create_shapefile_zip(shapefile_path: str, zip_path: Optional[str] = None) -> Optional[str]:
        """
        Create a zip file containing all shapefile components
        
        Args:
            shapefile_path: Path to the .shp file
            zip_path: Optional custom zip path, defaults to shapefile_path with .zip extension
            
        Returns:
            Path to created zip file, or None if shapefile doesn't exist
        """
        if not os.path.exists(shapefile_path):
            _log(f"Shapefile not found: {shapefile_path}")
            return None
        
        if zip_path is None:
            zip_path = os.path.splitext(shapefile_path)[0] + '.zip'
        
        components = ShapefileExporter._get_shapefile_components(shapefile_path)
        
        if not components:
            _log(f"No shapefile components found for: {shapefile_path}")
            return None
        
        _log(f"Creating shapefile zip: {zip_path} with {len(components)} files")
        
        try:
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for component in components:
                    arcname = os.path.basename(component)
                    zf.write(component, arcname)
                    _log(f"  Added: {arcname}")
            
            return zip_path
            
        except Exception as e:
            _log(f"Error creating shapefile zip: {e}")
            return None
    
    @staticmethod
    def export_airports(airports_df: pd.DataFrame, shapefile_path: str) -> Optional[str]:
        """
        Export airports to shapefile and create zip
        
        Returns:
            Path to zip file, or None if export failed
        """
        if airports_df is None or len(airports_df) == 0:
            _log("No airports to export")
            return None
        
        os.makedirs(os.path.dirname(shapefile_path), exist_ok=True)
        
        gdf = gpd.GeoDataFrame(
            airports_df.copy(),
            geometry=[
                Point(lon, lat)
                for lon, lat in zip(airports_df["longitude_deg"], airports_df["latitude_deg"])
            ],
            crs="EPSG:4326",
        )
        
        cols = [
            "ident", "gps_code", "iata_code", "name", "iso_region",
            "type", "max_wind_kts", "status",
        ]
        cols = [c for c in cols if c in gdf.columns]
        gdf = gdf[cols + ["geometry"]]
        
        _log(f"Exporting airport shapefile: {shapefile_path}")
        gdf.to_file(shapefile_path, driver="ESRI Shapefile")
        
        # Create zip file
        return ShapefileExporter.create_shapefile_zip(shapefile_path)
    
    @staticmethod
    def export_contour_polygons(contour_set, shapefile_path: str) -> Optional[str]:
        """
        Export contour polygons to shapefile and create zip
        
        Returns:
            Path to zip file, or None if export failed
        """
        if not hasattr(contour_set, "levels") or not hasattr(contour_set, "allsegs"):
            _log(f"Invalid contour set for polygon export")
            return None
        
        os.makedirs(os.path.dirname(shapefile_path), exist_ok=True)
        
        schema = {
            "geometry": "Polygon",
            "properties": {"level_min": "float", "level_max": "float"},
        }
        
        levels = list(contour_set.levels)
        with fiona.open(shapefile_path, "w", driver="ESRI Shapefile",
                       schema=schema, crs=from_epsg(4326)) as dst:
            for i, seglist in enumerate(contour_set.allsegs):
                if i == 0:
                    lvl_min, lvl_max = float("-inf"), float(levels[0])
                elif i == len(contour_set.allsegs) - 1:
                    lvl_min, lvl_max = float(levels[-1]), float("inf")
                else:
                    lvl_min, lvl_max = float(levels[i]), float(levels[i + 1])
                
                for poly_coords in seglist:
                    try:
                        poly = Polygon(poly_coords)
                        if poly.is_valid and not poly.is_empty:
                            dst.write({
                                "geometry": geom_mapping(poly),
                                "properties": {"level_min": lvl_min, "level_max": lvl_max},
                            })
                    except Exception:
                        continue
        
        _log(f"Exported contour polygons: {shapefile_path}")
        
        # Create zip file
        return ShapefileExporter.create_shapefile_zip(shapefile_path)
    
    @staticmethod
    def export_contour_lines(contour_set, shapefile_path: str) -> Optional[str]:
        """
        Export contour lines to shapefile and create zip
        
        Returns:
            Path to zip file, or None if export failed
        """
        if not hasattr(contour_set, "levels") or not hasattr(contour_set, "allsegs"):
            _log(f"Invalid contour set for line export")
            return None
        
        os.makedirs(os.path.dirname(shapefile_path), exist_ok=True)
        
        schema = {"geometry": "LineString", "properties": {"level": "float"}}
        
        with fiona.open(shapefile_path, "w", driver="ESRI Shapefile",
                       schema=schema, crs=from_epsg(4326)) as dst:
            for level, seglist in zip(contour_set.levels, contour_set.allsegs):
                for line_coords in seglist:
                    if len(line_coords) >= 2:
                        line = LineString(line_coords)
                        if not line.is_empty:
                            dst.write({
                                "geometry": geom_mapping(line),
                                "properties": {"level": float(level)},
                            })
        
        _log(f"Exported contour lines: {shapefile_path}")
        
        # Create zip file
        return ShapefileExporter.create_shapefile_zip(shapefile_path)


# =====================================================================
# Wind Map Builder
# =====================================================================

"""
CAP Winds HRRR Map Generator - Part 2 (Continuation)
APPEND THIS TO THE END OF PART 1

This is the continuation of states_service.py v5
Contains: WindMapBuilder, WindAnalysisService, and run_analysis function
"""

# =====================================================================
# Wind Map Builder (CONTINUED FROM PART 1)
# =====================================================================

class WindMapBuilder:
    """Generates wind analysis maps"""

    def __init__(self, config: Config):
        self.config = config
        self.exporter = ShapefileExporter()

    def create_map(self, map_spec: MapSpec, wind_data: WindData,
                  airports_df: pd.DataFrame,
                  export_shapefiles: Dict[str, str] = None) -> plt.Figure:
        """
        Create complete wind analysis map

        Args:
            map_spec: Map specification
            wind_data: Wind analysis data
            airports_df: Airports with wind data
            export_shapefiles: Dict with shapefile paths (airports, contour_poly, contour_line)

        Returns:
            Matplotlib figure
        """
        _log(f"Creating map: {map_spec.location_name}")

        # Create figure and axes
        fig = plt.figure(figsize=(20, 12))
        ax = plt.axes([0.1, 0.1, 0.7, 0.8], projection=ccrs.PlateCarree())

        # Add base features
        self._add_base_features(ax)

        # Add wind contours and export shapefiles
        cf_display = self._add_wind_contours(
            ax, wind_data, map_spec.bounds, export_shapefiles
        )

        # Add airports
        self._add_airports(ax, airports_df, map_spec)

        # Add annotations
        self._add_annotations(fig, ax, map_spec, wind_data, cf_display)

        return fig

    def _add_base_features(self, ax) -> None:
        """Add geographic base features"""
        ax.add_feature(cfeature.STATES, linewidth=1.0, edgecolor="black")
        ax.add_feature(cfeature.COASTLINE, linewidth=0.8)
        ax.coastlines("50m", linewidth=0.8)
        try:
            ax.add_feature(cfeature.BORDERS, linewidth=0.5, edgecolor="gray")
        except Exception:
            pass

    def _add_wind_contours(self, ax, wind_data: WindData, bounds: List[float],
                          export_paths: Optional[Dict[str, str]] = None):
        """Add wind contours and optionally export shapefiles"""
        levels = np.arange(0, 65, 5)

        # Create contours in data space for shapefile export
        if export_paths:
            # Create temporary figure for shapefile export (no projection)
            temp_fig, temp_ax = plt.subplots()

            cf_data = temp_ax.contourf(
                wind_data.lons, wind_data.lats, wind_data.max_wind_kts,
                levels=levels, cmap="YlOrRd"
            )
            cs_data = temp_ax.contour(
                wind_data.lons, wind_data.lats, wind_data.max_wind_kts,
                levels=levels, colors="none"
            )

            # Export shapefiles
            if "contour_poly" in export_paths:
                self.exporter.export_contour_polygons(cf_data, export_paths["contour_poly"])
            if "contour_line" in export_paths:
                self.exporter.export_contour_lines(cs_data, export_paths["contour_line"])

            # Close temporary figure
            plt.close(temp_fig)

        # Create display contours with Cartopy projection
        cf = ax.contourf(
            wind_data.lons, wind_data.lats, wind_data.max_wind_kts,
            levels=levels, cmap="YlOrRd",
            transform=ccrs.PlateCarree(),
            extend="max", alpha=0.6,
        )

        # Add critical wind contours
        cs_caution = ax.contour(
            wind_data.lons, wind_data.lats, wind_data.max_wind_kts,
            levels=[self.config.CAUTION_WIND],
            colors="orange", linewidths=2,
            transform=ccrs.PlateCarree(),
        )
        ax.clabel(cs_caution, inline=True, fontsize=10,
                 fmt=f"{self.config.CAUTION_WIND} kts")

        cs_max = ax.contour(
            wind_data.lons, wind_data.lats, wind_data.max_wind_kts,
            levels=[self.config.MAX_OPERATIONAL_WIND],
            colors="red", linewidths=3,
            transform=ccrs.PlateCarree(),
        )
        ax.clabel(cs_max, inline=True, fontsize=12,
                 fmt=f"{self.config.MAX_OPERATIONAL_WIND} kts")

        return cf

    def _add_airports(self, ax, airports_df: pd.DataFrame, map_spec: MapSpec) -> None:
        """Add airports to map with appropriate labeling"""
        if airports_df is None or len(airports_df) == 0:
            return

        # Calculate priorities
        airports_df = AirportLabeler.calculate_priorities(
            airports_df, map_spec.primary_airport
        )

        # Plot airport markers by status
        for status, color, size, marker in [
            ("Normal", "green", 25, "o"),
            ("Caution", "orange", 40, "^"),
            ("Out of Limits", "red", 60, "X"),
        ]:
            subset = airports_df[airports_df["status"] == status]
            if len(subset) > 0:
                ax.scatter(
                    subset["longitude_deg"], subset["latitude_deg"],
                    c=color, s=size, marker=marker, alpha=0.8,
                    transform=ccrs.PlateCarree(),
                    label=f"{status} ({len(subset)})",
                    edgecolors="black", linewidths=0.7, zorder=5,
                )

        # Add labels
        self._add_airport_labels(ax, airports_df, map_spec.is_conus)

        # Add legend
        legend = ax.legend(
            loc="upper left",
            bbox_to_anchor=(1.05, 1.0),
            fontsize=11,
            framealpha=0.95,
            title="Airport Status",
        )
        legend.get_title().set_fontsize(12)
        legend.get_title().set_fontweight("bold")

    def _add_airport_labels(self, ax, airports_df: pd.DataFrame, is_conus: bool) -> None:
        """Add labels to selected airports"""
        airports_sorted = airports_df.sort_values("priority")
        total_airports = len(airports_df)

        # Determine how many labels to show
        high_priority = airports_sorted[airports_sorted["priority"] <= 2]
        max_labels = AirportLabeler.determine_max_labels(
            total_airports, is_conus, len(high_priority)
        )

        # Add labels
        labeled_count = 0
        for _, row in airports_sorted.iterrows():
            if is_conus and row["priority"] > 2:
                continue
            if labeled_count >= max_labels:
                break

            label = row["gps_code"] if pd.notna(row["gps_code"]) else row["ident"]
            style = AirportLabeler.get_label_style(row["priority"])

            ax.text(
                row["longitude_deg"] + 0.05,
                row["latitude_deg"] + 0.05,
                label,
                fontsize=style["fontsize"],
                fontweight=style["fontweight"],
                color=style["color"],
                transform=ccrs.PlateCarree(),
                ha="left", va="bottom", zorder=6,
                path_effects=[path_effects.withStroke(linewidth=2, foreground="white")],
            )
            labeled_count += 1

    def _add_annotations(self, fig, ax, map_spec: MapSpec,
                        wind_data: WindData, cf) -> None:
        """Add title, colorbar, gridlines, and info box"""
        # Set map extent
#        padding = 0.5
        if map_spec.location_type == "radius":
           padding = 0.1
        else:
           padding = 0.5
        ax.set_extent(
            [
                map_spec.bounds[0] - padding,

                map_spec.bounds[1] + padding,
                map_spec.bounds[2] - padding,
                map_spec.bounds[3] + padding,
            ],
            crs=ccrs.PlateCarree(),
        )

        # Add gridlines
        gl = ax.gridlines(
            draw_labels=True,
            linewidth=0.5,
            color="gray",
            alpha=0.5,
            linestyle="--",
        )
        gl.top_labels = False
        gl.right_labels = False

        # Add colorbar
        cbar_ax = fig.add_axes([0.15, 0.05, 0.5, 0.02])
        cbar = plt.colorbar(cf, cax=cbar_ax, orientation="horizontal")
        cbar.set_label("Maximum Wind Speed (kts)", fontsize=12)

        # Add title
        plt.suptitle(
            f"CAP Aircraft Wind Constraint Analysis - {map_spec.location_name}\n"
            f"Init: {wind_data.init_time.strftime('%Y-%m-%d %H:%M')} UTC | "
            f"12-Hour Max Winds (Hours {wind_data.forecast_hours.start}-{wind_data.forecast_hours.stop - 1}) | "
            f"Data: NOAA HRRR (US Gov't)\n"
            f"Airports: Paved runways ≥ {self.config.MIN_RUNWAY_LENGTH} ft",
            fontsize=14,
            fontweight="bold",
            y=0.95,
        )

        # Add info box
        textstr = (
            f"CAP Wind Limits\n(CAPR 70-1):\n\n"
            f"≤ {self.config.CAUTION_WIND} kts:\nNormal operations\n\n"
            f"> {self.config.CAUTION_WIND} kts:\nCaution\n\n"
            f"> {self.config.MAX_OPERATIONAL_WIND} kts:\nRequires SFRO +\n"
            f"Wing Commander\napproval"
        )
        text_ax = fig.add_axes([0.82, 0.15, 0.16, 0.35])
        text_ax.axis("off")
        text_ax.text(
            0.5, 0.5, textstr,
            transform=text_ax.transAxes,
            fontsize=10,
            va="center",
            ha="center",
            bbox=dict(
                boxstyle="round",
                facecolor="wheat",
                alpha=0.95,
                edgecolor="black",
                linewidth=1,
            ),
        )


# =====================================================================
# Main Service
# =====================================================================

class WindAnalysisService:
    """Main orchestrator for wind analysis workflow"""

    def __init__(self, config: Config = None):
        self.config = config or Config()
        self.hrrr_manager = HRRRDataManager(self.config)
        self.airport_manager = AirportManager(self.config)
        self.map_builder = WindMapBuilder(self.config)
        self.exporter = ShapefileExporter()

    def _create_map_spec(self, location_type: str, location_code: str,
                        primary_airport: Optional[str],
                        center_coords: Optional[Tuple[float, float]] = None,
                        center_name: Optional[str] = None) -> MapSpec:
        """Create map specification from location parameters"""
        location_code = location_code.upper() if location_code else ""

        # Determine location details
        if location_type == "radius":
            if center_coords is None:
                raise ValueError("center_coords required for radius type")
    
            center_lat, center_lon = center_coords
            search_radius_nm = 50.0  # Search radius for airports
            buffer_nm = 25.0         # Buffer around search area
            total_extent_nm = search_radius_nm + buffer_nm  # 75nm from center
    
            location_name = f"50nm Radius: {center_name or f'{center_lat:.4f}, {center_lon:.4f}'}"
            bounds = calculate_radius_bounds(center_lat, center_lon, total_extent_nm)
            is_conus = False

        elif location_type == "conus":
            location_name = "Continental United States"
            bounds = self.config.CONUS_BOUNDS
            is_conus = True

        elif location_type == "state":
            if location_code not in self.config.STATE_BOUNDARIES:
                raise ValueError(f"Unknown state code: {location_code}")
            state_info = self.config.STATE_BOUNDARIES[location_code]
            location_name = state_info["name"]
            bounds = state_info["bounds"]
            is_conus = False

        elif location_type == "region":
            if location_code not in self.config.CAP_REGIONS:
                raise ValueError(f"Unknown region code: {location_code}")
            region_info = self.config.CAP_REGIONS[location_code]
            location_name = region_info["name"]

            # Calculate bounds from member states
            state_bounds = [
                self.config.STATE_BOUNDARIES[s]["bounds"]
                for s in region_info["states"]
            ]
            bounds = [
                min(b[0] for b in state_bounds),
                max(b[1] for b in state_bounds),
                min(b[2] for b in state_bounds),
                max(b[3] for b in state_bounds),
            ]
            is_conus = False
        else:
            raise ValueError(f"Unknown location_type: {location_type}")

        # Generate DTG (Date-Time Group)
        _, cycle_time, _, _ = self.hrrr_manager.get_current_cycle()
        dtg = cycle_time.strftime("%d%H%M") + "Z" + cycle_time.strftime("%b%y").upper()

        # Generate filename prefix
        if location_type == "radius":
            safe_name = center_name.replace(" ", "_")[:20] if center_name else "POINT"
            shape_prefix = f"cap_wind_50nm_{safe_name}_{dtg}"
        elif location_type == "conus":
            shape_prefix = f"cap_wind_CONUS_{dtg}"
        else:
            shape_prefix = f"cap_wind_{location_code}_{dtg}"

        return MapSpec(
            location_name=location_name,
            location_type=location_type,
            location_code=location_code,
            bounds=bounds,
            is_conus=is_conus,
            primary_airport=primary_airport,
            dtg=dtg,
            shape_prefix=shape_prefix,
        )

    def generate_analysis(self, location_type: str, location_code: str = "",
                         primary_airport: Optional[str] = None,
                         center_point: Optional[str] = None,
                         center_lat: Optional[float] = None,
                         center_lon: Optional[float] = None,
                         progress_callback: Optional[Callable] = None) -> List[Dict]:
        """
        Generate complete wind analysis

        Args:
            location_type: 'state', 'region', 'conus', or 'radius'
            location_code: State/region code (not used for radius)
            primary_airport: Optional ICAO code for primary airport
            center_point: For radius type: airport code or description
            center_lat: For radius type: center latitude
            center_lon: For radius type: center longitude
            progress_callback: Optional callback(step, total, message)

        Returns:
            List of dictionaries with map information
        """
        try:
            # Handle radius type - resolve coordinates
            center_coords = None
            center_name = None

            if location_type == "radius":
                # Try to look up airport first
                if center_point:
                    coords_result = self.airport_manager.lookup_airport_coordinates(center_point)
                    if coords_result:
                        center_lat, center_lon, center_name = coords_result
                        _log(f"Found airport {center_point}: {center_name} at ({center_lat:.4f}, {center_lon:.4f})")
                    else:
                        # Not an airport, use as name if coordinates provided
                        center_name = center_point

                # Validate coordinates
                if center_lat is None or center_lon is None:
                    if center_point:
                        raise ValueError(f"Airport code '{center_point}' not found and no coordinates provided")
                    else:
                        raise ValueError("For radius type, provide either airport code or lat/lon coordinates")

                center_coords = (center_lat, center_lon)
                if not center_name:
                    center_name = f"Point ({center_lat:.4f}, {center_lon:.4f})"

            # Step 1: Fetch forecast data (HRRR for CONUS, GFS for OCONUS)
            if progress_callback:
                progress_callback(1, 5, "Fetching forecast data...")
            _log("Step 1/5: Fetching model data")
            
            # Determine data source based on location (and coordinates for radius queries)
            if GFSDataManager.requires_gfs(location_code, center_coords):
                _log(f"Using GFS data for {location_code or 'radius query'} (OCONUS: no HRRR coverage)")
                gfs_manager = GFSDataManager(self.config)
                wind_data = gfs_manager.get_wind_analysis()
            else:
                _log(f"Using HRRR data for {location_code or 'radius query'} (CONUS coverage)")
                wind_data = self.hrrr_manager.get_wind_analysis(parallel=True)

            # Step 2: Create map specification
            if progress_callback:
                progress_callback(2, 5, "Preparing map configuration...")
            _log("Step 2/5: Creating map specification")
            map_spec = self._create_map_spec(
                location_type, location_code, primary_airport,
                center_coords, center_name
            )

            # Step 3: Get and filter airports
            if progress_callback:
                progress_callback(3, 5, "Filtering and analyzing airports...")
            _log("Step 3/5: Processing airports")
            airports_df = self.airport_manager.get_filtered_airports(
                location_type, location_code, wind_data, center_coords
            )

            # Step 4: Generate map
            if progress_callback:
                progress_callback(4, 5, "Generating map visualization...")
            _log("Step 4/5: Creating map")

            # Setup shapefile export paths
            shapefile_paths = {
                "airports": os.path.join(
                    self.config.SHAPE_OUTPUT_DIR,
                    f"{map_spec.shape_prefix}_airports.shp"
                ),
                "contour_poly": os.path.join(
                    self.config.SHAPE_OUTPUT_DIR,
                    f"{map_spec.shape_prefix}_contours_poly.shp"
                ),
                "contour_line": os.path.join(
                    self.config.SHAPE_OUTPUT_DIR,
                    f"{map_spec.shape_prefix}_contours_line.shp"
                ),
            }

            # Export airport shapefile and create zip
            airport_zip = self.exporter.export_airports(airports_df, shapefile_paths["airports"])

            # Create map with shapefile exports
            fig = self.map_builder.create_map(
                map_spec, wind_data, airports_df,
                export_shapefiles=shapefile_paths
            )

            # Step 5: Save outputs
            if progress_callback:
                progress_callback(5, 5, "Saving outputs...")
            _log("Step 5/5: Saving outputs")

            png_filename = f"{map_spec.shape_prefix}.png"
            png_path = os.path.join(self.config.WEB_OUTPUT_DIR, png_filename)
            _log(f"Saving map to {png_path}")
            fig.savefig(png_path, dpi=150, bbox_inches="tight")
            plt.close(fig)

            # Prepare result with zip file URLs
            result = {
                "map_name": map_spec.location_name,
                "filename": png_path,
                "url": f"/cap_winds/{png_filename}",
            }

            # Add shapefile zip URLs if they were created
            if airport_zip:
                result["airport_shp_url"] = f"/cap_winds_shp/{os.path.basename(airport_zip)}"

            # Create zip files for contours
            contour_poly_zip_path = os.path.splitext(shapefile_paths["contour_poly"])[0] + ".zip"
            contour_line_zip_path = os.path.splitext(shapefile_paths["contour_line"])[0] + ".zip"

            if os.path.exists(contour_poly_zip_path):
                result["contours_poly_url"] = f"/cap_winds_shp/{os.path.basename(contour_poly_zip_path)}"

            if os.path.exists(contour_line_zip_path):
                result["contours_line_url"] = f"/cap_winds_shp/{os.path.basename(contour_line_zip_path)}"

            results = [result]

            _log(f"Analysis complete: generated {len(results)} map(s)")
            return results

        except Exception as e:
            _log(f"Analysis failed: {e}")
            _log(traceback.format_exc())
            raise


# =====================================================================
# Backward Compatibility Function
# =====================================================================

def run_analysis(location_type: str, location_code: str = "",
                primary_airport: Optional[str] = None,
                center_point: Optional[str] = None,
                center_lat: Optional[float] = None,
                center_lon: Optional[float] = None) -> List[Dict]:
    """
    Main entry point for Flask app (backward compatible)

    Args:
        location_type: 'state', 'region', 'conus', or 'radius'
        location_code: state code (e.g., 'CO'), region code ('RMR'), or 'CONUS'
        primary_airport: optional ICAO (e.g., 'KFNL')
        center_point: for radius type, airport code or grid name
        center_lat: for radius type, center latitude
        center_lon: for radius type, center longitude

    Returns:
        List of dictionaries with map information
    """
    # Log environment info for debugging
    try:
        _log("=== Environment Info ===")
        _log(f"USER: {getpass.getuser()}")
        _log(f"HOME: {os.environ.get('HOME')}")
        _log(f"CWD: {os.getcwd()}")
        _log(f"MPLCONFIGDIR: {os.environ.get('MPLCONFIGDIR')}")
        _log(f"CARTOPY_CACHE_DIR: {os.environ.get('CARTOPY_CACHE_DIR')}")
    except Exception as e:
        _log(f"Environment logging error: {e}")

    # Create service and run analysis
    service = WindAnalysisService()
    return service.generate_analysis(
        location_type, location_code, primary_airport,
        center_point, center_lat, center_lon
    )


# =====================================================================
# Module Test
# =====================================================================

if __name__ == "__main__":
    # Example usage
    _log("Testing wind analysis service...")

    try:
        # Test state map
        _log("\n=== Testing State Map (Colorado) ===")
        results = run_analysis("state", "CO", "KFNL")
        _log(f"Success! Generated {len(results)} map(s)")
        for result in results:
            _log(f"  - {result['map_name']}: {result['url']}")

        # Test radius map
        _log("\n=== Testing Radius Map (50nm around KFNL) ===")
        results = run_analysis("radius", "", None, "KFNL")
        _log(f"Success! Generated {len(results)} map(s)")
        for result in results:
            _log(f"  - {result['map_name']}: {result['url']}")

    except Exception as e:
        _log(f"Test failed: {e}")
        _log(traceback.format_exc())



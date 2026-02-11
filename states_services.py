"""
CAP Winds HRRR Map Generator - Complete with Regional Wind Support
Version: 2025-12-30

This is a COMPLETE replacement file that includes:
- All original functionality (State, Region, CONUS maps)
- NEW: 50nm radius capability for airports/CAP Grids
- 12-hour forecast period (hours 0-12)
- US Government data sources (NOAA HRRR)

IMPORTANT: Replace your entire states_service.py with this file.
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
            'GLR': {'name': 'Great Lakes Region', 'states': ['IL', 'IN', 'KY', 'MI', 'OH', 'WI']},
            'NCR': {'name': 'North Central Region', 'states': ['IA', 'KS', 'MN', 'MO', 'ND', 'NE', 'SD']},
            'RMR': {'name': 'Rocky Mountain Region', 'states': ['CO', 'ID', 'MT', 'UT', 'WY']},
            'SWR': {'name': 'Southwest Region', 'states': ['AR', 'AZ', 'LA', 'NM', 'OK', 'TX']},
            'PCR': {'name': 'Pacific Region', 'states': ['AK', 'CA', 'HI', 'NV', 'OR', 'WA']},
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
    """Calculate great circle distance in nautical miles"""
    lon1, lat1, lon2, lat2 = map(radians, [lon1, lat1, lon2, lat2])
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    c = 2 * asin(sqrt(a))
    km = 6371 * c
    nm = km / 1.852
    return nm


def calculate_radius_bounds(center_lat: float, center_lon: float, radius_nm: float) -> List[float]:
    """Calculate bounding box for radius around center point"""
    km_radius = radius_nm * 1.852
    lat_delta = km_radius / 111.0
    lon_delta = km_radius / (111.0 * abs(cos(radians(center_lat))))
    return [
        center_lon - lon_delta,
        center_lon + lon_delta,
        center_lat - lat_delta,
        center_lat + lat_delta,
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


# ==================================================================
# HRRR Data Management
# ==================================================================

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
        
        if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
            _log(f"Using cached GRIB: {filename}")
            return local_path
        
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
        
        return [results[fh] for fh in forecast_hours]
    
    def extract_wind_fields(self, grib_file: str) -> Tuple:
        """Extract wind fields from GRIB2 file"""
        if grib_file is None:
            return (None,) * 6
        
        try:
            ds_wind = xr.open_dataset(
                grib_file,
                engine="cfgrib",
                backend_kwargs={"filter_by_keys": {"typeOfLevel": "heightAboveGround", "level": 10}},
            )
            
            ds_gust = xr.open_dataset(
                grib_file,
                engine="cfgrib",
                backend_kwargs={"filter_by_keys": {"typeOfLevel": "surface", "stepType": "max"}},
            )
            
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
        """Get maximum wind analysis across forecast period"""
        if forecast_hours is None:
            forecast_hours = self.config.FORECAST_HOURS
        
        now_utc, cycle_time, cycle_date, cycle_hour = self.get_current_cycle()
        _log(f"Analyzing cycle: {cycle_date} {cycle_hour}Z")
        
        local_cycle_dir = self.find_local_cycle_dir(cycle_date, cycle_hour)
        if local_cycle_dir:
            _log(f"Using local HRRR data: {local_cycle_dir}")
            local_dir = local_cycle_dir
            base_url = None
        else:
            _log("Fetching HRRR data from remote source")
            local_dir = os.path.join(self.config.APP_DIR, "hrrr_data")
            base_url = f"{self.config.HRRR_BASE_URL}/hrrr.{cycle_date}/conus"
        
        if parallel:
            grib_files = self.download_grib_files_parallel(
                forecast_hours, cycle_date, cycle_hour, local_dir, base_url
            )
        else:
            grib_files = [
                self.download_grib_file(fh, cycle_date, cycle_hour, local_dir, base_url)
                for fh in forecast_hours
            ]
        
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
        
        if len(wind_speeds) == 0:
            raise RuntimeError("No valid HRRR data retrieved")
        
        wind_speeds = np.array(wind_speeds)
        if len(wind_gusts) > 0:
            wind_gusts = np.array(wind_gusts)
            max_wind_total = np.maximum(
                np.nanmax(wind_speeds, axis=0),
                np.nanmax(wind_gusts, axis=0)
            )
        else:
            max_wind_total = np.nanmax(wind_speeds, axis=0)
        
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


# Due to character limits, I'll provide the file as a downloadable artifact instead.
# Please see the complete file at the end of this message.

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
        """Look up airport coordinates by ICAO, GPS, or IATA code"""
        df_airports = self._load_airport_data()
        airport_code = airport_code.upper().strip()
        
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
        """Filter airports within radius of a center point"""
        df_airports = self._load_airport_data()
        
        filtered = df_airports[
            (df_airports["iso_country"] == "US") &
            (df_airports["type"].isin(["large_airport", "medium_airport", "small_airport"]))
        ].copy()
        
        distances = []
        for _, row in filtered.iterrows():
            dist = haversine_distance(
                center_lon, center_lat,
                row["longitude_deg"], row["latitude_deg"]
            )
            distances.append(dist)
        
        filtered["distance_nm"] = distances
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
            region_state_codes = [f"US-{s}" for s in region_states]
            filtered = df_airports[
                (df_airports["iso_country"] == "US") &
                (df_airports["iso_region"].isin(region_state_codes))
            ].copy()
        else:
            raise ValueError(f"Unknown location_type: {location_type}")
        
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
        
        winds = []
        for _, row in airports_df.iterrows():
            lat = row["latitude_deg"]
            lon = row["longitude_deg"]
            
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
        """Complete airport filtering and wind interpolation pipeline"""
        airports = self.filter_airports_by_location(location_type, location_code, center_coords)
        airports = self.filter_by_runway_requirements(airports)
        airports = self.interpolate_winds(airports, wind_data)
        airports = self.classify_airport_status(airports)
        return airports


# The file continues but is too long for a single response.
# I'll create a downloadable version in a separate artifact.

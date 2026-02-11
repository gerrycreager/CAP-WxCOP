"""
GFS Data Manager for CAP Winds - OCONUS Coverage
Handles Alaska, Hawaii, Puerto Rico, Virgin Islands, Guam

FUTURE: If HRRR-AK or HRRR-HI become available via LDM feeds:
  - Update NON_CONUS_STATES to remove 'AK' and/or 'HI'
  - HRRR will be preferred for those regions (higher resolution)
  - GFS will remain as backup for PR, VI, GU

Add this to states_service.py or import as separate module
"""

import os
import glob
from datetime import datetime, timedelta
from typing import Optional, Tuple
import numpy as np
import xarray as xr

class GFSDataManager:
    """Manages GFS forecast data for OCONUS (Outside CONUS) regions"""
    
    # States/territories that require GFS (no HRRR coverage currently)
    # NOTE: Experimental HRRR-AK and HRRR-HI existed but unclear if available via LDM
    # If those feeds become available, remove 'AK' and/or 'HI' from this set
    NON_CONUS_STATES = {'AK', 'HI', 'PR', 'VI', 'GU'}
    
    def __init__(self, config):
        self.config = config
        self.gfs_root = os.path.join(os.path.dirname(config.MODEL_ROOT), 'gfs', '0p25')
        
    @staticmethod
    def requires_gfs(location_code: str, center_coords: tuple = None) -> bool:
        """
        Check if location requires GFS instead of HRRR
        
        Args:
            location_code: State or region code
            center_coords: Optional (lat, lon) tuple for radius queries
        """
        if not location_code:
            # For radius queries with no location_code, check coordinates
            if center_coords:
                lat, lon = center_coords
                # Check if coordinates fall in OCONUS territories
                # Puerto Rico: 17.9-18.6°N, 65.2-67.3°W
                if 17.9 <= lat <= 18.6 and -67.3 <= lon <= -65.2:
                    return True
                # Virgin Islands: 17.6-18.5°N, 65.1-64.6°W  
                if 17.6 <= lat <= 18.5 and -65.1 <= lon <= -64.6:
                    return True
                # Guam: 13.2-13.7°N, 144.6-144.9°E
                if 13.2 <= lat <= 13.7 and 144.6 <= lon <= 144.9:
                    return True
                # Alaska: 51-72°N, 180-129°W (simplified)
                if 51 <= lat <= 72 and -180 <= lon <= -129:
                    return True
                # Hawaii: 18.9-22.3°N, 160.3-154.7°W
                if 18.9 <= lat <= 22.3 and -160.3 <= lon <= -154.7:
                    return True
            return False
        
        # Check if state code directly
        if location_code in GFSDataManager.NON_CONUS_STATES:
            return True
            
        # Check if region code is OCONUS-only
        # NOTE: Mixed regions like 'PCR' (CA+AK+HI+GU) or 'SER' (CONUS+PR+VI)
        # are NOT supported - users must use subregions instead
        OCONUS_ONLY_REGIONS = {
            'SER-CARIB',  # Caribbean only: PR, VI
            'PCR-AK',     # Alaska only
            'PCR-HI',     # Hawaii only
            'PCR-GUAM',   # Guam only
        }
        
        if location_code in OCONUS_ONLY_REGIONS:
            return True
            
        return False
    
    def get_current_cycle(self, cycles_back: int = 0) -> Tuple[datetime, str, str]:
        """
        Get GFS cycle (00z, 06z, 12z, 18z)
        
        Args:
            cycles_back: Number of 6-hour cycles to go back (0=current, 1=6hrs ago, etc.)
        """
        now_utc = datetime.utcnow()
        
        # GFS runs at 00, 06, 12, 18z
        # GFS takes 3-4 hours to fully process, so go back 5 hours to ensure availability
        # Then add additional cycles_back * 6 hours
        hours_back = 5 + (cycles_back * 6)
        cycle_time = now_utc - timedelta(hours=hours_back)
        
        # Round down to nearest 6-hour cycle
        cycle_hour = (cycle_time.hour // 6) * 6
        cycle_time = cycle_time.replace(hour=cycle_hour, minute=0, second=0, microsecond=0)
        
        cycle_date = cycle_time.strftime("%Y%m%d")
        cycle_hour_str = f"{cycle_hour:02d}"
        
        if cycles_back == 0:
            _log(f"Current UTC: {now_utc.strftime('%Y-%m-%d %H:%M')}Z, "
                 f"Using GFS cycle: {cycle_date} {cycle_hour_str}Z")
        
        return cycle_time, cycle_date, cycle_hour_str
    
    def find_local_gfs_files(self, cycle_date: str, cycle_hour: str, 
                             forecast_hours: range) -> dict:
        """Find local GFS files for a given cycle"""
        # Use flat date directory structure: /LDM/models/gfs/0p25/YYYYMMDD/
        cycle_dir = os.path.join(self.gfs_root, cycle_date)
        
        if not os.path.isdir(cycle_dir):
            _log(f"GFS cycle directory not found: {cycle_dir}")
            return {}
        
        files = {}
        for fh in forecast_hours:
            # Expected filename: gfs_0p25_YYYYMMDD_HHz_fFFF.grib2
            pattern = f"gfs_0p25_{cycle_date}_{cycle_hour}z_f{fh:03d}.grib2"
            filepath = os.path.join(cycle_dir, pattern)
            
            if os.path.exists(filepath):
                files[fh] = filepath
            else:
                _log(f"GFS file not found: {filepath}")
                
        return files
    
    def extract_wind_fields_gfs(self, grib_file: str) -> Tuple:
        """Extract 10m wind fields from GFS GRIB2 file"""
        if grib_file is None or not os.path.exists(grib_file):
            return (None,) * 5
        
        try:
            # Open GFS file - 10m winds
            ds = xr.open_dataset(
                grib_file,
                engine="cfgrib",
                backend_kwargs={
                    "filter_by_keys": {
                        "typeOfLevel": "heightAboveGround",
                        "level": 10
                    }
                },
            )
            
            # Find wind component variables
            vars_list = list(ds.data_vars)
            u_var = next((v for v in vars_list if 'u' in v.lower() and '10' in v), None)
            v_var = next((v for v in vars_list if 'v' in v.lower() and '10' in v), None)
            
            if u_var is None or v_var is None:
                _log(f"Wind variables not found in {grib_file}")
                _log(f"Available variables: {vars_list}")
                return (None,) * 5
            
            u10 = ds[u_var]
            v10 = ds[v_var]
            
            # Extract coordinates
            if "latitude" in u10.coords:
                lats = u10["latitude"].values
                lons = u10["longitude"].values
            elif "lat" in u10.coords:
                lats = u10["lat"].values  
                lons = u10["lon"].values
            else:
                _log(f"Coordinates not found in {grib_file}")
                return (None,) * 5
            
            # GFS longitudes are 0-360, need to convert to -180 to 180
            # AND reorder data to maintain sorted grid
            if lons.max() > 180:
                # Find the split point where longitude wraps
                # GFS grid: [0, 0.25, ..., 179.75, 180, 180.25, ..., 359.75]
                # We want: [-180, -179.75, ..., -0.25, 0, 0.25, ..., 179.75]
                
                # Convert longitudes
                lons_converted = np.where(lons > 180, lons - 360, lons)
                
                # Find indices to reorder
                # Split at 180° (where it goes from positive to negative)
                split_idx = np.where(lons > 180)[0][0] if np.any(lons > 180) else len(lons)
                
                # Reorder lons to be ascending
                lons = np.concatenate([lons_converted[split_idx:], lons_converted[:split_idx]])
                
                # Reorder wind data to match
                # Check dimensionality - could be 1D (lons,) or 2D (lats, lons)
                if u10.values.ndim == 1:
                    u10_reordered = np.concatenate([u10.values[split_idx:], u10.values[:split_idx]])
                    v10_reordered = np.concatenate([v10.values[split_idx:], v10.values[:split_idx]])
                elif u10.values.ndim == 2:
                    # 2D: (lat, lon) - reorder along longitude axis
                    u10_reordered = np.concatenate([u10.values[:, split_idx:], u10.values[:, :split_idx]], axis=1)
                    v10_reordered = np.concatenate([v10.values[:, split_idx:], v10.values[:, :split_idx]], axis=1)
                else:
                    _log(f"Unexpected data dimensionality: {u10.values.ndim}")
                    return (None,) * 5
                
                return lats, lons, u10_reordered, v10_reordered, None
            else:
                # Already in -180 to 180 range
                return lats, lons, u10.values, v10.values, None
            
        except Exception as e:
            _log(f"Error extracting GFS wind fields: {e}")
            import traceback
            _log(traceback.format_exc())
            return (None,) * 5
    
    def get_wind_analysis(self, forecast_hours: range = None) -> Optional['WindData']:
        """Get GFS wind analysis for OCONUS regions"""
        # GFS forecast hours for 12-hour period (3-hourly intervals)
        # Convert hourly FORECAST_HOURS (0-12) to GFS 3-hourly (0, 3, 6, 9, 12)
        if forecast_hours is None:
            # Use 3-hourly intervals: 0, 3, 6, 9, 12
            gfs_forecast_hours = range(0, 13, 3)
        else:
            # Convert to 3-hourly by rounding down
            gfs_forecast_hours = range(0, max(forecast_hours) + 1, 3)
        
        # Try current cycle, then fall back to previous cycles
        # GFS runs every 6 hours, try up to 4 cycles back (24 hours)
        for cycles_back in range(0, 5):  # 0, 1, 2, 3, 4 = current + 4 previous
            try:
                cycle_time, cycle_date, cycle_hour = self.get_current_cycle(cycles_back)
                _log(f"Attempting GFS cycle: {cycle_date} {cycle_hour}Z (cycles_back={cycles_back})")
                _log(f"Using GFS forecast hours: {list(gfs_forecast_hours)}")
                
                # Find local GFS files
                gfs_files = self.find_local_gfs_files(cycle_date, cycle_hour, gfs_forecast_hours)
                
                if gfs_files and len(gfs_files) >= 3:  # Need at least 3 files for analysis
                    _log(f"SUCCESS: Found {len(gfs_files)} GFS files for cycle {cycle_date} {cycle_hour}Z")
                    _log(f"GFS files: {sorted(gfs_files.keys())}")
                    break
                else:
                    _log(f"Insufficient GFS data for cycle {cycle_date} {cycle_hour}Z "
                         f"(found {len(gfs_files)} files, need at least 3)")
                    continue
                    
            except Exception as e:
                _log(f"Error checking cycle {cycles_back} cycles back: {e}")
                continue
        else:
            # No cycles found
            error_msg = (
                f"No GFS data found after checking 5 cycles (24 hours)\n"
                f"Expected location: {self.gfs_root}/YYYYMMDD/\n"
                f"Expected files: gfs_0p25_YYYYMMDD_HHz_f000.grib2, f003.grib2, etc.\n"
                f"GFS cycles run every 6 hours (00z, 06z, 12z, 18z). Check:\n"
                f"  1. CONDUIT feed is active: ldmadmin watch | grep CONDUIT\n"
                f"  2. pqact_conduit.conf is correct and loaded\n"
                f"  3. Wait for next GFS cycle to complete"
            )
            _log(error_msg)
            raise RuntimeError(error_msg)
        
        # Extract wind fields
        wind_speeds = []
        lats = None
        lons = None
        
        for fh in sorted(gfs_files.keys()):
            grib_file = gfs_files[fh]
            lats_tmp, lons_tmp, u10, v10, _ = self.extract_wind_fields_gfs(grib_file)
            
            if lats_tmp is None:
                continue
            
            if lats is None:
                lats = lats_tmp
                lons = lons_tmp
            
            # Calculate wind speed
            wspd = np.sqrt(u10 ** 2 + v10 ** 2)
            wind_speeds.append(wspd)
        
        if len(wind_speeds) == 0:
            raise RuntimeError("No valid GFS wind data retrieved")
        
        # Calculate maximum winds across forecast period
        wind_speeds = np.array(wind_speeds)
        max_wind_total = np.nanmax(wind_speeds, axis=0)
        
        # Convert m/s to knots
        max_wind_kts = max_wind_total * 1.944
        
        # Import WindData from states_service
        from states_service import WindData
        
        return WindData(
            max_wind_kts=max_wind_kts,
            lats=lats,
            lons=lons,
            cycle_date=cycle_date,
            cycle_hour=cycle_hour,
            forecast_hours=range(0, 13) if forecast_hours is None else forecast_hours,
            init_time=cycle_time,
            data_source="GFS"
        )


def _log(msg: str) -> None:
    """Logging helper"""
    import sys
    sys.stderr.write(f"[GFS] {msg}\n")
    sys.stderr.flush()


# Integration instructions for states_service.py:
#
# 1. Add at top of WindAnalysisService.generate_analysis():
#
#    from gfs_manager import GFSDataManager
#
# 2. Replace line ~1449:
#    wind_data = self.hrrr_manager.get_wind_analysis(parallel=True)
#
#    With:
#    if GFSDataManager.requires_gfs(location_code):
#        _log(f"Using GFS data for {location_code} (non-CONUS)")
#        gfs_manager = GFSDataManager(self.config)
#        wind_data = gfs_manager.get_wind_analysis()
#    else:
#        wind_data = self.hrrr_manager.get_wind_analysis(parallel=True)
#

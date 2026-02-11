import os
import requests
import xarray as xr
import numpy as np
import matplotlib.pyplot as plt
from datetime import datetime, timedelta
import cartopy.crs as ccrs
import cartopy.feature as cfeature
import pandas as pd
import matplotlib.patheffects as path_effects

# ---------------------------------------------------------
# State/Territory Boundaries and CAP Regions
# ---------------------------------------------------------
STATE_BOUNDARIES = {
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

# CAP Region definitions
CAP_REGIONS = {
    'NER': {'name': 'Northeast Region', 'states': ['CT', 'ME', 'MA', 'NH', 'NY', 'RI', 'VT']},
    'MAR': {'name': 'Mid-Atlantic Region', 'states': ['DE', 'MD', 'NJ', 'NC', 'PA', 'VA', 'WV']},
    'SER': {'name': 'Southeast Region', 'states': ['AL', 'FL', 'GA', 'MS', 'SC', 'TN', 'PR', 'VI']},
    'GLR': {'name': 'Great Lakes Region', 'states': ['IL', 'IN', 'KY', 'MI', 'OH', 'WI']},
    'NCR': {'name': 'North Central Region', 'states': ['IA', 'KS', 'MN', 'MO', 'ND', 'NE', 'SD']},
    'RMR': {'name': 'Rocky Mountain Region', 'states': ['CO', 'ID', 'MT', 'UT', 'WY']},
    'SWR': {'name': 'Southwest Region', 'states': ['AR', 'AZ', 'LA', 'NM', 'OK', 'TX']},
    'PCR': {'name': 'Pacific Region', 'states': ['AK', 'CA', 'HI', 'NV', 'OR', 'WA']},
}

# Pacific Region sub-maps
PCR_SUBMAPS = {
    'PCR-AK': {'name': 'Alaska', 'states': ['AK'], 'bounds': [-180, -129, 51, 72]},
    'PCR-HI': {'name': 'Hawaii', 'states': ['HI'], 'bounds': [-160.3, -154.7, 18.9, 22.3]},
    'PCR-WC': {'name': 'West Coast', 'states': ['CA', 'NV', 'OR', 'WA'], 'bounds': [-125, -114, 32, 49]},
}

# CONUS bounds
CONUS_BOUNDS = [-125, -65, 24, 50]

# Create reverse lookup dictionary (name -> code)
NAME_TO_CODE = {v['name'].lower(): k for k, v in STATE_BOUNDARIES.items()}

# ---------------------------------------------------------
# Apache web server configuration
# ---------------------------------------------------------
# For Ubuntu 24.04 Apache2 default installation
# Create directory: sudo mkdir -p /var/www/html/cap_winds
# Set permissions: sudo chown www-data:www-data /var/www/html/cap_winds
# Set permissions: sudo chmod 755 /var/www/html/cap_winds
# Access via: http://your-server/cap_winds/

WEB_OUTPUT_DIR = '/var/www/html/cap_winds'

def ensure_output_directory():
    """Create output directory if it doesn't exist"""
    try:
        os.makedirs(WEB_OUTPUT_DIR, exist_ok=True)
        print(f"Output directory: {WEB_OUTPUT_DIR}")
        return WEB_OUTPUT_DIR
    except PermissionError:
        print(f"WARNING: Cannot write to {WEB_OUTPUT_DIR}")
        print("Falling back to current directory")
        fallback_dir = './cap_winds_output'
        os.makedirs(fallback_dir, exist_ok=True)
        return fallback_dir

# ---------------------------------------------------------
# CAP WIND CONSTRAINTS
# ---------------------------------------------------------
MAX_OPERATIONAL_WIND = 30  # knots
CAUTION_WIND = 20          # knots

# ---------------------------------------------------------
# Get user input for state or region
# ---------------------------------------------------------
def get_location_selection():
    print("\n" + "="*70)
    print("CAP WIND CONSTRAINTS ANALYSIS - LOCATION SELECTOR")
    print("="*70)
    print("\nSelect by STATE, REGION, or CONUS:")
    print("\nCONUS: Enter 'CONUS' or 'conus' for Continental US map")
    print("       (Military and Major airports only)")
    print("\nSTATE: Enter state name or 2-letter code (e.g., Texas, TX, California, CA)")
    print("\nREGION: Enter region code:")
    print("  NER  = Northeast Region")
    print("  MAR  = Mid-Atlantic Region")
    print("  SER  = Southeast Region")
    print("  GLR  = Great Lakes Region")
    print("  NCR  = North Central Region")
    print("  RMR  = Rocky Mountain Region")
    print("  SWR  = Southwest Region")
    print("  PCR  = Pacific Region (creates 3 maps: Alaska, Hawaii, West Coast)")
    print("-"*70)
    
    while True:
        user_input = input("\nEnter state/region/CONUS: ").strip().upper()
        
        if not user_input:
            print("Please enter a state, region, or CONUS")
            continue
        
        # Check for CONUS
        if user_input == 'CONUS':
            return 'conus', 'CONUS', 'Continental United States'
        
        # Check if it's a region code
        if user_input in CAP_REGIONS:
            return 'region', user_input, CAP_REGIONS[user_input]['name']
        
        # Check if it's a state code (2 letters)
        if len(user_input) == 2 and user_input in STATE_BOUNDARIES:
            return 'state', user_input, STATE_BOUNDARIES[user_input]['name']
        
        # Try as full state name
        user_lower = user_input.lower()
        for code, data in STATE_BOUNDARIES.items():
            if data['name'].lower() == user_lower:
                return 'state', code, data['name']
        
        print(f"'{user_input}' not recognized. Please try again.")
        print("Use state name, 2-letter code, region code, or CONUS")

def get_primary_airport():
    print("\n" + "-"*70)
    print("PRIMARY AIRPORT SELECTION (Optional)")
    print("-"*70)
    print("Enter a primary airport identifier to always label on map")
    print("Examples: KCOS, KDEN, KBOS, KJFK")
    print("Press ENTER to skip")
    print("-"*70)
    
    user_input = input("\nEnter primary airport (or press ENTER to skip): ").strip().upper()
    
    if not user_input:
        return None
    
    return user_input

# Get location selection
location_type, location_code, location_name = get_location_selection()

# Get primary airport
primary_airport = get_primary_airport()

print(f"\nSelected: {location_name} ({location_code})")
if primary_airport:
    print(f"Primary airport: {primary_airport}")

# Determine which states to process
if location_type == 'conus':
    # CONUS - all continental US states (exclude AK, HI, territories)
    conus_states = [code for code, data in STATE_BOUNDARIES.items() 
                   if code not in ['AK', 'HI', 'PR', 'VI', 'GU']]
    states_to_process = conus_states
    map_bounds = CONUS_BOUNDS
    is_region = False
    is_conus = True
elif location_type == 'state':
    states_to_process = [location_code]
    map_bounds = STATE_BOUNDARIES[location_code]['bounds']
    is_region = False
    is_conus = False
elif location_code == 'PCR':
    # Pacific Region gets 3 separate maps
    is_region = True
    is_pcr = True
    is_conus = False
    print("\nPacific Region: Will create 3 maps (Alaska, Hawaii, West Coast)")
else:
    # Other regions get combined into one map
    states_to_process = CAP_REGIONS[location_code]['states']
    is_region = True
    is_pcr = False
    is_conus = False
    # Calculate combined bounds for region
    all_bounds = [STATE_BOUNDARIES[s]['bounds'] for s in states_to_process]
    map_bounds = [
        min(b[0] for b in all_bounds),  # min lon
        max(b[1] for b in all_bounds),  # max lon
        min(b[2] for b in all_bounds),  # min lat
        max(b[3] for b in all_bounds)   # max lat
    ]

# ---------------------------------------------------------
# Get current HRRR cycle
# ---------------------------------------------------------
now_utc = datetime.utcnow()
cycle_time = now_utc - timedelta(hours=2)
cycle_date = cycle_time.strftime('%Y%m%d')
cycle_hour = cycle_time.strftime('%H')

print(f"\nCurrent UTC time: {now_utc.strftime('%Y-%m-%d %H:%M')}")
print(f"Using HRRR cycle: {cycle_date} {cycle_hour}Z")
print(f"\nCAP Wind Limits:")
print(f"  - Winds > {MAX_OPERATIONAL_WIND} kts: Requires SFRO + Wing Commander approval")
print(f"  - Winds > {CAUTION_WIND} kts: Caution - approaching limits")
print("="*70)

base_url = f'https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.{cycle_date}/conus'
local_dir = 'hrrr_data'
os.makedirs(local_dir, exist_ok=True)

forecast_hours = range(0, 13)

# ---------------------------------------------------------
# Download and process HRRR wind data
# ---------------------------------------------------------
def download_grib(hour):
    fhour_str = f'{hour:02d}'
    filename_remote = f'hrrr.t{cycle_hour}z.wrfsfcf{fhour_str}.grib2'
    url = f'{base_url}/{filename_remote}'
    
    fname_local = os.path.join(local_dir, f'hrrr_{cycle_date}_{cycle_hour}z_f{fhour_str}.grib2')
    
    if not os.path.exists(fname_local):
        print(f'Downloading f{fhour_str}...', end=' ')
        try:
            r = requests.get(url, stream=True, timeout=120)
            r.raise_for_status()
            with open(fname_local, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            print('Done')
        except Exception as e:
            print(f'Failed: {e}')
            return None
    else:
        print(f'Using cached f{fhour_str}')
    
    return fname_local

def open_hrrr_wind_fields(grib_file):
    if grib_file is None:
        return None, None, None, None, None, None
    
    try:
        ds_wind = xr.open_dataset(grib_file, engine='cfgrib',
            backend_kwargs={'filter_by_keys': {'typeOfLevel': 'heightAboveGround', 'level': 10}})
        
        ds_gust = xr.open_dataset(grib_file, engine='cfgrib',
            backend_kwargs={'filter_by_keys': {'typeOfLevel': 'surface', 'stepType': 'max'}})
        
        wind_vars = list(ds_wind.data_vars)
        gust_vars = list(ds_gust.data_vars)
        
        uvar = next((v for v in wind_vars if 'u' in v.lower()), None)
        vvar = next((v for v in wind_vars if 'v' in v.lower()), None)
        gvar = next((v for v in gust_vars if 'gust' in v.lower()), None)
        
        if uvar is None or vvar is None:
            return None, None, None, None, None, None
        
        u10 = ds_wind[uvar]
        v10 = ds_wind[vvar]
        gust10 = ds_gust[gvar] if gvar else None
        
        time = pd.to_datetime(u10['time'].values)
        
        if 'latitude' in u10.coords:
            lats = u10['latitude'].values
            lons = u10['longitude'].values
        elif 'lat' in u10.coords:
            lats = u10['lat'].values
            lons = u10['lon'].values
        else:
            return None, None, None, None, None, None
        
        return time, lats, lons, u10.values, v10.values, gust10.values if gust10 is not None else None
        
    except Exception as e:
        print(f"  ERROR: {e}")
        return None, None, None, None, None, None

# Process HRRR data
wind_speeds = []
wind_gusts = []
times = []
lats = None
lons = None

for fh in forecast_hours:
    grib_file = download_grib(fh)
    if grib_file is None:
        continue
    
    time, lats_tmp, lons_tmp, u10, v10, gust10 = open_hrrr_wind_fields(grib_file)
    
    if time is None:
        continue
    
    if lats is None:
        lats = lats_tmp
        lons = lons_tmp
    
    wspd = np.sqrt(u10**2 + v10**2)
    wind_speeds.append(wspd)
    if gust10 is not None:
        wind_gusts.append(gust10)
    times.append(time)

if len(wind_speeds) == 0:
    print("\nERROR: No valid data retrieved. Exiting.")
    exit(1)

wind_speeds = np.array(wind_speeds)
if len(wind_gusts) > 0:
    wind_gusts = np.array(wind_gusts)
    has_gust = True
else:
    has_gust = False

print(f"\nProcessed {len(times)} forecast hours")

max_wspd = np.nanmax(wind_speeds, axis=0)
if has_gust:
    max_gust = np.nanmax(wind_gusts, axis=0)
    max_wind_total = np.maximum(max_wspd, max_gust)
else:
    max_wind_total = max_wspd

max_wind_kts = max_wind_total * 1.944

# ---------------------------------------------------------
# Download airport data
# ---------------------------------------------------------
print("\nDownloading airport data...")
airport_url = 'https://davidmegginson.github.io/ourairports-data/airports.csv'

try:
    df_airports = pd.read_csv(airport_url)
    
    # Filter for airports in selected location
    if location_type == 'state':
        location_airports = df_airports[
            (df_airports['iso_country'] == 'US') & 
            (df_airports['iso_region'] == f'US-{location_code}')
        ].copy()
    elif location_type == 'conus':
        # CONUS - all continental US states
        conus_state_codes = ['US-' + s for s in states_to_process]
        location_airports = df_airports[
            (df_airports['iso_country'] == 'US') & 
            (df_airports['iso_region'].isin(conus_state_codes))
        ].copy()
    else:
        # Region - get all airports from states in region
        if is_pcr:
            # For PCR, we'll filter differently for each submap later
            location_airports = df_airports[
                (df_airports['iso_country'] == 'US') & 
                (df_airports['iso_region'].str.startswith('US-'))
            ].copy()
            # Filter to PCR states
            pcr_states = ['US-' + s for s in CAP_REGIONS['PCR']['states']]
            location_airports = location_airports[location_airports['iso_region'].isin(pcr_states)]
        else:
            region_state_codes = ['US-' + s for s in states_to_process]
            location_airports = df_airports[
                (df_airports['iso_country'] == 'US') & 
                (df_airports['iso_region'].isin(region_state_codes))
            ].copy()
    
    location_airports = location_airports[location_airports['type'].isin(['large_airport', 'medium_airport', 'small_airport'])]
    
    print(f"Found {len(location_airports)} airports in {location_name}")
    
except Exception as e:
    print(f"Error downloading airport data: {e}")
    state_airports = None

# Download runway data
print("Downloading runway data...")
runway_url = 'https://davidmegginson.github.io/ourairports-data/runways.csv'

try:
    df_runways = pd.read_csv(runway_url)
    
    paved_surfaces = ['ASP', 'ASPH', 'CON', 'CONC', 'concrete', 'asphalt']
    paved_runways = df_runways[
        (df_runways['surface'].isin(paved_surfaces)) & 
        (df_runways['length_ft'] >= 2500)
    ]
    
    qualifying_airport_ids = paved_runways['airport_ident'].unique()
    
    if location_airports is not None:
        location_airports = location_airports[location_airports['ident'].isin(qualifying_airport_ids)]
        print(f"Airports with paved runways >= 2500 ft: {len(location_airports)}")
    
except Exception as e:
    print(f"Error filtering by runway: {e}")

# ---------------------------------------------------------
# Sample wind at airport locations
# ---------------------------------------------------------
if location_airports is not None and len(location_airports) > 0:
    print("\nSampling winds at airport locations...")
    
    from scipy.interpolate import RegularGridInterpolator
    
    if lats.ndim == 2:
        lat_1d = lats[:, 0]
        lon_1d = lons[0, :]
    else:
        lat_1d = lats
        lon_1d = lons
    
    interp = RegularGridInterpolator((lat_1d, lon_1d), max_wind_kts, 
                                      bounds_error=False, fill_value=np.nan)
    
    airport_winds = []
    for idx, row in location_airports.iterrows():
        lat = row['latitude_deg']
        lon = row['longitude_deg']
        
        if lon < 0 and lon_1d.min() > 0:
            lon = lon + 360
        elif lon > 180 and lon_1d.max() < 180:
            lon = lon - 360
            
        try:
            wind_at_airport = interp((lat, lon))
        except:
            wind_at_airport = np.nan
        airport_winds.append(wind_at_airport)
    
    location_airports['max_wind_kts'] = airport_winds
    
    location_airports['status'] = 'Normal'
    location_airports.loc[location_airports['max_wind_kts'] >= CAUTION_WIND, 'status'] = 'Caution'
    location_airports.loc[location_airports['max_wind_kts'] >= MAX_OPERATIONAL_WIND, 'status'] = 'Out of Limits'
    
    normal = len(location_airports[location_airports['status'] == 'Normal'])
    caution = len(location_airports[location_airports['status'] == 'Caution'])
    out_of_limits = len(location_airports[location_airports['status'] == 'Out of Limits'])
    
    print(f"\nAirport Status Summary for {location_name}:")
    print(f"  Normal operations: {normal}")
    print(f"  Caution (>{CAUTION_WIND} kts): {caution}")
    print(f"  Out of limits (>{MAX_OPERATIONAL_WIND} kts): {out_of_limits}")

# ---------------------------------------------------------
# Create map(s)
# ---------------------------------------------------------
def create_map(map_name, map_bounds, airports_df, title_suffix="", is_conus_map=False):
    """Create a single map with given bounds and airports"""
    print(f"\nCreating map: {map_name}...")
    
    fig = plt.figure(figsize=(20, 12))
    
    # Create axes with space for legends on sides
    ax = plt.axes([0.1, 0.1, 0.7, 0.8], projection=ccrs.PlateCarree())
    
    # Add map features
    ax.add_feature(cfeature.STATES, linewidth=1.0, edgecolor='black')
    ax.add_feature(cfeature.COASTLINE, linewidth=0.8)
    ax.coastlines('50m', linewidth=0.8)
    
    try:
        ax.add_feature(cfeature.BORDERS, linewidth=0.5, edgecolor='gray')
    except:
        pass
    
    # Plot wind speed contours
    levels = np.arange(0, 65, 5)
    cf = ax.contourf(lons, lats, max_wind_kts, levels=levels, 
                     cmap='YlOrRd', transform=ccrs.PlateCarree(), 
                     extend='max', alpha=0.6)
    
    # Add CAP limit lines
    CS1 = ax.contour(lons, lats, max_wind_kts, levels=[CAUTION_WIND], 
                     colors='orange', linewidths=2, transform=ccrs.PlateCarree())
    ax.clabel(CS1, inline=True, fontsize=10, fmt=f'{CAUTION_WIND} kts')
    
    CS2 = ax.contour(lons, lats, max_wind_kts, levels=[MAX_OPERATIONAL_WIND], 
                     colors='red', linewidths=3, transform=ccrs.PlateCarree())
    ax.clabel(CS2, inline=True, fontsize=12, fmt=f'{MAX_OPERATIONAL_WIND} kts')
    
    # Plot airports with identifiers
    if airports_df is not None and len(airports_df) > 0:
        # Define display priority
        airports_df['priority'] = 4
        airports_df.loc[airports_df['type'] == 'medium_airport', 'priority'] = 3
        airports_df.loc[airports_df['type'] == 'large_airport', 'priority'] = 2
        
        military_keywords = ['AFB', 'AIR FORCE BASE', 'AIR FORCE', 'NAVAL', 'NAS ', 'MCAS', 
                            'ARMY', 'MILITARY', 'JOINT BASE', 'AIR STATION', 'ANGB', 
                            'AIR NATIONAL GUARD', 'COAST GUARD', 'USAF', 'USAFE', 
                            'SPACE FORCE', 'SPACEPORT', 'NATIONAL GUARD', 'RESERVE',
                            'FIELD AAF', 'ARMY AIRFIELD', 'NAVY', 'MARINE CORPS']
        
        for idx, row in airports_df.iterrows():
            name_upper = str(row['name']).upper()
            keywords_upper = str(row.get('keywords', '')).upper()
            combined = name_upper + ' ' + keywords_upper
            
            for keyword in military_keywords:
                if keyword in combined:
                    airports_df.at[idx, 'priority'] = 1
                    break
        
        if primary_airport:
            primary_matches = airports_df[
                (airports_df['gps_code'] == primary_airport) |
                (airports_df['ident'] == primary_airport) |
                (airports_df['iata_code'] == primary_airport)
            ]
            if len(primary_matches) > 0:
                airports_df.loc[primary_matches.index, 'priority'] = 0
        
        # Plot airports by status
        for status, color, size, marker in [
            ('Normal', 'green', 25, 'o'),
            ('Caution', 'orange', 40, '^'),
            ('Out of Limits', 'red', 60, 'X')
        ]:
            subset = airports_df[airports_df['status'] == status]
            if len(subset) > 0:
                ax.scatter(subset['longitude_deg'], subset['latitude_deg'],
                          c=color, s=size, marker=marker, alpha=0.8,
                          transform=ccrs.PlateCarree(), 
                          label=f'{status} ({len(subset)})',
                          edgecolors='black', linewidths=0.7, zorder=5)
        
        # Add labels
        airports_sorted = airports_df.sort_values('priority')
        total_airports = len(airports_df)
        
        # For CONUS maps, only label military (priority 1) and large (priority 2)
        if is_conus_map:
            max_labels = len(airports_sorted[airports_sorted['priority'] <= 2])
            print(f"CONUS map: Labeling {max_labels} military and major airports only")
        else:
            # Increase label density - show more airports for state/region maps
            if total_airports <= 10:
                max_labels = total_airports
            elif total_airports <= 20:
                max_labels = total_airports  # Show all
            elif total_airports <= 30:
                max_labels = 25  # Show most
            elif total_airports <= 50:
                max_labels = 35  # Show many
            elif total_airports <= 100:
                max_labels = 50  # Show half
            else:
                max_labels = 60  # Cap at 60 for very dense states
            
            # Ensure we show all military (priority 1) and large (priority 2) airports
            military_and_large = airports_sorted[airports_sorted['priority'] <= 2]
            num_priority = len(military_and_large)
            if num_priority > max_labels:
                max_labels = num_priority  # Always show all military and large
        
        labeled_count = 0
        for idx, row in airports_sorted.iterrows():
            # For CONUS, only label priority 0, 1, 2 (primary, military, large)
            if is_conus_map and row['priority'] > 2:
                continue
                
            if labeled_count >= max_labels:
                break
            
            label = row['gps_code'] if pd.notna(row['gps_code']) else row['ident']
            
            if row['priority'] == 0:
                fontsize, fontweight, color = 9, 'bold', 'darkred'
            elif row['priority'] == 1:
                fontsize, fontweight, color = 8, 'bold', 'darkblue'
            elif row['priority'] == 2:
                fontsize, fontweight, color = 8, 'bold', 'black'
            elif row['priority'] == 3:
                fontsize, fontweight, color = 7, 'normal', 'black'
            else:
                fontsize, fontweight, color = 7, 'normal', 'dimgray'
            
            # Position label to upper right of airport symbol to avoid covering it
            # Use small offset and no background box for clarity
            ax.text(row['longitude_deg'] + 0.05, row['latitude_deg'] + 0.05, label,
                   fontsize=fontsize, fontweight=fontweight, color=color,
                   transform=ccrs.PlateCarree(),
                   ha='left', va='bottom', zorder=6,
                   path_effects=[plt.matplotlib.patheffects.withStroke(linewidth=2, foreground='white')])
            labeled_count += 1
    
    # Colorbar - horizontal at bottom
    cbar_ax = fig.add_axes([0.15, 0.05, 0.5, 0.02])
    cbar = plt.colorbar(cf, cax=cbar_ax, orientation='horizontal')
    cbar.set_label('Maximum Wind Speed (kts)', fontsize=12)
    
    # Set extent with padding
    padding = 0.5
    ax.set_extent([
        map_bounds[0] - padding,
        map_bounds[1] + padding,
        map_bounds[2] - padding,
        map_bounds[3] + padding
    ], crs=ccrs.PlateCarree())
    
    # Add gridlines
    gl = ax.gridlines(draw_labels=True, linewidth=0.5, color='gray', alpha=0.5, linestyle='--')
    gl.top_labels = False
    gl.right_labels = False
    
    # Title
    init_time = pd.to_datetime(f'{cycle_date} {cycle_hour}:00')
    plt.suptitle(f'CAP Aircraft Wind Constraint Analysis - {map_name}{title_suffix}\n'
              f'Init: {init_time.strftime("%Y-%m-%d %H:%M")} UTC | '
              f'Max Winds: Forecast Hours 0-{forecast_hours.stop-1}\n'
              f'Airports: Paved runways ≥ 2500 ft',
              fontsize=14, fontweight='bold', y=0.95)
    
    # Airport status legend - right side upper
    if airports_df is not None and len(airports_df) > 0:
        legend = ax.legend(loc='upper left', bbox_to_anchor=(1.05, 1.0), 
                          fontsize=11, framealpha=0.95, title='Airport Status')
        legend.get_title().set_fontsize(12)
        legend.get_title().set_fontweight('bold')
    
    # CAP limits text box - right side lower (below legend)
    textstr = f'CAP Wind Limits\n(CAPR 70-1):\n\n'
    textstr += f'≤ {CAUTION_WIND} kts:\nNormal operations\n\n'
    textstr += f'> {CAUTION_WIND} kts:\nCaution\n\n'
    textstr += f'> {MAX_OPERATIONAL_WIND} kts:\nRequires SFRO +\nWing Commander\napproval'
    
    # Position text box on right side, below the legend
    text_ax = fig.add_axes([0.82, 0.15, 0.16, 0.35])
    text_ax.axis('off')
    text_ax.text(0.5, 0.5, textstr, transform=text_ax.transAxes, 
                fontsize=10, verticalalignment='center', horizontalalignment='center',
                bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.95, 
                         edgecolor='black', linewidth=1))
    
    return fig

# Generate maps based on selection
dtg = cycle_time.strftime('%d%H%M') + 'Z' + cycle_time.strftime('%b%y').upper()
output_dir = ensure_output_directory()

if location_type == 'state':
    fig = create_map(location_name, map_bounds, location_airports)
    output_file = os.path.join(output_dir, f'cap_wind_{location_code}_{dtg}.png')
    plt.savefig(output_file, dpi=150, bbox_inches='tight')
    print(f"\nSaved: {output_file}")
    plt.show()
    
elif location_type == 'conus':
    fig = create_map(location_name, map_bounds, location_airports, is_conus_map=True)
    output_file = os.path.join(output_dir, f'cap_wind_CONUS_{dtg}.png')
    plt.savefig(output_file, dpi=150, bbox_inches='tight')
    print(f"\nSaved: {output_file}")
    if WEB_OUTPUT_DIR in output_file:
        print(f"Access via web: http://your-server/cap_winds/cap_wind_CONUS_{dtg}.png")
    plt.show()
    
elif is_pcr:
    # Pacific Region - create 3 maps
    for submap_code, submap_data in PCR_SUBMAPS.items():
        submap_airports = location_airports[
            location_airports['iso_region'].isin(['US-' + s for s in submap_data['states']])
        ].copy()
        
        fig = create_map(submap_data['name'], submap_data['bounds'], 
                        submap_airports, f" ({submap_code})")
        output_file = os.path.join(output_dir, f'cap_wind_{submap_code}_{dtg}.png')
        plt.savefig(output_file, dpi=150, bbox_inches='tight')
        print(f"Saved: {output_file}")
        plt.close(fig)
    
    print("\nAll Pacific Region maps created!")
    if WEB_OUTPUT_DIR in output_dir:
        print(f"Access via web: http://your-server/cap_winds/")
    
else:
    # Other regions - single combined map
    fig = create_map(location_name, map_bounds, location_airports)
    output_file = os.path.join(output_dir, f'cap_wind_{location_code}_{dtg}.png')
    plt.savefig(output_file, dpi=150, bbox_inches='tight')
    print(f"\nSaved: {output_file}")
    plt.show()

print(f"\nAnalysis complete for {location_name}!")

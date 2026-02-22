#!/usr/bin/env python3
"""
Repopulate observations.airports table - COMPLETE VERSION
- Identifies military airfields
- Determines iso_region (US-XX) from coordinates
- Handles duplicates
- Uses PostgreSQL database

Prerequisites:
- CSV files must be cached in /var/www/cap_winds_app/.cache/
- Run update_ourairports_cache.sh first to download files
- is_military and iso_region columns must exist in observations.airports table
"""

import sys
import csv
import re
import psycopg2

# Import database connection from existing config
sys.path.insert(0, '/var/www/cap_winds_app')
from db_config import get_connection

# Paths to cached CSV files
CACHE_DIR = "/var/www/cap_winds_app/.cache"
AIRPORTS_CSV = f"{CACHE_DIR}/airports.csv"
RUNWAYS_CSV = f"{CACHE_DIR}/runways.csv"

# Filtering criteria
MIN_RUNWAY_LENGTH = 2500  # feet
AIRPORT_TYPES = {'large_airport', 'medium_airport', 'small_airport'}
PAVED_SURFACE_CODES = {'ASP', 'ASPH', 'CON', 'CONC', 'CONCRETE', 'ASPHALT', 'PEM'}
MIXED_SURFACE_INDICATORS = {'GRVL', 'GRAVEL', 'DIRT', 'TURF', 'GRASS', 'SALT', 'SAND'}

# Military airfield identifier patterns
MILITARY_PATTERNS = [
    # Air Force & Space Force
    r'\bUSAF\b', r'\bAFB\b', r'\bAir Force Base\b', r'\bSFB\b', r'\bSpace Force Base\b',
    r'\bARB\b', r'\bAir Reserve Base\b', r'\bJARB\b', r'\bJRB\b',
    r'\bANGB\b', r'\bAir National Guard Base\b', r'\bANGS\b', r'\bAFS\b',
    # Army
    r'\bAAF\b', r'\bArmy Air Field\b', r'\bAAFld\b', r'\bArmy Airfield\b',
    r'\bAHP\b', r'\bArmy Heliport\b', r'\bAASF\b', r'\bALF\b',
    # Navy & Marine Corps
    r'\bNAS\b', r'\bNaval Air Station\b', r'\bNAF\b', r'\bNaval Air Facility\b',
    r'\bNOLF\b', r'\bMCAS\b', r'\bMarine Corps Air Station\b', r'\bMCAF\b',
    # Coast Guard
    r'\bCGAS\b', r'\bCoast Guard Air Station\b', r'\bUSCGAS\b',
    # Joint Bases
    r'\bJoint Base\b', r'\bJoint Reserve Station\b', r'\bJRS\b',
    # Legacy/Historical
    r'\bAAB\b', r'\bArmy Air Base\b',
]

MILITARY_REGEX = re.compile('|'.join(MILITARY_PATTERNS), re.IGNORECASE)

# State boundaries for iso_region determination
STATE_BOUNDARIES = {
    'AL': [-88.5, -84.8, 30.1, 35.1], 'AK': [172, -129, 51, 72],
    'AZ': [-114.8, -109.0, 31.3, 37.1], 'AR': [-94.6, -89.6, 33.0, 36.5],
    'CA': [-124.5, -114.1, 32.5, 42.1], 'CO': [-109.1, -102.0, 36.9, 41.1],
    'CT': [-73.8, -71.8, 40.9, 42.1], 'DE': [-75.8, -75.0, 38.4, 39.9],
    'FL': [-87.7, -80.0, 24.4, 31.1], 'GA': [-85.6, -80.8, 30.3, 35.1],
    'HI': [-160.3, -154.7, 18.9, 22.3], 'ID': [-117.3, -111.0, 41.9, 49.1],
    'IL': [-91.5, -87.5, 36.9, 42.6], 'IN': [-88.1, -84.8, 37.7, 41.8],
    'IA': [-96.7, -90.1, 40.3, 43.6], 'KS': [-102.1, -94.6, 36.9, 40.1],
    'KY': [-89.6, -81.9, 36.5, 39.2], 'LA': [-94.1, -88.8, 28.9, 33.1],
    'ME': [-71.1, -66.9, 43.0, 47.5], 'MD': [-79.5, -75.0, 37.9, 39.8],
    'MA': [-73.5, -69.9, 41.2, 42.9], 'MI': [-90.5, -82.1, 41.6, 48.3],
    'MN': [-97.3, -89.5, 43.5, 49.4], 'MS': [-91.7, -88.1, 30.1, 35.1],
    'MO': [-95.8, -89.1, 35.9, 40.7], 'MT': [-116.1, -104.0, 44.3, 49.1],
    'NE': [-104.1, -95.3, 39.9, 43.1], 'NV': [-120.1, -114.0, 35.0, 42.1],
    'NH': [-72.6, -70.6, 42.7, 45.4], 'NJ': [-75.6, -73.9, 38.9, 41.4],
    'NM': [-109.1, -103.0, 31.3, 37.1], 'NY': [-79.8, -71.8, 40.5, 45.1],
    'NC': [-84.4, -75.4, 33.8, 36.6], 'ND': [-104.1, -96.5, 45.9, 49.1],
    'OH': [-84.9, -80.5, 38.4, 42.0], 'OK': [-103.1, -94.4, 33.6, 37.1],
    'OR': [-124.7, -116.5, 41.9, 46.3], 'PA': [-80.6, -74.7, 39.7, 42.3],
    'RI': [-71.9, -71.1, 41.1, 42.1], 'SC': [-83.4, -78.5, 32.0, 35.3],
    'SD': [-104.1, -96.4, 42.5, 45.9], 'TN': [-90.4, -81.6, 34.9, 36.7],
    'TX': [-106.7, -93.5, 25.8, 36.6], 'UT': [-114.1, -109.0, 37.0, 42.1],
    'VT': [-73.5, -71.5, 42.7, 45.1], 'VA': [-83.8, -75.2, 36.5, 39.5],
    'WA': [-124.9, -116.9, 45.5, 49.1], 'WV': [-82.7, -77.7, 37.2, 40.7],
    'WI': [-92.9, -86.2, 42.5, 47.3], 'WY': [-111.1, -104.0, 41.0, 45.1],
}

def determine_iso_region(lat, lon):
    """Determine US state code from coordinates"""
    # Check territories first
    if -67.3 <= lon <= -65.2 and 17.9 <= lat <= 18.6:
        return 'PR'  # Puerto Rico
    if -65.1 <= lon <= -64.5 and 17.6 <= lat <= 18.5:
        return 'VI'  # Virgin Islands
    if 144.6 <= lon <= 145.0 and 13.2 <= lat <= 13.7:
        return 'GU'  # Guam
    
    # Check all US states
    for state_code, bounds in STATE_BOUNDARIES.items():
        west, east, south, north = bounds
        
        # Handle Alaska dateline crossing
        if state_code == 'AK':
            # Alaska: 172°E to -129°W (crosses dateline)
            if (lon >= 172 or lon <= -129) and south <= lat <= north:
                return f'US-{state_code}'
        else:
            # Normal state bounds
            if west <= lon <= east and south <= lat <= north:
                return f'US-{state_code}'
    
    return None  # Outside US

def is_military_airfield(name):
    """Determine if an airport is a military airfield based on its name"""
    if not name:
        return False
    return bool(MILITARY_REGEX.search(name))

def get_aviation_identifier(row):
    """Get the identifier used by aviation/weather systems"""
    icao = row.get('icao_code', '').strip()
    gps = row.get('gps_code', '').strip()
    ident = row.get('ident', '').strip()
    
    aviation_id = None
    if icao and len(icao) == 4 and '-' not in icao:
        aviation_id = icao
    elif gps and len(gps) == 4 and '-' not in gps:
        aviation_id = gps
    elif ident and len(ident) == 4 and '-' not in ident:
        aviation_id = ident
    
    database_id = ident
    return aviation_id, database_id

def is_pure_paved(surface):
    """Check if surface is pure paved (not mixed)"""
    if not surface:
        return False
    surface_upper = surface.upper()
    is_paved = any(surface_upper.startswith(code) for code in PAVED_SURFACE_CODES)
    is_mixed = any(indicator in surface_upper for indicator in MIXED_SURFACE_INDICATORS)
    return is_paved and not is_mixed

def load_qualifying_runways():
    """Load runways and identify airports with qualifying runways"""
    print("\nStep 1: Processing runways...")
    qualifying_airports = {}
    
    try:
        with open(RUNWAYS_CSV, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            runway_count = 0
            qualifying_count = 0
            
            for row in reader:
                runway_count += 1
                try:
                    surface = row.get('surface', '')
                    length_ft = float(row.get('length_ft', 0))
                    airport_ident = row.get('airport_ident', '')
                    
                    if is_pure_paved(surface) and length_ft >= MIN_RUNWAY_LENGTH:
                        qualifying_count += 1
                        if airport_ident not in qualifying_airports or length_ft > qualifying_airports[airport_ident]:
                            qualifying_airports[airport_ident] = int(length_ft)
                
                except (ValueError, KeyError):
                    continue
            
            print(f"  - Processed {runway_count:,} runways")
            print(f"  - Found {qualifying_count:,} qualifying runways")
            print(f"  - {len(qualifying_airports):,} airports with qualifying runways")
            
    except FileNotFoundError:
        print(f"ERROR: Could not find {RUNWAYS_CSV}")
        sys.exit(1)
    
    return qualifying_airports

def process_airports(qualifying_airports):
    """Process airports CSV and prepare data for database insertion"""
    print("\nStep 2: Processing airports...")
    airports_data = []
    
    try:
        with open(AIRPORTS_CSV, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            total_count = 0
            filtered_count = 0
            no_id_count = 0
            military_count = 0
            no_region_count = 0
            
            for row in reader:
                total_count += 1
                
                try:
                    aviation_id, database_id = get_aviation_identifier(row)
                    
                    if not aviation_id:
                        no_id_count += 1
                        continue
                    
                    if database_id not in qualifying_airports:
                        continue
                    
                    airport_type = row.get('type', '')
                    if airport_type not in AIRPORT_TYPES:
                        continue
                    
                    filtered_count += 1
                    
                    name = row.get('name', '').strip()
                    lat = float(row.get('latitude_deg', 0))
                    lon = float(row.get('longitude_deg', 0))
                    elevation_ft = int(float(row.get('elevation_ft', 0)))
                    longest_runway_ft = qualifying_airports[database_id]
                    
                    # Determine military status
                    is_military = is_military_airfield(name)
                    if is_military:
                        military_count += 1
                    
                    # Determine iso_region from coordinates
                    iso_region = determine_iso_region(lat, lon)
                    if not iso_region:
                        no_region_count += 1
                        # Keep the airport but with NULL iso_region
                    
                    airports_data.append({
                        'aviation_id': aviation_id,
                        'database_id': database_id,
                        'name': name,
                        'lat': lat,
                        'lon': lon,
                        'elevation': elevation_ft,
                        'longest_runway': longest_runway_ft,
                        'is_military': is_military,
                        'iso_region': iso_region
                    })
                    
                except (ValueError, KeyError):
                    continue
            
            print(f"  - Processed {total_count:,} airports")
            print(f"  - Skipped {no_id_count:,} without valid ID")
            print(f"  - Filtered to {filtered_count:,} qualifying airports")
            print(f"  - Identified {military_count:,} military airfields")
            print(f"  - {no_region_count:,} airports outside US states")
            
    except FileNotFoundError:
        print(f"ERROR: Could not find {AIRPORTS_CSV}")
        sys.exit(1)
    
    # Deduplicate by aviation_id (keep first occurrence)
    print(f"  - Deduplicating airports by aviation_id...")
    seen_ids = set()
    unique_data = []
    dup_count = 0
    for airport in airports_data:
        aviation_id = airport['aviation_id']
        if aviation_id not in seen_ids:
            seen_ids.add(aviation_id)
            unique_data.append(airport)
        else:
            dup_count += 1
            print(f"    Skipping duplicate {aviation_id}: {airport['name']}")
    
    print(f"  - Removed {dup_count} duplicate(s)")
    
    return unique_data

def repopulate_database(airports_data):
    """Repopulate database with airport data"""
    print("\nStep 3: Repopulating database...")
    
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        # Clear existing data
        print("  - Clearing existing airports...")
        cur.execute("DELETE FROM observations.airports")
        conn.commit()
        
        # Insert new data
        print(f"  - Inserting {len(airports_data):,} airports...")
        success_count = 0
        fail_count = 0
        
        for airport in airports_data:
            try:
                cur.execute("""
                    INSERT INTO observations.airports (
                        station_id,
                        name,
                        location,
                        elevation_ft,
                        has_paved_runway,
                        longest_runway_ft,
                        is_military,
                        iso_region
                    ) VALUES (
                        %s, %s, 
                        ST_SetSRID(ST_MakePoint(%s, %s), 4326),
                        %s, %s, %s, %s, %s
                    )
                """, (
                    airport['aviation_id'],
                    airport['name'],
                    airport['lon'],
                    airport['lat'],
                    airport['elevation'],
                    True,
                    airport['longest_runway'],
                    airport['is_military'],
                    airport['iso_region']
                ))
                
                success_count += 1
                if success_count % 500 == 0:
                    print(f"    Inserted {success_count:,} airports...")
                    conn.commit()
                
            except Exception as e:
                fail_count += 1
                print(f"    Warning: Failed {airport['aviation_id']}: {e}")
                continue
        
        conn.commit()
        
        # Get statistics
        cur.execute("SELECT COUNT(*) FROM observations.airports")
        total = cur.fetchone()[0]
        
        cur.execute("SELECT COUNT(*) FROM observations.airports WHERE is_military = true")
        military = cur.fetchone()[0]
        
        cur.execute("SELECT COUNT(*) FROM observations.airports WHERE iso_region LIKE 'US-%'")
        us_states = cur.fetchone()[0]
        
        print(f"\nStep 4: Complete!")
        print(f"  - Successfully inserted: {success_count:,} airports")
        print(f"  - Failed insertions: {fail_count}")
        print(f"  - Total in database: {total:,}")
        print(f"  - Military airfields: {military:,}")
        print(f"  - US states airports: {us_states:,}")
        
        # Show Colorado examples
        print(f"\nSample Colorado airports:")
        cur.execute("""
            SELECT station_id, name, is_military, iso_region, longest_runway_ft
            FROM observations.airports
            WHERE iso_region = 'US-CO'
            ORDER BY longest_runway_ft DESC
            LIMIT 10
        """)
        
        for station_id, name, is_mil, iso_reg, runway_ft in cur.fetchall():
            mil_flag = " [MILITARY]" if is_mil else ""
            print(f"  {station_id}: {name}{mil_flag} ({iso_reg}, {runway_ft} ft)")
        
        cur.close()
        conn.close()
        return True
        
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        try:
            conn.rollback()
            conn.close()
        except:
            pass
        return False

def main():
    print("=" * 70)
    print("REPOPULATE observations.airports - COMPLETE VERSION")
    print("With Military Identification and iso_region")
    print("=" * 70)
    
    qualifying_airports = load_qualifying_runways()
    airports_data = process_airports(qualifying_airports)
    
    success = repopulate_database(airports_data)
    
    if success:
        print("\n" + "=" * 70)
        print("✓ DATABASE REPOPULATION COMPLETE")
        print("=" * 70)
        return 0
    else:
        print("\n" + "=" * 70)
        print("✗ DATABASE REPOPULATION FAILED")
        print("=" * 70)
        return 1

if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
"""
Repopulate observations.airports table - WITH MILITARY IDENTIFICATION
Uses cached CSV files and Python's csv module (no pandas)
Identifies military airfields based on name patterns

Prerequisites:
- CSV files must be cached in /var/www/cap_winds_app/.cache/
- Run update_ourairports_cache.sh first to download files
- is_military column must exist in observations.airports table
"""
import sys
import csv
import psycopg2
import re

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

# Military airfield identifiers (based on US Military nomenclature)
MILITARY_PATTERNS = [
    # Air Force & Space Force
    r'\bUSAF\b',
    r'\bAFB\b',  # Air Force Base
    r'\bAir Force Base\b',
    r'\bSFB\b',  # Space Force Base
    r'\bSpace Force Base\b',
    r'\bARB\b',  # Air Reserve Base
    r'\bAir Reserve Base\b',
    r'\bJARB\b',  # Joint Air Reserve Base
    r'\bJRB\b',  # Joint Reserve Base
    r'\bANGB\b',  # Air National Guard Base
    r'\bAir National Guard Base\b',
    r'\bANGS\b',  # Air National Guard Station
    r'\bAFS\b',  # Air Force Station
    
    # Army
    r'\bAAF\b',  # Army Air Field
    r'\bArmy Air Field\b',
    r'\bAAFld\b',
    r'\bArmy Airfield\b',
    r'\bAHP\b',  # Army Heliport
    r'\bArmy Heliport\b',
    r'\bAASF\b',  # Army Aviation Support Facility
    r'\bALF\b',  # Army Landing Field
    
    # Navy & Marine Corps
    r'\bNAS\b',  # Naval Air Station
    r'\bNaval Air Station\b',
    r'\bNAF\b',  # Naval Air Facility
    r'\bNaval Air Facility\b',
    r'\bNOLF\b',  # Naval Outlying Landing Field
    r'\bMCAS\b',  # Marine Corps Air Station
    r'\bMarine Corps Air Station\b',
    r'\bMCAF\b',  # Marine Corps Air Facility
    
    # Coast Guard
    r'\bCGAS\b',  # Coast Guard Air Station
    r'\bCoast Guard Air Station\b',
    r'\bUSCGAS\b',
    
    # Joint Bases
    r'\bJoint Base\b',
    r'\bJoint Reserve Station\b',
    r'\bJRS\b',
    
    # Legacy/Historical
    r'\bAAB\b',  # Army Air Base
    r'\bArmy Air Base\b',
]

# Compile patterns for efficiency
MILITARY_REGEX = re.compile('|'.join(MILITARY_PATTERNS), re.IGNORECASE)

def is_military_airfield(name):
    """
    Determine if an airport is a military airfield based on its name
    
    Args:
        name: Airport name string
        
    Returns:
        bool: True if military airfield
    """
    if not name:
        return False
    
    return bool(MILITARY_REGEX.search(name))

def get_aviation_identifier(row):
    """
    Get the identifier used by aviation/weather systems
    Priority: icao_code > gps_code > ident (if 4 chars and valid)
    
    Returns: (aviation_id, database_id) tuple
    """
    icao = row.get('icao_code', '').strip()
    gps = row.get('gps_code', '').strip()
    ident = row.get('ident', '').strip()
    
    # Aviation ID - what pilots/weather systems use
    aviation_id = None
    if icao and len(icao) == 4 and not '-' in icao:
        aviation_id = icao
    elif gps and len(gps) == 4 and not '-' in gps:
        aviation_id = gps
    elif ident and len(ident) == 4 and not '-' in ident:
        aviation_id = ident
    
    # Database ID - what OurAirports uses for runway lookups
    database_id = ident
    
    return aviation_id, database_id

def is_pure_paved(surface):
    """Check if surface is pure paved (not mixed)"""
    if not surface:
        return False
    surface_upper = surface.upper()
    
    # Must start with paved code
    is_paved = any(surface_upper.startswith(code) for code in PAVED_SURFACE_CODES)
    
    # Must not contain mixed surface indicators
    is_mixed = any(indicator in surface_upper for indicator in MIXED_SURFACE_INDICATORS)
    
    return is_paved and not is_mixed

def load_qualifying_runways():
    """
    Load runways and identify airports with qualifying runways
    
    Returns: dict of {airport_ident: longest_runway_ft}
    """
    print("\nStep 1: Processing runways...")
    qualifying_airports = {}  # {ident: longest_runway_ft}
    
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
                    
                    # Check if runway qualifies
                    if is_pure_paved(surface) and length_ft >= MIN_RUNWAY_LENGTH:
                        qualifying_count += 1
                        # Keep track of longest runway for each airport
                        if airport_ident not in qualifying_airports or length_ft > qualifying_airports[airport_ident]:
                            qualifying_airports[airport_ident] = int(length_ft)
                
                except (ValueError, KeyError):
                    continue
            
            print(f"  Processed {runway_count:,} runways")
            print(f"  Found {qualifying_count:,} qualifying runways")
            print(f"  Identified {len(qualifying_airports):,} airports with qualifying runways")
            
    except FileNotFoundError:
        print(f"ERROR: Could not find {RUNWAYS_CSV}")
        print("Run update_ourairports_cache.sh first")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR processing runways: {e}")
        sys.exit(1)
    
    return qualifying_airports

def process_airports(qualifying_airports):
    """
    Process airports CSV and prepare data for database insertion
    
    Args:
        qualifying_airports: dict of {ident: longest_runway_ft}
        
    Returns: list of airport data tuples ready for insertion
    """
    print("\nStep 2: Processing airports...")
    airports_data = []
    
    try:
        with open(AIRPORTS_CSV, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            total_count = 0
            qualifying_count = 0
            military_count = 0
            no_aviation_id_count = 0
            
            for row in reader:
                total_count += 1
                
                try:
                    # Get identifiers
                    aviation_id, database_id = get_aviation_identifier(row)
                    
                    # Skip if no usable aviation identifier
                    if not aviation_id:
                        no_aviation_id_count += 1
                        continue
                    
                    # Skip if not in qualifying airports list
                    if database_id not in qualifying_airports:
                        continue
                    
                    # Check airport type
                    airport_type = row.get('type', '')
                    if airport_type not in AIRPORT_TYPES:
                        continue
                    
                    qualifying_count += 1
                    
                    # Get airport details
                    name = row.get('name', '').strip()
                    lat = float(row.get('latitude_deg', 0))
                    lon = float(row.get('longitude_deg', 0))
                    elevation_ft = int(float(row.get('elevation_ft', 0)))
                    longest_runway_ft = qualifying_airports[database_id]
                    
                    # Determine if military
                    is_military = is_military_airfield(name)
                    if is_military:
                        military_count += 1
                    
                    # Prepare data tuple for insertion
                    airports_data.append((
                        aviation_id,
                        name,
                        lon,
                        lat,
                        elevation_ft,
                        longest_runway_ft,
                        is_military
                    ))
                    
                except (ValueError, KeyError) as e:
                    continue
            
            print(f"  Processed {total_count:,} airports")
            print(f"  Skipped {no_aviation_id_count:,} airports without aviation identifier")
            print(f"  Found {qualifying_count:,} airports with qualifying runways")
            print(f"  Identified {military_count:,} military airfields")
            
    except FileNotFoundError:
        print(f"ERROR: Could not find {AIRPORTS_CSV}")
        print("Run update_ourairports_cache.sh first")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR processing airports: {e}")
        sys.exit(1)
    
    return airports_data

def populate_database(airports_data):
    """
    Populate the database with airport data
    
    Args:
        airports_data: list of airport tuples
    """
    print("\nStep 3: Populating database...")
    
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        # Clear existing data
        print("  Clearing existing airports...")
        cur.execute("DELETE FROM observations.airports")
        
        # Insert new data
        print(f"  Inserting {len(airports_data):,} airports...")
        
        insert_query = """
        INSERT INTO observations.airports 
        (station_id, name, location, elevation_ft, has_paved_runway, longest_runway_ft, is_military)
        VALUES (%s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s, true, %s, %s)
        """
        
        cur.executemany(insert_query, airports_data)
        
        # Get counts
        cur.execute("SELECT COUNT(*) FROM observations.airports")
        total = cur.fetchone()[0]
        
        cur.execute("SELECT COUNT(*) FROM observations.airports WHERE is_military = true")
        military = cur.fetchone()[0]
        
        cur.execute("SELECT COUNT(*) FROM observations.airports WHERE longest_runway_ft >= 5000")
        long_runway = cur.fetchone()[0]
        
        conn.commit()
        
        print(f"\n✓ Successfully populated observations.airports")
        print(f"  Total airports: {total:,}")
        print(f"  Military airfields: {military:,}")
        print(f"  Airports with 5000+ ft runways: {long_runway:,}")
        
        # Show some military examples
        print(f"\nSample military airfields:")
        cur.execute("""
            SELECT station_id, name 
            FROM observations.airports 
            WHERE is_military = true 
            ORDER BY longest_runway_ft DESC 
            LIMIT 10
        """)
        for station_id, name in cur.fetchall():
            print(f"  {station_id}: {name}")
        
    except Exception as e:
        if conn:
            conn.rollback()
        print(f"ERROR populating database: {e}")
        sys.exit(1)
    finally:
        if conn:
            conn.close()

def main():
    """Main execution"""
    print("=" * 70)
    print("Repopulating observations.airports with Military Identification")
    print("=" * 70)
    
    # Step 1: Load qualifying runways
    qualifying_airports = load_qualifying_runways()
    
    # Step 2: Process airports
    airports_data = process_airports(qualifying_airports)
    
    # Step 3: Populate database
    populate_database(airports_data)
    
    print("\n" + "=" * 70)
    print("✓ Airport repopulation complete!")
    print("=" * 70)

if __name__ == '__main__':
    main()

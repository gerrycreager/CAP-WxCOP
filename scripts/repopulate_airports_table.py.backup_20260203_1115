#!/usr/bin/env python3
"""
Repopulate observations.airports table - LIGHTWEIGHT VERSION
Uses cached CSV files and Python's csv module (no pandas)
Much faster and more efficient than pandas version

Prerequisites:
- CSV files must be cached in /var/www/cap_winds_app/.cache/
- Run update_ourairports_cache.sh first to download files
"""

import sys
import csv
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
PAVED_SURFACE_CODES = {'ASP', 'ASPH', 'CON', 'CONC', 'CONCRETE', 'ASPHALT'}
MIXED_SURFACE_INDICATORS = {'GRVL', 'GRAVEL', 'DIRT', 'TURF', 'GRASS', 'SALT', 'SAND'}


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
        
        print(f"  - Total runways: {runway_count}")
        print(f"  - Qualifying runways (paved >= {MIN_RUNWAY_LENGTH} ft): {qualifying_count}")
        print(f"  - Airports with qualifying runways: {len(qualifying_airports)}")
        
        return qualifying_airports
    
    except FileNotFoundError:
        print(f"ERROR: Runways file not found: {RUNWAYS_CSV}")
        print("Run update_ourairports_cache.sh first to download the file")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR processing runways: {e}")
        sys.exit(1)


def load_filtered_airports(qualifying_airports):
    """
    Load and filter airports
    Returns: list of airport dicts ready for database insertion
    """
    print("\nStep 2: Processing airports...")
    airports = []
    stats = {
        'total': 0,
        'us_only': 0,
        'correct_type': 0,
        'has_runway': 0,
        'has_aviation_id': 0,
        'duplicates': 0,
        'final': 0
    }
    
    seen_aviation_ids = set()
    
    try:
        with open(AIRPORTS_CSV, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            
            for row in reader:
                stats['total'] += 1
                
                # Filter 1: US only
                if row.get('iso_country') != 'US':
                    continue
                stats['us_only'] += 1
                
                # Filter 2: Airport type
                if row.get('type') not in AIRPORT_TYPES:
                    continue
                stats['correct_type'] += 1
                
                # Filter 3: Has qualifying runway
                database_id = row.get('ident', '').strip()
                if database_id not in qualifying_airports:
                    continue
                stats['has_runway'] += 1
                
                # Get aviation identifier
                aviation_id, _ = get_aviation_identifier(row)
                if not aviation_id:
                    continue
                stats['has_aviation_id'] += 1
                
                # Check for duplicates
                if aviation_id in seen_aviation_ids:
                    stats['duplicates'] += 1
                    continue
                seen_aviation_ids.add(aviation_id)
                
                # Get coordinates and elevation
                try:
                    lat = float(row['latitude_deg'])
                    lon = float(row['longitude_deg'])
                    
                    elevation = None
                    if row.get('elevation_ft'):
                        try:
                            elevation = int(float(row['elevation_ft']))
                        except:
                            pass
                    
                    longest_runway = qualifying_airports[database_id]
                    
                    airports.append({
                        'aviation_id': aviation_id,
                        'name': row.get('name', ''),
                        'lat': lat,
                        'lon': lon,
                        'elevation': elevation,
                        'longest_runway': longest_runway
                    })
                    stats['final'] += 1
                    
                except (ValueError, KeyError):
                    continue
        
        print(f"  - Total airports: {stats['total']}")
        print(f"  - US airports: {stats['us_only']}")
        print(f"  - Correct type: {stats['correct_type']}")
        print(f"  - With qualifying runway: {stats['has_runway']}")
        print(f"  - With aviation ID: {stats['has_aviation_id']}")
        print(f"  - Duplicates excluded: {stats['duplicates']}")
        print(f"  - Final filtered: {stats['final']}")
        
        return airports
    
    except FileNotFoundError:
        print(f"ERROR: Airports file not found: {AIRPORTS_CSV}")
        print("Run update_ourairports_cache.sh first to download the file")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR processing airports: {e}")
        sys.exit(1)


def repopulate_database(airports):
    """
    Clear and repopulate the observations.airports table
    """
    try:
        print("\nStep 3: Connecting to database...")
        conn = get_connection()
        cur = conn.cursor()
        
        print("\nWARNING: This will delete ALL existing data in observations.airports")
        response = input("Continue? (yes/no): ").strip().lower()
        if response != 'yes':
            print("Operation cancelled")
            return False
        
        print("\nStep 4: Clearing existing airports table...")
        cur.execute("DELETE FROM observations.airports")
        deleted_count = cur.rowcount
        conn.commit()
        print(f"  - Deleted {deleted_count} existing records")
        
        print("\nStep 5: Inserting filtered airports...")
        success_count = 0
        fail_count = 0
        
        for airport in airports:
            try:
                cur.execute("""
                    INSERT INTO observations.airports (
                        station_id,
                        name,
                        location,
                        elevation_ft,
                        has_paved_runway,
                        longest_runway_ft
                    ) VALUES (
                        %s, %s, 
                        ST_SetSRID(ST_MakePoint(%s, %s), 4326),
                        %s, %s, %s
                    )
                """, (
                    airport['aviation_id'],
                    airport['name'],
                    airport['lon'],
                    airport['lat'],
                    airport['elevation'],
                    True,
                    airport['longest_runway']
                ))
                
                success_count += 1
                
                if success_count % 500 == 0:
                    print(f"  - Inserted {success_count} airports...")
                    conn.commit()
                
            except Exception as e:
                fail_count += 1
                if 'duplicate key' in str(e).lower():
                    print(f"  - Warning: Duplicate key for {airport['aviation_id']}")
                else:
                    print(f"  - Warning: Failed to insert {airport['aviation_id']}: {e}")
                continue
        
        conn.commit()
        
        # Get final count
        cur.execute("SELECT COUNT(*) FROM observations.airports")
        final_count = cur.fetchone()[0]
        
        print(f"\nStep 6: Complete!")
        print(f"  - Successfully inserted: {success_count} airports")
        print(f"  - Failed insertions: {fail_count}")
        print(f"  - Final table count: {final_count}")
        
        # Show sample
        print(f"\nSample of inserted airports:")
        cur.execute("""
            SELECT station_id, name, longest_runway_ft
            FROM observations.airports
            ORDER BY longest_runway_ft DESC NULLS LAST
            LIMIT 10
        """)
        
        for station_id, name, runway_ft in cur.fetchall():
            print(f"  {station_id}: {name} (longest runway: {runway_ft} ft)")
        
        cur.close()
        conn.close()
        
        return True
        
    except Exception as e:
        print(f"\nError: {e}")
        try:
            conn.rollback()
            cur.close()
            conn.close()
        except:
            pass
        return False


def main():
    print("=" * 70)
    print("REPOPULATE observations.airports - LIGHTWEIGHT VERSION")
    print("=" * 70)
    print("Uses cached CSV files from OurAirports")
    print(f"Criteria:")
    print(f"  - Paved surfaces: asphalt/concrete (pure, not mixed)")
    print(f"  - Minimum runway length: {MIN_RUNWAY_LENGTH} ft")
    print(f"  - Airport types: {', '.join(AIRPORT_TYPES)}")
    print(f"  - Using ICAO/GPS codes as station_id")
    
    # Load runways and get qualifying airports
    qualifying_airports = load_qualifying_runways()
    
    # Load and filter airports
    airports = load_filtered_airports(qualifying_airports)
    
    # Show sample
    print("\nSample of airports to be inserted:")
    for airport in airports[:10]:
        print(f"  {airport['aviation_id']}: {airport['name']} ({airport['longest_runway']} ft)")
    
    # Repopulate database
    success = repopulate_database(airports)
    
    if success:
        print("\n" + "=" * 70)
        print("DATABASE REPOPULATION COMPLETE")
        print("=" * 70)
        return 0
    else:
        print("\n" + "=" * 70)
        print("DATABASE REPOPULATION FAILED")
        print("=" * 70)
        return 1


if __name__ == '__main__':
    sys.exit(main())


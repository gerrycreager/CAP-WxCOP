#!/var/www/cap_winds_app/venv/bin/python3
"""
KQ Temporary Station Manager
Manage custom stations (KQ temporary fire weather stations, etc.)

Usage:
  # Add a new KQ station
  ./manage_kq_stations.py add KQXX 34.5 -118.2 --name "Fire Camp Alpha" --elevation 2500
  
  # List all KQ stations
  ./manage_kq_stations.py list
  
  # Deactivate a station
  ./manage_kq_stations.py deactivate KQXX
  
  # Import from CSV
  ./manage_kq_stations.py import stations.csv
"""
import sys
import os
import argparse
import csv

sys.path.insert(0, '/var/www/cap_winds_app')

try:
    import psycopg2
    from db_config import get_connection
except ImportError as e:
    print(f"Missing dependency: {e}")
    sys.exit(1)

def add_station(station_id, lat, lon, name=None, elevation=None, notes=None):
    """Add or update a custom station"""
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        cur.execute("""
            INSERT INTO observations.custom_stations 
            (station_id, name, latitude, longitude, elevation_ft, notes, active)
            VALUES (%s, %s, %s, %s, %s, %s, true)
            ON CONFLICT (station_id) DO UPDATE SET
                name = EXCLUDED.name,
                latitude = EXCLUDED.latitude,
                longitude = EXCLUDED.longitude,
                elevation_ft = EXCLUDED.elevation_ft,
                notes = EXCLUDED.notes,
                active = true
        """, (station_id, name, lat, lon, elevation, notes))
        
        conn.commit()
        cur.close()
        conn.close()
        
        print(f"✓ Added/updated station: {station_id}")
        if name:
            print(f"  Name: {name}")
        print(f"  Location: {lat}, {lon}")
        if elevation:
            print(f"  Elevation: {elevation} ft")
        
        return True
        
    except Exception as e:
        print(f"✗ Failed to add station: {e}")
        return False

def list_stations(active_only=True):
    """List all custom stations"""
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        query = """
            SELECT station_id, name, latitude, longitude, elevation_ft, notes, active, created_at
            FROM observations.custom_stations
        """
        
        if active_only:
            query += " WHERE active = true"
        
        query += " ORDER BY station_id"
        
        cur.execute(query)
        rows = cur.fetchall()
        
        if not rows:
            print("No custom stations found")
            return
        
        print(f"\n{'ID':<8} {'Name':<30} {'Lat':<10} {'Lon':<11} {'Elev':<8} {'Active':<8}")
        print("-" * 85)
        
        for row in rows:
            station_id, name, lat, lon, elev, notes, active, created = row
            name_str = (name or '')[:28]
            elev_str = f"{elev}" if elev else ''
            active_str = "✓" if active else "✗"
            
            print(f"{station_id:<8} {name_str:<30} {lat:<10.4f} {lon:<11.4f} {elev_str:<8} {active_str:<8}")
            
            if notes and not active_only:
                print(f"         Notes: {notes}")
        
        print(f"\nTotal: {len(rows)} stations")
        
        cur.close()
        conn.close()
        
    except Exception as e:
        print(f"✗ Failed to list stations: {e}")

def deactivate_station(station_id):
    """Deactivate a station"""
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        cur.execute("""
            UPDATE observations.custom_stations
            SET active = false
            WHERE station_id = %s
        """, (station_id,))
        
        if cur.rowcount == 0:
            print(f"✗ Station not found: {station_id}")
            return False
        
        conn.commit()
        cur.close()
        conn.close()
        
        print(f"✓ Deactivated station: {station_id}")
        return True
        
    except Exception as e:
        print(f"✗ Failed to deactivate station: {e}")
        return False

def activate_station(station_id):
    """Reactivate a station"""
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        cur.execute("""
            UPDATE observations.custom_stations
            SET active = true
            WHERE station_id = %s
        """, (station_id,))
        
        if cur.rowcount == 0:
            print(f"✗ Station not found: {station_id}")
            return False
        
        conn.commit()
        cur.close()
        conn.close()
        
        print(f"✓ Activated station: {station_id}")
        return True
        
    except Exception as e:
        print(f"✗ Failed to activate station: {e}")
        return False

def import_csv(filepath):
    """
    Import stations from CSV file
    CSV format: station_id,name,latitude,longitude,elevation_ft,notes
    """
    try:
        count_success = 0
        count_failed = 0
        
        with open(filepath, 'r') as f:
            reader = csv.DictReader(f)
            
            for row in reader:
                station_id = row['station_id'].strip().upper()
                name = row.get('name', '').strip() or None
                
                try:
                    lat = float(row['latitude'])
                    lon = float(row['longitude'])
                except ValueError:
                    print(f"✗ Invalid coordinates for {station_id}")
                    count_failed += 1
                    continue
                
                elevation = None
                if row.get('elevation_ft'):
                    try:
                        elevation = int(row['elevation_ft'])
                    except ValueError:
                        pass
                
                notes = row.get('notes', '').strip() or None
                
                if add_station(station_id, lat, lon, name, elevation, notes):
                    count_success += 1
                else:
                    count_failed += 1
        
        print(f"\n✓ Import complete: {count_success} successful, {count_failed} failed")
        
    except FileNotFoundError:
        print(f"✗ File not found: {filepath}")
    except Exception as e:
        print(f"✗ Failed to import: {e}")

def export_csv(filepath):
    """Export all stations to CSV"""
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT station_id, name, latitude, longitude, elevation_ft, notes, active
            FROM observations.custom_stations
            ORDER BY station_id
        """)
        
        rows = cur.fetchall()
        
        with open(filepath, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['station_id', 'name', 'latitude', 'longitude', 'elevation_ft', 'notes', 'active'])
            
            for row in rows:
                writer.writerow(row)
        
        print(f"✓ Exported {len(rows)} stations to {filepath}")
        
        cur.close()
        conn.close()
        
    except Exception as e:
        print(f"✗ Failed to export: {e}")

def main():
    parser = argparse.ArgumentParser(
        description='Manage custom weather stations (KQ temporary stations, etc.)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Add a fire weather station
  %(prog)s add KQFW 34.2345 -118.5678 --name "Fire Camp Weather"
  
  # List all active stations
  %(prog)s list
  
  # Import from CSV
  %(prog)s import kq_stations.csv
        """
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Command to execute')
    
    # Add command
    add_parser = subparsers.add_parser('add', help='Add or update a station')
    add_parser.add_argument('station_id', help='Station ID (e.g., KQFW)')
    add_parser.add_argument('latitude', type=float, help='Latitude (decimal degrees)')
    add_parser.add_argument('longitude', type=float, help='Longitude (decimal degrees)')
    add_parser.add_argument('--name', help='Station name')
    add_parser.add_argument('--elevation', type=int, help='Elevation in feet')
    add_parser.add_argument('--notes', help='Additional notes')
    
    # List command
    list_parser = subparsers.add_parser('list', help='List stations')
    list_parser.add_argument('--all', action='store_true', help='Include inactive stations')
    
    # Deactivate command
    deact_parser = subparsers.add_parser('deactivate', help='Deactivate a station')
    deact_parser.add_argument('station_id', help='Station ID to deactivate')
    
    # Activate command
    act_parser = subparsers.add_parser('activate', help='Activate a station')
    act_parser.add_argument('station_id', help='Station ID to activate')
    
    # Import command
    import_parser = subparsers.add_parser('import', help='Import stations from CSV')
    import_parser.add_argument('filepath', help='Path to CSV file')
    
    # Export command
    export_parser = subparsers.add_parser('export', help='Export stations to CSV')
    export_parser.add_argument('filepath', help='Path to output CSV file')
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    if args.command == 'add':
        add_station(
            args.station_id.upper(),
            args.latitude,
            args.longitude,
            args.name,
            args.elevation,
            args.notes
        )
    
    elif args.command == 'list':
        list_stations(active_only=not args.all)
    
    elif args.command == 'deactivate':
        deactivate_station(args.station_id.upper())
    
    elif args.command == 'activate':
        activate_station(args.station_id.upper())
    
    elif args.command == 'import':
        import_csv(args.filepath)
    
    elif args.command == 'export':
        export_csv(args.filepath)

if __name__ == '__main__':
    main()

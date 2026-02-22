#!/var/www/cap_winds_app/venv/bin/python3
"""
Cleanup Invalid Wind Forecasts
Removes wind forecast data for non-airports and invalid identifiers
Should be run after repopulating the airports table

Usage:
  ./cleanup_invalid_forecasts.py [--dry-run] [--verbose]
"""
import sys
import os
import logging
import argparse
from datetime import datetime

sys.path.insert(0, '/var/www/cap_winds_app')
from db_config import get_connection

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)


def analyze_invalid_forecasts(conn):
    """
    Analyze the model_wind_forecasts table to find invalid entries
    Returns dict with counts and samples
    """
    cur = conn.cursor()
    
    analysis = {}
    
    # Total forecasts
    cur.execute("SELECT COUNT(*) FROM observations.model_wind_forecasts")
    analysis['total_forecasts'] = cur.fetchone()[0]
    
    # Unique station IDs in forecasts
    cur.execute("SELECT COUNT(DISTINCT station_id) FROM observations.model_wind_forecasts")
    analysis['unique_stations'] = cur.fetchone()[0]
    
    # Station IDs not in airports table
    cur.execute("""
        SELECT COUNT(DISTINCT mwf.station_id)
        FROM observations.model_wind_forecasts mwf
        LEFT JOIN observations.airports a ON mwf.station_id = a.station_id
        WHERE a.station_id IS NULL
    """)
    analysis['stations_not_in_airports'] = cur.fetchone()[0]
    
    # Forecasts with invalid station IDs (not in airports table)
    cur.execute("""
        SELECT COUNT(*)
        FROM observations.model_wind_forecasts mwf
        LEFT JOIN observations.airports a ON mwf.station_id = a.station_id
        WHERE a.station_id IS NULL
    """)
    analysis['forecasts_with_invalid_stations'] = cur.fetchone()[0]
    
    # Sample invalid station IDs
    cur.execute("""
        SELECT DISTINCT mwf.station_id
        FROM observations.model_wind_forecasts mwf
        LEFT JOIN observations.airports a ON mwf.station_id = a.station_id
        WHERE a.station_id IS NULL
        ORDER BY mwf.station_id
        LIMIT 20
    """)
    analysis['sample_invalid_stations'] = [row[0] for row in cur.fetchall()]
    
    # Station IDs with hyphens (US-xxxx pattern)
    cur.execute("""
        SELECT COUNT(DISTINCT station_id)
        FROM observations.model_wind_forecasts
        WHERE station_id LIKE '%-%'
    """)
    analysis['stations_with_hyphens'] = cur.fetchone()[0]
    
    # Station IDs not exactly 4 characters
    cur.execute("""
        SELECT COUNT(DISTINCT station_id)
        FROM observations.model_wind_forecasts
        WHERE LENGTH(station_id) != 4
    """)
    analysis['stations_wrong_length'] = cur.fetchone()[0]
    
    # Valid forecasts (exist in airports table)
    cur.execute("""
        SELECT COUNT(*)
        FROM observations.model_wind_forecasts mwf
        INNER JOIN observations.airports a ON mwf.station_id = a.station_id
    """)
    analysis['valid_forecasts'] = cur.fetchone()[0]
    
    cur.close()
    return analysis


def cleanup_invalid_forecasts(conn, dry_run=False):
    """
    Remove forecast entries for station IDs not in airports table
    Returns count of deleted rows
    """
    cur = conn.cursor()
    
    if dry_run:
        log.info("DRY RUN: No data will be deleted")
        
        # Just count what would be deleted
        cur.execute("""
            SELECT COUNT(*)
            FROM observations.model_wind_forecasts mwf
            LEFT JOIN observations.airports a ON mwf.station_id = a.station_id
            WHERE a.station_id IS NULL
        """)
        would_delete = cur.fetchone()[0]
        
        cur.close()
        return would_delete
    
    else:
        log.info("DELETING invalid forecast entries...")
        
        # Delete forecasts for station IDs not in airports table
        cur.execute("""
            DELETE FROM observations.model_wind_forecasts
            WHERE station_id IN (
                SELECT DISTINCT mwf.station_id
                FROM observations.model_wind_forecasts mwf
                LEFT JOIN observations.airports a ON mwf.station_id = a.station_id
                WHERE a.station_id IS NULL
            )
        """)
        
        deleted_count = cur.rowcount
        conn.commit()
        
        cur.close()
        return deleted_count


def vacuum_analyze(conn):
    """Run VACUUM ANALYZE to reclaim space and update statistics"""
    log.info("Running VACUUM ANALYZE on model_wind_forecasts table...")
    
    # Need to close the transaction and use autocommit for VACUUM
    old_isolation = conn.isolation_level
    conn.set_isolation_level(0)  # autocommit
    
    cur = conn.cursor()
    cur.execute("VACUUM ANALYZE observations.model_wind_forecasts")
    cur.close()
    
    conn.set_isolation_level(old_isolation)
    log.info("✓ VACUUM ANALYZE complete")


def main():
    parser = argparse.ArgumentParser(
        description='Cleanup invalid wind forecasts from database'
    )
    parser.add_argument('--dry-run', action='store_true',
                       help='Show what would be deleted without actually deleting')
    parser.add_argument('--verbose', action='store_true',
                       help='Show detailed analysis')
    parser.add_argument('--skip-vacuum', action='store_true',
                       help='Skip VACUUM ANALYZE after cleanup')
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    log.info("=" * 70)
    log.info(f"Wind Forecast Cleanup - {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    log.info("=" * 70)
    
    # Connect to database
    try:
        conn = get_connection()
        log.info("✓ Connected to database")
    except Exception as e:
        log.error(f"Failed to connect to database: {e}")
        return 1
    
    # Analyze current state
    log.info("\nAnalyzing model_wind_forecasts table...")
    analysis = analyze_invalid_forecasts(conn)
    
    log.info(f"\n{'=' * 70}")
    log.info("CURRENT STATE:")
    log.info(f"{'=' * 70}")
    log.info(f"Total forecast entries: {analysis['total_forecasts']:,}")
    log.info(f"Unique station IDs: {analysis['unique_stations']:,}")
    log.info(f"Valid forecasts (in airports table): {analysis['valid_forecasts']:,}")
    log.info(f"\n{'INVALID DATA:':^70}")
    log.info(f"Station IDs not in airports table: {analysis['stations_not_in_airports']:,}")
    log.info(f"Forecast entries with invalid stations: {analysis['forecasts_with_invalid_stations']:,}")
    log.info(f"Station IDs with hyphens (US-xxxx): {analysis['stations_with_hyphens']:,}")
    log.info(f"Station IDs wrong length (!= 4 chars): {analysis['stations_wrong_length']:,}")
    
    if args.verbose and analysis['sample_invalid_stations']:
        log.info(f"\n{'SAMPLE INVALID STATION IDs:':^70}")
        for station_id in analysis['sample_invalid_stations']:
            log.info(f"  {station_id}")
    
    # Calculate what will be removed
    invalid_count = analysis['forecasts_with_invalid_stations']
    valid_count = analysis['valid_forecasts']
    
    if invalid_count == 0:
        log.info(f"\n{'=' * 70}")
        log.info("✓ No invalid forecasts found - database is clean!")
        log.info(f"{'=' * 70}")
        conn.close()
        return 0
    
    # Show impact
    percent_invalid = (invalid_count / analysis['total_forecasts'] * 100) if analysis['total_forecasts'] > 0 else 0
    
    log.info(f"\n{'=' * 70}")
    log.info(f"CLEANUP IMPACT:")
    log.info(f"{'=' * 70}")
    log.info(f"Will DELETE: {invalid_count:,} forecast entries ({percent_invalid:.1f}%)")
    log.info(f"Will KEEP: {valid_count:,} forecast entries ({100-percent_invalid:.1f}%)")
    
    # Confirm if not dry run
    if not args.dry_run:
        log.info(f"\n{'⚠ WARNING ⚠':^70}")
        log.info("This will permanently delete forecast data!")
        response = input("\nContinue with cleanup? (yes/no): ").strip().lower()
        
        if response != 'yes':
            log.info("Cleanup cancelled")
            conn.close()
            return 0
    
    # Perform cleanup
    log.info(f"\n{'=' * 70}")
    if args.dry_run:
        deleted = cleanup_invalid_forecasts(conn, dry_run=True)
        log.info(f"DRY RUN: Would delete {deleted:,} forecast entries")
    else:
        deleted = cleanup_invalid_forecasts(conn, dry_run=False)
        log.info(f"✓ Deleted {deleted:,} invalid forecast entries")
        
        # Vacuum unless skipped
        if not args.skip_vacuum:
            vacuum_analyze(conn)
    
    # Final analysis
    if not args.dry_run:
        log.info("\nFinal verification...")
        final_analysis = analyze_invalid_forecasts(conn)
        
        log.info(f"\n{'=' * 70}")
        log.info("AFTER CLEANUP:")
        log.info(f"{'=' * 70}")
        log.info(f"Total forecast entries: {final_analysis['total_forecasts']:,}")
        log.info(f"Valid forecasts: {final_analysis['valid_forecasts']:,}")
        log.info(f"Invalid stations remaining: {final_analysis['stations_not_in_airports']:,}")
        
        if final_analysis['stations_not_in_airports'] > 0:
            log.warning(f"⚠ Warning: {final_analysis['stations_not_in_airports']} invalid stations still remain")
        else:
            log.info("✓ All invalid forecasts have been removed!")
    
    conn.close()
    
    log.info(f"\n{'=' * 70}")
    log.info("✓ Cleanup complete")
    log.info(f"{'=' * 70}")
    
    return 0


if __name__ == '__main__':
    sys.exit(main())


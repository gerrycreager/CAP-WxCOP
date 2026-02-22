#!/var/www/cap_winds_app/venv/bin/python3
"""
Batch Map Generation Script - CORRECTED VERSION
Generates aviation wind maps for all configured regions
Maps are overwritten each run (no archive)

Usage:
  ./batch_generate_maps.py

Cron:
  5 * * * * /var/www/cap_winds_app/batch_generate_maps.py >> /var/log/batch_maps.log 2>&1
"""

import sys
import os
import logging
from datetime import datetime

# Add app directory to path
sys.path.insert(0, '/var/www/cap_winds_app')

try:
    from states_service import StatesService
except ImportError as e:
    print(f"Error importing StatesService: {e}")
    sys.exit(1)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)

# Output directory
OUTPUT_DIR = "/var/www/html/cap_winds"

# Region configurations (ONLY regions that are implemented in states_service.py)
# Each tuple: (region_code, display_name, output_filename)
REGIONS = [
    ("CONUS", "Continental United States", "conus.png"),
    ("NCR", "North Central Region", "ncr.png"),
    ("GLR", "Great Lakes Region", "glr.png"),
    ("NER", "Northeast Region", "ner.png"),
    ("PCR", "Pacific Region", "pcr.png"),
    ("PCR-WEST", "PCR-West Coast", "pcr-west.png"),
    ("PCR-AK", "PCR-Alaska", "pcr-ak.png"),
    ("PCR-HI", "PCR-Hawaii", "pcr-hi.png"),
    ("PCR-GUAM", "PCR-Guam", "pcr-guam.png"),
    ("RMR", "Rocky Mountain Region", "rmr.png"),
    ("SER", "Southeast Region", "ser.png"),
    ("SER-CARIB", "SER-Caribbean", "ser-carib.png"),
    ("SWR", "Southwest Region", "swr.png"),
]

def generate_region_map(service, region_code, display_name, output_filename):
    """
    Generate map for a single region
    Returns True on success, False on failure
    """
    try:
        log.info(f"Starting: {display_name}")
        
        # Generate the analysis
        result = service.generate_analysis(
            location_or_coords=region_code,
            radius_nm=None,  # Use region boundaries
            output_path=OUTPUT_DIR,
            model="auto",  # Will choose HRRR for CONUS, GFS for OCONUS
            output_format="png",
            include_shapefiles=True
        )
        
        if result and 'maps' in result and result['maps']:
            # Get the generated map path
            generated_map = result['maps'][0]
            
            # Rename to static filename (overwrite previous)
            static_path = os.path.join(OUTPUT_DIR, output_filename)
            if os.path.exists(generated_map) and generated_map != static_path:
                os.rename(generated_map, static_path)
                log.info(f"✓ Success: {display_name}")
                log.info(f"  → {output_filename}")
            else:
                log.info(f"✓ Success: {display_name}")
                log.info(f"  → {output_filename}")
            
            return True
        else:
            log.error(f"✗ Failed: {display_name} - No maps generated")
            return False
            
    except Exception as e:
        log.error(f"✗ Failed: {display_name} - {str(e)}")
        return False


def cleanup_old_timestamped_maps():
    """
    Clean up old timestamped maps from previous versions
    Keeps only the new static-named maps
    """
    if not os.path.exists(OUTPUT_DIR):
        return
    
    try:
        # Find all timestamped maps (contain date patterns like _041245ZJAN26)
        import re
        timestamped_pattern = re.compile(r'.*_\d{6}Z[A-Z]{3}\d{2}\.png$')
        
        removed_count = 0
        for filename in os.listdir(OUTPUT_DIR):
            if timestamped_pattern.match(filename):
                filepath = os.path.join(OUTPUT_DIR, filename)
                try:
                    os.remove(filepath)
                    removed_count += 1
                except Exception as e:
                    log.debug(f"Could not remove {filename}: {e}")
        
        if removed_count > 0:
            log.info(f"Cleaned up {removed_count} old timestamped maps")
            
    except Exception as e:
        log.debug(f"Cleanup error: {e}")


def main():
    """Main execution"""
    log.info("=" * 60)
    log.info(f"Batch Map Generation Started: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    log.info("=" * 60)
    
    # Ensure output directory exists
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    # Initialize service
    try:
        service = StatesService()
    except Exception as e:
        log.error(f"Failed to initialize StatesService: {e}")
        sys.exit(1)
    
    # Track results
    succeeded = []
    failed = []
    
    # Generate each region map
    for region_code, display_name, output_filename in REGIONS:
        success = generate_region_map(service, region_code, display_name, output_filename)
        
        if success:
            succeeded.append(display_name)
        else:
            failed.append(display_name)
    
    # Clean up old timestamped maps
    cleanup_old_timestamped_maps()
    
    # Summary
    log.info("=" * 60)
    log.info(f"Batch Generation Complete: {len(succeeded)} succeeded, {len(failed)} failed")
    if failed:
        log.info(f"Failed regions: {', '.join(failed)}")
    log.info("=" * 60)
    
    # Exit with appropriate code
    sys.exit(0 if len(failed) == 0 else 1)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        log.info("Interrupted by user")
        sys.exit(1)
    except Exception as e:
        log.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)

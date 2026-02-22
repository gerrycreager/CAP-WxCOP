"""
Radar Cache Management System
Manages georeferenced PNG cache for radar imagery

Features:
- Automatic cleanup of old files
- Cache size management
- Statistics tracking
- Performance monitoring
"""

import os
import time
import glob
from datetime import datetime, timedelta
from pathlib import Path
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
CACHE_RETENTION_HOURS = 24  # Keep cached files for 24 hours
MAX_CACHE_FILES_PER_PRODUCT = 288  # 24 hours at 5-minute intervals
CLEANUP_INTERVAL_SECONDS = 3600  # Run cleanup every hour

# Base path for radar data
RADAR_BASE_PATH = '/LDM/radar/level3'


def get_cache_stats(site_id=None, product=None):
    """
    Get cache statistics
    
    Args:
        site_id: Specific site to check (None = all sites)
        product: Specific product to check (None = all products)
        
    Returns:
        dict: Cache statistics
    """
    stats = {
        'total_files': 0,
        'total_size_mb': 0,
        'by_site': {},
        'oldest_file': None,
        'newest_file': None
    }
    
    if site_id:
        sites = [site_id.upper()]
    else:
        sites = [d for d in os.listdir(RADAR_BASE_PATH) 
                if os.path.isdir(os.path.join(RADAR_BASE_PATH, d))]
    
    oldest_time = None
    newest_time = None
    
    for site in sites:
        site_path = os.path.join(RADAR_BASE_PATH, site)
        
        if product:
            products = [product.upper()]
        else:
            products = [d for d in os.listdir(site_path) 
                       if os.path.isdir(os.path.join(site_path, d))]
        
        for prod in products:
            geo_path = os.path.join(site_path, prod, 'geo')
            
            if not os.path.exists(geo_path):
                continue
            
            # Count PNG files
            png_files = glob.glob(os.path.join(geo_path, '**', '*.png'), recursive=True)
            
            site_prod_key = f"{site}_{prod}"
            
            if site_prod_key not in stats['by_site']:
                stats['by_site'][site_prod_key] = {
                    'files': 0,
                    'size_mb': 0
                }
            
            for png_file in png_files:
                stats['total_files'] += 1
                
                file_size = os.path.getsize(png_file)
                stats['total_size_mb'] += file_size / (1024 * 1024)
                stats['by_site'][site_prod_key]['files'] += 1
                stats['by_site'][site_prod_key]['size_mb'] += file_size / (1024 * 1024)
                
                # Track oldest/newest
                mtime = os.path.getmtime(png_file)
                if oldest_time is None or mtime < oldest_time:
                    oldest_time = mtime
                    stats['oldest_file'] = png_file
                
                if newest_time is None or mtime > newest_time:
                    newest_time = mtime
                    stats['newest_file'] = png_file
    
    # Round sizes
    stats['total_size_mb'] = round(stats['total_size_mb'], 2)
    for key in stats['by_site']:
        stats['by_site'][key]['size_mb'] = round(stats['by_site'][key]['size_mb'], 2)
    
    # Add timestamps
    if oldest_time:
        stats['oldest_time'] = datetime.fromtimestamp(oldest_time).isoformat()
    if newest_time:
        stats['newest_time'] = datetime.fromtimestamp(newest_time).isoformat()
    
    return stats


def cleanup_old_files(site_id=None, product=None, max_age_hours=CACHE_RETENTION_HOURS, dry_run=False):
    """
    Clean up old cached radar files
    
    Args:
        site_id: Specific site to clean (None = all sites)
        product: Specific product to clean (None = all products)
        max_age_hours: Maximum age in hours
        dry_run: If True, only report what would be deleted
        
    Returns:
        dict: Cleanup statistics
    """
    cutoff_time = time.time() - (max_age_hours * 3600)
    
    stats = {
        'files_checked': 0,
        'files_deleted': 0,
        'size_freed_mb': 0,
        'errors': []
    }
    
    if site_id:
        sites = [site_id.upper()]
    else:
        sites = [d for d in os.listdir(RADAR_BASE_PATH) 
                if os.path.isdir(os.path.join(RADAR_BASE_PATH, d))]
    
    for site in sites:
        site_path = os.path.join(RADAR_BASE_PATH, site)
        
        if product:
            products = [product.upper()]
        else:
            products = [d for d in os.listdir(site_path) 
                       if os.path.isdir(os.path.join(site_path, d))]
        
        for prod in products:
            geo_path = os.path.join(site_path, prod, 'geo')
            
            if not os.path.exists(geo_path):
                continue
            
            # Find old PNG and JSON files
            for ext in ['*.png', '*.json']:
                files = glob.glob(os.path.join(geo_path, '**', ext), recursive=True)
                
                for filepath in files:
                    stats['files_checked'] += 1
                    
                    try:
                        mtime = os.path.getmtime(filepath)
                        
                        if mtime < cutoff_time:
                            size = os.path.getsize(filepath)
                            
                            if not dry_run:
                                os.remove(filepath)
                                logger.debug(f"Deleted old file: {filepath}")
                            
                            stats['files_deleted'] += 1
                            stats['size_freed_mb'] += size / (1024 * 1024)
                    
                    except Exception as e:
                        logger.error(f"Error processing {filepath}: {e}")
                        stats['errors'].append(str(e))
    
    stats['size_freed_mb'] = round(stats['size_freed_mb'], 2)
    
    if dry_run:
        logger.info(f"DRY RUN: Would delete {stats['files_deleted']} files ({stats['size_freed_mb']} MB)")
    else:
        logger.info(f"Deleted {stats['files_deleted']} old files ({stats['size_freed_mb']} MB)")
    
    return stats


def cleanup_empty_directories(site_id=None, product=None, dry_run=False):
    """
    Remove empty date directories in cache
    
    Args:
        site_id: Specific site (None = all sites)
        product: Specific product (None = all products)
        dry_run: If True, only report
        
    Returns:
        int: Number of directories removed
    """
    removed = 0
    
    if site_id:
        sites = [site_id.upper()]
    else:
        sites = [d for d in os.listdir(RADAR_BASE_PATH) 
                if os.path.isdir(os.path.join(RADAR_BASE_PATH, d))]
    
    for site in sites:
        site_path = os.path.join(RADAR_BASE_PATH, site)
        
        if product:
            products = [product.upper()]
        else:
            products = [d for d in os.listdir(site_path) 
                       if os.path.isdir(os.path.join(site_path, d))]
        
        for prod in products:
            geo_path = os.path.join(site_path, prod, 'geo')
            
            if not os.path.exists(geo_path):
                continue
            
            # Check date directories
            for date_dir in os.listdir(geo_path):
                date_path = os.path.join(geo_path, date_dir)
                
                if not os.path.isdir(date_path):
                    continue
                
                # Check if empty
                if not os.listdir(date_path):
                    if not dry_run:
                        try:
                            os.rmdir(date_path)
                            logger.debug(f"Removed empty directory: {date_path}")
                            removed += 1
                        except Exception as e:
                            logger.error(f"Error removing {date_path}: {e}")
                    else:
                        removed += 1
    
    if dry_run:
        logger.info(f"DRY RUN: Would remove {removed} empty directories")
    else:
        logger.info(f"Removed {removed} empty directories")
    
    return removed


def limit_cache_size(site_id, product, max_files=MAX_CACHE_FILES_PER_PRODUCT):
    """
    Limit cache size by keeping only the most recent N files
    
    Args:
        site_id: Radar site identifier
        product: Product code
        max_files: Maximum number of files to keep
        
    Returns:
        int: Number of files deleted
    """
    geo_path = os.path.join(RADAR_BASE_PATH, site_id.upper(), product.upper(), 'geo')
    
    if not os.path.exists(geo_path):
        return 0
    
    # Get all PNG files with timestamps
    png_files = []
    for png_file in glob.glob(os.path.join(geo_path, '**', '*.png'), recursive=True):
        mtime = os.path.getmtime(png_file)
        png_files.append((mtime, png_file))
    
    # Sort by modification time (newest first)
    png_files.sort(reverse=True)
    
    # Delete old files beyond max_files
    deleted = 0
    for _, filepath in png_files[max_files:]:
        try:
            # Delete PNG and JSON
            os.remove(filepath)
            json_file = filepath.replace('.png', '.json')
            if os.path.exists(json_file):
                os.remove(json_file)
            
            deleted += 1
            logger.debug(f"Deleted old cache file: {filepath}")
        
        except Exception as e:
            logger.error(f"Error deleting {filepath}: {e}")
    
    if deleted > 0:
        logger.info(f"Limited cache for {site_id}/{product}: deleted {deleted} old files")
    
    return deleted


def run_maintenance(max_age_hours=CACHE_RETENTION_HOURS, dry_run=False):
    """
    Run full cache maintenance
    
    Args:
        max_age_hours: Maximum age for cached files
        dry_run: If True, only report what would be done
        
    Returns:
        dict: Maintenance statistics
    """
    logger.info(f"Starting cache maintenance (max_age={max_age_hours}h, dry_run={dry_run})")
    
    # Get initial stats
    initial_stats = get_cache_stats()
    
    # Cleanup old files
    cleanup_stats = cleanup_old_files(max_age_hours=max_age_hours, dry_run=dry_run)
    
    # Remove empty directories
    empty_dirs = cleanup_empty_directories(dry_run=dry_run)
    
    # Get final stats
    final_stats = get_cache_stats()
    
    result = {
        'timestamp': datetime.utcnow().isoformat() + 'Z',
        'dry_run': dry_run,
        'initial': {
            'files': initial_stats['total_files'],
            'size_mb': initial_stats['total_size_mb']
        },
        'cleanup': cleanup_stats,
        'empty_dirs_removed': empty_dirs,
        'final': {
            'files': final_stats['total_files'],
            'size_mb': final_stats['total_size_mb']
        }
    }
    
    logger.info(f"Maintenance complete: {cleanup_stats['files_deleted']} files deleted, "
               f"{cleanup_stats['size_freed_mb']} MB freed")
    
    return result


if __name__ == '__main__':
    import sys
    import argparse
    
    parser = argparse.ArgumentParser(description='Radar cache management')
    parser.add_argument('--stats', action='store_true', help='Show cache statistics')
    parser.add_argument('--cleanup', action='store_true', help='Run cleanup')
    parser.add_argument('--site', help='Specific site (e.g., HGX)')
    parser.add_argument('--product', help='Specific product (e.g., N0Q)')
    parser.add_argument('--max-age', type=int, default=24, help='Maximum age in hours')
    parser.add_argument('--dry-run', action='store_true', help='Dry run (report only)')
    
    args = parser.parse_args()
    
    if args.stats:
        stats = get_cache_stats(site_id=args.site, product=args.product)
        print(f"\nCache Statistics:")
        print(f"  Total files: {stats['total_files']}")
        print(f"  Total size: {stats['total_size_mb']} MB")
        if stats['oldest_time']:
            print(f"  Oldest file: {stats['oldest_time']}")
        if stats['newest_time']:
            print(f"  Newest file: {stats['newest_time']}")
        
        if stats['by_site']:
            print(f"\nBy Site/Product:")
            for key, data in sorted(stats['by_site'].items()):
                print(f"  {key}: {data['files']} files, {data['size_mb']} MB")
    
    elif args.cleanup:
        result = run_maintenance(max_age_hours=args.max_age, dry_run=args.dry_run)
        print(f"\nMaintenance Results:")
        print(f"  Files deleted: {result['cleanup']['files_deleted']}")
        print(f"  Space freed: {result['cleanup']['size_freed_mb']} MB")
        print(f"  Empty dirs removed: {result['empty_dirs_removed']}")
    
    else:
        parser.print_help()

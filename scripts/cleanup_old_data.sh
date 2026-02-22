#!/bin/bash
#
# Aviation Weather Data Cleanup Script
# Removes old observations, model data, and generated products
#
# Usage: sudo ./cleanup_old_data.sh [--dry-run]
#
# Cron (run daily at 3 AM):
#   0 3 * * * /var/www/cap_winds_app/scripts/cleanup_old_data.sh >> /var/log/cleanup.log 2>&1
#

set -e

# Configuration
DB_NAME="avwx_data"
DRY_RUN=false

# Retention periods (days)
METAR_RETENTION=7        # Keep 7 days of METARs
TAF_RETENTION=7          # Keep 7 days of TAFs
HRRR_FILES_RETENTION=3   # Keep 3 days of HRRR GRIB files
GFS_FILES_RETENTION=3    # Keep 3 days of GFS GRIB files
MAPS_RETENTION=7         # Keep 7 days of generated maps
SHAPEFILES_RETENTION=7   # Keep 7 days of shapefiles

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 [--dry-run]"
            exit 1
            ;;
    esac
done

echo "========================================="
echo "Aviation Weather Data Cleanup"
echo "Started: $(date)"
if [ "$DRY_RUN" = true ]; then
    echo "MODE: DRY RUN (no changes will be made)"
fi
echo "========================================="
echo ""

# Function to execute or print command
exec_or_print() {
    if [ "$DRY_RUN" = true ]; then
        echo "[DRY RUN] $@"
    else
        eval "$@"
    fi
}

# =========================================
# 1. Clean PostGIS Database
# =========================================

echo "1. Cleaning PostGIS Database"
echo "----------------------------"

# Clean old METARs
echo "Removing METARs older than $METAR_RETENTION days..."
if [ "$DRY_RUN" = true ]; then
    COUNT=$(sudo -u postgres psql -d $DB_NAME -t -c "SELECT COUNT(*) FROM observations.metar WHERE observation_time < NOW() - INTERVAL '$METAR_RETENTION days';")
    echo "[DRY RUN] Would delete $COUNT METAR records"
else
    sudo -u postgres psql -d $DB_NAME -c "DELETE FROM observations.metar WHERE observation_time < NOW() - INTERVAL '$METAR_RETENTION days';"
    echo "✓ METARs cleaned"
fi

# Clean old TAFs
echo "Removing TAFs older than $TAF_RETENTION days..."
if [ "$DRY_RUN" = true ]; then
    COUNT=$(sudo -u postgres psql -d $DB_NAME -t -c "SELECT COUNT(*) FROM observations.taf WHERE issue_time < NOW() - INTERVAL '$TAF_RETENTION days';")
    echo "[DRY RUN] Would delete $COUNT TAF records"
else
    sudo -u postgres psql -d $DB_NAME -c "DELETE FROM observations.taf WHERE issue_time < NOW() - INTERVAL '$TAF_RETENTION days';"
    echo "✓ TAFs cleaned"
fi

# Vacuum database to reclaim space
echo "Vacuuming database..."
if [ "$DRY_RUN" = true ]; then
    echo "[DRY RUN] Would vacuum database"
else
    sudo -u postgres psql -d $DB_NAME -c "VACUUM ANALYZE;"
    echo "✓ Database vacuumed"
fi

echo ""

# =========================================
# 2. Clean HRRR Model Files
# =========================================

echo "2. Cleaning HRRR Model Files"
echo "----------------------------"

if [ -d "/LDM/models/hrrr" ]; then
    echo "Removing HRRR files older than $HRRR_FILES_RETENTION days..."
    
    if [ "$DRY_RUN" = true ]; then
        COUNT=$(find /LDM/models/hrrr -name "*.grib2" -mtime +$HRRR_FILES_RETENTION 2>/dev/null | wc -l)
        SIZE=$(find /LDM/models/hrrr -name "*.grib2" -mtime +$HRRR_FILES_RETENTION -exec du -ch {} + 2>/dev/null | tail -1 | cut -f1)
        echo "[DRY RUN] Would delete $COUNT HRRR files ($SIZE)"
        
        # Show directories that would be removed
        find /LDM/models/hrrr -type d -name "hrrr.*" -mtime +$HRRR_FILES_RETENTION 2>/dev/null | while read dir; do
            echo "[DRY RUN] Would remove directory: $dir"
        done
    else
        # Delete old GRIB files
        find /LDM/models/hrrr -name "*.grib2" -mtime +$HRRR_FILES_RETENTION -delete 2>/dev/null || true
        
        # Delete old .idx files
        find /LDM/models/hrrr -name "*.idx" -mtime +$HRRR_FILES_RETENTION -delete 2>/dev/null || true
        
        # Remove empty directories
        find /LDM/models/hrrr -type d -empty -delete 2>/dev/null || true
        
        echo "✓ HRRR files cleaned"
    fi
else
    echo "HRRR directory not found: /LDM/models/hrrr"
fi

echo ""

# =========================================
# 3. Clean GFS Model Files
# =========================================

echo "3. Cleaning GFS Model Files"
echo "----------------------------"

if [ -d "/LDM/models/gfs/0p25" ]; then
    echo "Removing GFS files older than $GFS_FILES_RETENTION days..."
    
    if [ "$DRY_RUN" = true ]; then
        COUNT=$(find /LDM/models/gfs/0p25 -name "*.grib2" -mtime +$GFS_FILES_RETENTION 2>/dev/null | wc -l)
        SIZE=$(find /LDM/models/gfs/0p25 -name "*.grib2" -mtime +$GFS_FILES_RETENTION -exec du -ch {} + 2>/dev/null | tail -1 | cut -f1)
        echo "[DRY RUN] Would delete $COUNT GFS files ($SIZE)"
        
        # Show directories that would be removed
        find /LDM/models/gfs/0p25 -type d -name "202*" -mtime +$GFS_FILES_RETENTION 2>/dev/null | while read dir; do
            echo "[DRY RUN] Would remove directory: $dir"
        done
    else
        # Delete old GRIB files
        find /LDM/models/gfs/0p25 -name "*.grib2" -mtime +$GFS_FILES_RETENTION -delete 2>/dev/null || true
        
        # Delete old .idx files
        find /LDM/models/gfs/0p25 -name "*.idx" -mtime +$GFS_FILES_RETENTION -delete 2>/dev/null || true
        
        # Remove empty directories
        find /LDM/models/gfs/0p25 -type d -empty -delete 2>/dev/null || true
        
        echo "✓ GFS files cleaned"
    fi
else
    echo "GFS directory not found: /LDM/models/gfs/0p25"
fi

echo ""

# =========================================
# 4. Clean Generated Maps
# =========================================

echo "4. Cleaning Generated Maps"
echo "----------------------------"

if [ -d "/var/www/html/cap_winds" ]; then
    echo "Removing maps older than $MAPS_RETENTION days..."
    
    if [ "$DRY_RUN" = true ]; then
        COUNT=$(find /var/www/html/cap_winds -name "*.png" -mtime +$MAPS_RETENTION 2>/dev/null | wc -l)
        SIZE=$(find /var/www/html/cap_winds -name "*.png" -mtime +$MAPS_RETENTION -exec du -ch {} + 2>/dev/null | tail -1 | cut -f1)
        echo "[DRY RUN] Would delete $COUNT map files ($SIZE)"
    else
        find /var/www/html/cap_winds -name "*.png" -mtime +$MAPS_RETENTION -delete 2>/dev/null || true
        echo "✓ Old maps cleaned"
    fi
else
    echo "Maps directory not found: /var/www/html/cap_winds"
fi

echo ""

# =========================================
# 5. Clean Generated Shapefiles
# =========================================

echo "5. Cleaning Generated Shapefiles"
echo "----------------------------"

if [ -d "/var/www/html/cap_winds_shp" ]; then
    echo "Removing shapefiles older than $SHAPEFILES_RETENTION days..."
    
    if [ "$DRY_RUN" = true ]; then
        COUNT=$(find /var/www/html/cap_winds_shp -name "*.zip" -mtime +$SHAPEFILES_RETENTION 2>/dev/null | wc -l)
        SIZE=$(find /var/www/html/cap_winds_shp -name "*.zip" -mtime +$SHAPEFILES_RETENTION -exec du -ch {} + 2>/dev/null | tail -1 | cut -f1)
        echo "[DRY RUN] Would delete $COUNT shapefile zips ($SIZE)"
    else
        # Delete old ZIP files
        find /var/www/html/cap_winds_shp -name "*.zip" -mtime +$SHAPEFILES_RETENTION -delete 2>/dev/null || true
        
        # Delete old loose shapefile components
        find /var/www/html/cap_winds_shp -name "*.shp" -mtime +$SHAPEFILES_RETENTION -delete 2>/dev/null || true
        find /var/www/html/cap_winds_shp -name "*.shx" -mtime +$SHAPEFILES_RETENTION -delete 2>/dev/null || true
        find /var/www/html/cap_winds_shp -name "*.dbf" -mtime +$SHAPEFILES_RETENTION -delete 2>/dev/null || true
        find /var/www/html/cap_winds_shp -name "*.prj" -mtime +$SHAPEFILES_RETENTION -delete 2>/dev/null || true
        find /var/www/html/cap_winds_shp -name "*.cpg" -mtime +$SHAPEFILES_RETENTION -delete 2>/dev/null || true
        
        echo "✓ Old shapefiles cleaned"
    fi
else
    echo "Shapefiles directory not found: /var/www/html/cap_winds_shp"
fi

echo ""

# =========================================
# 6. Disk Usage Summary
# =========================================

echo "6. Current Disk Usage"
echo "----------------------------"

if command -v df &> /dev/null; then
    echo "Filesystem usage:"
    df -h /LDM /var/www 2>/dev/null | grep -v "Filesystem" || echo "Could not get disk usage"
    echo ""
fi

if [ -d "/LDM/models" ]; then
    echo "Model data disk usage:"
    du -sh /LDM/models/hrrr /LDM/models/gfs 2>/dev/null || echo "Could not get model data usage"
    echo ""
fi

if [ -d "/var/www/html/cap_winds" ]; then
    echo "Generated products disk usage:"
    du -sh /var/www/html/cap_winds* 2>/dev/null || echo "Could not get products usage"
    echo ""
fi

# =========================================
# Summary
# =========================================

echo "========================================="
echo "Cleanup Complete"
echo "Finished: $(date)"
if [ "$DRY_RUN" = true ]; then
    echo ""
    echo "This was a DRY RUN. No changes were made."
    echo "Run without --dry-run to actually delete files."
fi
echo "========================================="


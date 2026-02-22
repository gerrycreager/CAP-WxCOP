#!/bin/bash
#
# Check GFS Data Availability
# Verifies GFS files are present for OCONUS wind forecast support
#

echo "========================================================================"
echo "GFS Data Availability Check"
echo "========================================================================"
echo ""

GFS_DIR="/LDM/models/gfs"

if [ ! -d "$GFS_DIR" ]; then
    echo "[ERROR] GFS directory not found: $GFS_DIR"
    echo ""
    echo "Expected location: /LDM/models/gfs"
    echo "Check LDM configuration for GFS data feed"
    exit 1
fi

echo "GFS Directory: $GFS_DIR"
echo ""

# Check for recent files
echo "Recent GFS files (last 24 hours):"
find "$GFS_DIR" -type f -name "*.grib2" -mtime -1 | sort | tail -10

echo ""
echo "File count by hour:"
for hour in 00 06 12 18; do
    count=$(find "$GFS_DIR" -type f -name "*_${hour}z_*" -mtime -1 | wc -l)
    echo "  ${hour}Z: $count files"
done

echo ""
echo "Total GFS files:"
find "$GFS_DIR" -type f -name "*.grib2" | wc -l

echo ""
echo "Disk usage:"
du -sh "$GFS_DIR"

echo ""
echo "Sample file structure:"
find "$GFS_DIR" -type f -name "*.grib2" | head -3

echo ""
echo "========================================================================"
echo "GFS Data Status"
echo "========================================================================"

recent_count=$(find "$GFS_DIR" -type f -name "*.grib2" -mtime -1 | wc -l)

if [ "$recent_count" -gt 0 ]; then
    echo "[OK] GFS data is available and current"
    echo ""
    echo "Next steps:"
    echo "1. Verify file naming pattern"
    echo "2. Check forecast hours available (need f000-f012)"
    echo "3. Update ingest_model_winds.py to use GFS for OCONUS"
    echo "4. Test with Puerto Rico airports"
else
    echo "[WARN] No recent GFS files found"
    echo "Check LDM is receiving GFS data feed"
fi

echo "========================================================================"


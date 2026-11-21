#!/bin/bash
# Mosaics every reprojected sectional chart (see warp_sectional_charts.sh)
# into one GDAL VRT -- a lightweight, re-runnable index, not a copy of
# the pixel data -- that MapServer reads as a single RASTER layer.
# Re-run any time warp_sectional_charts.sh produces new/updated files;
# rebuilding the VRT is cheap (seconds), unlike re-warping.
set -euo pipefail

WARPED_DIR=/var/www/cap_winds_app/data/sectionals/warped_3857
OUT_VRT=/var/www/cap_winds_app/data/sectionals/sectional_mosaic.vrt

shopt -s nullglob
files=("$WARPED_DIR"/*.tif)
if [ ${#files[@]} -eq 0 ]; then
    echo "no warped .tif files found in $WARPED_DIR -- run warp_sectional_charts.sh first" >&2
    exit 1
fi

echo "building VRT from ${#files[@]} charts..."
gdalbuildvrt -overwrite "$OUT_VRT" "${files[@]}"
echo "wrote $OUT_VRT"
chown www-data:www-data "$OUT_VRT"

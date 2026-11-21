#!/bin/bash
# Reprojects every fetched FAA sectional GeoTIFF (each in its own native
# Lambert Conformal Conic, per-chart parameters embedded in the file --
# see data/sectionals/README.md) into a common EPSG:3857, so they can be
# mosaicked into one VRT for MapServer/MapCache to serve as a single tile
# layer.
#
# -expand rgb converts the source's palette/indexed color to real RGB
# BEFORE warping -- without it, bilinear/cubic resampling blends palette
# INDEX NUMBERS across indexed pixels, which produces wrong colors at
# every edge (GDAL warns about exactly this). Confirmed necessary via a
# live test on the Great Falls chart, 2026-11-21.
#
# Parallelized (xargs -P) since this is pure local CPU work, not hitting
# FAA's servers -- unlike fetch_sectional_charts.py, which stays
# sequential out of courtesy to their server.
set -euo pipefail

LOCK_FILE=/var/www/cap_winds_app/data/sectionals/.warp.lock
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "another instance is already running (lock held on $LOCK_FILE) -- exiting" >&2
    exit 1
fi

RAW_BASE=/var/www/cap_winds_app/data/sectionals/raw
OUT_BASE=/var/www/cap_winds_app/data/sectionals/warped_3857
LOG=/var/www/cap_winds_app/logs/warp_sectional_charts.log
PARALLEL=3

# This host also serves the live production site (Apache/mod_wsgi) --
# a prior run of this script at -P6 with no scheduling hints drove load
# average past 35 and starved a concurrent Apache worker restart into
# timing out live requests. Re-nice ourselves (and everything we fork)
# to stay out of its way; CPU/IO still get used, just last in line.
renice -n 15 -p $$ > /dev/null
ionice -c2 -n7 -p $$ 2>/dev/null || true

mkdir -p "$OUT_BASE"
: > "$LOG"

warp_one() {
    dir="$1"
    name=$(basename "$dir")
    tif=$(find "$dir" -maxdepth 1 -iname "*.tif" | head -1)
    if [ -z "$tif" ]; then
        echo "SKIP $name -- no .tif found in $dir" >> "$LOG"
        return 1
    fi
    out="$OUT_BASE/${name}.tif"
    if [ -f "$out" ] && [ "$out" -nt "$tif" ]; then
        echo "SKIP $name -- already warped, newer than source" >> "$LOG"
        return 0
    fi
    rgb_tmp="${out}.rgb.tmp.tif"
    tmp="${out}.tmp.tif"
    # -expand rgb is a gdal_translate option (not gdalwarp) -- converts
    # palette/indexed pixels to real RGB first, so the warp's bilinear
    # resampling blends actual colors, not palette index numbers.
    if gdal_translate -expand rgb -co COMPRESS=LZW -q "$tif" "$rgb_tmp" 2>>"$LOG" && \
       gdalwarp -t_srs EPSG:3857 -r bilinear \
        -co COMPRESS=LZW -co TILED=YES -co BIGTIFF=IF_SAFER \
        -overwrite -q "$rgb_tmp" "$tmp" 2>>"$LOG"; then
        mv "$tmp" "$out"
        rm -f "$rgb_tmp"
        echo "OK   $name" >> "$LOG"
    else
        rm -f "$tmp" "$rgb_tmp"
        echo "FAIL $name" >> "$LOG"
        return 1
    fi
}
export -f warp_one
export OUT_BASE LOG

find "$RAW_BASE" -mindepth 1 -maxdepth 1 -type d | sort | \
    xargs -P "$PARALLEL" -I{} bash -c 'warp_one "$@"' _ {}

echo "=== warp pass done ===" >> "$LOG"
ok=$(grep -c '^OK' "$LOG" || true)
fail=$(grep -c '^FAIL' "$LOG" || true)
skip=$(grep -c '^SKIP' "$LOG" || true)
echo "ok=$ok fail=$fail skip=$skip" >> "$LOG"
echo "ok=$ok fail=$fail skip=$skip"

#!/bin/bash
# mrms_render_pipe.sh — LDM PIPE action handler for MRMS products
# Last modified: 2026-03-22
#
# Called by pqact PIPE action:
#   mrms_render_pipe.sh <product_key> <sector> <YYYYMMDD> <HHMMSS>
#
# pqact EXP pattern isolates date (\1) and time (\2) from product ID:
#   ProductName_HH.HH_([0-9]{8})-([0-9]{6})\.grib2\.gz$
#
# stdin: raw grib2.gz bytes from LDM product queue

set -euo pipefail

PRODUCT="${1:?Usage: mrms_render_pipe.sh <product_key> <sector> <YYYYMMDD> <HHMMSS>}"
SECTOR="${2:?Usage: mrms_render_pipe.sh <product_key> <sector> <YYYYMMDD> <HHMMSS>}"
DATE="${3:-}"     # YYYYMMDD from pqact \1
TIME="${4:-}"     # HHMMSS   from pqact \2

VENV_PYTHON=/var/www/cap_winds_app/venv/bin/python3
RENDERER=/home/ldm/bin/mrms_tile_renderer.py
LOG=/var/www/cap_winds_app/logs/mrms_renderer.log
TMPDIR_BASE=/tmp/mrms_render
LOCK_DIR=/tmp/mrms_locks

mkdir -p "$LOCK_DIR"
LOCK="${LOCK_DIR}/${PRODUCT}_${SECTOR}.lock"

if ! mkdir "$LOCK" 2>/dev/null; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) SKIP ${PRODUCT}/${SECTOR}: render already in progress" \
        >> "$LOG"
    cat > /dev/null
    exit 0
fi
trap 'rm -rf "$LOCK"' EXIT

mkdir -p "$TMPDIR_BASE"

# Build temp filename with timestamp so renderer can parse it
if [[ -n "$DATE" && -n "$TIME" ]]; then
    TMPFILE="${TMPDIR_BASE}/mrms_${PRODUCT}_${DATE}-${TIME}.grib2.gz"
else
    # Fallback: no timestamp available — use random name (timestamp parse will fail)
    TMPFILE=$(mktemp "${TMPDIR_BASE}/mrms_${PRODUCT}_XXXXXX.grib2.gz")
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) WARN ${PRODUCT}/${SECTOR}: no timestamp args, using random filename" \
        >> "$LOG"
fi

trap 'rm -f "$TMPFILE"; rm -rf "$LOCK"' EXIT

cat > "$TMPFILE"

if [ ! -s "$TMPFILE" ]; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ERROR ${PRODUCT}/${SECTOR}: empty stdin" \
        >> "$LOG"
    exit 1
fi

SIZE=$(stat -c%s "$TMPFILE")
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) START ${PRODUCT}/${SECTOR}: received ${SIZE} bytes" \
    >> "$LOG"

"$VENV_PYTHON" "$RENDERER" "$PRODUCT" "$SECTOR" "$TMPFILE"
STATUS=$?

if [ $STATUS -ne 0 ]; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ERROR ${PRODUCT}/${SECTOR}: renderer exited $STATUS" \
        >> "$LOG"
fi

exit $STATUS

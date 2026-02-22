#!/bin/bash
# PQACT Radar Processor Wrapper
# Called by PQACT via PIPE action
# Args: SITE PRODUCT HHMMSS

set -euo pipefail

# Arguments from PQACT
SITE="$1"
PRODUCT="$2"
TIMECODE="$3"  # HHMMSS format

# Log files in LDM directory (ldm user owns this)
LOG_FILE="/home/ldm/var/logs/radar_processor.log"
ERROR_LOG="/home/ldm/var/logs/radar_processor_errors.log"

# Build current date (YYYYMMDD)
CURRENT_DATE=$(date -u +%Y%m%d)

# Build NIDS file path
NIDS_FILE="/LDM/radar/level3/${SITE}/${PRODUCT}/nids/${CURRENT_DATE}/${SITE}_${PRODUCT}_${TIMECODE}.nids"

# Wait briefly for FILE action to complete writing
sleep 0.5

# Check if NIDS file exists
if [ ! -f "$NIDS_FILE" ]; then
    echo "$(date -u '+%Y-%m-%d %H:%M:%S') ERROR: NIDS file not found: $NIDS_FILE" >> "$ERROR_LOG"
    exit 1
fi

# Check if file is not empty
if [ ! -s "$NIDS_FILE" ]; then
    echo "$(date -u '+%Y-%m-%d %H:%M:%S') ERROR: NIDS file is empty: $NIDS_FILE" >> "$ERROR_LOG"
    exit 1
fi

# Log processing attempt
echo "$(date -u '+%Y-%m-%d %H:%M:%S') Processing: $SITE $PRODUCT $TIMECODE" >> "$LOG_FILE"

# Call Python processor
if /var/www/cap_winds_app/venv/bin/python3 /var/www/cap_winds_app/scripts/radar_processor.py "$SITE" "$PRODUCT" "$NIDS_FILE" >> "$LOG_FILE" 2>&1; then
    echo "$(date -u '+%Y-%m-%d %H:%M:%S') SUCCESS: $SITE $PRODUCT $TIMECODE" >> "$LOG_FILE"
else
    echo "$(date -u '+%Y-%m-%d %H:%M:%S') FAILED: $SITE $PRODUCT $TIMECODE - Python processor returned error" >> "$ERROR_LOG"
    exit 1
fi

exit 0


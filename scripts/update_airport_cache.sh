#!/bin/bash
#
# Update Airport Cache
# Downloads OurAirports database once daily
# Run via cron: 0 2 * * * /var/www/cap_winds_app/scripts/update_airport_cache.sh
#

CACHE_DIR="/var/www/cap_winds_app/.cache"
CACHE_FILE="${CACHE_DIR}/airports.csv"
TEMP_FILE="${CACHE_DIR}/airports.csv.tmp"
LOG_FILE="/var/log/airport_cache_update.log"

# Ensure cache directory exists
mkdir -p "$CACHE_DIR"

# Log with timestamp
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

log "Starting airport cache update..."

# Download to temp file
if curl -s -o "$TEMP_FILE" https://davidmegginson.github.io/ourairports-data/airports.csv; then
    # Verify file is not empty and looks like CSV
    if [ -s "$TEMP_FILE" ] && head -1 "$TEMP_FILE" | grep -q "ident"; then
        # Count airports
        AIRPORT_COUNT=$(wc -l < "$TEMP_FILE")
        FILE_SIZE=$(du -h "$TEMP_FILE" | cut -f1)
        
        # Replace old cache
        mv "$TEMP_FILE" "$CACHE_FILE"
        chown www-data:www-data "$CACHE_FILE"
        chmod 644 "$CACHE_FILE"
        
        log "✓ Airport cache updated successfully"
        log "  Airports: $AIRPORT_COUNT"
        log "  Size: $FILE_SIZE"
    else
        log "✗ Downloaded file appears invalid"
        rm -f "$TEMP_FILE"
        exit 1
    fi
else
    log "✗ Failed to download airport database"
    rm -f "$TEMP_FILE"
    exit 1
fi

log "Airport cache update complete"


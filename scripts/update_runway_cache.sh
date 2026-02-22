#!/bin/bash
#
# Update OurAirports Runway Database
# Downloads latest runway data for crosswind/runway analysis
#
# Run daily via cron:
# 0 3 * * * /var/www/cap_winds_app/scripts/update_runway_cache.sh

CACHE_DIR="/var/www/cap_winds_app/.cache"
RUNWAY_URL="https://davidmegginson.github.io/ourairports-data/runways.csv"
TEMP_FILE="/tmp/runways_download.csv"

# Create cache directory if it doesn't exist
mkdir -p "$CACHE_DIR"

# Download runway data
echo "Downloading runway data from OurAirports..."
if curl -s -o "$TEMP_FILE" "$RUNWAY_URL"; then
    # Verify file is valid CSV (has header)
    if head -n 1 "$TEMP_FILE" | grep -q "airport_ident"; then
        mv "$TEMP_FILE" "$CACHE_DIR/runways.csv"
        echo "Runway cache updated: $(wc -l < $CACHE_DIR/runways.csv) runways"
        ls -lh "$CACHE_DIR/runways.csv"
    else
        echo "ERROR: Downloaded file is not valid runway CSV"
        rm -f "$TEMP_FILE"
        exit 1
    fi
else
    echo "ERROR: Failed to download runway data"
    exit 1
fi

echo "Runway cache update complete"


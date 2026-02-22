# Find a recent NIDS file
cd /var/www/cap_winds_app
NIDS=$(find /LDM/radar/level3 -name "*_N0Q_*.nids" -mmin -30 | head -1)

if [ -n "$NIDS" ]; then
    echo "Testing with: $NIDS"
    
    # Extract site and product
    SITE=$(basename "$NIDS" | cut -d'_' -f1)
    PRODUCT=$(basename "$NIDS" | cut -d'_' -f2)
    
    echo "Site: $SITE, Product: $PRODUCT"
    
    # Run processor manually
    venv/bin/python scripts/radar_processor.py "$SITE" "$PRODUCT" "$NIDS"
    
    # Check if it created output
    GEO_DIR=$(dirname "$NIDS" | sed 's/nids/geo/')
    echo "Checking for output in: $GEO_DIR"
    ls -la "$GEO_DIR" | tail -5
fi

#!/bin/bash
#
# Diagnose TAF Issue for KMCO
#

echo "========================================================================"
echo "TAF System Diagnostic - KMCO"
echo "========================================================================"
echo ""

# Step 1: Check TAF file exists
echo "Step 1: Checking TAF file..."
TAF_FILE="/LDM/text/taf/2026/01/18/KWBC_TAF-20260118-1957.txt"

if [ -f "$TAF_FILE" ]; then
    echo "  [OK] TAF file exists"
    echo "  File: $TAF_FILE"
    echo "  Size: $(du -h $TAF_FILE | cut -f1)"
    echo ""
    echo "  First 20 lines:"
    head -20 "$TAF_FILE"
    echo ""
    
    # Check if KMCO is in the file
    if grep -q "KMCO" "$TAF_FILE"; then
        echo "  [OK] KMCO found in file"
        echo ""
        echo "  KMCO TAF:"
        grep -A 5 "TAF KMCO" "$TAF_FILE"
    else
        echo "  [WARN] KMCO not found in file"
    fi
else
    echo "  [ERROR] TAF file not found: $TAF_FILE"
fi

echo ""
echo "------------------------------------------------------------------------"

# Step 2: Check TAF directory structure
echo "Step 2: Checking TAF directory structure..."
TAF_DIR="/LDM/text/tav/2026/01/18"

if [ -d "$TAF_DIR" ]; then
    echo "  [OK] TAF directory exists"
    echo "  Directory: $TAF_DIR"
    echo "  File count: $(ls -1 $TAF_DIR | wc -l)"
    echo ""
    echo "  Recent TAF files:"
    ls -lht "$TAF_DIR" | head -10
else
    echo "  [ERROR] TAF directory not found"
fi

echo ""
echo "------------------------------------------------------------------------"

# Step 3: Check database for TAF table
echo "Step 3: Checking database for TAF table..."

psql -U avwx_user -d avwx_data -c "
SELECT table_name 
FROM information_schema.tables 
WHERE table_schema = 'observations' 
  AND table_name LIKE '%taf%';
" 2>&1 | head -20

echo ""
echo "------------------------------------------------------------------------"

# Step 4: Check if KMCO TAF exists in database
echo "Step 4: Checking database for KMCO TAF..."

psql -U avwx_user -d avwx_data -c "
SELECT 
    station_id, 
    issued_time, 
    LEFT(raw_text, 100) as taf_preview
FROM observations.taf 
WHERE station_id = 'KMCO' 
ORDER BY issued_time DESC 
LIMIT 5;
" 2>&1

echo ""
echo "------------------------------------------------------------------------"

# Step 5: Check weather station page code for TAF handling
echo "Step 5: Checking weather station page for TAF code..."

if grep -q "TAF\|taf" /var/www/cap_winds_app/templates/station.html; then
    echo "  [OK] TAF references found in station.html"
    echo ""
    echo "  TAF-related lines:"
    grep -n "TAF\|taf" /var/www/cap_winds_app/templates/station.html | head -10
else
    echo "  [WARN] No TAF references found in station.html"
fi

echo ""
echo "------------------------------------------------------------------------"

# Step 6: Check API endpoints
echo "Step 6: Checking API for TAF endpoint..."

if grep -q "def.*taf\|route.*taf" /var/www/cap_winds_app/app.py; then
    echo "  [OK] TAF endpoint found in app.py"
    echo ""
    echo "  TAF-related endpoints:"
    grep -n "def.*taf\|route.*taf" /var/www/cap_winds_app/app.py | head -10
else
    echo "  [WARN] No TAF endpoint found in app.py"
fi

echo ""
echo "========================================================================"
echo "Diagnostic Complete!"
echo "========================================================================"
echo ""
echo "Summary:"
echo "  - Check if TAF file exists and contains KMCO"
echo "  - Check if database has TAF table"
echo "  - Check if KMCO TAF is in database"
echo "  - Check if station page displays TAFs"
echo "  - Check if API endpoint exists for TAFs"
echo ""
echo "Next steps based on findings:"
echo "  1. If no TAF table: Create schema and ingest script"
echo "  2. If no KMCO in DB: Run TAF ingest"
echo "  3. If no API endpoint: Add TAF API route"
echo "  4. If no UI display: Add TAF section to station.html"
echo "========================================================================"


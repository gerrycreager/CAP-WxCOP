#!/bin/bash
#
# Fix Wing Nomenclature and Add Puerto Rico
# Changes "State" to "Wing" and adds PR/GU to wing codes
#

echo "========================================================================"
echo "Wing Nomenclature Fix - Adding PR/GU and Updating UI"
echo "========================================================================"
echo ""

cd /var/www/cap_winds_app

# Backup files
echo "Step 1: Creating backups..."
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
cp app.py app.py.backup_${TIMESTAMP}
cp templates/index.html templates/index.html.backup_${TIMESTAMP}
echo "  [OK] Backups created"
echo ""

# Fix app.py
echo "Step 2: Updating app.py..."

# Add PR and GU to state codes (insert before closing bracket)
# Find the line with 'WY' and add PR, GU before the closing bracket
sed -i "s/'WY'\s*\]/'WY',\n    'PR', 'GU'\n]/g" app.py

# Rename STATE_CODES to WING_CODES
sed -i 's/STATE_CODES/WING_CODES/g' app.py

echo "  [OK] Added PR and GU to wing codes"
echo "  [OK] Renamed STATE_CODES to WING_CODES"
echo ""

# Fix index.html
echo "Step 3: Updating index.html..."

cd templates

# Change "State" to "Wing" in dropdown option
sed -i 's/value="state"/value="wing"/g' index.html

# Change "State" text to "Wing" in option display
sed -i 's/>State</>Wing</g' index.html

# Change form field IDs and names
sed -i 's/state_code/wing_code/g' index.html
sed -i 's/state_group/wing_group/g' index.html

# Update JavaScript
sed -i "s/selectionType === 'state'/selectionType === 'wing'/g" index.html

echo "  [OK] Changed 'State' to 'Wing' in UI"
echo "  [OK] Updated form field IDs"
echo "  [OK] Updated JavaScript references"
echo ""

cd ..

# Verify changes
echo "Step 4: Verifying changes..."
echo ""

# Check app.py for PR and GU
if grep -q "'PR'" app.py && grep -q "'GU'" app.py; then
    echo "  [OK] PR and GU found in app.py"
else
    echo "  [WARN] PR/GU not found in app.py"
fi

# Check app.py for WING_CODES
if grep -q "WING_CODES" app.py; then
    echo "  [OK] WING_CODES found in app.py"
else
    echo "  [WARN] WING_CODES not found in app.py"
fi

# Check index.html for wing
if grep -q 'value="wing"' templates/index.html; then
    echo "  [OK] 'wing' value found in index.html"
else
    echo "  [WARN] 'wing' not found in index.html"
fi

echo ""

# Restart Apache
echo "Step 5: Restarting Apache..."
sudo systemctl restart apache2

if [ $? -eq 0 ]; then
    echo "  [OK] Apache restarted successfully"
else
    echo "  [ERROR] Apache restart failed"
    echo "  Check: sudo systemctl status apache2"
    exit 1
fi

echo ""
echo "========================================================================"
echo "SUCCESS - Wing Nomenclature Fix Complete!"
echo "========================================================================"
echo ""
echo "Changes made:"
echo "  - Added PR (Puerto Rico) and GU (Guam) to wing codes"
echo "  - Changed 'State' to 'Wing' throughout UI"
echo "  - Updated all form fields and JavaScript"
echo ""
echo "Test the changes:"
echo "  1. Go to: http://209.248.90.253/"
echo "  2. Selection Type dropdown should show 'Wing' (not 'State')"
echo "  3. Wing Code dropdown should include PR and GU"
echo ""
echo "Note: PR and GU will show 'No data' until GFS ingestion is implemented"
echo "========================================================================"


#!/bin/bash
#
# Fix states_service.py to Accept 'wing' Location Type
# This is in addition to fixing app.py
#

echo "========================================================================"
echo "Fixing states_service.py - Accept 'wing' Location Type"
echo "========================================================================"
echo ""

cd /var/www/cap_winds_app

# Backup
echo "Step 1: Creating backup..."
cp states_service.py states_service.py.backup_wing_$(date +%Y%m%d_%H%M%S)
echo "  [OK] Backup created"
echo ""

# Fix location_type checks in states_service.py
echo "Step 2: Updating location_type references..."

# Change 'state' to 'wing' in location_type checks
sed -i "s/location_type == 'state'/location_type == 'wing'/g" states_service.py
sed -i "s/location_type.*'state'/location_type == 'wing'/g" states_service.py

# Also handle any error messages or docstrings
sed -i "s/location_type: 'state'/location_type: 'wing'/g" states_service.py
sed -i "s/'state', 'region'/'wing', 'region'/g" states_service.py

echo "  [OK] Changed 'state' to 'wing' in location_type checks"
echo ""

# Verify changes
echo "Step 3: Verifying changes..."
if grep -q "location_type == 'wing'" states_service.py; then
    echo "  [OK] Found wing location_type handler"
else
    echo "  [WARN] wing location_type not found - may need manual fix"
fi
echo ""

# Restart Apache
echo "Step 4: Restarting Apache..."
sudo systemctl restart apache2

if [ $? -eq 0 ]; then
    echo "  [OK] Apache restarted"
else
    echo "  [ERROR] Apache restart failed"
    exit 1
fi

echo ""
echo "========================================================================"
echo "states_service.py Fix Complete!"
echo "========================================================================"
echo ""
echo "Next: Test wing selection"
echo "  Go to http://209.248.90.253/"
echo "  Select Wing -> CO"
echo "  Click Generate Map"
echo "  Should work now"
echo "========================================================================"

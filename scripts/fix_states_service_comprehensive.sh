#!/bin/bash
#
# Comprehensive Fix for states_service.py
# Fixes: 'wing' recognition, region bounds, and extent issues
#

echo "========================================================================"
echo "Comprehensive Fix - states_service.py"
echo "========================================================================"
echo ""

cd /var/www/cap_winds_app

# Backup
echo "Step 1: Creating backup..."
cp states_service.py states_service.py.backup_comprehensive_$(date +%Y%m%d_%H%M%S)
echo "  [OK] Backup created"
echo ""

# Fix 1: Change 'state' to 'wing' in _create_map_spec
echo "Step 2: Fixing 'state' -> 'wing' in location type handling..."
sed -i 's/elif location_type == "state":/elif location_type == "wing":/g' states_service.py
sed -i "s/location_type == 'state'/location_type == 'wing'/g" states_service.py
echo "  [OK] Changed location_type checks"
echo ""

# Fix 2: Update error messages and docstrings
echo "Step 3: Updating error messages..."
sed -i 's/Unknown state code/Unknown wing code/g' states_service.py
sed -i 's/state_info = self.config.STATE_BOUNDARIES/wing_info = self.config.STATE_BOUNDARIES/g' states_service.py
sed -i 's/location_name = state_info\["name"\]/location_name = wing_info["name"]/g' states_service.py
sed -i 's/bounds = state_info\["bounds"\]/bounds = wing_info["bounds"]/g' states_service.py
echo "  [OK] Updated variable names"
echo ""

# Fix 3: Check for region bounds calculation issues
echo "Step 4: Checking region bounds handling..."
if grep -q "elif location_type == \"region\"" states_service.py; then
    echo "  [OK] Region handling found"
    
    # Check if region bounds are calculated from states
    if grep -q "for.*in.*region_states\|calculate.*region.*bounds" states_service.py; then
        echo "  [OK] Region bounds calculation found"
    else
        echo "  [WARN] Region bounds may not be calculated correctly"
        echo "  [INFO] This could explain why RMR showed entire world"
    fi
else
    echo "  [WARN] No region handling found"
fi
echo ""

# Restart Apache
echo "Step 5: Restarting Apache..."
sudo systemctl restart apache2

if [ $? -eq 0 ]; then
    echo "  [OK] Apache restarted"
else
    echo "  [ERROR] Apache restart failed"
    exit 1
fi

echo ""
echo "========================================================================"
echo "Fix Applied!"
echo "========================================================================"
echo ""
echo "Changes made:"
echo "  1. Changed 'state' to 'wing' in location_type checks"
echo "  2. Updated variable names (state_info -> wing_info)"
echo "  3. Updated error messages"
echo ""
echo "Next steps:"
echo "  1. Test Colorado Wing (should work now)"
echo "  2. Test RMR Region (check if extent is correct)"
echo "  3. If RMR still shows entire world, we need to fix region bounds"
echo ""
echo "To diagnose region bounds issue, run:"
echo "  sed -n '/elif location_type == \"region\"/,/elif/p' /var/www/cap_winds_app/states_service.py"
echo "========================================================================"


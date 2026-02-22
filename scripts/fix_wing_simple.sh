#!/bin/bash
#
# Simple Fix - Just Change 'state' to 'wing' in states_service.py
#

echo "========================================================================" 
echo "Simple Fix: Changing 'state' to 'wing' in states_service.py"
echo "========================================================================"
echo ""

cd /var/www/cap_winds_app

# Backup
cp states_service.py states_service.py.backup_wing_simple_$(date +%Y%m%d_%H%M%S)
echo "[OK] Backup created"
echo ""

# Fix all instances of location_type == "state" to "wing"
echo "Fixing location_type checks..."
sed -i 's/location_type == "state"/location_type == "wing"/g' states_service.py
echo "[OK] location_type checks updated"
echo ""

# Fix variable names
echo "Fixing variable names..."
sed -i 's/state_info = self\.config\.STATE_BOUNDARIES\[location_code\]/wing_info = self.config.STATE_BOUNDARIES[location_code]/g' states_service.py
sed -i 's/location_name = state_info\["name"\]/location_name = wing_info["name"]/g' states_service.py
sed -i 's/bounds = state_info\["bounds"\]/bounds = wing_info["bounds"]/g' states_service.py
sed -i 's/is_conus = state_info\.get/is_conus = wing_info.get/g' states_service.py
echo "[OK] Variable names updated"
echo ""

# Fix error messages
echo "Fixing error messages..."
sed -i 's/Unknown state code/Unknown wing code/g' states_service.py
echo "[OK] Error messages updated"
echo ""

# Verify
echo "Verifying changes..."
if grep -q 'location_type == "wing"' states_service.py; then
    echo "[OK] 'wing' location_type found"
else
    echo "[WARN] 'wing' not found - check manually"
fi
echo ""

# Restart Apache
echo "Restarting Apache..."
sudo systemctl restart apache2
echo "[OK] Apache restarted"
echo ""

echo "========================================================================"
echo "Fix Complete! Test now:"
echo "  1. Wing: http://209.248.90.253/ -> Wing -> CO"
echo "  2. Region: http://209.248.90.253/ -> CAP Region -> RMR"
echo ""
echo "Check error log:"
echo "  tail -30 /var/log/apache2/cap_winds_error.log"
echo "========================================================================"


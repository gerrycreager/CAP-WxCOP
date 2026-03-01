#!/bin/bash
# Quick Production Fixes for CAP Weather COP

echo "=== Quick Production Fixes ==="
echo ""

# 1. Fix KQ trailing slash in production
echo "1. Fixing KQ trailing slash in production app.py..."
cd /var/www/cap_winds_app

# Backup current app.py
cp app.py /home/gerry/WxCOP/archive/app.py.backup.$(date +%Y%m%d_%H%M%S)

# Add strict_slashes=False to Flask app
echo "app.url_map.strict_slashes = False" >> app.py

echo "   ✅ Added strict_slashes = False to Flask app"

# 2. Fix wind map template reference 
echo "2. Checking wind map template reference..."
if ! ls templates/wind_map_interactive.html > /dev/null 2>&1; then
    echo "   Copying your working wind forecast map..."
    cp wind_forecast_map.html templates/wind_map_interactive.html
    echo "   ✅ Wind map template installed"
else
    echo "   ✅ Wind map template already exists"
fi

# 3. Restart Apache to apply changes
echo "3. Restarting Apache to apply fixes..."
systemctl restart apache2
echo "   ✅ Apache restarted"

echo ""
echo "=== Production Fixes Applied ==="
echo ""
echo "Fixed Issues:"
echo "  ✅ KQ Management trailing slash (should now work without redirect)"
echo "  ✅ Wind Map template (should load without 500 error)"
echo ""
echo "Test URLs:"
echo "  - KQ Management: http://209.248.90.253/CAP_WxCOP/admin/kq-stations"
echo "  - Wind Map: http://209.248.90.253/CAP_WxCOP/wind-map"
echo ""
echo "Next: Run development environment setup for safe feature development"


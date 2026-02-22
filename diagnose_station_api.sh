#!/bin/bash
# Diagnose CAP WxCOP API endpoints and fix station lookup

echo "=== CAP WxCOP API Endpoint Diagnosis ==="
echo

# Check current Flask routes in app.py
echo "1. Checking Flask routes in app.py..."
if [ -f "/var/www/cap_winds_app/app.py" ]; then
    grep -n "@app.route" /var/www/cap_winds_app/app.py | head -20
else
    echo "❌ app.py not found"
fi

echo
echo "2. Checking weather API routes..."
if [ -f "/var/www/cap_winds_app/weather_api.py" ]; then
    grep -n "@app.route\|def " /var/www/cap_winds_app/weather_api.py | head -20
else
    echo "❌ weather_api.py not found"  
fi

echo
echo "3. Testing API endpoints..."
BASE_URL="http://localhost/CAP_WxCOP"

# Test common API endpoints
endpoints=(
    "/api/weather/metar/recent"
    "/api/weather/station/KORD"  
    "/api/weather/health"
    "/weather/station/KORD"
    "/station/KORD"
    "/api/metar/KORD"
)

for endpoint in "${endpoints[@]}"; do
    echo -n "Testing $endpoint: "
    if curl -s -f "$BASE_URL$endpoint" > /dev/null 2>&1; then
        echo "✅ Working"
    else
        echo "❌ Failed"
    fi
done

echo
echo "4. Checking static files..."
STATIC_FILES=(
    "/var/www/cap_winds_app/templates/weather_station.html"
    "/var/www/cap_winds_app/static/js/weather_station.js" 
    "/var/www/cap_winds_app/templates/station_lookup.html"
)

for file in "${STATIC_FILES[@]}"; do
    if [ -f "$file" ]; then
        echo "✅ Found: $file"
    else
        echo "❌ Missing: $file"
    fi
done

echo
echo "5. Checking for JavaScript API calls..."
if [ -d "/var/www/cap_winds_app/static/js" ]; then
    echo "JavaScript files making API calls:"
    grep -r "fetch\|ajax\|XMLHttpRequest" /var/www/cap_winds_app/static/js/ 2>/dev/null | head -10
fi

if [ -d "/var/www/cap_winds_app/templates" ]; then
    echo "Template files with API calls:"  
    grep -r "fetch\|ajax\|api/" /var/www/cap_winds_app/templates/ 2>/dev/null | head -10
fi

echo
echo "6. Suggested fixes:"
echo "   - Check if /weather/station route exists in Flask app"
echo "   - Verify API endpoint URLs in JavaScript"
echo "   - Ensure weather_api.py is properly imported"
echo "   - Check Apache URL rewriting configuration"


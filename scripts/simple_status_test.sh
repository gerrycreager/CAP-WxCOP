#!/bin/bash
# CAP Weather COP - Simple Status Check Regression Test
# Tests HTTP status codes for all service endpoints

BASE_URL="http://209.248.90.253/CAP_WxCOP"
PASSED=0
FAILED=0

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

echo "=== CAP Weather COP Simple Regression Test ==="
echo "Testing HTTP status codes for all services..."
echo ""

test_url() {
    local url="$1"
    local description="$2"
    
    echo -n "Testing $description... "
    
    status=$(curl -s -o /dev/null -w "%{http_code}" "$url" 2>/dev/null || echo "ERROR")
    
    if [ "$status" = "200" ]; then
        echo -e "${GREEN}PASS${NC} ($status)"
        ((PASSED++))
    else
        echo -e "${RED}FAIL${NC} ($status)"
        ((FAILED++))
    fi
}

# Test all service pages
test_url "$BASE_URL/" "Landing Page"
test_url "$BASE_URL/weather-map" "Weather Map"
test_url "$BASE_URL/wind-map" "Wind Forecast Map"  
test_url "$BASE_URL/weather/station" "Station Lookup"
test_url "$BASE_URL/radar/animation" "NEXRAD Radar"
test_url "$BASE_URL/incident-archive" "Incident Archive"
test_url "$BASE_URL/admin/kq-stations" "KQ Station Management"
test_url "$BASE_URL/manual-taf" "Manual TAF Entry"

echo ""
echo "=== API Health Checks ==="

# Test key APIs
test_url "$BASE_URL/api/weather/health" "Weather API Health"
test_url "$BASE_URL/api/status" "System Status API"
test_url "$BASE_URL/health" "General Health Check"
test_url "$BASE_URL/api/weather/station/KORD" "Weather API - Station Detail"

echo ""
echo "=== Legacy Route Compatibility ==="

test_url "$BASE_URL/enhanced-weather-map" "Legacy Enhanced Weather Map"
test_url "$BASE_URL/enhanced_weather_map.html" "Legacy Enhanced Weather Map HTML"
test_url "$BASE_URL/weather_map.html" "Legacy Weather Map HTML"

echo ""
echo "=== Results Summary ==="
echo -e "PASSED: ${GREEN}$PASSED${NC}"
echo -e "FAILED: ${RED}$FAILED${NC}"
echo "Total: $((PASSED + FAILED))"

if [ $FAILED -eq 0 ]; then
    echo -e "\n${GREEN}✅ ALL TESTS PASSED - System operational${NC}"
    exit 0
else
    echo -e "\n${RED}❌ $FAILED TESTS FAILED - Review needed${NC}"
    exit 1
fi


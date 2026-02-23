#!/bin/bash
# CAP Weather COP - Service Regression Testing (Fixed Content Matching)
# Tests all service cards and API endpoints for production readiness

set -e

BASE_URL="http://209.248.90.253/CAP_WxCOP"
DATE=$(date '+%Y%m%d_%H%M%S')
LOG_FILE="/tmp/cap_wxcop_regression_${DATE}.log"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m' 
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Test results tracking
PASSED=0
FAILED=0
WARNINGS=0

echo "=== CAP Weather COP Regression Testing ===" | tee -a $LOG_FILE
echo "Started: $(date)" | tee -a $LOG_FILE
echo "Base URL: $BASE_URL" | tee -a $LOG_FILE
echo "" | tee -a $LOG_FILE

# Function to test HTTP endpoint
test_endpoint() {
    local url="$1"
    local expected_status="$2"
    local description="$3"
    local content_check="$4"
    
    echo -n "Testing $description... " | tee -a $LOG_FILE
    
    # Make request and capture response
    response=$(curl -s -w "\n%{http_code}" "$url" 2>/dev/null || echo -e "\nERROR")
    status_code=$(echo "$response" | tail -1)
    content=$(echo "$response" | head -n -1)
    
    if [ "$status_code" = "ERROR" ]; then
        echo -e "${RED}ERROR${NC} - Connection failed" | tee -a $LOG_FILE
        ((FAILED++))
        return 1
    fi
    
    if [ "$status_code" -eq "$expected_status" ]; then
        if [ -n "$content_check" ]; then
            if echo "$content" | grep -qi "$content_check"; then
                echo -e "${GREEN}PASS${NC} ($status_code)" | tee -a $LOG_FILE
                ((PASSED++))
                return 0
            else
                echo -e "${YELLOW}WARNING${NC} ($status_code) - Missing expected content pattern" | tee -a $LOG_FILE
                echo "    Expected pattern: $content_check" >> $LOG_FILE
                echo "    Content preview: $(echo "$content" | head -1 | cut -c1-80)..." >> $LOG_FILE
                ((WARNINGS++))
                return 2
            fi
        else
            echo -e "${GREEN}PASS${NC} ($status_code)" | tee -a $LOG_FILE
            ((PASSED++))
            return 0
        fi
    else
        echo -e "${RED}FAIL${NC} - Expected $expected_status, got $status_code" | tee -a $LOG_FILE
        ((FAILED++))
        return 1
    fi
}

# Function to test JSON API endpoint
test_api_endpoint() {
    local url="$1"
    local description="$2"
    local required_field="$3"
    
    echo -n "Testing $description... " | tee -a $LOG_FILE
    
    response=$(curl -s "$url" 2>/dev/null || echo "ERROR")
    
    if [ "$response" = "ERROR" ]; then
        echo -e "${RED}ERROR${NC} - Connection failed" | tee -a $LOG_FILE
        ((FAILED++))
        return 1
    fi
    
    # Check if response is valid JSON
    if ! echo "$response" | python3 -m json.tool >/dev/null 2>&1; then
        echo -e "${RED}FAIL${NC} - Invalid JSON response" | tee -a $LOG_FILE
        ((FAILED++))
        return 1
    fi
    
    # Check for required field if specified
    if [ -n "$required_field" ]; then
        if ! echo "$response" | python3 -c "import json,sys; data=json.load(sys.stdin); exit(0 if '$required_field' in data else 1)" 2>/dev/null; then
            echo -e "${YELLOW}WARNING${NC} - Missing required field: $required_field" | tee -a $LOG_FILE
            ((WARNINGS++))
            return 2
        fi
    fi
    
    echo -e "${GREEN}PASS${NC}" | tee -a $LOG_FILE
    ((PASSED++))
    return 0
}

echo "=== TESTING SERVICE PAGES ===" | tee -a $LOG_FILE
echo "" | tee -a $LOG_FILE

# Test 1: Landing Page - More flexible content matching
test_endpoint "$BASE_URL/" 200 "Landing Page" "Weather.*Map\|Wind.*Forecast\|NEXRAD\|Station.*Lookup"

# Test 2: Weather Map
test_endpoint "$BASE_URL/weather-map" 200 "Weather Map" "weather\|METAR\|station"

# Test 3: Wind Forecast Interactive Map  
test_endpoint "$BASE_URL/wind-map" 200 "Wind Forecast Map" "wind\|forecast\|CAPR"

# Test 4: Station Lookup
test_endpoint "$BASE_URL/weather/station" 200 "Station Lookup" "Weather.*Station\|METAR\|station"

# Test 5: NEXRAD Radar Animation
test_endpoint "$BASE_URL/radar/animation" 200 "NEXRAD Radar Animation" "radar\|NEXRAD\|animation"

# Test 6: Incident Archive
test_endpoint "$BASE_URL/incident-archive" 200 "Incident Archive" "incident\|archive\|weather"

# Test 7: KQ Station Management  
test_endpoint "$BASE_URL/admin/kq-stations" 200 "KQ Station Management" "KQ\|station\|management"

# Test 8: Manual TAF Entry
test_endpoint "$BASE_URL/manual-taf" 200 "Manual TAF Entry" "TAF\|manual"

echo "" | tee -a $LOG_FILE
echo "=== TESTING API ENDPOINTS ===" | tee -a $LOG_FILE
echo "" | tee -a $LOG_FILE

# Test 9: Weather API Health Check
test_api_endpoint "$BASE_URL/api/weather/health" "Weather API Health" "status"

# Test 10: System Status API
test_api_endpoint "$BASE_URL/api/status" "System Status API" "status"

# Test 11: Health Check
test_api_endpoint "$BASE_URL/health" "General Health Check" "status"

# Test 12: Weather API - METAR Recent (with bounds)
test_api_endpoint "$BASE_URL/api/weather/metar/recent?bounds=-88,41,-87,42&limit=5" "Weather API - Recent METAR" "metars"

# Test 13: Weather API - Station Detail (KORD)
test_api_endpoint "$BASE_URL/api/weather/station/KORD" "Weather API - Station Detail" "station"

# Test 14: Weather API - Stations List
test_api_endpoint "$BASE_URL/api/weather/stations?bounds=-88,41,-87,42" "Weather API - Stations List" "stations"

echo "" | tee -a $LOG_FILE
echo "=== TESTING BASIC CONNECTIVITY ===" | tee -a $LOG_FILE  
echo "" | tee -a $LOG_FILE

# Test 15: Static CSS Files
test_endpoint "$BASE_URL/static/css/enhanced_weather_map.css" 200 "Static CSS" ""

# Test 16: Regions API
test_api_endpoint "$BASE_URL/api/regions" "Regions API" ""

echo "" | tee -a $LOG_FILE
echo "=== TESTING LEGACY ROUTES ===" | tee -a $LOG_FILE
echo "" | tee -a $LOG_FILE

# Test 17: Legacy Enhanced Weather Map Route
test_endpoint "$BASE_URL/enhanced-weather-map" 200 "Legacy Enhanced Weather Map" ""

# Test 18: Legacy Weather Map HTML Route  
test_endpoint "$BASE_URL/weather_map.html" 200 "Legacy Weather Map HTML" ""

echo "" | tee -a $LOG_FILE
echo "=== TEST SUMMARY ===" | tee -a $LOG_FILE
echo "Completed: $(date)" | tee -a $LOG_FILE
echo -e "Results: ${GREEN}$PASSED PASSED${NC}, ${RED}$FAILED FAILED${NC}, ${YELLOW}$WARNINGS WARNINGS${NC}" | tee -a $LOG_FILE
echo "Log file: $LOG_FILE" | tee -a $LOG_FILE

TOTAL=$((PASSED + FAILED + WARNINGS))
echo "Total tests: $TOTAL" | tee -a $LOG_FILE

if [ $FAILED -eq 0 ]; then
    if [ $WARNINGS -eq 0 ]; then
        echo -e "\n${GREEN}✅ ALL TESTS PASSED - SYSTEM READY FOR PRODUCTION${NC}" | tee -a $LOG_FILE
        exit 0
    else
        echo -e "\n${YELLOW}⚠️  TESTS PASSED WITH WARNINGS - REVIEW RECOMMENDED${NC}" | tee -a $LOG_FILE
        exit 1
    fi
else
    echo -e "\n${RED}❌ TESTS FAILED - SYSTEM NOT READY FOR PRODUCTION${NC}" | tee -a $LOG_FILE
    exit 2
fi

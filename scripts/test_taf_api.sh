#!/bin/bash
#
# TAF API Test Script
# Tests TAF endpoints with various stations
#

echo "========================================================================"
echo "TAF API Test"
echo "========================================================================"
echo ""

# Get base URL
read -p "Enter your server URL (default: http://localhost): " BASE_URL
BASE_URL=${BASE_URL:-http://localhost}

echo ""
echo "Testing TAF API at: $BASE_URL"
echo ""

# Test stations
TEST_STATIONS=(
    "KMCO:Orlando"
    "KATL:Atlanta"
    "KORD:Chicago"
    "KJFK:New York"
    "KLAX:Los Angeles"
    "CYYZ:Toronto (Canadian)"
    "EGLL:London (International)"
)

echo "========================================================================"
echo "Test 1: Single TAF Endpoint"
echo "========================================================================"
echo ""

for station_info in "${TEST_STATIONS[@]}"; do
    IFS=':' read -r station name <<< "$station_info"
    
    echo "Testing: $station ($name)"
    echo "------------------------------------------------------------------------"
    
    response=$(curl -s -w "\n%{http_code}" "$BASE_URL/api/taf/$station")
    http_code=$(echo "$response" | tail -n1)
    body=$(echo "$response" | sed '$d')
    
    if [ "$http_code" -eq 200 ]; then
        echo "✅ SUCCESS (HTTP $http_code)"
        
        # Parse JSON (basic)
        issue_time=$(echo "$body" | grep -o '"issue_time":"[^"]*"' | cut -d'"' -f4)
        age_minutes=$(echo "$body" | grep -o '"age_minutes":[0-9]*' | cut -d':' -f2)
        raw_preview=$(echo "$body" | grep -o '"raw_text":"[^"]*"' | cut -d'"' -f4 | head -c 60)
        
        echo "  Issue Time: $issue_time"
        echo "  Age: ${age_minutes} minutes"
        echo "  Preview: ${raw_preview}..."
        
    elif [ "$http_code" -eq 404 ]; then
        echo "⚠️  NO TAF FOUND (HTTP $http_code)"
        echo "  Station $station has no TAF in database"
        
    else
        echo "❌ ERROR (HTTP $http_code)"
        echo "  Response: $body"
    fi
    
    echo ""
done

echo ""
echo "========================================================================"
echo "Test 2: All TAFs Endpoint (24 hours)"
echo "========================================================================"
echo ""

# Test with KMCO
echo "Testing: KMCO /all endpoint"
echo "------------------------------------------------------------------------"

response=$(curl -s -w "\n%{http_code}" "$BASE_URL/api/taf/KMCO/all")
http_code=$(echo "$response" | tail -n1)
body=$(echo "$response" | sed '$d')

if [ "$http_code" -eq 200 ]; then
    echo "✅ SUCCESS (HTTP $http_code)"
    
    count=$(echo "$body" | grep -o '"count":[0-9]*' | cut -d':' -f2)
    echo "  TAF Count (24h): $count"
    
    if [ "$count" -gt 0 ]; then
        echo "  ✅ Multiple TAFs available"
    else
        echo "  ⚠️  No TAFs in last 24 hours"
    fi
else
    echo "❌ ERROR (HTTP $http_code)"
    echo "  Response: $body"
fi

echo ""
echo "========================================================================"
echo "Test 3: Invalid Station"
echo "========================================================================"
echo ""

echo "Testing: XXXX (invalid)"
echo "------------------------------------------------------------------------"

response=$(curl -s -w "\n%{http_code}" "$BASE_URL/api/taf/XXXX")
http_code=$(echo "$response" | tail -n1)
body=$(echo "$response" | sed '$d')

if [ "$http_code" -eq 404 ]; then
    echo "✅ CORRECT - Returns 404 for invalid station"
else
    echo "⚠️  Expected 404, got HTTP $http_code"
fi

echo ""
echo "========================================================================"
echo "Test 4: Invalid Station ID Format"
echo "========================================================================"
echo ""

echo "Testing: ABC (too short)"
echo "------------------------------------------------------------------------"

response=$(curl -s -w "\n%{http_code}" "$BASE_URL/api/taf/ABC")
http_code=$(echo "$response" | tail -n1)

if [ "$http_code" -eq 400 ]; then
    echo "✅ CORRECT - Returns 400 for invalid format"
else
    echo "⚠️  Expected 400, got HTTP $http_code"
fi

echo ""
echo "========================================================================"
echo "Test Summary"
echo "========================================================================"
echo ""

# Check database
echo "Checking database TAF counts..."
echo ""

TAF_TOTAL=$(psql -U avwx_user -d avwx_data -t -c "SELECT COUNT(*) FROM observations.taf;" 2>/dev/null | xargs)
TAF_RECENT=$(psql -U avwx_user -d avwx_data -t -c "SELECT COUNT(*) FROM observations.taf WHERE issue_time > NOW() - INTERVAL '6 hours';" 2>/dev/null | xargs)

if [ -n "$TAF_TOTAL" ]; then
    echo "Database Stats:"
    echo "  Total TAFs: $TAF_TOTAL"
    echo "  Recent TAFs (6h): $TAF_RECENT"
    echo ""
    
    if [ "$TAF_TOTAL" -gt 0 ]; then
        echo "✅ Database has TAFs"
    else
        echo "❌ Database is empty - run ingest_taf.py"
    fi
else
    echo "⚠️  Could not check database (requires psql access)"
fi

echo ""
echo "Recommendations:"
echo "------------------------------------------------------------------------"

if [ "$TAF_TOTAL" -eq 0 ]; then
    echo "1. Run TAF ingest: cd /var/www/cap_winds_app/scripts && ./ingest_taf.py"
fi

echo "2. If APIs work, proceed to UI deployment:"
echo "   - Add endpoints to app.py"
echo "   - Add TAF component to station template"
echo "   - Test in browser"
echo ""

echo "========================================================================"
echo "Test Complete"
echo "========================================================================"

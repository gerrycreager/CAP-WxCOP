#!/bin/bash
# Diagnostic: Check which identifier fields we should use

CACHE="/var/www/cap_winds_app/.cache/airports.csv"

echo "=== Checking Identifier Field Usage in OurAirports ==="
echo ""

echo "1. Major ICAO airports (K-prefix):"
echo "   Format: ident,icao_code,gps_code,local_code"
grep "Phoenix Sky Harbor" "$CACHE" | cut -d, -f2,13,15,16
grep "Denver International" "$CACHE" | cut -d, -f2,13,15,16
grep "Dallas.*Love Field" "$CACHE" | cut -d, -f2,13,15,16

echo ""
echo "2. Small airports with FAA codes:"
grep "^23322," "$CACHE" | cut -d, -f2,13,15,16  # Yucca Airstrip
grep "Sedona Airport" "$CACHE" | cut -d, -f2,13,15,16

echo ""
echo "3. Check for airports where gps_code != ident:"
echo "   (These might be missing if we only use ident)"
awk -F',' 'NR>1 && $3=="small_airport" && $9=="US" && $15!="" && $2!=$15 {print $2","$15","$4}' "$CACHE" | head -10

echo ""
echo "4. Count airports by identifier field availability:"
echo "   US small_airports with ident only:"
awk -F',' '$3=="small_airport" && $9=="US" && $2!="" && $13=="" && $15==""' "$CACHE" | wc -l

echo "   US small_airports with icao_code:"
awk -F',' '$3=="small_airport" && $9=="US" && $13!=""' "$CACHE" | wc -l

echo "   US small_airports with gps_code:"
awk -F',' '$3=="small_airport" && $9=="US" && $15!=""' "$CACHE" | wc -l

echo ""
echo "5. What's in our database?"
psql -U avwx_user -d avwx_data << EOF
SELECT 
    COUNT(*) as total,
    COUNT(*) FILTER (WHERE station_id LIKE 'K%') as k_prefix,
    COUNT(*) FILTER (WHERE station_id ~ '^[A-Z]{4}$') as four_letters,
    COUNT(*) FILTER (WHERE station_id ~ '^[0-9]') as starts_with_number
FROM observations.airports;
EOF


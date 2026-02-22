#!/bin/bash
#
# Debug: Check what's in actual TAF files
#

echo "========================================================================"
echo "TAF File Content Analysis"
echo "========================================================================"
echo ""

# Get a recent TAF file
TAF_FILE=$(ls -t /LDM/text/taf/2026/01/18/*.txt | head -1)

echo "Sample file: $TAF_FILE"
echo ""

echo "First 100 lines:"
echo "------------------------------------------------------------------------"
head -100 "$TAF_FILE"

echo ""
echo "------------------------------------------------------------------------"
echo "Looking for TAF headers:"
echo "------------------------------------------------------------------------"
grep -n "^TAF" "$TAF_FILE" | head -20

echo ""
echo "------------------------------------------------------------------------"
echo "Looking for station IDs with timestamps:"
echo "------------------------------------------------------------------------"
grep -n "^[A-Z][A-Z][A-Z][A-Z]\s\+[0-9][0-9][0-9][0-9][0-9][0-9]Z" "$TAF_FILE" | head -20

echo ""
echo "------------------------------------------------------------------------"
echo "Count of potential TAF lines:"
echo "------------------------------------------------------------------------"
echo "Lines starting with 'TAF': $(grep -c "^TAF" "$TAF_FILE")"
echo "Lines starting with K***: $(grep -c "^K[A-Z][A-Z][A-Z]" "$TAF_FILE")"
echo "Lines starting with P***: $(grep -c "^P[A-Z][A-Z][A-Z]" "$TAF_FILE")"
echo "Lines with station + timestamp: $(grep -c "[A-Z][A-Z][A-Z][A-Z]\s\+[0-9][0-9][0-9][0-9][0-9][0-9]Z" "$TAF_FILE")"

echo ""
echo "========================================================================"


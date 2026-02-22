#!/var/www/cap_winds_app/venv/bin/python3
"""
Quick Test - Parse Single TAF File
Tests the updated parser with KMCO file
"""

import sys
sys.path.insert(0, '/var/www/cap_winds_app/scripts')

# Import the updated parser functions
from ingest_taf import parse_taf_file

# Test with the known file
test_file = '/LDM/text/taf/2026/01/18/KWBC_TAF-20260118-1957.txt'

print("=" * 70)
print(f"Testing TAF Parser with: {test_file}")
print("=" * 70)
print()

tafs = parse_taf_file(test_file)

print(f"Total TAFs parsed: {len(tafs)}")
print()

# Check if KMCO is in there
kmco_found = False
for taf in tafs:
    if taf['station_id'] == 'KMCO':
        kmco_found = True
        print("✓ KMCO TAF found!")
        print()
        print("Station ID:", taf['station_id'])
        print("Issue Time:", taf['issue_time'])
        print("Valid From:", taf['valid_from'])
        print("Valid To:", taf['valid_to'])
        print()
        print("Raw TAF:")
        print(taf['raw_text'])
        print()

if not kmco_found:
    print("✗ KMCO TAF NOT found")
    print()
    print("Stations found:")
    for taf in tafs:
        print(f"  - {taf['station_id']}")

print()
print("=" * 70)
print("All stations parsed:")
print("=" * 70)
stations = sorted(set(t['station_id'] for t in tafs))
print(', '.join(stations))
print()
print(f"Total unique stations: {len(stations)}")
print("=" * 70)

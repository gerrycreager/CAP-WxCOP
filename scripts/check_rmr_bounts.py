#!/var/www/cap_winds_app/venv/bin/python3
"""
Quick Check: Do all RMR states exist with valid bounds?
"""

import sys
sys.path.insert(0, '/var/www/cap_winds_app')

from states_service import Config

config = Config()

print("=" * 70)
print("RMR (Rocky Mountain Region) Bounds Check")
print("=" * 70)
print()

# Get RMR states
rmr_states = config.CAP_REGIONS['RMR']['states']
print(f"RMR States: {rmr_states}")
print()

# Check each state
all_valid = True
state_bounds_list = []

for state in rmr_states:
    if state in config.STATE_BOUNDARIES:
        bounds = config.STATE_BOUNDARIES[state]['bounds']
        name = config.STATE_BOUNDARIES[state]['name']
        print(f"✓ {state} ({name}): {bounds}")
        state_bounds_list.append(bounds)
    else:
        print(f"✗ {state}: NOT FOUND in STATE_BOUNDARIES")
        all_valid = False

print()

if all_valid:
    print("All RMR states found! Calculating combined bounds...")
    print()
    
    west = min(b[0] for b in state_bounds_list)
    east = max(b[1] for b in state_bounds_list)
    south = min(b[2] for b in state_bounds_list)
    north = max(b[3] for b in state_bounds_list)
    
    print(f"Calculated RMR Bounds:")
    print(f"  West:  {west:7.2f}°  (westmost edge)")
    print(f"  East:  {east:7.2f}°  (eastmost edge)")
    print(f"  South: {south:6.2f}°  (southmost edge)")
    print(f"  North: {north:6.2f}°  (northmost edge)")
    print()
    
    # Sanity checks
    if west >= east:
        print("❌ ERROR: West >= East (invalid!)")
    elif south >= north:
        print("❌ ERROR: South >= North (invalid!)")
    elif west < -120 or east > -100:
        print("⚠️  WARNING: Bounds seem off for Rocky Mountain region")
    else:
        print("✓ Bounds look valid for western US")
        print()
        print("Expected map should show:")
        print("  - Small area in western US")
        print("  - Just the 5 Rocky Mountain states")
        print("  - NOT the entire world!")
else:
    print("❌ Some RMR states are missing from STATE_BOUNDARIES")
    print("   This would cause the bounds calculation to fail")

print()
print("=" * 70)


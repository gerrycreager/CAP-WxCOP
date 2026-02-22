#!/usr/bin/python3
"""
Quick diagnostic - what's in states_service.py?
Run this to see what we can import from your existing file
"""

import sys
sys.path.insert(0, '/var/www/cap_winds_app')

print("=" * 70)
print("Checking states_service.py...")
print("=" * 70)

try:
    import states_service
    print("✓ Successfully imported states_service module")
    print()
    
    # List all classes and functions
    print("Available classes and functions:")
    for name in dir(states_service):
        if not name.startswith('_'):
            obj = getattr(states_service, name)
            obj_type = type(obj).__name__
            print(f"  - {name} ({obj_type})")
    
    print()
    print("=" * 70)
    print("Looking for main service class...")
    print("=" * 70)
    
    # Try common names
    for name in ['StatesService', 'StateService', 'WindService', 'AnalysisService', 
                 'MapService', 'generate_analysis', 'create_map']:
        if hasattr(states_service, name):
            print(f"✓ Found: {name}")
            obj = getattr(states_service, name)
            print(f"  Type: {type(obj)}")
            if hasattr(obj, '__doc__'):
                doc = (obj.__doc__ or '').strip()[:100]
                if doc:
                    print(f"  Doc: {doc}...")
    
except ImportError as e:
    print(f"✗ Failed to import: {e}")
    print()
    print("File location: /var/www/cap_winds_app/states_service.py")
    print("Check if file exists and has valid Python syntax")

except Exception as e:
    print(f"✗ Error: {e}")

print("=" * 70)

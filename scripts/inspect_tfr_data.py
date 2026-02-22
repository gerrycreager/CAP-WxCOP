#!/usr/bin/env python3
"""
TFR Data Structure Inspector
Diagnoses the actual field names and structure from ESRI/FAA API
"""

import requests
import json
import sys

def inspect_esri_tfr_data():
    """Inspect the actual ESRI TFR data structure"""
    
    # ESRI REST API endpoint
    url = 'https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services/National_Defense_Airspace_TFR_Areas/FeatureServer/0/query'
    
    params = {
        'where': '1=1',
        'outFields': '*',
        'f': 'json',
        'returnGeometry': 'true',
        'resultRecordCount': 5  # Just get a few records for inspection
    }
    
    try:
        print("🔍 Inspecting ESRI/FAA TFR data structure...")
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        
        data = response.json()
        
        print(f"✅ Response received")
        print(f"📊 Total features available: {data.get('count', 'unknown')}")
        
        features = data.get('features', [])
        print(f"📥 Downloaded {len(features)} sample features")
        
        if features:
            # Inspect first feature structure
            first_feature = features[0]
            print("\n🏗️  FEATURE STRUCTURE:")
            print(f"Feature keys: {list(first_feature.keys())}")
            
            # Inspect properties/attributes
            properties = first_feature.get('attributes', {})
            print(f"\n📋 AVAILABLE FIELDS ({len(properties)} total):")
            for field, value in properties.items():
                value_type = type(value).__name__
                value_preview = str(value)[:50] + "..." if len(str(value)) > 50 else str(value)
                print(f"  {field:<20} ({value_type:<10}): {value_preview}")
            
            # Inspect geometry
            geometry = first_feature.get('geometry', {})
            if geometry:
                print(f"\n🗺️  GEOMETRY:")
                print(f"  Type: {geometry.get('type', 'unknown')}")
                print(f"  Keys: {list(geometry.keys())}")
            
            # Show raw JSON for first feature (truncated)
            print(f"\n📄 SAMPLE RAW FEATURE (first 500 chars):")
            raw_json = json.dumps(first_feature, indent=2)[:500]
            print(raw_json + "...")
            
        else:
            print("❌ No features found")
            
        # Check service info
        info_url = 'https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services/National_Defense_Airspace_TFR_Areas/FeatureServer/0?f=json'
        info_response = requests.get(info_url, timeout=30)
        if info_response.status_code == 200:
            info_data = info_response.json()
            fields = info_data.get('fields', [])
            print(f"\n📚 SERVICE FIELD DEFINITIONS ({len(fields)} fields):")
            for field in fields:
                name = field.get('name', 'unknown')
                field_type = field.get('type', 'unknown')
                alias = field.get('alias', name)
                print(f"  {name:<25} ({field_type:<15}) - {alias}")
        
    except Exception as e:
        print(f"❌ Error inspecting TFR data: {e}")
        return False
    
    return True

if __name__ == '__main__':
    success = inspect_esri_tfr_data()
    sys.exit(0 if success else 1)


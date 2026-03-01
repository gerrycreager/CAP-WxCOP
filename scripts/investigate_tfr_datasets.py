#!/usr/bin/env python3
"""
Investigate Stadium TFR Dataset Structure
Check for actual TFR data vs just National Defense Airspace
"""

import requests
import json

def investigate_tfr_datasets():
    """Check multiple potential TFR datasets"""
    
    datasets = {
        'Stadium TFRs': {
            'service_url': 'https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services/Stadiums/FeatureServer/0',
            'dataset_id': '67af16061c014365ae9218c489a321be_0'
        },
        'National Defense Airspace': {
            'service_url': 'https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services/National_Defense_Airspace_TFR_Areas/FeatureServer/0',
            'dataset_id': 'national-defense-airspace-tfr-areas'
        }
    }
    
    for dataset_name, config in datasets.items():
        print(f"\n{'='*60}")
        print(f"🔍 INVESTIGATING: {dataset_name}")
        print(f"{'='*60}")
        
        try:
            # Get service info
            info_url = f"{config['service_url']}?f=json"
            info_response = requests.get(info_url, timeout=30)
            
            if info_response.status_code == 200:
                info_data = info_response.json()
                print(f"📋 Service Name: {info_data.get('name', 'Unknown')}")
                print(f"📝 Description: {info_data.get('description', 'No description')}")
                print(f"🔢 Max Record Count: {info_data.get('maxRecordCount', 'Unknown')}")
                
                # Show fields
                fields = info_data.get('fields', [])
                print(f"\n📚 FIELDS ({len(fields)} total):")
                for field in fields[:10]:  # Show first 10 fields
                    name = field.get('name', 'unknown')
                    field_type = field.get('type', 'unknown')
                    alias = field.get('alias', name)
                    print(f"  {name:<25} ({field_type:<20}) - {alias}")
                
                if len(fields) > 10:
                    print(f"  ... and {len(fields) - 10} more fields")
            
            # Get sample data
            query_url = f"{config['service_url']}/query"
            params = {
                'where': '1=1',
                'outFields': '*',
                'f': 'json',
                'returnGeometry': 'true',
                'resultRecordCount': 3
            }
            
            query_response = requests.get(query_url, params=params, timeout=30)
            
            if query_response.status_code == 200:
                query_data = query_response.json()
                features = query_data.get('features', [])
                
                print(f"\n📊 SAMPLE DATA:")
                print(f"Total available features: {query_data.get('count', 'unknown')}")
                print(f"Downloaded samples: {len(features)}")
                
                if features:
                    # Show first feature attributes
                    first_attrs = features[0].get('attributes', {})
                    print(f"\n📄 SAMPLE FEATURE ATTRIBUTES:")
                    for key, value in list(first_attrs.items())[:8]:
                        value_str = str(value)[:50] + "..." if len(str(value)) > 50 else str(value)
                        print(f"  {key:<25}: {value_str}")
                    
                    # Check geometry
                    geometry = features[0].get('geometry', {})
                    if geometry:
                        geom_type = geometry.get('type', 'unknown')
                        print(f"\n🗺️  GEOMETRY TYPE: {geom_type}")
                        print(f"Geometry keys: {list(geometry.keys())}")
                        
                        # Show geometry structure for ESRI format
                        if 'rings' in geometry:
                            print("Format: ESRI Polygon (rings)")
                        elif 'paths' in geometry:
                            print("Format: ESRI Polyline (paths)")
                        elif 'points' in geometry:
                            print("Format: ESRI Multipoint")
                        elif 'x' in geometry and 'y' in geometry:
                            print("Format: ESRI Point")
                else:
                    print("❌ No features found")
                    
        except Exception as e:
            print(f"❌ Error investigating {dataset_name}: {e}")
    
    # Also try to find other TFR-related services
    print(f"\n{'='*60}")
    print("🔍 SEARCHING FOR OTHER TFR SERVICES...")
    print(f"{'='*60}")
    
    # Check the main AIS portal for TFR services
    try:
        portal_url = "https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services?f=json"
        portal_response = requests.get(portal_url, timeout=30)
        
        if portal_response.status_code == 200:
            portal_data = portal_response.json()
            services = portal_data.get('services', [])
            
            tfr_services = [s for s in services if 'tfr' in s.get('name', '').lower() or 'restriction' in s.get('name', '').lower()]
            
            print(f"📡 FOUND {len(tfr_services)} POTENTIAL TFR SERVICES:")
            for service in tfr_services:
                print(f"  - {service.get('name', 'Unknown')}: {service.get('type', 'Unknown')}")
                
        else:
            print("❌ Could not access services directory")
            
    except Exception as e:
        print(f"❌ Error searching services: {e}")

if __name__ == '__main__':
    investigate_tfr_datasets()


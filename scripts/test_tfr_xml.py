#!/var/www/cap_winds_app/venv/bin/python3
import base64
import xml.etree.ElementTree as ET

# Use the base64 data you provided earlier
base64_data = "PD94bWwgdmVyc2lvbj0iMS4wIiBlbmNvZGluZz0iVVRGLTgiPz4KPFRGUkxpc3Q+CiAgPFRGUj4KICAgIDxEYXRlPjAyLzE5LzIwMjY8L0RhdGU+CiAgICA8Tk9UQU1JRD42LzcyNTU8L05PVEFNSUQ+CiAgICA8RmFjaWxpdHk+WktDPC9GYWNpbGl0eT4KICAgIDxTdGF0ZT5NTzwvU3RhdGU+CiAgICA8VHlwZT5IQVpBUkRTPC9UeXBlPgogICAgPERlc2NyaXB0aW9uPjEwTk0gTkUgT0YgV0FZTkVTVklMTEUsIE1PLCBGcmlkYXksIEZlYnJ1YXJ5IDIwLCAyMDI2IHRocm91Z2ggU2F0dXJkYXksIEZlYnJ1YXJ5IDIxLCAyMDI2IFVUQzwvRGVzY3JpcHRpb24+CiAgICA8Tm90YW1EZXRhaWw+aHR0cHM6Ly90ZnIuZmFhLmdvdi90ZnIzLz9wYWdlPWRldGFpbF82XzcyNTUuaHRtbDwvTm90YW1EZXRhaWw+CiAgPC9URlI+"

try:
    decoded = base64.b64decode(base64_data)
    xml_content = decoded.decode('utf-8')
    print("Decoded XML length:", len(xml_content))
    
    root = ET.fromstring(xml_content)
    tfr_count = len(root.findall('TFR'))
    print(f"Found {tfr_count} TFRs in XML")
    
    # Show first few TFRs
    for i, tfr in enumerate(root.findall('TFR')[:3]):
        notam = tfr.find('NOTAMID').text if tfr.find('NOTAMID') is not None else 'N/A'
        desc = tfr.find('Description').text if tfr.find('Description') is not None else 'N/A'
        print(f"TFR {i+1}: {notam} - {desc[:50]}...")
        
except Exception as e:
    print(f"Error: {e}")

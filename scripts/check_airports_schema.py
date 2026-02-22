#!/usr/bin/env python3
"""
Quick schema check for observations.airports table
"""
import sys
sys.path.insert(0, '/var/www/cap_winds_app')
from db_config import get_connection

try:
    conn = get_connection()
    cur = conn.cursor()
    
    # Get column names for observations.airports
    cur.execute("""
        SELECT column_name, data_type 
        FROM information_schema.columns 
        WHERE table_schema = 'observations' 
          AND table_name = 'airports'
        ORDER BY ordinal_position
    """)
    
    columns = cur.fetchall()
    print("observations.airports columns:")
    for col, dtype in columns:
        print(f"  {col} ({dtype})")
    
    # Sample some data to see the structure
    cur.execute("SELECT * FROM observations.airports LIMIT 3")
    sample = cur.fetchall()
    print(f"\nFirst 3 rows:")
    for i, row in enumerate(sample):
        print(f"  Row {i+1}: {row}")
    
    cur.close()
    conn.close()
    
except Exception as e:
    print(f"Error: {e}")


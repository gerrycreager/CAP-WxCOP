#!/usr/bin/env python3
"""
populate_wing_map.py — Populate observations.wing_map from CAPR 30-1 Table 2.

Region/Wing structure is authoritative per CAPR 30-1 with ICL 24-07.
52 Wings: 50 states + DC (National Capital Wing) + Puerto Rico (includes USVI).
GUWG (Guam) forthcoming, subordinate to PCR — included as pending.

Run once from r815:
  python3 populate_wing_map.py
"""

import psycopg2
import sys

DB_DSN = 'dbname=avwx_data user=avwx_user host=192.168.0.60'

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS observations.wing_map (
    iso_region  VARCHAR(10) NOT NULL PRIMARY KEY,
    wing_id     VARCHAR(6)  NOT NULL,
    region_code VARCHAR(5)  NOT NULL
);
COMMENT ON TABLE observations.wing_map IS
    'CAP Region/Wing assignments per CAPR 30-1 Table 2 with ICL 24-07';
"""

# Authoritative per CAPR 30-1 Table 2
# iso_region -> (wing_id, region_code)
WING_MAP = {
    # Northeast Region (NER)
    'US-CT': ('CTWG', 'NER'),
    'US-ME': ('MEWG', 'NER'),
    'US-MA': ('MAWG', 'NER'),
    'US-NH': ('NHWG', 'NER'),
    'US-NJ': ('NJWG', 'NER'),
    'US-NY': ('NYWG', 'NER'),
    'US-RI': ('RIWG', 'NER'),
    'US-VT': ('VTWG', 'NER'),

    # Middle Atlantic Region (MAR)
    'US-DE': ('DEWG', 'MAR'),
    'US-MD': ('MDWG', 'MAR'),
    'US-NC': ('NCWG', 'MAR'),
    'US-PA': ('PAWG', 'MAR'),
    'US-SC': ('SCWG', 'MAR'),
    'US-VA': ('VAWG', 'MAR'),
    'US-WV': ('WVWG', 'MAR'),
    'US-DC': ('DCWG', 'MAR'),   # National Capital Wing

    # Great Lakes Region (GLR)
    'US-IL': ('ILWG', 'GLR'),
    'US-IN': ('INWG', 'GLR'),
    'US-KY': ('KYWG', 'GLR'),
    'US-MI': ('MIWG', 'GLR'),
    'US-MN': ('MNWG', 'GLR'),
    'US-OH': ('OHWG', 'GLR'),
    'US-WI': ('WIWG', 'GLR'),

    # Southeast Region (SER)
    'US-AL': ('ALWG', 'SER'),
    'US-FL': ('FLWG', 'SER'),
    'US-GA': ('GAWG', 'SER'),
    'US-MS': ('MSWG', 'SER'),
    'US-PR': ('PRWG', 'SER'),   # Puerto Rico Wing (includes USVI)
    'US-TN': ('TNWG', 'SER'),

    # North Central Region (NCR)
    'US-IA': ('IAWG', 'NCR'),
    'US-KS': ('KSWG', 'NCR'),
    'US-MO': ('MOWG', 'NCR'),
    'US-NE': ('NEWG', 'NCR'),
    'US-ND': ('NDWG', 'NCR'),
    'US-SD': ('SDWG', 'NCR'),

    # Southwest Region (SWR)
    'US-AR': ('ARWG', 'SWR'),
    'US-LA': ('LAWG', 'SWR'),
    'US-NM': ('NMWG', 'SWR'),
    'US-OK': ('OKWG', 'SWR'),
    'US-TX': ('TXWG', 'SWR'),

    # Rocky Mountain Region (RMR)
    'US-CO': ('COWG', 'RMR'),
    'US-ID': ('IDWG', 'RMR'),
    'US-MT': ('MTWG', 'RMR'),
    'US-NV': ('NVWG', 'RMR'),
    'US-UT': ('UTWG', 'RMR'),
    'US-WY': ('WYWG', 'RMR'),

    # Pacific Region (PCR)
    'US-AK': ('AKWG', 'PCR'),
    'US-AZ': ('AZWG', 'PCR'),
    'US-CA': ('CAWG', 'PCR'),
    'US-HI': ('HIWG', 'PCR'),
    'US-OR': ('ORWG', 'PCR'),
    'US-WA': ('WAWG', 'PCR'),
    # GU-GU: GUWG forthcoming, subordinate to PCR per ICL 24-07
}

def main():
    conn = psycopg2.connect(DB_DSN)
    conn.autocommit = False
    cur = conn.cursor()

    # Create table
    cur.execute(CREATE_SQL)
    print("Table observations.wing_map created/verified")

    # Upsert all rows
    inserted = updated = 0
    for iso_region, (wing_id, region_code) in sorted(WING_MAP.items()):
        cur.execute("""
            INSERT INTO observations.wing_map (iso_region, wing_id, region_code)
            VALUES (%s, %s, %s)
            ON CONFLICT (iso_region) DO UPDATE SET
                wing_id     = EXCLUDED.wing_id,
                region_code = EXCLUDED.region_code
        """, (iso_region, wing_id, region_code))
        if cur.rowcount:
            inserted += 1

    conn.commit()
    print(f"Inserted/updated {inserted} wing map entries")

    # Verify
    cur.execute("""
        SELECT region_code, COUNT(*) as wings
        FROM observations.wing_map
        GROUP BY region_code
        ORDER BY region_code
    """)
    print("\nRegion summary:")
    for row in cur.fetchall():
        print(f"  {row[0]}: {row[1]} wings")

    cur.execute("SELECT COUNT(*) FROM observations.wing_map")
    print(f"\nTotal: {cur.fetchone()[0]} entries")

    # Show airports with wing mapping as sanity check
    cur.execute("""
        SELECT wm.region_code, wm.wing_id, COUNT(a.id) as airports
        FROM observations.wing_map wm
        LEFT JOIN observations.airports a ON a.iso_region = wm.iso_region
            AND a.station_id ~ '^[KTP]'
            AND a.has_paved_runway = true
            AND a.longest_runway_ft >= 2500
        GROUP BY wm.region_code, wm.wing_id
        ORDER BY wm.region_code, wm.wing_id
    """)
    print("\nAirport counts by wing (qualifying K/T/P airports):")
    cur_region = None
    for row in cur.fetchall():
        if row[0] != cur_region:
            print(f"  {row[0]}:")
            cur_region = row[0]
        print(f"    {row[1]}: {row[2]} airports")

    cur.close()
    conn.close()
    print("\nDone.")

if __name__ == '__main__':
    main()

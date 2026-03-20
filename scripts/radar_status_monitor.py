#!/home/ldm/venv/bin/python3
"""
NEXRAD Radar Status Monitor
Decodes FTM (Free Text Message) products for radar operational status
Writes to PostgreSQL/PostGIS on data2 (192.168.0.60)

Location: /home/ldm/bin/radar_status_monitor.py

Tables created in radar schema of avwx_data:
  radar.radar_status   - current status per site (upsert)
  radar.status_history - full history of status changes
"""

import sys
import re
from datetime import datetime
import psycopg2
from psycopg2.extras import RealDictCursor

# ---------------------------------------------------------------------------
# Database connection
# ---------------------------------------------------------------------------
DB_HOST = "192.168.0.60"
DB_PORT = 5432
DB_NAME = "avwx_data"
DB_USER = "avwx_user"
# Trust auth on LAN - no password needed
DB_DSN = f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} user={DB_USER}"

# VCP (Volume Coverage Pattern) descriptions
VCP_MODES = {
    11:  "Clear Air (14 min)",
    12:  "Clear Air (14 min)",
    21:  "Precipitation (6 min)",
    31:  "Precipitation (5 min)",
    32:  "Precipitation (5 min)",
    35:  "Precipitation (Supplemental)",
    80:  "Precipitation (SAILS)",
    90:  "Precipitation (MESO-SAILS)",
    121: "Clear Air (Fast)",
    211: "Precipitation (Fast)",
    212: "Precipitation (SAILS Fast)",
    215: "Precipitation (MRLE)",
}


def get_conn():
    """Get a database connection."""
    return psycopg2.connect(DB_DSN)


def init_database():
    """Create radar status tables in radar schema if they don't exist."""
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS radar.radar_status (
            site_id         TEXT        PRIMARY KEY,
            status          TEXT        NOT NULL,
            vcp_mode        INTEGER,
            vcp_description TEXT,
            operation_mode  TEXT,
            last_update     TIMESTAMPTZ,
            message         TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS radar.status_history (
            id              SERIAL      PRIMARY KEY,
            site_id         TEXT        NOT NULL,
            status          TEXT        NOT NULL,
            vcp_mode        INTEGER,
            operation_mode  TEXT,
            ts              TIMESTAMPTZ DEFAULT NOW(),
            message         TEXT
        )
    """)

    # Index for fast site lookups in history
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_status_history_site_id
        ON radar.status_history (site_id, ts DESC)
    """)

    conn.commit()
    cur.close()
    conn.close()


def parse_ftm_message(ftm_text):
    """Parse FTM (Free Text Message) for radar status.
    Returns dict with status, vcp_mode, operation_mode, message.
    """
    status_info = {
        'status':         'UNKNOWN',
        'vcp_mode':       None,
        'operation_mode': None,
        'message':        ftm_text.strip()[:500]
    }

    # Extract VCP mode
    vcp_match = re.search(r'VCP\s*(\d+)', ftm_text, re.IGNORECASE)
    if vcp_match:
        vcp_num = int(vcp_match.group(1))
        status_info['vcp_mode'] = vcp_num
        status_info['status']   = 'OPERATIONAL'

    # Failure indicators take highest priority
    failure_keywords = ['FAIL', 'DOWN', 'OFFLINE', 'INOPERATIVE', 'OUT OF SERVICE']
    for keyword in failure_keywords:
        if keyword in ftm_text.upper():
            status_info['status']         = 'FAILED'
            status_info['operation_mode'] = 'FAILED'
            break

    # Maintenance
    maintenance_keywords = ['MAINTENANCE', 'MAINT', 'SCHEDULED']
    for keyword in maintenance_keywords:
        if keyword in ftm_text.upper():
            status_info['status']         = 'MAINTENANCE'
            status_info['operation_mode'] = 'MAINTENANCE'
            break

    # Test mode
    if 'TEST' in ftm_text.upper() and status_info['status'] == 'UNKNOWN':
        status_info['operation_mode'] = 'TEST'
        status_info['status']         = 'TEST'

    # Operational resumption
    operational_keywords = ['RESUME', 'OPERATIONAL', 'NORMAL', 'OPERATING']
    for keyword in operational_keywords:
        if keyword in ftm_text.upper() and \
                status_info['status'] not in ['FAILED', 'MAINTENANCE']:
            status_info['status']         = 'OPERATIONAL'
            status_info['operation_mode'] = 'OPERATE'

    return status_info


def update_radar_status(site_id, status_info, timestamp):
    """Upsert current status and append history row."""
    vcp_desc = VCP_MODES.get(status_info['vcp_mode'], 'Unknown') \
               if status_info['vcp_mode'] else None

    conn = get_conn()
    cur  = conn.cursor()

    # Upsert current status (PostgreSQL ON CONFLICT replaces INSERT OR REPLACE)
    cur.execute("""
        INSERT INTO radar.radar_status
            (site_id, status, vcp_mode, vcp_description, operation_mode,
             last_update, message)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (site_id) DO UPDATE SET
            status          = EXCLUDED.status,
            vcp_mode        = EXCLUDED.vcp_mode,
            vcp_description = EXCLUDED.vcp_description,
            operation_mode  = EXCLUDED.operation_mode,
            last_update     = EXCLUDED.last_update,
            message         = EXCLUDED.message
    """, (
        site_id,
        status_info['status'],
        status_info['vcp_mode'],
        vcp_desc,
        status_info['operation_mode'],
        timestamp,
        status_info['message'],
    ))

    # Append history row
    cur.execute("""
        INSERT INTO radar.status_history
            (site_id, status, vcp_mode, operation_mode, ts, message)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (
        site_id,
        status_info['status'],
        status_info['vcp_mode'],
        status_info['operation_mode'],
        timestamp,
        status_info['message'],
    ))

    conn.commit()
    cur.close()
    conn.close()
    return True


def process_ftm_file(ftm_file, site_id, timestamp):
    """Read FTM file, parse it, and write status to PostgreSQL."""
    try:
        if ftm_file == '/dev/null':
            content = f"TEST initialization for {site_id}"
        else:
            with open(ftm_file, 'r', encoding='latin-1', errors='ignore') as f:
                content = f.read()

        status_info = parse_ftm_message(content)

        # Convert DDHHMM timestamp to datetime
        try:
            dt = datetime.strptime(timestamp, '%d%H%M')
            dt = dt.replace(year=datetime.now().year,
                            month=datetime.now().month)
        except Exception:
            dt = datetime.now()

        success = update_radar_status(site_id, status_info, dt)

        if success:
            sym = '✓' if status_info['status'] == 'OPERATIONAL' else '✗'
            print(f"{sym} {site_id}: {status_info['status']}", file=sys.stderr)
            if status_info['vcp_mode']:
                vcp_desc = VCP_MODES.get(status_info['vcp_mode'], 'Unknown')
                print(f"   VCP {status_info['vcp_mode']}: {vcp_desc}",
                      file=sys.stderr)

        return True

    except Exception as e:
        print(f"✗ Error processing {ftm_file}: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return False


def get_site_status(site_id):
    """Get current status for a site (called by web API)."""
    conn = get_conn()
    cur  = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT site_id, status, vcp_mode, vcp_description,
               operation_mode, last_update
        FROM radar.radar_status
        WHERE site_id = %s
    """, (site_id,))

    row = cur.fetchone()
    cur.close()
    conn.close()

    if row:
        return {
            'site_id':        row['site_id'],
            'status':         row['status'],
            'vcp_mode':       row['vcp_mode'],
            'vcp_description':row['vcp_description'],
            'operation_mode': row['operation_mode'],
            'last_update':    row['last_update'].isoformat() if row['last_update'] else None,
            'operational':    row['status'] == 'OPERATIONAL',
        }
    return None


def get_all_status():
    """Get current status for all sites (bulk API call)."""
    conn = get_conn()
    cur  = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT site_id, status, vcp_mode, vcp_description,
               operation_mode, last_update
        FROM radar.radar_status
        ORDER BY site_id
    """)

    rows = cur.fetchall()
    cur.close()
    conn.close()

    return [
        {
            'site_id':        r['site_id'],
            'status':         r['status'],
            'vcp_mode':       r['vcp_mode'],
            'vcp_description':r['vcp_description'],
            'operation_mode': r['operation_mode'],
            'last_update':    r['last_update'].isoformat() if r['last_update'] else None,
            'operational':    r['status'] == 'OPERATIONAL',
        }
        for r in rows
    ]


def main():
    """Main entry point - called by pqact PIPE/EXEC action."""
    if len(sys.argv) < 3:
        print("Usage: radar_status_monitor.py <ftm_file> <site_id> [<timestamp>]",
              file=sys.stderr)
        sys.exit(1)

    ftm_file  = sys.argv[1]
    site_id   = sys.argv[2].upper()
    timestamp = sys.argv[3] if len(sys.argv) > 3 \
                else datetime.now().strftime('%d%H%M')

    # Ensure tables exist (fast no-op if already created)
    init_database()

    success = process_ftm_file(ftm_file, site_id, timestamp)
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()

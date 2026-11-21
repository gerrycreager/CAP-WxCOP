#!/var/www/cap_winds_app/venv/bin/python3
"""
ingest_sounding.py — Decode WMO TEMP (TTAA/TTBB/UUAA/UUBB) upper-air
sounding bulletins into observations.sounding.

Scope: surface through 100mb only (TTAA mandatory levels + TTBB significant
levels) -- TTCC/TTDD (100mb and above) are explicitly out of scope, per
Gerry's direction: CAP aircraft never operate anywhere near those altitudes.
UUAA/UUBB are the wind-only PIBAL equivalent (used when the temperature
sensor fails but winds are still reported) -- same decoder, temp/dewpoint
just come back None.

Real-time PIPE ingest, matching scripts/ingest_wwa_vtec.py's pattern (not
ingest_taf.py's cron+file-batch pattern) -- IDS|DDPLUS runs independently on
both r815 and data1 (confirmed 2026-09-19: NOT a relay pair like
SATELLITE/NGRID), so both hosts' pqact will receive and process the same
bulletin independently. ON CONFLICT upsert absorbs that safely; a file-based
approach on shared NFS would not.

Called by pqact PIPE action (stdin = raw bulletin text):
    IDS|DDPLUS  <pattern -- confirmed against real captured headers first>
        PIPE  -close  venv/bin/python3 scripts/ingest_sounding.py

Usage:
    ingest_sounding.py              # read bulletin from stdin (pqact mode)
    ingest_sounding.py --file PATH  # read from a file (backfill/testing)
    ingest_sounding.py --dry-run    # decode and print, don't write to DB

WMO code reference: Manual on Codes (WMO-No. 306), Volume I.1, Part A,
FM 35-XI TEMP / FM 36-XI TEMP SHIP. Code tables cited inline below.

IMPORTANT: this decoder has NOT yet been validated against real captured
bulletins (as of 2026-09-19) -- built from the documented WMO code tables,
but real-world encoding quirks (station-specific habits, missing-data
conventions, older vs newer code table revisions) are common in practice.
Per the Soundings feature plan, run decode_ttaa()/decode_ttbb() against a
real backlog of captured bulletins and spot-check a couple of stations
against a known-good source (e.g. SPC's own sounding plots) before trusting
this for anything operational.
"""
import os
import re
import sys
import argparse
import logging
from datetime import datetime, timezone, timedelta

import psycopg2

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DB_HOST = '192.168.0.60'
DB_NAME = 'avwx_data'
DB_USER = 'avwx_user'
DB_PASS = 'avwx_pass'

LOG_FILE = '/var/log/cap_wxcop/sounding_ingest.log'
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%SZ',
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stderr)],
)
log = logging.getLogger('ingest_sounding')

# ---------------------------------------------------------------------------
# WMO header parsing
# ---------------------------------------------------------------------------
# TTAA00 KWBC 191200  /  TTBB00 KWBC 191200  /  UUAA00 KWBC 191200 ...
RE_WMO_HEADER = re.compile(
    r'^(TT|UU)(AA|BB)(\d{2})\s+([A-Z]{4})\s+(\d{2})(\d{2})(\d{2})',
    re.MULTILINE,
)

# Mandatory-level ID line: IIiii (5-digit block+station number)
RE_STATION = re.compile(r'\b(\d{5})\b')

# ---------------------------------------------------------------------------
# TEMP code tables
# ---------------------------------------------------------------------------
# Mandatory-level pressure indicators (TTAA), Code table 0577.
# Value = pressure in mb. Order as transmitted; height units switch at 500mb.
TTAA_LEVEL_MB = {
    '99': 'SFC', '00': 1000, '92': 925, '85': 850, '70': 700, '50': 500,
    '40': 400, '30': 300, '25': 250, '20': 200, '15': 150, '10': 100,
}
# '88' = tropopause, '77' = max wind -- handled separately, not a fixed level.

def _decode_temp_sign(ttt_raw):
    """TTT is temp*10 with sign folded into the last digit's parity (Code
    table 0777): even last digit = positive, odd = negative."""
    if ttt_raw == '///':
        return None
    val = int(ttt_raw)
    sign = -1 if (val % 10) % 2 == 1 else 1
    return sign * val / 10.0


def _decode_dewpoint_depression(dd_raw):
    """DD dewpoint-depression code (Code table 0777):
    00-50 -> tenths of a degree (0.0-5.0C); 56-99 -> whole degrees, value-50
    (6-49C). 51-55 unused/reserved."""
    if dd_raw == '//':
        return None
    val = int(dd_raw)
    if val <= 50:
        return val / 10.0
    if val >= 56:
        return float(val - 50)
    return None  # 51-55 reserved, shouldn't appear


def _decode_wind(ddfff_raw, wind_units_knots):
    """dddff wind group: ddd = direction, rounded to nearest 5 degrees. If
    ddd's value needs speeds >=100, a preceding indicator group ('51515')
    signals +100kt to all following ff speed values in this report -- the
    caller tracks that flag and passes the adjusted ff separately; here we
    just split direction/speed out of one 5-digit group where ddd occupies
    the first 3 digits (tens+units, i.e. already in degrees, a multiple of
    5) and ff the last 2."""
    if '/' in ddfff_raw:
        return None, None
    raw = int(ddfff_raw)
    direction = (raw // 100) * 10 // 10  # first 3 digits = ddd directly... see note below
    # ddd is transmitted as direction in degrees (already a multiple of 5,
    # e.g. 270 for due west) using 3 digits; speed ff uses the last 2.
    direction = raw // 100
    speed = raw % 100
    return direction, speed


def decode_ttaa(text):
    """
    Decode a TTAA (or UUAA, wind-only) mandatory-level bulletin.

    Returns dict: {station_id, obs_time, wind_units_knots, levels: [...]}
    or None if the text doesn't look like a parseable TTAA/UUAA bulletin.

    Each level dict: {pressure_mb, height_m, temp_c, dewpoint_c,
    wind_dir_deg, wind_speed_kt} -- pressure_mb for the surface level is
    decoded from its own PoPoPo group (see the '99' special-case below),
    not a fixed value.
    """
    m = RE_WMO_HEADER.search(text)
    if not m:
        return None
    report_kind, part, ii_cat, station_wmo_unused, day_raw, hour, minute = m.groups()
    if part != 'AA':
        return None

    day = int(day_raw)
    wind_units_knots = day > 50   # Code table 1855: +50 to day => wind in kt
    if wind_units_knots:
        day -= 50

    now = datetime.now(timezone.utc)
    try:
        obs_time = datetime(now.year, now.month, day, int(hour), 0, tzinfo=timezone.utc)
        if obs_time > now + timedelta(days=2):
            obs_time = obs_time.replace(month=now.month - 1 if now.month > 1 else 12,
                                         year=now.year if now.month > 1 else now.year - 1)
    except ValueError:
        return None

    # Station ID: first 5-digit group after the header line, on the next
    # "line" (bulletins are whitespace-separated groups, not always real
    # newlines -- work on the group stream, not literal lines).
    body = text[m.end():]
    groups = body.split()
    if not groups:
        return None
    station_id = groups[0]
    if not re.fullmatch(r'\d{5}', station_id):
        return None

    levels = []
    i = 0   # tokens already excludes the station-id group (groups[0])
    tokens = groups[1:]
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if len(tok) < 2:
            i += 1
            continue
        level_key = tok[:2]

        if level_key in ('88', '77', '66'):
            # Tropopause (88), max wind (77), or a shear/units indicator
            # (66/51515 etc.) -- skip the group(s); out of scope for a
            # basic Skew-T/hodograph (tropopause marker isn't a plotted
            # level, max-wind is informational).
            i += 2 if level_key == '77' else 3
            continue

        if level_key not in TTAA_LEVEL_MB:
            i += 1
            continue

        pressure = TTAA_LEVEL_MB[level_key]
        hhh = tok[2:]
        if i + 2 >= n:
            break
        ttt_dd = tokens[i + 1]
        ddfff = tokens[i + 2]

        if level_key == '99':
            # Surface group is the one exception: hhh is actually surface
            # PRESSURE (PoPoPo), not height -- station elevation isn't
            # transmitted here at all. Convention: value >= 500 -> pressure
            # = value directly (500-999mb); value < 500 -> 1000 + value
            # (1000-1099mb, the overwhelmingly common case).
            try:
                pval = int(hhh)
                pressure = pval if pval >= 500 else 1000 + pval
            except ValueError:
                pressure = None
            height_m = None
        else:
            # Height encoding (Code table 0577 conventions): 1000-700mb
            # transmitted in meters; 500mb and above in geopotential
            # decameters. This is the trickiest part of TEMP height decoding
            # in practice and the most likely thing to need correction
            # against real bulletins -- treating hhh as meters below 500mb
            # and decameters at/above 500mb here as a documented first pass.
            try:
                hval = int(hhh)
            except ValueError:
                hval = None
            if hval is not None and isinstance(pressure, int) and pressure <= 500:
                height_m = hval * 10
            else:
                height_m = hval

        temp_c = _decode_temp_sign(ttt_dd[:3]) if len(ttt_dd) >= 5 else None
        dewpoint_depression = _decode_dewpoint_depression(ttt_dd[3:5]) if len(ttt_dd) >= 5 else None
        dewpoint_c = (temp_c - dewpoint_depression) if (temp_c is not None and dewpoint_depression is not None) else None
        wind_dir, wind_speed = _decode_wind(ddfff, wind_units_knots) if len(ddfff) == 5 else (None, None)
        if wind_speed is not None and not wind_units_knots:
            wind_speed = round(wind_speed * 1.94384)  # m/s -> kt, for a consistent unit in storage

        levels.append({
            'pressure_mb': pressure,
            'height_m': height_m,
            'temp_c': temp_c,
            'dewpoint_c': dewpoint_c,
            'wind_dir_deg': wind_dir,
            'wind_speed_kt': wind_speed,
        })
        i += 3

    if not levels:
        return None

    return {
        'station_id': station_id,
        'obs_time': obs_time,
        'report_type': report_kind + part,
        'wind_units_knots': wind_units_knots,
        'levels': levels,
    }


def decode_ttbb(text):
    """
    Decode a TTBB (or UUBB) significant-level bulletin: temperature/dewpoint
    only (no wind -- significant-level winds come in a separate PPBB section
    this v1 decoder does not yet handle).

    Returns dict: {station_id, obs_time, report_type, levels: [...]}
    Each level: {pressure_mb, temp_c, dewpoint_c} (no height -- significant
    levels are identified by pressure only, not height).
    """
    m = RE_WMO_HEADER.search(text)
    if not m:
        return None
    report_kind, part, ii_cat, _unused, day_raw, hour, minute = m.groups()
    if part != 'BB':
        return None

    day = int(day_raw)
    if day > 50:
        day -= 50
    now = datetime.now(timezone.utc)
    try:
        obs_time = datetime(now.year, now.month, day, int(hour), 0, tzinfo=timezone.utc)
    except ValueError:
        return None

    body = text[m.end():]
    groups = body.split()
    if not groups:
        return None
    station_id = groups[0]
    if not re.fullmatch(r'\d{5}', station_id):
        return None

    levels = []
    tokens = groups[1:]
    i = 0
    n = len(tokens)
    while i < n - 1:
        tok = tokens[i]
        # Significant-level groups are '11PPP', '22PPP', ... '(N)(N)PPP'
        # (section-of-10 marker + 3-digit pressure in whole mb, or tenths
        # for levels below 100mb -- out of scope here) followed by a
        # TTTDD temp/dewpoint group. A bare 3-digit-prefixed group not
        # matching this shape ends the significant-level section (e.g. the
        # '21212' wind-section marker, or '31313' region indicator).
        if len(tok) == 5 and tok[:2].isdigit() and int(tok[:2]) not in (21, 31, 51):
            press_raw = tok[2:]
            try:
                pressure_mb = int(press_raw)
                if pressure_mb < 100:
                    pressure_mb += 1000  # e.g. '850' vs a stray '056' -- not fully general, see note below
            except ValueError:
                i += 1
                continue
            if pressure_mb < 100:
                # Below our 100mb floor -- stop (TTCC/TTDD territory).
                break
            ttt_dd = tokens[i + 1] if i + 1 < n else ''
            temp_c = _decode_temp_sign(ttt_dd[:3]) if len(ttt_dd) >= 5 else None
            dd = _decode_dewpoint_depression(ttt_dd[3:5]) if len(ttt_dd) >= 5 else None
            dewpoint_c = (temp_c - dd) if (temp_c is not None and dd is not None) else None
            levels.append({'pressure_mb': pressure_mb, 'temp_c': temp_c, 'dewpoint_c': dewpoint_c})
            i += 2
        else:
            i += 1

    return {
        'station_id': station_id,
        'obs_time': obs_time,
        'report_type': report_kind + part,
        'levels': levels,
    }


def merge_profile(ttaa, ttbb):
    """Merge TTAA mandatory levels + TTBB significant levels into one
    surface-to-100mb profile, sorted by decreasing pressure (surface up)."""
    by_pressure = {}
    if ttaa:
        for lv in ttaa['levels']:
            key = lv['pressure_mb']
            if key is None:
                continue  # surface pressure failed to parse
            by_pressure[key] = {
                'pressure_mb': key,
                'height_m': lv['height_m'],
                'temp_c': lv['temp_c'],
                'dewpoint_c': lv['dewpoint_c'],
                'wind_dir_deg': lv['wind_dir_deg'],
                'wind_speed_kt': lv['wind_speed_kt'],
            }
    if ttbb:
        for lv in ttbb['levels']:
            key = lv['pressure_mb']
            if key in by_pressure:
                # Prefer TTAA's value (has height/wind); TTBB just fills gaps.
                existing = by_pressure[key]
                if existing['temp_c'] is None:
                    existing['temp_c'] = lv['temp_c']
                if existing['dewpoint_c'] is None:
                    existing['dewpoint_c'] = lv['dewpoint_c']
            else:
                by_pressure[key] = {
                    'pressure_mb': key,
                    'height_m': None,
                    'temp_c': lv['temp_c'],
                    'dewpoint_c': lv['dewpoint_c'],
                    'wind_dir_deg': None,
                    'wind_speed_kt': None,
                }

    def sort_key(lv):
        return 10000 if lv['pressure_mb'] is None else lv['pressure_mb']

    return sorted(by_pressure.values(), key=sort_key, reverse=True)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def db_connect():
    return psycopg2.connect(host=DB_HOST, dbname=DB_NAME, user=DB_USER, password=DB_PASS)


def upsert_sounding(conn, station_id, obs_time, report_type, raw_text, profile):
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO observations.sounding
            (station_id, obs_time, report_type, raw_text, profile)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (station_id, obs_time, report_type)
        DO UPDATE SET raw_text = EXCLUDED.raw_text, profile = EXCLUDED.profile
    """, (station_id, obs_time, report_type, raw_text, psycopg2_json(profile)))
    conn.commit()
    cur.close()


def psycopg2_json(obj):
    import json
    return json.dumps(obj)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def process_bulletin(text, dry_run=False):
    ttaa = decode_ttaa(text)
    ttbb = None if ttaa else decode_ttbb(text)  # a single bulletin is one or the other

    parsed = ttaa or ttbb
    if not parsed:
        log.warning('Could not parse bulletin (not TTAA/TTBB/UUAA/UUBB, or malformed): %r', text[:80])
        return False

    profile = merge_profile(ttaa, None) if ttaa else merge_profile(None, ttbb)

    if dry_run:
        print(f"station={parsed['station_id']} obs_time={parsed['obs_time']} "
              f"type={parsed['report_type']} levels={len(profile)}")
        for lv in profile:
            print(' ', lv)
        return True

    conn = db_connect()
    try:
        upsert_sounding(conn, parsed['station_id'], parsed['obs_time'],
                         parsed['report_type'], text, profile)
        log.info('Ingested %s %s %s (%d levels)', parsed['report_type'],
                  parsed['station_id'], parsed['obs_time'], len(profile))
    finally:
        conn.close()
    return True


def main():
    ap = argparse.ArgumentParser(description='Ingest TTAA/TTBB/UUAA/UUBB sounding bulletins')
    ap.add_argument('--file', type=str, default=None, help='Read bulletin from a file instead of stdin')
    ap.add_argument('--dry-run', action='store_true', help='Decode and print, do not write to DB')
    args = ap.parse_args()

    if args.file:
        with open(args.file, 'r', errors='replace') as f:
            text = f.read()
    else:
        text = sys.stdin.read()

    ok = process_bulletin(text, dry_run=args.dry_run)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()

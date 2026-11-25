#!/var/www/cap_winds_app/venv/bin/python3
"""
ingest_sounding.py — Decode WMO upper-air BUFR soundings into
observations.sounding.

Scope: surface through 100mb only -- CAP aircraft never operate anywhere
near those altitudes, same floor the original (abandoned) TTAA/TTBB text
decoder used.

Rewritten 2026-11-24 from a from-scratch (and never-wired, never-validated)
TTAA/TTBB/UUAA/UUBB text decoder once the real observed feed turned out to
be BUFR, not WMO alphanumeric TEMP (see memory
project_soundings_observed_ingest_gap for the full history: the capture
pqact rule itself had two separate bugs before any real BUFR ever reached
disk). This version is built and tuned against 2,100+ real captured
products from the live feed, not guessed from the WMO code tables blind --
notes below on real-world template quirks found that way.

Real-world BUFR template quirks found by inspecting actual captured
traffic (not assumed from documentation):
  - Two distinct template families are both live on this feed:
      1. Modern high-resolution individual-station reports (WMO template
         3-09-052 and variants) -- near-1-second telemetry, thousands of
         samples per station. Carries `extendedVerticalSoundingSignificance`
         bit flags (WMO table 0 08 042) marking which samples are actual
         standard/significant levels -- filtering on that (nonzero) thins
         the firehose down to a normal-sized profile, the BUFR-native
         equivalent of TTAA mandatory + TTBB significant levels combined.
      2. Legacy collective bulletins (edition 3, e.g. the KWBC_IUSV/IUSZ/
         IUSY/IUSU family) -- each BUFR *subset* in the message is a
         DIFFERENT station (a regional roll-up), already at moderate
         resolution (no 1-second firehose), using the older
         `verticalSoundingSignificance` key. Confirmed by checking
         block/station numbers per subset -- they differ. No filtering
         needed/wanted here; nearly every transmitted level already is a
         reportable one.
  - A single product can carry MULTIPLE subsets = multiple stations in
    one message (both of the above families do this) -- the only correct
    way found to pull them apart is extract-then-re-decode-as-its-own-
    message (`extractSubset` + `doExtractSubsets` + `codes_get_message` +
    `codes_new_from_message`), NOT reading key arrays off the original
    handle by rank index -- that returns values from the WRONG subset.
  - In the legacy template, wind (`windDirection`/`windSpeed`) is encoded
    as a SEPARATELY-counted replicated group, shorter than and NOT
    index-aligned with the pressure/temp array (confirmed: a wind entry's
    BUFR rank number has no relationship to the pressure entry it
    logically pairs with). Rather than build unverified positional
    correlation logic, this decoder only trusts wind when its array
    length matches the pressure array's length 1:1 (true for the modern
    high-res template, false for legacy) -- legacy-sourced levels come
    through as temp/dewpoint only, no wind, which is an honest reflection
    of what we can safely extract, not a bug.
  - Products with no `BUFR` magic in the body are real WMO `NIL` reports
    (station had nothing to transmit this cycle), not malformed BUFR --
    treated as "nothing to decode", not an error.

Called by pqact PIPE action (stdin = raw captured product: WMO ASCII
header + full binary BUFR message, same as the FILE capture rule still
active in pqact_hds_tpw.conf -- not wired to PIPE yet as of this rewrite,
pending Gerry's sign-off per feedback_explain_before_cross_host_edits):
    HDS  <pattern>
        PIPE  -close  venv/bin/python3 scripts/ingest_sounding.py

Usage:
    ingest_sounding.py              # read raw product from stdin (pqact mode)
    ingest_sounding.py --file PATH  # read from a file (backfill/testing)
    ingest_sounding.py --dry-run    # decode and print, don't write to DB
"""
import os
import sys
import json
import base64
import argparse
import logging
from datetime import datetime, timezone

import psycopg2
from eccodes import (
    codes_get, codes_set, codes_get_array, codes_release,
    codes_get_message, codes_new_from_message, CodesInternalError,
    CODES_MISSING_LONG, CODES_MISSING_DOUBLE,
)

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

PRESSURE_FLOOR_MB = 100  # CAP aircraft scope -- see module docstring

# Geographic scope: CAP's own area of operations only -- CONUS, Alaska,
# Hawaii, the Caribbean, and Guam. Not a global sounding archive; Gerry's
# explicit call 2026-11-24 once real traffic showed plenty of Europe/Asia
# stations flowing on this feed. Checked against each subset's own decoded
# lat/lon when available (covers nearly all individual-station reports).
REGIONS_LATLON = [
    (24.0, 50.0, -125.5, -66.5),    # CONUS
    (51.0, 72.0, -180.0, -129.0),   # Alaska
    (18.0, 23.0, -161.0, -154.0),   # Hawaii
    (9.0, 27.5, -85.0, -59.0),      # Caribbean
    (13.0, 14.0, 144.5, 145.0),     # Guam
]
# Fallback for the legacy collective template, which doesn't carry lat/lon
# at all (see module docstring) -- WMO block numbers assigned to Alaska (70)
# and the continental/other USA (72, 74). Deliberately NOT including 78
# (Caribbean/Central America, shared with non-US nations) or 91 (Pacific,
# shared internationally incl. Hawaii/Guam) here -- without a position to
# check, those blocks are too ambiguous to trust; such subsets are dropped
# rather than risk pulling in an out-of-scope station.
US_BLOCK_FALLBACK = {70, 72, 74}
# Alaska's eastern border isn't a clean lat/lon rectangle -- the Alaska box
# above also sweeps up a slice of the Yukon/NWT. Found by real backfill
# data (71957, 71964 both landed inside that box). Canada's WMO blocks are
# unambiguous, so exclude them even when lat/lon says "in the box".
NON_US_BLOCK_EXCLUDE = {71, 73}  # Canada


def _in_scope(station_id, lat, lon):
    block = int(station_id[:2]) if station_id and station_id.isdigit() and len(station_id) == 5 else None
    if block in NON_US_BLOCK_EXCLUDE:
        return False
    if lat is not None and lon is not None:
        return any(lat_min <= lat <= lat_max and lon_min <= lon <= lon_max
                   for lat_min, lat_max, lon_min, lon_max in REGIONS_LATLON)
    if block is not None:
        return block in US_BLOCK_FALLBACK
    return False


def _is_missing(v):
    if v is None:
        return True
    if isinstance(v, str):
        return True  # eccodes returns the literal string on a missing non-numeric key
    if v in (CODES_MISSING_LONG, CODES_MISSING_DOUBLE):
        return True
    return isinstance(v, float) and v > 1e10


def _get_scalar(handle, key):
    try:
        v = codes_get(handle, key)
    except CodesInternalError:
        return None
    return None if _is_missing(v) else v


def _get_array_safe(handle, key):
    try:
        arr = list(codes_get_array(handle, key))
    except CodesInternalError:
        return None
    return arr if arr else None


def _station_id_from(handle):
    block = _get_scalar(handle, 'blockNumber')
    stn = _get_scalar(handle, 'stationNumber')
    if block is not None and stn is not None:
        return f'{int(block):02d}{int(stn):03d}'
    ident = _get_scalar(handle, 'shipOrMobileLandStationIdentifier')
    if ident:
        ident = str(ident).strip()
        if ident:
            return ident
    return None


def _obs_time_from(handle):
    year = _get_scalar(handle, 'year')
    month = _get_scalar(handle, 'month')
    day = _get_scalar(handle, 'day')
    hour = _get_scalar(handle, 'hour')
    minute = _get_scalar(handle, 'minute') or 0
    second = _get_scalar(handle, 'second') or 0
    if year is None or month is None or day is None or hour is None:
        return None
    year = int(year)
    if year < 100:
        year += 2000
    try:
        return datetime(year, int(month), int(day), int(hour), int(minute), int(second),
                         tzinfo=timezone.utc)
    except ValueError:
        return None


def decode_subset(handle):
    """Decode one already-unpacked, single-subset BUFR handle (one
    station's sounding) into {station_id, obs_time, lat, lon, levels: [...]}
    or None if nothing usable was found. See module docstring for the
    high-res-vs-legacy-template handling this applies."""
    station_id = _station_id_from(handle)
    obs_time = _obs_time_from(handle)
    if not station_id or not obs_time:
        return None

    lat = _get_scalar(handle, 'latitude')
    lon = _get_scalar(handle, 'longitude')

    if not _in_scope(station_id, lat, lon):
        return None

    pressure = _get_array_safe(handle, 'pressure')
    if not pressure:
        return None
    n = len(pressure)

    sig = _get_array_safe(handle, 'extendedVerticalSoundingSignificance')
    high_res = sig is not None
    if sig is None:
        sig = _get_array_safe(handle, 'verticalSoundingSignificance')

    height = _get_array_safe(handle, 'nonCoordinateGeopotentialHeight')
    if height is None:
        height = _get_array_safe(handle, 'nonCoordinateGeopotential')
    temp = _get_array_safe(handle, 'airTemperature')
    dewp = _get_array_safe(handle, 'dewpointTemperature')
    wdir = _get_array_safe(handle, 'windDirection')
    wspd = _get_array_safe(handle, 'windSpeed')
    # Trust wind only when it's (near enough to) the same length as the
    # pressure array -- confirmed empirically that when it IS close, index 0
    # of each lines up correctly (wind can run a couple of samples short at
    # the very end, e.g. a dropped fix near balloon burst); `at()` already
    # returns None past the end of a shorter array, so this degrades
    # gracefully. Legacy-template wind arrays are a SEPARATELY-counted
    # group unrelated by index (confirmed: lengths differ by the hundreds,
    # not a handful) -- that large a gap means don't even try to pair it.
    tolerance = max(10, n // 100)
    if wdir is not None and len(wdir) < n - tolerance:
        wdir = None
    if wspd is not None and len(wspd) < n - tolerance:
        wspd = None

    def at(arr, i):
        if arr is None or i >= len(arr):
            return None
        v = arr[i]
        return None if _is_missing(v) else v

    levels = []
    for i in range(n):
        p_pa = at(pressure, i)
        if p_pa is None:
            continue
        pressure_mb = p_pa / 100.0
        if pressure_mb < PRESSURE_FLOOR_MB:
            continue  # out of scope -- see module docstring

        if high_res:
            s = at(sig, i)
            if s is None or s == 0:
                continue  # thin the near-1-second firehose to named levels only

        t_k = at(temp, i)
        td_k = at(dewp, i)
        h_m = at(height, i)
        wd = at(wdir, i)
        ws_ms = at(wspd, i)

        levels.append({
            'pressure_mb': round(pressure_mb, 1),
            'height_m': round(h_m) if h_m is not None else None,
            'temp_c': round(t_k - 273.15, 1) if t_k is not None else None,
            'dewpoint_c': round(td_k - 273.15, 1) if td_k is not None else None,
            'wind_dir_deg': round(wd) if wd is not None else None,
            'wind_speed_kt': round(ws_ms * 1.94384, 1) if ws_ms is not None else None,
        })

    if not levels:
        return None

    levels.sort(key=lambda lv: -lv['pressure_mb'])  # surface up
    return {
        'station_id': station_id,
        'obs_time': obs_time,
        'lat': lat,
        'lon': lon,
        'levels': levels,
    }


def decode_bufr_product(data: bytes):
    """Decode every subset of one raw captured product (WMO ASCII header +
    binary BUFR body) into a list of sounding dicts, one per subset --
    a single product frequently bundles several stations (a regional
    collective) or, for an individual high-resolution report, just one --
    loop generically either way (see module docstring)."""
    idx = data.find(b'BUFR')
    if idx < 0:
        return []  # a real NIL report, or not a BUFR product at all
    body = data[idx:]

    try:
        outer = codes_new_from_message(body)
    except CodesInternalError as e:
        log.warning('not a decodable BUFR message (%d bytes): %s', len(body), e)
        return []
    if outer is None:
        return []

    results = []
    try:
        codes_set(outer, 'unpack', 1)
        nsub = codes_get(outer, 'numberOfSubsets')
        for i in range(1, nsub + 1):
            codes_set(outer, 'extractSubset', i)
            codes_set(outer, 'doExtractSubsets', 1)
            try:
                sub_msg = codes_get_message(outer)
            except CodesInternalError as e:
                log.warning('could not extract subset %d: %s', i, e)
                continue
            sub = codes_new_from_message(sub_msg)
            try:
                codes_set(sub, 'unpack', 1)
                decoded = decode_subset(sub)
                if decoded:
                    results.append(decoded)
            except CodesInternalError as e:
                log.warning('subset %d failed to decode: %s', i, e)
            finally:
                codes_release(sub)
    except CodesInternalError as e:
        log.warning('failed to unpack BUFR message: %s', e)
    finally:
        codes_release(outer)
    return results


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def db_connect():
    return psycopg2.connect(host=DB_HOST, dbname=DB_NAME, user=DB_USER, password=DB_PASS)


def upsert_sounding(conn, station_id, obs_time, raw_b64, levels, lat, lon):
    location = f'SRID=4326;POINT({lon} {lat})' if lat is not None and lon is not None else None
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO observations.sounding
            (station_id, obs_time, report_type, raw_text, profile, location)
        VALUES (%s, %s, 'BUFR', %s, %s, %s)
        ON CONFLICT (station_id, obs_time, report_type)
        DO UPDATE SET raw_text = EXCLUDED.raw_text, profile = EXCLUDED.profile,
                      location = EXCLUDED.location
    """, (station_id, obs_time, raw_b64, json.dumps(levels), location))
    conn.commit()
    cur.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def process_product(data: bytes, dry_run=False):
    decoded_list = decode_bufr_product(data)
    if not decoded_list:
        log.info('no decodable sounding subsets in this product (%d bytes)', len(data))
        return False

    raw_b64 = base64.b64encode(data).decode('ascii') if not dry_run else None
    conn = None if dry_run else db_connect()
    try:
        for d in decoded_list:
            if dry_run:
                print(f"station={d['station_id']} obs_time={d['obs_time']} "
                      f"lat={d['lat']} lon={d['lon']} levels={len(d['levels'])}")
                for lv in d['levels']:
                    print(' ', lv)
                continue
            upsert_sounding(conn, d['station_id'], d['obs_time'], raw_b64, d['levels'],
                             d['lat'], d['lon'])
            log.info('Ingested BUFR %s %s (%d levels)', d['station_id'], d['obs_time'],
                      len(d['levels']))
    finally:
        if conn:
            conn.close()
    return True


def main():
    ap = argparse.ArgumentParser(description='Ingest BUFR upper-air soundings into observations.sounding')
    ap.add_argument('--file', type=str, default=None,
                     help='Read a raw captured product (WMO header + BUFR body) from a file, '
                          'instead of stdin')
    ap.add_argument('--dry-run', action='store_true', help='Decode and print, do not write to DB')
    args = ap.parse_args()

    if args.file:
        with open(args.file, 'rb') as f:
            data = f.read()
    else:
        data = sys.stdin.buffer.read()

    ok = process_product(data, dry_run=args.dry_run)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()

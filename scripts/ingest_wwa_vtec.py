#!/usr/bin/env python3
"""
ingest_wwa_vtec.py — Lean VTEC Watch/Warning/Advisory ingest for CAP WxCOP
===========================================================================
Parses NWS VTEC products and loads into observations.wwa PostGIS table.
No external dependencies beyond psycopg2 and standard library.

Two modes:
  1. pqact PIPE: reads product from stdin
     IDS|DDPLUS ^WUUS[0-9][0-9] ([A-Z]{4})
         PIPE -strip -close /var/www/cap_winds_app/venv/bin/python3 \
              /var/www/cap_winds_app/scripts/ingest_wwa_vtec.py

  2. Single file: --file PATH (for testing/backfill)
     python3 ingest_wwa_vtec.py --file /LDM/text/warnings/svr/2026/06/13/KIND_SVR-20260613-1234.txt

VTEC string format:
  /O.NEW.KIND.SV.W.0042.260613T1234Z-260613T1500Z/
   ^ ^ ^    ^  ^ ^ ^                 ^
   | | wfo  ph sig etn begin         end
   | action (NEW/CON/EXT/EXA/EXB/UPG/CAN/EXP/COR)
   class (O=Operational, T=Test, E=Experimental — only O is stored)

LAT...LON polygon (Storm Based Warning):
  LAT...LON 3852 9666 3863 9664 ...
  lat*100, lon*100 pairs; NWS lon is positive-west

TIME...MOT...LOC:
  TIME...MOT...LOC 0239Z 265DEG 39KT 3856 9653
  storm motion direction (FROM), speed knots, current centroid lat*100 lon*100
"""

import argparse
import logging
import re
import sys
from datetime import datetime, timezone, timedelta

import psycopg2

# ── Configuration ──────────────────────────────────────────────────────────────
DB_HOST = '192.168.0.60'
DB_NAME = 'avwx_data'
DB_USER = 'avwx_user'
DB_PASS = 'avwx_pass'

# Phenomena to ingest (phenomena, significance)
WANTED = {
    ('TO', 'W'),  # Tornado Warning
    ('TO', 'A'),  # Tornado Watch
    ('SV', 'W'),  # Severe Thunderstorm Warning
    ('SV', 'A'),  # Severe Thunderstorm Watch
    ('FF', 'W'),  # Flash Flood Warning
    ('FF', 'A'),  # Flash Flood Watch
    ('FL', 'W'),  # Flood Warning
    ('FL', 'A'),  # Flood Watch
    ('EW', 'W'),  # Extreme Wind Warning
    ('HW', 'W'),  # High Wind Warning
    ('HW', 'A'),  # High Wind Watch
    ('BZ', 'W'),  # Blizzard Warning
    ('WS', 'W'),  # Winter Storm Warning
    ('WS', 'A'),  # Winter Storm Watch
    ('IS', 'W'),  # Ice Storm Warning
    ('WC', 'W'),  # Wind Chill Warning
    ('WC', 'A'),  # Wind Chill Watch
    ('HT', 'W'),  # Excessive Heat Warning
    ('HT', 'A'),  # Excessive Heat Watch
    ('EC', 'W'),  # Extreme Cold Warning
    ('EC', 'A'),  # Extreme Cold Watch
    ('DS', 'W'),  # Dust Storm Warning
}

DEACTIVATE_ACTIONS = {'CAN', 'EXP', 'UPG'}
UPDATE_ACTIONS     = {'CON', 'EXT', 'EXA', 'EXB', 'COR'}

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%SZ',
)
log = logging.getLogger('ingest_wwa_vtec')

# ── Regex ──────────────────────────────────────────────────────────────────────
RE_VTEC = re.compile(
    r'/([OTE])\.'
    r'(NEW|CON|EXT|EXA|EXB|UPG|CAN|EXP|COR)\.'
    r'([A-Z]{4})\.'
    r'([A-Z]{2})\.'
    r'([A-Z])\.'
    r'(\d{4})\.'
    r'(\d{6}T\d{4}Z|000000T0000Z)-'
    r'(\d{6}T\d{4}Z|000000T0000Z)'
    r'/'
)
RE_WMO    = re.compile(r'([A-Z]{4}\d{2})\s+([A-Z]{4})\s+(\d{6})', re.MULTILINE)
RE_LATLON = re.compile(
    r'LAT\.\.\.LON\s+([\d\s]+?)(?=\n\s*\n|\nTIME\.\.\.|\nHAIL|\nWIND|\nTORNADO|\nSOURCE|\Z)',
    re.DOTALL
)
RE_MOT    = re.compile(
    r'TIME\.\.\.MOT\.\.\.LOC\s+(\d{4})Z\s+(\d{1,3})DEG\s+(\d{1,3})KT\s+([\d\s]+)'
)
RE_HEADLINE = re.compile(r'\.{3}([A-Z][^\n]{5,120}?)\.{3}')


# ── Time helpers ───────────────────────────────────────────────────────────────
def parse_vtec_time(s, ref_year):
    """Parse YYMMDDTHHMM Z → UTC datetime or None."""
    if not s or s.startswith('000000T0000'):
        return None
    try:
        yr = 2000 + int(s[0:2])
        mo = int(s[2:4])
        dy = int(s[4:6])
        hr = int(s[7:9])
        mn = int(s[9:11])
        return datetime(yr, mo, dy, hr, mn, tzinfo=timezone.utc)
    except (ValueError, IndexError):
        return None


def parse_wmo_time(ddhhmm, ref):
    """Parse DDHHMM → UTC datetime near ref."""
    try:
        dy = int(ddhhmm[0:2])
        hr = int(ddhhmm[2:4])
        mn = int(ddhhmm[4:6])
        dt = ref.replace(day=dy, hour=hr, minute=mn, second=0, microsecond=0)
        if dt > ref + timedelta(days=15):
            mo = ref.month - 1 or 12
            yr = ref.year if ref.month > 1 else ref.year - 1
            dt = dt.replace(year=yr, month=mo)
        return dt
    except (ValueError, IndexError):
        return ref


# ── Product deduplication ──────────────────────────────────────────────────────
def deduplicate(text):
    """LDM sometimes writes the product twice; truncate at second WMO header."""
    m = list(re.finditer(r'\n[A-Z]{4}\d{2} [A-Z]{4} \d{6}', text))
    if len(m) >= 2:
        return text[:m[1].start()]
    return text


# ── Geo parsing ────────────────────────────────────────────────────────────────
def parse_polygon(text):
    """
    Parse LAT...LON block → PostGIS WKT polygon string or None.
    NWS coordinates: lat*100 (N positive), lon*100 (W positive).
    """
    m = RE_LATLON.search(text)
    if not m:
        return None
    nums = m.group(1).split()
    if len(nums) < 6 or len(nums) % 2:
        return None
    try:
        coords = []
        for i in range(0, len(nums), 2):
            lat =  float(nums[i])   / 100.0
            lon = -float(nums[i+1]) / 100.0   # positive-west → negative
            coords.append((lon, lat))
        coords.append(coords[0])               # close ring
        pts = ', '.join(f'{lo:.4f} {la:.4f}' for lo, la in coords)
        return f'POLYGON(({pts}))'
    except (ValueError, IndexError):
        return None


def parse_storm_motion(text):
    """
    Parse TIME...MOT...LOC line.
    Returns (motion_deg, motion_kts, loc_lat, loc_lon) or None.
    motion_deg is the direction the storm is moving FROM (meteorological convention).
    """
    m = RE_MOT.search(text)
    if not m:
        return None
    try:
        deg  = int(m.group(2))
        kts  = int(m.group(3))
        locs = m.group(4).split()
        if len(locs) >= 2:
            loc_lat =  float(locs[0]) / 100.0
            loc_lon = -float(locs[1]) / 100.0
        else:
            loc_lat = loc_lon = None
        return (deg, kts, loc_lat, loc_lon)
    except (ValueError, IndexError):
        return None


def parse_ugc_zones(text):
    """
    Parse UGC zone list, e.g. INZ001-003-005-KYZ002-130100-
    Returns list of 6-char zone codes.
    """
    zones = []
    lines = text.split('\n')
    ugc_re = re.compile(r'^([A-Z]{2}[CZ]\d{3}(?:[-\d]{0,4})*)')
    end_re = re.compile(r'\d{6}-\s*$')
    collecting = False
    ugc_buf = []

    for line in lines:
        stripped = line.strip()
        if ugc_re.match(stripped) and not collecting:
            collecting = True
            ugc_buf.append(stripped)
        elif collecting:
            if stripped and not re.match(r'^[A-Z]', stripped):
                ugc_buf.append(stripped)
            else:
                break
        if collecting and end_re.search(stripped):
            break

    if not ugc_buf:
        return zones

    raw = ' '.join(ugc_buf)
    raw = re.sub(r'\d{6}-\s*$', '', raw).strip().rstrip('-')
    tokens = re.split(r'-', raw)
    state = geo = None

    for tok in tokens:
        tok = tok.strip()
        if not tok:
            continue
        if re.match(r'^[A-Z]{2}[CZ]\d{3}$', tok):
            state, geo = tok[:2], tok[2]
            zones.append(tok)
        elif re.match(r'^\d{3}$', tok) and state and geo:
            zones.append(f'{state}{geo}{tok}')

    return zones


def extract_headline(text):
    """Extract first ...HEADLINE... style text."""
    m = RE_HEADLINE.search(text)
    if m:
        return ' '.join(m.group(1).split())[:500]
    return None


# ── Database ───────────────────────────────────────────────────────────────────
def db_connect():
    return psycopg2.connect(
        host=DB_HOST, dbname=DB_NAME,
        user=DB_USER, password=DB_PASS
    )


def db_deactivate(cur, wfo, ph, sig, etn, yr):
    cur.execute("""
        UPDATE observations.wwa SET is_active = FALSE
        WHERE wfo=%s AND phenomena=%s AND significance=%s
          AND event_number=%s AND vtec_year=%s AND is_active=TRUE
    """, (wfo, ph, sig, etn, yr))
    return cur.rowcount


def db_update_end(cur, wfo, ph, sig, etn, yr, end_time, raw_seg):
    cur.execute("""
        UPDATE observations.wwa
        SET end_time=%s, raw_segment=%s, ingested_at=NOW()
        WHERE wfo=%s AND phenomena=%s AND significance=%s
          AND event_number=%s AND vtec_year=%s AND is_active=TRUE
    """, (end_time, raw_seg, wfo, ph, sig, etn, yr))
    return cur.rowcount


def db_insert(cur, wfo, ph, sig, etn, yr, action, wmo_hdr, prod_id,
              issue_time, begin_time, end_time, headline, raw_seg,
              geom_wkt, ugc_zones, storm_motion):
    """Insert new event, or merge ugc_zones into an existing row sharing the
    same VTEC key (wfo/phenomena/significance/event_number/vtec_action/
    vtec_year/issue_time). Multi-segment products (e.g. a single SPC watch
    spanning several states) repeat the identical VTEC string once per
    state/zone segment -- without the merge, every segment after the first
    would silently overwrite nothing and its UGC zones would be lost.
    Returns True on success."""
    # Append storm motion metadata to raw_segment
    if storm_motion:
        deg, kts, slat, slon = storm_motion
        loc_str = f' LOC {slat:.2f},{slon:.2f}' if slat is not None else ''
        raw_seg = raw_seg + f'\nSTORM_MOTION: {deg}DEG {kts}KT{loc_str}'

    try:
        cur.execute('SAVEPOINT wwa_ins')
        if geom_wkt:
            cur.execute(f"""
                INSERT INTO observations.wwa
                    (wfo, phenomena, significance, event_number, vtec_year,
                     vtec_action, wmo_header, product_id, issue_time,
                     begin_time, end_time, headline, raw_segment,
                     geom, ugc_zones, is_active)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        ST_GeomFromText(%s,4326),%s,TRUE)
                ON CONFLICT (wfo,phenomena,significance,event_number,
                             vtec_action,vtec_year,issue_time)
                DO UPDATE SET
                    ugc_zones = ARRAY(
                        SELECT DISTINCT unnest(
                            observations.wwa.ugc_zones || EXCLUDED.ugc_zones
                        )
                    ),
                    geom = COALESCE(observations.wwa.geom, EXCLUDED.geom),
                    raw_segment = observations.wwa.raw_segment
                        || E'\\n---SEGMENT---\\n' || EXCLUDED.raw_segment,
                    ingested_at = NOW()
            """, (wfo, ph, sig, etn, yr, action, wmo_hdr, prod_id,
                  issue_time, begin_time, end_time, headline, raw_seg,
                  geom_wkt, ugc_zones))
        else:
            cur.execute("""
                INSERT INTO observations.wwa
                    (wfo, phenomena, significance, event_number, vtec_year,
                     vtec_action, wmo_header, product_id, issue_time,
                     begin_time, end_time, headline, raw_segment,
                     ugc_zones, is_active)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE)
                ON CONFLICT (wfo,phenomena,significance,event_number,
                             vtec_action,vtec_year,issue_time)
                DO UPDATE SET
                    ugc_zones = ARRAY(
                        SELECT DISTINCT unnest(
                            observations.wwa.ugc_zones || EXCLUDED.ugc_zones
                        )
                    ),
                    raw_segment = observations.wwa.raw_segment
                        || E'\\n---SEGMENT---\\n' || EXCLUDED.raw_segment,
                    ingested_at = NOW()
            """, (wfo, ph, sig, etn, yr, action, wmo_hdr, prod_id,
                  issue_time, begin_time, end_time, headline, raw_seg,
                  ugc_zones))
        cur.execute('RELEASE SAVEPOINT wwa_ins')
        return True
    except Exception as e:
        cur.execute('ROLLBACK TO SAVEPOINT wwa_ins')
        log.error('INSERT failed %s %s.%s ETN=%04d: %s', wfo, ph, sig, etn, e)
        return False


def db_expire_old(cur):
    cur.execute("""
        UPDATE observations.wwa SET is_active=FALSE
        WHERE is_active=TRUE AND end_time IS NOT NULL AND end_time < NOW()
    """)
    return cur.rowcount


# ── Core processor ─────────────────────────────────────────────────────────────
def process(text, dry_run=False, debug=False):
    """Parse one NWS product; return number of DB ops performed."""
    text = deduplicate(text)

    wmo_m = RE_WMO.search(text)
    if not wmo_m:
        log.warning('No WMO header — skipping')
        return 0

    wmo_hdr    = f'{wmo_m.group(1)} {wmo_m.group(2)} {wmo_m.group(3)}'
    now        = datetime.now(timezone.utc)
    issue_time = parse_wmo_time(wmo_m.group(3), now)
    prod_id    = f'{wmo_m.group(1)}.{wmo_m.group(2)}.{wmo_m.group(3)}'
    vtec_year  = issue_time.year

# ── Core processor ─────────────────────────────────────────────────────────────
def process(text, dry_run=False, debug=False):
    """Parse one NWS product; return number of DB ops performed.

    Products may contain multiple segments, each with its own VTEC string
    and its own local UGC zone block (e.g. a single SPC watch spanning
    several states — one VTEC+UGC block per state). Each segment is sliced
    out by VTEC match position and parsed independently so UGC zones,
    polygon, and headline are never accidentally taken from the wrong
    segment or only the first one in the product.
    """
    text = deduplicate(text)

    wmo_m = RE_WMO.search(text)
    if not wmo_m:
        log.warning('No WMO header — skipping')
        return 0

    wmo_hdr    = f'{wmo_m.group(1)} {wmo_m.group(2)} {wmo_m.group(3)}'
    now        = datetime.now(timezone.utc)
    issue_time = parse_wmo_time(wmo_m.group(3), now)
    prod_id    = f'{wmo_m.group(1)}.{wmo_m.group(2)}.{wmo_m.group(3)}'
    vtec_year  = issue_time.year

    vtec_matches = list(RE_VTEC.finditer(text))
    if not vtec_matches:
        if debug:
            log.debug('No VTEC strings in product')
        return 0

    # Slice the product into one segment per VTEC match, anchored on each
    # segment's UGC zone block rather than the VTEC line itself. Real NWS
    # product structure per segment is:
    #     <UGC zone list>
    #     /O.NEW.WFO.PH.SIG.ETN.begin-end/
    #     <headline / hazard text>
    #     LAT...LON ... (storm-based warnings only)
    #     TIME...MOT...LOC ...
    # i.e. UGC precedes its VTEC, while polygon/motion/hazard text follow
    # it. A segment must therefore start at its own UGC block (found by
    # scanning backward from the VTEC match to the nearest preceding
    # UGC-format line) and run through just before the *next* segment's
    # UGC block begins (or end of text for the last segment) — this keeps
    # each segment's local UGC, polygon, and motion data together without
    # bleeding into neighboring segments.
    ugc_line_starts = [m.start() for m in re.finditer(r'^[A-Z]{2}[CZ]\d{3}', text, re.MULTILINE)]

    def _segment_start(vtec_pos):
        candidates = [p for p in ugc_line_starts if p < vtec_pos]
        return candidates[-1] if candidates else 0

    raw_starts = [_segment_start(m.start()) for m in vtec_matches]
    segments = []
    for i, m in enumerate(vtec_matches):
        seg_start = raw_starts[i]
        seg_end   = raw_starts[i + 1] if i + 1 < len(raw_starts) else len(text)
        segments.append((m, text[seg_start:seg_end]))

    if debug:
        log.debug('WMO=%s  segments=%d', wmo_hdr, len(segments))

    ops  = 0
    conn = None

    try:
        if not dry_run:
            conn = db_connect()
            cur  = conn.cursor()

        for m, seg_text in segments:
            vtec_class, action, wfo, ph, sig, etn_s, begin_s, end_s = m.groups()
            etn = int(etn_s)

            if vtec_class != 'O':
                continue
            if (ph, sig) not in WANTED:
                if debug:
                    log.debug('Skip %s.%s (%s)', ph, sig, action)
                continue

            # Parse this segment's local elements. UGC immediately follows
            # the VTEC line in NWS products, so per-segment slicing gives
            # the correct local zone list rather than the first one in
            # the whole product.
            geom_wkt     = parse_polygon(seg_text)
            storm_motion = parse_storm_motion(seg_text)
            ugc_zones    = parse_ugc_zones(seg_text)
            headline     = extract_headline(seg_text)
            raw_seg      = seg_text[:4000]

            begin_time = parse_vtec_time(begin_s, vtec_year)
            end_time   = parse_vtec_time(end_s,   vtec_year)

            if debug:
                deg_str = (f"{storm_motion[0]}DEG/{storm_motion[1]}KT"
                           if storm_motion else 'none')
                log.debug('  seg %s.%s ETN=%04d  polygon=%s  motion=%s  ugcs=%d',
                          ph, sig, etn, 'YES' if geom_wkt else 'NO',
                          deg_str, len(ugc_zones))

            if dry_run:
                sm = (f"{storm_motion[0]}DEG/{storm_motion[1]}KT"
                      if storm_motion else 'none')
                log.info('DRY-RUN: %s %s.%s ETN=%04d action=%s '
                         'begin=%s end=%s polygon=%s motion=%s ugcs=%d',
                         wfo, ph, sig, etn, action, begin_time, end_time,
                         'YES' if geom_wkt else 'NO', sm, len(ugc_zones))
                ops += 1
                continue

            # Deactivate
            if action in DEACTIVATE_ACTIONS:
                n = db_deactivate(cur, wfo, ph, sig, etn, vtec_year)
                if n:
                    log.info('Deactivated %d: %s %s.%s ETN=%04d (%s)',
                             n, wfo, ph, sig, etn, action)
                if action != 'UPG':
                    ops += 1
                    continue

            # Update end_time
            if action in UPDATE_ACTIONS:
                n = db_update_end(cur, wfo, ph, sig, etn, vtec_year,
                                  end_time, raw_seg)
                if n:
                    log.info('Updated: %s %s.%s ETN=%04d (%s)',
                             wfo, ph, sig, etn, action)
                    ops += 1
                    continue
                # No existing record — fall through to insert

            # Insert (or merge ugc_zones into matching existing row — see
            # db_insert's ON CONFLICT DO UPDATE)
            ok = db_insert(cur, wfo, ph, sig, etn, vtec_year, action,
                           wmo_hdr, prod_id, issue_time, begin_time,
                           end_time, headline, raw_seg, geom_wkt,
                           ugc_zones, storm_motion)
            if ok:
                log.info('Inserted: %s %s.%s ETN=%04d action=%s polygon=%s ugcs=%d',
                         wfo, ph, sig, etn, action,
                         'YES' if geom_wkt else 'NO', len(ugc_zones))
                ops += 1

        if not dry_run and conn:
            n = db_expire_old(cur)
            if n:
                log.info('Expired %d records', n)
            conn.commit()

    except Exception as e:
        log.error('Fatal error: %s', e)
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

    return ops


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description='CAP WxCOP lean VTEC ingest')
    ap.add_argument('--file',    metavar='PATH', help='Process single file')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--debug',   action='store_true')
    args = ap.parse_args()

    if args.debug:
        log.setLevel(logging.DEBUG)

    if args.file:
        try:
            with open(args.file, errors='replace') as f:
                text = f.read()
        except OSError as e:
            log.error('Cannot read %s: %s', args.file, e)
            sys.exit(1)
    else:
        text = sys.stdin.read()

    if not text.strip():
        log.warning('Empty input')
        sys.exit(0)

    ops = process(text, dry_run=args.dry_run, debug=args.debug)
    log.info('Done: %d op(s)', ops)


if __name__ == '__main__':
    main()

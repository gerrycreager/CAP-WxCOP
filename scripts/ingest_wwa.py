#!/usr/bin/env python3
"""
ingest_wwa.py — CAP WxCOP Watch/Warning/Advisory Ingest Script
===============================================================
Scans /LDM/text/warnings/ and /LDM/text/watches/ for new NWS WWA
products, parses them using pyiem, and loads active events into
the observations.wwa PostGIS table.

Designed to run from cron every 2-3 minutes.
Uses pyiem's bundled UGC county/zone parquet data — no IEM database
connection required.

Directory structure consumed:
  /LDM/text/warnings/svr/YYYY/MM/DD/KWFO_SVR-YYYYMMDD-HHMM.txt
  /LDM/text/warnings/tor/YYYY/MM/DD/KWFO_TOR-YYYYMMDD-HHMM.txt
  /LDM/text/warnings/ffw/YYYY/MM/DD/KWFO_FFW-YYYYMMDD-HHMM.txt
  /LDM/text/warnings/other/YYYY/MM/DD/  (mixed; filter by VTEC)
  /LDM/text/watches/YYYY/MM/DD/KWFO_WATCH-YYYYMMDD-HHMM.txt

Phenomena we care about (cadet ops relevant):
  TO.W  Tornado Warning
  SV.W  Severe Thunderstorm Warning
  FF.W  Flash Flood Warning
  FL.W  Flood Warning
  FL.A  Flood Watch
  EW.W  Extreme Wind Warning
  HW.W  High Wind Warning
  HW.A  High Wind Watch
  BZ.W  Blizzard Warning
  WS.W  Winter Storm Warning
  WS.A  Winter Storm Watch
  FW.W  Red Flag Warning  (fire wx — skip for cadet ops)
  FW.A  Fire Weather Watch (skip)

Actions handled:
  NEW   Insert new record, is_active=True
  CON   Continue — update end_time if changed, ensure is_active=True
  EXT   Extend — update end_time
  EXA   Extend area — treat as CON
  EXB   Extend time and area — treat as EXT
  UPG   Upgrade — mark old record inactive, insert new
  CAN   Cancel — mark is_active=False
  EXP   Expire — mark is_active=False
  COR   Correction — update raw_segment

Usage:
  /var/www/cap_winds_dev/venv/bin/python3 ingest_wwa.py [--debug] [--dry-run]

Cron (every 3 minutes, as root or ldm user):
  */3 * * * * /var/www/cap_winds_dev/venv/bin/python3 \
      /var/www/cap_winds_dev/scripts/ingest_wwa.py >> /var/log/cap_wwa_ingest.log 2>&1
"""

import argparse
import glob
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import psycopg
import pyiem
from pyiem.nws.products import parser as nws_parser
from pyiem.nws.ugc import UGC, UGCProvider
from shapely.geometry import mapping

# ── Configuration ────────────────────────────────────────────────────────────

DB_CONN = "host=192.168.0.60 dbname=avwx_data user=avwx_user"

LDM_TEXT = "/LDM/text"
WARNING_DIRS = {
    "tor":   f"{LDM_TEXT}/warnings/tor",
    "svr":   f"{LDM_TEXT}/warnings/svr",
    "ffw":   f"{LDM_TEXT}/warnings/ffw",
    "other": f"{LDM_TEXT}/warnings/other",
}
WATCH_DIR = f"{LDM_TEXT}/watches"

# State file — tracks which files have already been ingested
STATE_FILE = "/var/www/cap_winds_dev/wwa_ingest_state.txt"

# Phenomena we want to store (phenomenon.significance)
WANTED_PHENOMENA = {
    ("TO", "W"),  # Tornado Warning
    ("SV", "W"),  # Severe Thunderstorm Warning
    ("FF", "W"),  # Flash Flood Warning
    ("FF", "A"),  # Flash Flood Watch
    ("FL", "W"),  # Flood Warning
    ("FL", "A"),  # Flood Watch
    ("EW", "W"),  # Extreme Wind Warning
    ("HW", "W"),  # High Wind Warning
    ("HW", "A"),  # High Wind Watch
    ("BZ", "W"),  # Blizzard Warning
    ("WS", "W"),  # Winter Storm Warning
    ("WS", "A"),  # Winter Storm Watch
    ("IS", "W"),  # Ice Storm Warning
    ("WC", "W"),  # Wind Chill Warning
    ("WC", "A"),  # Wind Chill Watch
    ("WC", "Y"),  # Wind Chill Advisory
    ("HT", "W"),  # Excessive Heat Warning
    ("HT", "A"),  # Excessive Heat Watch
    ("EC", "W"),  # Extreme Cold Warning
    ("EC", "A"),  # Extreme Cold Watch
}

# Actions that deactivate a prior record
DEACTIVATE_ACTIONS = {"CAN", "EXP", "UPG"}

# Actions that update an existing record's end_time
UPDATE_ACTIONS = {"CON", "EXT", "EXA", "EXB", "COR"}

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("ingest_wwa")

# ── UGC Provider (built once at startup) ─────────────────────────────────────

def build_ugc_provider() -> UGCProvider:
    """Build UGCProvider from pyiem's bundled parquet files.
    Avoids any connection to iemdb-postgis.local."""
    pkg_dir = os.path.dirname(pyiem.__file__)
    geo_dir = os.path.join(pkg_dir, "data/geodf")

    legacy = {}
    for parquet_file in ["ugcs_county.parquet", "ugcs_zone.parquet",
                         "ugcs_firewx.parquet"]:
        path = os.path.join(geo_dir, parquet_file)
        if not os.path.exists(path):
            log.warning("Bundled parquet not found: %s", path)
            continue
        df = pd.read_parquet(path)
        for ugc_str, row in df.iterrows():
            if len(ugc_str) == 6 and ugc_str not in legacy:
                cwa = row.get("cwa", "")
                legacy[ugc_str] = UGC(
                    state=ugc_str[:2],
                    geoclass=ugc_str[2],
                    number=ugc_str[3:],
                    name=ugc_str,
                    wfos=[cwa] if cwa else [],
                )

    log.info("UGCProvider built with %d entries", len(legacy))
    return UGCProvider(legacy_dict=legacy)


# ── State file (processed file tracking) ─────────────────────────────────────

def load_state() -> set:
    """Return set of already-processed file paths."""
    if not os.path.exists(STATE_FILE):
        return set()
    with open(STATE_FILE) as f:
        return set(line.strip() for line in f if line.strip())


def save_state(processed: set) -> None:
    """Persist processed file paths, pruning entries older than 2 days."""
    now = datetime.now(timezone.utc)
    kept = set()
    for path in processed:
        # Extract date from path like .../2026/03/08/...
        parts = Path(path).parts
        try:
            # Find YYYY/MM/DD in path parts
            for i, p in enumerate(parts):
                if len(p) == 4 and p.isdigit() and i + 2 < len(parts):
                    yr, mo, dy = int(p), int(parts[i+1]), int(parts[i+2])
                    file_date = datetime(yr, mo, dy, tzinfo=timezone.utc)
                    age_days = (now - file_date).days
                    if age_days <= 2:
                        kept.add(path)
                    break
            else:
                kept.add(path)  # Can't parse date — keep it
        except (ValueError, IndexError):
            kept.add(path)

    with open(STATE_FILE, "w") as f:
        for path in sorted(kept):
            f.write(path + "\n")


# ── File discovery ────────────────────────────────────────────────────────────

def find_new_files(processed: set) -> list:
    """Find all WWA text files not yet processed, sorted oldest-first."""
    found = []

    # warnings subdirectories
    for subdir in WARNING_DIRS.values():
        pattern = f"{subdir}/[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]/*.txt"
        found.extend(glob.glob(pattern))

    # watches directory
    pattern = f"{WATCH_DIR}/[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]/*.txt"
    found.extend(glob.glob(pattern))

    new_files = [f for f in found if f not in processed]
    new_files.sort()  # oldest first by path (YYYY/MM/DD sorts correctly)
    return new_files


# ── Database operations ───────────────────────────────────────────────────────

def deactivate_prior(cur, wfo: str, phenomena: str, significance: str,
                     etn: int, vtec_year: int) -> int:
    """Mark prior active records for this event as inactive. Returns rows updated."""
    cur.execute("""
        UPDATE observations.wwa
        SET is_active = FALSE
        WHERE wfo = %s
          AND phenomena = %s
          AND significance = %s
          AND event_number = %s
          AND vtec_year = %s
          AND is_active = TRUE
    """, (wfo, phenomena, significance, etn, vtec_year))
    return cur.rowcount


def update_end_time(cur, wfo: str, phenomena: str, significance: str,
                    etn: int, vtec_year: int, endts, raw_segment: str) -> int:
    """Update end_time on existing active record. Returns rows updated."""
    cur.execute("""
        UPDATE observations.wwa
        SET end_time = %s,
            raw_segment = %s,
            ingested_at = NOW()
        WHERE wfo = %s
          AND phenomena = %s
          AND significance = %s
          AND event_number = %s
          AND vtec_year = %s
          AND is_active = TRUE
    """, (endts, raw_segment, wfo, phenomena, significance, etn, vtec_year))
    return cur.rowcount


def insert_event(cur, wfo: str, phenomena: str, significance: str,
                 etn: int, vtec_year: int, action: str, wmo_header: str,
                 product_id: str, issue_time, begin_time, end_time,
                 headline: str, raw_segment: str, geom_wkt,
                 ugc_zones: list, dry_run: bool = False) -> bool:
    """Insert a new WWA event record. Returns True if inserted."""

    geom_sql = None
    if geom_wkt:
        geom_sql = f"ST_GeomFromText('{geom_wkt}', 4326)"

    if dry_run:
        log.info("  DRY-RUN INSERT: %s %s.%s ETN=%s action=%s begin=%s end=%s",
                 wfo, phenomena, significance, etn, action, begin_time, end_time)
        return True

    try:
        cur.execute("SAVEPOINT wwa_insert")
        if geom_sql:
            cur.execute(f"""
                INSERT INTO observations.wwa
                    (wfo, phenomena, significance, event_number, vtec_year,
                     vtec_action, wmo_header, product_id, issue_time,
                     begin_time, end_time, headline, raw_segment,
                     geom, ugc_zones, is_active)
                VALUES
                    (%s, %s, %s, %s, %s,
                     %s, %s, %s, %s,
                     %s, %s, %s, %s,
                     {geom_sql}, %s, TRUE)
                ON CONFLICT (wfo, phenomena, significance, event_number,
                             vtec_action, vtec_year, issue_time)
                DO NOTHING
            """, (wfo, phenomena, significance, etn, vtec_year,
                  action, wmo_header, product_id, issue_time,
                  begin_time, end_time, headline, raw_segment,
                  ugc_zones))
        else:
            cur.execute("""
                INSERT INTO observations.wwa
                    (wfo, phenomena, significance, event_number, vtec_year,
                     vtec_action, wmo_header, product_id, issue_time,
                     begin_time, end_time, headline, raw_segment,
                     ugc_zones, is_active)
                VALUES
                    (%s, %s, %s, %s, %s,
                     %s, %s, %s, %s,
                     %s, %s, %s, %s,
                     %s, TRUE)
                ON CONFLICT (wfo, phenomena, significance, event_number,
                             vtec_action, vtec_year, issue_time)
                DO NOTHING
            """, (wfo, phenomena, significance, etn, vtec_year,
                  action, wmo_header, product_id, issue_time,
                  begin_time, end_time, headline, raw_segment,
                  ugc_zones))
        cur.execute("RELEASE SAVEPOINT wwa_insert")
        return True
    except Exception as e:
        cur.execute("ROLLBACK TO SAVEPOINT wwa_insert")
        log.error("  INSERT failed: %s", e)
        return False


# ── Core parser / processor ───────────────────────────────────────────────────

def process_file(filepath: str, provider: UGCProvider, cur,
                 dry_run: bool = False, debug: bool = False) -> int:
    """Parse one WWA file and process all relevant segments.
    Returns number of database operations performed."""

    try:
        text = open(filepath, errors="replace").read()
    except OSError as e:
        log.warning("Cannot read %s: %s", filepath, e)
        return 0

    # Skip Canadian office products (WFO starts with C, e.g. CWHX)
    filename = os.path.basename(filepath)
    if filename.startswith("C") and not filename.startswith("K"):
        if debug:
            log.debug("Skipping Canadian product: %s", filename)
        return 0

    try:
        prod = nws_parser(text, ugc_provider=provider)
    except Exception as e:
        log.warning("Parse failed %s: %s", filepath, e)
        return 0

    wmo_header = getattr(prod, "wmo", "") or ""
    product_id = prod.get_product_id()
    issue_time = prod.valid
    vtec_year = issue_time.year if issue_time else datetime.now().year

    ops = 0

    for seg in prod.segments:
        if not seg.vtec:
            continue  # Skip non-VTEC segments (signature lines, etc.)

        # Extract polygon WKT if present
        geom_wkt = None
        if seg.sbw is not None:
            try:
                geom_wkt = seg.sbw.wkt
            except Exception:
                geom_wkt = None

        # Extract UGC zone codes
        ugc_zones = [str(u) for u in seg.ugcs] if seg.ugcs else []

        # Extract headline
        headline = None
        if seg.headlines:
            headline = seg.headlines[0][:500]  # truncate safety

        # Raw segment text
        raw_segment = seg.unixtext[:4000] if seg.unixtext else ""

        for v in seg.vtec:
            ph = v.phenomena
            sig = v.significance
            action = v.action

            # Skip phenomena we don't care about
            if (ph, sig) not in WANTED_PHENOMENA:
                if debug:
                    log.debug("  Skipping %s.%s (%s)", ph, sig, action)
                continue

            wfo = v.office4 or v.office or prod.source or "UNKN"
            etn = v.etn or 0
            begints = v.begints
            endts = v.endts

            if debug:
                log.debug("  %s %s.%s ETN=%s action=%s begin=%s end=%s ugcs=%s sbw=%s",
                          wfo, ph, sig, etn, action, begints, endts,
                          ugc_zones, "YES" if geom_wkt else "NO")

            # ── Handle deactivating actions ──────────────────────────────
            if action in DEACTIVATE_ACTIONS:
                if not dry_run:
                    n = deactivate_prior(cur, wfo, ph, sig, etn, vtec_year)
                    if n:
                        log.info("  Deactivated %d record(s): %s %s.%s ETN=%s (%s)",
                                 n, wfo, ph, sig, etn, action)
                else:
                    log.info("  DRY-RUN DEACTIVATE: %s %s.%s ETN=%s (%s)",
                             wfo, ph, sig, etn, action)
                # For UPG, also insert the new/upgraded event below
                if action != "UPG":
                    ops += 1
                    continue

            # ── Handle update actions ────────────────────────────────────
            if action in UPDATE_ACTIONS:
                if not dry_run:
                    n = update_end_time(cur, wfo, ph, sig, etn,
                                        vtec_year, endts, raw_segment)
                    if n:
                        log.info("  Updated end_time: %s %s.%s ETN=%s (%s)",
                                 wfo, ph, sig, etn, action)
                        ops += 1
                        continue
                    else:
                        # No existing record found — fall through to insert
                        log.debug("  No existing record for %s, inserting as NEW",
                                  action)
                else:
                    log.info("  DRY-RUN UPDATE: %s %s.%s ETN=%s (%s)",
                             wfo, ph, sig, etn, action)
                    ops += 1
                    continue

            # ── Insert new record ────────────────────────────────────────
            ok = insert_event(
                cur=cur,
                wfo=wfo,
                phenomena=ph,
                significance=sig,
                etn=etn,
                vtec_year=vtec_year,
                action=action,
                wmo_header=wmo_header,
                product_id=product_id,
                issue_time=issue_time,
                begin_time=begints,
                end_time=endts,
                headline=headline,
                raw_segment=raw_segment,
                geom_wkt=geom_wkt,
                ugc_zones=ugc_zones,
                dry_run=dry_run,
            )
            if ok:
                log.info("  Inserted: %s %s.%s ETN=%s action=%s",
                         wfo, ph, sig, etn, action)
                ops += 1

    return ops


# ── Expiry maintenance ────────────────────────────────────────────────────────

def expire_old_records(cur, dry_run: bool = False) -> int:
    """Mark records as inactive where end_time has passed."""
    if dry_run:
        cur.execute("""
            SELECT COUNT(*) FROM observations.wwa
            WHERE is_active = TRUE
              AND end_time IS NOT NULL
              AND end_time < NOW()
        """)
        n = cur.fetchone()[0]
        log.info("DRY-RUN: Would expire %d time-expired records", n)
        return n

    cur.execute("""
        UPDATE observations.wwa
        SET is_active = FALSE
        WHERE is_active = TRUE
          AND end_time IS NOT NULL
          AND end_time < NOW()
    """)
    n = cur.rowcount
    if n:
        log.info("Expired %d time-expired WWA records", n)
    return n


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="CAP WxCOP WWA ingest")
    ap.add_argument("--debug", action="store_true", help="Verbose logging")
    ap.add_argument("--dry-run", action="store_true",
                    help="Parse and log but do not write to database")
    ap.add_argument("--reprocess", action="store_true",
                    help="Ignore state file and reprocess all files")
    ap.add_argument("--file", metavar="PATH",
                    help="Process a single specific file")
    args = ap.parse_args()

    if args.debug:
        log.setLevel(logging.DEBUG)

    # Build UGC provider once
    log.info("Building UGC provider from bundled parquet data...")
    try:
        provider = build_ugc_provider()
    except Exception as e:
        log.error("Failed to build UGC provider: %s", e)
        sys.exit(1)

    # Load state
    if args.reprocess:
        processed = set()
        log.info("--reprocess: ignoring state file")
    else:
        processed = load_state()
        log.info("State: %d previously processed files", len(processed))

    # Single file mode
    if args.file:
        files = [args.file]
    else:
        files = find_new_files(processed)
        log.info("Found %d new files to process", len(files))

    if not files:
        log.info("Nothing to do")
        return

    # Connect to database
    try:
        conn = psycopg.connect(DB_CONN)
        conn.autocommit = False
    except Exception as e:
        log.error("DB connection failed: %s", e)
        sys.exit(1)

    total_ops = 0
    newly_processed = set()

    try:
        with conn.cursor() as cur:
            for filepath in files:
                if args.debug:
                    log.debug("Processing: %s", filepath)
                ops = process_file(filepath, provider, cur,
                                   dry_run=args.dry_run,
                                   debug=args.debug)
                newly_processed.add(filepath)
                total_ops += ops

            # Expire time-passed records
            expire_old_records(cur, dry_run=args.dry_run)

            if not args.dry_run:
                conn.commit()
                log.info("Committed. Total ops: %d across %d files",
                         total_ops, len(newly_processed))
            else:
                conn.rollback()
                log.info("Dry-run complete. Would have made %d ops across %d files",
                         total_ops, len(newly_processed))

    except Exception as e:
        log.error("Fatal error during processing: %s", e)
        conn.rollback()
        conn.close()
        sys.exit(1)
    finally:
        conn.close()

    # Update state file
    if not args.dry_run and not args.reprocess:
        processed.update(newly_processed)
        save_state(processed)


if __name__ == "__main__":
    main()


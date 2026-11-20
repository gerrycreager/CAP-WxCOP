#!/usr/bin/env python3
"""
MADIS FSL2 surface-obs ingest (hfmetar + filtered mesonet2).

Invoked by pqact via PIPE (-close), once per product, reading the raw
gzip-compressed netCDF product on stdin:

    FSL2  "^FSL\\.CompressedNetCDF\\.MADIS\\.hfmetar\\."   PIPE  -close  .../ingest_madis_surface.py --stream hfmetar
    FSL2  "^FSL\\.CompressedNetCDF\\.MADIS\\.mesonet2\\."  PIPE  -close  .../ingest_madis_surface.py --stream mesonet2

Also supports `--file PATH` (gzip or already-decompressed netCDF) for
testing/backfill, and `--dry-run` to parse+filter without writing to the
database.

Each MADIS hourly file is NOT a one-shot delivery -- MADIS resends the
same nominal hour repeatedly as more obs arrive through the hour, with a
growing record count each time (confirmed live 2026-10-28: two real
deliveries of the same hfmetar hour, 5 min apart; the second contained
every row from the first, plus more). Dedup is therefore done at the row
level via ON CONFLICT (source_feed, station_id, observation_time), never
at the file/product level.

mesonet2 provider scope (Gerry's decision, 2026-10-28 -- see the
reference-madis-archive-access memory) -- this is NOT a blanket ingest
of every mesonet2 row:
  - APRSWXNET (CWOP crowdsourced stations) -- always included. This was
    also the majority provider by row count in the live sample.
  - Any provider whose name ends in "DOT" (state DOT road-weather
    networks, e.g. OHDOT/AKDOT) -- included ONLY for rows reporting at
    least one of the standard met fields (temperature, relHumidity,
    windSpeed, visibility). The many road-condition/chemical-treatment
    fields those stations also report (roadState*, roadLiquidChem*,
    roadSubsurfaceTemp*, etc.) are never stored for any provider -- the
    table schema below simply has no columns for them.
  - Everything else (ag-nets, coastal, RAWS fire-weather, HADS hydro,
    soil-moisture/fuel-moisture-only providers) is dropped.
hfmetar has no provider filter -- it's airport-scoped by definition and
was always fully in scope.
"""
import sys
import os
import gzip
import json
import fcntl
import logging
import argparse
import tempfile
from datetime import datetime, timezone

import numpy as np
import netCDF4
from psycopg2.extras import execute_values

sys.path.insert(0, '/var/www/cap_winds_app')
from db_config import get_connection

LOG_PATH = '/home/ldm/var/logs/ingest_madis_surface.log'
logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)

# ── single-drainer concurrency fix (2026-11-14) ─────────────────────────────
# Root cause of the data2 lock pileups (documented live multiple times,
# worst case 92 backends / 10+ hour waits): every pqact PIPE -close
# invocation was its own independent process, each opening its own
# connection and UPSERTing straight into the same recurring hot station
# rows (APRSWXNET/CWOP stations report constantly) -- N processes racing
# the same rows, serializing on Postgres row locks instead of in our own
# code. A faster decoder doesn't fix this: it only gets each process to
# the lock race sooner.
#
# Fix (same pattern already proven live for MRMS's render backlog, see
# mrms_render_pipe.sh): every invocation durably spools its raw bytes
# first (a plain file write -- can't fail from contention, can't block
# pqact), then tries to become that STREAM's single drainer via a
# non-blocking flock. Win -> loop decoding+writing the spool oldest-first,
# holding the lock the whole time, until it's empty. Lose -> exit
# immediately; the file is already safe on disk for the active drainer
# (or whichever invocation next acquires the lock) to pick up. This makes
# all of one stream's DB writes go through exactly one process at a time
# -- the hot-row race is gone by construction, not just slowed down.
# hfmetar and mesonet2 get separate spool dirs/locks since they never
# share rows (source_feed is part of the table's own UNIQUE constraint).
#
# --file/--dry-run (manual testing/backfill) deliberately bypass all of
# this and keep the original direct-process behavior -- there's no
# concurrent pqact invocation to race against in that path.
#
# SPOOL_BASE moved to tmpfs 2026-11-18 at Gerry's direction, after the
# measured mesonet2 backlog (278 files, 82GB, median file 382.9MB
# uncompressed -- LDM/pqact auto-decompresses before PIPE, confirmed via
# notifyme showing the real wire size is only ~30MB) made it clear the
# per-file write path's dominant cost is disk I/O contention, not
# anything SQL-side (a COPY-based rewrite was tried and reverted the
# same day -- made things worse, since it added writes on top of an
# already-saturated disk rather than removing any). r815 has 86GB RAM
# (post-upgrade) with ~83GB free, so moving the temporary spool off
# disk entirely removes a real chunk of I/O for the read/decompress/
# parse cycle, leaving only the final DB write as genuine disk I/O.
#
# TRADEOFF, IMPORTANT: this makes the spool VOLATILE -- the comment
# above ("durably spools... can't fail from contention") no longer
# means "survives a reboot," only "survives pqact/process-level
# contention." Anything sitting in tmpfs at the moment of a reboot is
# gone. Gerry explicitly accepted this trade 2026-11-18. Do NOT resume
# live traffic into this path (systemctl start ldm) around a planned
# reboot without flushing/draining first -- the pre-existing disk-based
# backlog from before this change is NOT affected (different directory,
# see standalone_drain.py), only NEW arrivals after this change took
# effect.
MADIS_SPOOL_BASE = '/mnt/ldm_tmpfs/madis_spool'
MADIS_LOCK_DIR = '/home/ldm/var/run'


def spool_write(stream, raw_bytes):
    spool_dir = os.path.join(MADIS_SPOOL_BASE, stream)
    os.makedirs(spool_dir, exist_ok=True)
    fname = '{}.{}.nc.gz'.format(
        datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S.%f'), os.getpid())
    path = os.path.join(spool_dir, fname)
    with open(path, 'wb') as f:
        f.write(raw_bytes)
    return path


def acquire_drain_lock(stream):
    """Non-blocking -- returns None immediately if another process is
    already draining this stream, never waits."""
    os.makedirs(MADIS_LOCK_DIR, exist_ok=True)
    lock_path = os.path.join(MADIS_LOCK_DIR, 'ingest_madis_{}.drain.lock'.format(stream))
    fh = open(lock_path, 'w')
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def write_stations(conn, rows):
    """PHASE 1 of the schema normalization Gerry asked for 2026-11-18:
    madis_surface_obs stores a full geom/name/provider/elevation on
    EVERY observation row, even though those are station-level
    properties that essentially never change between a station's
    observations -- confirmed live: 26.5M rows, only 14,320 distinct
    (source_feed, station_id) pairs, i.e. each station's geometry is
    redundantly stored and GIST-indexed ~1,856x on average. This writes
    that metadata into the new, small observations.madis_stations
    table instead (GIST index lives there now, on a few thousand slow-
    changing rows, not 26M+ fast-growing ones). Phase 1 is additive --
    madis_surface_obs keeps its own geom/name/provider/elevation
    columns for now, so any still-running old-code process (e.g. a
    backlog drainer started before this change) keeps working
    unchanged. Phase 2 (dropping those columns + the GIST index from
    madis_surface_obs, where the real I/O win lands) is a deliberate
    separate step, done only once nothing is still running pre-Phase-1
    code.

    last_seen is refreshed on every call here (real upsert, same
    connection as the observation write) -- this does NOT repeat
    observations.airports.has_reporting's mistake (a one-time import
    flag nobody ever updated); it can't go stale the same way."""
    latest = {}
    for r in rows:
        key = (r['source_feed'], r['station_id'])
        prev = latest.get(key)
        if prev is None or r['observation_time'] > prev['observation_time']:
            latest[key] = r
    if not latest:
        return
    values = [(
        r['source_feed'], r['station_id'], r['station_name'], r['data_provider'],
        r['lon'], r['lat'], r['elevation_m'], r['observation_time'], r['observation_time'],
    ) for r in latest.values()]
    sql = """
        INSERT INTO observations.madis_stations
            (source_feed, station_id, station_name, data_provider, geom, elevation_m, first_seen, last_seen)
        VALUES %s
        ON CONFLICT (source_feed, station_id) DO UPDATE SET
            station_name = COALESCE(EXCLUDED.station_name, observations.madis_stations.station_name),
            data_provider = COALESCE(EXCLUDED.data_provider, observations.madis_stations.data_provider),
            geom = COALESCE(EXCLUDED.geom, observations.madis_stations.geom),
            elevation_m = COALESCE(EXCLUDED.elevation_m, observations.madis_stations.elevation_m),
            last_seen = GREATEST(observations.madis_stations.last_seen, EXCLUDED.last_seen)
    """
    template = "(%s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s, %s, %s)"
    with conn.cursor() as cur:
        execute_values(cur, sql, values, template=template, page_size=500)


def process_bytes(conn, stream, raw_bytes):
    """Decode + write a single product's bytes against an already-open
    connection. Returns row count written."""
    if raw_bytes[:2] == b'\x1f\x8b':
        raw_bytes = gzip.decompress(raw_bytes)
    ds = open_dataset(raw_bytes)
    try:
        rows = parse_rows(ds, stream)
    finally:
        ds.close()
    if not rows:
        return 0
    write_stations(conn, rows)
    write_rows(conn, rows)
    if stream == 'hfmetar':
        write_metar_rows(conn, rows)
    return len(rows)


def drain_spool(stream, lock_fh):
    """Caller already holds the drain lock. Loop oldest-file-first until
    the spool is empty -- re-list fresh each pass (while still holding
    the lock) so anything that lands mid-drain still gets picked up this
    same pass, same fix as mrms_render_pipe.sh's drain loop."""
    spool_dir = os.path.join(MADIS_SPOOL_BASE, stream)
    conn = get_connection()
    try:
        ensure_schema(conn)
        while True:
            pending = sorted(os.listdir(spool_dir))
            if not pending:
                break
            fname = pending[0]
            fpath = os.path.join(spool_dir, fname)
            try:
                with open(fpath, 'rb') as f:
                    raw_bytes = f.read()
                n = process_bytes(conn, stream, raw_bytes)
                log.info('drained stream=%s file=%s rows=%d', stream, fname, n)
            except Exception:
                log.exception('failed processing spooled file %s (stream=%s) -- discarding', fpath, stream)
                # Without this, one failed file leaves the connection's
                # transaction aborted, and EVERY subsequent file drained on
                # this same long-lived connection fails too (psycopg2
                # InFailedSqlTransaction) until the process exits -- found
                # 2026-11-19: 46 original failures had cascaded into 425
                # knock-on failures / 473 discarded files total in this
                # log's history. Roll back so one bad file costs one file,
                # not everything drained after it in this session.
                conn.rollback()
            finally:
                os.remove(fpath)
    finally:
        conn.close()
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()


ALWAYS_INCLUDE_PROVIDERS = {'APRSWXNET'}
STANDARD_FIELD_VARS = ('temperature', 'relHumidity', 'windSpeed', 'visibility')

# Station-level identity/location lives in observations.madis_stations
# (created separately, see write_stations()) -- not repeated here. This
# table is purely the fast-growing temporal/sequential observation log,
# normalized 2026-11-18.
CREATE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS observations.madis_surface_obs (
        id                SERIAL PRIMARY KEY,
        source_feed       VARCHAR(16) NOT NULL,
        station_id        VARCHAR(16) NOT NULL,
        observation_time  TIMESTAMP NOT NULL,
        received_time     TIMESTAMP,
        temp_c            REAL,
        dewpoint_c        REAL,
        relative_humidity REAL,
        wind_dir          REAL,
        wind_speed_kts    REAL,
        wind_gust_kts     REAL,
        visibility_sm     REAL,
        altimeter_hg      REAL,
        raw_message       TEXT,
        inserted_at       TIMESTAMP DEFAULT NOW(),
        UNIQUE (source_feed, station_id, observation_time)
    )
"""
CREATE_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_madis_surface_obs_time ON observations.madis_surface_obs (observation_time)",
    "CREATE INDEX IF NOT EXISTS idx_madis_surface_obs_station ON observations.madis_surface_obs (station_id)",
]

# PHASE 2 of the schema normalization (2026-11-18): source_feed/
# data_provider/station_name/geom/elevation_m are station-level
# properties, not observation-level ones -- they now live in
# observations.madis_stations (written by write_stations(), keyed on
# (source_feed, station_id)) instead of being redundantly repeated and
# GIST-indexed on every one of this table's 26M+ rows. See the
# MADIS_SPOOL_BASE comment block above for the full rationale/numbers.
UPSERT_SQL = """
    INSERT INTO observations.madis_surface_obs (
        source_feed, station_id, observation_time, received_time, temp_c,
        dewpoint_c, relative_humidity, wind_dir, wind_speed_kts,
        wind_gust_kts, visibility_sm, altimeter_hg, raw_message
    ) VALUES %s
    ON CONFLICT (source_feed, station_id, observation_time) DO UPDATE SET
        received_time = EXCLUDED.received_time,
        temp_c = EXCLUDED.temp_c,
        dewpoint_c = EXCLUDED.dewpoint_c,
        relative_humidity = EXCLUDED.relative_humidity,
        wind_dir = EXCLUDED.wind_dir,
        wind_speed_kts = EXCLUDED.wind_speed_kts,
        wind_gust_kts = EXCLUDED.wind_gust_kts,
        visibility_sm = EXCLUDED.visibility_sm,
        altimeter_hg = EXCLUDED.altimeter_hg,
        raw_message = EXCLUDED.raw_message
"""
UPSERT_VALUES_TEMPLATE = """(
    %s, %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s, %s
)"""

# hfmetar-only: merge into observations.metar (same table EWMC/Flight Impacts
# already read via weather_enhanced_api.py's get_recent_metar_enhanced()), so
# every existing consumer picks up HFMETAR's 5-min cadence for CONUS/OCONUS
# ASOS stations with zero changes elsewhere. mesonet2 (CWOP/DOT) is NOT
# merged here -- different station universe, no sky-cover data, not what
# Gerry asked to replace. The hourly WMO-bulletin ingest_metar.py keeps
# running unchanged: it's still the only source for stations HFMETAR doesn't
# cover (non-ASOS, international).
UPSERT_METAR_SQL = """
    INSERT INTO observations.metar (
        station_id, observation_time, raw_text,
        temp_c, dewpoint_c, wind_dir, wind_speed_kts, wind_gust_kts,
        visibility_sm, altimeter_hg, flight_category,
        sky_conditions, present_weather, location, is_speci
    ) VALUES %s
    ON CONFLICT (station_id, observation_time) DO UPDATE SET
        raw_text = EXCLUDED.raw_text,
        temp_c = EXCLUDED.temp_c,
        dewpoint_c = EXCLUDED.dewpoint_c,
        wind_dir = EXCLUDED.wind_dir,
        wind_speed_kts = EXCLUDED.wind_speed_kts,
        wind_gust_kts = EXCLUDED.wind_gust_kts,
        visibility_sm = EXCLUDED.visibility_sm,
        altimeter_hg = EXCLUDED.altimeter_hg,
        flight_category = EXCLUDED.flight_category,
        sky_conditions = EXCLUDED.sky_conditions,
        present_weather = EXCLUDED.present_weather,
        location = EXCLUDED.location,
        is_speci = EXCLUDED.is_speci
"""
UPSERT_METAR_VALUES_TEMPLATE = """(
    %s, %s, %s,
    %s, %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s,
    ST_SetSRID(ST_MakePoint(%s, %s), 4326),
    %s
)"""


def ensure_schema(conn):
    """Each pqact PIPE -close product spawns a fresh process, so this runs
    on every single delivery. CREATE INDEX IF NOT EXISTS still takes a lock
    that serializes against other concurrent CREATE INDEX statements on the
    same table even when the index already exists -- harmless at low
    concurrency, but confirmed live (2026-11-01) to pile up a real backend
    lock queue once several hfmetar/mesonet2 children are decoding at once.
    Check cheaply (plain catalog SELECT, no DDL, no lock) and only run the
    actual CREATE statements on a genuine first run."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('observations.madis_surface_obs')")
        if cur.fetchone()[0] is not None:
            return
        cur.execute(CREATE_TABLE_SQL)
        for stmt in CREATE_INDEX_SQL:
            cur.execute(stmt)
    conn.commit()


def read_input(file_path):
    if file_path:
        with open(file_path, 'rb') as f:
            data = f.read()
    else:
        data = sys.stdin.buffer.read()
    if data[:2] == b'\x1f\x8b':
        data = gzip.decompress(data)
    return data


def open_dataset(netcdf_bytes):
    """Prefer netCDF4's in-memory reader (no temp file, no disk I/O for a
    PIPE child that may be spawned often); fall back to a temp file for
    older netCDF4 builds that lack the memory= kwarg."""
    try:
        return netCDF4.Dataset('inmemory', mode='r', memory=netcdf_bytes)
    except Exception:
        tmp = tempfile.NamedTemporaryFile(suffix='.nc', delete=False)
        try:
            tmp.write(netcdf_bytes)
            tmp.flush()
            tmp.close()
            return netCDF4.Dataset(tmp.name, mode='r')
        finally:
            os.unlink(tmp.name)


def fetch_str_array(ds, name, n):
    """Vectorized char-array -> numpy string array, via netCDF4's own
    chartostring (one call for all n rows, not one per row). chartostring
    truncates at the FIRST null byte but a stray embedded null mid-buffer
    (confirmed live in ~0.8% of mesonet2 rawMessage values) survives that
    and breaks psycopg2's mogrify -- strip every embedded null, not just
    leading/trailing whitespace."""
    if name not in ds.variables:
        return None
    arr = netCDF4.chartostring(ds.variables[name][:])
    return np.array([s.replace('\x00', '').strip() for s in arr])


def fetch_num_array(ds, name, n):
    """Vectorized numeric fetch for all n rows in one call -- masked/fill
    values become NaN. Per-row netCDF4 indexing (520k individual
    __getitem__ calls for a 30k-row/17-field mesonet2 file) measured at
    5+ minutes; the equivalent bulk fetch is ~0.05s per variable."""
    if name not in ds.variables:
        return np.full(n, np.nan)
    arr = np.ma.filled(ds.variables[name][:].astype('float64'), np.nan)
    arr[arr > 1e30] = np.nan  # defense-in-depth if auto-masking is ever off
    return arr


def nz(v):
    return None if np.isnan(v) else float(v)


def epoch_to_dt(v):
    if v is None or np.isnan(v):
        return None
    try:
        return datetime.fromtimestamp(v, tz=timezone.utc).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def k_to_c(v):
    return None if v is None else v - 273.15


def mps_to_kt(v):
    return None if v is None else v * 1.943844


def m_to_sm(v):
    return None if v is None else v / 1609.344


def pa_to_inhg(v):
    return None if v is None else v / 3386.389


M_TO_FT = 3.28084


def fetch_sky_layers(ds, n):
    """hfmetar only: decode the (recNum, maxSkyCover, maxSkyLen) skyCvr char
    array + parallel skyCovLayerBase (meters) into a per-row list of
    {'cover','height_ft'} dicts -- same shape as ingest_metar.py's
    sky_conditions, confirmed live against a real hfmetar sample (standard
    FEW/SCT/BKN/OVC/CLR codes). numSkyCoverObs says how many of the
    maxSkyCover slots are real for that row; masked/missing means no sky
    ob at all for that station (distinct from an explicit CLR layer)."""
    needed = ('skyCvr', 'skyCovLayerBase', 'numSkyCoverObs')
    if any(v not in ds.variables for v in needed):
        return [None] * n
    num_layers = ds.variables['numSkyCoverObs'][:]
    base_m = ds.variables['skyCovLayerBase'][:]
    cvr_chars = ds.variables['skyCvr'][:]
    max_layers = ds.dimensions['maxSkyCover'].size

    result = []
    for i in range(n):
        nl = num_layers[i]
        if np.ma.is_masked(nl) or nl <= 0:
            result.append(None)
            continue
        layers = []
        for L in range(min(int(nl), max_layers)):
            cover = str(netCDF4.chartostring(np.array(cvr_chars[i, L]))).strip()
            if not cover:
                continue
            entry = {'cover': cover}
            h = base_m[i, L]
            if not np.ma.is_masked(h):
                entry['height_ft'] = round(float(h) * M_TO_FT)
            layers.append(entry)
        result.append(layers or None)
    return result


def ceiling_from_sky(sky_conditions):
    if not sky_conditions:
        return None
    ceiling = None
    for layer in sky_conditions:
        if layer.get('cover') in ('BKN', 'OVC'):
            h = layer.get('height_ft')
            if h is not None and (ceiling is None or h < ceiling):
                ceiling = h
    return ceiling


def compute_flight_category(visibility_sm, ceiling_ft):
    """Identical thresholds to ingest_metar.py's parse_metar(), so a station
    fed by HFMETAR gets the same flight_category semantics as one fed by
    the hourly WMO bulletin."""
    if visibility_sm is None:
        return 'UNKNOWN'
    if visibility_sm < 1 or (ceiling_ft is not None and ceiling_ft < 500):
        return 'LIFR'
    if visibility_sm < 3 or (ceiling_ft is not None and ceiling_ft < 1000):
        return 'IFR'
    if visibility_sm < 5 or (ceiling_ft is not None and ceiling_ft < 3000):
        return 'MVFR'
    return 'VFR'


def should_include_mesonet2_row(provider, has_standard_fields):
    if provider in ALWAYS_INCLUDE_PROVIDERS:
        return True
    if provider and provider.upper().endswith('DOT'):
        return has_standard_fields
    return False


def parse_rows(ds, stream):
    n = ds.dimensions['recNum'].size

    station_ids = fetch_str_array(ds, 'stationId', n)
    station_names = fetch_str_array(ds, 'stationName', n)
    raw_messages = fetch_str_array(ds, 'rawMessage', n)
    providers = fetch_str_array(ds, 'dataProvider', n) if stream == 'mesonet2' else None

    lons = fetch_num_array(ds, 'longitude', n)
    lats = fetch_num_array(ds, 'latitude', n)
    elevs = fetch_num_array(ds, 'elevation', n)
    obs_times = fetch_num_array(ds, 'observationTime', n)
    recv_times = fetch_num_array(ds, 'receivedTime', n)
    temps = fetch_num_array(ds, 'temperature', n)
    dewps = fetch_num_array(ds, 'dewpoint', n)
    rh = fetch_num_array(ds, 'relHumidity', n)
    wdir = fetch_num_array(ds, 'windDir', n)
    wspd = fetch_num_array(ds, 'windSpeed', n)
    wgst = fetch_num_array(ds, 'windGust', n)
    vis = fetch_num_array(ds, 'visibility', n)
    alt = fetch_num_array(ds, 'altimeter', n)
    sky_layers = fetch_sky_layers(ds, n) if stream == 'hfmetar' else [None] * n

    # Standard-field presence, for the DOT provider gate -- vectorized once,
    # not recomputed per row.
    standard_present = ~(np.isnan(temps) & np.isnan(rh) & np.isnan(wspd) & np.isnan(vis))

    rows = []
    skipped_no_id = 0
    skipped_no_time = 0
    skipped_provider = 0
    for i in range(n):
        station_id = station_ids[i] if station_ids is not None else ''
        if not station_id:
            skipped_no_id += 1
            continue
        obs_dt = epoch_to_dt(obs_times[i])
        if obs_dt is None:
            skipped_no_time += 1
            continue

        if stream == 'mesonet2':
            provider = providers[i] if providers is not None else None
            if not should_include_mesonet2_row(provider, bool(standard_present[i])):
                skipped_provider += 1
                continue
        else:
            provider = 'HFMETAR'

        rows.append({
            'source_feed': stream,
            'data_provider': provider,
            'station_id': station_id,
            'station_name': (station_names[i] or None) if station_names is not None else None,
            'lon': nz(lons[i]),
            'lat': nz(lats[i]),
            'elevation_m': nz(elevs[i]),
            'observation_time': obs_dt,
            'received_time': epoch_to_dt(recv_times[i]),
            'temp_c': k_to_c(nz(temps[i])),
            'dewpoint_c': k_to_c(nz(dewps[i])),
            'relative_humidity': nz(rh[i]),
            'wind_dir': nz(wdir[i]),
            'wind_speed_kts': mps_to_kt(nz(wspd[i])),
            'wind_gust_kts': mps_to_kt(nz(wgst[i])),
            'visibility_sm': m_to_sm(nz(vis[i])),
            'altimeter_hg': pa_to_inhg(nz(alt[i])),
            'raw_message': (raw_messages[i] or None) if raw_messages is not None else None,
            'sky_conditions': sky_layers[i],
        })
    log.info(
        'stream=%s recNum=%d included=%d skipped_no_id=%d skipped_no_time=%d skipped_provider=%d',
        stream, n, len(rows), skipped_no_id, skipped_no_time, skipped_provider
    )
    return rows


def write_rows(conn, rows):
    """Batched via execute_values -- a per-row execute() round trip measured
    at ~37s for 2999 hfmetar rows (network RTT-bound against data2), which
    would be minutes for mesonet2's larger filtered row count. Batching
    cuts this to a handful of multi-row statements.

    De-dup by the ON CONFLICT target key first -- a single execute_values()
    batch can't apply "ON CONFLICT DO UPDATE" to the same target row twice
    (psycopg2.errors.CardinalityViolation), which happens whenever the
    source file itself contains two rows for the same station+time. Found
    2026-11-19: this was discarding the WHOLE file (not just the duplicate
    rows), and worse, cascading into every subsequent file drained on the
    same long-lived connection via drain_spool() -- see the conn.rollback()
    added there alongside this fix. 46 original failures had cascaded into
    425 knock-on failures / 473 discarded files in this log's history."""
    deduped = {}
    for r in rows:
        deduped[(r['source_feed'], r['station_id'], r['observation_time'])] = r
    values = [(
        r['source_feed'], r['station_id'], r['observation_time'], r['received_time'],
        r['temp_c'], r['dewpoint_c'], r['relative_humidity'],
        r['wind_dir'], r['wind_speed_kts'], r['wind_gust_kts'],
        r['visibility_sm'], r['altimeter_hg'], r['raw_message'],
    ) for r in deduped.values()]
    with conn.cursor() as cur:
        execute_values(cur, UPSERT_SQL, values, template=UPSERT_VALUES_TEMPLATE, page_size=1000)
    conn.commit()


def write_metar_rows(conn, rows):
    """hfmetar only -- merge into observations.metar. station_id there is
    VARCHAR(4); every real hfmetar station_id seen so far is already a
    plain 4-char ICAO id (confirmed live, 948/1031 distinct stations match
    observations.airports exactly), but guard defensively rather than let
    a longer/odd id blow up the whole batch. wind_dir/speed/gust are
    INTEGER in this table (vs. REAL in madis_surface_obs) -- round, don't
    truncate. raw_text is NOT NULL on this table; MADIS's own rawMessage
    (a comma-delimited internal format, not WMO bulletin text) is stored
    as-is -- it reads differently than a classic METAR string on click,
    but every structured field (temp/dewpoint/wind/vis/ceiling/altimeter)
    is still correct and that's what the EWMC/Flight-Impacts UI displays
    structured, not the raw string itself."""
    # De-dup by (station_id, observation_time) -- this table's ON CONFLICT
    # target -- same CardinalityViolation/cascading-failure risk and fix
    # as write_rows(); see its docstring for the 2026-11-19 finding.
    deduped = {}
    skipped_long_id = 0
    skipped_no_coords = 0
    for r in rows:
        if len(r['station_id']) > 4:
            skipped_long_id += 1
            continue
        if r['lon'] is None or r['lat'] is None:
            skipped_no_coords += 1
            continue
        ceiling_ft = ceiling_from_sky(r['sky_conditions'])
        flight_category = compute_flight_category(r['visibility_sm'], ceiling_ft)
        sky_json = json.dumps(r['sky_conditions']) if r['sky_conditions'] else None
        wind_dir = round(r['wind_dir']) if r['wind_dir'] is not None else None
        wind_speed_kts = round(r['wind_speed_kts']) if r['wind_speed_kts'] is not None else None
        wind_gust_kts = round(r['wind_gust_kts']) if r['wind_gust_kts'] is not None else None
        deduped[(r['station_id'], r['observation_time'])] = (
            r['station_id'], r['observation_time'], r['raw_message'] or '',
            r['temp_c'], r['dewpoint_c'], wind_dir, wind_speed_kts, wind_gust_kts,
            r['visibility_sm'], r['altimeter_hg'], flight_category,
            sky_json, [],
            r['lon'], r['lat'],
            False,
        )
    values = list(deduped.values())
    if skipped_long_id:
        log.warning('skipped %d hfmetar row(s) with station_id longer than 4 chars', skipped_long_id)
    if skipped_no_coords:
        log.warning('skipped %d hfmetar row(s) with missing lon/lat', skipped_no_coords)
    if not values:
        return
    with conn.cursor() as cur:
        execute_values(cur, UPSERT_METAR_SQL, values, template=UPSERT_METAR_VALUES_TEMPLATE, page_size=1000)
    conn.commit()


def main():
    ap = argparse.ArgumentParser(description='CAP WxCOP MADIS FSL2 surface-obs ingest')
    ap.add_argument('--stream', choices=['hfmetar', 'mesonet2'], required=True)
    ap.add_argument('--file', metavar='PATH', help='Read from a local file instead of stdin (testing/backfill)')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    # --file/--dry-run: unchanged direct-process path, no spool/lock --
    # there's no concurrent pqact invocation to race against here.
    if args.file or args.dry_run:
        try:
            raw = read_input(args.file)
        except Exception:
            log.exception('failed to read input (stream=%s)', args.stream)
            sys.exit(1)

        if not raw:
            log.warning('empty input, nothing to do (stream=%s)', args.stream)
            return

        try:
            ds = open_dataset(raw)
        except Exception:
            log.exception('failed to decode netCDF (stream=%s, %d bytes)', args.stream, len(raw))
            sys.exit(1)

        try:
            rows = parse_rows(ds, args.stream)
        finally:
            ds.close()

        if args.dry_run:
            log.info('dry-run: would write %d rows (stream=%s)', len(rows), args.stream)
            print(f'dry-run: would write {len(rows)} rows (stream={args.stream})')
            return

        if not rows:
            return

        conn = get_connection()
        try:
            ensure_schema(conn)
            write_stations(conn, rows)
            write_rows(conn, rows)
            if args.stream == 'hfmetar':
                write_metar_rows(conn, rows)
        finally:
            conn.close()
        return

    # Live pqact path: durably spool first (never blocks pqact, never
    # drops), then either drain (if we win the lock) or exit immediately
    # (if we don't -- the file is already safe for the active drainer).
    try:
        raw = read_input(None)
    except Exception:
        log.exception('failed to read stdin (stream=%s)', args.stream)
        sys.exit(1)

    if not raw:
        log.warning('empty input, nothing to do (stream=%s)', args.stream)
        return

    try:
        spool_write(args.stream, raw)
    except Exception:
        log.exception('failed to write spool file (stream=%s) -- discarding', args.stream)
        sys.exit(1)

    lock_fh = acquire_drain_lock(args.stream)
    if lock_fh is None:
        log.info('queued stream=%s (another drainer active)', args.stream)
        return

    drain_spool(args.stream, lock_fh)


if __name__ == '__main__':
    main()

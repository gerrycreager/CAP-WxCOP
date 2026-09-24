#!/var/www/cap_winds_app/venv/bin/python3
"""
mef_cluster_outlook.py — ceiling/wind/vis fly-or-no-fly outlook for the
KBAK MEF cluster (the same 9-airport set mission_taf_extract.py and
EWMC's MEF-priority tier use), across RRFS/HRRR/NAM. Produces 3 separate
per-model HTML files (a short combined verdict goes in the email body;
the full hourly grid for each model is its own attachment), saved to
--archive-dir (default ~/CAP/Weather/MEF_cluster) with a run-timestamped
filename and attached to the outgoing email.

Built 2026-09-23 for Gerry's real fly/no-fly calls, after two live runs
found (a) RRFS's ceiling field bug (see docs/rrfs-model-taf.md /
project_rrfs_ceiling_field_bug memory) and (b) a real NAM .idx parsing bug
(composite message numbers like '325.1'/'325.2' for co-located UGRD/VGRD,
silently dropped by naive int() parsing). Both fixes live here.

Why a new script rather than reusing rrfs_model_taf.py/mission_taf_extract.py
directly: those generate single-point TAF *text*, tuned for one station at
a time. This is a 9-point-simultaneous grid tool — the actual product is
"does the WHOLE cluster clear at once," not any one station's forecast.
Reuses their cycle-selection logic (`find_cycle_covering()`, the
RRFS-fetch-and-cache path) rather than duplicating it.

Two modes:
  --auto            Scan the whole available forecast range, report every
                     window >= --min-hours (default 4) where all 9 fields
                     are simultaneously >= --threshold-ft ceiling.
  --window START END [--window START END ...]
                     Evaluate specific windows (ISO 8601 UTC, e.g.
                     2026-09-24T12:00 2026-09-24T22:00) instead of
                     auto-detecting -- for when you already know the
                     operational window and just want the verdict.

Examples:
  mef_cluster_outlook.py --auto
  mef_cluster_outlook.py --window 2026-09-23T18:00 2026-09-23T22:00 \\
                          --window 2026-09-24T12:00 2026-09-24T22:00
  mef_cluster_outlook.py --auto --skip-nam --to someone@example.com

Run manually (not a cron job -- this is an on-demand analysis tool, not a
routine ingest). Takes several minutes: RRFS is ~30-40 NOMADS byte-range
fetches (polite pacing/retry built in, same lesson as rrfs_wind_fetch.py),
HRRR is local/fast, NAM is another ~35 NOMADS fetches (--skip-nam to drop
it for a faster run).

Note on email: needs a per-user msmtp config (~/.msmtprc) since /etc/msmtprc
is deliberately ACL-restricted to root+ldm on this host -- if you get an
auth/config error sending, that's why; ask for a private copy rather than
loosening the system one.
"""
import argparse
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, '/var/www/cap_winds_app')
import numpy as np
import pygrib
import requests
from scipy.spatial import cKDTree

from scripts import rrfs_model_taf as rmt
from scripts import rrfs_wind_fetch as rwf

AIRPORTS = {
    'KBAK': (39.262386, -85.895576, 656),
    'KBFR': (38.84000015, -86.44539642, 727),
    'KCEV': (39.6985015869, -85.129699707, 867),
    'KGEZ': (39.586044, -85.804024, 803),
    'KHLB': (39.342861, -85.258441, 973),
    'KI34': (39.32690048, -85.52249908, 912),
    'KMQJ': (39.843498, -85.8971, 862),
    'KOVO': (39.045601, -85.605301, 757),
    'KSER': (38.923599243164, -85.907402038574, 583),
}
ORDER = ['KBAK', 'KOVO', 'KMQJ', 'KSER', 'KBFR', 'KGEZ', 'KI34', 'KHLB', 'KCEV']

PA_TO_INHG = 1.0 / 3386.39
HRRR_BASE = '/LDM/models/hrrr'
DEFAULT_RECIPIENT = 'gcreager@capnhq.gov'
MSLET_WANTED = {('MSLET', 'mean sea level')}

NAM_BASE = 'https://nomads.ncep.noaa.gov/pub/data/nccf/com/nam/prod'
NAM_WANTED = {
    ('UGRD', '10 m above ground'), ('VGRD', '10 m above ground'),
    ('GUST', 'surface'), ('VIS', 'surface'),
    ('HGT', 'cloud base'), ('PRMSL', 'mean sea level'), ('PRES', 'surface'),
}
NAM_CACHE_DIR = '/tmp/mef_cluster_nam_cache'


def log(msg):
    print(f'{datetime.now(timezone.utc):%H:%M:%S} - {msg}', flush=True)


def altimeter(sp_pa, elev_ft):
    if sp_pa is None:
        return None
    p_inhg = sp_pa * PA_TO_INHG
    h_m = elev_ft * 0.3048
    return p_inhg / ((288.0 - 0.0065 * h_m) / 288.0) ** 5.2561


def wind_dir_speed(u, v):
    if u is None or v is None:
        return None, None
    speed_kt = math.hypot(u, v) * 1.94384
    direction = math.degrees(math.atan2(-u, -v)) % 360
    return (round(direction) if speed_kt >= 1 else None), round(speed_kt, 1)


def find(msgs, short, level_type):
    for m in msgs:
        if m.shortName == short and m.typeOfLevel == level_type:
            return m
    return None


def build_kdtree(lats, lons):
    pts = np.column_stack([lats.ravel(), lons.ravel()])
    return cKDTree(pts), lats.shape


def nearest_indices(tree, shape):
    out = {}
    for icao, (lat, lon, _elev) in AIRPORTS.items():
        _, idx = tree.query([lat, lon])
        out[icao] = np.unravel_index(idx, shape)
    return out


def extract_all(u_msg, v_msg, gust_msg, vis_msg, ceil_msg, mslp_msg, sp_msg, idx_map, valid, result):
    for icao in AIRPORTS:
        i, j = idx_map[icao]
        d, s = wind_dir_speed(u_msg.values[i, j], v_msg.values[i, j])
        gust = round(gust_msg.values[i, j] * 1.94384, 1) if gust_msg else None
        vis = round(min(vis_msg.values[i, j] / 1609.34, 10.0), 1) if vis_msg else None
        ceil_v = ceil_msg.values[i, j] if ceil_msg else None
        ceil_ft = round(ceil_v * 3.28084) if (ceil_v is not None and ceil_v < 1e10) else None
        mslp = round(mslp_msg.values[i, j] / 100.0, 1) if mslp_msg else None
        alt = round(altimeter(sp_msg.values[i, j], AIRPORTS[icao][2]), 2) if sp_msg else None
        result[icao][valid.isoformat()] = dict(wind_dir=d, wind_kt=s, gust_kt=gust,
                                                vis_sm=vis, ceil_ft=ceil_ft, mslp_mb=mslp, altim=alt)


def _rrfs_cycle_fully_posted(date, cyc, max_fhour):
    """Network-only check (no cache) -- is F000 AND the far-out hour both
    actually posted for this cycle right now."""
    for fhr in (0, max_fhour) if max_fhour > 0 else (0,):
        url = (f'{rwf.SOURCE_BASE}/rrfs.{date}/{cyc}/rrfs.t{cyc}z.2dfld.'
               f'3km.f{fhr:03d}.conus.grib2.idx')
        try:
            if requests.head(url, timeout=10).status_code != 200:
                return False
        except requests.RequestException:
            return False
    return True


def find_latest_rrfs_cycle(max_fhour, max_lookback_hours=36):
    """Deliberately NOT rmt.find_cycle_covering() -- that function checks
    the LOCAL CACHE first (correct for rrfs_model_taf.py's own real-time
    TAF use case, where a fully-cached recent cycle is normally still the
    current one). Found live 2026-09-24: for a tool meant to always fetch
    the latest data, that cache-first shortcut is a real footgun -- an
    OLDER cycle that happened to be fully cached from earlier testing (this
    script's own prior runs, same 'modeltaf' cache tag) won over a genuinely
    fresher cycle (18Z) that just hadn't been fetched yet, because the
    cache-first loop returns on the first *cached* hit scanning backward,
    never reaching the network-verification pass that would have found 18Z.
    This function always checks the network, freshest cycle first, and
    ignores what happens to already be on disk for the SELECTION decision
    (the actual fetch step still benefits from cache, via fetch_fhour())."""
    need_synoptic = max_fhour > 17
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cursor = (now - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    oldest = now - timedelta(hours=max_lookback_hours)
    c = cursor
    while c >= oldest:
        if not need_synoptic or c.hour in (0, 6, 12, 18):
            date, cyc = c.strftime('%Y%m%d'), c.strftime('%H')
            if _rrfs_cycle_fully_posted(date, cyc, max_fhour):
                return date, cyc
        c -= timedelta(hours=1)
    return None, None


# ---------------------------------------------------------------------------
# RRFS -- reuses rrfs_model_taf.py's fetch/decode path (includes the
# HGT:cloud-ceiling fix) but NOT its cycle selection (see
# find_latest_rrfs_cycle() above for why). Adds a supplementary MSLET
# fetch (not in rrfs_model_taf's own field set, which doesn't need it for
# TAF text).
# ---------------------------------------------------------------------------
def run_rrfs(max_fhour):
    date, cyc = find_latest_rrfs_cycle(max_fhour)
    if date is None:
        log('RRFS: no ready cycle found for this range')
        return {}, None
    log(f'RRFS: {date} {cyc}z, F000-F{max_fhour:03d}')
    cycle_dt = datetime.strptime(f'{date}{cyc}', '%Y%m%d%H').replace(tzinfo=timezone.utc)
    result = {icao: {} for icao in AIRPORTS}
    tree = None
    idx_map = None
    for fhr in range(0, max_fhour + 1):
        path = None
        for attempt in range(4):
            try:
                path = rmt.fetch_fhour(date, cyc, fhr)
                if path:
                    break
            except Exception as e:
                log(f'RRFS F{fhr:03d} attempt {attempt + 1} error: {e}')
            time.sleep(4)
        if path is None:
            log(f'RRFS F{fhr:03d}: not available, skipping')
            continue
        bands = rmt.load_rrfs_bands(path)
        if not bands:
            log(f'RRFS F{fhr:03d}: empty decode, skipping')
            continue
        try:
            mslp_path = rwf.fetch_and_cache('2dfld', date, cyc, fhr, MSLET_WANTED, 'mef_cluster_mslp')
            mslp_v = None
            if mslp_path:
                gr = pygrib.open(str(mslp_path)); mmsgs = list(gr); gr.close()
                mslp_v = mmsgs[0] if mmsgs else None
        except Exception as e:
            log(f'RRFS F{fhr:03d} MSLET fetch failed: {e}')
            mslp_v = None

        gr = pygrib.open(str(path)); msgs = list(gr); gr.close()
        u_msg = find(msgs, '10u', 'heightAboveGround'); v_msg = find(msgs, '10v', 'heightAboveGround')
        gust_msg = find(msgs, 'gust', 'surface'); vis_msg = find(msgs, 'vis', 'surface')
        ceil_msg = find(msgs, 'gh', 'cloudCeiling') or find(msgs, 'unknown', 'cloudCeiling')
        sp_msg = find(msgs, 'sp', 'surface')
        if tree is None:
            lats, lons = u_msg.latlons()
            tree, shape = build_kdtree(lats, lons)
            idx_map = nearest_indices(tree, shape)
        valid = cycle_dt + timedelta(hours=fhr)
        extract_all(u_msg, v_msg, gust_msg, vis_msg, ceil_msg, mslp_v, sp_msg, idx_map, valid, result)
        if fhr % 5 == 0:
            log(f'RRFS F{fhr:03d} done')
    log('RRFS: complete')
    return result, cycle_dt


# ---------------------------------------------------------------------------
# HRRR -- latest synoptic cycle with local coverage through max_fhour;
# any gap in the middle (a real LDM relay hiccup, seen live 2026-09-23,
# not something LDM will ever backfill) is patched from NOAA's public S3
# archive, same technique as fetch_hrrr_extended.py's F19+ top-up.
# ---------------------------------------------------------------------------
def find_latest_hrrr_synoptic_cycle(max_fhour):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cursor = now.replace(minute=0, second=0, microsecond=0)
    for _ in range(48):
        if cursor.hour in (0, 6, 12, 18):
            d = f"{HRRR_BASE}/hrrr.{cursor:%Y%m%d}/{cursor.hour:02d}z"
            f0 = os.path.join(d, f"hrrr.t{cursor.hour:02d}z.wrfsfcf000.grib2")
            if os.path.isdir(d) and os.path.exists(f0):
                return cursor.strftime('%Y%m%d'), f'{cursor.hour:02d}'
        cursor -= timedelta(hours=1)
    return None, None


def hrrr_s3_fill(date, cyc, fhr):
    S3_BASE = 'https://noaa-hrrr-bdp-pds.s3.amazonaws.com'
    HRRR_WANTED = {
        ('VIS', 'surface'), ('UGRD', '10 m above ground'), ('VGRD', '10 m above ground'),
        ('GUST', 'surface'), ('MSLMA', 'mean sea level'), ('PRES', 'surface'),
        ('HGT', 'cloud ceiling'),
    }
    key = f'hrrr.{date}/conus/hrrr.t{cyc}z.wrfsfcf{fhr:02d}.grib2'
    idx_url, grib_url = f'{S3_BASE}/{key}.idx', f'{S3_BASE}/{key}'
    with requests.Session() as s:
        r = s.get(idx_url, timeout=20)
        r.raise_for_status()
        entries = []
        for line in r.text.strip().splitlines():
            parts = line.split(':')
            if len(parts) < 5:
                continue
            entries.append((parts[0], int(parts[1]), parts[3], parts[4]))
        entries.sort(key=lambda e: e[1])
        head = s.head(grib_url, timeout=20)
        total = int(head.headers['Content-Length'])
        ranges = []
        for i, (mn, off, var, level) in enumerate(entries):
            if (var, level) in HRRR_WANTED:
                end = entries[i + 1][1] - 1 if i + 1 < len(entries) else total - 1
                ranges.append((off, end))
        out_path = f'{NAM_CACHE_DIR}/hrrr_gapfill/hrrr.t{cyc}z.f{fhr:03d}.grib2'
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, 'wb') as out:
            for start, end in ranges:
                resp = s.get(grib_url, headers={'Range': f'bytes={start}-{end}'}, timeout=30)
                out.write(resp.content)
    return out_path


def run_hrrr(max_fhour):
    date, cyc = find_latest_hrrr_synoptic_cycle(max_fhour)
    if date is None:
        log('HRRR: no synoptic cycle with local F000 found')
        return {}, None
    log(f'HRRR: {date} {cyc}z, F000-F{max_fhour:03d} (local, S3-backfilled on gaps)')
    cycle_dt = datetime.strptime(f'{date}{cyc}', '%Y%m%d%H').replace(tzinfo=timezone.utc)
    result = {icao: {} for icao in AIRPORTS}
    tree = None
    idx_map = None
    for fhr in range(0, max_fhour + 1):
        path = f'{HRRR_BASE}/hrrr.{date}/{cyc}z/hrrr.t{cyc}z.wrfsfcf{fhr:03d}.grib2'
        if not os.path.exists(path):
            try:
                path = hrrr_s3_fill(date, cyc, fhr)
                log(f'HRRR F{fhr:03d}: local gap, backfilled from S3')
            except Exception as e:
                log(f'HRRR F{fhr:03d}: not available locally or via S3 ({e}), skipping')
                continue
        try:
            gr = pygrib.open(path); msgs = list(gr); gr.close()
        except Exception as e:
            log(f'HRRR F{fhr:03d}: decode failed ({e}), skipping')
            continue
        u_msg = find(msgs, '10u', 'heightAboveGround'); v_msg = find(msgs, '10v', 'heightAboveGround')
        gust_msg = find(msgs, 'gust', 'surface'); vis_msg = find(msgs, 'vis', 'surface')
        ceil_msg = find(msgs, 'gh', 'cloudCeiling')
        mslp_msg = find(msgs, 'prmsl', 'meanSea') or find(msgs, 'mslma', 'meanSea')
        sp_msg = find(msgs, 'sp', 'surface')
        if tree is None:
            lats, lons = u_msg.latlons()
            tree, shape = build_kdtree(lats, lons)
            idx_map = nearest_indices(tree, shape)
        valid = cycle_dt + timedelta(hours=fhr)
        extract_all(u_msg, v_msg, gust_msg, vis_msg, ceil_msg, mslp_msg, sp_msg, idx_map, valid, result)
        if fhr % 8 == 0:
            log(f'HRRR F{fhr:03d} done')
    log('HRRR: complete')
    return result, cycle_dt


# ---------------------------------------------------------------------------
# NAM -- optional (--with-nam). No true ceiling field, HGT:cloud base used
# as a proxy (documented in the report, not silently treated as equivalent).
# NOMADS only -- S3's noaa-nam-pds mirror was confirmed stale (no files for
# the current date at all) 2026-09-23; NOMADS itself was current.
# ---------------------------------------------------------------------------
def fetch_nam_hour(date, cyc, fhr):
    key = f'nam.{date}/nam.t{cyc}z.awphys{fhr:02d}.tm00.grib2'
    idx_url, grib_url = f'{NAM_BASE}/{key}.idx', f'{NAM_BASE}/{key}'
    out_path = f'{NAM_CACHE_DIR}/nam.t{cyc}z.f{fhr:03d}.grib2'
    if os.path.exists(out_path):
        return out_path
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with requests.Session() as s:
        r = s.get(idx_url, timeout=20)
        if r.status_code != 200:
            return None
        entries = []
        for line in r.text.strip().splitlines():
            parts = line.split(':')
            if len(parts) < 5:
                continue
            # NAM's idx uses composite message numbers ('325.1'/'325.2') for
            # co-located UGRD/VGRD sharing one byte offset -- found live
            # 2026-09-23: int(parts[0]) silently dropped these (ValueError
            # swallowed), so wind never made it into the fetched subset.
            # msgnum is identity-only here, not arithmetic -- keep as string.
            try:
                offset = int(parts[1])
            except ValueError:
                continue
            entries.append((parts[0], offset, parts[3], parts[4]))
        entries.sort(key=lambda e: e[1])
        head = s.head(grib_url, timeout=20)
        if head.status_code != 200:
            return None
        total = int(head.headers['Content-Length'])
        # Entries can share an identical offset (the composite pair above) --
        # the usual "next entry's offset - 1" end-boundary trick breaks for
        # those (gives end < start). Use the next DIFFERENT offset instead.
        distinct = sorted(set(e[1] for e in entries))
        offset_end = {off: (distinct[k + 1] - 1 if k + 1 < len(distinct) else total - 1)
                      for k, off in enumerate(distinct)}
        seen, ranges = set(), []
        for mn, off, var, level in entries:
            if (var, level) in NAM_WANTED:
                r2 = (off, offset_end[off])
                if r2 not in seen:
                    seen.add(r2)
                    ranges.append(r2)
        if not ranges:
            return None
        tmp = out_path + '.tmp'
        with open(tmp, 'wb') as out:
            for start, end in ranges:
                resp = s.get(grib_url, headers={'Range': f'bytes={start}-{end}'}, timeout=30)
                if resp.status_code not in (200, 206):
                    return None
                out.write(resp.content)
        os.rename(tmp, out_path)
    return out_path


def run_nam(max_fhour):
    now = datetime.now(timezone.utc)
    date, cyc = None, None
    for hours_back in range(0, 12):
        t = now - timedelta(hours=hours_back)
        if t.hour in (0, 6, 12, 18):
            code = requests.head(f'{NAM_BASE}/nam.{t:%Y%m%d}/nam.t{t.hour:02d}z.awphys00.tm00.grib2',
                                  timeout=15).status_code
            if code == 200:
                date, cyc = t.strftime('%Y%m%d'), f'{t.hour:02d}'
                break
    if date is None:
        log('NAM: no ready cycle found')
        return {}, None
    log(f'NAM: {date} {cyc}z, F000-F{max_fhour:03d}')
    cycle_dt = datetime.strptime(f'{date}{cyc}', '%Y%m%d%H').replace(tzinfo=timezone.utc)
    result = {icao: {} for icao in AIRPORTS}
    tree = None
    idx_map = None
    for fhr in range(0, max_fhour + 1):
        path = None
        for attempt in range(3):
            try:
                path = fetch_nam_hour(date, cyc, fhr)
                if path:
                    break
            except Exception as e:
                log(f'NAM F{fhr:03d} attempt {attempt + 1} error: {e}')
            time.sleep(6)
        if path is None:
            log(f'NAM F{fhr:03d}: not available, skipping')
            continue
        gr = pygrib.open(path); msgs = list(gr); gr.close()
        u_msg = find(msgs, '10u', 'heightAboveGround'); v_msg = find(msgs, '10v', 'heightAboveGround')
        gust_msg = find(msgs, 'gust', 'surface'); vis_msg = find(msgs, 'vis', 'surface')
        cbase_msg = find(msgs, 'gh', 'cloudBase')
        mslp_msg = find(msgs, 'prmsl', 'meanSea')
        sp_msg = find(msgs, 'sp', 'surface')
        if tree is None:
            lats, lons = u_msg.latlons()
            tree, shape = build_kdtree(lats, lons)
            idx_map = nearest_indices(tree, shape)
        valid = cycle_dt + timedelta(hours=fhr)
        extract_all(u_msg, v_msg, gust_msg, vis_msg, cbase_msg, mslp_msg, sp_msg, idx_map, valid, result)
        if fhr % 6 == 0:
            log(f'NAM F{fhr:03d} done')
        time.sleep(1)
    log('NAM: complete')
    return result, cycle_dt


# ---------------------------------------------------------------------------
# Window analysis
# ---------------------------------------------------------------------------
def find_auto_windows(model_data, threshold_ft, min_hours):
    all_hours = sorted(set.intersection(*[set(model_data[icao].keys()) for icao in AIRPORTS]))
    flags = []
    for h in all_hours:
        ceils = [model_data[icao][h]['ceil_ft'] for icao in AIRPORTS]
        flags.append((h, all(c is None or c >= threshold_ft for c in ceils)))
    windows, start = [], None
    for i, (h, ok) in enumerate(flags):
        if ok and start is None:
            start = h
        elif not ok and start is not None:
            windows.append((start, flags[i - 1][0]))
            start = None
    if start is not None:
        windows.append((start, flags[-1][0]))
    out = []
    for s, e in windows:
        dur = int((datetime.fromisoformat(e) - datetime.fromisoformat(s)).total_seconds() / 3600) + 1
        if dur >= min_hours:
            out.append((s, e, dur))
    return out, all_hours


def window_verdict(model_data, win_start, win_end, threshold_ft):
    hours = [h for h in sorted(model_data['KBAK'].keys())
              if win_start <= datetime.fromisoformat(h) <= win_end]
    if not hours:
        return 'NO DATA', hours
    clear = sum(1 for h in hours
                if all(model_data[icao][h]['ceil_ft'] is None or model_data[icao][h]['ceil_ft'] >= threshold_ft
                       for icao in AIRPORTS))
    total = len(hours)
    if clear == total:
        return f'ALL CLEAR — all {total}h &ge;{threshold_ft}ft at all 9 fields', hours
    if clear == 0:
        return f'NOT CLEAR — 0/{total}h had all 9 fields &ge;{threshold_ft}ft', hours
    return f'PARTIAL — {clear}/{total}h had all 9 fields &ge;{threshold_ft}ft', hours


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
def ceiling_grid_html(model_name, model_data, hours, threshold_ft, label=''):
    if not hours:
        return ''
    rows = []
    for h in hours:
        dt = datetime.fromisoformat(h)
        cells = []
        for icao in ORDER:
            v = model_data[icao].get(h, {})
            c = v.get('ceil_ft')
            if c is None:
                cells.append('<td style="background:#c8e6c9;text-align:center;">CLR</td>')
            elif c >= threshold_ft:
                cells.append(f'<td style="background:#c8e6c9;text-align:center;">{c}</td>')
            else:
                cells.append(f'<td style="background:#ffcdd2;text-align:center;font-weight:bold;">{c}</td>')
        rows.append(f'<tr><td style="font-weight:bold;">{dt:%m/%d %HZ}</td>' + ''.join(cells) + '</tr>')
    header = ''.join(f'<th>{icao}</th>' for icao in ORDER)
    return f'''
    <h4>{model_name}{' — ' + label if label else ''} — Ceiling (ft AGL)</h4>
    <table style="border-collapse:collapse;font:12px monospace;" border="1" cellpadding="3">
    <tr style="background:#eee;"><th>Valid</th>{header}</tr>
    {''.join(rows)}
    </table>
    '''


def model_summary_html(name, data, cycle_dt, threshold_ft, min_hours, windows_arg):
    """Short verdict-only block for the email BODY (not the per-model
    attachment, which carries the full grid)."""
    if not data or not any(data[icao] for icao in AIRPORTS):
        return f'<p><b>{name}</b>: no usable data this run.</p>'
    cyc_label = f' ({cycle_dt:%Y-%m-%d %HZ} cycle)' if cycle_dt else ''
    parts = []
    if windows_arg:
        parts.append(f'<p><b>{name}{cyc_label}</b>:</p><ul>')
        for ws, we in windows_arg:
            verdict, _ = window_verdict(data, ws, we, threshold_ft)
            parts.append(f'<li>{ws:%a %m/%d %HZ}&ndash;{we:%a %m/%d %HZ}: {verdict}</li>')
        parts.append('</ul>')
    else:
        windows, _ = find_auto_windows(data, threshold_ft, min_hours)
        if not windows:
            parts.append(f'<p><b>{name}{cyc_label}</b>: no window &ge;{min_hours}h found.</p>')
        else:
            parts.append(f'<p><b>{name}{cyc_label}</b> — flyable window(s):</p><ul>')
            for s, e, dur in windows:
                flag = ' — <b>6+ hrs, ideal</b>' if dur >= 6 else ''
                parts.append(f'<li>{datetime.fromisoformat(s):%a %m/%d %HZ}&ndash;'
                              f'{datetime.fromisoformat(e):%a %m/%d %HZ} ({dur}h){flag}</li>')
            parts.append('</ul>')
    return ''.join(parts)


def model_attachment_html(name, data, cycle_dt, threshold_ft, min_hours, windows_arg, caveat):
    """Full standalone HTML document for one model -- verdict + full grid(s),
    saved as its own file and attached to the email separately."""
    cyc_label = f'{cycle_dt:%Y-%m-%d %HZ} cycle' if cycle_dt else 'no data'
    body = [f'''
    <html><body style="font-family:sans-serif;">
    <h2>KBAK MEF Cluster — {name} ({cyc_label})</h2>
    <p>Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}. Threshold: ceiling &ge;{threshold_ft}ft AGL
    at KBAK and all 8 outlying fields simultaneously.</p>
    {f"<p>{caveat}</p>" if caveat else ""}
    <hr>
    ''', model_summary_html(name, data, cycle_dt, threshold_ft, min_hours, windows_arg), '<hr>']

    if not data:
        body.append('</body></html>')
        return ''.join(body)

    if windows_arg:
        for ws, we in windows_arg:
            _, hours = window_verdict(data, ws, we, threshold_ft)
            body.append(ceiling_grid_html(name, data, hours, threshold_ft, f'{ws:%m/%d %HZ}-{we:%m/%d %HZ}'))
    else:
        all_hours = (sorted(set.union(*[set(data[icao].keys()) for icao in AIRPORTS]))
                     if any(data[i] for i in AIRPORTS) else [])
        body.append(ceiling_grid_html(name, data, all_hours, threshold_ft))
    body.append('</body></html>')
    return ''.join(body)


MODEL_CAVEATS = {
    'RRFS': 'RRFS is not proven for this use case (fine-grid convection-allowing model output at named GA '
            'airfields is beyond its designed/verified scope) -- treat as experimental guidance, not definitive.',
    'HRRR': 'HRRR is not proven for this use case either, though its dynamical core is more established than '
            "RRFS's current (pre-MPAS) one -- still experimental guidance for named GA airfields, not definitive.",
    'NAM': 'NAM has no true ceiling field -- HGT:cloud base is used as a proxy, not RRFS/HRRR\'s real diagnosed '
           'ceiling height. Included as a coarser, more conventionally-validated cross-check.',
}


def save_attachment(name, html, archive_dir, run_stamp):
    os.makedirs(archive_dir, exist_ok=True)
    path = os.path.join(archive_dir, f'mef_cluster_{name}_{run_stamp}.html')
    with open(path, 'w') as f:
        f.write(html)
    return path


def send_email_with_attachments(summary_html, attachments, recipient, subject):
    """attachments: list of (display_name, file_path) to attach as
    text/html. Uses email.mime for correct multipart/mixed + base64
    encoding -- raw string concatenation (the original single-body
    approach) doesn't extend cleanly to real file attachments."""
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.application import MIMEApplication

    msg = MIMEMultipart('mixed')
    msg['Subject'] = subject
    msg['To'] = recipient
    msg.attach(MIMEText(summary_html, 'html'))

    for display_name, path in attachments:
        with open(path, 'rb') as f:
            part = MIMEApplication(f.read(), _subtype='html')
        part.add_header('Content-Disposition', 'attachment', filename=os.path.basename(path))
        msg.attach(part)

    # msmtp is the real, working mail path on this host -- confirmed live
    # 2026-09-23. auth.py's smtplib-to-localhost:25 approach has no
    # listener here; don't copy that pattern for new scripts.
    p = subprocess.run(['msmtp', '-a', 'gmail', recipient], input=msg.as_string(), capture_output=True, text=True)
    if p.returncode != 0:
        log(f'EMAIL SEND FAILED: {p.stderr}')
        return False
    log(f'EMAIL SENT to {recipient} with {len(attachments)} attachment(s)')
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--auto', action='store_true', help='Auto-detect windows >= --min-hours (default mode)')
    ap.add_argument('--window', nargs=2, action='append', metavar=('START', 'END'),
                     help='ISO 8601 UTC window to evaluate, e.g. 2026-09-24T12:00 2026-09-24T22:00. Repeatable.')
    ap.add_argument('--threshold-ft', type=int, default=2000, help='Ceiling threshold, ft AGL (default 2000)')
    ap.add_argument('--min-hours', type=int, default=4, help='Minimum window length for --auto (default 4)')
    ap.add_argument('--max-fhour', type=int, default=34, help='Max forecast hour to fetch (default 34)')
    ap.add_argument('--skip-nam', action='store_true', help='Skip NAM (runs by default alongside RRFS/HRRR)')
    ap.add_argument('--to', default=DEFAULT_RECIPIENT, help=f'Email recipient (default {DEFAULT_RECIPIENT})')
    ap.add_argument('--no-email', action='store_true', help='Build the attachments but skip sending')
    ap.add_argument('--archive-dir', default=os.path.expanduser('~/CAP/Weather/MEF_cluster'),
                     help='Directory to save the 3 per-model HTML files (default ~/CAP/Weather/MEF_cluster)')
    args = ap.parse_args()

    windows_arg = None
    if args.window:
        windows_arg = [(datetime.fromisoformat(s).replace(tzinfo=timezone.utc),
                         datetime.fromisoformat(e).replace(tzinfo=timezone.utc))
                        for s, e in args.window]

    log('=== MEF cluster outlook starting ===')
    models = {}
    models['RRFS'] = run_rrfs(args.max_fhour)
    models['HRRR'] = run_hrrr(args.max_fhour)
    if not args.skip_nam:
        models['NAM'] = run_nam(args.max_fhour)

    run_stamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%SZ')
    attachments = []
    for name, (data, cycle_dt) in models.items():
        html = model_attachment_html(name, data, cycle_dt, args.threshold_ft, args.min_hours,
                                      windows_arg, MODEL_CAVEATS.get(name, ''))
        path = save_attachment(name, html, args.archive_dir, run_stamp)
        attachments.append((name, path))
        log(f'{name}: saved to {path}')

    summary_html = f'''
    <html><body style="font-family:sans-serif;">
    <h2>KBAK MEF Cluster (9 airports) — Fly/No-Fly Ceiling Outlook</h2>
    <p>Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}. Threshold: ceiling &ge;{args.threshold_ft}ft AGL
    at KBAK and all 8 outlying fields simultaneously. Full per-model hourly grids attached separately
    (RRFS/HRRR{"" if args.skip_nam else "/NAM"}).</p>
    <hr><h3>Summary</h3>
    {''.join(model_summary_html(name, data, cycle_dt, args.threshold_ft, args.min_hours, windows_arg)
             for name, (data, cycle_dt) in models.items())}
    </body></html>
    '''

    if not args.no_email:
        send_email_with_attachments(summary_html, attachments, args.to, 'KBAK MEF Cluster Fly/No-Fly Outlook')

    log('=== Done ===')


if __name__ == '__main__':
    main()

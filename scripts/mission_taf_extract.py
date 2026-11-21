"""
mission_taf_extract.py - Model-driven mission-window TAF generator.

Builds genuine ICAO-coded TAF text for a fixed cluster of airports near
KBAK, for CAP mission-planning purposes -- these are NOT official NWS
TAFs, they're model-derived text meant to be scraped by Gerry's laptop
Mission Execution Forecast (MEF) tool (2026-09-21).

Data source: RRFS 2dfld product via rrfs_wind_fetch.py (NOMADS byte-range
.idx fetch-and-cache), decoded via rasterio + GRIB_ELEMENT tag matching --
same proven pattern as ingest_nam_oconus.py's load_rrfs_bands()/
airport_grid_coords(), reused here rather than cfgrib, since that pattern
was specifically adopted for this exact field set (VIS/GUST/CEIL/etc.) and
already confirmed robust. TCDC (needed here for sky-condition amount, not
used by ingest_nam_oconus.py) confirmed 2026-09-21 to resolve cleanly the
same way (GRIB_ELEMENT='TCDC', unit %, real 0-100 values).

Known fidelity limit, by design, not an oversight: RRFS's 2dfld gives only
ONE cloud base height (CEIL), even though it separately reports low/mid/
high cloud cover fractions (LCDC/MCDC/HCDC) -- there's no per-layer height
for the mid/high layers. Rather than fabricate heights for a fake
multi-layer sky condition group, this generates exactly ONE sky condition
group per valid time, anchored to CEIL's real height and amount-coded from
TCDC (entire-atmosphere total cloud cover). This is the same simplification
most automated/NWP-derived TAF tools make for the same reason.

Target times: the next three occurrences (>=now) of 06Z/14Z/22Z -- CAP's
own mission-window times, not the standard 00/06/12/18Z NWS TAF issuance
schedule. Worst case (now just after 06Z) needs forecast hours out to
~F24, which requires a synoptic (00/06/12/18Z) RRFS cycle -- non-synoptic
hourly cycles only extend to F18 (see project_noaaport_sbn_href_retirement
memory, and the same F18 cap ingest_nam_oconus.py hit for non-synoptic
cycles). find_synoptic_cycle() below handles that.
"""

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import rasterio
import requests
from rasterio.warp import transform as warp_transform

from scripts import rrfs_wind_fetch as rwf

MPS_TO_KTS = 1.94384
M_TO_FT = 3.28084

# Fixed cluster: KBAK + everything within ~65nm Gerry named, 2026-09-21.
# KMJQ/I34 corrected to their real DB idents (KMQJ/KI34) after confirming
# with Gerry -- KMJQ doesn't exist near KBAK (transposition typo).
AIRPORTS = {
    'KBAK': (39.262386, -85.895576),
    'KOVO': (39.045601, -85.605301),
    'KMQJ': (39.843498, -85.897100),
    'KSER': (38.923599, -85.907402),
    'KBFR': (38.840000, -86.445396),
    'KGEZ': (39.586044, -85.804024),
    'KI34': (39.326900, -85.522499),
    'KHLB': (39.342861, -85.258441),
    'KCEV': (39.698502, -85.129700),
}

WANTED_FIELDS = {
    ('UGRD', '10 m above ground'), ('VGRD', '10 m above ground'),
    ('GUST', 'surface'), ('VIS', 'surface'),
    ('HGT', 'cloud ceiling'),
    ('TCDC', 'entire atmosphere (considered as a single layer)'),
    ('CRAIN', 'surface'), ('CSNOW', 'surface'),
    ('CICEP', 'surface'), ('CFRZR', 'surface'),
    ('PRATE', 'surface'),
}
# Real bug found+fixed 2026-09-23 (same one fixed in rrfs_model_taf.py, same
# root cause): RRFS's 2dfld carries two distinct ceiling fields. GRIB_ELEMENT
# CEIL is a genuine model artifact -- at KBAK, 00z 2026-09-23, it alternated
# 1795->3468->1798->3346ft hour to hour while low cloud cover sat at a
# rock-steady 100% the whole time. GRIB_ELEMENT HGT at typeOfLevel='cloud
# ceiling' (a geopotential-height-based ceiling, same file) was smooth over
# the same hours: 1650->1644->1644->1854ft. Switched to HGT; see
# HGT_SHORT_NAME_KEYS below for why this needs its own disambiguation.
HGT_SHORT_NAME_KEYS = {
    '0-CEIL': 'ceil',
}

VIS_TABLE = [
    (0.25, '1/4SM'), (0.5, '1/2SM'), (0.75, '3/4SM'), (1.0, '1SM'),
    (1.5, '1 1/2SM'), (2.0, '2SM'), (3.0, '3SM'), (4.0, '4SM'),
    (5.0, '5SM'), (6.0, '6SM'),
]


# ---------------------------------------------------------------------------
# Target-time / cycle selection
# ---------------------------------------------------------------------------

def next_target_times(now=None):
    """Next three ascending 06Z/14Z/22Z timestamps >= now, rolling across
    midnight/multiple days as needed."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    now = now.replace(second=0, microsecond=0)
    today = now.replace(hour=0, minute=0)
    candidates = []
    for day_offset in (0, 1, 2):
        d = today + timedelta(days=day_offset)
        for hh in (6, 14, 22):
            candidates.append(d.replace(hour=hh))
    candidates = sorted(t for t in candidates if t >= now)
    return candidates[:3]


def _cycle_has_fhour(date, cyc, fhour, domain='conus', resolution='3km'):
    """Like rrfs_wind_fetch._cycle_has_f000(), generalized to an arbitrary
    forecast hour -- a fresh cycle can have F000 posted while a far-out
    hour (e.g. F20) isn't rolled out yet, so F000-only readiness isn't
    enough to trust a cycle covers our actual target times."""
    url = (f'{rwf.SOURCE_BASE}/rrfs.{date}/{cyc}/rrfs.t{cyc}z.2dfld.'
           f'{resolution}.f{fhour:03d}.{domain}.grib2.idx')
    try:
        r = requests.head(url, timeout=10)
        if r.status_code == 404:
            r = requests.get(url, timeout=10, stream=True)
            r.close()
        return r.status_code == 200
    except requests.RequestException:
        return False


def find_cycle_covering(target_times, max_lookback_hours=36, domain='conus', resolution='3km'):
    """Find the most recent RRFS cycle where every target time's specific
    forecast hour is actually posted -- not just F000. Only considers
    synoptic (00/06/12/18Z) cycles when a target exceeds RRFS's 18h
    non-synoptic cap. This can mean stepping back further than the
    'latest' cycle: a fresh 00z cycle 2h old may not have posted its own
    F20 yet, even though an older but more mature 18z cycle already has
    (confirmed live 2026-09-21 -- a 2h-old synoptic cycle had F06 posted
    but not F14/F22)."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    max_hours_out = (max(target_times) - now).total_seconds() / 3600.0
    need_synoptic = max_hours_out > 17

    cursor = (now - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    oldest = now - timedelta(hours=max_lookback_hours)
    while cursor >= oldest:
        if not need_synoptic or cursor.hour in (0, 6, 12, 18):
            date, cyc = cursor.strftime('%Y%m%d'), cursor.strftime('%H')
            fhours = [int(round((t - cursor).total_seconds() / 3600)) for t in target_times]
            if all(f >= 0 for f in fhours) and all(
                _cycle_has_fhour(date, cyc, f, domain, resolution) for f in fhours
            ):
                return date, cyc
        cursor -= timedelta(hours=1)
    return None, None


# ---------------------------------------------------------------------------
# RRFS decode (rasterio + GRIB_ELEMENT, matching ingest_nam_oconus.py)
# ---------------------------------------------------------------------------

def fetch_fhour(date, cyc, fhour):
    return rwf.fetch_and_cache('2dfld', date, cyc, fhour, WANTED_FIELDS,
                                tag='missiontaf', domain='conus', resolution='3km')


def load_rrfs_bands(grib_path: Path) -> dict:
    """Same as ingest_nam_oconus.py's function of the same name -- kept as
    its own copy rather than a cross-script import, matching this
    codebase's convention of small per-script utility duplication (only
    rrfs_wind_fetch.py's fetch mechanism is actually shared). GRIB_ELEMENT
    'HGT' is ambiguous by itself (RRFS's 2dfld has several distinct HGT
    fields) -- disambiguated via GRIB_SHORT_NAME, see HGT_SHORT_NAME_KEYS."""
    arrays = {}
    try:
        with rasterio.open(grib_path) as src:
            for i in range(1, src.count + 1):
                tags = src.tags(i)
                elem = (tags.get('GRIB_ELEMENT') or '').lower()
                if elem == 'hgt':
                    key = HGT_SHORT_NAME_KEYS.get(tags.get('GRIB_SHORT_NAME'))
                    if key is None:
                        continue
                else:
                    key = elem
                if key and key not in arrays:
                    arrays[key] = src.read(i).astype(np.float32)
            arrays['_transform'] = src.transform
            arrays['_crs'] = src.crs
            arrays['_shape'] = (src.height, src.width)
    except Exception as e:
        return {}
    return arrays


def point_rowcol(lat, lon, raster_crs, transform, shape):
    grid_h, grid_w = shape
    try:
        xs, ys = warp_transform('EPSG:4326', raster_crs, [lon], [lat])
    except Exception:
        return None
    inv = ~transform
    col_f, row_f = inv * (xs[0], ys[0])
    row, col = int(round(row_f)), int(round(col_f))
    if 0 <= row < grid_h and 0 <= col < grid_w:
        return row, col
    return None


def safe_val(arr, row, col):
    try:
        v = float(arr[row, col])
        if math.isnan(v) or math.isinf(v) or abs(v) > 1e15:
            return None
        return v
    except Exception:
        return None


def extract_point(bands, lat, lon):
    if not bands:
        return {}
    rc = point_rowcol(lat, lon, bands['_crs'], bands['_transform'], bands['_shape'])
    if rc is None:
        return {}
    row, col = rc
    return {name: safe_val(arr, row, col) for name, arr in bands.items()
            if not name.startswith('_')}


# ---------------------------------------------------------------------------
# TAF group formatting
# ---------------------------------------------------------------------------

def wind_group(u, v, gust):
    if u is None or v is None:
        return None
    spd = math.sqrt(u * u + v * v) * MPS_TO_KTS
    if spd < 1:
        return '00000KT'
    wdir = (270.0 - math.degrees(math.atan2(v, u))) % 360.0
    wdir10 = int(round(wdir / 10.0)) * 10 % 360
    if wdir10 == 0:
        wdir10 = 360
    gust_str = ''
    if gust is not None:
        gust_kts = gust * MPS_TO_KTS
        if gust_kts - spd >= 10:
            gust_str = f'G{int(round(gust_kts)):02d}'
    return f'{wdir10:03d}{int(round(spd)):02d}{gust_str}KT'


def vis_group(vis_m):
    if vis_m is None:
        return None
    sm = vis_m / 1609.344
    if sm >= 6:
        return 'P6SM'
    chosen = VIS_TABLE[0][1]
    for thresh, label in VIS_TABLE:
        if sm >= thresh:
            chosen = label
    return chosen


def sky_group(ceil_m, tcdc_pct):
    """Single-layer sky condition -- see module docstring for why only
    one layer is reportable from RRFS 2dfld's fields. SKC when CEIL is
    undefined (no BKN/OVC layer, matching this codebase's established
    clear-sky convention -- see project_noaaport_sbn_href_retirement
    memory re: RRFS CEIL returning NaN for clear-sky points), otherwise
    BKN/OVC at CEIL's real height, amount from TCDC."""
    if ceil_m is None:
        return 'SKC'
    ceil_ft = ceil_m * M_TO_FT
    hgt_code = f'{int(round(ceil_ft / 100)):03d}'
    amount = 'OVC' if (tcdc_pct is not None and tcdc_pct >= 87.5) else 'BKN'
    return f'{amount}{hgt_code}'


def wx_group(crain, csnow, cicep, cfrzr, prate):
    """Dominant precip type (most hazardous first: freezing rain > ice
    pellets > snow > rain), intensity from PRATE (kg/m^2/s == mm/s)."""
    types = []
    if cfrzr and cfrzr >= 0.5:
        types.append('FZRA')
    if cicep and cicep >= 0.5:
        types.append('PL')
    if csnow and csnow >= 0.5:
        types.append('SN')
    if crain and crain >= 0.5:
        types.append('RA')
    if not types:
        return None
    intensity = ''
    if prate is not None:
        mmhr = prate * 3600.0
        if mmhr < 2.5:
            intensity = '-'
        elif mmhr > 7.6:
            intensity = '+'
    return f'{intensity}{types[0]}'


# ---------------------------------------------------------------------------
# TAF assembly
# ---------------------------------------------------------------------------

def groups_for_point(all_bands, target_times, lat, lon):
    """all_bands: list of (target_time, bands_dict_or_None), one per
    target time, already fetched+decoded once for the whole airport
    cluster. Returns [(t, group_dict_or_None), ...] for this one point."""
    out = []
    for t, bands in all_bands:
        if not bands:
            out.append((t, None))
            continue
        vals = extract_point(bands, lat, lon)
        wind = wind_group(vals.get('ugrd'), vals.get('vgrd'), vals.get('gust'))
        vis = vis_group(vals.get('vis'))
        sky = sky_group(vals.get('ceil'), vals.get('tcdc'))
        wx = wx_group(vals.get('crain'), vals.get('csnow'), vals.get('cicep'),
                      vals.get('cfrzr'), vals.get('prate'))
        out.append((t, {'wind': wind, 'vis': vis, 'sky': sky, 'wx': wx}))
    return out


def format_taf(icao, groups, issue_time):
    valid = [(t, g) for t, g in groups if g is not None]
    if not valid:
        return f'{icao} {issue_time.strftime("%d%H%M")}Z NO DATA='

    first_t, last_t = valid[0][0], valid[-1][0]
    header = (f'{icao} {issue_time.strftime("%d%H%M")}Z '
              f'{first_t.strftime("%d%H")}/{last_t.strftime("%d%H")}')

    lines = []
    for i, (t, g) in enumerate(valid):
        parts = [g['wind'] or '/////KT', g['vis'] or '////']
        if g['wx']:
            parts.append(g['wx'])
        parts.append(g['sky'] or 'SKC')
        line = ' '.join(parts)
        lines.append(f'{header} {line}' if i == 0 else f'     FM{t.strftime("%d%H%M")} {line}')

    return '\n'.join(lines) + '='


def generate_mission_tafs(now=None):
    """Returns (cycle_date, cycle_cyc, target_times, {icao: taf_text}).
    Fetches+decodes each target time's RRFS file exactly ONCE for the
    whole airport cluster (all 9 airports share the same CONUS grid file
    per forecast hour) -- point extraction per airport is cheap, the
    fetch+rasterio decode is not, so this matters if the page gets
    scraped repeatedly."""
    target_times = next_target_times(now)
    date, cyc = find_cycle_covering(target_times)
    if not date:
        return None, None, target_times, {
            icao: f'{icao} NO RRFS CYCLE AVAILABLE=' for icao in AIRPORTS
        }

    cycle_dt = datetime.strptime(f'{date}{cyc}', '%Y%m%d%H')
    all_bands = []
    for t in target_times:
        fhour = int(round((t - cycle_dt).total_seconds() / 3600))
        grib_path = fetch_fhour(date, cyc, fhour)
        bands = load_rrfs_bands(grib_path) if grib_path else {}
        all_bands.append((t, bands or None))

    issue_time = datetime.now(timezone.utc).replace(tzinfo=None, second=0, microsecond=0)
    tafs = {}
    for icao, (lat, lon) in AIRPORTS.items():
        groups = groups_for_point(all_bands, target_times, lat, lon)
        tafs[icao] = format_taf(icao, groups, issue_time)
    return date, cyc, target_times, tafs

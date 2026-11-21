"""
rrfs_model_taf.py - Model-derived TAF generator (RRFS primary, HRRR fallback).

Built 2026-09-22 against the spec in
~gerry/CAP/RPP/MEF_dev/RRFS_Model_TAF_Spec.20260920_2300-C.md (another
Claude instance, on Gerry's laptop). Feeds Gerry's laptop-side Mission
Execution Forecast (MEF) tool via mission_taf_api.py's model-TAF routes --
NOT an official NWS/USAF product, model guidance only.

Differs from yesterday's scripts/mission_taf_extract.py (KBAK-cluster,
single sky layer, fixed 9-airport list, RRFS-only): this is the general,
any-station version -- multi-layer clouds, BECMG trend groups, altimeter,
RRFS-primary/HRRR-fallback. mission_taf_extract.py's fixed-cluster,
06/14/22Z product stays as its own separate thing; this doesn't replace it.

Grammar target: USAF FM51 / AFMAN 15-124 (per spec, "TAF text rules").
Output is deliberately round-trippable through
~gerry/CAP/RPP/MEF_dev/taf_usaf.py's parser (that module's own acceptance
test, spec section 7.8): the ceiling this module puts in JSON
(ceiling_ft_by_hour) must equal what taf_usaf.py's Cond.ceiling_ft computes
from the SAME hour's generated taf_text.

Multi-layer sky condition (the real gap flagged in yesterday's
mission_taf_extract.py -- see project_mission_taf_kbak_cluster memory):
RRFS's 2dfld carries per-band cloud cover fractions (LCDC/MCDC/HCDC -- low/
mid/high, STANDARDIZED altitude bands in the GRIB2 tables, not model-
diagnosed heights) plus one real diagnosed ceiling height. This module
assigns each band's okta-derived amount (FEW/SCT/BKN/OVC) to a
representative band height, then overrides whichever band is actually
ceiling-defining with the real ceiling height -- band midpoints for the
other layers, a real model height for the one that matters most. Honest
about the difference: banded layers use a placeholder height, only the
ceiling layer's height is model-diagnosed.

Ceiling field note (2026-09-23): fetched as GRIB_ELEMENT=HGT at
typeOfLevel='cloud ceiling', NOT GRIB_ELEMENT=CEIL -- RRFS's 2dfld carries
both, and CEIL is a confirmed artifact (alternates wildly hour to hour even
under a rock-steady cloud fraction; see docs/rrfs-model-taf.md and
project_rrfs_ceiling_field_bug memory for the full story). HGT is
ambiguous by itself (several distinct HGT fields exist in the same file),
disambiguated via GRIB_SHORT_NAME -- see HGT_SHORT_NAME_KEYS below.
"""

import math
import os
import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import rasterio
from rasterio.warp import transform as warp_transform

from scripts import rrfs_wind_fetch as rwf

MPS_TO_KTS = 1.94384
M_TO_FT = 3.28084
PA_TO_INHG = 1.0 / 3386.39

# ---------------------------------------------------------------------------
# RRFS field set
# ---------------------------------------------------------------------------
RRFS_WANTED_FIELDS = {
    ('UGRD', '10 m above ground'), ('VGRD', '10 m above ground'),
    ('GUST', 'surface'), ('VIS', 'surface'),
    ('HGT', 'cloud ceiling'),
    ('LCDC', 'low cloud layer'), ('MCDC', 'middle cloud layer'), ('HCDC', 'high cloud layer'),
    ('CRAIN', 'surface'), ('CSNOW', 'surface'), ('CICEP', 'surface'), ('CFRZR', 'surface'),
    ('PRATE', 'surface'),
    ('TMP', '2 m above ground'), ('DPT', '2 m above ground'),
    ('PRES', 'surface'),
}
# Real bug found and fixed 2026-09-23 (a KBAK hourly-comparison request from
# Gerry surfaced it): RRFS's 2dfld carries TWO distinct ceiling fields --
# GRIB_ELEMENT=CEIL (a separate diagnostic) and GRIB_ELEMENT=HGT at
# typeOfLevel='cloud ceiling' (a geopotential-height-based ceiling). CEIL
# was the one fetched here and is a genuine model artifact: at KBAK,
# 2026-09-23 00z cycle, it alternated 1795->3468->1798->3346->1949ft
# hour to hour while LCDC (low cloud cover fraction) sat at a rock-steady
# 100% the whole time -- physically impossible for a real ceiling under an
# unbroken overcast layer. HGT:cloud ceiling from the SAME file, SAME
# hours: 1650->1644->1644->1854->1831ft -- smooth, consistent with the
# steady cloud fraction. Switched to HGT; see load_rrfs_bands() below for
# why this needs its own disambiguation.

# Standard low/mid/high band boundaries (AGL feet), representative
# (midpoint) heights used only when that band isn't the ceiling layer.
BANDS = {
    'low':  {'range_ft': (0, 6500),     'mid_ft': 2000},
    'mid':  {'range_ft': (6500, 20000), 'mid_ft': 12000},
    'high': {'range_ft': (20000, 40000), 'mid_ft': 30000},
}


def find_ready_cycle():
    return rwf.find_ready_cycle(product='2dfld', domain='conus', resolution='3km')


def _cache_has_fhour(date, cyc, fhour, domain='conus', resolution='3km', tag='modeltaf'):
    return rwf._cache_path('2dfld', date, cyc, fhour, tag, domain, resolution).exists()


def _cache_covers(date, cyc, max_fhour, domain='conus', resolution='3km', tag='modeltaf'):
    """True if every forecast hour 0..max_fhour for this cycle is already
    on disk -- zero network calls."""
    return all(_cache_has_fhour(date, cyc, fh, domain, resolution, tag) for fh in range(0, max_fhour + 1))


def find_cycle_covering(max_fhour, max_lookback_hours=36):
    """Same logic as mission_taf_extract.py's find_cycle_covering() (built
    2026-09-21 for the KBAK-cluster generator, same underlying gap): RRFS
    hourly (non-synoptic) cycles only extend to F18; a 30-hour MEF window
    always needs a synoptic (00/06/12/18Z) cycle, and even then must
    verify the SPECIFIC far-out hour is actually posted yet, not just
    F000 -- a fresh cycle's F000 can be ready while F28-30 isn't rolled
    out yet. Duplicated here rather than imported (see the module-
    duplication convention already established across this app's RRFS
    scripts -- only rrfs_wind_fetch.py's fetch mechanism is actually
    shared).

    Checks the LOCAL CACHE first, before any network call. Found live
    2026-09-22: this function always re-verified availability over the
    network and chased the freshest possible cycle, even when an older
    cycle was already fully cached locally (e.g. every hour of an 18z
    cycle out to F030, from earlier testing) -- meaning a request that
    could have been served entirely from disk instead triggered fresh
    NOMADS probes and fetches, contributing to real Akamai/NOMADS rate-
    limiting. Only falls through to network probing if no cached cycle
    already covers the request."""
    need_synoptic = max_fhour > 17
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cursor = (now - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    oldest = now - timedelta(hours=max_lookback_hours)

    c = cursor
    while c >= oldest:
        if not need_synoptic or c.hour in (0, 6, 12, 18):
            date, cyc = c.strftime('%Y%m%d'), c.strftime('%H')
            if _cache_covers(date, cyc, max_fhour):
                return date, cyc
        c -= timedelta(hours=1)

    c = cursor
    while c >= oldest:
        if not need_synoptic or c.hour in (0, 6, 12, 18):
            date, cyc = c.strftime('%Y%m%d'), c.strftime('%H')
            if rwf._cycle_has_f000('2dfld', date, cyc, domain='conus', resolution='3km'):
                # F000 ready is necessary but not sufficient for a
                # far-out request -- verify the actual farthest hour too.
                if max_fhour == 0 or _cycle_has_fhour(date, cyc, max_fhour):
                    return date, cyc
        c -= timedelta(hours=1)
    return None, None


def _cycle_has_fhour(date, cyc, fhour, domain='conus', resolution='3km'):
    import requests
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


def fetch_fhour(date, cyc, fhour):
    return rwf.fetch_and_cache('2dfld', date, cyc, fhour, RRFS_WANTED_FIELDS,
                                tag='modeltaf', domain='conus', resolution='3km')


#  GRIB_ELEMENT='HGT' is ambiguous by itself -- RRFS's 2dfld carries several
#  distinct HGT fields (cloud base, cloud ceiling, cloud top, surface,
#  tropopause, ...) that would silently collide under one 'hgt' dict key
#  (load_rrfs_bands() used to key purely by GRIB_ELEMENT, fine for every
#  other field here since each is fetched at exactly one level, but not
#  safe for HGT once more than one HGT level is in play). GRIB_SHORT_NAME
#  disambiguates them cleanly (confirmed live 2026-09-23): '0-CEIL' for
#  cloud ceiling, '0-CBL' for cloud base, '0-CTL' for cloud top. Only the
#  ceiling one is fetched today, mapped back to the 'ceil' key the rest of
#  this module (derive_sky()) already expects -- add an entry here first if
#  cloud base/top ever get added to RRFS_WANTED_FIELDS.
HGT_SHORT_NAME_KEYS = {
    '0-CEIL': 'ceil',
}


def load_rrfs_bands(grib_path: Path) -> dict:
    """Same rasterio+GRIB_ELEMENT pattern as ingest_nam_oconus.py /
    mission_taf_extract.py. LCDC/MCDC/HCDC are each a single, unambiguous
    element/level pair in RRFS's 2dfld (unlike TCDC, which has multiple
    level-typed variants -- not used here, deliberately: LCDC/MCDC/HCDC
    give what's needed directly without disambiguating TCDC's variants).
    HGT is the one element that needs GRIB_SHORT_NAME disambiguation --
    see HGT_SHORT_NAME_KEYS."""
    arrays = {}
    try:
        with rasterio.open(grib_path) as src:
            for i in range(1, src.count + 1):
                tags = src.tags(i)
                elem = (tags.get('GRIB_ELEMENT') or '').lower()
                if elem == 'hgt':
                    key = HGT_SHORT_NAME_KEYS.get(tags.get('GRIB_SHORT_NAME'))
                    if key is None:
                        continue  # an HGT level we don't have a mapping for yet
                else:
                    key = elem
                if key and key not in arrays:
                    arrays[key] = src.read(i).astype(np.float32)
            arrays['_transform'] = src.transform
            arrays['_crs'] = src.crs
            arrays['_shape'] = (src.height, src.width)
    except Exception:
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
# Multi-layer sky condition
# ---------------------------------------------------------------------------
def _amount_code(pct):
    """Okta-based amount from a coverage percentage, or None if < 1 okta
    (not reportable)."""
    if pct is None:
        return None
    if pct < 12.5:
        return None
    if pct < 37.5:
        return 'FEW'
    if pct < 62.5:
        return 'SCT'
    if pct < 87.5:
        return 'BKN'
    return 'OVC'


PLACEHOLDER_SUPPRESS_FT = 10000  # per Gerry (2026-09-22): "TAFs rarely have
    # really precise ceiling or cloud height values... anything over
    # 10,000 ft, or certainly over 25,000 ft should be CLR." The mid/high
    # BANDS heights below are fabricated placeholders (there's no per-layer
    # height field for them, only a coverage fraction -- see module
    # docstring), and reporting them with exact-looking precision
    # (BKN330, FEW300, etc, which dominated nearly every generated line)
    # is false precision for data that was never a real diagnosed height
    # in the first place. Only the CEIL-assigned layer's height is real
    # model output regardless of altitude; placeholder heights above this
    # threshold are dropped rather than reported.


def derive_sky(vals):
    """Returns list of (cover, height_ft) tuples, lowest first, using
    LCDC/MCDC/HCDC for amount and CEIL for the real height of whichever
    band is ceiling-defining (first BKN/OVC band, low to high). Bands that
    round to no reportable coverage are omitted. A non-ceiling-defining
    layer whose height is a fabricated placeholder (not real CEIL data)
    above PLACEHOLDER_SUPPRESS_FT is also omitted -- see that constant's
    comment. Returns [] for clear sky."""
    band_pct = {
        'low': vals.get('lcdc'),
        'mid': vals.get('mcdc'),
        'high': vals.get('hcdc'),
    }
    ceil_ft = vals.get('ceil')
    ceil_ft = None if ceil_ft is None else ceil_ft * M_TO_FT
    if ceil_ft is not None and ceil_ft > 25000:
        # Real diagnosed height, but per Gerry: "certainly over 25,000 ft
        # should be CLR" -- a ceiling that high has zero operational
        # relevance for CAP/GA ops, and a 3km grid cell's precise value up
        # there isn't trustworthy enough to report as a hard number anyway.
        ceil_ft = None

    layers = []
    ceiling_assigned = False
    for band in ('low', 'mid', 'high'):
        amount = _amount_code(band_pct[band])
        if amount is None:
            continue
        if not ceiling_assigned and amount in ('BKN', 'OVC') and ceil_ft is not None:
            height = ceil_ft
            ceiling_assigned = True
        else:
            height = BANDS[band]['mid_ft']
            if height > PLACEHOLDER_SUPPRESS_FT:
                continue  # fabricated placeholder, too high to report meaningfully
        layers.append((amount, height))

    # If CEIL is defined but no band's coverage rounded to BKN/OVC (can
    # happen at coarse okta boundaries), still report it as a BKN layer at
    # the real height -- CEIL is model-diagnosed and shouldn't be silently
    # dropped just because the coverage-fraction fields rounded down.
    if ceil_ft is not None and not ceiling_assigned:
        layers.append(('BKN', ceil_ft))

    layers.sort(key=lambda x: x[1])
    return layers


# ---------------------------------------------------------------------------
# Altimeter (inverse of taf_usaf.py's station_pressure_inhg, same constants
# for consistency -- see that module's docstring)
# ---------------------------------------------------------------------------
def altimeter_from_surface_pressure(surface_pressure_pa, elev_ft):
    """Model surface pressure (Pa) + station elevation -> altimeter (inHg).
    Per the spec: use station elevation, not model sea-level pressure, so
    PA/DA computed downstream (taf_usaf.py) behave like real METAR
    altimeter. Does not correct for RRFS grid-point terrain elevation vs.
    the real station's -- a known, accepted simplification at 3km
    resolution (spec section 8)."""
    if surface_pressure_pa is None:
        return None
    p_inhg = surface_pressure_pa * PA_TO_INHG
    h_m = elev_ft * 0.3048
    return p_inhg / ((288.0 - 0.0065 * h_m) / 288.0) ** 5.2561


# ---------------------------------------------------------------------------
# Weather group (matches mission_taf_extract.py's wx_group logic)
# ---------------------------------------------------------------------------
def wx_group(vals):
    types = []
    if vals.get('cfrzr') and vals['cfrzr'] >= 0.5:
        types.append('FZRA')
    if vals.get('cicep') and vals['cicep'] >= 0.5:
        types.append('PL')
    if vals.get('csnow') and vals['csnow'] >= 0.5:
        types.append('SN')
    if vals.get('crain') and vals['crain'] >= 0.5:
        types.append('RA')
    if not types:
        return None
    intensity = ''
    prate = vals.get('prate')
    if prate is not None:
        mmhr = prate * 3600.0
        if mmhr < 2.5:
            intensity = '-'
        elif mmhr > 7.6:
            intensity = '+'
    return f'{intensity}{types[0]}'


# ---------------------------------------------------------------------------
# Hourly condition assembly
# ---------------------------------------------------------------------------
VRB_THRESHOLD_KT = 3  # spec section 8, "Wind": VRB at/below this speed


def hourly_condition(vals, elev_ft):
    u, v, gust = vals.get('ugrd'), vals.get('vgrd'), vals.get('gust')
    wind_dir = wind_kt = gust_kt = None
    wind_var = False
    if u is not None and v is not None:
        spd = math.sqrt(u * u + v * v) * MPS_TO_KTS
        wind_kt = int(round(spd))
        if wind_kt <= VRB_THRESHOLD_KT:
            wind_var = True
        else:
            wind_dir = int(round((270.0 - math.degrees(math.atan2(v, u))) % 360.0 / 10.0)) * 10 % 360
            if wind_dir == 0:
                wind_dir = 360
        if gust is not None:
            gk = gust * MPS_TO_KTS
            if gk - spd >= 5:
                gust_kt = int(round(gk))

    vis_m = vals.get('vis')
    clouds = derive_sky(vals)
    weather = wx_group(vals)
    altim = altimeter_from_surface_pressure(vals.get('pres'), elev_ft)

    return {
        'wind_dir': wind_dir, 'wind_kt': wind_kt, 'gust_kt': gust_kt, 'wind_var': wind_var,
        'vis_m': vis_m, 'weather': weather, 'clouds': clouds,
        'altimeter_inhg': altim,
        'temp_c': vals.get('tmp'), 'dewpoint_c': vals.get('dpt'),
        'ceiling_ft': next((h for c, h in clouds if c in ('BKN', 'OVC')), None),
    }


# ---------------------------------------------------------------------------
# HRRR fallback (only used when RRFS has no ready cycle -- per the spec,
# "RRFS is primary; HRRR is a fallback only while it exists"). Single-layer
# sky only, unlike RRFS's multi-layer derivation above -- HRRR's wrfsfc
# field set as used elsewhere in this app (ingest_model_site_wx.py's
# FIELD_SPECS) has no verified low/mid/high cloud-fraction fields, and
# HRRR is explicitly the lower-priority path, so this doesn't chase down
# unverified HRRR field names for a fallback. CEIL alone -> OVC when
# defined (same convention the old NAM-based Flight Ops Impacts pipeline
# already uses for ceiling without amount granularity), else SKC.
# ---------------------------------------------------------------------------
HRRR_BASE = '/LDM/models/hrrr'

HRRR_FIELD_SPECS = [
    ('2t',    'heightAboveGround', 2,  'tmp'),
    ('2d',    'heightAboveGround', 2,  'dpt'),
    ('10u',   'heightAboveGround', 10, 'ugrd'),
    ('10v',   'heightAboveGround', 10, 'vgrd'),
    ('gust',  'surface',           0,  'gust'),
    ('prate', 'surface',           0,  'prate'),
    ('crain', 'surface',           0,  'crain'),
    ('csnow', 'surface',           0,  'csnow'),
    ('cicep', 'surface',           0,  'cicep'),
    ('cfrzr', 'surface',           0,  'cfrzr'),
    ('vis',   'surface',           0,  'vis'),
    ('sp',    'surface',           0,  'pres'),
]
# Cloud fields have their own typeOfLevel per field (no `level` kwarg,
# unlike HRRR_FIELD_SPECS above) -- (shortName, typeOfLevel, dict_key).
# Verified live 2026-09-22 against a real hrrr.t18z file: ceiling height
# resolves as shortName='gh' at typeOfLevel='cloudCeiling', NOT
# shortName='ceil' as ingest_model_site_wx.py's docstring assumed (that
# assumption was never actually exercised against a real file for this
# purpose) -- and lcc/mcc/hcc (low/mid/high cloud cover, %) are present,
# so HRRR gets the same multi-layer derive_sky() as RRFS, not a fallback
# to single-layer.
HRRR_CLOUD_SPECS = [
    ('gh',  'cloudCeiling',      'ceil'),
    ('lcc', 'lowCloudLayer',     'lcdc'),
    ('mcc', 'middleCloudLayer',  'mcdc'),
    ('hcc', 'highCloudLayer',    'hcdc'),
]


def find_latest_hrrr_cycle():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for hours_back in range(0, 7):
        t = now - timedelta(hours=hours_back)
        d = f"{HRRR_BASE}/hrrr.{t.strftime('%Y%m%d')}/{t.hour:02d}z"
        f0 = os.path.join(d, f"hrrr.t{t.hour:02d}z.wrfsfcf000.grib2")
        if os.path.isdir(d) and os.path.exists(f0):
            return datetime(t.year, t.month, t.day, t.hour), d
    return None, None


def _nearest_val(msg, lat, lon):
    lats, lons = msg.latlons()
    dist2 = (lats - lat) ** 2 + (lons - lon) ** 2
    idx = np.unravel_index(np.argmin(dist2), dist2.shape)
    v = float(msg.values[idx])
    return None if (np.ma.is_masked(v) or math.isnan(v)) else v


def hrrr_hourly_condition(fpath, lat, lon, elev_ft):
    """Same return shape as hourly_condition(), reusing derive_sky() with
    HRRR's own cloud-cover field names -- real multi-layer, matching RRFS."""
    import pygrib
    vals = {}
    try:
        grbs = pygrib.open(fpath)
        for shortname, level_type, level, key in HRRR_FIELD_SPECS:
            # Per-field try/except, not one around the whole loop: fields
            # like PRATE legitimately have no data at F000 (accumulated/
            # rate field, nothing to report at analysis time -- same
            # pattern as APCP at RRFS F000, found in an earlier phase) and
            # pygrib.select() raises rather than returning empty. One
            # missing field must not abort every other field's extraction.
            try:
                msgs = grbs.select(shortName=shortname, typeOfLevel=level_type, level=level)
            except Exception:
                continue
            if msgs:
                vals[key] = _nearest_val(msgs[0], lat, lon)
        for shortname, level_type, key in HRRR_CLOUD_SPECS:
            try:
                msgs = grbs.select(shortName=shortname, typeOfLevel=level_type)
            except Exception:
                continue
            if msgs:
                # ceil (gh, gpm) needs the same meters convention derive_sky()
                # expects from RRFS's CEIL; lcdc/mcdc/hcdc are already %.
                vals[key] = _nearest_val(msgs[0], lat, lon)
        grbs.close()
    except Exception:
        return None

    wind_dir = wind_kt = gust_kt = None
    wind_var = False
    u, v_ = vals.get('ugrd'), vals.get('vgrd')
    if u is not None and v_ is not None:
        spd = math.sqrt(u * u + v_ * v_) * MPS_TO_KTS
        wind_kt = int(round(spd))
        if wind_kt <= VRB_THRESHOLD_KT:
            wind_var = True
        else:
            wind_dir = int(round((270.0 - math.degrees(math.atan2(v_, u))) % 360.0 / 10.0)) * 10 % 360
            if wind_dir == 0:
                wind_dir = 360
        gust = vals.get('gust')
        if gust is not None:
            gk = gust * MPS_TO_KTS
            if gk - spd >= 5:
                gust_kt = int(round(gk))

    clouds = derive_sky(vals)
    ceil_ft = next((h for c, h in clouds if c in ('BKN', 'OVC')), None)
    altim = altimeter_from_surface_pressure(vals.get('pres'), elev_ft)

    # pygrib returns TMP/DPT in native GRIB units (Kelvin) -- unlike
    # rasterio/GDAL's GRIB driver, which auto-converts these to Celsius
    # (confirmed live 2026-09-22: GRIB_UNIT tag reads '[C]' for RRFS's
    # TMP/DPT via load_rrfs_bands()). Only the HRRR/pygrib path needs this.
    tmp_c = vals.get('tmp')
    dpt_c = vals.get('dpt')
    tmp_c = None if tmp_c is None else tmp_c - 273.15
    dpt_c = None if dpt_c is None else dpt_c - 273.15

    return {
        'wind_dir': wind_dir, 'wind_kt': wind_kt, 'gust_kt': gust_kt, 'wind_var': wind_var,
        'vis_m': vals.get('vis'), 'weather': wx_group(vals), 'clouds': clouds,
        'altimeter_inhg': altim,
        'temp_c': tmp_c, 'dewpoint_c': dpt_c,
        'ceiling_ft': ceil_ft,
    }


MAX_FETCH_WORKERS = 4  # ProcessPoolExecutor, not threads -- confirmed live
                       # 2026-09-22 that decode (rasterio reading 15 bands)
                       # takes ~3s/file and is the real bottleneck for warm-
                       # cache data, not the NOMADS fetch itself.
                       # ThreadPoolExecutor barely helped (CPU-bound decode
                       # doesn't parallelize under the GIL); real
                       # parallelism needs separate processes ('spawn', not
                       # fork -- same reason ingest_model_winds.py's RRFS
                       # worker does: avoid fork()-from-multithreaded-mod_
                       # wsgi-worker hazards). BUT: originally set to 8-12,
                       # which triggered real Akamai/NOMADS rate-limiting
                       # (confirmed live: HTTP 302 on simple HEAD requests,
                       # not just our heavy byte-range fetches) after
                       # sustained high request volume across this whole
                       # session's testing. Async job-queue (see
                       # model_taf_api.py's /batch/start) already removes
                       # the timeout pressure that motivated high
                       # concurrency in the first place -- this number is
                       # now about being a considerate NOMADS client, not
                       # about beating a 60s deadline. Lower is safer;
                       # raise only with real evidence it's needed.
MP_CTX = mp.get_context('spawn')


def _rrfs_one_hour(args):
    date, cyc, cycle_dt, fhour, lat, lon, elev_ft = args
    path = fetch_fhour(date, cyc, fhour)
    if not path:
        return None
    bands = load_rrfs_bands(path)
    if not bands:
        return None
    vals = extract_point(bands, lat, lon)
    cond = hourly_condition(vals, elev_ft)
    return (cycle_dt + timedelta(hours=fhour), cond)


def _hrrr_one_hour(args):
    cycle_dt, cycle_dir, fhour, lat, lon, elev_ft = args
    fpath = os.path.join(cycle_dir, f"hrrr.t{cycle_dt.hour:02d}z.wrfsfcf{fhour:03d}.grib2")
    if not os.path.exists(fpath):
        return None
    cond = hrrr_hourly_condition(fpath, lat, lon, elev_ft)
    return (cycle_dt + timedelta(hours=fhour), cond) if cond else None


_POOL = None  # lazy, persistent across requests within one WSGI worker
              # process -- spawning a fresh ProcessPoolExecutor (and paying
              # rasterio/numpy's cold-import cost in each new worker) on
              # every single request was most of the remaining wall-clock
              # cost after switching off threads (measured live 2026-09-22:
              # 27s for 19 already-cached hours, far more than 19/8*3s of
              # actual decode time). One pool per WSGI worker process,
              # reused for every request that worker handles.


def _get_pool():
    global _POOL
    if _POOL is None:
        _POOL = ProcessPoolExecutor(max_workers=MAX_FETCH_WORKERS, mp_context=MP_CTX)
    return _POOL


def get_hourly_conditions(lat, lon, elev_ft, max_fhour=18):
    """Try RRFS first; fall back to HRRR only if RRFS has no ready cycle at
    all (spec: "RRFS is primary; HRRR is a fallback only while it
    exists"). Returns (model_name, cycle_time, [(valid_time, cond), ...]).
    Fetches all forecast hours concurrently -- fetch_and_cache() already
    per-key-locks in rrfs_wind_fetch.py, so parallel fhours (different
    cache keys) is safe; rasterio/pygrib opens of different files are
    independent reads."""
    ex = _get_pool()
    # Graceful degradation, per Gerry (2026-09-22): "If we can't do 30 hrs
    # we can at least do 24 from the latest run." Try the requested
    # duration first; if no cycle (cached or network) covers it, fall back
    # to a shorter window rather than dropping to HRRR (or failing
    # outright) just because the last few hours of a long window aren't
    # posted yet.
    candidates = [max_fhour]
    if max_fhour > 24:
        candidates.append(24)
    for fh in candidates:
        date, cyc = find_cycle_covering(fh)
        if date:
            cycle_dt = datetime.strptime(f'{date}{cyc}', '%Y%m%d%H')
            jobs = [(date, cyc, cycle_dt, h, lat, lon, elev_ft) for h in range(0, fh + 1)]
            results = list(ex.map(_rrfs_one_hour, jobs))
            hourly = sorted((r for r in results if r is not None), key=lambda x: x[0])
            if hourly:
                return 'RRFS', cycle_dt, hourly

    cycle_dt, cycle_dir = find_latest_hrrr_cycle()
    if not cycle_dt:
        return None, None, []
    jobs = [(cycle_dt, cycle_dir, fh, lat, lon, elev_ft) for fh in range(0, min(max_fhour, 18) + 1)]
    results = list(ex.map(_hrrr_one_hour, jobs))
    hourly = sorted((r for r in results if r is not None), key=lambda x: x[0])
    return ('HRRR', cycle_dt, hourly) if hourly else (None, None, [])


# ---------------------------------------------------------------------------
# TAF text assembly (USAF grammar per spec section 7)
# ---------------------------------------------------------------------------
def _vis_group_text(vis_m):
    """Meters, rounded down to a reportable value, per spec rule 2. RRFS
    VIS is already in meters; snap down to the standard reportable steps."""
    if vis_m is None:
        return '9999'
    steps = [0, 50, 100, 200, 300, 500, 800, 1000, 1200, 1500, 2000, 3000,
             4000, 5000, 6000, 8000, 9999]
    chosen = 0
    for s in steps:
        if vis_m >= s:
            chosen = s
    return f'{chosen:04d}'


def _round_cloud_height(ft):
    """AFMAN 15-124 rounding: nearest 100 up to 5000, nearest 500 to
    10000, nearest 1000 above (spec rule 3)."""
    if ft <= 5000:
        return int(round(ft / 100.0)) * 100
    if ft <= 10000:
        return int(round(ft / 500.0)) * 500
    return int(round(ft / 1000.0)) * 1000


def _cloud_group_text(clouds):
    if not clouds:
        return 'SKC'
    return ' '.join(f'{cover}{_round_cloud_height(h) // 100:03d}' for cover, h in clouds)


def _wind_group_text(cond):
    if cond['wind_kt'] is None:
        return '00000KT'
    g = f"G{cond['gust_kt']:02d}" if cond['gust_kt'] else ''
    if cond['wind_kt'] == 0:
        return f"00000{g}KT"
    if cond['wind_var']:
        return f"VRB{cond['wind_kt']:02d}{g}KT"
    return f"{cond['wind_dir']:03d}{cond['wind_kt']:02d}{g}KT"


def _significant_change(a, b):
    """Whether hour b differs enough from hour a to justify a BECMG group
    (spec rule 7: prefer BECMG for model-derived trends)."""
    if a is None:
        return True
    if (a['ceiling_ft'] is None) != (b['ceiling_ft'] is None):
        return True
    if a['ceiling_ft'] is not None and b['ceiling_ft'] is not None:
        if abs(a['ceiling_ft'] - b['ceiling_ft']) >= 500:
            return True
    av = a['vis_m'] if a['vis_m'] is not None else 9999
    bv = b['vis_m'] if b['vis_m'] is not None else 9999
    if abs(av - bv) >= 1600:
        return True
    aw = a['wind_kt'] or 0
    bw = b['wind_kt'] or 0
    if abs(aw - bw) >= 10:
        return True
    if bool(a['weather']) != bool(b['weather']):
        return True
    return False


def _reverts_toward(baseline, outlier, next_cond):
    """Whether next_cond counts as "back to normal" after outlier, for
    TEMPO-vs-BECMG classification. Not just "not significantly different
    from baseline" -- found live 2026-09-22 (real laptop-side testing,
    KSER/KBAK): a ceiling spike sandwiched between two hours that are
    close to EACH OTHER (e.g. 19Z~36600ft, 20Z~2214ft, 21Z~33800ft) was
    missed because 21Z's absolute value still differs from the exact 19Z
    baseline by >500ft, even though it's obviously "recovered" relative to
    the 2214ft outlier. A single-hour blip is characterized by being an
    outlier relative to ITS NEIGHBORS, not by exactly matching some
    earlier baseline value -- so reversion is judged by how far next_cond
    moved back toward baseline relative to how far outlier had strayed,
    not by an absolute re-application of the same significance threshold."""
    if not _significant_change(baseline, next_cond):
        return True

    def _better(key, scale=1.0):
        b, o, n = baseline.get(key), outlier.get(key), next_cond.get(key)
        if b is None or o is None or n is None:
            return None
        strayed = abs(b - o)
        remaining = abs(b - n)
        return strayed > 0 and remaining <= strayed * 0.5

    # Ceiling and visibility are the fields actually driving the observed
    # cases; wind reversion isn't attempted here (gustier/noisier by
    # nature, no real evidence yet that the same fix is needed there).
    for key in ('ceiling_ft', 'vis_m'):
        result = _better(key)
        if result is True:
            return True
    return False


MIN_BECMG_GAP_HOURS = 3  # real TAFs don't chase every hourly model wobble;
                         # without a floor, noisy ceiling values oscillating
                         # near the 500ft threshold produced 8 consecutive
                         # one-hour BECMG groups in testing -- unusable.

# BECMG means a LASTING change; TEMPO means a fluctuation that reverts
# within roughly an hour (WMO/AFMAN convention, confirmed with Gerry
# 2026-09-22). The original design only ever emitted BECMG, wrongly
# treating any one-hour model blip as a permanent regime change. Hourly
# model data can't resolve anything finer than "does it revert by next
# hour," so that's the classification test: if the very next hour's
# condition reverts back toward the prior baseline, it's TEMPO (an
# overlay -- doesn't change the underlying prevailing conditions); if it
# persists into a second hour too, it's BECMG (lasting, updates the
# baseline).


def build_taf_text(station, cycle_time, issue_time, hourly, elev_ft):
    """hourly: list of (valid_time, cond) tuples, hourly, ascending."""
    if not hourly:
        return None, {}

    valid_from = hourly[0][0]
    valid_to = hourly[-1][0] + timedelta(hours=1)
    header = (f"TAF {station} {issue_time.strftime('%d%H%M')}Z "
              f"{valid_from.strftime('%d%H')}/{valid_to.strftime('%d%H')}")

    base_cond = hourly[0][1]
    lines = [
        f"{header} {_wind_group_text(base_cond)} {_vis_group_text(base_cond['vis_m'])}"
        + (f" {base_cond['weather']}" if base_cond['weather'] else '')
        + f" {_cloud_group_text(base_cond['clouds'])}"
        + (f" QNH{int(round(base_cond['altimeter_inhg']*100)):04d}INS" if base_cond['altimeter_inhg'] else '')
    ]

    # ceiling_ft_by_hour must reflect what the GENERATED TEXT implies per
    # hour, not the raw per-hour model value -- a BECMG group compresses a
    # continuously-varying model timeseries into discrete steps, and a
    # BECMG's new conditions only become prevailing at the window's END
    # time (matching taf_usaf.py's own Taf.snapshot() semantics: `t >=
    # g.end` before treating a BECMG as complete), not at its start. This
    # is what spec section 7.8's round-trip acceptance test checks against.
    def _rounded_ceil(c):
        return None if c['ceiling_ft'] is None else _round_cloud_height(c['ceiling_ft'])

    ceiling_ft_by_hour = {}
    prev = base_cond
    pending = None  # (activation_time, cond) for a BECMG not yet complete
    compare = base_cond
    last_becmg_start = hourly[0][0]
    last_tempo_end = hourly[0][0]
    for i, (t, cond) in enumerate(hourly):
        if pending is not None and t >= pending[0]:
            prev = pending[1]
            pending = None
        ceiling_ft_by_hour[t.isoformat() + 'Z'] = _rounded_ceil(prev)
        if t == hourly[0][0]:
            continue
        if pending is not None or t < last_tempo_end:
            continue
        if not _significant_change(compare, cond):
            continue

        next_cond = hourly[i + 1][1] if i + 1 < len(hourly) else None
        reverts = next_cond is not None and _reverts_toward(compare, cond, next_cond)

        if reverts:
            tempo_end = t + timedelta(hours=1)
            lines.append(
                f"    TEMPO {t.strftime('%d%H')}/{tempo_end.strftime('%d%H')} "
                f"{_wind_group_text(cond)} {_vis_group_text(cond['vis_m'])}"
                + (f" {cond['weather']}" if cond['weather'] else ' NSW')
                + f" {_cloud_group_text(cond['clouds'])}"
            )
            last_tempo_end = tempo_end
            continue

        gap_ok = (t - last_becmg_start) >= timedelta(hours=MIN_BECMG_GAP_HOURS)
        if not gap_ok:
            continue
        becmg_end = t + timedelta(hours=1)
        lines.append(
            f"    BECMG {t.strftime('%d%H')}/{becmg_end.strftime('%d%H')} "
            f"{_wind_group_text(cond)} {_vis_group_text(cond['vis_m'])}"
            + (f" {cond['weather']}" if cond['weather'] else ' NSW')
            + f" {_cloud_group_text(cond['clouds'])}"
            + (f" QNH{int(round(cond['altimeter_inhg']*100)):04d}INS" if cond['altimeter_inhg'] else '')
        )
        pending = (becmg_end, cond)
        compare = cond
        last_becmg_start = t

    temps = [c['temp_c'] for _, c in hourly if c['temp_c'] is not None]
    if temps:
        tx_i = max(range(len(hourly)), key=lambda i: hourly[i][1]['temp_c'] if hourly[i][1]['temp_c'] is not None else -999)
        tn_i = min(range(len(hourly)), key=lambda i: hourly[i][1]['temp_c'] if hourly[i][1]['temp_c'] is not None else 999)
        tx_t, tx_c = hourly[tx_i][0], hourly[tx_i][1]['temp_c']
        tn_t, tn_c = hourly[tn_i][0], hourly[tn_i][1]['temp_c']
        def _t(c):
            return f"M{abs(round(c)):02d}" if c < 0 else f"{round(c):02d}"
        lines.append(f"    TX{_t(tx_c)}/{tx_t.strftime('%d%H')}Z TN{_t(tn_c)}/{tn_t.strftime('%d%H')}Z")

    return '\n'.join(lines) + '=', ceiling_ft_by_hour

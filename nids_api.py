#!/usr/bin/env python3
"""
nids_api.py — Single-site NIDS radar API v2.1
Renders NIDS files on demand -- nids_site C binary for the "digital"
packet-16 products (N0B, N0H, N0Q, TDWR), MetPy for N0S (legacy 4-bit RLE
storm-relative velocity, a genuinely different packet format nids_site
can't parse; see _render_n0s()).
Supports WSR-88D (N0B, N0H, N0Q, N0S) and TDWR (TZ0, TZ1, TZL) products.

Endpoints:
  GET /api/nids/<site>/<product>           — render newest file, return PNG
  GET /api/nids/<site>/<product>/meta      — metadata JSON only (fast)
  GET /api/nids/<site>/<product>/history   — list available files for animation
  GET /api/nids/sites                      — list sites with coords, type, range
                                             ?radar_type=WSR-88D|TDWR (default: WSR-88D)
"""
import os
import io
import math
import glob
import json
import time
import subprocess
import tempfile
import logging
from datetime import datetime, timezone, timedelta
from flask import Blueprint, Response, jsonify, request
import psycopg2
import psycopg2.extras

# numpy/metpy/PIL are imported here at module load, not lazily inside
# _render_n0s() -- under mod_wsgi's threaded daemon mode (threads=15, one
# process), a per-request `import metpy` let two concurrent requests race on
# Pint's one-time global unit-registry initialization the first time MetPy
# was imported, corrupting it and hanging the whole daemon process (observed
# directly: "descriptor '__setattr__' requires a 'super' object but received
# a 'UnitDefinition'", then the entire daemon timing out on unrelated
# requests). Importing at module level makes Python's own import lock do the
# one-time serialization instead, at process startup, before any request
# thread exists to race against.
import numpy as np
from metpy.io import Level3File
from PIL import Image

log = logging.getLogger(__name__)

nids_api = Blueprint('nids_api', __name__)

L3_BASE    = '/LDM/radar/level3'
NIDS_SITE  = '/home/ldm/bin/nids_site'
CACHE_DIR  = '/tmp/nids_cache'
CACHE_SECS = 300    # 5 min render cache
MAX_AGE    = 720    # 12 min — skip stale for "live" display
DB_DSN     = 'host=192.168.0.60 port=5432 dbname=avwx_data user=avwx_user'
RENDER_SIZE = 1024  # PNG pixels

# WSR-88D products: N0B=dual-pol refl, N0H=hydrometeor class, N0Q=legacy refl,
#                    N0S=storm-relative velocity (legacy 4-bit RLE, decoded via
#                    MetPy -- see render_n0s() -- not the nids_site C binary,
#                    which only understands the newer "digital" packet format
#                    N0B/N0H use; N0S has no nested-bzip2 packet-16 structure
#                    at all, confirmed empirically against real SHV data)
# TDWR products:    TZ0=base refl 48nmi tilt1, TZ1=base refl tilt2, TZL=long range refl 225nmi
VALID_PRODUCTS = {'N0B', 'N0H', 'N0Q', 'N0S', 'TZ0', 'TZ1', 'TZ2', 'TZL'}

TDWR_PRODUCTS   = {'TZ0', 'TZ1', 'TZ2', 'TZL'}
WSR88D_PRODUCTS = {'N0B', 'N0H', 'N0Q', 'N0S'}

os.makedirs(CACHE_DIR, exist_ok=True)

# ── In-memory site list cache — separate for WSR-88D and TDWR ────────────────
_site_cache = {
    'WSR-88D': {'data': None, 'ts': 0},
    'TDWR':    {'data': None, 'ts': 0},
    'ttl': 300,
}


def get_sites(radar_type='WSR-88D'):
    """Return radar sites from DB by type, cached 5 min.
    Returns list of dicts: {site_id, lat, lon, range_km, radar_type}
    """
    radar_type = radar_type.upper()
    if radar_type not in ('WSR-88D', 'TDWR'):
        radar_type = 'WSR-88D'

    now = time.time()
    cache = _site_cache[radar_type]
    if cache['data'] and (now - cache['ts']) < _site_cache['ttl']:
        return cache['data']

    try:
        conn = psycopg2.connect(DB_DSN)
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT site_id, lat, lon, range_km, radar_type
            FROM radar.radar_sites
            WHERE radar_type = %s
            ORDER BY site_id
        """, (radar_type,))
        sites = [{'site_id':    r['site_id'],
                  'lat':        float(r['lat']),
                  'lon':        float(r['lon']),
                  'range_km':   int(r['range_km']),
                  'radar_type': r['radar_type']} for r in cur.fetchall()]
        conn.close()
        cache['data'] = sites
        cache['ts']   = now
        return sites
    except Exception as e:
        log.error(f'get_sites({radar_type}) error: {e}')
        return cache['data'] or []


def get_site_coords(site_id):
    """Get lat/lon/range_km/radar_type for a single site."""
    site_id = site_id.upper()
    # Check both caches
    for rtype in ('WSR-88D', 'TDWR'):
        for s in get_sites(rtype):
            if s['site_id'] == site_id:
                return s
    # Miss — try DB directly (e.g. OCONUS not in CONUS cache)
    try:
        conn = psycopg2.connect(DB_DSN)
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""SELECT lat, lon, range_km, radar_type
                       FROM radar.radar_sites WHERE site_id=%s""",
                    (site_id,))
        row = cur.fetchone()
        conn.close()
        if row:
            return {'site_id':    site_id,
                    'lat':        float(row['lat']),
                    'lon':        float(row['lon']),
                    'range_km':   int(row['range_km']),
                    'radar_type': row['radar_type']}
    except Exception:
        pass
    return None


def find_newest_nids(site_id, product):
    """Find newest NIDS file within MAX_AGE seconds. Returns (path, age) or (None, None)."""
    now   = time.time()
    today = datetime.now(timezone.utc).strftime('%Y%m%d')
    yest  = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y%m%d')
    for date in [today, yest]:
        pattern = os.path.join(L3_BASE, site_id, product, 'nids', date, '*.nids')
        files = sorted(glob.glob(pattern))
        if not files:
            continue
        newest = files[-1]
        age = now - os.path.getmtime(newest)
        if age <= MAX_AGE:
            return newest, int(age)
    return None, None


def find_history_nids(site_id, product, n=10, max_age_secs=14400):
    """Return list of recent NIDS files for animation (newest first)."""
    now   = time.time()
    today = datetime.now(timezone.utc).strftime('%Y%m%d')
    yest  = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y%m%d')
    files = []
    for date in [today, yest]:
        pattern = os.path.join(L3_BASE, site_id, product, 'nids', date, '*.nids')
        day_files = sorted(glob.glob(pattern), reverse=True)
        for f in day_files:
            age = now - os.path.getmtime(f)
            if age <= max_age_secs:
                files.append({'path': f, 'age': int(age),
                              'name': os.path.basename(f)})
            if len(files) >= n:
                break
        if len(files) >= n:
            break
    return files[:n]


def _render_via_nids_site(site_id, product, nids_file, lat, lon):
    """Render via the nids_site C binary (N0B/N0H/N0Q/TDWR -- the "digital"
    packet-16 NIDS format). Returns (png_bytes, meta_partial) or (None, None).
    """
    with tempfile.NamedTemporaryFile(suffix='.png', delete=False,
                                     dir=CACHE_DIR) as tmp:
        tmp_path = tmp.name
    try:
        result = subprocess.run(
            [NIDS_SITE,
             '-i', nids_file,
             '-o', tmp_path,
             '-la', str(lat),
             '-lo', str(lon),
             '-p', product,
             '-s', str(RENDER_SIZE)],
            capture_output=True, text=True, timeout=20
        )
        if result.returncode != 0 or not result.stdout.strip():
            log.error(f'nids_site failed {site_id}/{product}: {result.stderr}')
            return None, None
        meta = json.loads(result.stdout.strip())
        with open(tmp_path, 'rb') as f: png = f.read()
        return png, meta
    except subprocess.TimeoutExpired:
        log.error(f'nids_site timeout: {site_id}')
        return None, None
    except Exception as e:
        log.error(f'_render_via_nids_site error: {e}')
        return None, None
    finally:
        if os.path.exists(tmp_path):
            try: os.unlink(tmp_path)
            except Exception: pass


# ── N0S storm-relative velocity (legacy 4-bit RLE) ───────────────────────────
# nids_site's C parser only understands the "digital" packet-16 format N0B/N0H
# use -- confirmed empirically that N0S has no nested-bzip2 packet-16
# structure at all, it's the older legacy Radial Data Packet encoding.
# Decoded here via MetPy (already vendored, and it already solves the hard
# part: LegacyMapper correctly applies the embedded per-scan threshold table
# via Level3File.map_data(), giving real calibrated kt values rather than
# raw 0-15 codes -- reimplementing that calibration by hand would be a real
# way to get a safety-adjacent product subtly wrong).
N0S_MAX_RANGE_KM = 230.0
# Dead band -5 to +5 kt suppressed (matches the noise-suppression convention
# already used for MRMS AzShear elsewhere in this app). Green = inbound
# (negative, toward radar), red = outbound (positive, away).
N0S_BINS = [
    (-1e9, -50, (0, 60, 0, 240)),  (-50, -40, (0, 100, 0, 230)),
    (-40, -30, (0, 140, 0, 220)),  (-30, -20, (0, 170, 0, 210)),
    (-20, -10, (50, 200, 50, 200)),(-10, -5,  (100, 230, 100, 190)),
    (-5, 5,    (0, 0, 0, 0)),      (5, 10,    (255, 180, 180, 180)),
    (10, 20,   (240, 120, 120, 190)),(20, 30,  (230, 70, 70, 200)),
    (30, 40,   (220, 20, 20, 210)),(40, 50,   (190, 0, 0, 220)),
    (50, 1e9,  (110, 0, 0, 240)),
]


def _n0s_colorize(vel_kt):
    """vel_kt: 2D numpy array of calibrated velocity (kt), NaN = no data.
    Returns (H, W, 4) uint8 RGBA array.
    """
    rgba = np.zeros(vel_kt.shape + (4,), dtype=np.uint8)
    finite = ~np.isnan(vel_kt)
    for lo, hi, color in N0S_BINS:
        mask = finite & (vel_kt >= lo) & (vel_kt < hi)
        rgba[mask] = color
    return rgba


def _render_n0s(site_id, nids_file, lat, lon, px_size=RENDER_SIZE):
    """Render N0S via MetPy decode + our own georeferencing/rasterization
    (same site-centered ± range_km projection nids_site.c uses).
    Returns (png_bytes, meta_partial) or (None, None).
    """
    try:
        f = Level3File(nids_file)
        sym = f.sym_block[0][0]
        raw = np.array(sym['data'])
        vel = f.map_data(raw)  # calibrated kt, NaN = missing/range-folded
        # MetPy doesn't currently distinguish RF from missing (upstream TODO
        # in nexrad.py) -- both come back NaN, both render transparent here.

        start_az = np.array(sym['start_az'])
        end_az   = np.array(sym['end_az'])
        gate_scale = sym['gate_scale']
        n_radials, n_gates = vel.shape

        rgba_by_radial = _n0s_colorize(vel)  # (n_radials, n_gates, 4)

        km_per_lat = 111.32
        km_per_lon = km_per_lat * math.cos(math.radians(lat))
        lat_range  = N0S_MAX_RANGE_KM / km_per_lat
        lon_range  = N0S_MAX_RANGE_KM / km_per_lon
        lat_min, lat_max = lat - lat_range, lat + lat_range
        lon_min, lon_max = lon - lon_range, lon + lon_range

        canvas = np.zeros((px_size, px_size, 4), dtype=np.uint8)
        gate_range_km = (np.arange(n_gates) + 0.5) * gate_scale
        in_range = gate_range_km <= N0S_MAX_RANGE_KM

        # Fully vectorized: 3 sub-angles per radial (start/mid/end, to close
        # gaps between adjacent ~1deg radials at outer range -- same idea as
        # nids_site.c's nsub subdivision) computed for all 360 radials x 230
        # gates at once via broadcasting, instead of a 360-iteration Python
        # loop. Matters under mod_wsgi's threaded single-process daemon --
        # a slow CPU-bound render holds the GIL and stalls every other
        # concurrent request on this app, not just this one.
        opaque = in_range[np.newaxis, :] & (rgba_by_radial[:, :, 3] > 0)  # (n_radials, n_gates)
        mid_az = (start_az + end_az) / 2.0
        range_grid = np.broadcast_to(gate_range_km, (n_radials, n_gates))

        for az in (start_az, mid_az, end_az):
            az_rad = np.radians(az)[:, np.newaxis]  # (n_radials, 1)
            dx = range_grid * np.sin(az_rad)
            dy = range_grid * np.cos(az_rad)
            pt_lat = lat + dy / km_per_lat
            pt_lon = lon + dx / km_per_lon
            px = ((pt_lon - lon_min) / (lon_max - lon_min) * px_size).astype(int)
            py = ((lat_max - pt_lat) / (lat_max - lat_min) * px_size).astype(int)
            inb = opaque & (px >= 0) & (px < px_size) & (py >= 0) & (py < px_size)
            canvas[py[inb], px[inb]] = rgba_by_radial[inb]

        img = Image.fromarray(canvas, mode='RGBA')
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        png = buf.getvalue()

        meta = {
            'lat_min': lat_min, 'lat_max': lat_max,
            'lon_min': lon_min, 'lon_max': lon_max,
            'site_lat': lat, 'site_lon': lon,
            'n_radials': int(n_radials), 'n_bins': int(n_gates),
        }
        return png, meta
    except Exception as e:
        log.error(f'_render_n0s error for {site_id}: {e}')
        return None, None


def render_nids(site_id, product, nids_file, lat, lon):
    """Render NIDS file to PNG, dispatching to the right decoder for the
    product's packet format. Returns (png_bytes, meta) or (None, None).
    """
    mtime      = int(os.path.getmtime(nids_file))
    cache_key  = f'{site_id}_{product}_{mtime}'
    cache_png  = os.path.join(CACHE_DIR, f'{cache_key}.png')
    cache_json = os.path.join(CACHE_DIR, f'{cache_key}.json')

    if os.path.exists(cache_png) and os.path.exists(cache_json):
        with open(cache_png, 'rb') as f: png = f.read()
        with open(cache_json) as f:      meta = json.load(f)
        return png, meta

    if product == 'N0S':
        png, meta = _render_n0s(site_id, nids_file, lat, lon)
    else:
        png, meta = _render_via_nids_site(site_id, product, nids_file, lat, lon)

    if png is None:
        return None, None

    meta.update({
        'site_id':   site_id,
        'product':   product,
        'file':      os.path.basename(nids_file),
        'generated': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    })

    with open(cache_png, 'wb') as f: f.write(png)
    with open(cache_json, 'w') as f: json.dump(meta, f)

    # Scour old cache files for this site/product (keep newest 12)
    old = sorted(glob.glob(os.path.join(CACHE_DIR, f'{site_id}_{product}_*.png')))
    for old_f in old[:-12]:
        try: os.unlink(old_f)
        except Exception: pass
        try: os.unlink(old_f.replace('.png', '.json'))
        except Exception: pass

    return png, meta


# ── Routes ────────────────────────────────────────────────────────────────────

@nids_api.route('/sites')
def list_sites():
    """Return sites that have recent data for any product of the requested type.
    ?radar_type=WSR-88D|TDWR  (default WSR-88D)
    ?product=N0B|N0H|N0Q|TZ0|TZ1|TZ2|TZL  (optional; used to infer radar_type)

    For TDWR: a site is available if it has data for ANY TZ product —
    not all sites broadcast all tilts. Returns range_km and radar_type
    so nearestSite() can apply the correct range constraint.
    """
    radar_type  = request.args.get('radar_type', 'WSR-88D').upper()
    product_arg = request.args.get('product', '').upper()
    if product_arg in TDWR_PRODUCTS:
        radar_type = 'TDWR'
    product = product_arg or ('TZ0' if radar_type == 'TDWR' else 'N0B')

    check_products = list(TDWR_PRODUCTS) if radar_type == 'TDWR' else [product]

    sites     = get_sites(radar_type)
    available = []
    for s in sites:
        best_product = None
        best_age     = None
        for prod in ([product] + [p for p in check_products if p != product]):
            _, age = find_newest_nids(s['site_id'], prod)
            if age is not None:
                if best_product is None:
                    best_product = prod
                    best_age     = age
                if prod == product:
                    best_product = prod
                    best_age     = age
                    break
        if best_product is not None:
            available.append({
                'site_id':      s['site_id'],
                'lat':          s['lat'],
                'lon':          s['lon'],
                'range_km':     s['range_km'],
                'radar_type':   s['radar_type'],
                'best_product': best_product,
                'age_secs':     best_age,
            })

    return jsonify({'product': product, 'radar_type': radar_type,
                    'sites': available, 'count': len(available)})


@nids_api.route('/<site_id>/<product>/meta')
def get_meta(site_id, product):
    site_id = site_id.upper(); product = product.upper()
    if product not in VALID_PRODUCTS:
        return jsonify({'error': 'Invalid product'}), 400
    site = get_site_coords(site_id)
    if not site:
        return jsonify({'error': f'Unknown site: {site_id}'}), 404
    nids_file, age = find_newest_nids(site_id, product)
    if not nids_file:
        return jsonify({'error': f'No recent {product} for {site_id}'}), 404
    return jsonify({
        'site_id':    site_id,
        'product':    product,
        'file':       os.path.basename(nids_file),
        'age_secs':   age,
        'lat':        site['lat'],
        'lon':        site['lon'],
        'range_km':   site['range_km'],
        'radar_type': site['radar_type'],
    })


@nids_api.route('/<site_id>/<product>/history')
def get_history(site_id, product):
    site_id = site_id.upper(); product = product.upper()
    if product not in VALID_PRODUCTS:
        return jsonify({'error': 'Invalid product'}), 400
    site = get_site_coords(site_id)
    if not site:
        return jsonify({'error': f'Unknown site: {site_id}'}), 404

    hours = request.args.get('hours', 0, type=float)
    if hours > 0:
        # TDWR scans ~every 2.5 min = ~24/hr (TZ0/TZ1/TZL); WSR-88D N0B ~2min = ~30/hr; N0H ~6min = ~10/hr
        fps = 10 if product == 'N0H' else (24 if product in TDWR_PRODUCTS else 30)
        n = min(int(hours * fps) + 5, 200)
        max_age = int(hours * 3600) + 300
    else:
        n = request.args.get('n', 10, type=int)
        max_age = 14400

    files  = find_history_nids(site_id, product, n, max_age)
    frames = []
    for i, f in enumerate(files):
        frames.append({
            'index':    i,
            'file':     f['name'],
            'age_secs': f['age'],
            'url':      f'/CAP_WxCOP/api/nids/{site_id}/{product}'
                        f'?file={f["name"]}&_={int(os.path.getmtime(f["path"]))}'
        })
    return jsonify({
        'site_id':    site_id,
        'product':    product,
        'lat':        site['lat'],
        'lon':        site['lon'],
        'range_km':   site['range_km'],
        'radar_type': site['radar_type'],
        'frames':     frames,
        'count':      len(frames),
    })


@nids_api.route('/<site_id>/<product>')
def get_png(site_id, product):
    site_id = site_id.upper(); product = product.upper()
    if product not in VALID_PRODUCTS:
        return jsonify({'error': 'Invalid product'}), 400

    site = get_site_coords(site_id)
    if not site:
        return jsonify({'error': f'Unknown site: {site_id}'}), 404
    lat, lon = site['lat'], site['lon']

    fname = request.args.get('file')
    if fname:
        today = datetime.now(timezone.utc).strftime('%Y%m%d')
        yest  = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y%m%d')
        nids_file = None
        for date in [today, yest]:
            candidate = os.path.join(L3_BASE, site_id, product,
                                     'nids', date, fname)
            if os.path.exists(candidate):
                nids_file = candidate
                break
        if not nids_file:
            return jsonify({'error': f'File not found: {fname}'}), 404
        age = int(time.time() - os.path.getmtime(nids_file))
    else:
        nids_file, age = find_newest_nids(site_id, product)
        if not nids_file:
            return jsonify({'error': f'No recent {product} for {site_id}'}), 404

    png, meta = render_nids(site_id, product, nids_file, lat, lon)
    if not png:
        return jsonify({'error': f'Render failed for {site_id}'}), 500

    resp = Response(png, mimetype='image/png')
    resp.headers['X-Radar-Meta']  = json.dumps(meta)
    resp.headers['X-Age-Seconds'] = str(age)
    resp.headers['Cache-Control'] = f'max-age={CACHE_SECS}'
    return resp

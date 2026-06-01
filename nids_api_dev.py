#!/usr/bin/env python3
"""
nids_api.py — Single-site NIDS radar API v2.1
Renders NIDS files on demand via nids_site C binary.
Supports WSR-88D (N0B, N0H, N0Q) and TDWR (TZ0, TZ1, TZL) products.

Endpoints:
  GET /api/nids/<site>/<product>           — render newest file, return PNG
  GET /api/nids/<site>/<product>/meta      — metadata JSON only (fast)
  GET /api/nids/<site>/<product>/history   — list available files for animation
  GET /api/nids/sites                      — list sites with coords, type, range
                                             ?radar_type=WSR-88D|TDWR (default: WSR-88D)
"""
import os
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

log = logging.getLogger(__name__)

nids_api = Blueprint('nids_api', __name__)

L3_BASE    = '/LDM/radar/level3'
NIDS_SITE  = '/home/ldm/bin/nids_site'
CACHE_DIR  = '/tmp/nids_cache'
CACHE_SECS = 300    # 5 min render cache
MAX_AGE    = 720    # 12 min — skip stale for "live" display
DB_DSN     = 'host=192.168.0.60 port=5432 dbname=avwx_data user=avwx_user'
RENDER_SIZE = 1024  # PNG pixels

# WSR-88D products: N0B=dual-pol refl, N0H=hydrometeor class, N0Q=legacy refl
# TDWR products:    TZ0=base refl 48nmi tilt1, TZ1=base refl tilt2, TZL=long range refl 225nmi
VALID_PRODUCTS = {'N0B', 'N0H', 'N0Q', 'TZ0', 'TZ1', 'TZ2', 'TZL'}

TDWR_PRODUCTS   = {'TZ0', 'TZ1', 'TZ2', 'TZL'}
WSR88D_PRODUCTS = {'N0B', 'N0H', 'N0Q'}

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


def render_nids(site_id, product, nids_file, lat, lon):
    """Render NIDS file to PNG via nids_site C binary.
    Returns (png_bytes, meta) or (None, None).
    """
    mtime      = int(os.path.getmtime(nids_file))
    cache_key  = f'{site_id}_{product}_{mtime}'
    cache_png  = os.path.join(CACHE_DIR, f'{cache_key}.png')
    cache_json = os.path.join(CACHE_DIR, f'{cache_key}.json')

    if os.path.exists(cache_png) and os.path.exists(cache_json):
        with open(cache_png, 'rb') as f: png = f.read()
        with open(cache_json) as f:      meta = json.load(f)
        return png, meta

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
        meta.update({
            'site_id':   site_id,
            'product':   product,
            'file':      os.path.basename(nids_file),
            'generated': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        })

        with open(tmp_path, 'rb') as f: png = f.read()
        os.rename(tmp_path, cache_png)
        with open(cache_json, 'w') as f: json.dump(meta, f)

        # Scour old cache files for this site/product (keep newest 12)
        old = sorted(glob.glob(os.path.join(CACHE_DIR,
                     f'{site_id}_{product}_*.png')))
        for old_f in old[:-12]:
            try: os.unlink(old_f)
            except Exception: pass
            try: os.unlink(old_f.replace('.png', '.json'))
            except Exception: pass

        return png, meta

    except subprocess.TimeoutExpired:
        log.error(f'nids_site timeout: {site_id}')
        return None, None
    except Exception as e:
        log.error(f'render_nids error: {e}')
        return None, None
    finally:
        if os.path.exists(tmp_path):
            try: os.unlink(tmp_path)
            except Exception: pass


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

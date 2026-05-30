#!/usr/bin/env python3
"""
nids_api.py — Single-site NIDS radar API v2
Renders NIDS files on demand via nids_site C binary.

Endpoints:
  GET /api/nids/<site>/<product>           — render newest file, return PNG
  GET /api/nids/<site>/<product>/meta      — metadata JSON only (fast)
  GET /api/nids/<site>/<product>/history   — list available files for animation
  GET /api/nids/sites                      — list CONUS sites with coords
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
MAX_AGE    = 720    # 12 min — skip stale files
DB_DSN     = 'host=192.168.0.60 port=5432 dbname=avwx_data user=avwx_user'
RENDER_SIZE = 1024  # PNG pixels

VALID_PRODUCTS = {'N0B', 'N0H', 'N0Q'}

os.makedirs(CACHE_DIR, exist_ok=True)

# ── In-memory site list cache ─────────────────────────────────────────────
_site_cache = {'data': None, 'ts': 0, 'ttl': 300}

def get_conus_sites():
    """Return CONUS radar sites from DB, cached 5 min."""
    now = time.time()
    if _site_cache['data'] and (now - _site_cache['ts']) < _site_cache['ttl']:
        return _site_cache['data']
    try:
        conn = psycopg2.connect(DB_DSN)
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT site_id, lat, lon
            FROM radar.radar_sites
            WHERE lat BETWEEN 20 AND 52
              AND lon BETWEEN -127 AND -65
            ORDER BY site_id
        """)
        sites = [{'site_id': r['site_id'],
                  'lat': float(r['lat']),
                  'lon': float(r['lon'])} for r in cur.fetchall()]
        conn.close()
        _site_cache['data'] = sites
        _site_cache['ts']   = now
        return sites
    except Exception as e:
        log.error(f'get_conus_sites error: {e}')
        return _site_cache['data'] or []


def get_site_coords(site_id):
    """Get lat/lon for a single site."""
    sites = get_conus_sites()
    for s in sites:
        if s['site_id'] == site_id:
            return s['lat'], s['lon']
    # Try DB directly for OCONUS sites
    try:
        conn = psycopg2.connect(DB_DSN)
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute('SELECT lat, lon FROM radar.radar_sites WHERE site_id=%s',
                    (site_id,))
        row = cur.fetchone()
        conn.close()
        return (float(row['lat']), float(row['lon'])) if row else None
    except Exception:
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


def find_history_nids(site_id, product, n=10):
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
            if age <= 7200:  # 2 hours history
                files.append({'path': f, 'age': int(age),
                              'name': os.path.basename(f)})
        if len(files) >= n:
            break
    return files[:n]


def render_nids(site_id, product, nids_file, lat, lon):
    """Render NIDS file to PNG. Returns (png_bytes, meta) or (None, None)."""
    mtime = int(os.path.getmtime(nids_file))
    cache_key  = f'{site_id}_{product}_{mtime}'
    cache_png  = os.path.join(CACHE_DIR, f'{cache_key}.png')
    cache_json = os.path.join(CACHE_DIR, f'{cache_key}.json')

    # Return cached version if available
    if os.path.exists(cache_png) and os.path.exists(cache_json):
        with open(cache_png, 'rb') as f: png = f.read()
        with open(cache_json) as f:      meta = json.load(f)
        return png, meta

    # Render via C binary
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
            log.error(f'nids_site failed {site_id}: {result.stderr}')
            return None, None

        meta = json.loads(result.stdout.strip())
        meta.update({
            'site_id':   site_id,
            'product':   product,
            'file':      os.path.basename(nids_file),
            'generated': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        })

        with open(tmp_path, 'rb') as f: png = f.read()

        # Atomically move to cache
        os.rename(tmp_path, cache_png)
        with open(cache_json, 'w') as f: json.dump(meta, f)

        # Scour old cache files for this site/product (keep newest 12)
        old = sorted(glob.glob(os.path.join(CACHE_DIR,
                     f'{site_id}_{product}_*.png')))
        for old_f in old[:-12]:
            try: os.unlink(old_f)
            except Exception: pass
            try: os.unlink(old_f.replace('.png','.json'))
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


# ── Routes ────────────────────────────────────────────────────────────────

@nids_api.route('/sites')
def list_sites():
    """Return CONUS sites that have recent N0B data."""
    product = request.args.get('product', 'N0B').upper()
    sites = get_conus_sites()
    # Filter to only sites with recent data
    available = []
    for s in sites:
        _, age = find_newest_nids(s['site_id'], product)
        if age is not None:
            available.append(s)
    return jsonify({'product': product, 'sites': available, 'count': len(available)})


@nids_api.route('/<site_id>/<product>/meta')
def get_meta(site_id, product):
    site_id = site_id.upper(); product = product.upper()
    if product not in VALID_PRODUCTS:
        return jsonify({'error': 'Invalid product'}), 400
    coords = get_site_coords(site_id)
    if not coords:
        return jsonify({'error': f'Unknown site: {site_id}'}), 404
    nids_file, age = find_newest_nids(site_id, product)
    if not nids_file:
        return jsonify({'error': f'No recent {product} for {site_id}'}), 404
    return jsonify({
        'site_id': site_id, 'product': product,
        'file': os.path.basename(nids_file), 'age_secs': age,
        'lat': coords[0], 'lon': coords[1],
    })


@nids_api.route('/<site_id>/<product>/history')
def get_history(site_id, product):
    site_id = site_id.upper(); product = product.upper()
    if product not in VALID_PRODUCTS:
        return jsonify({'error': 'Invalid product'}), 400
    coords = get_site_coords(site_id)
    if not coords:
        return jsonify({'error': f'Unknown site: {site_id}'}), 404
    n = request.args.get('n', 10, type=int)
    files = find_history_nids(site_id, product, n)
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
        'site_id': site_id, 'product': product,
        'lat': coords[0], 'lon': coords[1],
        'frames': frames, 'count': len(frames),
    })


@nids_api.route('/<site_id>/<product>')
def get_png(site_id, product):
    site_id = site_id.upper(); product = product.upper()
    if product not in VALID_PRODUCTS:
        return jsonify({'error': 'Invalid product'}), 400

    coords = get_site_coords(site_id)
    if not coords:
        return jsonify({'error': f'Unknown site: {site_id}'}), 404
    lat, lon = coords

    # Support specific file for animation playback
    fname = request.args.get('file')
    if fname:
        today = datetime.now(timezone.utc).strftime('%Y%m%d')
        yest  = (datetime.now(timezone.utc)-timedelta(days=1)).strftime('%Y%m%d')
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

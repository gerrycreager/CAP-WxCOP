"""
mrms_wms_api.py — Flask blueprint for MRMS MapServer WMS proxy + frame index.

Endpoints:
  GET /CAP_WxCOP/api/mrms/frames?product=composite&hours=3
      Returns JSON list of available cached timestamps for the product.

  GET /CAP_WxCOP/api/mrms/wms?product=composite&ts=20260406-200438
                              &SERVICE=WMS&VERSION=1.1.1&REQUEST=GetMap
                              &LAYERS=composite&SRS=EPSG:4326
                              &BBOX=-100,35,-90,45&WIDTH=256&HEIGHT=256
                              &FORMAT=image/png&TRANSPARENT=TRUE
      Proxies a MapServer WMS GetMap for the specified cached frame.
      Falls back to _current.grib2 if ts is omitted or not found.

Author: CAP WxCOP
"""

import os
import re
import subprocess
import tempfile
import datetime
from pathlib import Path
from flask import Blueprint, request, jsonify, Response, abort

mrms_wms_bp = Blueprint('mrms_wms', __name__)

CACHE_DIR   = Path('/var/www/mapserver/cache')
MAPFILE_DIR = Path('/var/www/mapserver/mapfiles')
MS_CONFIG   = '/var/www/mapserver/mapserver.conf'
MAPSERV_BIN = '/usr/bin/mapserv'

# Product → mapfile template and WMS layer name
PRODUCT_CONFIG = {
    # CONUS products
    'composite':          {'mapfile': 'mrms_composite.map',          'layer': 'composite'},
    'azshear_low':        {'mapfile': 'mrms_azshear_low.map',        'layer': 'azshear_low'},
    'mesh':               {'mapfile': 'mrms_mesh.map',               'layer': 'mesh'},
    # OCONUS composite sectors
    'composite_alaska':   {'mapfile': 'mrms_composite_alaska.map',   'layer': 'composite_alaska'},
    'composite_hawaii':   {'mapfile': 'mrms_composite_hawaii.map',   'layer': 'composite_hawaii'},
    'composite_carib':    {'mapfile': 'mrms_composite_carib.map',    'layer': 'composite_carib'},
    'composite_guam':     {'mapfile': 'mrms_composite_guam.map',     'layer': 'composite_guam'},
}

TS_RE = re.compile(r'^\d{8}-\d{6}$')


def get_cache_path(product: str, ts: str | None) -> Path | None:
    """Resolve product + optional timestamp to a cached grib2 file path."""
    prod_dir = CACHE_DIR / product
    if ts and TS_RE.match(ts):
        candidate = prod_dir / f'{product}_{ts}.grib2'
        if candidate.exists():
            return candidate
    # Fall back to current symlink
    current = CACHE_DIR / f'{product}_current.grib2'
    if current.exists():
        return current
    return None


def make_temp_mapfile(base_mapfile: Path, data_path: Path) -> str:
    """
    Write a temp mapfile identical to base but with DATA pointing at data_path.
    Returns the temp file path. Caller must delete it.
    """
    with open(base_mapfile) as f:
        content = f.read()

    # Replace the DATA line
    prod_dir_str = str(CACHE_DIR / base_mapfile.stem.replace('mrms_', ''))
    # Match DATA "/var/www/mapserver/cache/..." line
    content = re.sub(
        r'DATA\s+"[^"]*"',
        f'DATA "{data_path}"',
        content
    )

    tmp = tempfile.NamedTemporaryFile(
        mode='w', suffix='.map', dir='/tmp',
        prefix='mrms_tmp_', delete=False
    )
    tmp.write(content)
    tmp.close()
    return tmp.name


@mrms_wms_bp.route('/api/mrms/frames')
def mrms_frames():
    """
    Return sorted list of available frame timestamps for a product.

    Query params:
      product : composite | azshear_low | mesh  (required)
      hours   : 1 | 2 | 3  (default 3)

    Response:
      {
        "product": "composite",
        "hours": 3,
        "frames": [
          {"ts": "20260406-170238", "label": "-180 min"},
          ...
          {"ts": "20260406-200438", "label": "LIVE"}
        ]
      }
    """
    product = request.args.get('product', '')
    if product not in PRODUCT_CONFIG:
        return jsonify({'error': f'Unknown product: {product}'}), 400

    try:
        hours = min(3, max(1, int(request.args.get('hours', 3))))
    except ValueError:
        hours = 3

    prod_dir = CACHE_DIR / product
    if not prod_dir.is_dir():
        return jsonify({'product': product, 'hours': hours, 'frames': []})

    cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=hours)
    ts_re  = re.compile(r'_(\d{8}-\d{6})\.grib2$')

    frames = []
    for f in sorted(prod_dir.glob(f'{product}_*.grib2')):
        m = ts_re.search(f.name)
        if not m:
            continue
        ts_str = m.group(1)
        try:
            ts_dt = datetime.datetime.strptime(ts_str, '%Y%m%d-%H%M%S')
        except ValueError:
            continue
        if ts_dt < cutoff:
            continue
        frames.append((ts_dt, ts_str))

    if not frames:
        return jsonify({'product': product, 'hours': hours, 'frames': []})

    frames.sort(key=lambda x: x[0])
    now = frames[-1][0]

    result = []
    for i, (ts_dt, ts_str) in enumerate(frames):
        delta_min = int((now - ts_dt).total_seconds() / 60)
        if i == len(frames) - 1:
            label = 'LIVE'
        else:
            label = f'-{delta_min} min'
        result.append({'ts': ts_str, 'label': label})

    return jsonify({'product': product, 'hours': hours, 'frames': result})


@mrms_wms_bp.route('/api/mrms/wms')
def mrms_wms():
    """
    MapServer WMS proxy for a specific cached MRMS frame.

    Required query params:
      product : composite | azshear_low | mesh
      ts      : timestamp string YYYYMMDD-HHMMSS (optional, defaults to current)
      + standard WMS params: SERVICE, VERSION, REQUEST, LAYERS, SRS/CRS,
                             BBOX, WIDTH, HEIGHT, FORMAT, TRANSPARENT, STYLES
    """
    product = request.args.get('product', '')
    if product not in PRODUCT_CONFIG:
        abort(400)

    ts = request.args.get('ts', None)
    cfg = PRODUCT_CONFIG[product]

    # Resolve cached grib2 file
    data_path = get_cache_path(product, ts)
    if data_path is None:
        abort(404)

    # Build WMS query string — pass through all standard WMS params
    wms_params = {k: v for k, v in request.args.items()
                  if k.upper() in ('SERVICE','VERSION','REQUEST','LAYERS',
                                   'SRS','CRS','BBOX','WIDTH','HEIGHT',
                                   'FORMAT','TRANSPARENT','STYLES',
                                   'EXCEPTIONS','MAP_RESOLUTION')}
    wms_params.setdefault('SERVICE', 'WMS')
    wms_params.setdefault('VERSION', '1.1.1')
    wms_params.setdefault('STYLES', '')
    wms_params['LAYERS'] = cfg['layer']

    query_string = '&'.join(f'{k}={v}' for k, v in wms_params.items())

    # Write temp mapfile with correct DATA path
    base_mapfile = MAPFILE_DIR / cfg['mapfile']
    tmp_mapfile  = make_temp_mapfile(base_mapfile, data_path)

    try:
        env = os.environ.copy()
        env['MAPSERVER_CONFIG_FILE'] = MS_CONFIG
        env['MS_MAPFILE']            = tmp_mapfile
        env['QUERY_STRING']          = query_string
        env['REQUEST_METHOD']        = 'GET'

        result = subprocess.run(
            [MAPSERV_BIN, '-nh'],
            env=env,
            capture_output=True,
            timeout=30
        )

        output = result.stdout
        # Parse CGI headers from output
        # MapServer output: headers\r\n\r\nbody
        if b'\r\n\r\n' in output:
            header_part, body = output.split(b'\r\n\r\n', 1)
        elif b'\n\n' in output:
            header_part, body = output.split(b'\n\n', 1)
        else:
            header_part, body = b'', output

        # Extract Content-Type
        content_type = 'image/png'
        for line in header_part.decode('utf-8', errors='ignore').splitlines():
            if line.lower().startswith('content-type:'):
                content_type = line.split(':', 1)[1].strip()
                break

        # Add CORS header for cross-origin Leaflet requests
        headers = {
            'Content-Type': content_type,
            'Access-Control-Allow-Origin': '*',
            'Cache-Control': 'max-age=120',
        }

        return Response(body, headers=headers)

    except subprocess.TimeoutExpired:
        abort(504)
    except Exception as e:
        abort(500)
    finally:
        try:
            os.unlink(tmp_mapfile)
        except Exception:
            pass


def register(app):
    app.register_blueprint(mrms_wms_bp)

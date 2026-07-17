"""
hrrr_smoke_wms_api.py — Flask blueprint for HRRR near-surface smoke
(MASSDEN) MapServer WMS proxy + frame index.

Endpoints:
  GET /CAP_WxCOP/api/hrrr-smoke/frames?hours=6
      Returns JSON list of available cached timestamps.

  GET /CAP_WxCOP/api/hrrr-smoke/wms?ts=20260717-180000
                                    &SERVICE=WMS&VERSION=1.1.1&REQUEST=GetMap
                                    &LAYERS=hrrr_smoke&SRS=EPSG:4326
                                    &BBOX=-130,20,-60,55&WIDTH=256&HEIGHT=256
                                    &FORMAT=image/png&TRANSPARENT=TRUE
      Proxies a MapServer WMS GetMap for the specified cached frame.
      Falls back to hrrr_smoke_current.tif if ts is omitted or not found.

See scripts/r815/hrrr_smoke_cache_updater.py for how these frames are
produced -- direct S3 range-fetch of the MASSDEN GRIB2 message per HRRR
cycle (bypasses LDM; the relay in use doesn't carry this field), GDAL
Translate straight to tiled GeoTIFF (native Lambert Conformal grid, no
manual reprojection -- the mapfile's own PROJECTION block declares that
CRS and MapServer reprojects to whatever SRS the client requests).

Mirrors tpw_wms_api.py's pattern; single product here so no PRODUCT_CONFIG
dict is needed.

Author: CAP WxCOP
"""

import os
import re
import subprocess
import tempfile
import datetime
from pathlib import Path
from flask import Blueprint, request, jsonify, Response, abort

hrrr_smoke_wms_bp = Blueprint('hrrr_smoke_wms', __name__)

CACHE_DIR   = Path('/var/www/mapserver/cache/hrrr_smoke')
MAPFILE     = Path('/var/www/mapserver/mapfiles/hrrr_smoke.map')
MS_CONFIG   = '/var/www/mapserver/mapserver.conf'
MAPSERV_BIN = '/usr/bin/mapserv'
PRODUCT     = 'hrrr_smoke'

TS_RE = re.compile(r'^\d{8}-\d{6}$')


def get_cache_path(ts: str | None) -> Path | None:
    if ts and TS_RE.match(ts):
        candidate = CACHE_DIR / f'{PRODUCT}_{ts}.tif'
        if candidate.exists():
            return candidate
    current = CACHE_DIR / f'{PRODUCT}_current.tif'
    if current.exists():
        return current
    return None


def make_temp_mapfile(data_path: Path) -> str:
    with open(MAPFILE) as f:
        content = f.read()

    content = re.sub(
        r'DATA\s+"[^"]*"',
        f'DATA "{data_path}"',
        content
    )

    tmp = tempfile.NamedTemporaryFile(
        mode='w', suffix='.map', dir='/tmp',
        prefix='hrrr_smoke_tmp_', delete=False
    )
    tmp.write(content)
    tmp.close()
    return tmp.name


@hrrr_smoke_wms_bp.route('/api/hrrr-smoke/frames')
def hrrr_smoke_frames():
    """
    Return sorted list of available frame timestamps.

    Query params:
      hours : 1-12 (default 6)

    Response:
      {
        "hours": 6,
        "frames": [
          {"ts": "20260717-150000", "label": "F00"},
          ...
          {"ts": "20260717-180000", "label": "LIVE"}
        ]
      }
    """
    try:
        hours = min(12, max(1, int(request.args.get('hours', 6))))
    except ValueError:
        hours = 6

    if not CACHE_DIR.is_dir():
        return jsonify({'hours': hours, 'frames': []})

    cutoff = datetime.datetime.now(datetime.UTC).replace(tzinfo=None) - datetime.timedelta(hours=hours)
    ts_re  = re.compile(r'_(\d{8}-\d{6})\.tif$')

    frames = []
    for f in sorted(CACHE_DIR.glob(f'{PRODUCT}_*.tif')):
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
        return jsonify({'hours': hours, 'frames': []})

    frames.sort(key=lambda x: x[0])

    result = []
    for i, (ts_dt, ts_str) in enumerate(frames):
        label = 'LIVE' if i == len(frames) - 1 else ts_dt.strftime('%HZ')
        result.append({'ts': ts_str, 'label': label})

    return jsonify({'hours': hours, 'frames': result})


@hrrr_smoke_wms_bp.route('/api/hrrr-smoke/wms')
def hrrr_smoke_wms():
    """
    MapServer WMS proxy for a specific cached HRRR-smoke frame.

    Query params:
      ts : timestamp string YYYYMMDD-HHMMSS (optional, defaults to current)
      + standard WMS params: SERVICE, VERSION, REQUEST, LAYERS, SRS/CRS,
                             BBOX, WIDTH, HEIGHT, FORMAT, TRANSPARENT, STYLES
    """
    ts = request.args.get('ts', None)

    data_path = get_cache_path(ts)
    if data_path is None:
        abort(404)

    wms_params = {k: v for k, v in request.args.items()
                  if k.upper() in ('SERVICE', 'VERSION', 'REQUEST', 'LAYERS',
                                    'SRS', 'CRS', 'BBOX', 'WIDTH', 'HEIGHT',
                                    'FORMAT', 'TRANSPARENT', 'STYLES',
                                    'EXCEPTIONS', 'MAP_RESOLUTION')}
    wms_params.setdefault('SERVICE', 'WMS')
    wms_params.setdefault('VERSION', '1.1.1')
    wms_params.setdefault('STYLES', '')
    wms_params['LAYERS'] = PRODUCT

    query_string = '&'.join(f'{k}={v}' for k, v in wms_params.items())

    tmp_mapfile = make_temp_mapfile(data_path)

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
        if b'\r\n\r\n' in output:
            header_part, body = output.split(b'\r\n\r\n', 1)
        elif b'\n\n' in output:
            header_part, body = output.split(b'\n\n', 1)
        else:
            header_part, body = b'', output

        content_type = 'image/png'
        for line in header_part.decode('utf-8', errors='ignore').splitlines():
            if line.lower().startswith('content-type:'):
                content_type = line.split(':', 1)[1].strip()
                break

        headers = {
            'Content-Type': content_type,
            'Access-Control-Allow-Origin': '*',
            'Cache-Control': 'max-age=120',
        }

        return Response(body, headers=headers)

    except subprocess.TimeoutExpired:
        abort(504)
    except Exception:
        abort(500)
    finally:
        try:
            os.unlink(tmp_mapfile)
        except Exception:
            pass


def register(app):
    app.register_blueprint(hrrr_smoke_wms_bp)

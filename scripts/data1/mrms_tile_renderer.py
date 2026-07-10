#!/usr/bin/env python3
"""
mrms_tile_renderer.py  —  CAP WxCOP MRMS tile renderer
Renders MRMS GRIB2 products to Leaflet-compatible PNG tile pyramids.

Usage (called from mrms_render_pipe.sh):
    python3 mrms_tile_renderer.py <product_key> <sector> <grib2_gz_path>

product_key : composite | mesh | mesh_max_30min | mesh_max_60min |
              mesh_max_120min | mesh_max_240min | mesh_max_360min |
              mesh_max_1440min | lightning | azshear
sector      : CONUS | ALASKA | HAWAII | CARIB | GUAM

Tile pyramid: zoom 3–10, written to TILE_ROOT/<product>/<SECTOR>/<timestamp>/{z}/{x}/{y}.png
Index JSON:   TILE_ROOT/<product>/<SECTOR>/index.json  (rolling 30-frame window)
"""

import sys, os, json, gzip, shutil, tempfile, logging, re, math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

# ── logging ───────────────────────────────────────────────────────────────────
LOG_FILE = '/var/www/cap_winds_app/logs/mrms_renderer.log'
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    filename=LOG_FILE, level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ── paths ─────────────────────────────────────────────────────────────────────
TILE_ROOT   = Path('/LDM/radar/mrms_tiles')
MAX_FRAMES  = 360
ZOOM_MIN    = 3
ZOOM_MAX    = 10

# ── product definitions ────────────────────────────────────────────────────────
#   vmin/vmax are the physical units rendered by the colormap.
#   Units: composite → dBZ;  mesh* → inches;  azshear → 0.001/s;  lightning → fl/km²/min

_REFL_STOPS = [
    (-30, (0,   0,   0,   0)),
    (  5, (4,   233, 231, 200)),
    ( 10, (1,   159, 244, 220)),
    ( 15, (3,   0,   244, 220)),
    ( 20, (3,   0,   244, 220)),
    ( 25, (2,   253, 2,   220)),
    ( 30, (0,   200, 0,   220)),
    ( 35, (0,   144, 0,   220)),
    ( 40, (255, 255, 0,   220)),
    ( 45, (231, 192, 0,   220)),
    ( 50, (255, 144, 0,   220)),
    ( 55, (255, 0,   0,   220)),
    ( 60, (214, 0,   0,   220)),
    ( 65, (192, 0,   0,   220)),
    ( 70, (255, 0,   255, 230)),
    ( 75, (192, 0,   192, 230)),
    ( 80, (255, 255, 255, 240)),
]

# MESH colormap: 0–3 inches.
# 0.00–0.25: transparent (no hail / trace)
# 0.25–0.75: light blue → cyan (small hail)
# 0.75–1.00: yellow (marginally severe, ~penny)
# 1.00–1.50: orange (severe, quarter+)
# 1.50–2.00: red
# 2.00–2.50: dark red / crimson
# 2.50–3.00: magenta / white (extreme)
_MESH_STOPS = [
    (0.00, (0,   0,   0,   0)),
    (0.10, (0,   0,   0,   0)),    # keep transparent below 0.1"
    (0.25, (100, 200, 255, 180)),  # light blue — trace hail
    (0.50, (0,   200, 255, 200)),  # cyan
    (0.75, (0,   255, 150, 210)),  # cyan-green
    (1.00, (255, 255, 0,   220)),  # yellow — severe threshold
    (1.25, (255, 165, 0,   225)),  # orange
    (1.50, (255, 80,  0,   230)),  # red-orange
    (1.75, (255, 0,   0,   230)),  # red
    (2.00, (180, 0,   0,   235)),  # dark red
    (2.50, (200, 0,   200, 240)),  # magenta
    (3.00, (255, 255, 255, 250)),  # white cap
]

# Azimuthal shear: divergent blue↔red centered on 0. Units: 0.001/s
# Operational concern threshold ≈ ±0.005 s⁻¹  (stored as ±5 in 0.001/s)
_AZSHEAR_STOPS = [
    (-20, (0,   0,   200, 230)),   # strong anticyclonic — deep blue
    (-10, (0,   100, 255, 210)),
    ( -5, (100, 180, 255, 180)),   # weak anticyclonic — light blue
    ( -2, (150, 210, 255, 120)),
    (  0, (0,   0,   0,   0)),     # zero — transparent
    (  2, (255, 210, 150, 120)),
    (  5, (255, 180, 100, 180)),   # weak cyclonic — light orange
    ( 10, (255, 80,  0,   210)),
    ( 20, (255, 0,   0,   230)),   # strong cyclonic — red
]

# Lightning density: flash/km²/min — used for NLDN (CONUS only).
# Kept here for future use.
_LIGHTNING_STOPS = [
    (0.0,  (0,   0,   0,   0)),
    (0.01, (255, 255, 0,   180)),
    (0.05, (255, 165, 0,   200)),
    (0.10, (255, 0,   0,   220)),
    (0.50, (255, 0,   255, 240)),
]

PRODUCTS = {
    'composite': {
        'stops': _REFL_STOPS,
        'vmin': -30, 'vmax': 80,
        'label': 'Composite Reflectivity (dBZ)',
        'units': 'dBZ',
        'cb_labels': ['-30', '0', '20', '40', '60', '80'],
        'missing': -99,
    },
    'mesh': {
        'stops': _MESH_STOPS,
        'vmin': 0.0, 'vmax': 3.0,
        'label': 'MESH — Max Estimated Hail Size (in)',
        'units': 'inches',
        'cb_labels': ['0', '0.5', '1.0', '1.5', '2.0', '2.5', '3.0+'],
        'missing': -99,
    },
    'mesh_max_30min': {
        'stops': _MESH_STOPS,
        'vmin': 0.0, 'vmax': 3.0,
        'label': 'MESH 30-min Max Hail Size (in)',
        'units': 'inches',
        'cb_labels': ['0', '0.5', '1.0', '1.5', '2.0', '2.5', '3.0+'],
        'missing': -99,
    },
    'mesh_max_60min': {
        'stops': _MESH_STOPS,
        'vmin': 0.0, 'vmax': 3.0,
        'label': 'MESH 1-hr Max Hail Size (in)',
        'units': 'inches',
        'cb_labels': ['0', '0.5', '1.0', '1.5', '2.0', '2.5', '3.0+'],
        'missing': -99,
    },
    'mesh_max_120min': {
        'stops': _MESH_STOPS,
        'vmin': 0.0, 'vmax': 3.0,
        'label': 'MESH 2-hr Max Hail Size (in)',
        'units': 'inches',
        'cb_labels': ['0', '0.5', '1.0', '1.5', '2.0', '2.5', '3.0+'],
        'missing': -99,
    },
    'mesh_max_240min': {
        'stops': _MESH_STOPS,
        'vmin': 0.0, 'vmax': 3.0,
        'label': 'MESH 4-hr Max Hail Size (in)',
        'units': 'inches',
        'cb_labels': ['0', '0.5', '1.0', '1.5', '2.0', '2.5', '3.0+'],
        'missing': -99,
    },
    'mesh_max_360min': {
        'stops': _MESH_STOPS,
        'vmin': 0.0, 'vmax': 3.0,
        'label': 'MESH 6-hr Max Hail Size (in)',
        'units': 'inches',
        'cb_labels': ['0', '0.5', '1.0', '1.5', '2.0', '2.5', '3.0+'],
        'missing': -99,
    },
    'mesh_max_1440min': {
        'stops': _MESH_STOPS,
        'vmin': 0.0, 'vmax': 3.0,
        'label': 'MESH 24-hr Max Hail Size (in)',
        'units': 'inches',
        'cb_labels': ['0', '0.5', '1.0', '1.5', '2.0', '2.5', '3.0+'],
        'missing': -99,
    },
    'azshear': {
        'stops': _AZSHEAR_STOPS,
        'vmin': -20, 'vmax': 20,
        'label': 'Azimuthal Shear 0–2 km AGL (×10⁻³ s⁻¹)',
        'units': '0.001/s',
        'cb_labels': ['-20', '-10', '-5', '0', '5', '10', '20'],
        'missing': 0,
    },
    'lightning': {
        'stops': _LIGHTNING_STOPS,
        'vmin': 0.0, 'vmax': 0.5,
        'label': 'CG Lightning Density (fl/km²/min)',
        'units': 'fl/km²/min',
        'cb_labels': ['0', '0.01', '0.05', '0.1', '0.5'],
        'missing': -1,
    },
}

# ── colormap interpolation ─────────────────────────────────────────────────────
def interp_color(stops, vmin, vmax, value):
    """Return (r,g,b,a) uint8 for a physical value."""
    s = stops
    if value <= s[0][0]:
        return s[0][1]
    if value >= s[-1][0]:
        return s[-1][1]
    for i in range(len(s) - 1):
        v0, c0 = s[i]
        v1, c1 = s[i + 1]
        if v0 <= value <= v1:
            f = (value - v0) / (v1 - v0) if v1 > v0 else 0.0
            return tuple(int(c0[k] + f * (c1[k] - c0[k])) for k in range(4))
    return (0, 0, 0, 0)


def build_lut(stops, vmin, vmax, n=4096):
    """Pre-build a lookup table mapping [0..n-1] → RGBA."""
    lut = np.zeros((n, 4), dtype=np.uint8)
    for i in range(n):
        v = vmin + (i / (n - 1)) * (vmax - vmin)
        lut[i] = interp_color(stops, vmin, vmax, v)
    return lut


# ── tile math ──────────────────────────────────────────────────────────────────
def ll_to_tile(lat, lon, zoom):
    """Convert lat/lon to tile x,y at given zoom."""
    n = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n)
    return x, y


def tile_to_ll_bounds(x, y, zoom):
    """Return (lat_max, lat_min, lon_min, lon_max) for tile."""
    n = 2 ** zoom
    lon_min = x / n * 360.0 - 180.0
    lon_max = (x + 1) / n * 360.0 - 180.0

    def y_to_lat(yi):
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yi / n))))

    lat_max = y_to_lat(y)
    lat_min = y_to_lat(y + 1)
    return lat_max, lat_min, lon_min, lon_max


# ── timestamp parsing ──────────────────────────────────────────────────────────
TS_RE = re.compile(r'(\d{8})-(\d{6})')

def parse_timestamp(path_str):
    """Extract timestamp from filename pattern YYYYMMDD-HHMMSS.
    Falls back to reading valid_time from GRIB2 metadata if filename
    contains no parseable timestamp (e.g. mktemp random names).
    """
    m = TS_RE.search(path_str)
    if m:
        date_s, time_s = m.group(1), m.group(2)
        dt = datetime(
            int(date_s[:4]), int(date_s[4:6]), int(date_s[6:8]),
            int(time_s[:2]), int(time_s[2:4]), int(time_s[4:6]),
            tzinfo=timezone.utc)
        stamp = f'{date_s[:4]}{date_s[4:6]}{date_s[6:8]}-{time_s[:2]}{time_s[2:4]}'
        return stamp, dt

    # Fallback: read valid_time from GRIB2 metadata
    log.debug('No timestamp in filename %s — reading from GRIB2 metadata', path_str)
    try:
        import cfgrib
        tmp = None
        src = str(path_str)
        if src.endswith('.gz'):
            tmp = tempfile.NamedTemporaryFile(suffix='.grib2', delete=False)
            with gzip.open(src, 'rb') as gz:
                shutil.copyfileobj(gz, tmp)
            tmp.close()
            src = tmp.name
        datasets = cfgrib.open_datasets(src,
                                         errors='ignore',
                                         backend_kwargs={'filter_by_keys': {}})
        if datasets:
            ds = datasets[0]
            var = list(ds.data_vars)[0]
            vt = ds[var].attrs.get('GRIB_validityDate'), ds[var].attrs.get('GRIB_validityTime')
            if vt[0] and vt[1] is not None:
                date_s = str(int(vt[0]))          # e.g. 20260322
                time_s = str(int(vt[1])).zfill(6) # e.g. 003000 → 003000
                dt = datetime(
                    int(date_s[:4]), int(date_s[4:6]), int(date_s[6:8]),
                    int(time_s[:2]), int(time_s[2:4]), int(time_s[4:6]),
                    tzinfo=timezone.utc)
                stamp = f'{date_s[:4]}{date_s[4:6]}{date_s[6:8]}-{time_s[:2]}{time_s[2:4]}'
                log.info('Timestamp from GRIB2 metadata: %s', stamp)
                return stamp, dt
    except Exception as e:
        log.warning('GRIB2 metadata timestamp read failed: %s', e)
    finally:
        if tmp and os.path.exists(tmp.name):
            os.unlink(tmp.name)

    return None, None


# ── GRIB2 reading ──────────────────────────────────────────────────────────────
def read_grib2(path):
    """
    Read first message from a GRIB2(.gz) file.
    Returns (data_2d, lats_1d, lons_1d) all as numpy arrays.
    Missing/fill values are replaced with np.nan.
    Requires cfgrib (eccodes backend).
    """
    import cfgrib
    tmp = None
    try:
        if str(path).endswith('.gz'):
            tmp = tempfile.NamedTemporaryFile(suffix='.grib2', delete=False)
            with gzip.open(path, 'rb') as gz:
                shutil.copyfileobj(gz, tmp)
            tmp.close()
            src = tmp.name
        else:
            src = str(path)

        datasets = cfgrib.open_datasets(src,
                                         errors='ignore',
                                         backend_kwargs={'filter_by_keys': {}})
        if not datasets:
            raise ValueError('No GRIB2 messages decoded')

        ds = datasets[0]
        var = list(ds.data_vars)[0]
        da  = ds[var]

        # MRMS GRIB2 uses 0 → 360 longitude convention
        lons = da.longitude.values
        lats = da.latitude.values
        data = da.values.astype(np.float32)

        # Convert fill / missing to nan
        fill = float(da.attrs.get('_FillValue', -999.0))
        data[data == fill] = np.nan
        data[data < -900]  = np.nan

        # Convert MESH from mm to inches if needed
        # MRMS MESH GRIB2 is stored in mm
        if 'mesh' in str(path).lower() and np.nanmax(data) > 20:
            data = data / 25.4   # mm → inches

        return data, lats, lons

    finally:
        if tmp and os.path.exists(tmp.name):
            os.unlink(tmp.name)


# ── tile rendering ─────────────────────────────────────────────────────────────
TILE_SIZE = 256

def render_tile(data, lats, lons, x, y, zoom, lut, vmin, vmax):
    """
    Render a single 256×256 RGBA PNG tile.
    Returns bytes or None if tile is entirely transparent.
    """
    from PIL import Image

    lat_max, lat_min, lon_min, lon_max = tile_to_ll_bounds(x, y, zoom)

    # Subset the data to the tile bbox (with a small buffer)
    dlat = (lats.max() - lats.min()) / lats.shape[0] if lats.ndim > 1 else abs(lats[1] - lats[0]) if len(lats) > 1 else 0.01
    dlon = (lons.max() - lons.min()) / lons.shape[1] if lons.ndim > 1 else abs(lons[1] - lons[0]) if len(lons) > 1 else 0.01
    buf = max(dlat, dlon) * 2

    if lats.ndim == 2:
        lat_1d = lats[:, 0]
        lon_1d = lons[0, :]
    else:
        lat_1d = lats
        lon_1d = lons

    # Handle 0→360 longitude
    if lon_1d.max() > 180:
        lon_1d = lon_1d - 360.0

    lat_mask = (lat_1d >= lat_min - buf) & (lat_1d <= lat_max + buf)
    lon_mask = (lon_1d >= lon_min - buf) & (lon_1d <= lon_max + buf)

    if not lat_mask.any() or not lon_mask.any():
        return None

    lat_idx = np.where(lat_mask)[0]
    lon_idx = np.where(lon_mask)[0]
    sub_data = data[np.ix_(lat_idx, lon_idx)]
    sub_lats = lat_1d[lat_idx]
    sub_lons = lon_1d[lon_idx]

    if np.all(np.isnan(sub_data)):
        return None

    n_lut = len(lut) - 1

    # ── Vectorized nearest-neighbour resampling ───────────────────────────────
    px_lons = lon_min + (np.arange(TILE_SIZE) + 0.5) / TILE_SIZE * (lon_max - lon_min)
    px_lats = lat_max - (np.arange(TILE_SIZE) + 0.5) / TILE_SIZE * (lat_max - lat_min)

    dlon_step = sub_lons[1] - sub_lons[0] if len(sub_lons) > 1 else dlon
    dlat_step = sub_lats[0] - sub_lats[1] if len(sub_lats) > 1 else dlat

    lx_arr = np.round((px_lons - sub_lons[0]) / dlon_step).astype(np.int32)
    ly_arr = np.round((sub_lats[0] - px_lats) / abs(dlat_step)).astype(np.int32)

    lx_oob = (lx_arr < 0) | (lx_arr >= len(sub_lons))
    ly_oob = (ly_arr < 0) | (ly_arr >= len(sub_lats))

    lx_arr = np.clip(lx_arr, 0, len(sub_lons) - 1)
    ly_arr = np.clip(ly_arr, 0, len(sub_lats) - 1)

    ly_2d = ly_arr[:, np.newaxis]
    lx_2d = lx_arr[np.newaxis, :]

    sampled = sub_data[ly_2d, lx_2d]

    oob_2d = ly_oob[:, np.newaxis] | lx_oob[np.newaxis, :]
    sampled = sampled.copy()
    sampled[oob_2d] = np.nan

    nan_mask = np.isnan(sampled)
    t = np.where(nan_mask, 0.0, np.clip((sampled - vmin) / (vmax - vmin), 0.0, 1.0))
    lut_idx = (t * n_lut).astype(np.int32)
    lut_idx[nan_mask] = 0

    tile_img = lut[lut_idx]
    tile_img = tile_img.copy()
    tile_img[nan_mask] = 0

    if tile_img[:, :, 3].max() == 0:
        return None

    img = Image.fromarray(tile_img, 'RGBA')
    import io
    buf_io = io.BytesIO()
    img.save(buf_io, 'PNG', optimize=True)
    return buf_io.getvalue()


# ── index management ───────────────────────────────────────────────────────────
def update_index(product, sector, timestamp, valid_dt, zoom_min, zoom_max):
    index_path = TILE_ROOT / product / sector / 'index.json'
    tile_url = f'/CAP_WxCOP/static/mrms_tiles/{product}/{sector}/{timestamp}/{{z}}/{{x}}/{{y}}.png'

    entry = {
        'timestamp':  timestamp,
        'valid_time': valid_dt.isoformat(),
        'rendered':   datetime.now(timezone.utc).isoformat(),
        'tile_url':   tile_url,
    }

    if index_path.exists():
        with open(index_path) as f:
            idx = json.load(f)
    else:
        idx = {
            'product':    product,
            'sector':     sector,
            'label':      PRODUCTS[product]['label'],
            'units':      PRODUCTS[product]['units'],
            'cb_labels':  PRODUCTS[product]['cb_labels'],
            'vmin':       PRODUCTS[product]['vmin'],
            'vmax':       PRODUCTS[product]['vmax'],
            'max_frames': MAX_FRAMES,
            'zoom_min':   zoom_min,
            'zoom_max':   zoom_max,
            'latest':     timestamp,
            'frames':     [],
        }

    # Remove duplicate timestamp if re-rendered
    idx['frames'] = [f for f in idx['frames'] if f['timestamp'] != timestamp]
    idx['frames'].append(entry)
    idx['frames'].sort(key=lambda f: f['timestamp'])
    idx['frames'] = idx['frames'][-MAX_FRAMES:]
    idx['latest'] = idx['frames'][-1]['timestamp']
    idx['label']  = PRODUCTS[product]['label']
    idx['units']  = PRODUCTS[product]['units']
    idx['cb_labels'] = PRODUCTS[product]['cb_labels']
    idx['vmin']   = PRODUCTS[product]['vmin']
    idx['vmax']   = PRODUCTS[product]['vmax']

    with open(index_path, 'w') as f:
        json.dump(idx, f, indent=2)
    # Update 'latest' symlink for weather map overlay
    if product == 'composite':
        latest_link = TILE_ROOT / product / sector / 'latest'
        if latest_link.is_symlink() or latest_link.exists():
            latest_link.unlink()
        latest_link.symlink_to(timestamp)


def scour_old_frames(product, sector):
    """Remove tile directories not referenced by the current index."""
    index_path = TILE_ROOT / product / sector / 'index.json'
    if not index_path.exists():
        return
    with open(index_path) as f:
        idx = json.load(f)
    keep = {fr['timestamp'] for fr in idx['frames']}
    sector_dir = TILE_ROOT / product / sector
    for d in sector_dir.iterdir():
        if d.is_dir() and d.name not in keep:
            shutil.rmtree(d, ignore_errors=True)
            log.info('Scoured old frame dir: %s', d)


# ── main render pipeline ───────────────────────────────────────────────────────
def render(product_key, sector, grib_path):
    if product_key not in PRODUCTS:
        log.error('Unknown product_key: %s', product_key)
        sys.exit(1)

    pdef = PRODUCTS[product_key]
    grib_path = Path(grib_path)
    if not grib_path.exists():
        log.error('GRIB2 file not found: %s', grib_path)
        sys.exit(1)

    timestamp, valid_dt = parse_timestamp(str(grib_path))
    if not timestamp:
        log.error('Could not parse timestamp from: %s', grib_path)
        sys.exit(1)

    out_dir = TILE_ROOT / product_key / sector / timestamp
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info('Rendering %s/%s  timestamp=%s  from %s', product_key, sector, timestamp, grib_path.name)

    try:
        data, lats, lons = read_grib2(grib_path)
    except Exception as e:
        log.error('GRIB2 read failed: %s', e)
        sys.exit(1)

    lut = build_lut(pdef['stops'], pdef['vmin'], pdef['vmax'])

    # Per-sector zoom cap: CONUS has a huge grid, cap at z8 to keep render time <30s.
    # OCONUS sectors are smaller — z9 is fine. All sectors floor at ZOOM_MIN.
    ZOOM_MAX_SECTOR = 10 if sector == 'CONUS' else ZOOM_MAX

    # Pre-compute 1-D lat/lon arrays once (used for tile range and bbox checks)
    if lats.ndim == 2:
        lat_1d_g = lats[:, 0]; lon_1d_g = lons[0, :]
    else:
        lat_1d_g = lats; lon_1d_g = lons.copy()
    if lon_1d_g.max() > 180:
        lon_1d_g = lon_1d_g - 360.0
    dlat_g = abs(lat_1d_g[1] - lat_1d_g[0]) if len(lat_1d_g) > 1 else 0.01
    dlon_g = abs(lon_1d_g[1] - lon_1d_g[0]) if len(lon_1d_g) > 1 else 0.01

    tiles_written = 0
    for zoom in range(ZOOM_MIN, ZOOM_MAX_SECTOR + 1):
        x_min, y_max = ll_to_tile(lat_1d_g.min(), lon_1d_g.min(), zoom)
        x_max, y_min = ll_to_tile(lat_1d_g.max(), lon_1d_g.max(), zoom)
        n = 2 ** zoom
        x_min = max(0, x_min - 1); x_max = min(n - 1, x_max + 1)
        y_min = max(0, y_min - 1); y_max = min(n - 1, y_max + 1)

        for tx in range(x_min, x_max + 1):
            for ty in range(y_min, y_max + 1):
                # Quick bbox pre-check: skip tile if no data falls within it
                lat_max_t, lat_min_t, lon_min_t, lon_max_t = tile_to_ll_bounds(tx, ty, zoom)
                buf = max(dlat_g, dlon_g)
                lat_mask = (lat_1d_g >= lat_min_t - buf) & (lat_1d_g <= lat_max_t + buf)
                lon_mask = (lon_1d_g >= lon_min_t - buf) & (lon_1d_g <= lon_max_t + buf)
                if not lat_mask.any() or not lon_mask.any():
                    continue
                # Check if any non-NaN data in this bbox
                li = np.where(lat_mask)[0]; lj = np.where(lon_mask)[0]
                if np.all(np.isnan(data[np.ix_(li, lj)])):
                    continue
                try:
                    png = render_tile(data, lats, lons, tx, ty, zoom,
                                      lut, pdef['vmin'], pdef['vmax'])
                except Exception as e:
                    log.warning('Tile z%d/%d/%d failed: %s', zoom, tx, ty, e)
                    continue
                if png is None:
                    continue
                tile_path = out_dir / str(zoom) / str(tx)
                tile_path.mkdir(parents=True, exist_ok=True)
                (tile_path / f'{ty}.png').write_bytes(png)
                tiles_written += 1

    log.info('Wrote %d tiles for %s/%s/%s', tiles_written, product_key, sector, timestamp)

    update_index(product_key, sector, timestamp, valid_dt, ZOOM_MIN, ZOOM_MAX)
    scour_old_frames(product_key, sector)
    log.info('Done: %s/%s/%s', product_key, sector, timestamp)


if __name__ == '__main__':
    if len(sys.argv) != 4:
        print(f'Usage: {sys.argv[0]} <product_key> <sector> <grib2_gz_path>')
        print(f'Products: {", ".join(PRODUCTS.keys())}')
        sys.exit(1)
    render(sys.argv[1], sys.argv[2], sys.argv[3])


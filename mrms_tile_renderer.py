#!/var/www/cap_winds_app/venv/bin/python3
"""
mrms_tile_renderer.py - MRMS GRIB2 to Leaflet PNG tile pyramid renderer

Called by mrms_render_pipe.sh on every LDM ingest. Renders tiles into a
timestamped directory, updates index.json, and prunes frames to keep exactly
MAX_FRAMES most recent frames. The map polls index.json every 4 minutes
and animates using pre-rendered static tiles - no per-request rendering.

Usage:
    mrms_tile_renderer.py <product_key> <sector> <grib2gz_path>

    product_key : composite | mesh | lightning | azshear
    sector      : CONUS | ALASKA | HAWAII | CARIB | GUAM

Output structure:
    <TILE_ROOT>/<product>/<sector>/<YYYYMMDD-HHMM>/3..8/{x}/{y}.png
    <TILE_ROOT>/<product>/<sector>/index.json
"""

import sys, os, gzip, json, math, shutil, tempfile, logging, argparse, traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pygrib
from PIL import Image

# -- Configuration -------------------------------------------------------------

_DIR        = Path(__file__).resolve().parent
TILE_ROOT   = '/LDM/radar/mrms_tiles'
LOG_FILE    = str(_DIR / 'logs' / 'mrms_renderer.log')

ZOOM_LEVELS      = [3, 4, 5, 6, 7, 8, 9, 10]  # z10 ~2.5km/tile; pixelated but operationally useful
TILE_SIZE        = 256
FILL_THRESH      = -990.0
MAX_FRAMES       = 30    # keep exactly this many frames; ~60 min at 2-min cycle
MAX_ZOOM_WORKERS = 4     # parallel zoom-level renders; leave headroom for concurrent products

# -- Colormaps: (value, R, G, B, A) breakpoints --------------------------------

_REFL = [
    (-30,   0,   0,   0,   0), (  5,   4, 233, 231, 200),
    ( 10,   1, 159, 244, 220), ( 15,   3,   0, 244, 220),
    ( 20,   3,   0, 244, 220), ( 25,   2, 253,   2, 220),
    ( 30,   0, 200,   0, 220), ( 35,   0, 144,   0, 220),
    ( 40, 255, 255,   0, 220), ( 45, 231, 192,   0, 220),
    ( 50, 255, 144,   0, 220), ( 55, 255,   0,   0, 220),
    ( 60, 214,   0,   0, 220), ( 65, 192,   0,   0, 220),
    ( 70, 255,   0, 255, 230), ( 75, 192,   0, 192, 230),
    ( 80, 255, 255, 255, 240),
]
_MESH = [
    (  0,   0,   0,   0,   0), (  1,   0, 255, 255, 180),
    (  6,   0, 200, 255, 200), ( 12,   0, 255,   0, 210),
    ( 19, 255, 255,   0, 220), ( 25, 255, 165,   0, 230),
    ( 38, 255,   0,   0, 230), ( 50, 200,   0, 200, 240),
    ( 75, 255, 255, 255, 250),
]
_LTNG = [
    (  0,   0,   0,   0,   0), (  5, 255, 255, 100, 160),
    ( 20, 255, 200,   0, 190), ( 40, 255, 140,   0, 210),
    ( 60, 255,   0,   0, 220), ( 80, 200,   0, 200, 230),
    (100, 255, 255, 255, 250),
]
# Azimuthal shear: bipolar colormap. Units: s⁻¹
# Negative = anticyclonic (blue), near-zero = transparent, positive = cyclonic (red)
# Operationally significant: |shear| > 0.005 s⁻¹; tornado-warning threshold ~0.02 s⁻¹
_AZSH = [
    (-0.050, 100,   0, 255, 230),   # strong anticyclonic: deep blue
    (-0.020,   0, 100, 255, 220),   # moderate anticyclonic: blue
    (-0.010,   0, 200, 255, 160),   # weak anticyclonic: light blue
    (-0.005,   0,   0,   0,   0),   # below noise floor: transparent
    ( 0.000,   0,   0,   0,   0),   # zero: transparent
    ( 0.005,   0,   0,   0,   0),   # below noise floor: transparent
    ( 0.010, 255, 200,   0, 160),   # weak cyclonic: yellow
    ( 0.020, 255, 100,   0, 220),   # moderate cyclonic: orange
    ( 0.030, 255,   0,   0, 230),   # significant cyclonic: red
    ( 0.050, 255, 255, 255, 240),   # extreme cyclonic: white
]

PRODUCTS = {
    'composite': {'cmap': _REFL, 'vmin': -30,    'vmax':  80,
                  'label': 'Composite Reflectivity (dBZ)'},
    'mesh':      {'cmap': _MESH, 'vmin':   0,    'vmax':  75,
                  'label': 'MESH Hail Size (mm)'},
    'lightning': {'cmap': _LTNG, 'vmin':   0,    'vmax': 100,
                  'label': 'Lightning Probability 60min (%)'},
    'azshear':   {'cmap': _AZSH, 'vmin': -0.05,  'vmax':  0.05,
                  'label': 'Azimuthal Shear 0-2km AGL (s⁻¹)'},
}

# -- Logging -------------------------------------------------------------------

def setup_logging():
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    logging.basicConfig(
        filename=LOG_FILE, level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
        datefmt='%Y-%m-%dT%H:%M:%SZ',
    )
    logging.getLogger().addHandler(logging.StreamHandler(sys.stderr))

log = logging.getLogger(__name__)

# -- Colormap LUT --------------------------------------------------------------

def _interp(cmap, v):
    if v <= cmap[0][0]:  return (0, 0, 0, 0)
    if v >= cmap[-1][0]: return cmap[-1][1:]
    for i in range(len(cmap) - 1):
        v0, r0, g0, b0, a0 = cmap[i]
        v1, r1, g1, b1, a1 = cmap[i+1]
        if v0 <= v <= v1:
            t = (v - v0) / (v1 - v0) if v1 > v0 else 0.0
            return (int(r0+t*(r1-r0)), int(g0+t*(g1-g0)),
                    int(b0+t*(b1-b0)), int(a0+t*(a1-a0)))
    return (0, 0, 0, 0)

def build_lut(cmap, vmin, vmax):
    lut = np.zeros((256, 4), dtype=np.uint8)
    for i in range(256):
        lut[i] = _interp(cmap, vmin + (vmax - vmin) * i / 255.0)
    return lut

# -- Tile math -----------------------------------------------------------------

def deg2tile(lat, lon, z):
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return x, y

def tile2deg(x, y, z):
    n = 2 ** z
    return (math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n)))),
            x / n * 360 - 180)

def merc_frac(lat):
    lr = math.radians(max(min(lat, 85.051129), -85.051129))
    return (1 - math.asinh(math.tan(lr)) / math.pi) / 2

# -- GRIB2 reading -------------------------------------------------------------

def read_grib2gz(path):
    """Return (data_2d, lat_1d_descending, lon_1d_ascending, valid_time)."""
    with gzip.open(path, 'rb') as gz:
        raw = gz.read()
    with tempfile.NamedTemporaryFile(suffix='.grib2', delete=False) as tmp:
        tmp.write(raw)
        tmp_path = tmp.name
    try:
        grbs   = pygrib.open(tmp_path)
        grb    = grbs[1]
        data   = grb.values.astype(np.float32)
        lats2d, lons2d = grb.latlons()
        try:
            vt = datetime(
                grb.validityDate // 10000,
                (grb.validityDate % 10000) // 100,
                grb.validityDate % 100,
                grb.validityTime // 100,
                grb.validityTime % 100,
                tzinfo=timezone.utc,
            )
        except Exception:
            vt = datetime.now(timezone.utc)
        grbs.close()
    finally:
        os.unlink(tmp_path)

    data[data < FILL_THRESH] = np.nan
    lons2d = np.where(lons2d > 180, lons2d - 360, lons2d)
    lat_1d = lats2d[:, 0]
    lon_1d = lons2d[0, :]
    if lat_1d[0] < lat_1d[-1]:
        data   = np.flipud(data)
        lat_1d = lat_1d[::-1]
    return data, lat_1d, lon_1d, vt

# -- Warp + slice --------------------------------------------------------------

STRIP_ROWS = 4

def _warp_strip(data, lat_1d, lon_1d, zoom, lut, cfg,
                x_min, x_max, y_start, y_end):
    nrows, ncols = data.shape
    cW = (x_max - x_min + 1) * TILE_SIZE
    cH = (y_end - y_start) * TILE_SIZE

    c_lat_max, c_lon_min = tile2deg(x_min,     y_start, zoom)
    c_lat_min, c_lon_max = tile2deg(x_max + 1, y_end,   zoom)

    out_lons  = np.linspace(c_lon_min, c_lon_max, cW, dtype=np.float32)
    merc_rows = np.linspace(merc_frac(c_lat_max), merc_frac(c_lat_min),
                            cH, dtype=np.float64)
    out_lats  = np.degrees(
        np.arctan(np.sinh(np.pi * (1 - 2 * merc_rows)))
    ).astype(np.float32)

    lat_asc  = lat_1d[::-1]
    raw_r    = np.searchsorted(lat_asc,
                               out_lats.clip(float(lat_asc[0]), float(lat_asc[-1])))
    src_rows = (nrows - 1) - np.clip(raw_r, 0, nrows - 1)
    src_cols = np.searchsorted(lon_1d,
                               out_lons.clip(float(lon_1d[0]), float(lon_1d[-1])))
    src_cols = np.clip(src_cols, 0, ncols - 1)

    strip    = data[np.ix_(src_rows, src_cols)]
    vmin, vmax = cfg['vmin'], cfg['vmax']
    nan_mask = np.isnan(strip)
    scaled   = np.clip((strip - vmin) / (vmax - vmin) * 255, 0, 255)
    scaled[nan_mask] = 0
    rgba     = lut[scaled.astype(np.uint8)]
    rgba[nan_mask] = [0, 0, 0, 0]
    return rgba


def render_zoom(data, lat_1d, lon_1d, zoom, lut, cfg, out_base):
    n = 2 ** zoom
    x_min, y_min = deg2tile(float(lat_1d[0]),  float(lon_1d[0]),  zoom)
    x_max, y_max = deg2tile(float(lat_1d[-1]), float(lon_1d[-1]), zoom)
    x_min = max(0, x_min);     y_min = max(0, y_min)
    x_max = min(n - 1, x_max); y_max = min(n - 1, y_max)

    rendered = empty = 0
    y = y_min
    while y <= y_max:
        y_end = min(y + STRIP_ROWS, y_max + 1)
        rgba  = _warp_strip(data, lat_1d, lon_1d, zoom, lut, cfg,
                            x_min, x_max, y, y_end)
        for xi, tx in enumerate(range(x_min, x_max + 1)):
            cs = xi * TILE_SIZE;  ce = cs + TILE_SIZE
            for yi, ty in enumerate(range(y, y_end)):
                rs = yi * TILE_SIZE;  re = rs + TILE_SIZE
                patch = rgba[rs:re, cs:ce]
                if patch.shape == (TILE_SIZE, TILE_SIZE, 4) and patch[:, :, 3].any():
                    tdir = out_base / str(zoom) / str(tx)
                    tdir.mkdir(parents=True, exist_ok=True)
                    Image.fromarray(patch, 'RGBA').save(tdir / f'{ty}.png', 'PNG')
                    rendered += 1
                else:
                    empty += 1
        y = y_end
    return rendered, empty

# -- index.json management -----------------------------------------------------

def ts_to_dirname(dt):
    return dt.strftime('%Y%m%d-%H%M')

def dirname_to_dt(name):
    try:
        return datetime.strptime(name, '%Y%m%d-%H%M').replace(tzinfo=timezone.utc)
    except ValueError:
        return None

def update_index(product_key, sector, valid_time, cfg):
    """
    Append new frame, prune oldest frames beyond MAX_FRAMES, write atomically.
    Frame count is fixed at MAX_FRAMES so the animation always has the same
    number of frames to display regardless of poll timing.
    Returns the final index dict.
    """
    sector_up  = sector.upper()
    base       = Path(TILE_ROOT) / product_key / sector_up
    index_path = base / 'index.json'
    tmp_path   = base / 'index.json.tmp'
    now_utc    = datetime.now(timezone.utc)

    frames = []
    if index_path.exists():
        try:
            frames = json.loads(index_path.read_text()).get('frames', [])
        except Exception as e:
            log.warning(f"index.json unreadable, starting fresh: {e}")

    ts_name  = ts_to_dirname(valid_time)
    tile_url = (f'/CAP_WxCOP/static/mrms_tiles/{product_key}/{sector_up}'
                f'/{ts_name}/{{z}}/{{x}}/{{y}}.png')

    # Replace any existing entry for this timestamp
    frames = [f for f in frames if f.get('timestamp') != ts_name]
    frames.append({
        'timestamp':  ts_name,
        'valid_time': valid_time.isoformat(),
        'rendered':   now_utc.isoformat(),
        'tile_url':   tile_url,
    })

    # Sort by timestamp, then trim to MAX_FRAMES keeping the newest
    frames.sort(key=lambda f: f.get('timestamp', ''))
    if len(frames) > MAX_FRAMES:
        to_remove = frames[:-MAX_FRAMES]
        frames    = frames[-MAX_FRAMES:]
        for f in to_remove:
            old_dir = base / f.get('timestamp', '')
            if old_dir.is_dir():
                try:
                    shutil.rmtree(old_dir)
                    log.info(f"Pruned {old_dir}")
                except Exception as e:
                    log.warning(f"Could not prune {old_dir}: {e}")

    index = {
        'product':    product_key,
        'sector':     sector_up,
        'label':      cfg['label'],
        'max_frames': MAX_FRAMES,
        'zoom_min':   min(ZOOM_LEVELS),
        'zoom_max':   max(ZOOM_LEVELS),
        'latest':     frames[-1]['timestamp'] if frames else ts_name,
        'frames':     frames,
    }

    base.mkdir(parents=True, exist_ok=True)
    tmp_path.write_text(json.dumps(index, indent=2))
    tmp_path.rename(index_path)
    return index

# -- Main pipeline -------------------------------------------------------------

def _render_zoom_task(args):
    """Module-level wrapper so ProcessPoolExecutor can pickle it."""
    data, lat_1d, lon_1d, zoom, lut, cfg, out_base = args
    t0 = datetime.now()
    r, e = render_zoom(data, lat_1d, lon_1d, zoom, lut, cfg, out_base)
    dt = (datetime.now() - t0).total_seconds()
    return r, e, dt


def render_product(product_key, sector, grib_path):
    cfg = PRODUCTS[product_key]
    lut = build_lut(cfg['cmap'], cfg['vmin'], cfg['vmax'])

    log.info(f"START {product_key}/{sector} <- {Path(grib_path).name}")
    data, lat_1d, lon_1d, vt = read_grib2gz(grib_path)
    log.info(f"  Grid {data.shape}, valid {vt.isoformat()}, "
             f"range {np.nanmin(data):.4f}..{np.nanmax(data):.4f}")

    ts_name  = ts_to_dirname(vt)
    out_base = Path(TILE_ROOT) / product_key / sector.upper() / ts_name

    tot_r = tot_e = 0

    # Render zoom levels in parallel — each zoom is fully independent.
    # ProcessPoolExecutor avoids the GIL for numpy-heavy work.
    # Pass all args as a tuple to the module-level _render_zoom_task helper
    # (nested functions cannot be pickled by multiprocessing).
    zoom_args = [(data, lat_1d, lon_1d, z, lut, cfg, out_base) for z in ZOOM_LEVELS]
    with ProcessPoolExecutor(max_workers=MAX_ZOOM_WORKERS) as pool:
        futures = {pool.submit(_render_zoom_task, args): args[3] for args in zoom_args}
        results = {}
        for fut in as_completed(futures):
            zoom = futures[fut]
            try:
                r, e, dt = fut.result()
                results[zoom] = (r, e, dt)
            except Exception as exc:
                log.error(f"  z{zoom}: FAILED — {exc}")
                results[zoom] = (0, 0, 0)

    for zoom in sorted(results):
        r, e, dt = results[zoom]
        log.info(f"  z{zoom}: {r} tiles — {dt:.1f}s")
        tot_r += r

    index = update_index(product_key, sector, vt, cfg)
    log.info(f"DONE {product_key}/{sector}/{ts_name}: "
             f"{tot_r} tiles, {len(index['frames'])}/{MAX_FRAMES} frames in index")
    return index

# -- Entry point ---------------------------------------------------------------

if __name__ == '__main__':
    setup_logging()
    p = argparse.ArgumentParser(description='MRMS GRIB2 tile renderer')
    p.add_argument('product_key', choices=list(PRODUCTS.keys()))
    p.add_argument('sector')
    p.add_argument('grib_path')
    args = p.parse_args()

    if not os.path.exists(args.grib_path):
        log.error(f"Not found: {args.grib_path}")
        sys.exit(1)

    try:
        result = render_product(args.product_key, args.sector, args.grib_path)
        print(json.dumps(result, indent=2))
    except Exception as e:
        log.error(f"FAILED: {e}\n{traceback.format_exc()}")
        sys.exit(1)


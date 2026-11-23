#!/usr/bin/env python3
"""
Crops the non-map legend/title margin off each raw FAA sectional chart
before reprojection -- see data/sectionals/README.md "Known limitation"
section (now being fixed, not just documented). FAA's distributed
GeoTIFFs embed a real, georeferenced legend/title panel along the
chart's west edge (title block, locator inset, legend color scale,
QR code, etc.) as if it were map content -- this is what was bleeding
through as ugly borders/legend boxes in the tiled mosaic, and blocking
real chart content from showing in areas where two charts overlap.

The margin's exact width varies chart-to-chart (not a fixed fraction --
measured 9.4%-37.8% across a 5-chart sample on 2026-11-21), so this
detects it per chart rather than using one constant crop.

Per FAA's own digital-chart documentation: "The area inside the neat
line is georeferenced to the surface of the earth. Only the main body
of the chart is accurately georeferenced." The neatline -- the actual
printed rectangle bounding the real chart body -- is the trim line on
all four sides, not just the big west-edge legend column (confirmed
2026-11-21 that smaller margins also exist on other edges on some
charts, e.g. a thin top collar, a bottom info strip).

Detection approach: downsample to a working resolution, then for each
row/column compute the fraction of pixels that look like "page
background" (bright AND low-saturation -- i.e. white/cream/light-blue;
real chart content, by contrast, is densely colored almost everywhere,
whatever its basemap color happens to be). Independently scan inward
from each of the four edges for where a sustained LOW-background-
fraction run begins -- that's the real chart's neatline on that side.
Any edge with no confident boundary found is left uncropped on that
side (most charts only need the west trim; the heuristic doesn't force
a crop where there's nothing to remove). Validated visually against
charts with very different backgrounds (tan terrain, mountainous AK,
blue-ocean Caribbean) before being applied pipeline-wide.

UPDATE 2026-11-23: the above was still leaving a buffer short of the
true neatline on some charts, not the neatline itself (Gerry, live on
AvTrack: "poorly clipped sectionals because you're not cropping on the
neat line but on its buffer"). Root cause, confirmed visually on the
Chicago chart: each chart's margin usually contains small, LOCALIZED
graphics near its corners (an inset scale bar, a locator index map, a
date/QR box) that are dense/non-background enough to flip a single
row's or column's AGGREGATE background fraction below threshold well
before the real chart body starts -- a one-off, pixel-exact artifact
of mixing 2-D content into a 1-D row/column scan, not a tuning
mistake. (A from-the-metadata shortcut was also tried and rejected:
each chart's own .htm "Bounding_Coordinates" turned out to describe
the FULL raster's geographic extent, margin included, not the inner
neatline -- confirmed by gdalwarp'ing straight to that bbox and still
seeing the whole legend panel.) Two targeted fixes, not a rewrite:

1. SAFETY_PAD_FRAC -- after the scan finds a boundary, push it further
   in by a small fraction of that dimension. This is deliberately a
   blunt instrument: it sacrifices a thin sliver of real edge content
   to guarantee no corner artifact survives, rather than trying to
   model every possible small margin graphic. Confirmed cheap: FAA
   sectionals overlap their neighbors generously by design (checked
   Chicago's west edge against Omaha's east edge via their published
   Bounding_Coordinates -- padding this much still leaves roughly a
   quarter-degree of real overlap, so the mosaic has no gap).
2. A relaxed-threshold fallback ladder for the opposite failure (seen
   on Lake_Huron, which detected NO margin on any edge and fell back
   to the fully-uncropped source, legend and all): pale/low-contrast
   terrain near the legend can keep a column's background fraction
   hovering right at the threshold long enough that the strict scan
   never confirms. Retried at progressively looser thresholds instead
   of giving up outright.
"""
import logging
import os
import subprocess
import sys

import numpy as np
from PIL import Image

RAW_BASE = '/var/www/cap_winds_app/data/sectionals/raw'
OUT_BASE = '/var/www/cap_winds_app/data/sectionals/cropped'
LOG_PATH = '/var/www/cap_winds_app/logs/crop_sectional_margins.log'
PREVIEW_WIDTH = 1600
WINDOW = 40
BG_THRESHOLD = 0.25
TAIL_LEN = 300
MAX_SEARCH_FRACTION = 0.45
# Extra crop pushed past the detected boundary, as a fraction of that edge's
# full dimension -- swallows small corner artifacts the scan's 1-D view
# can't see coming (see module docstring, 2026-11-23 update).
SAFETY_PAD_FRAC = 0.04
# Fallback ladder when the strict (WINDOW, BG_THRESHOLD, TAIL_LEN) scan finds
# nothing at all on an edge -- each tuple is (bg_threshold, window, tail_len),
# tried in order until one finds a boundary.
FALLBACK_LADDER = [
    (0.30, 30, 250),
    (0.35, 25, 200),
    (0.40, 20, 150),
    (0.45, 15, 120),
]

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler(LOG_PATH)],
)
log = logging.getLogger(__name__)


def _find_boundary_at(bg_frac, bg_threshold, window, tail_len):
    n = len(bg_frac)
    limit = int(n * MAX_SEARCH_FRACTION) - window
    for x in range(0, max(limit, 0)):
        if bg_frac[x:x + window].mean() < bg_threshold:
            tail = bg_frac[x:min(x + tail_len, n)]
            if tail.mean() < bg_threshold + 0.1:
                return x
    return None


def _find_boundary(bg_frac):
    """Scans a 1-D background-fraction array from index 0 inward for
    where a sustained low-background (real chart content) run begins,
    at the strict default threshold first, then progressively looser
    ones (FALLBACK_LADDER) if that finds nothing -- a chart with pale,
    low-contrast terrain near its margin (e.g. Lake_Huron) can otherwise
    never confirm a boundary at all and fall back to fully uncropped.
    Returns an offset from index 0, or None if nothing found even at
    the loosest setting."""
    boundary = _find_boundary_at(bg_frac, BG_THRESHOLD, WINDOW, TAIL_LEN)
    if boundary is not None:
        return boundary
    for bg_threshold, window, tail_len in FALLBACK_LADDER:
        boundary = _find_boundary_at(bg_frac, bg_threshold, window, tail_len)
        if boundary is not None:
            return boundary
    return None


def detect_neatline_box(preview_path):
    """Returns (left, top, right, bottom) margin widths in PREVIEW
    pixels to trim from each of the four edges (0 if none detected on
    that side) -- the neatline is a rectangle, not just a west-edge
    legend column (per FAA: only the area inside it is accurately
    georeferenced, see module docstring). Each detected boundary gets
    an extra SAFETY_PAD_FRAC pushed in past it, to swallow small
    corner artifacts the scan can't otherwise see (see module
    docstring, 2026-11-23 update) -- padding is skipped on a side
    where no boundary was found at all, since there's nothing to push
    past and padding a zero would just eat real content for no reason."""
    img = Image.open(preview_path).convert('RGB')
    arr = np.array(img).astype(int)
    h, w, _ = arr.shape
    maxc = arr.max(axis=2)
    minc = arr.min(axis=2)
    sat = maxc - minc
    bright = maxc
    is_bg = (bright > 225) & (sat < 25)

    col_bg_frac = is_bg.mean(axis=0)   # length w, for left/right scans
    row_bg_frac = is_bg.mean(axis=1)   # length h, for top/bottom scans

    left = _find_boundary(col_bg_frac)
    right = _find_boundary(col_bg_frac[::-1])
    top = _find_boundary(row_bg_frac)
    bottom = _find_boundary(row_bg_frac[::-1])

    left = left + int(w * SAFETY_PAD_FRAC) if left else 0
    right = right + int(w * SAFETY_PAD_FRAC) if right else 0
    top = top + int(h * SAFETY_PAD_FRAC) if top else 0
    bottom = bottom + int(h * SAFETY_PAD_FRAC) if bottom else 0
    return left, top, right, bottom


def crop_one(name, src_tif):
    out_tif = os.path.join(OUT_BASE, name + '.tif')
    if os.path.exists(out_tif) and os.path.getmtime(out_tif) > os.path.getmtime(src_tif):
        log.info('SKIP %-28s already cropped, newer than source', name)
        return True

    preview = '/tmp/_sectional_crop_preview_%s.png' % name
    subprocess.run(
        ['gdal_translate', '-of', 'PNG', '-outsize', str(PREVIEW_WIDTH), '0', '-q', src_tif, preview],
        check=True,
    )
    try:
        preview_w, preview_h = Image.open(preview).size
        left, top, right, bottom = detect_neatline_box(preview)
        if (left, top, right, bottom) == (0, 0, 0, 0):
            log.warning('NO MARGIN DETECTED for %-28s -- using uncropped source', name)
            subprocess.run(['cp', src_tif, out_tif], check=True)
            return True

        info = subprocess.run(['gdalinfo', '-json', src_tif], capture_output=True, text=True, check=True)
        import json as _json
        real_w, real_h = _json.loads(info.stdout)['size']

        xoff = int((left / preview_w) * real_w)
        yoff = int((top / preview_h) * real_h)
        right_px = int((right / preview_w) * real_w)
        bottom_px = int((bottom / preview_h) * real_h)
        xsize = real_w - xoff - right_px
        ysize = real_h - yoff - bottom_px

        log.info('%-28s crop L=%d T=%d R=%d B=%d (of %dx%d, %.1f%%/%.1f%%/%.1f%%/%.1f%%)',
                  name, xoff, yoff, right_px, bottom_px, real_w, real_h,
                  100 * xoff / real_w, 100 * yoff / real_h,
                  100 * right_px / real_w, 100 * bottom_px / real_h)
        subprocess.run(
            ['gdal_translate', '-srcwin', str(xoff), str(yoff), str(xsize), str(ysize),
             '-co', 'COMPRESS=LZW', '-q', src_tif, out_tif],
            check=True,
        )
        return True
    finally:
        if os.path.exists(preview):
            os.remove(preview)


def main():
    try:
        os.nice(15)
    except OSError:
        pass
    os.makedirs(OUT_BASE, exist_ok=True)
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

    dirs = sorted(d for d in os.listdir(RAW_BASE) if os.path.isdir(os.path.join(RAW_BASE, d)))
    ok, fail = 0, []
    for i, name in enumerate(dirs, 1):
        chart_dir = os.path.join(RAW_BASE, name)
        tifs = [f for f in os.listdir(chart_dir) if f.lower().endswith(('.tif', '.tiff'))]
        if not tifs:
            log.warning('[%d/%d] %-28s no .tif found, skipping', i, len(dirs), name)
            fail.append(name)
            continue
        src_tif = os.path.join(chart_dir, tifs[0])
        log.info('[%d/%d] %s', i, len(dirs), name)
        try:
            if crop_one(name, src_tif):
                ok += 1
            else:
                fail.append(name)
        except Exception:
            log.exception('FAILED cropping %s', name)
            fail.append(name)

    log.info('=== crop pass done: ok=%d fail=%d ===', ok, len(fail))
    if fail:
        log.error('FAILED: %s', ', '.join(fail))


if __name__ == '__main__':
    main()

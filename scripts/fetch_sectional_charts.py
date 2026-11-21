#!/usr/bin/env python3
"""
Downloads every current FAA VFR Sectional chart (CONUS + Alaska +
Hawaii + the Caribbean/Puerto Rico-VI pair) as real georeferenced
GeoTIFFs, for self-hosting as a map layer with no Esri-style zoom cap.

Fetch mechanism (XML lookup -> zip -> unzip -> .tif/.tfw/.htm) is
exactly as documented by Charlie Jackson, Capt, PCR-WA-068
(charles.jackson@wawg.cap.gov), creator of https://capgrids.com, who
shared this directly with Gerry 2026-11-21. Credit due in full --
see README.md in this directory.

No Guam/Mariana Islands sectional exists in FAA's current catalog
(confirmed via aeronav.faa.gov directory listing, 2026-11-19) -- not
an omission here, FAA just doesn't publish one. Nor does a "Wall
Planning Chart" overview product have any currently-live download URL
(checked 2026-11-21) -- low-zoom overview is handled by the tile
pyramid/basemap instead, not a separate chart.

The Caribbean 1/2 charts (which cover Puerto Rico/US Virgin Islands --
there's no sectional literally named "Puerto Rico-Virgin Islands")
are special-cased: external-api.faa.gov's own XML lookup for them
resolves to a dead URL (sectional-files/Caribbean_N.zip, confirmed
404), while the real files live one directory over, with a _VFR
suffix (.../Caribbean/Caribbean_N_VFR.zip). Verified live 2026-11-21.
We still call the API for these two, purely to read the current
edition date, then build the real URL from that date ourselves --
same as the AutoDOLE/cron-safety idea Charlie uses, so this doesn't
silently rot the next time the edition folder rolls over.

Double duty: this is BOTH the one-time backfill (ran 2026-11-21, 55/55
ok) AND the daily update-check cron job. Each chart's resolved edition
date is tracked in manifest.json; a chart is only re-fetched when its
edition date has actually changed, so running this daily costs nothing
on days nothing changed. Sequential, one chart at a time -- polite to
FAA's server, same spirit as Charlie's own verified-working daily cron.
"""
import json
import logging
import os
import re
import sys
import time
import zipfile
import urllib.request

LOG_PATH = '/var/www/cap_winds_app/logs/fetch_sectional_charts.log'
OUT_BASE = '/var/www/cap_winds_app/data/sectionals/raw'
MANIFEST_PATH = '/var/www/cap_winds_app/data/sectionals/manifest.json'
USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) CAP-WxCOP-sectional-fetch/1.0'
API_BASE = 'https://external-api.faa.gov/apra/vfr/sectional/chart'
DELAY_BETWEEN_CHARTS_SEC = 3

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler(LOG_PATH)],
)
log = logging.getLogger(__name__)

CONUS = [
    'Albuquerque', 'Atlanta', 'Billings', 'Brownsville', 'Charlotte',
    'Cheyenne', 'Chicago', 'Cincinnati', 'Dallas-Ft Worth', 'Denver',
    'Detroit', 'El Paso', 'Great Falls', 'Green Bay', 'Halifax',
    'Houston', 'Jacksonville', 'Kansas City', 'Klamath Falls',
    'Lake Huron', 'Las Vegas', 'Los Angeles', 'Memphis', 'Miami',
    'Montreal', 'New Orleans', 'New York', 'Omaha', 'Phoenix',
    'Salt Lake City', 'San Antonio', 'San Francisco', 'Seattle',
    'St Louis', 'Twin Cities', 'Washington', 'Wichita',
]

ALASKA = [
    'Anchorage', 'Bethel', 'Cape Lisburne', 'Cold Bay', 'Dawson',
    'Dutch Harbor', 'Fairbanks', 'Juneau', 'Ketchikan', 'Kodiak',
    'McGrath', 'Nome', 'Point Barrow', 'Seward',
    'Western Aleutian Islands',
]

HAWAII = ['Hawaiian Islands']

# geoname -> real filename stem under .../<edition>/Caribbean/, since
# the API's own product url for these two is stale (see module docstring).
CARIBBEAN = {
    'Caribbean 1': 'Caribbean_1_VFR',
    'Caribbean 2': 'Caribbean_2_VFR',
}

ALL_API_GEONAMES = CONUS + ALASKA + HAWAII


def http_get(url, dest_path=None):
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    if dest_path:
        with open(dest_path, 'wb') as f:
            f.write(data)
    return data


def resolve_product(geoname):
    """Returns (zip_url, edition_date_str e.g. '10/29/2026')."""
    url = API_BASE + '?geoname=' + geoname.replace(' ', '+') + '&edition=current&format=tiff'
    xml = http_get(url).decode('utf-8', errors='replace')
    url_m = re.search(r'<product[^>]+url="([^"]+)"', xml)
    date_m = re.search(r'<editionDate>([^<]+)</editionDate>', xml)
    if not url_m or not date_m:
        raise RuntimeError('unexpected API response for %r: %s' % (geoname, xml[:300]))
    return url_m.group(1), date_m.group(1)


def resolve_caribbean(geoname, filename_stem):
    """Real file lives outside the API's own (stale) product url -- see
    module docstring -- so we only use the API for the edition date,
    then build the actual Caribbean/ URL from it ourselves."""
    _stale_url, edition_date = resolve_product(geoname)
    mm, dd, yyyy = edition_date.split('/')
    edition_folder = '%s-%s-%s' % (mm, dd, yyyy)
    zip_url = 'https://aeronav.faa.gov/visual/%s/Caribbean/%s.zip' % (edition_folder, filename_stem)
    return zip_url, edition_date


def load_manifest():
    if os.path.exists(MANIFEST_PATH):
        with open(MANIFEST_PATH) as f:
            return json.load(f)
    return {}


def save_manifest(manifest):
    tmp = MANIFEST_PATH + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    os.replace(tmp, MANIFEST_PATH)


def fetch_one(geoname, zip_url):
    safe_name = geoname.replace(' ', '_').replace('/', '-')
    out_dir = os.path.join(OUT_BASE, safe_name)
    os.makedirs(out_dir, exist_ok=True)

    for f in os.listdir(out_dir):
        os.remove(os.path.join(out_dir, f))

    zip_path = os.path.join(out_dir, safe_name + '.zip')
    log.info('FETCH %-28s %s', geoname, zip_url)
    http_get(zip_url, dest_path=zip_path)
    size_mb = os.path.getsize(zip_path) / 1e6
    log.info('  downloaded %.1f MB', size_mb)

    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(out_dir)
    os.remove(zip_path)

    extracted = os.listdir(out_dir)
    has_tif = any(f.lower().endswith(('.tif', '.tiff')) for f in extracted)
    has_tfw = any(f.lower().endswith('.tfw') for f in extracted)
    if not has_tif:
        log.error('  NO TIF FOUND after extracting %s -- contents: %s', geoname, extracted)
        return False
    log.info('  OK %-28s tif=%s tfw=%s', geoname, has_tif, has_tfw)
    return True


def process_chart(geoname, resolver, manifest, counts):
    try:
        zip_url, edition_date = resolver(geoname)
    except Exception:
        log.exception('FAILED resolving %s', geoname)
        counts['failed'].append(geoname)
        return

    prev = manifest.get(geoname, {})
    if prev.get('edition_date') == edition_date and prev.get('ok'):
        log.info('CURRENT %-28s edition %s unchanged, skipping', geoname, edition_date)
        counts['unchanged'] += 1
        return

    log.info('%-28s edition %s -> fetching (had %s)', geoname, edition_date, prev.get('edition_date', 'nothing'))
    ok = fetch_one(geoname, zip_url)
    manifest[geoname] = {
        'edition_date': edition_date,
        'fetched_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'ok': ok,
    }
    save_manifest(manifest)
    if ok:
        counts['fetched'] += 1
    else:
        counts['failed'].append(geoname)


def main():
    os.makedirs(OUT_BASE, exist_ok=True)
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    manifest = load_manifest()

    total = len(ALL_API_GEONAMES) + len(CARIBBEAN)
    counts = {'fetched': 0, 'unchanged': 0, 'failed': []}

    log.info('=== sectional update-check: %d charts (CONUS=%d AK=%d HI=%d Caribbean=%d) ===',
              total, len(CONUS), len(ALASKA), len(HAWAII), len(CARIBBEAN))

    for i, geoname in enumerate(ALL_API_GEONAMES, 1):
        log.info('[%d/%d] %s', i, total, geoname)
        process_chart(geoname, resolve_product, manifest, counts)
        time.sleep(DELAY_BETWEEN_CHARTS_SEC)

    for i, (geoname, stem) in enumerate(CARIBBEAN.items(), len(ALL_API_GEONAMES) + 1):
        log.info('[%d/%d] %s', i, total, geoname)
        process_chart(geoname, lambda g, s=stem: resolve_caribbean(g, s), manifest, counts)
        time.sleep(DELAY_BETWEEN_CHARTS_SEC)

    log.info('=== DONE: %d fetched, %d unchanged, %d failed ===',
              counts['fetched'], counts['unchanged'], len(counts['failed']))
    if counts['failed']:
        log.error('FAILED charts (%d): %s', len(counts['failed']), ', '.join(counts['failed']))


if __name__ == '__main__':
    main()

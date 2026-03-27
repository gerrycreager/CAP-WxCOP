#!/usr/bin/env python3
"""
update_airspace_geo.py - Weekly FAA Airspace GeoJSON Updater
=============================================================
Downloads Special Use Airspace (SUA) and Military Training Route (MTR)
GeoJSON from FAA AIS Open Data and saves to the static/geo/ directory
for use by the WxCOP weather map.

Datasets:
  SUA - Special Use Airspace (Prohibited, Restricted, Warning, Alert, MOA)
        FAA ADDS item: dd0d1b726e504137ab3c41b21835d05b_0
        Covers CONUS, Puerto Rico, Virgin Islands
        Updated: each 56-day AIRAC cycle

  MTR - Military Training Route Segments
        FAA ADDS item: 0c6899de28af447c801231ed7ba7baa6_0
        IR (Instrument), VR (Visual), SR (Slow) route segments
        Covers CONUS, Puerto Rico, Virgin Islands

Cron (r815, run as www-data or root):
  30 3 * * 0  /var/www/cap_winds_app/scripts/update_airspace_geo.py \
              >> /var/log/airspace_geo_update.log 2>&1

Output:
  /var/www/cap_winds_app/static/geo/sua.geojson   (~500 KB)
  /var/www/cap_winds_app/static/geo/mtr.geojson   (~2 MB)

Note: FAA ADDS uses hub.arcgis.com since June 2024 (Esri migration).
      Old opendata.arcgis.com URLs will redirect but may be slower.
"""

import os
import sys
import json
import time
import logging
import requests
import tempfile
import shutil
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OUTPUT_DIR = Path('/var/www/cap_winds_app/static/geo')

# FAA AIS hub.arcgis.com GeoJSON download URLs (updated Sept 2024 format)
DATASETS = {
    'sua': {
        'url': ('https://hub.arcgis.com/api/v3/datasets/'
                'dd0d1b726e504137ab3c41b21835d05b_0/'
                'downloads/data?format=geojson&spatialRefId=4326&where=1=1'),
        'output': 'sua.geojson',
        'description': 'Special Use Airspace (Prohibited/Restricted/MOA/Warning/Alert)',
    },
    'mtr': {
        'url': ('https://hub.arcgis.com/api/v3/datasets/'
                '0c6899de28af447c801231ed7ba7baa6_0/'
                'downloads/data?format=geojson&spatialRefId=4326&where=1=1'),
        'output': 'mtr.geojson',
        'description': 'Military Training Routes (IR/VR/SR segments)',
    },
}

TIMEOUT_SECS  = 300   # 5 minutes per download (files can be large)
RETRY_LIMIT   = 3
RETRY_DELAY   = 30    # seconds between retries

# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------

def download_geojson(name, url, output_path, description):
    """
    Download a GeoJSON file from the FAA AIS hub with retry logic.
    Writes to a temp file first, then atomically moves to output_path
    so the serving copy is never partially written.
    """
    log.info(f"[{name}] Downloading: {description}")
    log.info(f"[{name}] URL: {url}")

    for attempt in range(1, RETRY_LIMIT + 1):
        try:
            t0 = time.time()
            resp = requests.get(url, timeout=TIMEOUT_SECS, stream=True)
            resp.raise_for_status()

            # Write to temp file in same directory for atomic rename
            tmp_fd, tmp_path = tempfile.mkstemp(dir=output_path.parent,
                                                 suffix='.geojson.tmp')
            bytes_written = 0
            with os.fdopen(tmp_fd, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)
                    bytes_written += len(chunk)

            elapsed = time.time() - t0

            # Validate it's parseable JSON with features
            with open(tmp_path) as f:
                data = json.load(f)
            feature_count = len(data.get('features', []))
            if feature_count == 0:
                raise ValueError(f"GeoJSON has 0 features — likely a bad response")

            # Atomic replace
            shutil.move(tmp_path, output_path)
            log.info(f"[{name}] OK: {feature_count} features, "
                     f"{bytes_written/1024:.0f} KB in {elapsed:.1f}s -> {output_path}")
            return True

        except Exception as e:
            log.error(f"[{name}] Attempt {attempt}/{RETRY_LIMIT} failed: {e}")
            if 'tmp_path' in locals() and os.path.exists(tmp_path):
                os.unlink(tmp_path)
            if attempt < RETRY_LIMIT:
                log.info(f"[{name}] Retrying in {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)

    log.error(f"[{name}] All {RETRY_LIMIT} attempts failed — keeping existing file")
    return False


def write_metadata(output_dir, results):
    """Write a metadata JSON file with download timestamps and feature counts."""
    meta_path = output_dir / 'airspace_meta.json'
    meta = {
        'updated': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        'datasets': {}
    }
    for name, success in results.items():
        geo_path = output_dir / DATASETS[name]['output']
        if geo_path.exists():
            try:
                with open(geo_path) as f:
                    data = json.load(f)
                meta['datasets'][name] = {
                    'features': len(data.get('features', [])),
                    'size_kb': round(geo_path.stat().st_size / 1024, 1),
                    'last_download': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
                    'success': success,
                }
            except Exception:
                meta['datasets'][name] = {'success': False}

    with open(meta_path, 'w') as f:
        json.dump(meta, f, indent=2)
    log.info(f"Metadata written to {meta_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("=" * 60)
    log.info(f"FAA Airspace GeoJSON update started: "
             f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%SZ')}")
    log.info("=" * 60)

    # Ensure output directory exists
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    results = {}
    for name, cfg in DATASETS.items():
        output_path = OUTPUT_DIR / cfg['output']
        results[name] = download_geojson(
            name, cfg['url'], output_path, cfg['description']
        )

    write_metadata(OUTPUT_DIR, results)

    success_count = sum(results.values())
    log.info("=" * 60)
    log.info(f"Complete: {success_count}/{len(DATASETS)} datasets updated")
    log.info("=" * 60)
    return 0 if success_count == len(DATASETS) else 1


if __name__ == '__main__':
    sys.exit(main())

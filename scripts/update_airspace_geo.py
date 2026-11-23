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

  CLASS_AIRSPACE - Controlled airspace boundaries (Class B/C/D/E surface)
        FAA ADDS item: c6a62360338e408cb1512366ad61559e_0
        Added 2026-11-22 for the vector "master" low-zoom map layer
        (real sectionals only kick in at 7.5/5 nm; see
        project_nasr_sectional_rendering_idea memory) -- this was the
        one controlled-airspace gap SUA/MTR don't cover (those are
        special-use, not the standard Class B/C/D/E rings).
        ~6,000 records. Covers CONUS, Puerto Rico, Virgin Islands.

Cron (r815, run as www-data or root):
  30 3 * * 0  /var/www/cap_winds_app/scripts/update_airspace_geo.py \
              >> /var/log/airspace_geo_update.log 2>&1

Output:
  /var/www/cap_winds_app/static/geo/sua.geojson             (~500 KB)
  /var/www/cap_winds_app/static/geo/mtr.geojson              (~2 MB)
  /var/www/cap_winds_app/static/geo/class_airspace.geojson  (~1 MB)

Note: FAA ADDS uses hub.arcgis.com since June 2024 (Esri migration).
      Old opendata.arcgis.com URLs will redirect but may be slower.
"""

import os
import sys
import json
import time
import logging
import requests
import subprocess
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
    'class_airspace': {
        'url': ('https://hub.arcgis.com/api/v3/datasets/'
                'c6a62360338e408cb1512366ad61559e_0/'
                'downloads/data?format=geojson&spatialRefId=4326&where=1=1'),
        'output': 'class_airspace.geojson',
        'description': 'Controlled Airspace Boundaries (Class B/C/D/E)',
        # Esri's raw export densifies circular boundaries (Mode C veils,
        # Class B/C/D rings) into thousands of straight-line vertices per
        # feature, each a 3D coordinate (useless Z=0.0) at ~16 significant
        # digits -- found 2026-11-22 to balloon this to 542MB for only
        # 6,041 features (vs. 32MB for SUA's 1,545). ogr2ogr -dim XY drops
        # the Z, COORDINATE_PRECISION rounds to ~11cm (plenty for airspace
        # display), and -simplify at ~5.5m tolerance cuts a 3,143-vertex
        # ring down to 265 with no visible change at any zoom this map
        # uses. Result: 28.5MB, same ballpark as SUA. SUA/MTR don't need
        # this (no comparable circular-boundary vertex explosion).
        'simplify_tolerance': 0.00005,
    },
}

TIMEOUT_SECS  = 300   # 5 minutes per download (files can be large)
RETRY_LIMIT   = 3
RETRY_DELAY   = 30    # seconds between retries

# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------

def simplify_geojson(tmp_path, output_path, tolerance):
    """
    Run the downloaded GeoJSON through ogr2ogr to drop the Z dimension,
    round coordinate precision, and simplify vertex-dense geometry (see
    the 'simplify_tolerance' comment on the class_airspace dataset config
    for why this exists). Writes to a second temp file in the same
    directory as output_path, so the caller's atomic rename still applies.
    Returns the new temp path, or raises on failure.
    """
    simplified_fd, simplified_path = tempfile.mkstemp(
        dir=output_path.parent, suffix='.geojson.simplified.tmp')
    os.close(simplified_fd)
    os.unlink(simplified_path)  # ogr2ogr must create this itself
    subprocess.run([
        'ogr2ogr', '-f', 'GeoJSON', simplified_path, tmp_path,
        '-dim', 'XY', '-lco', 'COORDINATE_PRECISION=6',
        '-simplify', str(tolerance),
    ], check=True, capture_output=True, text=True)
    return simplified_path


def download_geojson(name, url, output_path, description, simplify_tolerance=None):
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

            final_path = tmp_path
            if simplify_tolerance:
                log.info(f"[{name}] Simplifying ({bytes_written/1024/1024:.0f} MB raw)...")
                final_path = simplify_geojson(tmp_path, output_path, simplify_tolerance)
                os.unlink(tmp_path)
                bytes_written = os.path.getsize(final_path)

            # Atomic replace
            shutil.move(final_path, output_path)
            log.info(f"[{name}] OK: {feature_count} features, "
                     f"{bytes_written/1024:.0f} KB in {elapsed:.1f}s -> {output_path}")
            return True

        except Exception as e:
            log.error(f"[{name}] Attempt {attempt}/{RETRY_LIMIT} failed: {e}")
            if 'tmp_path' in locals() and os.path.exists(tmp_path):
                os.unlink(tmp_path)
            if 'final_path' in locals() and final_path != tmp_path and os.path.exists(final_path):
                os.unlink(final_path)
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
            name, cfg['url'], output_path, cfg['description'],
            simplify_tolerance=cfg.get('simplify_tolerance'),
        )

    write_metadata(OUTPUT_DIR, results)

    success_count = sum(results.values())
    log.info("=" * 60)
    log.info(f"Complete: {success_count}/{len(DATASETS)} datasets updated")
    log.info("=" * 60)
    return 0 if success_count == len(DATASETS) else 1


if __name__ == '__main__':
    sys.exit(main())

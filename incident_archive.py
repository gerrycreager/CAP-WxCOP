"""
Aircraft Incident Data Archive Tool
Flask blueprint for collecting weather data around an aviation incident.

Collects from local database and LDM radar archive:
  - METARs/SPECIs: all reports within 100nm for 6hr window ending at incident time
  - TAFs: all valid TAFs within 100nm issued in 24hr before incident time
  - Radar PNGs: all sites within 140nm, +/-1hr window, NCR product
  - Satellite: NCEI/AWS manual download instructions (Phase 1)

Radar path: /LDM/radar/level3/{SITE}/{PRODUCT}/png/{YYYYMMDD}/{SITE}_{PRODUCT}_{HHMMSS}.png
Site IDs are 3-char (e.g. DMX, FTG) matching LDM directory names.
RADAR_SITES dict imported from radar_api to avoid duplication.
"""

from flask import Blueprint, jsonify, request, send_file
import sys
import os
import json
import zipfile
import tempfile
import re
from math import radians, cos, sin, asin, sqrt
from datetime import datetime, timedelta

sys.path.insert(0, '/var/www/cap_winds_app')
from db_config import get_connection

# Import radar site coordinates from radar_api
# Keys are 3-char site IDs matching LDM directory names (e.g. 'DMX', 'FTG')
try:
    from radar_api import RADAR_SITES
except ImportError:
    RADAR_SITES = {}

incident_archive = Blueprint('incident_archive', __name__)

RADAR_BASE  = '/LDM/radar/level3'
METAR_RADIUS_NM  = 100
RADAR_RADIUS_NM  = 140
METAR_WINDOW_HRS = 6    # window ends at incident time
TAF_LOOKBACK_HRS = 24   # TAFs issued within this window before incident time


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def haversine_nm(lat1, lon1, lat2, lon2):
    """Return great-circle distance in nautical miles."""
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    return 2 * asin(sqrt(a)) * 3440.065


def parse_location(location_data):
    """
    Convert location input to (lat, lon) float tuple.

    Accepts:
      {'type': 'airport', 'value': 'KCOS'}
      {'type': 'latlon',  'value': {'lat': '38.8058', 'lon': '-104.7008'}}
      {'type': 'mgrs',    'value': '13TDE1234567890'}

    Lat/lon value may also be decimal degrees string or
    degrees-decimal-minutes string: "38 48.348N" / "104 42.048W"
    """
    loc_type = location_data.get('type')

    if loc_type == 'airport':
        sid = location_data['value'].upper().strip()
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT ST_Y(location), ST_X(location)
            FROM observations.airports
            WHERE station_id = %s
            LIMIT 1
        """, (sid,))
        row = cur.fetchone()
        if not row:
            # Fall back to most recent METAR location
            cur.execute("""
                    ST_Y(location), ST_X(location)
                FROM observations.metar
                WHERE station_id = %s
                AND location IS NOT NULL
                ORDER BY station_id, observation_time DESC
                LIMIT 1
            """, (sid,))
            row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            raise ValueError(f"Airport {sid} not found in database")
        return float(row[0]), float(row[1])

    elif loc_type == 'latlon':
        v = location_data['value']
        lat = _parse_coord(str(v.get('lat', '')))
        lon = _parse_coord(str(v.get('lon', '')))
        return lat, lon

    elif loc_type == 'mgrs':
        try:
            import mgrs as mgrs_lib
            m = mgrs_lib.MGRS()
            lat, lon = m.toLatLon(location_data['value'].encode())
            return float(lat), float(lon)
        except ImportError:
            raise ValueError("mgrs library not installed; use pip install mgrs")

    else:
        raise ValueError(f"Unknown location type: {loc_type}")


def _parse_coord(s):
    """
    Parse coordinate string to float.
    Accepts: '38.8058', '38 48.348N', '38 48.348', '-104.7008', '104 42.048W'
    """
    s = s.strip()
    if not s:
        raise ValueError("Empty coordinate string")

    # Detect hemisphere suffix
    hemi = None
    if s[-1].upper() in ('N', 'S', 'E', 'W'):
        hemi = s[-1].upper()
        s = s[:-1].strip()

    # Degrees decimal-minutes: "38 48.348"
    ddm = re.match(r'^(-?\d+)\s+([\d.]+)$', s)
    if ddm:
        deg = float(ddm.group(1))
        mins = float(ddm.group(2))
        val = abs(deg) + mins / 60.0
        if deg < 0:
            val = -val
    else:
        val = float(s)

    if hemi in ('S', 'W'):
        val = -abs(val)

    return val


def radar_png_timestamp(filepath):
    """
    Extract UTC datetime from radar PNG filename.
    Pattern: {SITE}_{PRODUCT}_{HHMMSS}.png in directory .../png/{YYYYMMDD}/
    Example: /LDM/radar/level3/DMX/NCR/png/20260217/DMX_NCR_170003.png
    """
    # Date from parent directory name
    date_match = re.search(r'/png/(\d{8})/', filepath)
    # Time from filename
    time_match = re.search(r'_(\d{6})\.png$', filepath)
    if date_match and time_match:
        try:
            return datetime.strptime(
                date_match.group(1) + time_match.group(1),
                '%Y%m%d%H%M%S'
            )
        except ValueError:
            pass
    # Fallback to file mtime
    return datetime.utcfromtimestamp(os.path.getmtime(filepath))


# =============================================================================
# DATA COLLECTION FUNCTIONS
# =============================================================================

def collect_metars(lat, lon, incident_time):
    """
    Collect METARs and SPECIs from local database.

    Returns (summary_text, by_station_dict, station_list)

    Window: [incident_time - 6hr, incident_time]
    Radius: 100nm
    """
    window_start = incident_time - timedelta(hours=METAR_WINDOW_HRS)
    radius_m     = METAR_RADIUS_NM * 1852.0

    conn = get_connection()
    cur  = conn.cursor()

    cur.execute("""
        SELECT
            station_id,
            observation_time,
            raw_text,
            flight_category,
            ST_Y(location) as lat,
            ST_X(location) as lon
        FROM observations.metar
        WHERE
            observation_time BETWEEN %s AND %s
            AND location IS NOT NULL
            AND ST_DWithin(
                location::geography,
                ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                %s
            )
        ORDER BY station_id, observation_time DESC
    """, (window_start, incident_time, lon, lat, radius_m))

    rows = cur.fetchall()
    cur.close()
    conn.close()

    by_station = {}
    for row in rows:
        sid  = row[0]
        time = row[1]
        raw  = row[2] or ''
        cat  = row[3] or ''
        slat = float(row[4])
        slon = float(row[5])
        dist = round(haversine_nm(lat, lon, slat, slon), 1)

        if sid not in by_station:
            by_station[sid] = {
                'distance_nm': dist,
                'reports': []
            }
        by_station[sid]['reports'].append({
            'time': time.strftime('%Y-%m-%d %H:%MZ'),
            'raw':  raw,
            'cat':  cat
        })

    # Build combined text file
    lines = [
        f"METAR/SPECI ARCHIVE",
        f"Incident Time : {incident_time.strftime('%Y-%m-%d %H:%MZ')}",
        f"Window        : {window_start.strftime('%Y-%m-%d %H:%MZ')} to {incident_time.strftime('%H:%MZ')}",
        f"Center        : {lat:.4f}, {lon:.4f}",
        f"Radius        : {METAR_RADIUS_NM} nm",
        f"Stations      : {len(by_station)}",
        f"Reports       : {len(rows)}",
        "=" * 72
    ]

    for sid in sorted(by_station, key=lambda s: by_station[s]['distance_nm']):
        info = by_station[sid]
        lines.append(f"\n{sid}  ({info['distance_nm']} nm)")
        lines.append("-" * 40)
        for r in info['reports']:
            lines.append(f"  {r['time']}  [{r['cat']:4s}]  {r['raw']}")

    return '\n'.join(lines), by_station


def collect_tafs(lat, lon, incident_time):
    """
    Collect TAFs from local database.

    Returns (summary_text, by_station_dict)

    Window: ALL TAFs issued within 24hr before incident time (regardless of validity)
    to show forecast progression. Radius: 100nm.
    """
    window_start = incident_time - timedelta(hours=TAF_LOOKBACK_HRS)
    radius_m     = METAR_RADIUS_NM * 1852.0

    conn = get_connection()
    cur  = conn.cursor()

    # Collect ALL TAFs in the time window, not just most recent per station
    cur.execute("""
        SELECT
            station_id,
            issue_time,
            valid_from,
            valid_to,
            raw_text,
            ST_Y(location) as lat,
            ST_X(location) as lon
        FROM observations.taf
        WHERE
            issue_time BETWEEN %s AND %s
            AND location IS NOT NULL
            AND ST_DWithin(
                location::geography,
                ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                %s
            )
        ORDER BY station_id, issue_time DESC
    """, (window_start, incident_time, lon, lat, radius_m))

    rows = cur.fetchall()
    cur.close()
    conn.close()

    # Group TAFs by station
    by_station = {}
    for row in rows:
        sid   = row[0]
        issue = row[1]
        vfrom = row[2]
        vto   = row[3]
        raw   = row[4] or ''
        slat  = float(row[5])
        slon  = float(row[6])
        dist  = round(haversine_nm(lat, lon, slat, slon), 1)

        if sid not in by_station:
            by_station[sid] = {
                'distance_nm': dist,
                'tafs': []
            }
        
        by_station[sid]['tafs'].append({
            'issue_time': issue.strftime('%Y-%m-%d %H:%MZ') if issue else '',
            'valid_from': vfrom.strftime('%Y-%m-%d %H:%MZ') if vfrom else '',
            'valid_to':   vto.strftime('%Y-%m-%d %H:%MZ') if vto else '',
            'raw':        raw
        })

    # Count total TAFs
    total_tafs = sum(len(info['tafs']) for info in by_station.values())

    lines = [
        f"TAF ARCHIVE",
        f"Incident Time : {incident_time.strftime('%Y-%m-%d %H:%MZ')}",
        f"Lookback      : {TAF_LOOKBACK_HRS} hours",
        f"Center        : {lat:.4f}, {lon:.4f}",
        f"Radius        : {METAR_RADIUS_NM} nm",
        f"Stations      : {len(by_station)}",
        f"Total TAFs    : {total_tafs}",
        "=" * 72
    ]

    for sid in sorted(by_station, key=lambda s: by_station[s]['distance_nm']):
        info = by_station[sid]
        lines.append(f"\n{sid}  ({info['distance_nm']} nm)  ({len(info['tafs'])} TAFs)")
        lines.append("-" * 50)
        
        for taf in info['tafs']:
            lines.append(f"  Issued: {taf['issue_time']}  Valid: {taf['valid_from']} - {taf['valid_to']}")
            lines.append(f"  {taf['raw']}")
            lines.append("")  # blank line between TAFs

    return '\n'.join(lines), by_station


def collect_radar(lat, lon, incident_time):
    """
    Collect radar PNGs from local LDM archive.

    Path: /LDM/radar/level3/{SITE}/{PRODUCT}/png/{YYYYMMDD}/{SITE}_{PRODUCT}_{HHMMSS}.png
    Window: [incident_time - 1hr, incident_time + 1hr]
    Products: NCR (Composite Reflectivity) — primary product available on this system
    Radius: 140nm from incident location

    Returns dict: {site_id: {'distance_nm': x, 'name': y, 'files': [paths]}}
    """
    window_start = incident_time - timedelta(hours=1)
    window_end   = incident_time + timedelta(hours=1)
    products     = ['NCR']

    # Dates to search (window may span midnight)
    search_dates = set()
    t = window_start
    while t <= window_end:
        search_dates.add(t.strftime('%Y%m%d'))
        t += timedelta(hours=1)

    # Find radar sites within radius
    nearby = []
    for site_id, info in RADAR_SITES.items():
        dist = haversine_nm(lat, lon, info['lat'], info['lon'])
        if dist <= RADAR_RADIUS_NM:
            nearby.append((site_id, dist))
    nearby.sort(key=lambda x: x[1])

    result = {}
    for site_id, dist in nearby:
        for product in products:
            files = []
            for date_str in sorted(search_dates):
                png_dir = os.path.join(RADAR_BASE, site_id, product, 'png', date_str)
                if not os.path.isdir(png_dir):
                    continue
                for fname in sorted(os.listdir(png_dir)):
                    if not fname.endswith('.png'):
                        continue
                    fpath = os.path.join(png_dir, fname)
                    try:
                        ftime = radar_png_timestamp(fpath)
                        if window_start <= ftime <= window_end:
                            files.append(fpath)
                    except Exception:
                        continue

            if files:
                key = f"{site_id}_{product}"
                result[key] = {
                    'site':        site_id,
                    'product':     product,
                    'distance_nm': round(dist, 1),
                    'name':        RADAR_SITES.get(site_id, {}).get('name', site_id),
                    'files':       files
                }

    return result


def generate_satellite_links(lat, lon, incident_time):
    """
    Generate NCEI/AWS manual download instructions for satellite imagery.
    GOES-East (16) for lon > -105, GOES-West (18) for lon <= -105.
    """
    satellite = 'GOES-16' if lon > -105 else 'GOES-18'
    bucket    = 'noaa-goes16' if lon > -105 else 'noaa-goes18'
    doy       = incident_time.timetuple().tm_yday
    hour      = incident_time.strftime('%H')
    date_str  = incident_time.strftime('%Y-%m-%d')
    time_str  = incident_time.strftime('%H:%M')

    return f"""SATELLITE IMAGERY - MANUAL DOWNLOAD INSTRUCTIONS
Incident  : {incident_time.strftime('%Y-%m-%d %H:%MZ')}
Location  : {lat:.4f}, {lon:.4f}
Satellite : {satellite}

RECOMMENDED PRODUCTS:
  Band 2  (0.64 um)  - Visible (daytime only)
  Band 13 (10.3 um)  - Clean IR Longwave (cloud-top temps)
  Band 8  (6.2 um)   - Upper-level Water Vapor
  Band 10 (7.3 um)   - Low-level Water Vapor
  MCMIP              - Multi-band composite

AWS OPEN DATA (free, no account required):
  Bucket : s3://{bucket}/
  Path   : ABI-L2-MCMIPC/{incident_time.year}/{doy:03d}/{hour}/

  AWS CLI:
    aws s3 ls s3://{bucket}/ABI-L2-MCMIPC/{incident_time.year}/{doy:03d}/{hour}/ --no-sign-request
    aws s3 cp s3://{bucket}/ABI-L2-MCMIPC/{incident_time.year}/{doy:03d}/{hour}/<filename> . --no-sign-request

  Web browser:
    https://registry.opendata.aws/noaa-goes/

NOAA CLASS (full archive, requires free account):
  URL    : https://www.class.noaa.gov/
  Search : Satellite={satellite}, Date={date_str}, Time~{time_str}Z, Sector=CONUS

NCEI GOES-R Archive:
  URL    : https://www.ncei.noaa.gov/products/satellite/goes-r-series
"""


def generate_readme(lat, lon, incident_time, location_input,
                    n_metars, n_tafs, radar_sites):
    """Generate README.txt for the archive zip."""
    return f"""AIRCRAFT INCIDENT WEATHER DATA ARCHIVE
Generated : {datetime.utcnow().strftime('%Y-%m-%d %H:%MZ')}
Tool      : CAP METWatch Incident Archive Tool

INCIDENT PARAMETERS
  Time     : {incident_time.strftime('%Y-%m-%d %H:%MZ')}
  Location : {lat:.4f}, {lon:.4f}
  Input    : {location_input}

CONTENTS
  metars/
    all_metars.txt         All METAR/SPECI reports, sorted by station
    by_station/KXXX.txt    Individual station files
    (Window: {METAR_WINDOW_HRS}hr ending at incident time, radius {METAR_RADIUS_NM}nm)
    (Stations: {n_metars})

  tafs/
    all_tafs.txt           All TAFs valid at incident time
    (Lookback: {TAF_LOOKBACK_HRS}hr, radius {METAR_RADIUS_NM}nm)
    (Stations: {n_tafs})

  radar/
    SITE_PRODUCT_dist/     One directory per site/product
      *.png                Georeferenced composite reflectivity images
    (Window: +/-1hr, radius {RADAR_RADIUS_NM}nm)
    (Sites with data: {radar_sites})

  satellite/
    DOWNLOAD_INSTRUCTIONS.txt
    (Automated satellite download is Phase 2 - see instructions for manual retrieval)

  metadata.json            Machine-readable archive parameters

DATA SOURCES
  METARs/TAFs : Local PostgreSQL database (LDM ingest)
  Radar       : Local LDM archive (/LDM/radar/level3/)
  Satellite   : Manual download - see satellite/DOWNLOAD_INSTRUCTIONS.txt

CAP WEATHER TEAM TESTBED
"""


# =============================================================================
# FLASK ENDPOINTS
# =============================================================================

@incident_archive.route('/incident-archive')
def incident_archive_page():
    """Serve the incident archive HTML page."""
    from flask import render_template
    return render_template('incident_archive.html')


@incident_archive.route('/incident-archive/preview', methods=['POST'])
def preview_archive():
    """
    Preview what data would be collected without generating the zip.
    Returns JSON summary for the UI to display before download.
    """
    try:
        data = request.json

        lat, lon      = parse_location(data['location'])
        incident_time = datetime.fromisoformat(
            data['incident_time'].replace('Z', '+00:00')
        ).replace(tzinfo=None)

        radius_nm       = float(data.get('radius_nm', METAR_RADIUS_NM))
        radar_radius_nm = float(data.get('radar_radius_nm', RADAR_RADIUS_NM))

        # Count METARs
        window_start = incident_time - timedelta(hours=METAR_WINDOW_HRS)
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT COUNT(DISTINCT station_id), COUNT(*)
            FROM observations.metar
            WHERE observation_time BETWEEN %s AND %s
            AND ST_DWithin(
                location::geography,
                ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                %s
            )
        """, (window_start, incident_time, lon, lat, radius_nm * 1852))
        metar_row = cur.fetchone()

        # Count TAFs
        cur.execute("""
            SELECT COUNT(DISTINCT station_id)
            FROM observations.taf
            WHERE issue_time BETWEEN %s AND %s
            AND ST_DWithin(
                location::geography,
                ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                %s
            )
        """, (incident_time - timedelta(hours=TAF_LOOKBACK_HRS),
              incident_time,
              lon, lat, radius_nm * 1852))
        taf_row = cur.fetchone()
        cur.close()
        conn.close()

        # Radar sites
        radar_nearby = [
            {'site': sid, 'distance_nm': round(haversine_nm(lat, lon, info['lat'], info['lon']), 1)}
            for sid, info in RADAR_SITES.items()
            if haversine_nm(lat, lon, info['lat'], info['lon']) <= radar_radius_nm
        ]
        radar_nearby.sort(key=lambda x: x['distance_nm'])

        return jsonify({
            'lat': lat,
            'lon': lon,
            'incident_time': incident_time.strftime('%Y-%m-%d %H:%MZ'),
            'metars': {
                'stations': metar_row[0],
                'reports':  metar_row[1]
            },
            'tafs': {
                'stations': taf_row[0]
            },
            'radar': {
                'sites': radar_nearby
            }
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 400


@incident_archive.route('/incident-archive/create', methods=['POST'])
def create_archive():
    """
    Generate and return the incident weather data archive as a zip file.

    POST body (JSON):
    {
        "location": {
            "type": "airport" | "latlon" | "mgrs",
            "value": "KCOS" | {"lat": "38.8058", "lon": "-104.7008"} | "13TDE..."
        },
        "incident_time": "2026-02-17T14:30:00Z",
        "radius_nm": 100,
        "radar_radius_nm": 140
    }
    """
    try:
        data = request.json

        lat, lon      = parse_location(data['location'])
        incident_time = datetime.fromisoformat(
            data['incident_time'].replace('Z', '+00:00')
        ).replace(tzinfo=None)

        location_label = (
            data['location'].get('value')
            if data['location']['type'] == 'airport'
            else f"{lat:.2f}_{lon:.2f}"
        )
        zip_name = f"incident_{incident_time.strftime('%Y%m%d_%H%MZ')}_{location_label}.zip"

        # Collect all data
        metar_text, metar_by_station = collect_metars(lat, lon, incident_time)
        taf_text,   taf_by_station   = collect_tafs(lat, lon, incident_time)
        radar_data                   = collect_radar(lat, lon, incident_time)
        sat_text                     = generate_satellite_links(lat, lon, incident_time)

        # Build metadata
        metadata = {
            'incident_time':        incident_time.isoformat() + 'Z',
            'location':             {'lat': lat, 'lon': lon},
            'location_input':       data['location'],
            'collection_time':      datetime.utcnow().isoformat() + 'Z',
            'metar_window_hrs':     METAR_WINDOW_HRS,
            'taf_lookback_hrs':     TAF_LOOKBACK_HRS,
            'metar_radius_nm':      METAR_RADIUS_NM,
            'radar_radius_nm':      RADAR_RADIUS_NM,
            'metar_stations':       len(metar_by_station),
            'metar_reports':        sum(len(v['reports']) for v in metar_by_station.values()),
            'taf_stations':         len(taf_by_station),
            'total_tafs':           sum(len(v['tafs']) for v in taf_by_station.values()),
            'radar_products':       len(radar_data),
            'radar_sites':          list({v['site'] for v in radar_data.values()})
        }

        readme = generate_readme(
            lat, lon, incident_time,
            str(data['location'].get('value', f"{lat:.4f},{lon:.4f}")),
            len(metar_by_station),
            len(taf_by_station),
            len({v['site'] for v in radar_data.values()})
        )

        # Write zip to a named temp file (send_file needs a persistent path)
        tmp = tempfile.NamedTemporaryFile(
            suffix='.zip', delete=False,
            dir='/tmp', prefix='incident_'
        )
        tmp.close()

        with zipfile.ZipFile(tmp.name, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('README.txt',        readme)
            zf.writestr('metadata.json',     json.dumps(metadata, indent=2))
            zf.writestr('metars/all_metars.txt', metar_text)
            zf.writestr('tafs/all_tafs.txt',     taf_text)

            # Per-station METAR files
            for sid, info in metar_by_station.items():
                lines = [f"{r['time']}  [{r['cat']:4s}]  {r['raw']}"
                         for r in info['reports']]
                zf.writestr(
                    f"metars/by_station/{sid}.txt",
                    '\n'.join(lines)
                )

            # Per-station TAF files
            for sid, info in taf_by_station.items():
                lines = []
                for taf in info['tafs']:
                    lines.append(f"Issued: {taf['issue_time']}")
                    lines.append(f"Valid:  {taf['valid_from']} - {taf['valid_to']}")
                    lines.append("")
                    lines.append(taf['raw'])
                    lines.append("")
                    lines.append("-" * 50)
                    lines.append("")
                zf.writestr(
                    f"tafs/by_station/{sid}.txt",
                    '\n'.join(lines)
                )

            # Radar PNGs
            for product_key, pinfo in radar_data.items():
                site    = pinfo['site']
                product = pinfo['product']
                dist    = pinfo['distance_nm']
                dir_name = f"radar/{site}_{dist}nm/{product}"
                for fpath in pinfo['files']:
                    fname = os.path.basename(fpath)
                    zf.write(fpath, f"{dir_name}/{fname}")

            # Satellite instructions
            zf.writestr('satellite/DOWNLOAD_INSTRUCTIONS.txt', sat_text)

        return send_file(
            tmp.name,
            as_attachment=True,
            download_name=zip_name,
            mimetype='application/zip'
        )

    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


"""
model_taf_api.py - Serves RRFS-primary/HRRR-fallback model-derived TAFs
for arbitrary stations, added 2026-09-22 against the spec in
~gerry/CAP/RPP/MEF_dev/RRFS_Model_TAF_Spec.20260920_2300-C.md.

Deliberately NOT linked from any template or the WxCOP homepage -- per
Gerry, this feeds his laptop-side Mission Execution Forecast (MEF) tool
directly, no web presence needed. On-demand only, no cron, no DB writes;
each request calls scripts/rrfs_model_taf.py, which fetches/decodes at
most a handful of RRFS files (cached 6h by rrfs_wind_fetch.py) or reads
already-local HRRR files.

Separate from yesterday's mission_taf_api.py (fixed KBAK-cluster, 06/14/
22Z, single sky layer) -- that one still exists as its own narrower
product; this is the general, any-station, multi-layer, BECMG-capable one
the MEF spec asked for.
"""

import threading
import time
import uuid
from datetime import datetime, timezone

from flask import Blueprint, Response, jsonify, request

import db_config
from scripts.rrfs_model_taf import get_hourly_conditions, build_taf_text

model_taf_api = Blueprint('model_taf_api', __name__)

DEFAULT_MAX_FHOUR = 18       # synchronous single-station default -- kept
                             # conservative so /<icao> stays fast enough
                             # for one request/response.
AREA_MEF_MAX_FHOUR = 30      # async batch default, per Gerry (2026-09-22):
                             # real MEF duration is 30h, matching standard
                             # TAF validity convention.
MAX_STATIONS_PER_BATCH = 25


def _lookup_stations(idents):
    conn = db_config.get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT station_id, ST_Y(location), ST_X(location), elevation_ft
        FROM observations.airports
        WHERE station_id = ANY(%s) AND location IS NOT NULL
    """, (idents,))
    rows = {r[0]: {'lat': float(r[1]), 'lon': float(r[2]),
                   'elev_ft': float(r[3]) if r[3] is not None else 0.0}
            for r in cur.fetchall()}
    cur.close()
    conn.close()
    return rows


def _generate_one(station_id, lat, lon, elev_ft, max_fhour):
    model, cycle_dt, hourly = get_hourly_conditions(lat, lon, elev_ft, max_fhour=max_fhour)
    issue_time = datetime.now(timezone.utc).replace(tzinfo=None, second=0, microsecond=0)
    if not hourly:
        return {
            'station': {'id': station_id, 'elevation_ft': elev_ft, 'lat': lat, 'lon': lon},
            'model': None, 'guidance_only': True,
            'generated_utc': issue_time.isoformat() + 'Z',
            'kind': 'full_24h' if max_fhour >= 24 else 'short_range_update',
            'status': 'unavailable', 'status_reason': 'no RRFS or HRRR cycle available',
            'taf_text': None, 'ceiling_ft_by_hour': {}, 'hourly': [],
        }

    taf_text, ceil_by_hour = build_taf_text(station_id, cycle_dt, issue_time, hourly, elev_ft)
    age_min = int((issue_time - cycle_dt).total_seconds() / 60)

    hourly_json = []
    for t, cond in hourly:
        hourly_json.append({
            'time_utc': t.isoformat() + 'Z',
            'temp_c': cond['temp_c'], 'dewpoint_c': cond['dewpoint_c'],
            'altimeter_inhg': cond['altimeter_inhg'],
            'wind_dir': cond['wind_dir'], 'wind_kt': cond['wind_kt'],
            'gust_kt': cond['gust_kt'], 'vis_m': cond['vis_m'],
            'ceiling_ft': cond['ceiling_ft'],
            'clouds': [{'cover': c, 'base_ft': round(h)} for c, h in cond['clouds']],
            'weather': [cond['weather']] if cond['weather'] else [],
        })

    return {
        'product': 'modeltaf', 'version': '1.0', 'guidance_only': True,
        'station': {'id': station_id, 'elevation_ft': elev_ft, 'lat': lat, 'lon': lon},
        'model': {'name': model, 'cycle_utc': cycle_dt.isoformat() + 'Z'},
        'generated_utc': issue_time.isoformat() + 'Z',
        'age_min': age_min,
        'kind': 'full_24h' if max_fhour >= 24 else 'short_range_update',
        'status': 'ok', 'status_reason': None,
        'valid_from': hourly[0][0].isoformat() + 'Z',
        'valid_to': (hourly[-1][0]).isoformat() + 'Z',
        'taf_text': taf_text,
        'ceiling_ft_by_hour': ceil_by_hour,
        'hourly': hourly_json,
    }


def _bulletin_text(results):
    header = 'MODEL-DERIVED TAF GUIDANCE -- NOT AN OFFICIAL FORECAST\nGRAMMAR: USAF FM51 / AFMAN 15-124\n'
    blocks = []
    for r in results:
        if r['status'] != 'ok':
            blocks.append(f"{r['station']['id']} -- {r['status'].upper()}: {r['status_reason']}")
            continue
        blocks.append(
            f"STATION: {r['station']['id']}  ELEV_FT: {r['station']['elevation_ft']:.0f}  "
            f"MODEL: {r['model']['name']}  CYCLE: {r['model']['cycle_utc']}  "
            f"GENERATED: {r['generated_utc']}  AGE_MIN: {r['age_min']}\n{r['taf_text']}"
        )
    return header + '\n\n' + '\n\n'.join(blocks) + '\n'


@model_taf_api.route('/<icao>')
def single_json(icao):
    icao = icao.upper()
    stations = _lookup_stations([icao])
    if icao not in stations:
        return jsonify({'error': f'{icao} not found in airports table'}), 404
    s = stations[icao]
    max_fhour = request.args.get('max_fhour', DEFAULT_MAX_FHOUR, type=int)
    return jsonify(_generate_one(icao, s['lat'], s['lon'], s['elev_ft'], max_fhour))


@model_taf_api.route('/<icao>.txt')
def single_text(icao):
    icao = icao.upper()
    stations = _lookup_stations([icao])
    if icao not in stations:
        return Response(f'{icao} not found in airports table\n', mimetype='text/plain'), 404
    s = stations[icao]
    max_fhour = request.args.get('max_fhour', DEFAULT_MAX_FHOUR, type=int)
    result = _generate_one(icao, s['lat'], s['lon'], s['elev_ft'], max_fhour)
    return Response(_bulletin_text([result]), mimetype='text/plain')


@model_taf_api.route('/batch')
def batch():
    """?stations=KAAA,KBBB,... -- for a mission profile's satellite list.
    Stations resolve from the airports table so ad hoc mission lists work
    without code changes, per spec section 4."""
    raw = request.args.get('stations', '')
    idents = [s.strip().upper() for s in raw.split(',') if s.strip()]
    if not idents:
        return jsonify({'error': 'stations parameter required, comma-separated ICAOs'}), 400
    if len(idents) > MAX_STATIONS_PER_BATCH:
        return jsonify({'error': f'max {MAX_STATIONS_PER_BATCH} stations per batch'}), 400

    found = _lookup_stations(idents)
    max_fhour = request.args.get('max_fhour', DEFAULT_MAX_FHOUR, type=int)
    results = []
    for icao in idents:
        if icao not in found:
            results.append({
                'station': {'id': icao, 'elevation_ft': None, 'lat': None, 'lon': None},
                'model': None, 'guidance_only': True,
                'generated_utc': datetime.now(timezone.utc).isoformat(),
                'kind': None, 'status': 'unavailable',
                'status_reason': 'not found in airports table',
                'taf_text': None, 'ceiling_ft_by_hour': {}, 'hourly': [],
            })
            continue
        s = found[icao]
        results.append(_generate_one(icao, s['lat'], s['lon'], s['elev_ft'], max_fhour))

    if request.args.get('format') == 'text':
        return Response(_bulletin_text(results), mimetype='text/plain')
    return jsonify({'products': results})


# ---------------------------------------------------------------------------
# Async job queue -- an "area MEF" (many stations x 30h each) doesn't fit
# Apache/mod_wsgi's ~60s timeout in one synchronous request (confirmed
# live 2026-09-22: even a single 24h station took over a minute through
# the real web server). Same pattern soundings_api.py already uses for the
# same underlying reason (multi-minute RRFS fetches, no timeout ceiling on
# a background thread the client polls instead of blocking on).
#
# Stations are processed SEQUENTIALLY within the job, not concurrently --
# per Gerry (2026-09-22): "generated on the fly... sequentially to keep
# load down, for the application." Each station's own per-hour fetch still
# uses scripts/rrfs_model_taf.py's internal ProcessPoolExecutor, so one
# station's 30h window is still reasonably fast; it's the ACROSS-stations
# axis that stays sequential, bounding how much load an area MEF request
# can put on the server at once.
# ---------------------------------------------------------------------------
_JOBS = {}
_JOBS_LOCK = threading.Lock()
_JOB_TTL_SEC = 3600


def _prune_jobs():
    cutoff = time.time() - _JOB_TTL_SEC
    stale = [jid for jid, j in _JOBS.items() if j['created'] < cutoff]
    for jid in stale:
        _JOBS.pop(jid, None)


def _run_batch_job(job_id, idents, found, max_fhour):
    results = []
    try:
        for i, icao in enumerate(idents):
            with _JOBS_LOCK:
                if job_id not in _JOBS:
                    return  # job was pruned/expired mid-run
                _JOBS[job_id]['progress'] = f'{i} of {len(idents)} stations complete'
            if icao not in found:
                results.append({
                    'station': {'id': icao, 'elevation_ft': None, 'lat': None, 'lon': None},
                    'model': None, 'guidance_only': True,
                    'generated_utc': datetime.now(timezone.utc).isoformat(),
                    'kind': None, 'status': 'unavailable',
                    'status_reason': 'not found in airports table',
                    'taf_text': None, 'ceiling_ft_by_hour': {}, 'hourly': [],
                })
                continue
            s = found[icao]
            results.append(_generate_one(icao, s['lat'], s['lon'], s['elev_ft'], max_fhour))
        with _JOBS_LOCK:
            if job_id in _JOBS:
                _JOBS[job_id].update(status='done', results=results,
                                     progress=f'{len(idents)} of {len(idents)} stations complete')
    except Exception as e:
        with _JOBS_LOCK:
            if job_id in _JOBS:
                _JOBS[job_id].update(status='error', error=str(e))


@model_taf_api.route('/batch/start')
def batch_start():
    """?stations=KAAA,KBBB,...&max_fhour=30 -- kicks off a background job
    processing stations one at a time, returns immediately with a job_id."""
    raw = request.args.get('stations', '')
    idents = [s.strip().upper() for s in raw.split(',') if s.strip()]
    if not idents:
        return jsonify({'error': 'stations parameter required, comma-separated ICAOs'}), 400
    if len(idents) > MAX_STATIONS_PER_BATCH:
        return jsonify({'error': f'max {MAX_STATIONS_PER_BATCH} stations per batch'}), 400

    found = _lookup_stations(idents)
    max_fhour = request.args.get('max_fhour', AREA_MEF_MAX_FHOUR, type=int)

    job_id = uuid.uuid4().hex
    with _JOBS_LOCK:
        _prune_jobs()
        _JOBS[job_id] = {
            'status': 'pending', 'created': time.time(),
            'progress': f'0 of {len(idents)} stations complete',
            'results': None, 'error': None,
            'stations': idents, 'max_fhour': max_fhour,
        }
    threading.Thread(target=_run_batch_job, args=(job_id, idents, found, max_fhour), daemon=True).start()
    return jsonify({'job_id': job_id, 'stations': idents, 'max_fhour': max_fhour})


@model_taf_api.route('/batch/status/<job_id>')
def batch_status(job_id):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return jsonify({'error': 'Unknown or expired job'}), 404
        resp = {'status': job['status'], 'progress': job['progress']}
        if job['status'] == 'error':
            resp['error'] = job['error']
    return jsonify(resp)


@model_taf_api.route('/batch/result/<job_id>')
def batch_result_json(job_id):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return jsonify({'error': 'Unknown or expired job'}), 404
        if job['status'] != 'done':
            return jsonify({'error': f"Job not ready (status={job['status']})", 'progress': job['progress']}), 425
        results = job['results']
    return jsonify({'products': results})


@model_taf_api.route('/batch/result/<job_id>.txt')
def batch_result_text(job_id):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return Response('Unknown or expired job\n', mimetype='text/plain'), 404
        if job['status'] != 'done':
            return Response(f"Job not ready (status={job['status']}, {job['progress']})\n",
                            mimetype='text/plain'), 425
        results = job['results']
    return Response(_bulletin_text(results), mimetype='text/plain')

"""
soundings_api.py — CAP WxCOP Soundings API Blueprint
=====================================================
Observed (TTAA/TTBB) and on-demand forecast upper-air soundings, rendered as
Skew-T log-P + hodograph. Scope: surface to 100mb (CAP aircraft don't
operate near or above 100mb) — see /root/.claude/plans and
scripts/ingest_sounding.py / scripts/rrfs_sounding_extract.py /
scripts/hrrr_sounding_extract.py for the full design rationale.

Forecast source: RRFS is the default (`source=rrfs` or omitted), per the
HRRR->RRFS migration (Gerry's deadline: replace all HRRR usage by
2026-10-16 — see memory project_noaaport_sbn_href_retirement and
/root/.claude/plans/rosy-wobbling-music.md). HRRR remains available as an
explicit `source=hrrr` fallback rather than being removed outright, since
RRFS currently depends on NOMADS (no S3 mirror exists yet, no LDM access
confirmed) and decodes ~5x slower than HRRR — real, open risk that argues
for keeping a working fallback until RRFS has proven itself in production.

Endpoints:
  GET /api/soundings/observed/latest              -- single most recent observed sounding, any station
  GET /api/soundings/observed/latest/skewt.png
  GET /api/soundings/site/<site_id>               -- resolve a site ID (e.g. KBAK) to lat/lon
  GET /api/soundings/forecast                     -- synchronous, JSON profile only
  GET /api/soundings/forecast/skewt.png           -- synchronous, JSON+PNG (may take ~30-90s, see below)
  GET /api/soundings/forecast/start               -- async: kicks off extraction, returns job_id
  GET /api/soundings/forecast/status/<job_id>
  GET /api/soundings/forecast/result/<job_id>.png

Deliberately simple for now, per Gerry's direction: observed soundings just
show the single latest one available (no station browser/history picker --
that can come later once TTAA/TTBB ingest is actually live and there's real
data to browse). Forecast soundings default to "now" (F00) for a named site
or clicked point, with the rest of HRRR's own forecast timeframe (through
F48 on synoptic cycles) reachable via the same site/point. Longer-range
soundings (GFS, eventually MPAS) are an explicitly out-of-scope future idea
noted for later, not built here.

Why both a synchronous and an async forecast path: the HRRR extraction
(scripts/hrrr_sounding_extract.py) genuinely takes ~30-45s even on a cache
hit, more on a cold S3 fetch -- and this app's WSGIDaemonProcess is
configured with socket-timeout=60 (cap_winds.conf), a server-level setting
this blueprint has no way to raise for just this one route. The browser
frontend uses the async job endpoints so a slow extraction just means a
longer spinner, never a killed connection; /forecast and /forecast/skewt.png
stay available synchronously for scripted/CLI callers that manage their own
timeout.

Observed-sounding ingest (TTAA/TTBB pqact rule) is not live yet — pending
confirmation of the real bulletin header format from a live notifyme
capture. The observed routes below are functional against whatever rows
exist in observations.sounding today (none, until ingest is wired up) and
will start returning real data the moment ingest goes live, with no code
change needed here.
"""
import time
import uuid
import logging
import threading
from datetime import datetime, timezone, timedelta

from flask import Blueprint, jsonify, request, Response
import psycopg2
import psycopg2.extras

from sounding_render import render_skewt
from scripts.hrrr_sounding_extract import get_hrrr_sounding
from scripts.rrfs_sounding_extract import get_rrfs_sounding, _latest_available_rrfs_cycle

log = logging.getLogger(__name__)
if not log.handlers:
    _fh = logging.FileHandler('/var/log/cap_wxcop_soundings_api.log')
    _fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    log.addHandler(_fh)
    log.setLevel(logging.INFO)

soundings_api = Blueprint('soundings_api', __name__)

DB_HOST = '192.168.0.60'
DB_NAME = 'avwx_data'
DB_USER = 'avwx_user'
DB_PASS = 'avwx_pass'


def db_connect():
    return psycopg2.connect(host=DB_HOST, dbname=DB_NAME, user=DB_USER, password=DB_PASS)


def _latest_available_hrrr_cycle(now=None, fhr=0):
    """HRRR wrfprs on S3 typically posts ~1-1.5hr after cycle time. Pick a
    cycle 2 hours old as a safe default when the caller doesn't specify one,
    same margin used elsewhere in this app for HRRR availability.

    HRRR only extends past F18 on the four synoptic cycles (00/06/12/18Z) --
    hourly-off-cycle runs stop at F18. If the caller wants an extended-range
    hour, pick the most recent synoptic cycle old enough to actually have
    that hour posted, instead of the plain "2 hours ago" default (which
    would often land on a non-synoptic cycle that simply doesn't have it)."""
    now = now or datetime.now(timezone.utc)
    lag = timedelta(hours=2)
    if fhr <= 18:
        cycle_dt = (now - lag).replace(minute=0, second=0, microsecond=0)
        return cycle_dt.strftime('%Y%m%d'), cycle_dt.strftime('%H')

    candidate = now - lag
    synoptic_hours = [0, 6, 12, 18]
    eligible = [h for h in synoptic_hours if h <= candidate.hour]
    if eligible:
        cycle_dt = candidate.replace(hour=max(eligible), minute=0, second=0, microsecond=0)
    else:
        cycle_dt = (candidate - timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
    return cycle_dt.strftime('%Y%m%d'), cycle_dt.strftime('%H')


@soundings_api.route('/observed/latest')
def observed_latest():
    """The single most recent observed sounding across all stations --
    deliberately not a browsable history/picker for now, just "what's the
    latest balloon data we have"."""
    conn = db_connect()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT station_id, obs_time, report_type, raw_text, profile
            FROM observations.sounding
            WHERE report_type IN ('TTAA', 'UUAA')
            ORDER BY obs_time DESC
            LIMIT 1
        """)
        row = cur.fetchone()
        cur.close()
    except Exception as e:
        log.exception('observed_latest failed')
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()
    if not row:
        return jsonify({'error': 'No observed soundings available yet'}), 404
    row['obs_time'] = row['obs_time'].isoformat() + 'Z'
    return jsonify(row)


@soundings_api.route('/observed/latest/skewt.png')
def observed_latest_skewt_png():
    conn = db_connect()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT station_id, obs_time, report_type, profile
            FROM observations.sounding
            WHERE report_type IN ('TTAA', 'UUAA')
            ORDER BY obs_time DESC
            LIMIT 1
        """)
        row = cur.fetchone()
        cur.close()
    except Exception as e:
        log.exception('observed_latest_skewt_png failed')
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()
    if not row:
        return jsonify({'error': 'No observed soundings available yet'}), 404
    try:
        title = f"{row['station_id']} — {row['report_type']} {row['obs_time'].strftime('%Y-%m-%d %H%MZ')} (Observed)"
        png = render_skewt(row['profile'], title=title)
    except ValueError as e:
        return jsonify({'error': str(e)}), 422
    return Response(png, mimetype='image/png')


@soundings_api.route('/site/<site_id>')
def lookup_site(site_id):
    """Resolve a 3-4 letter site identifier (e.g. KBAK) to a lat/lon via
    observations.airports, so a HRRR forecast sounding can be requested for
    a named site directly -- not every site has an observed sounding (no
    radiosonde launch nearby), but any site can get an on-demand HRRR
    forecast sounding."""
    site_id = site_id.strip().upper()
    conn = db_connect()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT station_id, name, ST_Y(location) AS lat, ST_X(location) AS lon
            FROM observations.airports
            WHERE station_id = %s
        """, (site_id,))
        row = cur.fetchone()
        cur.close()
    except Exception as e:
        log.exception('lookup_site failed for %s', site_id)
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()
    if not row or row['lat'] is None:
        return jsonify({'error': f'Site "{site_id}" not found (or has no location on file)'}), 404
    return jsonify(row)


def _parse_forecast_params(args):
    """Returns (lat, lon, date, cycle, fhr, source, error). On failure, lat
    is None and error holds a message; callers must check lat before using
    the rest. source defaults to 'rrfs' -- see module docstring for why."""
    try:
        lat = float(args['lat'])
        lon = float(args['lon'])
    except (KeyError, ValueError):
        return None, None, None, None, None, None, 'lat and lon are required'
    try:
        fhr = int(args.get('fhr', 0))
    except ValueError:
        return None, None, None, None, None, None, 'fhr must be an integer'
    source = args.get('source', 'rrfs').lower()
    if source not in ('rrfs', 'hrrr'):
        return None, None, None, None, None, None, "source must be 'rrfs' or 'hrrr'"
    date = args.get('date')
    cycle = args.get('cycle')
    if not date or not cycle:
        if source == 'hrrr':
            date, cycle = _latest_available_hrrr_cycle(fhr=fhr)
        else:
            date, cycle = _latest_available_rrfs_cycle(fhr=fhr)
    return lat, lon, date, cycle, fhr, source, None


def _get_sounding(source, lat, lon, date, cycle, fhr):
    if source == 'hrrr':
        return get_hrrr_sounding(lat, lon, date, cycle, fhr)
    return get_rrfs_sounding(lat, lon, date, cycle, fhr)


def _sounding_title(source, date, cycle, fhr, lat, lon):
    model = 'HRRR' if source == 'hrrr' else 'RRFS'
    return f"{model} {date} {cycle}Z +{fhr:02d}h — {lat:.3f}, {lon:.3f} (Forecast)"


def _not_available_msg(source, date, cycle, fhr):
    product = 'wrfprs' if source == 'hrrr' else 'prslev'
    model = 'HRRR' if source == 'hrrr' else 'RRFS'
    return f'{model} {product} not available for {date} {cycle}z f{fhr:02d} (not posted yet, or too old)'


@soundings_api.route('/forecast')
def forecast_sounding():
    lat, lon, date, cycle, fhr, source, error = _parse_forecast_params(request.args)
    if error:
        return jsonify({'error': error}), 400
    try:
        profile = _get_sounding(source, lat, lon, date, cycle, fhr)
    except Exception as e:
        log.exception('%s sounding extraction failed for lat=%s lon=%s %s %sz f%s', source, lat, lon, date, cycle, fhr)
        return jsonify({'error': f'{source} extraction failed: {e}'}), 500
    if profile is None:
        return jsonify({'error': _not_available_msg(source, date, cycle, fhr)}), 404
    return jsonify({'lat': lat, 'lon': lon, 'date': date, 'cycle': cycle, 'fhr': fhr, 'source': source, 'profile': profile})


@soundings_api.route('/forecast/skewt.png')
def forecast_skewt_png():
    lat, lon, date, cycle, fhr, source, error = _parse_forecast_params(request.args)
    if error:
        return jsonify({'error': error}), 400
    try:
        profile = _get_sounding(source, lat, lon, date, cycle, fhr)
    except Exception as e:
        log.exception('%s sounding extraction failed for lat=%s lon=%s %s %sz f%s', source, lat, lon, date, cycle, fhr)
        return jsonify({'error': f'{source} extraction failed: {e}'}), 500
    if profile is None:
        return jsonify({'error': _not_available_msg(source, date, cycle, fhr)}), 404
    try:
        png = render_skewt(profile, title=_sounding_title(source, date, cycle, fhr, lat, lon))
    except ValueError as e:
        return jsonify({'error': str(e)}), 422
    return Response(png, mimetype='image/png')


# ---------------------------------------------------------------------------
# Async forecast job queue -- see module docstring for why this exists
# (socket-timeout=60 on the WSGIDaemonProcess, extraction genuinely takes
# 30-90s for HRRR, 3-5+ min for RRFS via NOMADS -- see rrfs_sounding_extract.py).
# In-memory, single-daemon-process job store; fine at this app's scale
# (processes=1 in cap_winds.conf), not meant to survive a restart.
# ---------------------------------------------------------------------------
_JOBS = {}
_JOBS_LOCK = threading.Lock()
_JOB_TTL_SEC = 3600


def _prune_jobs():
    cutoff = time.time() - _JOB_TTL_SEC
    stale = [jid for jid, j in _JOBS.items() if j['created'] < cutoff]
    for jid in stale:
        _JOBS.pop(jid, None)


def _run_forecast_job(job_id, source, lat, lon, date, cycle, fhr):
    try:
        profile = _get_sounding(source, lat, lon, date, cycle, fhr)
        if profile is None:
            with _JOBS_LOCK:
                _JOBS[job_id].update(status='error', error=_not_available_msg(source, date, cycle, fhr))
            return
        png = render_skewt(profile, title=_sounding_title(source, date, cycle, fhr, lat, lon))
        with _JOBS_LOCK:
            _JOBS[job_id].update(status='done', png=png, profile=profile)
    except Exception as e:
        log.exception('Async %s sounding job %s failed for lat=%s lon=%s %s %sz f%s',
                       source, job_id, lat, lon, date, cycle, fhr)
        with _JOBS_LOCK:
            _JOBS[job_id].update(status='error', error=str(e))


@soundings_api.route('/forecast/start')
def forecast_start():
    lat, lon, date, cycle, fhr, source, error = _parse_forecast_params(request.args)
    if error:
        return jsonify({'error': error}), 400
    job_id = uuid.uuid4().hex
    with _JOBS_LOCK:
        _prune_jobs()
        _JOBS[job_id] = {
            'status': 'pending', 'created': time.time(),
            'png': None, 'profile': None, 'error': None,
            'lat': lat, 'lon': lon, 'date': date, 'cycle': cycle, 'fhr': fhr, 'source': source,
        }
    threading.Thread(target=_run_forecast_job, args=(job_id, source, lat, lon, date, cycle, fhr), daemon=True).start()
    return jsonify({'job_id': job_id, 'date': date, 'cycle': cycle, 'fhr': fhr, 'source': source})


@soundings_api.route('/forecast/status/<job_id>')
def forecast_status(job_id):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return jsonify({'error': 'Unknown or expired job'}), 404
        resp = {'status': job['status']}
        if job['status'] == 'error':
            resp['error'] = job['error']
    return jsonify(resp)


@soundings_api.route('/forecast/result/<job_id>.png')
def forecast_result_png(job_id):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return jsonify({'error': 'Unknown or expired job'}), 404
        if job['status'] != 'done':
            return jsonify({'error': f"Job not ready (status={job['status']})"}), 425
        png = job['png']
    return Response(png, mimetype='image/png')

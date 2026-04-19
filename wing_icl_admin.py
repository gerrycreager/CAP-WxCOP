"""
wing_icl_admin.py — Wing ICL Override Admin Blueprint
======================================================
Auth-gated admin page for Wing Directors of Operations and Stan/Eval
to manage CAPR 70-1 / CAPF 70-1A Individual/Command Limit overrides.

Access control:
  role=admin  — sees all wings (NHQ, gcreager)
  role=wing   — sees only their assigned wing_id

ICL overrides may only be MORE CONSERVATIVE than national defaults.
Ceiling and visibility overrides are strictly enforced in this direction.

Routes:
  GET  /admin/wing-icl          — list ICL entries for authorized wing(s)
  POST /admin/wing-icl/add      — add or update an ICL entry
  POST /admin/wing-icl/delete   — delete an ICL entry by id
"""

import logging
import os
import psycopg2
import psycopg2.extras
from flask import (Blueprint, render_template, request, redirect,
                   session, jsonify)

log = logging.getLogger(__name__)

wing_icl_admin = Blueprint('wing_icl_admin', __name__)

DB_HOST = os.environ.get('DB_HOST', '192.168.0.60')
DB_NAME = os.environ.get('DB_NAME', 'avwx_data')
DB_USER = os.environ.get('DB_USER', 'avwx_user')
DB_PASS = os.environ.get('DB_PASS', '')

# ---------------------------------------------------------------------------
# ICL parameter definitions
# Each entry: label, unit, national default caution, national default no-go,
#             more_conservative_only (True = ICL value must be >= national default,
#             i.e. stricter threshold means lower number for temp, higher for wind)
# ---------------------------------------------------------------------------
ICL_PARAMETERS = {
    # ── Wind ──────────────────────────────────────────────────────────────
    'wind_vfr_yellow': {
        'label':    'Surface Wind — Caution threshold',
        'unit':     'kts',
        'group':    'Wind',
        'caution':  True,
        'national': 21,
        'conservative': 'lower',   # ICL must be <= national (more restrictive)
        'hint':     'CAPF 70-1A: 21-30 kts = Nominal (default 21)',
    },
    'wind_vfr_red': {
        'label':    'Surface Wind — No-Go threshold',
        'unit':     'kts',
        'group':    'Wind',
        'caution':  False,
        'national': 30,
        'conservative': 'lower',
        'hint':     'CAPF 70-1A: >30 kts requires Wing DO approval (default 30)',
    },
    # ── Crosswind VFR ─────────────────────────────────────────────────────
    'crosswind_vfr_yellow': {
        'label':    'Crosswind VFR — Caution threshold',
        'unit':     'kts',
        'group':    'Crosswind VFR',
        'caution':  True,
        'national': 8,
        'conservative': 'lower',
        'hint':     'CAPR 70-1: default 8 kts',
    },
    'crosswind_vfr_red': {
        'label':    'Crosswind VFR — No-Go threshold',
        'unit':     'kts',
        'group':    'Crosswind VFR',
        'caution':  False,
        'national': 15,
        'conservative': 'lower',
        'hint':     'CAPR 70-1: default 15 kts (POH max crosswind)',
    },
    # ── Crosswind IFR ─────────────────────────────────────────────────────
    'crosswind_ifr_yellow': {
        'label':    'Crosswind IFR — Caution threshold',
        'unit':     'kts',
        'group':    'Crosswind IFR',
        'caution':  True,
        'national': 8,
        'conservative': 'lower',
        'hint':     'CAPR 70-1: default 8 kts',
    },
    'crosswind_ifr_red': {
        'label':    'Crosswind IFR — No-Go threshold',
        'unit':     'kts',
        'group':    'Crosswind IFR',
        'caution':  False,
        'national': 13,
        'conservative': 'lower',
        'hint':     'CAPR 70-1: default 13 kts',
    },
    # ── Temperature ───────────────────────────────────────────────────────
    'temp_cold_yellow': {
        'label':    'Cold Temperature — Caution threshold',
        'unit':     '°F',
        'group':    'Temperature',
        'caution':  True,
        'national': 20,
        'conservative': 'higher',  # ICL must be >= national (flag lower temps sooner)
        'hint':     'CAPF 70-1A: 20-39°F = Reduced Risk (default 20°F)',
    },
    'temp_cold_red': {
        'label':    'Cold Temperature — No-Go threshold',
        'unit':     '°F',
        'group':    'Temperature',
        'caution':  False,
        'national': -10,
        'conservative': 'higher',
        'hint':     'CAPF 70-1A: -10°F requires Wing DO approval (default -10°F)',
    },
    # ── Ceiling VFR ───────────────────────────────────────────────────────
    'ceil_vfr_yellow': {
        'label':    'Ceiling VFR — Caution threshold',
        'unit':     'ft',
        'group':    'Ceiling / Visibility',
        'caution':  True,
        'national': 800,
        'conservative': 'higher',  # more conservative = flag at higher ceiling
        'hint':     'CAPF 70-1A: <800ft = SFRO approval required (default 800ft) — MORE conservative only',
    },
    'ceil_vfr_red': {
        'label':    'Ceiling VFR — No-Go threshold',
        'unit':     'ft',
        'group':    'Ceiling / Visibility',
        'caution':  False,
        'national': 500,
        'conservative': 'higher',
        'hint':     'CAPF 70-1A: <500ft requires Wing DO approval (default 500ft) — MORE conservative only',
    },
    # ── Visibility VFR ────────────────────────────────────────────────────
    'vis_vfr_yellow': {
        'label':    'Visibility VFR — Caution threshold',
        'unit':     'SM',
        'group':    'Ceiling / Visibility',
        'caution':  True,
        'national': 2.0,
        'conservative': 'higher',
        'hint':     'CAPF 70-1A: <2SM = SFRO approval required (default 2.0 SM) — MORE conservative only',
    },
    'vis_vfr_red': {
        'label':    'Visibility VFR — No-Go threshold',
        'unit':     'SM',
        'group':    'Ceiling / Visibility',
        'caution':  False,
        'national': 1.0,
        'conservative': 'higher',
        'hint':     'CAPF 70-1A: <1SM requires Wing DO approval (default 1.0 SM) — MORE conservative only',
    },
}

def get_db():
    return psycopg2.connect(
        host=DB_HOST, dbname=DB_NAME, user=DB_USER, password=DB_PASS,
        connect_timeout=5
    )

def require_icl_auth():
    if not session.get('authenticated'):
        return None
    return {
        'username': session.get('username'),
        'role':     session.get('role', 'admin'),
        'wing_id':  session.get('wing_id'),
    }

def get_wings_and_regions(conn):
    """Return deduplicated wings and distinct regions."""
    with conn.cursor() as cur:
        # Distinct wings — one row per wing_id
        cur.execute("""
            SELECT DISTINCT ON (wing_id) wing_id, region_code
            FROM observations.wing_map
            ORDER BY wing_id, region_code
        """)
        wings = cur.fetchall()
        # Distinct regions with wing counts
        cur.execute("""
            SELECT region_code, COUNT(DISTINCT wing_id) as wing_count
            FROM observations.wing_map
            GROUP BY region_code
            ORDER BY region_code
        """)
        regions = cur.fetchall()
    conn.commit()
    return wings, regions

def get_icl_entries(conn, wing_id=None):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if wing_id:
            cur.execute("""
                SELECT id, wing_id, parameter, threshold, regulation,
                       notes, effective, expires, created_at
                FROM observations.wing_icl
                WHERE wing_id = %s
                ORDER BY wing_id, parameter
            """, (wing_id,))
        else:
            cur.execute("""
                SELECT id, wing_id, parameter, threshold, regulation,
                       notes, effective, expires, created_at
                FROM observations.wing_icl
                ORDER BY wing_id, parameter
            """)
        entries = cur.fetchall()
    conn.commit()
    return entries

def validate_conservative(parameter, threshold):
    """
    Enforce that ICL overrides are more conservative than national defaults.
    Returns (ok, error_message).
    """
    meta = ICL_PARAMETERS.get(parameter)
    if not meta:
        return False, 'Unknown parameter'
    national = meta['national']
    direction = meta['conservative']
    if direction == 'lower' and threshold > national:
        return False, (f"{meta['label']}: ICL threshold ({threshold} {meta['unit']}) "
                      f"must be ≤ national default ({national} {meta['unit']}). "
                      f"Wing ICLs may only be more conservative.")
    if direction == 'higher' and threshold < national:
        return False, (f"{meta['label']}: ICL threshold ({threshold} {meta['unit']}) "
                      f"must be ≥ national default ({national} {meta['unit']}). "
                      f"Wing ICLs may only be more conservative.")
    return True, None

@wing_icl_admin.route('/admin/wing-icl')
def icl_index():
    user = require_icl_auth()
    if not user:
        login_url = request.script_root + '/auth/login?next=' + request.url
        return redirect(login_url)

    error = request.args.get('error')
    success = request.args.get('success')

    try:
        conn = get_db()
        wing_id = None if user['role'] == 'admin' else user['wing_id']
        entries = get_icl_entries(conn, wing_id)
        wings, regions = get_wings_and_regions(conn) if user['role'] == 'admin' else ([], [])
        conn.close()
    except Exception as e:
        log.error(f'ICL admin DB error: {e}')
        entries, wings = [], []

    # Group parameters by group name for template
    groups = {}
    for key, meta in ICL_PARAMETERS.items():
        g = meta['group']
        if g not in groups:
            groups[g] = []
        groups[g].append((key, meta))

    return render_template('wing_icl_admin.html',
        user=user,
        entries=entries,
        wings=wings,
        regions=regions if user['role'] == 'admin' else [],
        parameters=ICL_PARAMETERS,
        groups=groups,
        error=error,
        success=success,
    )

@wing_icl_admin.route('/admin/wing-icl/add', methods=['POST'])
def icl_add():
    user = require_icl_auth()
    if not user:
        return redirect(request.script_root + '/auth/login')

    wing_id    = request.form.get('wing_id', '').strip().upper()
    parameter  = request.form.get('parameter', '').strip()
    threshold  = request.form.get('threshold', '').strip()
    regulation = request.form.get('regulation', '').strip()
    notes      = request.form.get('notes', '').strip()
    effective  = request.form.get('effective', '').strip() or None
    expires    = request.form.get('expires', '').strip() or None

    # Wing users can only add for their own wing
    if user['role'] == 'wing':
        wing_id = user['wing_id']

    # Validate parameter
    if not wing_id or parameter not in ICL_PARAMETERS:
        return redirect(request.script_root +
                       '/admin/wing-icl?error=Invalid+wing+or+parameter')

    # Validate threshold
    try:
        threshold = float(threshold)
    except ValueError:
        return redirect(request.script_root +
                       '/admin/wing-icl?error=Invalid+threshold+value')

    # Enforce more-conservative-only rule
    ok, err = validate_conservative(parameter, threshold)
    if not ok:
        import urllib.parse
        return redirect(request.script_root +
                       '/admin/wing-icl?error=' + urllib.parse.quote(err))

    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO observations.wing_icl
                    (wing_id, parameter, threshold, regulation, notes,
                     effective, expires, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (wing_id, parameter)
                DO UPDATE SET
                    threshold  = EXCLUDED.threshold,
                    regulation = EXCLUDED.regulation,
                    notes      = EXCLUDED.notes,
                    effective  = EXCLUDED.effective,
                    expires    = EXCLUDED.expires,
                    created_at = NOW()
            """, (wing_id, parameter, threshold,
                  regulation or None, notes or None,
                  effective, expires))
        conn.commit()
        conn.close()
        log.info(f'ICL upsert: {wing_id}/{parameter}={threshold} by {user["username"]}')
    except Exception as e:
        log.error(f'ICL add error: {e}')
        return redirect(request.script_root +
                       '/admin/wing-icl?error=Database+error')

    return redirect(request.script_root +
                   '/admin/wing-icl?success=Override+saved')

@wing_icl_admin.route('/admin/wing-icl/delete', methods=['POST'])
def icl_delete():
    user = require_icl_auth()
    if not user:
        return redirect(request.script_root + '/auth/login')

    try:
        entry_id = int(request.form.get('id', ''))
    except ValueError:
        return redirect(request.script_root + '/admin/wing-icl')

    try:
        conn = get_db()
        with conn.cursor() as cur:
            if user['role'] == 'wing':
                cur.execute("""
                    DELETE FROM observations.wing_icl
                    WHERE id = %s AND wing_id = %s
                """, (entry_id, user['wing_id']))
            else:
                cur.execute("DELETE FROM observations.wing_icl WHERE id = %s",
                            (entry_id,))
        conn.commit()
        conn.close()
        log.info(f'ICL delete id={entry_id} by {user["username"]}')
    except Exception as e:
        log.error(f'ICL delete error: {e}')

    return redirect(request.script_root + '/admin/wing-icl')

"""
wxcop_geo_admin.py — CAP WxCOP Access & Threat Report (admin page + API)

Serves the pre-generated geo/fail2ban report produced by
scripts/r815/wxcop_geo_access_report.py, cached to
/var/lib/wxcop_geo_report/latest.json every 15 minutes by the
wxcop-geo-report-cache systemd timer (runs as root - reads the Apache
access log and the fail2ban socket, neither of which www-data can touch).
This blueprint only ever reads that cache file; it never runs the script
or touches log/fail2ban permissions directly.

URL: /admin/geo-access        (linked from the landing page Administration section)
Auth: login_required / login_required_json (same session auth as other admin pages)
"""

import json
from datetime import datetime, timezone

from flask import Blueprint, render_template, jsonify
from auth import login_required, login_required_json

wxcop_geo_admin = Blueprint('wxcop_geo_admin', __name__)

REPORT_FILE = '/var/lib/wxcop_geo_report/latest.json'
STALE_AFTER_SECONDS = 20 * 60


@wxcop_geo_admin.route('/admin/geo-access')
@login_required
def index():
    return render_template('wxcop_geo_admin.html')


@wxcop_geo_admin.route('/admin/geo-access/api/report')
@login_required_json
def api_report():
    try:
        with open(REPORT_FILE) as f:
            data = json.load(f)
    except FileNotFoundError:
        return jsonify({'error': 'No report generated yet — cache timer may not have run.'}), 503
    except (OSError, json.JSONDecodeError) as e:
        return jsonify({'error': f'Report cache unreadable: {e}'}), 503

    generated_at = datetime.fromisoformat(data['generated_at'])
    age = (datetime.now(timezone.utc) - generated_at).total_seconds()
    data['stale'] = age > STALE_AFTER_SECONDS

    return jsonify(data)

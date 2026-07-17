#!/usr/bin/env python3
"""
wxcop_freshness_monitor.py — Data pipeline freshness/heartbeat monitor

Purpose:
    Detects silent pipeline failures in WxCOP by checking, per configured
    source:
      - DB_MAX      : freshness of the newest row in a DB table (for cyclic
                       feeds like LAMP, GLMP, GFS — a fixed cadence means a
                       stale MAX(timestamp) is a real failure)
      - FILE_MTIME  : freshness of the newest file matching a glob pattern
                       (for feeds that land on disk before/without a DB step)
      - LOG_HEARTBEAT: whether a "success" pattern has appeared in a log file
                       recently, REGARDLESS of whether new data existed (for
                       event-driven feeds like VTEC/WWA, where "no new
                       watches in 3 hours" is normal, but "ingest script
                       hasn't logged a successful run in 3 hours" is not)

Alerting:
    - Alerts once when a check transitions OK -> STALE (not on every poll)
    - Re-alerts on an escalation interval while still STALE (default 4h)
    - Sends a single RECOVERED notice on STALE -> OK
    - State persisted to a JSON file so this is safe to run frequently

Deploy:
    Runs standalone with psycopg2 + stdlib only. Intended for the host with
    DB access (data2) and/or NFS visibility into /LDM/models. If a single
    host can't see everything, run one instance per host with only the
    checks relevant to that host in CHECKS below.

Install as a systemd timer (see wxcop-monitor.timer / .service alongside
this script) rather than cron, per LDM/WxCOP convention of using systemd
for anything that should be supervised and logged consistently.
"""

import glob
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Callable, Optional

# ----------------------------------------------------------------------
# Configuration — edit this section to add/remove/adjust checks
# ----------------------------------------------------------------------

STATE_FILE = Path("/var/lib/wxcop_monitor/state.json")
LOG_FILE = Path("/var/log/cap_wxcop/freshness_monitor.log")

# How long a check must remain STALE before we re-send the alert (avoid
# spamming every 5 minutes while an outage is already known/being worked)
ESCALATION_INTERVAL = timedelta(hours=4)

# Email alerting via msmtp (/etc/msmtprc, relays through Gmail SMTP as
# capwxcop.alerts@gmail.com) -- there's no local MTA on this box, so
# smtplib-to-localhost silently failed on every alert until this was
# found. msmtprc is 600 root:www-data; this script runs as ldm (systemd
# User=ldm), so it needs a POSIX ACL for read access:
#   setfacl -m u:ldm:r /etc/msmtprc
# From address must match the authenticated msmtp account -- Gmail
# rejects/rewrites an arbitrary From header otherwise. Swap ALERT_TO for
# an SMS gateway address (e.g. 1234567890@vtext.com) to also text
# yourself, same pattern planned for cadet lightning alerts.
ALERT_FROM = "capwxcop.alerts@gmail.com"
ALERT_TO = ["gcreager@capnhq.gov"]
MSMTP_BIN = "/usr/bin/msmtp"


@dataclass
class Check:
    name: str
    kind: str                      # "db_max" | "file_mtime" | "log_heartbeat"
    max_age: timedelta
    # db_max
    db_query: Optional[str] = None
    # file_mtime
    file_glob: Optional[str] = None
    # log_heartbeat
    log_path: Optional[str] = None
    success_pattern: Optional[str] = None
    tail_lines: int = 2000


DB_DSN = dict(
    host="192.168.0.60",
    dbname="avwx_data",
    user="avwx_user",
)
_db_password = os.environ.get("AVWX_DB_PASSWORD")
if _db_password:
    DB_DSN["password"] = _db_password

CHECKS = [
    Check(
        name="LAMP TSTM01 (thunderstorm probability)",
        kind="db_max",
        max_age=timedelta(minutes=90),
        db_query="""
            SELECT MAX(ingested_at) FROM observations.airport_wx_impacts
            WHERE model_source = 'LAMP'
        """,
    ),
    Check(
        name="GLMP CONUS (VFR/IFR impacts)",
        kind="db_max",
        max_age=timedelta(minutes=90),
        db_query="""
            SELECT MAX(ingested_at) FROM observations.airport_wx_impacts
            WHERE model_source = 'GLMP_CO'
        """,
    ),
    Check(
        name="GFS OCONUS ingest",
        kind="db_max",
        max_age=timedelta(hours=7),
        db_query="""
            SELECT MAX(ingested_at) FROM observations.model_wind_forecasts
            WHERE model_name = 'GFS'
        """,
    ),
    Check(
        name="TDWR poller",
        kind="file_mtime",
        max_age=timedelta(minutes=10),
        file_glob="/LDM/radar/level3/*/T*/nids/*/*.nids",
    ),
    # GLM East (G19) and West (G18) are two independent satellite feeds,
    # not a primary/failover pair -- checked separately because a raw-file
    # check across both directories combined would mask a real outage on
    # one satellite as long as the other keeps delivering (this is exactly
    # how a live G19 outage went unnoticed: querying the combined
    # observations.glm_flashes table showed "fresh" because G18/West
    # flashes were still landing normally). Checks file arrival, not flash
    # content, since a legitimately quiet lightning period on one satellite
    # shouldn't alert -- LCFA granules are produced continuously regardless
    # of whether they contain any flashes.
    Check(
        name="GLM East (G19) lightning feed",
        kind="file_mtime",
        max_age=timedelta(minutes=15),
        file_glob="/LDM/satellite/GLM/EAST/*.nc",
    ),
    Check(
        name="GLM West (G18) lightning feed",
        kind="file_mtime",
        max_age=timedelta(minutes=15),
        file_glob="/LDM/satellite/GLM/WEST/*.nc",
    ),
    Check(
        # ingest_wwa_vtec.py logs "Done: N op(s)" on every pqact invocation,
        # ops=0 included -- this is a heartbeat that the pipe is alive, not
        # a claim that a hazard is active. WWA/VTEC products are issued
        # as-needed, and calm/stagnant weather can mean multiple days with
        # nothing matching nationwide, so the threshold has to be generous
        # enough to not false-alarm on genuinely quiet weather.
        name="VTEC/WWA ingest heartbeat",
        kind="log_heartbeat",
        max_age=timedelta(hours=36),
        log_path="/var/log/cap_wxcop/wwa_vtec_ingest.log",
        success_pattern="Done:",
    ),
    Check(
        name="AF Weather email parser heartbeat",
        kind="log_heartbeat",
        max_age=timedelta(hours=2),
        log_path="/var/log/cap_wxcop/afwx_email.log",
        success_pattern="poll complete",
    ),
]

# ----------------------------------------------------------------------
# Implementation — should not need edits for normal use
# ----------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE) if LOG_FILE.parent.exists() else logging.NullHandler(),
    ],
)
log = logging.getLogger("wxcop_monitor")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            log.warning("State file unreadable, starting fresh")
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    tmp.replace(STATE_FILE)


def send_alert(subject: str, body: str) -> None:
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = ALERT_FROM
    msg["To"] = ", ".join(ALERT_TO)
    try:
        subprocess.run(
            [MSMTP_BIN] + ALERT_TO,
            input=msg.as_bytes(),
            check=True,
            timeout=20,
        )
        log.info(f"Alert sent: {subject}")
    except Exception as e:
        log.error(f"msmtp send failed: {e}")


def check_db_max(check: Check) -> Optional[datetime]:
    """Opens and closes its own short-lived connection so a slow/stuck
    query, or a run with several db_max checks, never leaves a connection
    sitting idle-in-transaction for the duration of unrelated checks."""
    import psycopg2

    conn = psycopg2.connect(**DB_DSN, connect_timeout=10)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '15s'")
            cur.execute(check.db_query)
            row = cur.fetchone()
    finally:
        conn.close()
    if not row or row[0] is None:
        return None
    ts = row[0]
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def check_file_mtime(check: Check) -> Optional[datetime]:
    """
    Newest mtime among files matching check.file_glob, skipping any that
    vanish between glob() and stat() rather than treating that race as a
    check failure. Confirmed in production: LDM's scour/retention cleanup
    can delete a just-globbed file (e.g. TDWR NIDS, 44 sites x 7 products,
    globbed and scoured concurrently) before getmtime() runs on it,
    raising FileNotFoundError -- which propagated all the way up to a
    false "STALE" alert (100% of TDWR poller alerts traced back to this,
    none were genuine sustained staleness). Only returns None if every
    matched file vanished or nothing matched at all.
    """
    matches = glob.glob(check.file_glob)
    if not matches:
        return None
    newest_mtime = None
    for f in matches:
        try:
            mtime = os.path.getmtime(f)
        except OSError:
            continue
        if newest_mtime is None or mtime > newest_mtime:
            newest_mtime = mtime
    if newest_mtime is None:
        return None
    return datetime.fromtimestamp(newest_mtime, tz=timezone.utc)


def check_log_heartbeat(check: Check) -> Optional[datetime]:
    """
    Returns the current time if the success pattern appears within the
    tail of the log recently enough to imply a live process, else None.
    We can't get a precise timestamp from arbitrary log formats reliably,
    so this checks: does the log file's mtime advance AND does the
    success pattern appear in the tail? Together these approximate "the
    process ran successfully recently" without needing to parse every
    possible log timestamp format.
    """
    path = Path(check.log_path)
    if not path.exists():
        return None
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    try:
        with path.open("r", errors="ignore") as f:
            lines = f.readlines()[-check.tail_lines:]
    except OSError:
        return None
    if any(check.success_pattern in line for line in lines):
        return mtime
    return None


def run_check(check: Check) -> tuple[bool, str]:
    """Returns (is_ok, detail_message)."""
    now = datetime.now(timezone.utc)

    if check.kind == "db_max":
        last = check_db_max(check)
    elif check.kind == "file_mtime":
        last = check_file_mtime(check)
    elif check.kind == "log_heartbeat":
        last = check_log_heartbeat(check)
    else:
        return False, f"Unknown check kind: {check.kind}"

    if last is None:
        return False, "No data/success pattern found at all"

    age = now - last
    if age > check.max_age:
        return False, f"Last success {age} ago (threshold {check.max_age})"
    return True, f"Last success {age} ago (within {check.max_age} threshold)"


def main():
    state = load_state()
    now = datetime.now(timezone.utc)

    for check in CHECKS:
        try:
            ok, detail = run_check(check)
        except Exception as e:
            ok, detail = False, f"Check raised exception: {e}"

        prev = state.get(check.name, {})
        was_stale = prev.get("status") == "STALE"
        last_alert_str = prev.get("last_alert_at")
        last_alert_at = (
            datetime.fromisoformat(last_alert_str) if last_alert_str else None
        )

        if ok:
            log.info(f"[OK]    {check.name}: {detail}")
            if was_stale:
                send_alert(
                    subject=f"[WxCOP] RECOVERED: {check.name}",
                    body=f"{check.name} is healthy again.\n\n{detail}",
                )
            state[check.name] = {"status": "OK", "last_alert_at": None}
        else:
            log.warning(f"[STALE] {check.name}: {detail}")
            should_alert = (not was_stale) or (
                last_alert_at is not None
                and now - last_alert_at >= ESCALATION_INTERVAL
            )
            if should_alert:
                send_alert(
                    subject=f"[WxCOP] STALE: {check.name}",
                    body=f"{check.name} appears to have stopped updating.\n\n{detail}",
                )
                state[check.name] = {
                    "status": "STALE",
                    "last_alert_at": now.isoformat(),
                }
            else:
                state[check.name] = prev  # keep existing alert timestamp

    save_state(state)


if __name__ == "__main__":
    main()

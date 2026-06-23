#!/usr/bin/env python3
"""
ingest_afwx_email.py -- AF Weather email ingest for CAP WxCOP
==============================================================
Polls capwxcop.alerts@gmail.com via IMAP for:

  1. AF Weather WWA notification emails (from afweather.mil)
       Subject: Weather Warning 06-007 for Camp Atterbury (IN5)
       -> parsed and inserted into observations.cadet_notification_log

  2. KQ station TAF emails (from afweather.mil OR forwarded by CAP staff)
       Body contains a TAF block starting with "TAF KQ..."
       -> extracted and inserted into observations.taf

Site/airfield mapping is maintained in /etc/cap_wxcop_afwx_sites.conf:

  [sites]
  IN5 = 18   ; cadet_site_id in observations.cadet_sites

  [airfields]
  KBAK = KBAK  ; airport station_id in observations.airports
                ; add when CAP requests AF Weather WWA coverage for an airfield
                ; remove when operation ends

Runs every 60 seconds via cron:
  * * * * * /var/www/cap_winds_app/venv/bin/python3 \
      /var/www/cap_winds_app/scripts/ingest_afwx_email.py \
      >> /var/log/cap_wxcop/afwx_email.log 2>&1
"""

import configparser
import email
import email.header
import imaplib
import logging
import re
import sys
from datetime import datetime, timezone

import psycopg2

# -- Configuration -------------------------------------------------------------
IMAP_CONF     = '/etc/cap_wxcop_imap.conf'
SITES_CONF    = '/etc/cap_wxcop_afwx_sites.conf'
DB_HOST       = '192.168.0.60'
DB_NAME       = 'avwx_data'
DB_USER       = 'avwx_user'
DB_PASS       = 'avwx_pass'
AFWX_SENDER   = 'afweather.mil'      # primary trusted domain
CAP_SENDER    = 'capnhq.gov'
AFWX_MIL      = 'af.mil'         # allow forwarded TAFs from CAP staff
AFWX_LABEL    = 'afwx_processed'     # Gmail label to apply after processing

# -- Logging -------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%SZ',
)
log = logging.getLogger('ingest_afwx')

# -- Regex patterns ------------------------------------------------------------
# WWA subject: Weather Warning 06-007 for Camp Atterbury (IN5)
RE_SUBJECT = re.compile(
    r'(?:Cancellation\s+of\s+)?Weather\s+(?:Warning|Watch|Advisory)\s+'
    r'(\d{2}-\d{3})\s+for\s+(.+?)\s+\(([A-Z0-9]+)\)',
    re.IGNORECASE
)

# WWA body: valid 14/1354L (14/1754Z) UFN
RE_VALID = re.compile(
    r'valid\s+\d+/\d+L\s+\((\d+)/(\d{4})Z\)\s+(\S+)',
    re.IGNORECASE
)

# WWA body: Observed Lightning within 5 nm
RE_LIGHTNING = re.compile(
    r'Observed\s+Lightning\s+within\s+(\d+(?:\.\d+)?)\s+nm',
    re.IGNORECASE
)

# WWA body: Lightning Distance observed value 5 nautical miles
RE_DISTANCE = re.compile(
    r'Lightning\s+Distance\s+observed\s+value\s+(\d+(?:\.\d+)?)\s+nautical\s+miles',
    re.IGNORECASE
)

# TAF block: starts with "TAF" then a KQ station ID
# Captures the full TAF through the terminating '=' or end of TAF content
RE_TAF_START = re.compile(
    r'^TAF\s+([A-Z0-9]{4})\s+',
    re.IGNORECASE | re.MULTILINE
)

# TAF header for parsing times: TAF [AMD|COR] STID DDHHMMZ DDHH/DDHH
RE_TAF_HEADER = re.compile(
    r'TAF\s+(?:AMD\s+|COR\s+)?([A-Z0-9]{4})\s+(\d{6}Z)\s+(\d{4})/(\d{4})',
    re.IGNORECASE
)


# -- Site mapping --------------------------------------------------------------
def load_site_map():
    """
    Load AF Weather site code -> CAP cadet_site_id mapping ([sites] section)
    and operational airfield mapping ([airfields] section).

    Returns:
        cadet_map  : {afwx_code: cadet_site_id}   int values
        airfield_map: {afwx_code: station_id}      str values (e.g. 'KBAK')
    """
    cfg = configparser.ConfigParser()
    cfg.read(SITES_CONF)

    cadet_map = {}
    if 'sites' in cfg:
        for code, val in cfg['sites'].items():
            try:
                cadet_map[code.upper()] = int(val.split(';')[0].strip())
            except ValueError:
                pass

    airfield_map = {}
    if 'airfields' in cfg:
        for code, val in cfg['airfields'].items():
            station_id = val.split(';')[0].strip().upper()
            if station_id:
                airfield_map[code.upper()] = station_id

    return cadet_map, airfield_map


# -- Email helpers -------------------------------------------------------------
def decode_header(raw):
    """Decode RFC2047 encoded email header."""
    parts = email.header.decode_header(raw)
    decoded = []
    for part, enc in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(enc or 'utf-8', errors='replace'))
        else:
            decoded.append(part)
    return ' '.join(decoded)


def get_body(msg):
    """Extract plain text body from email message."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == 'text/plain':
                charset = part.get_content_charset() or 'utf-8'
                return part.get_payload(decode=True).decode(charset, errors='replace')
    else:
        charset = msg.get_content_charset() or 'utf-8'
        return msg.get_payload(decode=True).decode(charset, errors='replace')
    return ''


def is_trusted_sender(from_hdr):
    """
    Return True if email is from a trusted sender domain.
    Accepts AF Weather directly or CAP staff forwarding a TAF.
    """
    from_lower = from_hdr.lower()
    return AFWX_SENDER in from_lower or CAP_SENDER in from_lower or AFWX_MIL in from_lower


# -- TAF extraction ------------------------------------------------------------
def extract_taf(body):
    """
    Extract a TAF block from an email body.

    Handles two formats:
      1. Bare TAF (body IS the TAF, possibly with whitespace/line wrapping)
      2. Embedded TAF in conversational email prose

    Returns the raw TAF string (normalized whitespace) or None.
    Only extracts KQ station TAFs (station_id starts with 'KQ').
    """
    # Normalize line endings
    body = body.replace('\r\n', '\n').replace('\r', '\n')

    # Find the start of a TAF block
    m = RE_TAF_START.search(body)
    if not m:
        return None

    station_id = m.group(1).upper()
    if not station_id.startswith('KQ'):
        log.debug("TAF found for non-KQ station %s -- skipping", station_id)
        return None

    # Extract from the TAF start position forward
    taf_start = m.start()
    taf_body = body[taf_start:]

    # Collect TAF lines until:
    #   - a line ending with '=' (ICAO terminator)
    #   - a blank line followed by a line that looks like prose (not a TAF group)
    #   - an email signature marker (V/R, --, From:, ________________________________)
    STOP_PATTERNS = re.compile(
        r'^(V/R|--|From:|_{10,}|Sent:|To:|Cc:|Subject:|Phone:|Email:)',
        re.IGNORECASE
    )

    lines = taf_body.split('\n')
    taf_lines = []
    for line in lines:
        stripped = line.strip()
        # Stop at email artifact lines
        if STOP_PATTERNS.match(stripped):
            break
        taf_lines.append(stripped)
        # Stop at ICAO terminator
        if stripped.endswith('='):
            break

    # Join and clean up
    raw_taf = ' '.join(l for l in taf_lines if l)
    if not raw_taf:
        return None

    # Must still contain a parseable TAF header
    if not RE_TAF_HEADER.search(raw_taf):
        log.debug("Extracted text does not contain valid TAF header: %s", raw_taf[:80])
        return None

    return raw_taf


def parse_taf_times(raw_taf):
    """
    Parse issue_time, valid_from, valid_to from a raw TAF string.
    Returns (station_id, issue_time, valid_from, valid_to) as naive UTC datetimes,
    or raises ValueError on parse failure.
    """
    m = RE_TAF_HEADER.search(raw_taf)
    if not m:
        raise ValueError("Cannot parse TAF header: " + raw_taf[:80])

    station_id = m.group(1).upper()
    issue_str  = m.group(2)   # DDHHMMZ
    vf_str     = m.group(3)   # DDHH
    vt_str     = m.group(4)   # DDHH

    now = datetime.now(timezone.utc)

    day = int(issue_str[0:2])
    hr  = int(issue_str[2:4])
    mn  = int(issue_str[4:6])
    issue_time = datetime(now.year, now.month, day, hr, mn)

    vf_day  = int(vf_str[0:2])
    vf_hour = int(vf_str[2:4])
    vt_day  = int(vt_str[0:2])
    vt_hour = int(vt_str[2:4])

    valid_from = datetime(now.year, now.month, vf_day, vf_hour, 0)
    valid_to   = datetime(now.year, now.month, vt_day, vt_hour, 0)

    # Handle month rollover
    if vt_day < vf_day:
        if now.month == 12:
            valid_to = valid_to.replace(year=now.year + 1, month=1)
        else:
            valid_to = valid_to.replace(month=now.month + 1)

    return station_id, issue_time, valid_from, valid_to


# -- WWA parsing ---------------------------------------------------------------
def parse_afwx_wwa(subject, body):
    """
    Parse AF Weather WWA notification email.
    Returns dict with parsed fields or None if not a WWA email.
    """
    sm = RE_SUBJECT.search(subject)
    if not sm:
        return None

    warn_num  = sm.group(1)
    site_name = sm.group(2)
    afwx_code = sm.group(3)

    vm = RE_VALID.search(body)
    valid_day  = int(vm.group(1)) if vm else None
    valid_hhmm = vm.group(2)      if vm else None
    valid_ufn  = vm.group(3)      if vm else None

    phenomenon  = 'UNKNOWN'
    distance_nm = None

    lm = RE_LIGHTNING.search(body)
    if lm:
        phenomenon  = 'LIGHTNING'
        dm = RE_DISTANCE.search(body)
        distance_nm = float(dm.group(1)) if dm else float(lm.group(1))

    # Cancellation
    if re.search(r'Cancellation', subject, re.IGNORECASE):
        phenomenon = 'CANCELLATION/' + phenomenon

    valid_time = None
    if valid_day and valid_hhmm:
        now = datetime.now(timezone.utc)
        try:
            valid_time = datetime(now.year, now.month, valid_day,
                                  int(valid_hhmm[0:2]), int(valid_hhmm[2:4]),
                                  tzinfo=timezone.utc)
        except ValueError:
            pass

    return {
        'warn_num':    warn_num,
        'site_name':   site_name,
        'afwx_code':   afwx_code,
        'phenomenon':  phenomenon,
        'distance_nm': distance_nm,
        'valid_time':  valid_time,
        'until':       valid_ufn,
        'raw_subject': subject,
        'raw_body':    body[:2000],
    }


# -- Database ------------------------------------------------------------------
def db_connect():
    return psycopg2.connect(
        host=DB_HOST, dbname=DB_NAME,
        user=DB_USER, password=DB_PASS
    )


def already_processed_wwa(conn, warn_num, afwx_code):
    """Check if this WWA number has already been logged in the last 24h."""
    cur = conn.cursor()
    cur.execute("""
        SELECT id FROM observations.cadet_notification_log
        WHERE details LIKE %s
        AND sent_at > NOW() - INTERVAL '24 hours'
        LIMIT 1
    """, (f'%{warn_num}%{afwx_code}%',))
    row = cur.fetchone()
    cur.close()
    return row is not None


def already_processed_taf(conn, station_id, issue_time):
    """Check if this TAF is already in observations.taf."""
    cur = conn.cursor()
    cur.execute("""
        SELECT id FROM observations.taf
        WHERE station_id = %s AND issue_time = %s
        LIMIT 1
    """, (station_id, issue_time))
    row = cur.fetchone()
    cur.close()
    return row is not None


def insert_wwa_notification(conn, site_id, parsed):
    """Insert AF Weather WWA into cadet_notification_log."""
    msg = (
        f"AF Weather {parsed['warn_num']}: "
        f"{parsed['phenomenon']}"
        + (f" within {parsed['distance_nm']:.0f} nm" if parsed['distance_nm'] else '')
        + f" at {parsed['site_name']} ({parsed['afwx_code']})"
        + (f", valid until {parsed['until']}" if parsed['until'] else '')
    )
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO observations.cadet_notification_log
            (site_id, alert_type, details)
        VALUES (%s, %s, %s)
        ON CONFLICT DO NOTHING
    """, (site_id, parsed['phenomenon'], msg))
    conn.commit()
    cur.close()
    log.info("WWA logged: %s", msg)
    return msg


def insert_taf(conn, station_id, issue_time, valid_from, valid_to, raw_taf):
    """
    Insert KQ TAF into observations.taf.
    Location is pulled from observations.kq_stations if available,
    falling back to observations.airports for co-located host stations.
    """
    cur = conn.cursor()

    # Try kq_stations first, then airports for location
    cur.execute("""
        SELECT location FROM observations.kq_stations
        WHERE station_id = %s
        LIMIT 1
    """, (station_id,))
    row = cur.fetchone()

    if row:
        location = row[0]
    else:
        # Fallback: check kq_associations for host airfield location
        cur.execute("""
            SELECT a.location
            FROM observations.kq_associations ka
            JOIN observations.airports a ON a.station_id = ka.host_station
            WHERE ka.kq_station = %s
            AND now() BETWEEN ka.valid_from AND COALESCE(ka.valid_to, 'infinity')
            LIMIT 1
        """, (station_id,))
        row = cur.fetchone()
        location = row[0] if row else None

    cur.execute("""
        INSERT INTO observations.taf
            (station_id, issue_time, valid_from, valid_to, raw_text, location)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (station_id, issue_time)
        DO UPDATE SET
            valid_from = EXCLUDED.valid_from,
            valid_to   = EXCLUDED.valid_to,
            raw_text   = EXCLUDED.raw_text,
            location   = COALESCE(EXCLUDED.location, observations.taf.location)
    """, (station_id, issue_time, valid_from, valid_to, raw_taf, location))
    conn.commit()
    cur.close()
    log.info("TAF inserted: %s issued %s valid %s/%s",
             station_id, issue_time, valid_from, valid_to)


def send_alert(msg, site_id, conn):
    """Send alert via msmtp pipeline."""
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT r.phone_number, r.email
            FROM observations.cadet_notification_recipients r
            WHERE r.site_id = %s AND r.active = TRUE
        """, (site_id,))
        recipients = cur.fetchall()
        cur.close()

        import subprocess
        for phone, email_addr in recipients:
            if email_addr:
                subprocess.run(
                    ['msmtp', email_addr],
                    input=f"Subject: CAP WxCOP AF Weather Alert\n\n{msg}\n",
                    text=True, capture_output=True
                )
                log.info("Email sent to %s", email_addr)
    except Exception as e:
        log.error("Alert send failed: %s", e)


# -- Main ----------------------------------------------------------------------
def main():
    imap_cfg = configparser.ConfigParser()
    imap_cfg.read(IMAP_CONF)
    if 'imap' not in imap_cfg:
        log.error("IMAP config not found: %s", IMAP_CONF)
        sys.exit(1)

    cadet_map, airfield_map = load_site_map()
    if not cadet_map and not airfield_map:
        log.warning("No site mappings found in %s", SITES_CONF)

    try:
        mail = imaplib.IMAP4_SSL(
            imap_cfg['imap']['host'],
            int(imap_cfg['imap']['port'])
        )
        mail.login(imap_cfg['imap']['username'], imap_cfg['imap']['password'])
        mail.select('INBOX')
    except Exception as e:
        log.error("IMAP connect failed: %s", e)
        sys.exit(1)

    typ, data = mail.search(None, 'UNSEEN')
    if typ != 'OK' or not data[0]:
        log.debug("No unread messages")
        mail.logout()
        return

    msg_ids = data[0].split()
    log.info("Found %d unread message(s)", len(msg_ids))

    conn = db_connect()
    processed = 0

    for mid in msg_ids:
        try:
            typ, msg_data = mail.fetch(mid, '(RFC822)')
            if typ != 'OK':
                continue

            msg     = email.message_from_bytes(msg_data[0][1])
            from_hdr = decode_header(msg.get('From', ''))
            subject  = decode_header(msg.get('Subject', ''))
            body     = get_body(msg)

            log.info("Processing: %s", subject)

            # Reject non-trusted senders
            if not is_trusted_sender(from_hdr):
                log.debug("Skipping untrusted sender: %s", from_hdr)
                mail.store(mid, '+FLAGS', '\\Seen')
                continue

            handled = False

            # ── Path 1: TAF extraction (body-driven, any trusted sender) ──
            raw_taf = extract_taf(body)
            if raw_taf:
                try:
                    station_id, issue_time, valid_from, valid_to = parse_taf_times(raw_taf)
                    if already_processed_taf(conn, station_id, issue_time):
                        log.info("TAF already in DB: %s %s", station_id, issue_time)
                    else:
                        insert_taf(conn, station_id, issue_time,
                                   valid_from, valid_to, raw_taf)
                    handled = True
                except ValueError as e:
                    log.warning("TAF parse error: %s", e)

            # ── Path 2: WWA notification (subject-driven, afweather.mil) ──
            if AFWX_SENDER in from_hdr.lower():
                parsed = parse_afwx_wwa(subject, body)
                if parsed:
                    afwx_code = parsed['afwx_code']

                    # Check cadet sites
                    site_id = cadet_map.get(afwx_code)
                    if site_id:
                        if already_processed_wwa(conn, parsed['warn_num'], afwx_code):
                            log.info("WWA already processed: %s", parsed['warn_num'])
                        else:
                            alert_msg = insert_wwa_notification(conn, site_id, parsed)
                            send_alert(alert_msg, site_id, conn)
                        handled = True

                    # Check operational airfields
                    elif afwx_code in airfield_map:
                        station = airfield_map[afwx_code]
                        log.info("WWA for operational airfield %s (%s): %s %s",
                                 station, afwx_code,
                                 parsed['phenomenon'], parsed['warn_num'])
                        # Log to file only for now; no cadet_notification_log row
                        # (no cadet_site_id to reference). Future: airfield_wx_log table.
                        handled = True

                    else:
                        log.warning("Unknown AF Weather site code: %s -- add to %s",
                                    afwx_code, SITES_CONF)
                        # Mark read to avoid looping on unknown sites
                        mail.store(mid, '+FLAGS', '\\Seen')
                        continue

            if not handled:
                log.warning("Message not recognized as TAF or WWA: %s", subject)

            mail.store(mid, '+FLAGS', '\\Seen')
            processed += 1

        except Exception as e:
            log.error("Error processing message %s: %s", mid, e)

    conn.close()
    mail.logout()
    log.info("Done: %d message(s) processed", processed)


if __name__ == '__main__':
    main()

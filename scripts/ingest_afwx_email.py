#!/usr/bin/env python3
"""
ingest_afwx_email.py — AF Weather email notification ingest for CAP WxCOP
==========================================================================
Polls capwxcop.alerts@gmail.com via IMAP for AF Weather warning emails,
parses them, and inserts into observations.cadet_notification_log.
Optionally triggers SMS via existing Twilio/msmtp pipeline.

AF Weather email format:
  From:    AF Weather Notifications <no-reply@afweather.mil>
  Subject: Weather Warning 06-007 for Camp Atterbury (IN5)
  Body:    Weather Warning 06-007 for Camp Atterbury (IN5) valid
           14/1354L (14/1754Z) UFN Observed Lightning within 5 nm.
           Lightning Distance observed value 5 nautical miles.

AF Weather site code → CAP cadet_site mapping is maintained in
/etc/cap_wxcop_afwx_sites.conf (INI format, [sites] section).

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

# ── Configuration ──────────────────────────────────────────────────────────────
IMAP_CONF     = '/etc/cap_wxcop_imap.conf'
SITES_CONF    = '/etc/cap_wxcop_afwx_sites.conf'
DB_HOST       = '192.168.0.60'
DB_NAME       = 'avwx_data'
DB_USER       = 'avwx_user'
DB_PASS       = 'avwx_pass'
AFWX_SENDER   = 'afweather.mil'   # domain to accept mail from
AFWX_LABEL    = 'afwx_processed'  # Gmail label to apply after processing

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%SZ',
)
log = logging.getLogger('ingest_afwx')

# ── Regex patterns ─────────────────────────────────────────────────────────────
# Subject: Weather Warning 06-007 for Camp Atterbury (IN5)
RE_SUBJECT = re.compile(
    r'Weather\s+(?:Warning|Watch|Advisory)\s+'
    r'(\d{2}-\d{3})\s+for\s+(.+?)\s+\(([A-Z0-9]+)\)',
    re.IGNORECASE
)

# Body: valid 14/1354L (14/1754Z) UFN
RE_VALID = re.compile(
    r'valid\s+\d+/\d+L\s+\((\d+)/(\d{4})Z\)\s+(\S+)',
    re.IGNORECASE
)

# Body: Observed Lightning within 5 nm
RE_LIGHTNING = re.compile(
    r'(Observed\s+Lightning\s+within\s+(\d+(?:\.\d+)?)\s+nm)',
    re.IGNORECASE
)

# Body: Lightning Distance observed value 5 nautical miles
RE_DISTANCE = re.compile(
    r'Lightning\s+Distance\s+observed\s+value\s+(\d+(?:\.\d+)?)\s+nautical\s+miles',
    re.IGNORECASE
)

# ── Site mapping ───────────────────────────────────────────────────────────────
def load_site_map():
    """
    Load AF Weather site code → CAP cadet_site_id mapping.
    Config format (/etc/cap_wxcop_afwx_sites.conf):
      [sites]
      IN5 = 12        ; site_id in observations.cadet_sites
      CO3 = 7
    Returns dict {afwx_code: cadet_site_id}
    """
    cfg = configparser.ConfigParser()
    cfg.read(SITES_CONF)
    sites = {}
    if 'sites' in cfg:
        for code, sid in cfg['sites'].items():
            try:
                sites[code.upper()] = int(sid.split(';')[0].strip())
            except ValueError:
                pass
    return sites


# ── Email helpers ──────────────────────────────────────────────────────────────
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


# ── Parsing ────────────────────────────────────────────────────────────────────
def parse_afwx_email(subject, body):
    """
    Parse AF Weather notification email.
    Returns dict with parsed fields or None if not recognized.
    """
    # Parse subject
    sm = RE_SUBJECT.search(subject)
    if not sm:
        log.debug("Subject not recognized: %s", subject)
        return None

    warn_num  = sm.group(1)   # 06-007
    site_name = sm.group(2)   # Camp Atterbury
    afwx_code = sm.group(3)   # IN5

    # Parse valid time from body
    vm = RE_VALID.search(body)
    valid_day  = int(vm.group(1))   if vm else None
    valid_hhmm = vm.group(2)        if vm else None
    valid_ufn  = vm.group(3)        if vm else None   # UFN or end time

    # Parse phenomenon
    phenomenon = 'UNKNOWN'
    distance_nm = None

    lm = RE_LIGHTNING.search(body)
    if lm:
        phenomenon  = 'LIGHTNING'
        dm = RE_DISTANCE.search(body)
        if dm:
            distance_nm = float(dm.group(1))
        else:
            # Try to get distance from lightning pattern
            try:
                distance_nm = float(lm.group(2))
            except (ValueError, IndexError):
                pass

    # Build valid_time UTC
    valid_time = None
    if valid_day and valid_hhmm:
        now = datetime.now(timezone.utc)
        hr  = int(valid_hhmm[0:2])
        mn  = int(valid_hhmm[2:4])
        try:
            valid_time = datetime(now.year, now.month, valid_day,
                                  hr, mn, tzinfo=timezone.utc)
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


# ── Database ───────────────────────────────────────────────────────────────────
def db_connect():
    return psycopg2.connect(
        host=DB_HOST, dbname=DB_NAME,
        user=DB_USER, password=DB_PASS
    )


def already_processed(conn, warn_num, afwx_code):
    """Check if this warning number has already been logged."""
    cur = conn.cursor()
    cur.execute("""
        SELECT id FROM observations.cadet_notification_log
        WHERE message LIKE %s
        AND created_at > NOW() - INTERVAL '24 hours'
        LIMIT 1
    """, (f'%{warn_num}%{afwx_code}%',))
    row = cur.fetchone()
    cur.close()
    return row is not None


def insert_notification(conn, site_id, parsed):
    """Insert AF Weather notification into cadet_notification_log."""
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
            (site_id, notification_type, message, sent_at)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT DO NOTHING
    """, (site_id, parsed['phenomenon'], msg))
    conn.commit()
    cur.close()
    log.info("Logged: %s", msg)
    return msg


def send_alert(msg, site_id, conn):
    """Send SMS/email alert via existing msmtp pipeline."""
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


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    # Load configs
    imap_cfg = configparser.ConfigParser()
    imap_cfg.read(IMAP_CONF)
    if 'imap' not in imap_cfg:
        log.error("IMAP config not found: %s", IMAP_CONF)
        sys.exit(1)

    site_map = load_site_map()
    if not site_map:
        log.warning("No AF Weather site mappings found in %s", SITES_CONF)

    # Connect IMAP
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

    # Search for unread AF Weather emails
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

            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)

            # Check sender domain
            from_hdr = decode_header(msg.get('From', ''))
            if AFWX_SENDER not in from_hdr.lower():
                log.debug("Skipping non-AF Weather email from: %s", from_hdr)
                # Still mark as read to avoid reprocessing
                mail.store(mid, '+FLAGS', '\\Seen')
                continue

            subject = decode_header(msg.get('Subject', ''))
            body    = get_body(msg)

            log.info("Processing: %s", subject)

            parsed = parse_afwx_email(subject, body)
            if not parsed:
                log.warning("Could not parse: %s", subject)
                mail.store(mid, '+FLAGS', '\\Seen')
                continue

            # Look up CAP site
            afwx_code = parsed['afwx_code']
            site_id   = site_map.get(afwx_code)
            if not site_id:
                log.warning("Unknown AF Weather site code: %s — add to %s",
                            afwx_code, SITES_CONF)
                # Still mark read — we don't want to loop on unknown sites
                mail.store(mid, '+FLAGS', '\\Seen')
                continue

            # Deduplicate
            if already_processed(conn, parsed['warn_num'], afwx_code):
                log.info("Already processed: %s", parsed['warn_num'])
                mail.store(mid, '+FLAGS', '\\Seen')
                continue

            # Insert and alert
            alert_msg = insert_notification(conn, site_id, parsed)
            send_alert(alert_msg, site_id, conn)

            # Mark as read
            mail.store(mid, '+FLAGS', '\\Seen')
            processed += 1

        except Exception as e:
            log.error("Error processing message %s: %s", mid, e)

    conn.close()
    mail.logout()
    log.info("Done: %d message(s) processed", processed)


if __name__ == '__main__':
    main()

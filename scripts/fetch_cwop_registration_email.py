#!/usr/bin/env python3
"""
fetch_cwop_registration_email.py -- pull the weekly CWOP registration dump
from Gmail and (by default) load it into the DB
===============================================================================
The weekly CWOP/APRSWXNET "Station Table" dump (see ingest_cwop_registration.py)
arrives by email to Gerry's personal Gmail account (address lives in the
credentials file below, not hardcoded here), not as a file drop -- sent from
cwop-support@noaa.gov, subject "all3files", with three plain-text
attachments: atham4backup.txt, reg_all.txt, reg_not.txt. The attachments as
sent carry NO date in the filename (Gerry has been adding that by hand); this
script stamps one on itself, using the email's own Date header, before
saving -- so downstream filename-based date parsing (see
ingest_cwop_registration.py's guess_dump_date) keeps working unchanged. This
script checks that mailbox via IMAP, saves any new matching email's
attachments into ~/CWOP/, and by default runs ingest_cwop_registration.py
against the atham4backup file.

Credentials: ~/.config/cwop_fetch/credentials (mode 600), two lines:
    GMAIL_USER=...
    GMAIL_APP_PASSWORD=...
(a Gmail App Password, not the account password -- requires 2-Step
Verification enabled on the account).

State: ~/.config/cwop_fetch/state.json tracks the highest IMAP UID processed
so re-runs (e.g. the daily cron) don't reprocess the same email.

Run manually:
    venv/bin/python3 scripts/fetch_cwop_registration_email.py [--dry-run] [--no-ingest]

Intended to run via cron under the owning user's own crontab (not root/www-data),
since it writes into ~/CWOP/ and reads ~/.config/cwop_fetch/credentials.
"""

import argparse
import email
import email.utils
import imaplib
import json
import logging
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from email.message import Message
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%SZ',
)
log = logging.getLogger('fetch_cwop_registration_email')

IMAP_HOST = 'imap.gmail.com'
MAILBOX = 'INBOX'
FROM_ADDR = 'cwop-support@noaa.gov'
SUBJECT = 'all3files'

ATTACHMENT_PATTERNS = {
    'main': re.compile(r'^atham4backup(\d{8})?\.txt$', re.IGNORECASE),
    'reg_all': re.compile(r'^reg_all(\d{8})?\.txt$', re.IGNORECASE),
    'reg_not': re.compile(r'^reg_not(\d{8})?\.txt$', re.IGNORECASE),
}

CONFIG_DIR = Path.home() / '.config' / 'cwop_fetch'
CREDENTIALS_FILE = CONFIG_DIR / 'credentials'
STATE_FILE = CONFIG_DIR / 'state.json'
CWOP_DIR = Path.home() / 'CWOP'

SCRIPT_DIR = Path(__file__).resolve().parent
APP_DIR = SCRIPT_DIR.parent
VENV_PYTHON = APP_DIR / 'venv' / 'bin' / 'python3'
INGEST_SCRIPT = SCRIPT_DIR / 'ingest_cwop_registration.py'


def load_credentials():
    if not CREDENTIALS_FILE.exists():
        log.error('credentials file not found: %s', CREDENTIALS_FILE)
        sys.exit(1)
    creds = {}
    for line in CREDENTIALS_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        creds[k.strip()] = v.strip()
    missing = [k for k in ('GMAIL_USER', 'GMAIL_APP_PASSWORD') if k not in creds]
    if missing:
        log.error('credentials file missing keys: %s', missing)
        sys.exit(1)
    return creds['GMAIL_USER'], creds['GMAIL_APP_PASSWORD']


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            log.warning('state file unreadable, starting from last_uid=0')
    return {'last_uid': 0}


def save_state(state):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))


def get_email_date(msg: Message) -> date:
    raw = msg.get('Date')
    try:
        return email.utils.parsedate_to_datetime(raw).date()
    except (TypeError, ValueError):
        log.warning('could not parse Date header %r, using today', raw)
        return datetime.now(timezone.utc).date()


def stamp_filename(filename: str, dump_date: date) -> str:
    """Attachments arrive with no date in the name; add one (matching
    Gerry's existing manual convention, e.g. atham4backup20261123.txt) so
    downstream filename-based date parsing keeps working unchanged."""
    if re.search(r'\d{8}', filename):
        return filename
    stem, dot, ext = filename.rpartition('.')
    stamp = dump_date.strftime('%Y%m%d')
    return f'{stem}{stamp}.{ext}' if dot else f'{filename}{stamp}'


def extract_attachments(msg: Message):
    """Return {kind: (filename, bytes)} for attachments matching our patterns."""
    found = {}
    for part in msg.walk():
        filename = part.get_filename()
        if not filename:
            continue
        for kind, pattern in ATTACHMENT_PATTERNS.items():
            if pattern.match(filename):
                found[kind] = (filename, part.get_payload(decode=True))
    return found


def run_ingest(main_file: Path) -> bool:
    python = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable
    cmd = [python, str(INGEST_SCRIPT), '--file', str(main_file)]
    log.info('running: %s', ' '.join(cmd))
    result = subprocess.run(cmd, cwd=str(SCRIPT_DIR), capture_output=True, text=True)
    for line in result.stdout.splitlines():
        log.info('[ingest] %s', line)
    for line in result.stderr.splitlines():
        log.warning('[ingest] %s', line)
    if result.returncode != 0:
        log.error('ingest_cwop_registration.py failed (exit %d)', result.returncode)
        return False
    return True


def main():
    ap = argparse.ArgumentParser(description='Fetch weekly CWOP registration email and load it')
    ap.add_argument('--dry-run', action='store_true',
                     help='search and report only, no files saved, no DB writes, state not advanced')
    ap.add_argument('--no-ingest', action='store_true',
                     help='save attachments but skip running ingest_cwop_registration.py')
    ap.add_argument('--since-days', type=int, default=30,
                     help='on first run (no state file yet), only look back this many days')
    args = ap.parse_args()

    user, app_password = load_credentials()
    state = load_state()
    last_uid = state.get('last_uid', 0)

    CWOP_DIR.mkdir(parents=True, exist_ok=True)

    imap = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        imap.login(user, app_password)
        imap.select(MAILBOX)

        if last_uid:
            criteria = f'(UID {last_uid + 1}:*) (FROM "{FROM_ADDR}") (SUBJECT "{SUBJECT}")'
        else:
            since_date = datetime.now(timezone.utc).date() - timedelta(days=args.since_days)
            criteria = f'(FROM "{FROM_ADDR}") (SUBJECT "{SUBJECT}") (SINCE {since_date.strftime("%d-%b-%Y")})'

        status, data = imap.uid('search', None, criteria)
        if status != 'OK':
            log.error('IMAP search failed: %s', data)
            sys.exit(1)

        uids = [int(u) for u in data[0].split()] if data and data[0] else []
        # defend against servers clamping an out-of-range UID endpoint to the
        # highest existing UID, which could otherwise re-match an old email
        uids = sorted(u for u in uids if u > last_uid)

        if not uids:
            log.info('no new matching emails (last_uid=%d)', last_uid)
            return

        log.info('found %d new matching email(s): uids=%s', len(uids), uids)

        highest_ok = last_uid
        for uid in uids:
            status, msg_data = imap.uid('fetch', str(uid), '(RFC822)')
            if status != 'OK' or not msg_data or not msg_data[0]:
                log.error('failed to fetch uid %d: %s', uid, msg_data)
                continue
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            log.info('uid %d: subject=%r date=%r', uid, msg.get('Subject'), msg.get('Date'))

            attachments = extract_attachments(msg)
            if 'main' not in attachments:
                log.warning('uid %d: no atham4backup*.txt attachment found, skipping', uid)
                continue
            dump_date = get_email_date(msg)
            log.info('uid %d: found attachments: %s (dump_date=%s)', uid,
                      {k: v[0] for k, v in attachments.items()}, dump_date)

            if args.dry_run:
                log.info('[dry-run] would save %d attachment(s) and %s ingest', len(attachments),
                          'skip' if args.no_ingest else 'run')
                continue

            saved_paths = {}
            for kind, (filename, content) in attachments.items():
                dest = CWOP_DIR / stamp_filename(filename, dump_date)
                dest.write_bytes(content)
                saved_paths[kind] = dest
                log.info('uid %d: saved %s (%d bytes)', uid, dest, len(content))

            ok = True
            if not args.no_ingest:
                ok = run_ingest(saved_paths['main'])

            if ok:
                highest_ok = uid
            else:
                log.error('uid %d: ingest failed, will retry this email next run', uid)
                break  # don't advance past a failed one so it's retried next run

        if not args.dry_run and highest_ok > last_uid:
            save_state({'last_uid': highest_ok})
            log.info('state updated: last_uid=%d', highest_ok)
    finally:
        try:
            imap.logout()
        except Exception:
            pass


if __name__ == '__main__':
    main()

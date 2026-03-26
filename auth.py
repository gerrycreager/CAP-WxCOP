"""
CAP WxCOP Authentication Module
Provides login/logout, TOTP MFA, and session management for protected routes.

Protected routes require:
  1. Username + bcrypt password
  2. TOTP 6-digit code (authenticator app) OR email/SMS OTP fallback

Credentials file: /etc/cap_wxcop/users.conf
  Format: username:bcrypt_hash:totp_secret[:email]
  Example: gcreager:$2b$12$...:BASE32SECRET:gerry@example.com

Session secret: /etc/cap_wxcop/secret.key

Admin tool: /usr/local/bin/cap_wxcop_user
"""

import os
import sys
import time
import secrets
import logging
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import (Blueprint, render_template, request, session,
                   redirect, url_for, flash, jsonify, current_app)

import bcrypt
import pyotp

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
USERS_CONF     = '/etc/cap_wxcop/users.conf'
SECRET_KEY_FILE = '/etc/cap_wxcop/secret.key'
SESSION_TIMEOUT = 8 * 3600  # 8 hours in seconds

# Email/SMS OTP settings
OTP_VALIDITY_SECONDS = 600   # 10 minutes
OTP_LENGTH           = 6

# In-memory OTP store: {username: (otp_code, expiry_timestamp)}
# Fine for a small user base; not shared across workers but acceptable
# since this is a single-process mod_wsgi app
_pending_otps = {}

# ---------------------------------------------------------------------------
# Blueprint
# ---------------------------------------------------------------------------
auth = Blueprint('auth', __name__, url_prefix='/auth')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_secret_key():
    """Load Flask session secret from file, generate if missing."""
    key_path = Path(SECRET_KEY_FILE)
    if key_path.exists():
        return key_path.read_text().strip()
    # Generate and save
    key_path.parent.mkdir(parents=True, exist_ok=True)
    new_key = secrets.token_hex(32)
    key_path.write_text(new_key)
    key_path.chmod(0o640)
    return new_key


def load_users():
    """
    Load users from /etc/cap_wxcop/users.conf.
    Returns dict: {username: {'hash': str, 'totp_secret': str, 'email': str|None}}
    Lines starting with # are comments. Blank lines ignored.
    Format: username:bcrypt_hash:totp_secret[:email_or_sms_gateway]
    """
    users = {}
    conf  = Path(USERS_CONF)
    if not conf.exists():
        return users
    for line in conf.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split(':')
        if len(parts) < 3:
            continue
        username     = parts[0]
        pw_hash      = parts[1]
        totp_secret  = parts[2]
        email        = parts[3] if len(parts) > 3 else None
        users[username] = {
            'hash':        pw_hash,
            'totp_secret': totp_secret,
            'email':       email,
        }
    return users


def verify_password(username, password):
    """Verify username/password against users.conf. Returns user dict or None."""
    users = load_users()
    user  = users.get(username)
    if not user:
        # Constant-time dummy check to prevent username enumeration
        bcrypt.checkpw(b'dummy', bcrypt.hashpw(b'dummy', bcrypt.gensalt(rounds=4)))
        return None
    try:
        if bcrypt.checkpw(password.encode(), user['hash'].encode()):
            return user
    except Exception:
        pass
    return None


def verify_totp(username, code):
    """Verify a TOTP code for the given username. Returns True/False."""
    users = load_users()
    user  = users.get(username)
    if not user or not user.get('totp_secret'):
        return False
    totp = pyotp.TOTP(user['totp_secret'])
    # valid_window=1 allows 30 seconds clock drift
    return totp.verify(code, valid_window=1)


def generate_email_otp(username):
    """
    Generate a time-limited OTP and store it in memory.
    Returns the OTP code string.
    """
    code   = ''.join([str(secrets.randbelow(10)) for _ in range(OTP_LENGTH)])
    expiry = time.time() + OTP_VALIDITY_SECONDS
    _pending_otps[username] = (code, expiry)
    return code


def verify_email_otp(username, code):
    """Verify email/SMS OTP. Returns True if valid, False otherwise."""
    entry = _pending_otps.get(username)
    if not entry:
        return False
    stored_code, expiry = entry
    if time.time() > expiry:
        _pending_otps.pop(username, None)
        return False
    if secrets.compare_digest(code.strip(), stored_code):
        _pending_otps.pop(username, None)
        return True
    return False


def send_otp_email(username, code):
    """
    Send OTP via email (or SMS via carrier gateway).
    Uses Python smtplib with localhost relay or configured SMTP.
    """
    import smtplib
    from email.mime.text import MIMEText

    users     = load_users()
    user      = users.get(username)
    recipient = user.get('email') if user else None

    if not recipient:
        log.warning(f"No email configured for user {username}")
        return False

    msg      = MIMEText(
        f"Your CAP WxCOP verification code is: {code}\n\n"
        f"This code expires in {OTP_VALIDITY_SECONDS // 60} minutes.\n"
        f"If you did not request this, someone may be attempting to access your account."
    )
    msg['Subject'] = f"CAP WxCOP Login Code: {code}"
    msg['From']    = 'noreply@n5jxs.ampr.org'
    msg['To']      = recipient

    try:
        with smtplib.SMTP('localhost', 25, timeout=10) as smtp:
            smtp.sendmail(msg['From'], [recipient], msg.as_string())
        log.info(f"OTP sent to {recipient} for user {username}")
        return True
    except Exception as e:
        log.error(f"Failed to send OTP email to {recipient}: {e}")
        return False


def is_authenticated():
    """Check if current session is authenticated and not expired."""
    if not session.get('authenticated'):
        return False
    login_time = session.get('login_time', 0)
    if time.time() - login_time > SESSION_TIMEOUT:
        session.clear()
        return False
    return True


def login_required(f):
    """
    Decorator for routes that require authentication.
    Redirects to login page with next= parameter on failure.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_authenticated():
            return redirect(url_for('auth.login', next=request.url))
        return f(*args, **kwargs)
    return decorated


def login_required_json(f):
    """
    Decorator for JSON API routes that require authentication.
    Returns 401 JSON response instead of redirect.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_authenticated():
            return jsonify({'error': 'Authentication required', 'code': 401}), 401
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@auth.route('/login', methods=['GET', 'POST'])
def login():
    """Step 1: Username + password."""
    if is_authenticated():
        return redirect(url_for('kq_admin.list_stations'))

    next_url = request.args.get('next') or request.form.get('next', '')

    if request.method == 'POST':
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')

        user = verify_password(username, password)
        if not user:
            # Small delay to slow brute force
            time.sleep(0.5)
            flash('Invalid username or password.', 'error')
            return render_template('auth/login.html', next=next_url)

        # Password OK — move to MFA step
        session['pending_user'] = username
        session['pending_time'] = time.time()

        return redirect(url_for('auth.mfa', next=next_url))

    return render_template('auth/login.html', next=next_url)


@auth.route('/mfa', methods=['GET', 'POST'])
def mfa():
    """Step 2: TOTP or email OTP."""
    username = session.get('pending_user')
    if not username:
        return redirect(url_for('auth.login'))

    # Pending session timeout (5 minutes to complete MFA)
    if time.time() - session.get('pending_time', 0) > 300:
        session.pop('pending_user', None)
        session.pop('pending_time', None)
        flash('Login session expired. Please start again.', 'error')
        return redirect(url_for('auth.login'))

    next_url = request.args.get('next') or request.form.get('next', '')

    if request.method == 'POST':
        action = request.form.get('action', 'totp')
        code   = request.form.get('code', '').strip().replace(' ', '')

        if action == 'send_email':
            # Generate and send email/SMS OTP
            otp = generate_email_otp(username)
            if send_otp_email(username, otp):
                flash('A verification code has been sent to your registered email/phone.', 'info')
            else:
                flash('Failed to send code. Please use your authenticator app.', 'error')
            return render_template('auth/mfa.html', username=username,
                                   next=next_url, show_email_sent=True)

        # Verify code — try TOTP first, then email OTP
        if verify_totp(username, code) or verify_email_otp(username, code):
            # Full authentication complete
            session.pop('pending_user', None)
            session.pop('pending_time', None)
            session['authenticated'] = True
            session['username']      = username
            session['login_time']    = time.time()
            session.permanent        = True

            log.info(f"Successful login: {username} from {request.remote_addr}")

            # Redirect to original destination or KQ admin
            if next_url and next_url.startswith('/'):
                return redirect(next_url)
            return redirect(url_for('kq_admin.list_stations'))

        else:
            time.sleep(0.5)
            flash('Invalid verification code. Please try again.', 'error')

    return render_template('auth/mfa.html', username=username,
                           next=next_url, show_email_sent=False)


@auth.route('/logout')
def logout():
    """Clear session and redirect to KQ station list (public view)."""
    username = session.get('username', 'unknown')
    session.clear()
    log.info(f"Logout: {username}")
    flash('You have been logged out.', 'info')
    return redirect(url_for('kq_admin.list_stations'))


@auth.route('/status')
def status():
    """Return current auth status as JSON (for UI use)."""
    if is_authenticated():
        return jsonify({
            'authenticated': True,
            'username': session.get('username'),
            'expires_in': int(SESSION_TIMEOUT - (time.time() - session.get('login_time', 0)))
        })
    return jsonify({'authenticated': False})


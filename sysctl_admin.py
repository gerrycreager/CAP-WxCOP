"""
sysctl_admin.py — CAP WxCOP System Management Blueprint

Hidden management page for remote reboot, LDM control, and Apache restart
across r815 (local), data1, and data2.

URL: /CAP_WxCOP/sysctl  (not linked from any other page)
Auth: login_required (same session auth as other admin pages)

Execution:
  r815 — subprocess with sudo
  data1/data2 — ssh root@host sudo <command>

All actions are logged to /var/log/cap_wxcop/sysctl_admin.log
"""

import subprocess
import logging
import os
from datetime import datetime, timezone
from flask import Blueprint, render_template_string, jsonify, request, session
from auth import login_required

sysctl_admin = Blueprint('sysctl_admin', __name__)

LOG_FILE = '/var/log/cap_wxcop/sysctl_admin.log'
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

log = logging.getLogger('sysctl_admin')
if not log.handlers:
    fh = logging.FileHandler(LOG_FILE)
    fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    log.addHandler(fh)
    log.setLevel(logging.INFO)

# ── Command definitions ───────────────────────────────────────────────────────
# Each command is a list of args passed to subprocess/ssh
# r815 commands run via: sudo <cmd>
# data1/data2 commands run via: ssh root@<host> sudo <cmd>

SERVERS = {
    'r815':  {'label': 'r815 (Web/LDM relay)', 'host': None},       # local
    'data1': {'label': 'data1 (LDM primary/MRMS)', 'host': 'data1'},
    'data2': {'label': 'data2 (PostgreSQL/OCONUS)', 'host': 'data2'},
}

COMMANDS = {
    'ldm_restart': {
        'label':   'Restart LDM',
        'cmd':     ['systemctl', 'restart', 'ldm'],
        'servers': ['r815', 'data1', 'data2'],
        'danger':  False,
        'confirm': 'Restart LDM on {server}?',
    },
    'ldm_stop': {
        'label':   'Stop LDM',
        'cmd':     ['systemctl', 'stop', 'ldm'],
        'servers': ['r815', 'data1', 'data2'],
        'danger':  True,
        'confirm': 'Stop LDM on {server}? Data ingest will halt.',
    },
    'ldm_start': {
        'label':   'Start LDM',
        'cmd':     ['systemctl', 'start', 'ldm'],
        'servers': ['r815', 'data1', 'data2'],
        'danger':  False,
        'confirm': 'Start LDM on {server}?',
    },
    'apache_restart': {
        'label':   'Restart Apache',
        'cmd':     ['systemctl', 'restart', 'apache2'],
        'servers': ['r815'],
        'danger':  False,
        'confirm': 'Restart Apache on r815? Active sessions will drop briefly.',
    },
    'reboot': {
        'label':   'Reboot Server',
        'cmd':     ['reboot'],
        'servers': ['r815', 'data1', 'data2'],
        'danger':  True,
        'confirm': '⚠ REBOOT {server}? All services will be unavailable for ~2 minutes.',
    },
}


def run_command(server_key, cmd_key):
    """Execute a system command on the specified server."""
    srv = SERVERS.get(server_key)
    cmd_def = COMMANDS.get(cmd_key)
    if not srv or not cmd_def:
        return False, 'Unknown server or command'

    if server_key not in cmd_def['servers']:
        return False, f'Command {cmd_key} not available on {server_key}'

    user = session.get('username', 'unknown')
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')

    if srv['host'] is None:
        # Local — run via sudo
        full_cmd = ['sudo'] + cmd_def['cmd']
    else:
        # Remote — ssh then sudo
        full_cmd = ['ssh', '-o', 'ConnectTimeout=10',
                    '-o', 'StrictHostKeyChecking=no',
                    f"root@{srv['host']}",
                    'sudo'] + cmd_def['cmd']

    log.info(f'[{ts}] user={user} server={server_key} cmd={cmd_key} '
             f'full_cmd={" ".join(full_cmd)}')

    try:
        result = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        success = result.returncode == 0
        output  = (result.stdout + result.stderr).strip() or '(no output)'
        log.info(f'  → rc={result.returncode} output={output[:200]}')
        return success, output
    except subprocess.TimeoutExpired:
        log.error(f'  → TIMEOUT after 30s')
        return False, 'Command timed out after 30 seconds'
    except Exception as e:
        log.error(f'  → EXCEPTION: {e}')
        return False, str(e)


# ── Routes ────────────────────────────────────────────────────────────────────

@sysctl_admin.route('/sysctl')
@login_required
def index():
    return render_template_string(SYSCTL_TEMPLATE,
                                  servers=SERVERS,
                                  commands=COMMANDS)


@sysctl_admin.route('/sysctl/exec', methods=['POST'])
@login_required
def execute():
    data       = request.get_json()
    server_key = data.get('server', '')
    cmd_key    = data.get('command', '')
    confirmed  = data.get('confirmed', False)

    if not confirmed:
        return jsonify({'error': 'Confirmation required'}), 400

    success, output = run_command(server_key, cmd_key)

    return jsonify({
        'success': success,
        'server':  server_key,
        'command': cmd_key,
        'output':  output,
    })


# ── Template ──────────────────────────────────────────────────────────────────

SYSCTL_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>System Management — CAP WxCOP</title>
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', monospace;
            background: #0d1117; color: #c9d1d9; min-height: 100vh; padding: 2rem;
        }
        header {
            display: flex; justify-content: space-between; align-items: center;
            margin-bottom: 2rem; padding-bottom: 1rem;
            border-bottom: 1px solid #30363d;
        }
        h1 { font-size: 1.4rem; color: #f0883e; letter-spacing: 0.05em; }
        h1 span { color: #888; font-size: 0.9rem; font-weight: 400; margin-left: 1rem; }
        .home-btn {
            background: #21262d; color: #c9d1d9; border: 1px solid #30363d;
            padding: 6px 14px; border-radius: 6px; text-decoration: none;
            font-size: 0.875rem; transition: background 0.2s;
        }
        .home-btn:hover { background: #30363d; }

        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
            gap: 1.5rem;
        }
        .server-card {
            background: #161b22; border: 1px solid #30363d;
            border-radius: 10px; padding: 1.5rem;
        }
        .server-title {
            font-size: 1rem; font-weight: 600; color: #79c0ff;
            margin-bottom: 1rem; padding-bottom: 0.5rem;
            border-bottom: 1px solid #30363d;
            display: flex; align-items: center; gap: 0.5rem;
        }
        .status-dot {
            width: 8px; height: 8px; border-radius: 50%;
            background: #3fb950; display: inline-block;
        }
        .cmd-row {
            display: flex; justify-content: space-between; align-items: center;
            padding: 0.5rem 0; border-bottom: 1px solid #21262d;
        }
        .cmd-row:last-child { border-bottom: none; }
        .cmd-label { font-size: 0.875rem; color: #c9d1d9; }
        .cmd-btn {
            padding: 4px 12px; border-radius: 5px; border: none;
            cursor: pointer; font-size: 0.8rem; font-weight: 600;
            transition: all 0.15s; min-width: 80px;
        }
        .cmd-btn.normal {
            background: #238636; color: white;
        }
        .cmd-btn.normal:hover { background: #2ea043; }
        .cmd-btn.danger {
            background: #da3633; color: white;
        }
        .cmd-btn.danger:hover { background: #f85149; }
        .cmd-btn:disabled { background: #30363d; color: #666; cursor: not-allowed; }

        /* All servers row */
        .all-row {
            background: #161b22; border: 1px solid #30363d;
            border-radius: 10px; padding: 1rem 1.5rem;
            margin-bottom: 1.5rem;
            display: flex; gap: 1rem; align-items: center; flex-wrap: wrap;
        }
        .all-row label { color: #888; font-size: 0.875rem; margin-right: 0.5rem; }

        /* Output console */
        #console {
            background: #0d1117; border: 1px solid #30363d;
            border-radius: 8px; padding: 1rem;
            font-family: 'Courier New', monospace; font-size: 0.8rem;
            color: #7ee787; min-height: 80px; max-height: 200px;
            overflow-y: auto; margin-top: 1.5rem;
            white-space: pre-wrap;
        }
        .console-err { color: #f85149; }
        .console-info { color: #79c0ff; }

        /* Modal */
        .modal-overlay {
            display: none; position: fixed; inset: 0;
            background: rgba(0,0,0,0.7); z-index: 1000;
            align-items: center; justify-content: center;
        }
        .modal-overlay.active { display: flex; }
        .modal {
            background: #161b22; border: 1px solid #f85149;
            border-radius: 10px; padding: 2rem; max-width: 440px; width: 90%;
        }
        .modal h2 { color: #f85149; margin-bottom: 1rem; font-size: 1.1rem; }
        .modal p { color: #c9d1d9; margin-bottom: 1.5rem; font-size: 0.95rem; line-height: 1.5; }
        .modal-btns { display: flex; gap: 1rem; justify-content: flex-end; }
        .modal-cancel {
            background: #21262d; color: #c9d1d9; border: 1px solid #30363d;
            padding: 8px 18px; border-radius: 6px; cursor: pointer; font-size: 0.9rem;
        }
        .modal-confirm {
            background: #da3633; color: white; border: none;
            padding: 8px 18px; border-radius: 6px; cursor: pointer;
            font-size: 0.9rem; font-weight: 600;
        }
        .modal-confirm:hover { background: #f85149; }
    </style>
</head>
<body>
    <header>
        <h1>⚙ System Management <span>CAP WxCOP Infrastructure</span></h1>
        <a href="/CAP_WxCOP/" class="home-btn">🏠 Home</a>
    </header>

    <!-- All-servers quick actions -->
    <div class="all-row">
        <label>All servers:</label>
        <button class="cmd-btn normal" onclick="execAll('ldm_restart')">Restart LDM (all)</button>
        <button class="cmd-btn danger" onclick="execAll('reboot')">Reboot All ⚠</button>
    </div>

    <!-- Per-server cards -->
    <div class="grid">
        {% for srv_key, srv in servers.items() %}
        <div class="server-card">
            <div class="server-title">
                <span class="status-dot"></span>
                {{ srv.label }}
            </div>
            {% for cmd_key, cmd in commands.items() %}
            {% if srv_key in cmd.servers %}
            <div class="cmd-row">
                <span class="cmd-label">{{ cmd.label }}</span>
                <button class="cmd-btn {{ 'danger' if cmd.danger else 'normal' }}"
                        onclick="execCmd('{{ srv_key }}', '{{ cmd_key }}',
                                 '{{ cmd.confirm.replace('{server}', srv.label) }}',
                                 {{ 'true' if cmd.danger else 'false' }})">
                    {{ cmd.label }}
                </button>
            </div>
            {% endif %}
            {% endfor %}
        </div>
        {% endfor %}
    </div>

    <!-- Output console -->
    <div id="console">» Ready. Select a command above.</div>

    <!-- Confirmation modal -->
    <div class="modal-overlay" id="modal">
        <div class="modal">
            <h2 id="modal-title">Confirm Action</h2>
            <p id="modal-msg"></p>
            <div class="modal-btns">
                <button class="modal-cancel" onclick="closeModal()">Cancel</button>
                <button class="modal-confirm" id="modal-ok" onclick="doConfirm()">Execute</button>
            </div>
        </div>
    </div>

    <script>
    var pending = null;

    function log(msg, cls) {
        var el = document.getElementById('console');
        var ts = new Date().toISOString().replace('T',' ').slice(0,19) + 'Z';
        var line = document.createElement('div');
        if (cls) line.className = cls;
        line.textContent = '[' + ts + '] ' + msg;
        el.appendChild(line);
        el.scrollTop = el.scrollHeight;
    }

    function execCmd(server, command, confirmMsg, isDanger) {
        pending = {server, command};
        document.getElementById('modal-title').textContent =
            isDanger ? '⚠ Dangerous Action — Confirm' : 'Confirm Action';
        document.getElementById('modal-msg').textContent = confirmMsg;
        document.getElementById('modal-ok').className =
            'modal-confirm' + (isDanger ? '' : '');
        document.getElementById('modal').classList.add('active');
    }

    function execAll(command) {
        var cmd = {{ commands | tojson }};
        var servers = cmd[command] ? cmd[command].servers : [];
        var label = cmd[command] ? cmd[command].label : command;
        var msg = 'Execute "' + label + '" on ALL servers: ' +
                  servers.join(', ') + '?\n\nThis will run sequentially.';
        pending = {server: '__all__', command, servers};
        document.getElementById('modal-title').textContent = '⚠ All Servers — Confirm';
        document.getElementById('modal-msg').textContent = msg;
        document.getElementById('modal').classList.add('active');
    }

    function closeModal() {
        document.getElementById('modal').classList.remove('active');
        pending = null;
    }

    async function doConfirm() {
        closeModal();
        if (!pending) return;

        if (pending.server === '__all__') {
            for (var srv of pending.servers) {
                await sendCmd(srv, pending.command);
                // Small delay between servers for reboot safety
                if (pending.command === 'reboot') {
                    await new Promise(r => setTimeout(r, 3000));
                }
            }
        } else {
            await sendCmd(pending.server, pending.command);
        }
        pending = null;
    }

    async function sendCmd(server, command) {
        log('Sending: ' + command + ' → ' + server + '...', 'console-info');
        // Disable all buttons during execution
        document.querySelectorAll('.cmd-btn').forEach(b => b.disabled = true);

        try {
            var resp = await fetch('/CAP_WxCOP/sysctl/exec', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({server, command, confirmed: true})
            });
            var data = await resp.json();
            if (data.success) {
                log('✓ ' + server + ' — ' + command + ': ' + data.output);
            } else {
                log('✗ ' + server + ' — ' + command + ': ' + data.output, 'console-err');
            }
        } catch (e) {
            log('✗ Network error: ' + e.message, 'console-err');
        } finally {
            document.querySelectorAll('.cmd-btn').forEach(b => b.disabled = false);
        }
    }

    // Close modal on overlay click
    document.getElementById('modal').addEventListener('click', function(e) {
        if (e.target === this) closeModal();
    });
    </script>
</body>
</html>"""


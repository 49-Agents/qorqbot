"""Small, bounded recovery for the dedicated Outlook browser; no email ledger writes.

Read-only probes never wait for the sender. The existing engine flock stays
held only during restart and verification.
Only two failed probes within two minutes can trigger recovery. An unsuccessful
restart latches until a healthy probe, and successful recoveries have a cooldown.
"""
import argparse
import contextlib
import fcntl
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import socket
import time

ENGINE = 'bazarlyk-email-engine.service'
BROWSER = 'qorqut-outlook-browser.service'
STATE = Path('/var/lib/qorqut-outlook-watchdog/state.json')
HEALTH = STATE.with_name('health.json')
NOTIFY_CONFIG = Path('/etc/qorqut-email/outlook-watchdog-notify.json')
LOCK = Path('/var/lib/qorqut-email/fastmail.sqlite3.outlook.lock')
COOLDOWN = 1800
FAILURE_WINDOW = 120
LOCK_ALERT_AFTER = 120
RECOVERABLE = {'connection_failed', 'mailbox_not_ready'}
STATUSES = RECOVERABLE | {'healthy', 'login_required', 'configuration_error'}


def run(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, check=True, **kwargs)


def service_value(unit, field):
    return run(['/usr/bin/systemctl', 'show', unit, '-p', field, '--value'], timeout=5).stdout.strip()


def runtime():
    if service_value(ENGINE, 'ActiveState') != 'active' or service_value(BROWSER, 'ActiveState') != 'active':
        return None  # Respect intentionally stopped services and maintenance.
    pid = service_value(ENGINE, 'MainPID')
    if not pid.isdigit() or int(pid) < 1:
        raise ValueError('engine_not_running')
    proc = Path('/proc') / pid
    env = dict(x.split('=', 1) for x in (proc / 'environ').read_text().split('\0') if '=' in x)
    command = (proc / 'cmdline').read_bytes().split(b'\0')
    executable = command[0].decode()
    if (not executable.startswith('/opt/qorqut-email/runtime/')
            or command[1:4] != [b'-I', b'-m', b'engine_email']
            or env.get('EMAIL_DATABASE') != str(LOCK).removesuffix('.outlook.lock')):
        raise ValueError('engine_identity_changed')
    # Resolve the LIVE engine package, so a later release cannot leave a stale pin.
    root = run([executable, '-I', '-c',
        'import importlib.util,pathlib;print(pathlib.Path(importlib.util.find_spec("engine_email").origin).parent)'],
        timeout=5).stdout.strip()
    node = shutil.which('node', path=env.get('PATH', ''))
    if not root.startswith('/opt/qorqut-email/runtime/') or not node:
        raise ValueError('runtime_unavailable')
    config = json.loads(Path(env['EMAIL_OUTLOOK_WEB_FILE']).read_text())
    return {'node': node, 'bridge_root': root, 'cdp_url': config['cdp_url'],
            'address': config['address'], 'account_ref': config['account_ref']}


@contextlib.contextmanager
def browser_lock(path=LOCK, wait=8):
    # Never create/replace the engine's lock: a second inode would not fence it.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    acquired = False
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        if acquired and os.fstat(fd).st_ino != path.stat().st_ino:
            raise ValueError('lock_replaced')
        yield acquired
    finally:
        os.close(fd)


def probe(config):
    try:
        # The fixed read-only client inherits the watchdog's hardened sandbox;
        # it needs neither a PAM/keyring session nor a user-ID transition.
        reply = run([config['node'], str(Path(__file__).with_name('probe.mjs'))],
                    input=json.dumps(config), timeout=17)
        status = json.loads(reply.stdout)['status']
        return status if status in STATUSES else 'configuration_error'
    except subprocess.TimeoutExpired:
        return 'connection_failed'
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError):
        return 'configuration_error'


def load_state(path):
    if not path.exists():
        return {}
    state = json.loads(path.read_text())
    if not isinstance(state, dict):
        raise ValueError('invalid_state')
    for field in ('failures', 'last_failure_at', 'last_restart_at'):
        if field in state and (type(state[field]) not in (int, float)
                               or not math.isfinite(state[field]) or state[field] < 0):
            raise ValueError('invalid_state')
    return state


def save_state(state, path=STATE, mode=0o600):
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    temporary = path.with_suffix('.new')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    os.fchmod(fd, mode)
    with os.fdopen(fd, 'w') as file:
        json.dump(state, file, sort_keys=True)
        file.write('\n')
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


def decide(previous, status, now):
    state = {**previous, 'checked_at': now, 'probe': status}
    if status == 'healthy':
        state.update(failures=0, restart_attempted=False, status='healthy')
        state.pop('lock_wait_since', None)
        return state, False
    if status not in RECOVERABLE:
        state.update(failures=0, status=status, restart_attempted=True)
        return state, False
    recent = 0 <= now - previous.get('last_failure_at', 0) <= FAILURE_WINDOW
    failures = min(2, previous.get('failures', 0) + 1) if recent else 1
    state.update(failures=failures, last_failure_at=now, status='first_failure')
    if previous.get('restart_attempted'):
        state['status'] = 'needs_attention'
        return state, False
    if failures < 2:
        return state, False
    if 'last_restart_at' in previous and now - previous['last_restart_at'] < COOLDOWN:
        state['status'] = 'cooldown'
        return state, False
    # Do not consume the restart allowance until we actually hold the lock.
    state.update(status='recovery_wait')
    return state, True


def restart_and_verify(config):
    try:
        run(['/usr/bin/systemctl', 'restart', BROWSER], timeout=65)
    except (OSError, subprocess.SubprocessError):
        return 'restart_failed'
    deadline = time.monotonic() + 30
    while True:
        time.sleep(2)
        status = probe(config)
        if status in {'healthy', 'login_required', 'configuration_error'} or time.monotonic() >= deadline:
            return status


def tick(config, previous, save=save_state, check=probe, restart=restart_and_verify,
         now=None, lock=browser_lock, clock=time.time):
    status = check(config)  # Independent of the sender lock, including --check-only.
    now = clock() if now is None else now
    state, should_restart = decide(previous, status, now)
    save(state)
    if should_restart:
        with lock() as acquired:
            if not acquired:
                state.setdefault('lock_wait_since', now)
                if now - state['lock_wait_since'] >= LOCK_ALERT_AFTER:
                    state['status'] = 'needs_attention'
                save(state)
                return state
            # The sender may have recovered while we waited. Recheck under lock.
            status = check(config)
            if status not in RECOVERABLE:
                state, _ = decide(state, status, clock())
                save(state)
                return state
            state.update(status='restarting', restart_attempted=True, last_restart_at=clock())
            state.pop('lock_wait_since', None)
            save(state)  # Crash-safe: persist cooldown/latch BEFORE restarting.
            status = restart(config)
            state.update(probe=status, checked_at=clock(), failures=0)
            if status == 'healthy':
                state.update(status='healthy', restart_attempted=False, recovered_at=clock())
            else:
                state.update(status='login_required' if status == 'login_required' else 'needs_attention',
                             restart_attempted=True)
            save(state)
    return state


def publish_health(state, account_ref, path=HEALTH):
    probe_status = state.get('probe')
    status = ('healthy' if probe_status == 'healthy' else
              'login_required' if probe_status == 'login_required' else
              'unknown' if probe_status == 'maintenance' else 'unavailable')
    # Deliberately public to the engine: no credentials, mailbox address or mail.
    save_state({'account_ref': account_ref, 'status': status,
                'checked_at': state['checked_at'], 'reason': probe_status,
                'recovery': state['status'],
                'notification': state.get('notification_status', 'none')}, path, mode=0o644)


def notify_owner(event, path=NOTIFY_CONFIG, *, action='notify'):
    """Use the existing local urgent-notification gateway, with stable dedupe keys."""
    config = json.loads(path.read_text())
    secret_path = Path(config['secret_file'])
    if secret_path.stat().st_mode & 0o077:
        raise ValueError('notification_secret_permissions')
    secret = secret_path.read_text().strip()
    if not secret or len(secret) > 512:
        raise ValueError('notification_secret_invalid')
    request = json.dumps({'version': 1, 'action': action, 'secret': secret, **event}).encode() + b'\n'
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(config['socket_path'])
        connection.sendall(request)
        response = b''
        while b'\n' not in response and len(response) <= 65536:
            part = connection.recv(4096)
            if not part:
                break
            response += part
    if len(response) > 65536:
        raise ValueError('notification_rejected')
    reply = json.loads(response.split(b'\n', 1)[0])
    # A new producer has no incident yet; not_found still proves authenticated
    # access to its status endpoint, without creating a synthetic owner alert.
    if action == 'status' and reply.get('error', {}).get('code') == 'not_found':
        return
    if reply.get('ok') is not True:
        raise ValueError('notification_rejected')


def report_attention(state, send=notify_owner, save=lambda state: None):
    previous = state.get('alert_state')
    pending = state.get('alert_pending')
    target = ('resolved' if state['status'] == 'healthy' and (previous == 'firing' or pending is not None) else
              'firing' if state['status'] in {'needs_attention', 'login_required',
                                            'configuration_error', 'cooldown'} else None)
    if target is None or (target == previous and pending is None):
        return state
    event = {'event_key': 'outlook-browser-health', 'state': target,
             'title': 'Outlook sender unavailable' if target == 'firing' else 'Outlook sender recovered',
             'text': ('Outlook is not responding or requires attention. Approved emails may remain queued. '
                      'Inspect the Outlook desktop and watchdog health.' if target == 'firing' else
                      'The Outlook mailbox responds again. Check send receipts for delivery progress.'),
             'reason': 'Email sending depends on this mailbox; queued does not mean sent.'}
    # Persist intent before the gateway call: a lost acknowledgement must not
    # leave an alert firing forever after the browser has already recovered.
    state['alert_pending'] = target
    save(state)
    try:
        send(event)
    except Exception:
        state['notification_status'] = 'failed'  # Retry next tick, with the same gateway dedupe key.
    else:
        state.update(alert_state=target, notification_status='queued')
        state.pop('alert_pending', None)
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only', action='store_true', help='probe only; never restart or save state')
    parser.add_argument('--check-notifications', action='store_true',
                        help='read gateway event status; never send, restart or save state')
    args = parser.parse_args()
    previous, config = {}, None
    try:
        if args.check_notifications:
            notify_owner({'event_key': 'outlook-browser-health'}, action='status')
            print(json.dumps({'notification_gateway': 'reachable'}))
            return
        config = runtime()
        if config is None:
            if not args.check_only:
                publish_health({'probe': 'maintenance', 'status': 'maintenance_skip',
                                'checked_at': time.time()}, None)
            print(json.dumps({'status': 'maintenance_skip'}))
            return
        previous = {} if args.check_only else load_state(STATE)
        if args.check_only:
            print(json.dumps({'status': probe(config)}))
            return
        def persist(state):
            save_state(state)
            publish_health(state, config['account_ref'])
        state = tick(config, previous, save=persist)
        report_attention(state, save=persist)
        persist(state)
        if (state.get('status'), state.get('last_restart_at')) != (previous.get('status'), previous.get('last_restart_at')):
            print(json.dumps(state, sort_keys=True))
        if state['status'] in {'needs_attention', 'login_required', 'configuration_error', 'cooldown'} or state.get('notification_status') == 'failed':
            raise SystemExit(1)
    except Exception:
        # Never print exceptions: runtime/environment/provider details stay private.
        if not args.check_only and not args.check_notifications:
            try:
                state = {**previous, 'status': 'configuration_error', 'probe': 'configuration_error',
                         'checked_at': time.time(), 'restart_attempted': True}
                report_attention(state, save=save_state)
                save_state(state)
                publish_health(state, config.get('account_ref') if config else None)
            except Exception:
                pass  # The previous health snapshot expires; systemd records failure.
        print(json.dumps({'status': 'configuration_error', 'action': 'no_restart'}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()

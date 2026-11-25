"""Read the watchdog's credential-free snapshot; never launch a browser from an API read."""
import json
import math
from datetime import datetime, timezone
import time

MAX_AGE = 120
REASONS = {'healthy', 'connection_failed', 'mailbox_not_ready', 'login_required',
           'configuration_error', 'restart_failed', 'maintenance'}


def read_health(path, account_ref, *, now=None):
    result = {'status': 'unknown', 'checked_at': None, 'reason': 'health_unavailable'}
    try:
        with path.open('rb') as source:
            raw = source.read(4097)
        if len(raw) > 4096:
            return result
        data = json.loads(raw)
        checked = data['checked_at']
        if (data.get('account_ref') != account_ref or type(checked) not in (int, float)
                or not math.isfinite(checked) or data.get('status') not in
                {'healthy', 'unavailable', 'login_required', 'unknown'}
                or data.get('reason') not in REASONS):
            return result
        stamp = datetime.fromtimestamp(checked, timezone.utc).isoformat()
        age = (time.time() if now is None else now) - checked
        if not 0 <= age <= MAX_AGE:
            return {**result, 'checked_at': stamp, 'reason': 'health_stale'}
        return {'status': data['status'], 'checked_at': stamp, 'reason': data['reason']}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return result

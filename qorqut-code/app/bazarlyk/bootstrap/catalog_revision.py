"""Bounded, read-only observation of the local host's canonical catalogue."""
from contextlib import contextmanager
import json
import re
import subprocess
import sys
from pathlib import Path

from qorqut_operations.contracts import DomainError


def _read(root, timeout):
    from bazarlyk.agentic.installation import _validated_installed_binding
    from bazarlyk.instance_paths import InstancePaths
    from qorqut_agentic import AgenticLayer

    paths = InstancePaths(Path(root), {})
    with _validated_installed_binding(paths, timeout=max(0, timeout)) as record:
        layer = AgenticLayer.open(paths.agentic_root, paths.agentic_runtime, attribution_source='host_authenticated')
        if layer.instance_id != record['instance_id']:
            raise ValueError('Wrong agentic module instance')
        result = layer.catalog.revision()
    if result['recovery_required'] or not isinstance(result['catalog_revision'], str) or not result['catalog_revision']:
        raise ValueError('Catalogue revision is unavailable')
    return result['catalog_revision']


def observe(root, timeout):
    """Also bound store opening, SQLite and hashing, which have independent waits."""
    if timeout <= 0:
        raise DomainError('bootstrap_pending', 'The authored revision check is pending', retryable=True)
    code = ("import json,sys; sys.path[:0]=json.loads(sys.argv[1]); "
            "from bazarlyk.bootstrap.catalog_revision import _read; "
            "print(json.dumps(_read(sys.argv[2],float(sys.argv[3]))))")
    try:
        result = subprocess.run([sys.executable, '-I', '-c', code, json.dumps(sys.path), str(root), str(timeout)],
                                capture_output=True, text=True, timeout=timeout, check=True)
        revision = json.loads(result.stdout)
        if not isinstance(revision, str) or not revision:
            raise ValueError('Invalid catalogue revision')
        return revision
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        raise DomainError('bootstrap_pending', 'The authored revision check is unavailable or still pending', retryable=True) from exc


# Each hook owns one short-lived interpreter. Each observation reads and verifies
# current state anew. Only pure historical derivations with identical inputs can
# be reused between observations; they disappear when this hook closes.
_OBSERVER_CODE = r'''
import json, sys
sys.path[:0] = json.loads(sys.argv[1])
from bazarlyk.bootstrap.catalog_revision import _read
from bazarlyk.agentic.proof_memo import retained_derivations
from bazarlyk.agentic.layout_intent import _retained_reads
root = sys.argv[2]
with retained_derivations(), _retained_reads():
    while True:
        line = sys.stdin.buffer.readline(4097)
        if not line:
            break
        if len(line) > 4096 or not line.endswith(b'\n'):
            break
        try:
            request = json.loads(line)
            if set(request) != {'timeout'} or not isinstance(request['timeout'], (float, int)) or isinstance(request['timeout'], bool):
                raise ValueError('invalid request')
            revision = _read(root, max(0, request['timeout']))
            response = {'revision': revision}
        except Exception:
            response = {'error': 'unavailable'}
        sys.stdout.write(json.dumps(response, separators=(',', ':')) + '\n')
        sys.stdout.flush()
'''
_OBSERVER_MAX_LINE = 4096


class _Observer:
    def __init__(self, process, deadline):
        self.process = process
        self.deadline = deadline
        self.buffer = bytearray()

    def observe(self):
        import os
        import selectors
        import time
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise DomainError('bootstrap_pending', 'The authored revision check is pending', retryable=True)
        request = json.dumps({'timeout': remaining}, separators=(',', ':')).encode() + b'\n'
        pending = memoryview(request)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdout, selectors.EVENT_READ)
                selector.register(self.process.stdin, selectors.EVENT_WRITE)
                while True:
                    remaining = self.deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('revision observer deadline')
                    events = selector.select(remaining)
                    if not events:
                        raise TimeoutError('revision observer deadline')
                    for key, _ in events:
                        if key.fileobj is self.process.stdin:
                            count = os.write(key.fd, pending)
                            pending = pending[count:]
                            if not pending:
                                selector.unregister(self.process.stdin)
                        else:
                            data = os.read(key.fd, _OBSERVER_MAX_LINE + 1)
                            if not data:
                                raise ValueError('revision observer closed')
                            self.buffer.extend(data)
                            if len(self.buffer) > _OBSERVER_MAX_LINE:
                                raise ValueError('revision observer output is too large')
                            if b'\n' in self.buffer:
                                line, _, extra = self.buffer.partition(b'\n')
                                if extra:
                                    raise ValueError('unexpected revision observer output')
                                self.buffer.clear()
                                def unique(pairs):
                                    result = {}
                                    for key, value in pairs:
                                        if key in result:
                                            raise ValueError('duplicate revision observer key')
                                        result[key] = value
                                    return result
                                response = json.loads(line, object_pairs_hook=unique)
                                if (not isinstance(response, dict) or set(response) != {'revision'}
                                        or not isinstance(response['revision'], str)
                                        or not re.fullmatch(r'[0-9a-f]{64}', response['revision'])):
                                    raise ValueError('invalid revision observer response')
                                if time.monotonic() >= self.deadline:
                                    raise TimeoutError('revision observer deadline')
                                return response['revision']
        except (OSError, ValueError, TimeoutError, subprocess.SubprocessError) as exc:
            raise DomainError('bootstrap_pending', 'The authored revision check is unavailable or still pending', retryable=True) from exc


@contextmanager
def open_observer(root, deadline):
    """Own one bounded subprocess for this hook; every observation reopens state."""
    import os
    import time
    if deadline <= time.monotonic():
        raise DomainError('bootstrap_pending', 'The authored revision check is pending', retryable=True)
    process = None
    try:
        process = subprocess.Popen([sys.executable, '-I', '-c', _OBSERVER_CODE, json.dumps(sys.path), str(root)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        os.set_blocking(process.stdin.fileno(), False)
        os.set_blocking(process.stdout.fileno(), False)
        yield _Observer(process, deadline)
    except OSError as exc:
        raise DomainError('bootstrap_pending', 'The authored revision check is unavailable or still pending', retryable=True) from exc
    finally:
        if process is not None:
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()
            if process.poll() is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=.1)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
            try:
                process.wait(timeout=.2)
            except subprocess.TimeoutExpired as exc:
                raise DomainError('bootstrap_pending', 'The revision observer could not be reaped', retryable=True) from exc

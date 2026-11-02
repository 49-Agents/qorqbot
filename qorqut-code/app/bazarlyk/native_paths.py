"""Tool locations supplied by the native launcher; ordinary deployments keep their defaults."""
import os
from pathlib import Path


def root():
    value = os.environ.get('QORQUT_NATIVE_ROOT')
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute() or not path.is_dir():
        raise ValueError('QORQUT_NATIVE_ROOT must name the installed native runtime')
    return path


def executable(name, default=None):
    native = root()
    if native is None:
        return default or name
    path = native / 'bin' / name
    if not path.is_file() or not os.access(path, os.X_OK):
        raise FileNotFoundError('Missing bundled tool: ' + str(path))
    return str(path)


def environment(base):
    """Retain each caller's restricted environment, adding only bundled tool locations."""
    result = dict(base)
    native = root()
    if native is not None:
        result.update({'QORQUT_NATIVE_ROOT': str(native), 'LD_LIBRARY_PATH': str(native / 'lib'),
                       'GIT_EXEC_PATH': str(native / 'lib/git-core'),
                       'GIT_TEMPLATE_DIR': str(native / 'share/git-core/templates'),
                       'SSL_CERT_FILE': str(native / 'share/ca-certificates.crt')})
        result['PATH'] = str(native / 'bin') + ':' + str(native / 'node/bin') + ':' + result.get('PATH', os.defpath)
    return result

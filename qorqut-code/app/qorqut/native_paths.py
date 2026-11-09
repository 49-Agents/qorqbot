"""Folder-installation locations and legacy bundled-tool compatibility."""
import os
from pathlib import Path
import shutil
import sys


MAC_CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'


def folder_root():
    """The writable application folder, independent of where its tools are installed."""
    value = os.environ.get('QORQUT_FOLDER_ROOT')
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute() or not path.is_dir():
        raise ValueError('QORQUT_FOLDER_ROOT must name the application folder')
    return path


def installed_executable(name, *alternatives, override=None):
    """Select a user-installed tool; an explicit path never silently falls back."""
    if override and os.environ.get(override):
        return os.environ[override]
    return next((os.path.abspath(found) for candidate in (name, *alternatives)
                 if (found := shutil.which(candidate))), None)


def installed_chrome():
    """Find installed Chrome without downloading it or overriding an explicit choice."""
    selected = installed_executable('google-chrome-stable', 'google-chrome', 'chromium', 'chromium-browser',
                                    override='QORQUT_CHROME_EXECUTABLE')
    if selected is not None:
        return selected
    if sys.platform == 'darwin' and Path(MAC_CHROME).is_file() and os.access(MAC_CHROME, os.X_OK):
        return MAC_CHROME
    return None


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
    """Retain the caller's restricted environment and the selected installation paths."""
    result = dict(base)
    folder = folder_root()
    if folder is not None:
        result['QORQUT_FOLDER_ROOT'] = str(folder)
    native = root()
    if native is not None:
        result.update({'QORQUT_NATIVE_ROOT': str(native), 'LD_LIBRARY_PATH': str(native / 'lib'),
                       'GIT_EXEC_PATH': str(native / 'lib/git-core'),
                       'GIT_TEMPLATE_DIR': str(native / 'share/git-core/templates'),
                       'SSL_CERT_FILE': str(native / 'share/ca-certificates.crt')})
        result['PATH'] = str(native / 'bin') + ':' + str(native / 'node/bin') + ':' + result.get('PATH', os.defpath)
    return result

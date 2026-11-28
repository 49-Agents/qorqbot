"""Folder-installation locations and legacy bundled-tool compatibility."""
import os
from pathlib import Path
import shutil
import sys


MAC_CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
# Google Chrome's program names on PATH (Linux). Chromium is not among them: it is no closer to Google Chrome than Qorqbot's
# own downloaded build.
GOOGLE_CHROME_NAMES = ('google-chrome-stable', 'google-chrome')


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
    return _mac_chrome()


def installed_google_chrome():
    """The installed Google Chrome itself, or None: never Chromium, and never ``QORQUT_CHROME_EXECUTABLE`` (an explicit
    choice is the caller's to honour first)."""
    return installed_executable(*GOOGLE_CHROME_NAMES) or _mac_chrome()


def _mac_chrome():
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


def shipped(name):
    """A program the installed package ships itself (``runtime/native/bin`` of a self-contained folder), or None."""
    folder = folder_root()
    if folder is None:
        return None
    path = folder / 'runtime' / 'native' / 'bin' / name
    return str(path) if path.is_file() and os.access(path, os.X_OK) else None


def executable(name, default=None):
    native = root()
    if native is None:
        return shipped(name) or default or name
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

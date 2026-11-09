"""Detached browser supervision for the folder installation, without system services.

The existing supervisor owns the process lock and records boot/start identities.
AgentBrowserManager retains its authorization, receipts and exact-process stop.
"""
import os
from pathlib import Path
import subprocess
import sys

from qorqut import native_paths


class NativeLauncher:
    def __init__(self, state_root):
        self.root = Path(state_root)

    def directory(self, unit):
        from .agent_browsers import UNIT, _read_json, unit_name
        if not UNIT.fullmatch(unit):
            raise ValueError('Invalid browser supervisor identity')
        if self.root.is_dir():
            for path in self.root.iterdir():
                if path.is_dir() and unit_name(self.root, path.name) == unit:
                    return path
        return None

    def state(self, unit):
        from .agent_browsers import _read_json, supervisor_state
        directory = self.directory(unit)
        if directory is None:
            return 'inactive'
        meta = _read_json(directory / 'meta.json')
        if meta is None:
            return 'inactive'
        observed = supervisor_state(directory, meta)
        return 'active' if observed.get('held') else 'inactive'

    def active(self, unit):
        return self.state(unit) == 'active'

    def start(self, unit, directory):
        from .agent_browsers import unit_name
        directory = Path(directory)
        if directory.parent != self.root or unit_name(self.root, directory.name) != unit:
            raise ValueError('Browser supervisor does not belong to this installation')
        if self.active(unit):
            return False
        search_path = os.environ.get('PATH', os.defpath) if native_paths.folder_root() else os.defpath
        env = native_paths.environment({'PATH': search_path, 'HOME': str(directory), 'LANG': 'C.UTF-8',
                                        'PYTHONDONTWRITEBYTECODE': '1'})
        for key, name in (('TMPDIR', 'tmp'), ('XDG_CACHE_HOME', 'cache'), ('XDG_CONFIG_HOME', 'config'),
                          ('XDG_RUNTIME_DIR', 'run')):
            path = directory / name
            path.mkdir(mode=0o700, exist_ok=True)
            env[key] = str(path)
        with (directory / 'supervisor.log').open('ab') as log:
            subprocess.Popen([sys.executable, '-B', '-m', 'qorqut.browser.agent_supervisor', '--state', str(directory)],
                             env=env, cwd=directory, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             start_new_session=True, close_fds=True)
        return True

    def stop(self, unit):
        # Manager has already stopped the exact recorded processes, including its bounded escalation.
        # No service manager or broad process lookup is needed in this installation.
        self.directory(unit)

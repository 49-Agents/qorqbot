"""Scoped executable feature probes, cached by installed file identity, not version labels."""
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


def executable_identity(executable):
    path = Path(shutil.which(str(executable)) or executable).resolve(strict=True)
    info = path.stat()
    companions = []
    if path.name == 'codex.js':
        for candidate in sorted(path.parent.parent.glob('node_modules/@openai/codex-*/vendor/*/bin/codex')):
            entry = candidate.stat()
            companions.append((str(candidate),entry.st_dev,entry.st_ino,entry.st_size,entry.st_mtime_ns))
    return str(path), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, json.dumps(companions)


@lru_cache(maxsize=32)
def _probe(identity):
    executable = identity[0]
    def help(*arguments):
        try:
            result = subprocess.run([executable,*arguments,'--help'],stdin=subprocess.DEVNULL,
                capture_output=True,text=True,timeout=2,env={**os.environ,'NO_COLOR':'1'})
            return result.stdout if result.returncode == 0 else ''
        except (OSError,subprocess.SubprocessError):return ''
    root=help()
    if 'codex' in root.lower():
        execute,resume,queue=help('exec'),help('exec','resume'),help('queue')
        return {'json_events':'--json' in execute,'resume':'--json' in resume,
            'isolated_configuration':'--ignore-user-config' in execute,
            'queue':'--thread' in queue and '--message' in queue,
            'hooks':'--dangerously-bypass-hook-trust' in root,
            'scoped_mcp':'--strict-config' in root,
            'identity':list(identity),'verification':'executable_feature_surface',
            'native_session_observed':False}
    return {'hooks':'--settings' in root and '--print' in root,'scoped_mcp':'--mcp-config' in root,
        'resume':'--resume' in root,'identity':list(identity),
        'verification':'executable_feature_surface','native_session_observed':False}


def probe(executable):
    try:return _probe(executable_identity(executable))
    except OSError:return {'verification':'executable_unavailable','native_session_observed':False}


def checkpoint_supported(executable,records=None):
    # Help text cannot establish correction lineage. The installer accepts focused
    # native contract evidence, bound to this exact executable build identity.
    if records is None:
        from bazarlyk.installation import installation_config
        config=installation_config()
        records=Path(config['root'])/'state/native-contracts' if config else None
    if records is None:return False
    try:
        identity=executable_identity(executable)
        key=hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        value=json.loads(Path(records).joinpath(key+'.json').read_text())
        return value['identity']==list(identity) and value['stop_hook_correction'] is True
    except (OSError,ValueError,KeyError):return False


def record_contract(executable,records,**features):
    from bazarlyk.release_worker.lifetime import atomic_json
    identity=executable_identity(executable)
    key=hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    atomic_json(Path(records)/(key+'.json'),{'identity':list(identity),**features})


@lru_cache(maxsize=32)
def _browser_probe(identity):
    try:
        result=subprocess.run([identity[0],'--help'],stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=2)
        return result.returncode==0 and all(word in result.stdout.lower() for word in
            ('snapshot','connect','eval','--session','--cdp','--config'))
    except (OSError,subprocess.SubprocessError):return False


def browser_supported(executable,runtime):
    try:
        references=[]
        for path in (Path(executable),Path(runtime)/'node/bin/node',Path(runtime)/'node_modules/agent-browser/package.json',
                Path(runtime)/'node_modules/agent-browser/bin/agent-browser.js'):
            if path.exists():
                value=path.stat();references.append((str(path),value.st_ino,value.st_size,value.st_mtime_ns))
        return _browser_probe((str(executable),json.dumps(references)))
    except OSError:return False

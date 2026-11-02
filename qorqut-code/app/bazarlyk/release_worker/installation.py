"""One-time persistent installation and package-source migration primitives."""
from __future__ import annotations

from contextlib import nullcontext

import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import zipfile

from .errors import ReleaseError
from .lifetime import atomic_json, exclusion

# What a staged core artifact must carry: the committed host storage contribution, recovery code and packaged resources.
CORE_RESOURCES = [("bazarlyk.composition", "schema_ddl.py"), ("bazarlyk.storage", "recovery.py"),
    ("bazarlyk.bootstrap", "instructions/agent-foundation.md"), ("bazarlyk.bootstrap", "instructions/ecosystem.md"),
    ("bazarlyk.bootstrap", "instructions/operating/ecosystem-agent.md"), ("bazarlyk.release_worker", "installation.schema.json")]

HELPERS = ["bazarlyk.bootstrap." + name for name in
    ("claude_session", "codex_session", "mcp_headers", "mcp_reconnect", "transcript_snapshot",
     "activity_hook", "browser_entry", "supervisor", "migration", "reference_probe")]


def validate_config(config):
    if config.get('release_layout') == 'qorqut-v1.2':
        raise ReleaseError('The v1 release lifecycle cannot operate on a v1.2 instance; use the explicit v1.2 commands')
    from importlib.resources import files
    from jsonschema import Draft202012Validator
    schema=json.loads(files('bazarlyk.release_worker').joinpath('installation.schema.json').read_text())
    errors=sorted(Draft202012Validator(schema).iter_errors(config),key=lambda error:str(list(error.path)))
    if errors:
        raise ReleaseError('Invalid installation field: '+'.'.join(map(str,errors[0].path)))
    # The extension selection is structural here: each package validates its own configuration when the host starts it.
    from bazarlyk.extensions import validate_entries
    from qorqut_operations.contracts import DomainError
    try:
        validate_entries(config.get('extensions'))
    except DomainError as exc:
        raise ReleaseError('Invalid extensions installation selection') from exc
    from bazarlyk.bootstrap.client import service_url
    service_url(config['origin'])
    return config


def dependency_key(lock, *, python=sys.executable):
    # The lock must contain resolved transitive versions and exact internal sources.
    abi = subprocess.check_output([python, '-I', '-c',
        'import sys,sysconfig;print(sys.implementation.cache_tag,sysconfig.get_platform())'], text=True).strip()
    return hashlib.sha256((abi + '\n').encode() + Path(lock).read_bytes()).hexdigest()


def install_dependencies(root, lock, *, python=sys.executable, run=subprocess.run,reference=None):
    target = Path(root) / 'runtime/python' / dependency_key(lock, python=python)
    if reference:reference(target,lock)
    marker = target / 'validated.json'
    if marker.is_file():
        return target
    if target.exists():
        raise ReleaseError('Unvalidated dependency environment exists; inspect it before explicit recovery')
    target.parent.mkdir(parents=True, exist_ok=True)
    run([python, '-I', '-m', 'venv', str(target)], check=True)
    interpreter = str(target / 'bin/python')
    run([interpreter, '-I', '-m', 'pip', 'install', '--disable-pip-version-check', '-r', str(Path(lock).resolve())], check=True)
    run([interpreter, '-I', '-m', 'pip', 'check'], check=True)
    result = run([interpreter, '-I', '-m', 'pip', 'list', '--format=json'], check=True, capture_output=True, text=True)
    if any(item['name'].lower().replace('_', '-') == 'bazarlyk-core' for item in json.loads(result.stdout)):
        raise ReleaseError('Dependency environment must not contain an application installation')
    if list(target.glob('lib/python*/site-packages/*editable*')):
        raise ReleaseError('Editable dependencies are not immutable installation inputs')
    atomic_json(marker, {'key': target.name, 'lock_sha256': hashlib.sha256(Path(lock).read_bytes()).hexdigest(),
                         'resolved': json.loads(result.stdout)})
    return target


def unpack_wheel(wheel, target):
    target = Path(target)
    if target.exists():
        raise ReleaseError('Immutable code artifact already exists')
    with zipfile.ZipFile(wheel) as archive:
        for item in archive.infolist():
            relative = Path(item.filename)
            if relative.is_absolute() or '..' in relative.parts or (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise ReleaseError('Wheel contains an unsafe entry')
        target.mkdir(parents=True)
        archive.extractall(target)
    # .dist-info stays adjacent to the module: importlib.metadata and resources work.
    if not (target / 'bazarlyk/__init__.py').is_file() or not list(target.glob('*.dist-info/METADATA')):
        raise ReleaseError('Core wheel lacks application code or distribution metadata')
    return target


def install_helpers(root, wheel, python, *, launcher_source, activate=False,reference=None,lock_held=False):
    root, wheel = Path(root), Path(wheel)
    key = hashlib.sha256(wheel.read_bytes()).hexdigest()
    target = root / 'runtime/agent-tools' / key
    if not Path(python).is_file() or not os.access(python, os.X_OK):
        raise ReleaseError('Persistent helper interpreter is missing or not executable')
    expected = {'format': 1, 'code': str(target / 'code'),
                'python': str(python), 'modules': HELPERS, 'wheel_sha256': key, 'binding_protocol': 1}
    with (nullcontext() if lock_held else exclusion(root)):
        if reference:reference(target,python)
        if not (target / 'manifest.json').is_file():
            unpack_wheel(wheel, target / 'code')
            subprocess.run([str(python), '-I', '-B', '-c',
                'import sys,importlib;sys.path.insert(0,sys.argv[1]);'
                '[importlib.import_module(name) for name in '+repr(HELPERS)+'];'
                'from bazarlyk.release_worker.schema_contract import declared;assert declared()["binding_protocol"]==1', str(target / 'code')], check=True)
            atomic_json(target / 'manifest.json', expected)
        if json.loads(target.joinpath('manifest.json').read_text()) != expected:
            raise ReleaseError('Retained helper manifest differs from the validated interpreter, modules or fencing contract')
        if activate:
            bin_root = root / 'bin'; bin_root.mkdir(parents=True, exist_ok=True)
            launcher = bin_root / 'launcher.py'
            shutil.copyfile(launcher_source, launcher); launcher.chmod(0o755)
            for name in ('app', 'agent-helper', 'reference-probe'):
                link = bin_root / name
                if not link.exists():
                    link.symlink_to('launcher.py')
            atomic_link(root / 'runtime/agent-tools/current', target)
    return target


def atomic_link(path, target):
    path = Path(path)
    temporary = path.with_name('.' + path.name + '-' + str(os.getpid()))
    temporary.symlink_to(target)
    try:
        os.replace(temporary, path)
        descriptor=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def migrate_sources(workspace, root, registry, *, config_path=None):
    from bazarlyk.catalog.sources import load_sources
    from bazarlyk.catalog.packages import load_package
    root = Path(root)
    migrated = {}
    with exclusion(root):
        for name, (kind, source, manifest) in load_sources(workspace, config_path=config_path).items():
            package = load_package(source, manifest, kind, registry)
            target = root / 'packages' / package.digest.removeprefix('sha256:')
            for relative, content in package.files.items():
                path = target / relative
                if path.exists() and path.read_bytes() != content:
                    raise ReleaseError('Persistent package artifact conflicts with verified source')
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            migrated[name] = {'kind': kind, 'path': str(target), 'manifest': manifest,
                             'repository': str(source)}
        atomic_json(root / 'config/package-sources.json', migrated)
    return migrated


def resolved_dependencies(root, inputs, *, python=sys.executable, run=subprocess.run,reference=None):
    """Resolve complete transitive inputs once, then build at the final immutable path."""
    abi=run([str(python),'-I','-c','import sys,sysconfig;print(sys.implementation.cache_tag,sysconfig.get_platform())'],
        check=True,capture_output=True,text=True).stdout
    key = hashlib.sha256((abi+json.dumps(inputs, sort_keys=True)).encode()).hexdigest()
    directory = Path(root)/'state/dependency-resolutions'/key
    lock = directory/'requirements.lock'
    directory.mkdir(parents=True,exist_ok=True)
    if not lock.is_file():
        requirements = directory/'requirements.in'
        requirements.write_text('\n'.join(inputs['dependencies'] + [item for values in inputs.get('extras',{}).values() for item in values])+'\n')
        constraints = directory/'constraints.txt'; constraints.write_text(inputs.get('constraints',''))
        report = directory/'resolution.json'
        run([str(python),'-I','-m','pip','install','--dry-run','--ignore-installed','--report',str(report),
            '-c',str(constraints),'-r',str(requirements)],check=True,capture_output=True)
        lines = []
        for item in json.loads(report.read_text())['install']:
            name, version = item['metadata']['name'], item['metadata']['version']
            download = item['download_info']
            vcs = download.get('vcs_info')
            if vcs:
                if vcs['vcs'] != 'git' or not vcs.get('commit_id'):raise ReleaseError('Internal dependency must resolve to an immutable Git commit')
                lines.append(name+' @ git+'+download['url']+'@'+vcs['commit_id'])
            elif download['url'].startswith('file:'):
                digest = download.get('archive_info',{}).get('hashes',{}).get('sha256')
                if not digest:raise ReleaseError('Local dependency needs a verified immutable wheel')
                lines.append(name+' @ '+download['url']+'#sha256='+digest)
            else:lines.append(name+'=='+version)
        lock.write_text('\n'.join(sorted(lines))+'\n')
    environment=install_dependencies(root,lock,python=python,run=run,reference=reference)
    validate_dependencies(environment/'bin/python',inputs,run=run)
    return environment, lock


def browser_inputs(directory):
    directory=Path(directory)
    digest=hashlib.sha256((platform.system()+':'+platform.machine()).encode())
    files = sorted(p for p in directory.iterdir() if p.is_file() and p.suffix in {'.py','.json','.js','.cjs','.sh'}
        and p.name not in {'validated.json','runtime-manifest.json','manifest.json'})
    if not {'package.json','package-lock.json','build_runtime.py'} <= {p.name for p in files}:
        raise ReleaseError('Browser runtime inputs are incomplete')
    for path in files:digest.update(path.name.encode()+b'\0'+path.read_bytes())
    return digest.hexdigest(), files


def ensure_browser(root, source, existing=None, *, python=sys.executable, run=subprocess.run,reference=None):
    key, files = browser_inputs(source)
    if existing:
        existing=Path(existing)
        try:existing_key,_=browser_inputs(existing)
        except (OSError,ReleaseError):existing_key=None
        if existing_key==key and (existing/'node/bin/node').is_file() and (existing/'node_modules').is_dir():
            if reference:reference(existing)
            marker=existing/'validated.json'
            if not marker.is_file() or json.loads(marker.read_text()).get('key')!=key:
                validate_browser(existing,run=run)
                atomic_json(marker,{'key':key})
            return existing,key
    target=Path(root)/'runtime/browser'/key
    if reference:reference(target)
    if target.joinpath('validated.json').is_file():return target,key
    if target.exists():raise ReleaseError('Incomplete browser artifact needs explicit reconciliation')
    target.mkdir(parents=True)
    for path in files:shutil.copy2(path,target/path.name)
    run([str(python),'-I',str(target/'build_runtime.py')],check=True,capture_output=True)
    validate_browser(target,run=run)
    atomic_json(target/'validated.json',{'key':key})
    return target,key


def validate_browser(directory, *, run=subprocess.run):
    directory=Path(directory)
    # Locked versions remain reproducible inputs; selection checks executable
    # behavior rather than requiring a particular CLI patch number.
    result=run([str(directory/'node/bin/node'),'-e',
        'const fs=require("fs");const l=JSON.parse(fs.readFileSync("package-lock.json"));'
        'for(const [p,v] of Object.entries(l.packages)){if(p&&v.version&&'
        'JSON.parse(fs.readFileSync(p+"/package.json")).version!==v.version)throw Error("lock mismatch");}'
        'require("proxy-chain");require("agent-browser/package.json");'],
        check=True,cwd=directory,capture_output=True)
    return result


def stage_initial(root,config,sources,python,*,run=subprocess.run,recovery_name=None,helper_compatibility=None):
    from .ordinary import AppRelease
    if set(sources)!={'core','dashboard','template'}:raise ReleaseError('Initial staging needs exact core/dashboard/template source refs')
    worker=AppRelease(config,run=run)
    release_id=recovery_name or 'initial-'+hashlib.sha256(json.dumps(sources,sort_keys=True).encode()).hexdigest()[:16]
    if not __import__('re').fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}',release_id):raise ReleaseError('Recovery name must be a managed release name')
    with exclusion(root,shared=True):
        from .staging import StagingRoot
        target=Path(root)/'releases'/release_id
        staging=StagingRoot(root,release_id,[target,python,config['browser_runtime'],config['package_sources']])
        components={name:worker.build_component(name,reference,release_id,staging=staging) for name,reference in sources.items()}
        core=Path(components['core']['path']).parent
        inputs=json.loads(core.joinpath('dependency-inputs.json').read_text())
        validate_dependencies(python,inputs,run=run)
        compatibility=json.loads(core.joinpath('core-compatibility.json').read_text())
        browser,key=ensure_browser(root,core/'source/tools/browser',config['browser_runtime'],python=config['build_python'],run=run,reference=staging.include)
        if target.exists():raise ReleaseError('Initial recovery identity already exists; never replace an immutable manifest')
        manifest={'format':1,'release_id':release_id,'components':components,'python':str(python),
            'dependency_inputs':inputs,'schema_min':compatibility['schema_min'],'schema_max':compatibility['schema_max'],
            'binding_protocol':compatibility['binding_protocol'],'browser_runtime':str(browser),'browser_key':key,
            'helper_compatibility':helper_compatibility,
            'runtime_references':[str(browser),config['package_sources']]}
        atomic_json(target/'manifest.json',manifest)
        probe_core(python,components['core']['path'],run=run)
        staging.ready()
    return target,manifest


def probe_core(python,path,*,run=subprocess.run):
    """The staged core imports from its own path, is installed-distribution metadata and carries its current resources."""
    return run([str(python),'-I','-B','-c',
        'import sys,importlib.metadata,importlib.resources;sys.path.insert(0,sys.argv[1]);import bazarlyk.service;'
        'assert bazarlyk.service.__file__.startswith(sys.argv[1]);importlib.metadata.version("bazarlyk-core");'
        'assert all(importlib.resources.files(package).joinpath(name).is_file() for package,name in '+repr(CORE_RESOURCES)+')',
        str(path)],check=True,capture_output=True)


def select_recovery(root,manifest_path,*,kind='cold_start',locked=False):
    """Pin an exact staged manifest; never imply independent bug fallback coverage."""
    root=Path(root);path=Path(manifest_path).resolve(strict=True)
    if path.name!='manifest.json' or path.parent.parent!=(root/'releases').resolve():
        raise ReleaseError('Recovery manifest must be a retained managed application artifact')
    value=json.loads(path.read_text())
    if value.get('binding_protocol')!=1:raise ReleaseError('Recovery source must understand generation fencing')
    record={'manifest':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'kind':kind,
        'release_id':value['release_id'],'schema_min':value['schema_min'],'schema_max':value['schema_max'],
        'binding_protocol':value['binding_protocol']}
    from contextlib import nullcontext
    with nullcontext() if locked else exclusion(root):
        atomic_json(root/'state/recovery.json',record)
        from .staging import handoff
        handoff(root,value['release_id'],root/'state/recovery.json')
    return record


def validate_dependencies(python,inputs,*,run=subprocess.run):
    """Check source requirements, extras, transitive metadata and the provenance a requirement names.

    A plain version pin needs only a satisfying installed version. A ``git+`` requirement needs its exact commit; any other
    direct URL (an explicit local wheel) needs the recorded URL and, when the requirement supplies ``#sha256=``, the
    recorded (or, failing that, the file's own) SHA-256.

    pip check alone does not know the application requirements because application
    code is deliberately absent from the dependency environment.
    """
    script = '''import importlib.metadata as m,json,sys
from pip._vendor.packaging.requirements import Requirement
from pip._vendor.packaging.markers import default_environment
inputs=json.loads(sys.argv[1]);pending=list(inputs['dependencies'])
pending += [r for rs in inputs.get('extras',{}).values() for r in rs]
seen=set();installed={d.metadata['Name'].lower().replace('_','-'):d for d in m.distributions()}
while pending:
 text=pending.pop();r=Requirement(text)
 if r.marker and not r.marker.evaluate({**default_environment(),'extra':''}):continue
 key=(r.name.lower().replace('_','-'),tuple(sorted(r.extras)),r.url,str(r.specifier))
 if key in seen:continue
 seen.add(key);d=installed.get(key[0])
 if d is None or not r.specifier.contains(d.version,prereleases=True):raise ValueError('Unsatisfied dependency input: '+r.name)
 if r.url:
  direct=json.loads(d.read_text('direct_url.json') or '{}')
  if r.url.startswith('git+'):
   url,commit=r.url[4:].rsplit('@',1)
   if direct.get('url')!=url or direct.get('vcs_info',{}).get('commit_id')!=commit:raise ValueError('Incorrect immutable source: '+r.name)
  elif direct.get('url')!=r.url.split('#',1)[0]:raise ValueError('Incorrect direct dependency source: '+r.name)
  else:
   supplied=dict(part.split('=',1) for part in r.url.partition('#')[2].split('&') if '=' in part).get('sha256')
   if supplied:
    recorded=direct.get('archive_info',{}).get('hashes',{}).get('sha256')
    if recorded is None and direct['url'].startswith('file://'):
     import hashlib,urllib.parse
     recorded=hashlib.sha256(open(urllib.parse.unquote(urllib.parse.urlparse(direct['url']).path),'rb').read()).hexdigest()
    if recorded!=supplied.lower():raise ValueError('Incorrect direct dependency hash: '+r.name)
 for text in d.requires or []:
  child=Requirement(text)
  if not child.marker or any(child.marker.evaluate({**default_environment(),'extra':extra}) for extra in ('',*r.extras)):
   child.marker=None;pending.append(str(child))
for text in inputs.get('constraints','').splitlines():
 text=text.split('#',1)[0].strip()
 if not text:continue
 r=Requirement(text);d=installed.get(r.name.lower().replace('_','-'))
 if d and not r.specifier.contains(d.version,prereleases=True):raise ValueError('Constraint mismatch: '+r.name)
print(json.dumps({'validated':True,'requirements':len(seen)}))
'''
    return run([str(python),'-I','-B','-c',script,json.dumps(inputs)],check=True,capture_output=True,text=True)


def staged_units(root,config):
    """The reviewed fixed units, as exact text; the installer stages and activation compares them."""
    from shlex import quote
    root=Path(root)
    user=('User='+config['service_user']+'\n') if config.get('service_user') else ''
    environment=''.join('Environment='+json.dumps(name+'='+value)+'\n' for name,value in config.get('service_environment',{}).items())
    return {config['service']:'[Unit]\nDescription=Qorqut application\n[Service]\nType=simple\n'+
        user+environment+f'ExecStart={quote(str(root/"bin/dashboard-foreground"))}\nRestart=on-failure\nTimeoutStopSec=20\nKillMode=process\n'
        f'StandardOutput=append:{root}/state/dashboard/service.log\nStandardError=inherit\n'
        '[Install]\nWantedBy=default.target\n',
        'qorqut-agent-supervisor.service':'[Unit]\nDescription=Qorqut helper supervision\n[Service]\nType=simple\n'+
        user+environment+f'ExecStart={quote(str(root/"bin/agent-helper"))} bazarlyk.bootstrap.supervisor --config {quote(str(root/"config/installation.json"))}\n'
        'Restart=on-failure\nTimeoutStopSec=10\n[Install]\nWantedBy=default.target\n'}


def cgroup_evidence(application_pid,native_pids,*,proc=Path('/proc')):
    """Bounded read-only check; pane names are not evidence of cgroup isolation."""
    proc=Path(proc)
    try:
        app=proc.joinpath(str(application_pid),'cgroup').read_text().splitlines()
        observations=[]
        for pid in native_pids:
            group=proc.joinpath(str(pid),'cgroup').read_text().splitlines()
            stat=proc.joinpath(str(pid),'stat').read_text().rsplit(')',1)[1].split()
            observations.append({'pid':pid,'started':stat[19],'outside_application':group!=app})
        return {'verified':bool(observations) and all(v['outside_application'] for v in observations),'processes':observations}
    except (OSError,IndexError):return {'verified':False,'reason':'cgroup_observation_unavailable'}

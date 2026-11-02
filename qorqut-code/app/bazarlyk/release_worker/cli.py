"""Command line entry point for the shared Qorqut release worker."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import os

from .errors import ReleaseError
from .manifest import load_manifest, plan_manifest
from .worker import ReleaseWorker


def _json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True))


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--plan", type=Path, help="frozen plan.json")
    parser.add_argument("--manifest", type=Path, help="release manifest for plan/inspect")
    parser.add_argument("--records-root", type=Path, default=Path(".qorqut-releases"))
    parser.add_argument("--installation-config", type=Path)


def _worker(args: argparse.Namespace) -> ReleaseWorker:
    if not args.plan:
        raise ReleaseError("--plan is required for this command")
    config = {}
    if args.installation_config:
        config = json.loads(args.installation_config.read_text())
        if not isinstance(config, dict):
            raise ReleaseError("installation configuration must be an object")
    records_root = args.records_root
    if records_root == Path(".qorqut-releases") and args.plan.parent.name == args.plan.parent.parent.name:
        records_root = args.plan.parent.parent
    elif records_root == Path(".qorqut-releases") and args.plan.name == "plan.json":
        records_root = args.plan.parent.parent
    return ReleaseWorker.from_plan_path(args.plan, records_root=records_root, installation_config=config or None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="release-worker", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="resolve exact commits and freeze a plan")
    plan.add_argument("--manifest", type=Path, required=True)
    plan.add_argument("--records-root", type=Path, default=Path(".qorqut-releases"))
    plan.add_argument("--installation-config", type=Path, help="effect-bearing installation configuration to freeze")
    inspect = commands.add_parser("inspect", help="discover source/runtime state")
    _common(inspect)
    for name in ("check", "build", "stage", "deploy", "adopt", "status", "resume", "recover", "cleanup", "merge"):
        command = commands.add_parser(name)
        _common(command)
        if name in {"deploy", "adopt", "recover", "cleanup", "merge"}:
            command.add_argument(f"--confirm-{name}", action="store_true")
        if name == 'deploy':
            command.add_argument('--recover-from',help='explicit recovery of the active failed short-path operation ID')
            command.add_argument('--migrate',action='store_true')
            command.add_argument('--backup-before-migration',action='store_true')
            for component in ('core','dashboard'):
                command.add_argument('--'+component,help='changed /absolute/repository@ref; unchanged partners come from current')
    activate = commands.add_parser('activate',help='first activation of the persistent installation from a legacy service')
    _common(activate);activate.add_argument('--release',type=Path,required=True,help='absolute installer-staged application directory')
    mode = activate.add_mutually_exclusive_group()
    mode.add_argument('--prepare',action='store_true',help='freeze and record exact inputs and effects; change nothing')
    mode.add_argument('--prepared',help='execute the exact prepared activation ID')
    mode.add_argument('--recover-from',help='explicitly replace the failed activation operation ID with this staged application')
    activate.add_argument('--migrate',action='store_true')
    activate.add_argument('--backup-before-migration',action='store_true')
    rollback = commands.add_parser('rollback',help='restore a retained schema/binding-compatible app')
    _common(rollback);rollback.add_argument('--release',type=Path,required=True)
    prune = commands.add_parser('prune',help='reference-based retention, report-only by default')
    _common(prune);prune.add_argument('--delete',action='store_true')
    abandon = commands.add_parser('abandon',help='retire an unfinished build after its exact owner process exits')
    _common(abandon);abandon.add_argument('--build-id',required=True)
    authorize = commands.add_parser("authorize", help="record supplied owner authorization evidence")
    _common(authorize)
    authorize.add_argument("--scope", choices=("merge", "deploy", "adopt", "recover", "cleanup"), required=True)
    authorize.add_argument("--evidence", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command=='deploy' and args.recover_from and (args.plan or args.manifest):
            raise ReleaseError('--recover-from uses the saved-installation short path, not a coordinated plan')
        if args.command in {'deploy','status','rollback','prune','resume','abandon','activate'} and not args.plan and not args.manifest:
            path=args.installation_config or os.environ.get('QORQUT_INSTALLATION_CONFIG')
            if not path:raise ReleaseError('Saved installation configuration is required for the short path')
            config=json.loads(Path(path).read_text())
            from .installation import validate_config
            validate_config(config)
            from .ordinary import AppRelease
            worker=AppRelease(config)
            if args.command=='deploy':
                _json(worker.deploy({name:getattr(args,name) for name in ('core','dashboard') if getattr(args,name)},migrate=args.migrate,backup_before_migration=args.backup_before_migration,recover_from=args.recover_from))
            elif args.command=='activate':
                from .activation import Activation
                _json(Activation(worker).activate(args.release,migrate=args.migrate,backup_before_migration=args.backup_before_migration,
                    prepare=args.prepare,prepared=args.prepared,recover_from=args.recover_from))
            elif args.command=='resume':_json(worker.reconcile())
            elif args.command=='status':_json(worker.status())
            elif args.command=='rollback':_json(worker.rollback(args.release))
            elif args.command=='abandon':
                from .staging import abandon
                _json(abandon(worker.root,args.build_id))
            else:
                from .retention import Pruner
                _json(Pruner(config).prune(delete=args.delete))
            return 0
        if args.command == "plan":
            manifest = load_manifest(args.manifest)
            if args.installation_config:
                config = json.loads(args.installation_config.read_text())
                if not isinstance(config, dict):
                    raise ReleaseError("installation configuration must be an object")
                manifest["installation"] = config
            value = plan_manifest(manifest)
            worker = ReleaseWorker(value, records_root=args.records_root)
            record = worker.initialize()
            _json({"plan": worker.store.plan(), "record": str(args.records_root / value["release_id"] / "record.json"), "state": record["state"]})
            return 0
        if args.command == "inspect" and args.manifest:
            worker = ReleaseWorker.from_manifest_path(args.manifest, records_root=args.records_root)
            _json(worker.inspect())
            return 0
        worker = _worker(args)
        if args.command == "authorize":
            _json(worker.authorize(args.scope, args.evidence))
        elif args.command == "inspect":
            _json(worker.inspect())
        elif args.command == "check":
            _json(worker.check())
        elif args.command == "build":
            _json(worker.build())
        elif args.command == "stage":
            _json(worker.stage())
        elif args.command == "merge":
            _json(worker.merge(confirm=args.confirm_merge))
        elif args.command == "deploy":
            _json(worker.deploy(confirm=args.confirm_deploy))
        elif args.command == "adopt":
            _json(worker.adopt(confirm=args.confirm_adopt))
        elif args.command == "status":
            _json(worker.status())
        elif args.command == "resume":
            _json(worker.resume(confirm=False))
        elif args.command == "recover":
            _json(worker.recover(confirm=args.confirm_recover))
        elif args.command == "cleanup":
            _json(worker.cleanup(confirm=args.confirm_cleanup))
        else:  # pragma: no cover - argparse constrains commands
            raise ReleaseError(f"unknown command {args.command}")
        return 0
    except (ReleaseError, OSError, ValueError) as exc:
        print(f"release-worker: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

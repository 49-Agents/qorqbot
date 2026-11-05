#!/usr/bin/env python3
"""Baseline-created pending work and its continuation controls (module-split plan 11.1, S1-S4).

One subcommand per phase, each a separate process with explicit paths, so baseline writers are always gone before a candidate
touches the same fixture root:

``capture``  baseline code only: create the pending work with the real stock-host fixtures, stop at the interruption point,
             close every store and snapshot the closed fixture (``checkpoints/<case>/closed.tar`` + ``closed.json``).
``control``  baseline continuation of that checkpoint, then the fixture root is restored to the closed state again.
``resume``   the same continuation with whatever code the import options select (the candidate), no automatic restore.
``restore``  put the closed checkpoint back at the same absolute fixture root.
``check``    compare candidate evidence with the baseline control evidence (imports no host code at all).
``info``     what this interpreter would import and where from.

Import selection is always explicit: ``--code-path`` roots go first on ``sys.path``, ``--component NAME=DIR`` names the
code locations stored in ``InstancePaths``, and ``--expect-origin-under`` / ``--forbid-origin-under`` turn an accidental import
of the other code base into a refusal before anything runs.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import shutil
import sys

sys.dont_write_bytecode = True  # a runner never writes bytecode into the code it is judging

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import resumption_support as support  # noqa: E402  (no host, Operations or Context import)

COMPONENT_NAMES = ("app", "operations_engine", "context_engine", "dashboard")
CASE_IDS = ("S1", "S2", "S3a", "S3b", "S4a", "S4b")


class Refused(Exception):
    """The run cannot start (or must not continue): setup, integrity or import-origin problem."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# -- locations ---------------------------------------------------------------------------------------------------------------

def layout(args):
    artifacts = Path(args.artifacts).resolve()
    return {"root": artifacts, "fixtures": artifacts / "fixtures", "checkpoints": artifacts / "checkpoints",
            "evidence": artifacts / "evidence"}


def baseline_layout(checkout: Path) -> tuple:
    """The stock ``qorqut-code`` checkout layout: import roots, component locations and the sibling document-history source."""
    checkout = Path(checkout).resolve()
    group = checkout.parent
    roots = [checkout / "app", checkout / "qorqut-operations", checkout / "qorqut-context",
             group / "dependencies" / "document-history" / "src"]
    components = {"app": checkout / "app", "operations_engine": checkout / "qorqut-operations",
                  "context_engine": checkout / "qorqut-context", "dashboard": checkout / "biz-dashboard"}
    return roots, components


def selected_imports(args) -> tuple:
    roots, components = [], {}
    if args.baseline_checkout:
        roots, components = baseline_layout(args.baseline_checkout)
    roots = [Path(item) for item in (args.code_path or [])] + roots
    for item in args.component or []:
        name, _, location = item.partition("=")
        if name not in COMPONENT_NAMES or not location:
            raise Refused(f"--component expects NAME=DIR with NAME one of {', '.join(COMPONENT_NAMES)}: {item!r}")
        components[name] = Path(location).resolve()
    missing = [name for name in COMPONENT_NAMES if name not in components]
    if missing and args.command != "info":
        raise Refused("component locations are required for " + ", ".join(missing)
                      + " (use --baseline-checkout for the stock layout or --component NAME=DIR)")
    if not roots and args.command != "info":
        raise Refused("no import roots were given (--code-path DIR, or --baseline-checkout DIR)")
    return [Path(item).resolve() for item in roots], components


def guard_imports(args, roots, *, label) -> dict:
    """Select the import roots and refuse before any host code is imported when the origins are not the intended ones."""
    support.configure_imports(roots)
    origins = support.import_origins()
    problems = support.origin_problems(origins, args.expect_origin_under or (), args.forbid_origin_under or ())
    report = {"label": label, "code_paths": [str(item) for item in roots], "origins": origins,
              "expect_under": [str(Path(item).resolve()) for item in args.expect_origin_under or ()],
              "forbid_under": [str(Path(item).resolve()) for item in args.forbid_origin_under or ()],
              "distributions": support.installed_distributions(), "problems": problems}
    if problems:
        raise Refused("import origins are not the intended ones: " + "; ".join(problems))
    return report


def environment() -> dict:
    from importlib import metadata

    packages = {}
    for distribution in metadata.distributions():
        name = distribution.metadata["Name"]
        if name:
            packages[name.lower()] = distribution.version
    return {"python": sys.version.split()[0], "executable": sys.executable, "prefix": sys.prefix, "platform": platform.platform(),
            "bytecode_disabled": bool(sys.dont_write_bytecode), "third_party": dict(sorted(packages.items()))}


def runner_identity() -> dict:
    """The exact runner sources behind a piece of evidence (the commit that contains them is recorded in the report)."""
    return {path.name: support.sha256_file(path) for path in sorted(HERE.glob("*.py"))}


def source_pins(roots, components) -> dict:
    seen, pins = set(), {}
    for location in [*roots, *components.values()]:
        top = support.git(location, "rev-parse", "--show-toplevel", check=False) if Path(location).exists() else ""
        key = top or str(location)
        if key in seen:
            continue
        seen.add(key)
        pins[key] = support.git_pin(top or location)
    return pins


# -- capture -----------------------------------------------------------------------------------------------------------------

def scan_component_paths(fixture: Path, locations) -> dict:
    """Whether the saved state names the code locations it was captured with (it would then not survive a code move)."""
    needles = {str(item): str(item).encode() for item in locations}
    hits = {name: [] for name in needles}
    for path in sorted(Path(fixture).rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        data = path.read_bytes()
        for name, needle in needles.items():
            if needle in data:
                hits[name].append(path.relative_to(fixture).as_posix())
    return {"checked": sorted(needles), "found_in": {name: files for name, files in hits.items() if files}}


def command_capture(args) -> int:
    from resumption_cases import CASES, gather

    roots, components = selected_imports(args)
    paths = layout(args)
    case = CASES[args.case]
    fixture, checkpoint = paths["fixtures"] / case.id, paths["checkpoints"] / case.id
    if fixture.exists() or checkpoint.exists():
        if not args.replace:
            raise Refused(f"{case.id} already has a fixture or checkpoint; --replace deletes both and captures again")
        for target in (fixture, checkpoint):
            if target.exists():
                shutil.rmtree(target)
    imports = guard_imports(args, roots, label="baseline-capture")
    if not args.tests_dir:
        raise Refused("capture needs --tests-dir (the baseline checkout's app/tests)")
    import resumption_host as hosts

    fixture.mkdir(parents=True)
    host = hosts.build_baseline_host(fixture, Path(args.tests_dir))
    details = case.capture(host, fixture)
    hosts.close_instance(host)
    imports["origins_after"] = support.import_origins()
    snapshot = support.snapshot_tree(fixture, checkpoint / "closed.tar")
    facts = gather(fixture)
    pre = case.preconditions(facts, fixture)
    if support.digest(support.tree_manifest(fixture)) != snapshot["manifest_sha256"]:
        raise Refused("reading the closed fixture changed it")
    record = {"schema": 1, "case": case.id, "title": case.title, "created_by": "baseline", "created_at": now(),
              "fixture": str(fixture), "archive": snapshot["archive"], "archive_sha256": snapshot["archive_sha256"],
              "entries": snapshot["entries"], "manifest_sha256": snapshot["manifest_sha256"], "manifest": snapshot["manifest"],
              "capture": details, "preconditions": list(pre), "facts": facts, "imports": imports,
              "environment": environment(), "runner": runner_identity(), "source_pins": source_pins(roots, components),
              "component_paths_in_saved_state": scan_component_paths(fixture, [*roots, *components.values()]),
              "interpreter_hash_seed": os.environ.get("PYTHONHASHSEED")}
    support.write_json(checkpoint / "closed.json", record)
    print(json.dumps({"case": case.id, "checkpoint": str(checkpoint / "closed.tar"), "manifest_sha256": snapshot["manifest_sha256"],
                      "entries": snapshot["entries"], "preconditions_failed": pre.failed}, indent=2))
    return 0 if not pre.failed else 1


# -- continuation ------------------------------------------------------------------------------------------------------------

def comparable_state(before, after, volatile=None) -> dict:
    """The part of the outcome that must be identical between baseline and candidate: state of the saved rows by id, and the new
    rows (by their projection only, since their identifiers are generated by the run)."""
    result = {}
    for table, rows in after["state"].items():
        old = before["state"].get(table, {})
        left_out = (volatile or {}).get(table, ())
        rows = {key: {name: value for name, value in item.items() if name not in left_out} for key, item in rows.items()}
        fresh = [item for key, item in rows.items() if key not in old]
        result[table] = {"existing": {key: item for key, item in rows.items() if key in old},
                         "new": sorted(fresh, key=lambda item: json.dumps(item, sort_keys=True))}
    return result


def comparable_outcome(case, before, after, effects, steps, repeat_steps, applied, checks) -> dict:
    """What a baseline control and a candidate must agree on for the same checkpoint.

    Run-generated values are scrubbed (timestamps, newly generated identifiers, Git object names, engine-drawn event ids). The
    ``exact`` part is NOT scrubbed: published table content (paths and SHA-256 of the blob bytes per commit) and, for work that
    was already published at the checkpoint, the saved event ids, bindings and Context commit names."""
    known = set(before["ids"])
    comparable = support.scrub(support.jsonable({
        "variant_applied": {key: value for key, value in (applied or {}).items() if key in ("how", "operation", "mode", "kind", "variant")},
        "state": comparable_state(before, after, case.volatile), "counts": after["counts"], "files": after["files"],
        "table_history": after["git"]["operations"]["table_history"], "context_commits": after["git"]["context"]["commits"],
        "effects": {key: value for key, value in effects.items() if key != "model_calls"}, "model_calls": effects["model_calls"],
        "checks": [{"name": item["name"], "ok": item["ok"]} for item in checks]}), known)
    comparable["steps"] = support.scrub(support.jsonable(steps), known)
    comparable["repeat_steps"] = support.scrub(support.jsonable(repeat_steps), known)
    comparable["exact"] = support.jsonable(case.exact(before, after))
    return comparable


def run_continuation(args, *, label) -> tuple:
    from resumption_cases import CASES, gather

    roots, components = selected_imports(args)
    paths = layout(args)
    case = CASES[args.case]
    if args.variant not in case.variants:
        raise Refused(f"{case.id} has variants {', '.join(case.variants)}, not {args.variant!r}")
    fixture, checkpoint = paths["fixtures"] / case.id, paths["checkpoints"] / case.id
    record = support.read_json(checkpoint / "closed.json")
    if not fixture.is_dir() or support.digest(support.tree_manifest(fixture)) != record["manifest_sha256"]:
        raise Refused(f"{fixture} is not the closed checkpoint; run `restore --case {case.id}` first")
    imports = guard_imports(args, roots, label=label)
    import resumption_host as hosts

    started = now()
    before = gather(fixture)
    pre = case.preconditions(before, fixture)
    evidence = {"schema": 1, "label": label, "case": case.id, "variant": args.variant, "title": case.title, "started_at": started,
                "environment": environment(), "runner": runner_identity(), "imports": imports,
                "source_pins": source_pins(roots, components),
                "checkpoint": {"archive": record["archive"], "archive_sha256": record["archive_sha256"],
                               "manifest_sha256": record["manifest_sha256"], "fixture": str(fixture), "verified_closed": True},
                "preconditions": list(pre)}
    if pre.failed:
        evidence.update(result="invalid-checkpoint", finished_at=now())
        return evidence, 2

    steps, repeat_steps = [], []
    opened = hosts.open_instance(fixture, components)
    try:
        applied = case.prepare(opened, args.variant, before)
        case.proceed(opened, steps)
        calls = list(opened.runner.calls)
    finally:
        opened.close()
    middle = gather(fixture)

    reopened = hosts.open_instance(fixture, components)
    try:
        case.proceed(reopened, repeat_steps, repeat=True)
        repeat_calls = list(reopened.runner.calls)
    finally:
        reopened.close()
    final = gather(fixture)
    imports["origins_after"] = support.import_origins()
    leaks = support.origin_problems(imports["origins_after"], imports["expect_under"], imports["forbid_under"])

    effects = {"model_calls": calls, "model_propagations": calls.count("propagation"),
               "table_commits_added": len(middle["git"]["operations"]["table_history"]) - len(before["git"]["operations"]["table_history"]),
               "context_commits_added": middle["git"]["context"]["commits"] - before["git"]["context"]["commits"],
               "document_versions_added": middle["counts"].get("qbdv_document_versions", 0) - before["counts"].get("qbdv_document_versions", 0),
               "receipts_applied": sum(1 for item in middle["state"].get("apply_receipts", {}).values() if item["state"] == "applied")
               - sum(1 for item in before["state"].get("apply_receipts", {}).values() if item["state"] == "applied")}
    checks = case.expectations(args.variant, before, middle, effects, support.jsonable(steps))
    checks("imports stayed on the intended code after the run", not leaks, problems=leaks)
    checks("repeating the completed continuation after closing and reopening the runtime and database changed nothing "
           "(same interpreter process)", final["digest"] == middle["digest"] and final["git"] == middle["git"],
           first=middle["digest"], repeated=final["digest"])
    checks("the repeat made no model call", repeat_calls == [], calls=repeat_calls)

    comparable = comparable_outcome(case, before, middle, effects, steps, repeat_steps, applied, checks)
    evidence.update(variant_applied=applied, steps=support.jsonable(steps), repeat_steps=support.jsonable(repeat_steps),
                    effects=effects, before=before, after=middle, after_repeat_digest=final["digest"], checks=list(checks),
                    failed=checks.failed, comparable=comparable, comparable_digest=support.digest(comparable),
                    result="pass" if not checks.failed else "fail", finished_at=now())
    return evidence, (0 if not checks.failed else 1)


def evidence_path(args, label) -> Path:
    return layout(args)["evidence"] / label / f"{args.case}-{args.variant}.json"


def command_resume(args, *, control=False) -> int:
    label = args.label or ("baseline-control" if control else "candidate")
    paths = layout(args)
    checkpoint = paths["checkpoints"] / args.case
    record = support.read_json(checkpoint / "closed.json")
    status, evidence = 0, None
    try:
        evidence, status = run_continuation(args, label=label)
    finally:
        if control and not args.keep:
            fixture = paths["fixtures"] / args.case
            support.restore_tree(Path(record["archive"]), fixture, within=paths["fixtures"], expect=record)
            if evidence is not None:
                evidence["restored_closed_checkpoint"] = {"fixture": str(fixture), "manifest_sha256": record["manifest_sha256"]}
    if evidence is not None:
        support.write_json(evidence_path(args, label), evidence)
        failed = evidence.get("failed", [])
        print(json.dumps({"label": label, "case": args.case, "variant": args.variant, "result": evidence["result"],
                          "failed": failed, "evidence": str(evidence_path(args, label)),
                          "comparable_digest": evidence.get("comparable_digest")}, indent=2))
    return status


def command_restore(args) -> int:
    paths = layout(args)
    record = support.read_json(paths["checkpoints"] / args.case / "closed.json")
    result = support.restore_tree(Path(record["archive"]), paths["fixtures"] / args.case, within=paths["fixtures"], expect=record)
    print(json.dumps(result, indent=2))
    return 0


# -- comparison --------------------------------------------------------------------------------------------------------------

def difference(left, right, path="") -> list:
    if isinstance(left, dict) and isinstance(right, dict):
        found = []
        for key in sorted(set(left) | set(right)):
            if key not in left or key not in right:
                found.append(f"{path}/{key}: only in {'candidate' if key in right else 'baseline'}")
            else:
                found.extend(difference(left[key], right[key], f"{path}/{key}"))
        return found
    if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
        found = []
        for index, (a, b) in enumerate(zip(left, right)):
            found.extend(difference(a, b, f"{path}[{index}]"))
        return found
    return [] if left == right else [f"{path}: baseline={json.dumps(left, default=str)[:160]} candidate={json.dumps(right, default=str)[:160]}"]


def command_check(args) -> int:
    paths = layout(args)
    base_dir, candidate_dir = paths["evidence"] / args.baseline_label, paths["evidence"] / args.candidate_label
    names = sorted(item.name for item in base_dir.glob("*.json"))
    if args.case:
        names = [name for name in names if name.startswith(args.case + "-")]
    if not names:
        raise Refused(f"no baseline evidence in {base_dir}")
    report, failures = [], 0
    for name in names:
        baseline = support.read_json(base_dir / name)
        problems = []
        if not (candidate_dir / name).is_file():
            problems.append("no candidate evidence")
        else:
            candidate = support.read_json(candidate_dir / name)
            if candidate["result"] != "pass":
                problems.append(f"candidate result is {candidate['result']}: {candidate.get('failed')}")
            if baseline["result"] != "pass":
                problems.append(f"baseline control result is {baseline['result']}: {baseline.get('failed')}")
            if candidate["checkpoint"]["manifest_sha256"] != baseline["checkpoint"]["manifest_sha256"]:
                problems.append("the two runs did not start from the same closed checkpoint")
            if candidate["imports"]["problems"]:
                problems.append(f"candidate import problems: {candidate['imports']['problems']}")
            if not args.self_test:
                if not candidate["imports"]["forbid_under"]:
                    problems.append("the candidate run declared no forbidden (baseline) import roots")
                if candidate["imports"]["origins"] == baseline["imports"]["origins"]:
                    problems.append("the candidate imported exactly what the baseline imported")
            if candidate["comparable_digest"] != baseline["comparable_digest"]:
                problems.extend(["outcome differs: " + item for item in difference(baseline["comparable"], candidate["comparable"])][:40])
        failures += bool(problems)
        report.append({"evidence": name, "equivalent": not problems, "problems": problems})
    print(json.dumps({"baseline": args.baseline_label, "candidate": args.candidate_label, "self_test": args.self_test,
                      "compared": report}, indent=2))
    return 0 if not failures else 1


def command_info(args) -> int:
    roots, components = selected_imports(args)
    imports = guard_imports(args, roots, label="info") if roots else {"origins": support.import_origins()}
    print(json.dumps({"environment": environment(), "imports": imports, "source_pins": source_pins(roots, components)}, indent=2, default=str))
    return 0


# -- command line ------------------------------------------------------------------------------------------------------------

def parser() -> argparse.ArgumentParser:
    main = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = main.add_subparsers(dest="command", required=True)

    def common(sub, *, case=True, variant=False, imports=True, artifacts=True):
        if artifacts:
            sub.add_argument("--artifacts", required=True, help="the resumption artifacts directory (fixtures/, checkpoints/, evidence/)")
        if case:
            sub.add_argument("--case", required=True, choices=CASE_IDS)
        if variant:
            sub.add_argument("--variant", default="none", choices=("none", "revoked", "conflict"))
        if imports:
            sub.add_argument("--baseline-checkout", help="stock qorqut-code layout: fills --code-path and --component")
            sub.add_argument("--code-path", action="append", help="import root placed first on sys.path (repeatable)")
            sub.add_argument("--component", action="append", help="NAME=DIR code location stored in InstancePaths (repeatable)")
            sub.add_argument("--expect-origin-under", action="append", help="every tracked package must come from below this root")
            sub.add_argument("--forbid-origin-under", action="append", help="no tracked package may come from below this root")

    capture = commands.add_parser("capture", help="baseline: create and close the pending-work checkpoint")
    common(capture)
    capture.add_argument("--tests-dir", help="the baseline checkout's app/tests (real stock-host fixtures)")
    capture.add_argument("--replace", action="store_true", help="delete an existing fixture and checkpoint of the case first")
    control = commands.add_parser("control", help="baseline continuation control; restores the closed checkpoint afterwards")
    common(control, variant=True)
    control.add_argument("--label", help="evidence label (default baseline-control)")
    control.add_argument("--keep", action="store_true", help="leave the continued state in place (no restore)")
    resume = commands.add_parser("resume", help="continue the closed checkpoint with the selected (candidate) code")
    common(resume, variant=True)
    resume.add_argument("--label", help="evidence label (default candidate)")
    restore = commands.add_parser("restore", help="restore the closed checkpoint at the same fixture root")
    common(restore, imports=False)
    check = commands.add_parser("check", help="compare candidate evidence with the baseline control evidence")
    common(check, case=False, imports=False)
    check.add_argument("--case", choices=CASE_IDS)
    check.add_argument("--baseline-label", default="baseline-control")
    check.add_argument("--candidate-label", default="candidate")
    check.add_argument("--self-test", action="store_true",
                       help="compare two baseline runs (reproducibility of the evidence): skips the candidate import-origin rules")
    info = commands.add_parser("info", help="show the interpreter's import origins and source pins")
    common(info, case=False, artifacts=False)
    return main


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "capture":
            return command_capture(args)
        if args.command == "control":
            return command_resume(args, control=True)
        if args.command == "resume":
            return command_resume(args)
        if args.command == "restore":
            return command_restore(args)
        if args.command == "check":
            return command_check(args)
        return command_info(args)
    except Refused as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

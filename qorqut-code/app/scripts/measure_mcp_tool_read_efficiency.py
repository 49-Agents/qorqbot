#!/usr/bin/env python3
"""Paired offline MCP read-sequence measurement, not a live outcome/billing eval.

Collect real domain outputs in a temporary, hermetic service ONCE. Replay the
same corpus through each checkout's presentation/finalization/serialization.
No companion repos, production records, providers, or character-token fallback.
Requires the service, test and benchmark dependency extras.
"""
from __future__ import annotations

import argparse
from functools import lru_cache
import hashlib
import importlib.metadata
import inspect
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

TOKENIZER_VERSION = "0.14.0"
ENCODING = "cl100k_base"
WEIGHTS = {"ordinary_triage": .35, "task_work": .20, "inbox_processing": .15,
           "business_entry": .15, "evidence_heavy_review": .15}


@lru_cache(maxsize=1)
def tokenizer():
    try:
        import tiktoken
        version = importlib.metadata.version("tiktoken")
        if version != TOKENIZER_VERSION:
            raise RuntimeError(f"Expected tiktoken=={TOKENIZER_VERSION}; found {version}")
        return tiktoken.get_encoding(ENCODING)
    except Exception as exc:
        raise RuntimeError("Token measurement unavailable; install the pinned benchmark extra. "
                           "No character estimate will be substituted.") from exc


def tokens(text):
    # Tool results contain literal authored text, not model control tokens.
    return len(tokenizer().encode(text, disallowed_special=()))


def packed(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True)


def collect(repo):
    sys.path[:0] = [str(Path(repo).resolve()), str(Path(repo).resolve() / "tests")]
    from starlette.testclient import TestClient
    from mcp_efficiency_support import isolated_service, seed_reads

    with TemporaryDirectory(prefix="mcp-read-measure-") as directory:
        with isolated_service(Path(directory)) as service:
            records = seed_reads(service)
            with TestClient(service.web) as client:
                headers = {name: service.initialize(client, principal) for name, principal in (
                    ("coordinator", service.ordu), ("manager", service.agent), ("recipient", service.agent))}
                cases = {}

                def read(key, operation, arguments, *, actor="coordinator", detail="summary", error=False):
                    value = service.tool(client, headers[actor], operation,
                        {**arguments, "response_detail": "full"})["decoded"]
                    assert value["status"] == ("error" if error else "ok"), value
                    cases[key] = {"operation": operation, "value": value, "arguments": arguments,
                                  "after_detail": detail}
                    return value.get("data", {})

                focus = []
                offset = 0
                while True:
                    key = f"focus_page_{len(focus) + 1}"
                    page = read(key, "focus_get", {"scope": "global", "limit": 2, "offset": offset})
                    focus.append(key)
                    offset = page["next_offset"]
                    if offset is None:
                        break
                activity, cursor = [], None
                while True:
                    key = f"activity_page_{len(activity) + 1}"
                    arguments = {"limit": 1, **({"cursor": cursor} if cursor else {})}
                    page = read(key, "agents_activity_list", arguments, actor="manager")
                    activity.append(key)
                    cursor = page["next_cursor"]
                    if cursor is None:
                        break
                target = {"scope": "global", "item_id": records["cards"][-1]["id"]}
                read("focus_item", "focus_get", target, detail="compact")
                read("evidence_thread", "focus_get", {**target, "conversation": True}, detail="compact")
                assert "thread" in cases["evidence_thread"]["value"]["data"]["item"]
                read("task_work", "tasks_get", {"task_id": records["tasks"][1]["id"]}, actor="manager", detail="compact")
                read("inbox", "inbox_list", {"consumer_id": records["consumer"]}, actor="recipient", detail="compact")
                read("config", "agent_config_get", {}, actor="manager")
                cases["context"] = {**cases["config"], "after_detail": "context", "before_detail": "context"}
                read("missing_task", "tasks_get", {"task_id": "missing-benchmark-task"},
                     actor="manager", detail="compact", error=True)
                service.fixture.update(service.agent, {"assignment_brief": "Validate pilot evidence, then coordinate the approved rollout."})
                read("refresh_config", "agent_config_get", {}, actor="manager")
                assert cases["refresh_config"]["value"]["data"]["effective_config"]["profile_revision"] > cases["config"]["value"]["data"]["effective_config"]["profile_revision"]
                cases["refresh_context"] = {**cases["refresh_config"], "after_detail": "context", "before_detail": "context"}

                descriptors = {}
                for name in ("focus_get", "agents_activity_list", "agent_config_get"):
                    descriptor = service.runtime.registry.get(name)
                    from bazarlyk.contracts import plain
                    descriptors[name] = {"name": name, "input_schema": plain(descriptor.input_schema),
                        "transport_only": descriptor.transport_only, "effect_kind": descriptor.effect_kind,
                        "alias_bindings": plain(descriptor.alias_bindings), "description": descriptor.description}
    # Both projections see exactly these bytes, including generated identities,
    # timestamps and paths. Hash them so a report identifies its precise corpus.
    scenarios = {
        "ordinary_triage": {"before": focus + activity + ["config"],
                            "after": focus + ["focus_item"] + activity + ["config"]},
        "task_work": {"before": ["task_work"], "after": ["task_work"]},
        "inbox_processing": {"before": ["inbox"], "after": ["inbox"]},
        "business_entry": {"before": ["config", "context"], "after": ["config", "context"]},
        "evidence_heavy_review": {"before": focus + ["evidence_thread", "task_work"],
                                  "after": focus + ["focus_item", "evidence_thread", "task_work"]},
        "configuration_change_recovery": {"before": ["refresh_config", "refresh_context"],
                             "after": ["refresh_config", "refresh_context"]},
        "error_then_retry": {"before": ["missing_task", "task_work"], "after": ["missing_task", "task_work"]},
    }
    return {"cases": cases, "descriptors": descriptors, "scenarios": scenarios}


def measure(repo, profile, corpus):
    repo = Path(repo).resolve()
    sys.path.insert(0, str(repo))
    from bazarlyk.transport import mcp_presentation as presentation
    from bazarlyk.transport.mcp import tool_schema
    assert Path(presentation.__file__).resolve().is_relative_to(repo), "Wrong checkout imported"
    accepts_arguments = "arguments" in inspect.signature(presentation.present).parameters
    rows = {}
    for key, case in corpus["cases"].items():
        detail = case["after_detail"] if profile == "phase1" else case.get("before_detail", "compact")
        kwargs = {"arguments": case["arguments"]} if accepts_arguments else {}
        value = presentation.present(case["operation"], case["value"], detail, **kwargs)
        text = presentation.serialize(presentation.finalize(value))
        wire = {"content": [{"type": "text", "text": text}], "isError": value["status"] == "error"}
        arguments = {**case["arguments"], **({"response_detail": detail} if detail != "compact" else {})}
        rows[key] = {"operation": case["operation"], "detail": detail,
            "characters": len(text), "tokens": tokens(text), "mcp_result_tokens": tokens(packed(wire)),
            "request_tokens": tokens(packed({"name": case["operation"], "arguments": arguments}))}
    descriptors = {key: SimpleNamespace(**value) for key, value in corpus["descriptors"].items()}
    registry = SimpleNamespace(get=lambda name: descriptors[name])
    definitions = [{"name": name, "description": descriptor.description + presentation.description(name),
                    "inputSchema": tool_schema(registry, name)} for name, descriptor in descriptors.items()]
    return {"measurements": rows, "three_tool_definition_tokens": tokens(packed(definitions)),
        "source": source_identity(repo)}


def source_identity(repo):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()
    files = ("bazarlyk/transport/mcp.py", "bazarlyk/transport/mcp_presentation.py")
    return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain")),
            "sha256": {path: hashlib.sha256((Path(repo) / path).read_bytes()).hexdigest() for path in files}}


def compare(before, after, corpus):
    scenarios = {}
    for name, calls in corpus["scenarios"].items():
        row = {"before_calls": calls["before"], "after_calls": calls["after"]}
        for side, result in (("before", before), ("after", after)):
            row[side + "_tokens"] = sum(result["measurements"][key]["tokens"] for key in calls[side])
            row[side + "_request_tokens"] = sum(result["measurements"][key]["request_tokens"] for key in calls[side])
        row["reduction_percent"] = round(100 * (1 - row["after_tokens"] / row["before_tokens"]), 2)
        scenarios[name] = row
    weighted = {side: sum(weight * scenarios[name][side + "_tokens"] for name, weight in WEIGHTS.items())
                for side in ("before", "after")}
    reduction = 100 * (1 - weighted["after"] / weighted["before"])
    with_requests = {side: weighted[side] + sum(weight * scenarios[name][side + "_request_tokens"]
                    for name, weight in WEIGHTS.items()) for side in ("before", "after")}
    return {"before": before, "after": after, "scenarios": scenarios, "frozen_workflow_weights": WEIGHTS,
        "weighted_workflow": {"before_tokens": round(weighted["before"], 2),
            "after_tokens": round(weighted["after"], 2), "reduction_percent": round(reduction, 2)},
        "weighted_including_requests": {"before_tokens": round(with_requests["before"], 2),
            "after_tokens": round(with_requests["after"], 2),
            "reduction_percent": round(100 * (1 - with_requests["after"] / with_requests["before"]), 2)},
        "three_tool_definition_delta_tokens": after["three_tool_definition_tokens"] - before["three_tool_definition_tokens"],
        "target_percent": 70, "offline_target_met": reduction >= 70, "production_target_validated": False,
        "corpus_sha256": hashlib.sha256(packed(corpus).encode()).hexdigest(),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "tokenizer": {"package": "tiktoken", "version": TOKENIZER_VERSION, "encoding": ENCODING},
        "limitations": ["Simulated complete READ sequences, not agent decisions or completed business outcomes.",
            "Small synthetic business with real domain outputs; weights are assumptions, not traffic measurements.",
            "Extra detail reads, all pages, mandatory context and the explicit retry are counted; unknown live retries are not modeled.",
            "Configuration-change and error recovery are separate stress cases, excluded from the frozen five-scenario weighting.",
            "Configuration change is a real profile update, not a simulation of hook delivery acknowledgments or the refresh flag.",
            "Result text tokens are the primary metric; MCP wrapper and request tokens are reported separately.",
            "Only the three changed tool definitions are measured; provider prompt framing, caches, and billing are not modeled."]}


def worker(*args, input=None):
    return json.loads(subprocess.check_output([sys.executable, str(Path(__file__).resolve()), *args],
                                             input=input, text=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--profile", choices=("baseline", "phase1"), default="phase1")
    parser.add_argument("--compare", action="store_true")
    parser.add_argument("--before-repo")
    parser.add_argument("--after-repo")
    parser.add_argument("--corpus", type=Path, help="Replay a saved corpus instead of generating a new temporary business")
    parser.add_argument("--save-corpus", type=Path, help="Save generated, synthetic responses for exact replay; refuses overwrite")
    parser.add_argument("--collect", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--measure", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    tokenizer()  # Fail before doing any fixture work, never silently estimate.
    if args.collect:
        result = collect(args.repo)
    elif args.measure:
        result = measure(args.repo, args.profile, json.load(sys.stdin))
    elif args.compare:
        if not args.before_repo or not args.after_repo:
            parser.error("--compare requires --before-repo and --after-repo")
        corpus = json.loads(args.corpus.read_text()) if args.corpus else worker("--collect", "--repo", args.after_repo)
        if args.save_corpus:
            with args.save_corpus.open("x") as output:
                output.write(packed(corpus) + "\n")
        before = worker("--measure", "--repo", args.before_repo, "--profile", "baseline", input=packed(corpus))
        after = worker("--measure", "--repo", args.after_repo, "--profile", "phase1", input=packed(corpus))
        result = compare(before, after, corpus)
    else:
        corpus = worker("--collect", "--repo", args.repo)
        result = worker("--measure", "--repo", args.repo, "--profile", args.profile, input=packed(corpus))
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

import json
import unittest

from qorqut.experience import tool_calls as tc


def use(tool_id, name, inp, at="2026-11-20T10:00:00.000Z", side=False):
    return {"type": "assistant", "timestamp": at, "isSidechain": side, "message": {"role": "assistant", "content": [
        {"type": "text", "text": "x"}, {"type": "tool_use", "id": tool_id, "name": name, "input": inp}]}}


def result(tool_id, content="ok", is_error=False, extra=None):
    record = {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_id, "content": content, "is_error": is_error}]}}
    if extra is not None:
        record["toolUseResult"] = extra
    return record


def human(text="hello", **flags):
    return dict({"type": "user", "message": {"role": "user", "content": text}}, **flags)


def fake(tool, arg="a", ok=True, segment=0, sidechain=False, shape=None, at=None, index=0):
    return {"index": index, "at": at, "tool": tool, "operation": "other", "shape": shape or "other:" + tool,
            "arg": arg, "ok": ok, "error": None if ok else "boom", "interrupted": False,
            "segment": segment, "sidechain": sidechain}


def numbered(items):
    for i, item in enumerate(items):
        item["index"] = i
    return items


class ClaudeParsing(unittest.TestCase):
    def test_ok_error_interrupt_and_missing_result(self):
        records = [
            use("1", "Bash", {"command": "ls"}), result("1", [{"type": "text", "text": "a"}]),
            use("2", "Bash", {"command": "false"}), result("2", "  bad \n thing ", True),
            use("3", "Bash", {"command": "sleep 9"}), result("3", "", False, {"interrupted": True, "stderr": ""}),
            use("4", "Bash", {"command": "pwd"}),
            use("5", "Bash", {"command": "x"}), result("5", "", True, {"stderr": "from stderr"}),
        ]
        found = tc.calls(records)
        self.assertEqual([c["ok"] for c in found], [True, False, False, None, False])
        self.assertEqual([c["index"] for c in found], [0, 1, 2, 3, 4])
        self.assertIsNone(found[0]["error"])
        self.assertEqual(found[1]["error"], "bad thing")
        self.assertTrue(found[2]["interrupted"])
        self.assertEqual(found[2]["error"], "interrupted")
        self.assertIsNone(found[3]["error"])
        self.assertEqual(found[4]["error"], "from stderr")
        self.assertEqual(found[0]["at"], "2026-11-20T10:00:00.000Z")

    def test_sidechain_and_segments(self):
        records = [
            use("1", "Read", {"file_path": "/a"}), result("1"),
            human("one"),
            use("2", "Read", {"file_path": "/b"}), result("2"),
            human("meta", isMeta=True),
            human("side", isSidechain=True),
            use("3", "Read", {"file_path": "/c"}, side=True),
            {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "list text"}]}},
            use("4", "Read", {"file_path": "/d"}),
            {"type": "summary"}, "junk", 5, {"type": "user", "message": 3},
        ]
        found = tc.calls(records)
        self.assertEqual([c["segment"] for c in found], [0, 1, 1, 2])
        self.assertEqual([c["sidechain"] for c in found], [False, False, True, False])

    def test_garbage_never_raises(self):
        self.assertEqual(tc.calls(None), [])
        self.assertEqual(tc.calls([{"type": "assistant", "message": {"content": [{"type": "tool_use"}]}}]), [])
        self.assertEqual(tc.calls([{"payload": 3}, None], provider="codex"), [])


class CodexParsing(unittest.TestCase):
    def test_calls_results_and_segments(self):
        def item(payload, at="t"):
            return {"timestamp": at, "type": "response_item", "payload": payload}
        records = [
            {"type": "event_msg", "payload": {"type": "user_message", "message": "go"}},
            item({"type": "function_call", "name": "exec_command", "arguments": json.dumps({"cmd": "git status"}),
                  "call_id": "c1"}),
            item({"type": "function_call_output", "call_id": "c1",
                  "output": json.dumps({"output": "nope", "metadata": {"exit_code": 1}})}),
            item({"type": "function_call", "name": "exec_command", "arguments": "{bad", "call_id": "c2"}),
            item({"type": "function_call_output", "call_id": "c2", "output": {"content": "fine", "success": True}}),
            item({"type": "custom_tool_call", "name": "apply_patch", "input": "*** patch", "call_id": "c3"}),
            item({"type": "custom_tool_call_output", "call_id": "c3", "output": {"content": "bad", "success": False}}),
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "UserMessage"}}},
            item({"type": "local_shell_call", "action": {"command": ["ls", "-la"]}, "call_id": "c4"}),
        ]
        found = tc.calls(records, provider="codex")
        self.assertEqual([c["ok"] for c in found], [False, True, False, None])
        self.assertEqual(found[0]["error"], "nope")
        self.assertEqual(found[0]["shape"], "execute:git status")
        self.assertEqual(found[0]["segment"], 1)
        self.assertEqual(found[1]["operation"], "execute")
        self.assertEqual(found[2]["operation"], "edit")
        self.assertEqual(found[3]["segment"], 2)
        self.assertEqual(found[3]["arg"], "ls -la")


class OperationAndShape(unittest.TestCase):
    def one(self, name, inp):
        return tc.calls([use("1", name, inp)])[0]

    def test_bash(self):
        self.assertEqual(self.one("Bash", {"command": "git status -s"})["shape"], "execute:git status")
        self.assertEqual(self.one("Bash", {"command": "git -C x log"})["shape"], "execute:git x")
        self.assertEqual(self.one("Bash", {"command": "ls -la /tmp"})["shape"], "execute:ls")
        self.assertEqual(self.one("Bash", {"command": "cd /a && make"})["shape"], "execute:cd+")
        self.assertEqual(self.one("Bash", {"command": "cat f | wc"})["shape"], "execute:cat+")
        self.assertEqual(self.one("Bash", {"command": "echo 'unclosed"})["shape"], "execute:?")
        self.assertEqual(self.one("Bash", {})["shape"], "execute:?")
        self.assertEqual(self.one("Bash", {"command": "uv pip install x"})["shape"], "execute:uv pip")

    def test_operations(self):
        expected = {"Read": "read", "Grep": "search", "Glob": "list", "Write": "write", "MultiEdit": "edit",
                    "WebFetch": "web_fetch", "mcp__qorqut__tasks_get": "read", "mcp__qorqut__tasks_list": "list",
                    "mcp__qorqut__task_create": "write", "mcp__x__updateTask": "edit",
                    "mcp__x__run_it": "execute", "mcp__x__browse_page": "web_fetch", "mcp__x__ping": "other",
                    "mcp__x__find_things": "search"}
        for name, operation in expected.items():
            self.assertEqual(tc.calls([use("1", name, {})])[0]["operation"], operation, name)

    def test_other_shape_and_arg(self):
        found = self.one("Read", {"file_path": "/a/b", "offset": 1, "limit": 2})
        self.assertEqual(found["shape"], "read:file_path,limit,offset")
        self.assertEqual(found["arg"], "/a/b")
        self.assertEqual(self.one("Grep", {"pattern": "foo", "path": "src"})["arg"], "foo src")
        self.assertEqual(self.one("Agent", {"description": "dig", "prompt": "long"})["arg"], "dig")
        mcp = self.one("mcp__qorqut__tasks_get", {"b": 1, "a": "x"})
        self.assertEqual(mcp["arg"], '{"a":"x","b":1}')
        self.assertEqual(mcp["shape"], "read:a,b")
        self.assertEqual(len(self.one("Bash", {"command": "x " * 500})["arg"]), 160)
        many = self.one("mcp__x__ping", {"k%02d" % i: 1 for i in range(20)})
        self.assertEqual(len(many["shape"].split(":")[1].split(",")), 12)


class Redaction(unittest.TestCase):
    def test_arg_and_error(self):
        cmd = "curl -H 'Authorization: Bearer abcdefghijklmnop1234' -d password=hunter2 sk-abcdefghijkl1234 ghp_aaaaaaaaaaaaaaaa"
        found = tc.calls([use("1", "Bash", {"command": cmd}), result("1", 'bad token: "s3 cret" api_key=zzz', True)])
        arg, error = found[0]["arg"], found[0]["error"]
        for secret in ("abcdefghijklmnop1234", "hunter2", "sk-abcdefghijkl1234", "ghp_aaaaaaaaaaaaaaaa"):
            self.assertNotIn(secret, arg)
        self.assertIn("password=[REDACTED]", arg)
        self.assertNotIn("s3 cret", error)
        self.assertNotIn("zzz", error)
        self.assertIn("[REDACTED]", error)

    def test_json_key(self):
        found = tc.calls([use("1", "mcp__x__set", {"password": "hunter2", "name": "n"})])
        self.assertNotIn("hunter2", found[0]["arg"])


class Problems(unittest.TestCase):
    def test_retried_recovered_streak(self):
        found = numbered([
            fake("A", ok=False), fake("A", ok=False), fake("A", ok=True),
            fake("B", ok=False), fake("C", ok=True), fake("B", ok=True, segment=1),
            fake("D", ok=False, segment=1), fake("D", ok=None, segment=1),
        ])
        found[0]["at"] = "t0"
        got = tc.problems(found)
        self.assertEqual([p["index"] for p in got], [0, 1, 3, 6])
        self.assertEqual([p["retried"] for p in got], [True, True, False, True])
        self.assertEqual([p["recovered"] for p in got], [True, True, False, False])
        self.assertEqual([p["streak"] for p in got], [2, 2, 1, 1])
        self.assertEqual(got[0]["at"], "t0")
        self.assertEqual(set(got[0]), {"index", "at", "tool", "arg", "error", "interrupted", "retried", "recovered", "streak"})

    def test_none_result_is_not_a_problem(self):
        self.assertEqual(tc.problems([fake("A", ok=None)]), [])


class Signature(unittest.TestCase):
    def test_stable(self):
        a = tc.error_signature("Read", "File not found: /home/a/x.py line 12 id 3f2a9c1de5b0 at 2026-11-20T10:00:00Z 'foo'")
        b = tc.error_signature("Read", "File not found: /srv/b/y.py line 99 id 9d8e7f6a5b4c at 2026-01-02T03:04:05Z 'bar'")
        self.assertEqual(a, b)
        self.assertEqual(a, a.lower())
        self.assertTrue(a.startswith("read: file not found"))
        self.assertNotEqual(a, tc.error_signature("Write", "File not found"))
        self.assertLessEqual(len(tc.error_signature("T", "x" * 500)), 120)
        self.assertEqual(tc.error_signature("T", None), "t: ")

    def test_uuid(self):
        one = tc.error_signature("T", "no 123e4567-e89b-12d3-a456-426614174000")
        two = tc.error_signature("T", "no 223e4567-e89b-12d3-a456-426614174999")
        self.assertEqual(one, two)


class Grouping(unittest.TestCase):
    def test_group(self):
        def prob(tool, error, at, recovered=False):
            return {"tool": tool, "error": error, "at": at, "arg": "a", "recovered": recovered}
        sessions = [
            {"key": "s1", "agent": "zed", "problems": [prob("Read", "missing /a/1", "2026-01-02"),
                                                       prob("Read", "missing /b/2", "2026-01-01", True)]},
            {"key": "s2", "agent": "amy", "problems": [prob("Read", "missing /c/3", None, True),
                                                       prob("Bash", "exit 1", "2026-01-05")]},
        ]
        got = tc.group_problems(sessions)
        self.assertEqual([g["tool"] for g in got], ["Read", "Bash"])
        first = got[0]
        self.assertEqual((first["count"], first["sessions"], first["recovered"]), (3, 2, 2))
        self.assertEqual(first["agents"], ["amy", "zed"])
        self.assertEqual((first["first_at"], first["last_at"]), ("2026-01-01", "2026-01-02"))
        self.assertEqual(len(first["examples"]), 3)
        self.assertEqual(first["examples"][0]["session"], "s1")
        self.assertEqual(tc.group_problems([]), [])


class Repeats(unittest.TestCase):
    def test_runs(self):
        found = numbered([
            fake("A", "x"), fake("A", "x", ok=False), fake("A", "x", ok=False), fake("A", "y"),
            fake("B", "q"), fake("B", "q"), fake("B", "q", segment=1),
            fake("C", "z"), fake("C", "z"), fake("C", "z"), fake("C", "z"),
        ])
        got = tc.repeats(found)
        self.assertEqual([(r["tool"], r["count"], r["first_index"], r["failed"]) for r in got],
                         [("A", 3, 0, 2), ("C", 4, 7, 0)])
        self.assertEqual(len(tc.repeats(found, minimum=2)), 3)


def session(key, tools, agent="a", segments=None, sidechains=None):
    items = [fake(t, arg=t + str(i), shape="s:" + t, segment=(segments or {}).get(i, 0),
                  sidechain=i in (sidechains or ())) for i, t in enumerate(tools)]
    return {"key": key, "agent": agent, "calls": numbered(items)}


def tool_patterns(got):
    return [(p["sequence"], p["occurrences"]) for p in got if p["projection"] == "tool"]


class Patterns(unittest.TestCase):
    def test_counting_without_overlap(self):
        got = tc.patterns([session("s", list("ABABAB") + ["X"])], min_occurrences=3, max_length=2)
        self.assertEqual(tool_patterns(got), [(["A", "B"], 3)])
        self.assertEqual(tc.patterns([session("s", list("ABABA"))], min_occurrences=3, max_length=2), [])

    def test_not_crossing_segments_or_sidechains(self):
        crossing = session("s", list("ABABAB"), segments={2: 1, 4: 2})
        self.assertEqual(tc.patterns([crossing], min_occurrences=2), [])
        side = session("s", list("ABAB"), sidechains={1})
        self.assertEqual(tool_patterns(tc.patterns([side], min_occurrences=2)), [])

    def test_dominated_and_single_tool_dropped(self):
        sessions = [session("s%d" % i, list("XABCY"), agent="ag%d" % (i % 2)) for i in range(3)]
        got = tc.patterns(sessions, min_occurrences=3)
        self.assertEqual(tool_patterns(got)[0], (["X", "A", "B", "C", "Y"], 3))
        self.assertEqual(len(tool_patterns(got)), 1)
        top = got[0]
        self.assertEqual((top["length"], top["sessions"], top["agents"]), (5, 3, ["ag0", "ag1"]))
        self.assertEqual(top["examples"][0]["args"], ["X0", "A1", "B2", "C3", "Y4"])
        self.assertEqual(len(top["examples"]), 3)
        self.assertEqual(tc.patterns([session("s", list("AAAAAA"))], min_occurrences=2), [])

    def test_shorter_kept_when_more_frequent(self):
        sessions = [session("s1", list("ABC")), session("s2", list("ABC")), session("s3", list("ABD"))]
        got = tool_patterns(tc.patterns(sessions, min_occurrences=2))
        self.assertIn((["A", "B"], 3), got)
        self.assertIn((["A", "B", "C"], 2), got)
        self.assertNotIn((["B", "C"], 2), got)

    def test_order_limit_and_determinism(self):
        sessions = [session("s%d" % i, list("PQRSTQ")) for i in range(4)] + [session("t", list("MN") * 3)]
        first = tc.patterns(sessions, min_occurrences=3)
        self.assertEqual(first, tc.patterns(list(sessions), min_occurrences=3))
        scores = [p["occurrences"] * p["length"] for p in first]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(len(tc.patterns(sessions, min_occurrences=3, limit=1)), 1)
        self.assertEqual({p["projection"] for p in first}, {"tool", "shape"})


if __name__ == "__main__":
    unittest.main()

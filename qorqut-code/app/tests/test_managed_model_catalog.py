"""The model lists the clients report, in the catalog shape (``managed_runtime.model_catalog``). The shapes below are what
Claude Code 2.1.329 (``initialize``) and Codex 0.167.1 (``model/list``) answered."""

import unittest

from qorqut.managed_runtime import model_catalog


class Shapes(unittest.TestCase):
    def test_claude_models_keep_their_names_efforts_and_mark_pinned_versions_older(self):
        shaped = model_catalog.claude_models([
            {"value": "default", "displayName": "Default (recommended)", "description": "Opus 5.5 · Best for everyday, complex tasks",
             "supportsEffort": True, "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"]},
            {"value": "sonnet", "displayName": "Sonnet 5.5", "description": "Most efficient", "supportsEffort": True,
             "supportedEffortLevels": ["low", "high", "bogus"]},
            {"value": "claude-haiku-4-5-20251001", "displayName": "Haiku 4.5", "description": "Fastest"},
            {"displayName": "no value"}, "junk"])
        self.assertEqual([(m["id"], m["default"], m["older"], m["efforts"]) for m in shaped], [
            ("default", True, False, ["low", "medium", "high", "xhigh", "max"]), ("sonnet", False, False, ["low", "high"]),
            ("claude-haiku-4-5-20251001", False, True, [])])
        self.assertEqual(shaped[0]["label"], "Default (recommended)")

    def test_claude_aliases_name_the_full_model_they_stand_for(self):
        shaped = model_catalog.claude_models([
            {"value": "default", "resolvedModel": "claude-opus-5-5", "displayName": "Default (recommended)"},
            {"value": "opus", "resolvedModel": "claude-opus-5-5", "displayName": "Opus 5.5"},
            {"value": "claude-opus-4-6", "displayName": "Opus 4.6"}])
        self.assertEqual([m["resolved"] for m in shaped], ["claude-opus-5-5", "claude-opus-5-5", None])

    def test_claude_settings_say_which_model_and_effort_are_used_when_nothing_is_chosen(self):
        # Claude Code 2.1.329's get_settings answer, shortened.
        current = model_catalog.claude_current({
            "effective": {"model": "opus", "modelSettings": {"claude-opus-5-5": {"effortLevel": "medium"},
                                                             "claude-fable-5-2": {"effortLevel": "xhigh"},
                                                             "claude-x": {"effortLevel": "bogus"}, "claude-y": "junk"}},
            "applied": {"model": "claude-opus-5-5", "effort": "medium", "advisor": None}})
        self.assertEqual(current, {"model": "claude-opus-5-5", "effort": "medium",
                                   "efforts": {"claude-opus-5-5": "medium", "claude-fable-5-2": "xhigh"}})
        self.assertEqual(model_catalog.claude_current(None), model_catalog.NO_CURRENT)
        self.assertEqual(model_catalog.claude_current({"applied": {"model": 4, "effort": "loud"}}),
                         {"model": None, "effort": None, "efforts": {}})

    def test_codex_models_keep_their_efforts_and_default_and_drop_hidden_ones(self):
        shaped = model_catalog.codex_models([
            {"id": "gpt-6-luna", "displayName": "GPT-6-Luna", "description": "Fast", "isDefault": True, "hidden": False,
             "defaultReasoningEffort": "medium", "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "max"}]},
            {"id": "gpt-5.6-terra", "displayName": "GPT-5.6-Terra", "description": "Legacy", "isDefault": False,
             "defaultReasoningEffort": "medium", "supportedReasoningEfforts": [{"reasoningEffort": "ultra"}]},
            {"id": "secret", "hidden": True, "supportedReasoningEfforts": []}])
        self.assertEqual([(m["id"], m["default"], m["efforts"], m["default_effort"]) for m in shaped], [
            ("gpt-6-luna", True, ["low", "max"], "medium"), ("gpt-5.6-terra", False, ["ultra"], "medium")])

    def test_answers_are_kept_and_a_failure_is_retried_later(self):
        model_catalog.forget()
        self.addCleanup(model_catalog.forget)
        clock, calls = [0.0], []

        def ask(client):
            calls.append(client)
            return None if len(calls) == 1 else {"client": client, "models": [{"id": "x"}]}
        now = lambda: clock[0]
        self.assertIsNone(model_catalog.catalog("codex", ask=ask, now=now))
        self.assertIsNone(model_catalog.catalog("codex", ask=ask, now=now), "a failure is not asked again at once")
        clock[0] += model_catalog.RETRY_SECONDS + 1
        self.assertEqual(model_catalog.catalog("codex", ask=ask, now=now)["models"], [{"id": "x"}])
        clock[0] += 60
        model_catalog.catalog("codex", ask=ask, now=now)
        self.assertEqual(len(calls), 2, "a fresh answer is reused")
        self.assertIsNone(model_catalog.catalog("other", ask=ask, now=now))


if __name__ == "__main__":
    unittest.main()

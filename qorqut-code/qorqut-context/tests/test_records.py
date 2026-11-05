import unittest

from qorqut_context import ContextError, normalize_date
from qorqut_context.schema import attributes, change_details, source_path


class RecordRules(unittest.TestCase):
    def test_dates_default_to_earliest_components_and_utc(self):
        cases = {"2026": "2026-01-01T00:00:00Z", "2026-10": "2026-10-01T00:00:00Z",
                 "2026-10-07": "2026-10-07T00:00:00Z", "2026-10-07T14": "2026-10-07T14:00:00Z",
                 "2026-10-07T14:30": "2026-10-07T14:30:00Z",
                 "2026-10-07T14:30:12-04:00": "2026-10-07T18:30:12Z", 0: 0}
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(normalize_date(source), expected)
        for bad in ("2026-02-30", "unknown", None, True, "2026-00", "2026-10-07T99:99"):
            with self.subTest(bad=bad), self.assertRaises(ContextError):
                normalize_date(bad)
        with self.assertRaises(ContextError):
            normalize_date(0, unknown=False)

    def test_lists_are_flat_and_type_aware_and_required_category_survives(self):
        self.assertEqual(attributes({"basic_entity_type": ["person", "person"],
                                     "mixed": [True, 1, "1", True, 1, "1"], "unused": []}, node=True),
                         {"basic_entity_type": ["person"], "mixed": [True, 1, "1"]})
        for value in (None, {}, [], float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ContextError):
                attributes({"field": [value]})
        for attrs in ({}, {"basic_entity_type": []}, {"basic_entity_type": [""]}, {"basic_entity_type": [1]}):
            with self.subTest(attrs=attrs), self.assertRaises(ContextError):
                attributes(attrs, node=True)

    def test_paths_and_change_explanations(self):
        self.assertEqual(source_path("foundation/notes/one.md"), "foundation/notes/one.md")
        for path in ("/tmp/one.md", "foundation/../outside.md", "foundation/a/../../x.txt",
                     "foundation//x.md", "foundation/.git/a.md", "foundation/a.pdf"):
            with self.subTest(path=path), self.assertRaises(ContextError):
                source_path(path)
        with self.assertRaises(ContextError):
            change_details("word " * 151, ["self"], ["self"], [])
        with self.assertRaises(ContextError):
            change_details("Change", [f"{n:040x}" for n in range(16)], ["self"], [])

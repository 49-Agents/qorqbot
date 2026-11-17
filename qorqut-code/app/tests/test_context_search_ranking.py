"""Search returns records holding every word first, most relevant first, then records holding only some."""

from pathlib import Path
import tempfile
import unittest

from qorqut_context.search import SearchRecord, SQLiteIndex

REVISION = "r1"


def record(kind, key, text, **attributes):
    return SearchRecord(kind, key, text, attributes, "digest-" + key)


class SearchRankingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.index = SQLiteIndex(Path(temporary.name) / "search.sqlite3")
        self.index.rebuild(REVISION, [
            # Keys sort a-source first, so key order alone would put the long source ahead of the person.
            record("source", "aaaaa", "Meeting minutes. " + "The team reviewed the quarterly plan in detail. " * 40
                   + "Dana Okoro joined late."),
            record("node", "zzzzz", "Dana Okoro, finance lead", role=["finance lead"]),
            record("node", "mmmmm", "Northwind Supply, the packaging supplier"),
            record("note", "nnnnn", "Okoro asked Northwind for the revised quote."),
            record("node", "ppppp", "The bakery project, at the planning stage"),
            record("node", "qqqqq", "The office lease, the landlord and the renewal date"),
        ])

    def ids(self, query, **options):
        hits, match = self.index.matched(REVISION, query, **options)
        return [hit.id for hit in hits], match

    def test_the_short_record_about_a_name_outranks_a_long_text_that_mentions_it(self):
        ids, match = self.ids("Okoro")
        self.assertEqual(match, "all")
        self.assertEqual(ids[0], "zzzzz")
        self.assertEqual(set(ids), {"zzzzz", "aaaaa", "nnnnn"})

    def test_records_holding_every_word_come_before_partial_matches(self):
        ids, match = self.ids("Okoro Northwind")
        self.assertEqual(match, "all")
        self.assertEqual(ids[0], "nnnnn")
        self.assertEqual(set(ids[1:]), {"zzzzz", "mmmmm", "aaaaa"})
        self.assertEqual(len(ids), len(set(ids)))

    def test_a_question_no_record_fully_matches_returns_records_with_any_word(self):
        ids, match = self.ids("who is Dana Okoro at the company")
        self.assertEqual(match, "any")
        self.assertEqual(ids[0], "zzzzz")

    def test_a_single_word_that_matches_nothing_stays_empty(self):
        self.assertEqual(self.ids("Zephyr"), ([], "all"))

    def test_nothing_matching_any_word_is_empty(self):
        self.assertEqual(self.ids("Zephyr Quill"), ([], "any"))

    def test_empty_query_browses_in_key_order(self):
        ids, match = self.ids("")
        self.assertEqual((ids, match), (["mmmmm", "ppppp", "qqqqq", "zzzzz", "nnnnn", "aaaaa"], "browse"))

    def test_kind_and_attribute_filters_apply_to_the_fallback(self):
        ids, match = self.ids("Dana bakery", kind="node", attributes={"role": ["finance lead"]})
        self.assertEqual((ids, match), (["zzzzz"], "any"))
        self.assertEqual(self.ids("Dana bakery", kind="note"), ([], "any"))

    def test_paging_is_stable_because_the_mode_ignores_the_limit(self):
        full, mode = self.ids("who is Okoro Northwind bakery", limit=10)
        self.assertEqual(mode, "any")
        for size in range(1, len(full) + 1):
            self.assertEqual(self.ids("who is Okoro Northwind bakery", limit=size), (full[:size], "any"))

    def test_search_returns_the_same_hits_as_matched(self):
        self.assertEqual([hit.id for hit in self.index.search(REVISION, "Okoro")], self.ids("Okoro")[0])


if __name__ == "__main__":
    unittest.main()

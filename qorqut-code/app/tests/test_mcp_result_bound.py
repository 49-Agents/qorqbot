"""A result over the size a harness accepts is cut, and every cut is named in the result."""
import base64
import json
import unittest

from qorqut.transport import mcp_presentation
from qorqut.transport.mcp_presentation import RESULT_CHARACTER_BUDGET, bound, serialize


def rows(count, characters):
    return [{"id": f"row-{index}", "note": "n" * characters} for index in range(count)]


class ResultBoundTests(unittest.TestCase):
    def sent(self, value):
        return len(serialize(mcp_presentation._unprotected(value)))

    def test_a_result_within_the_limit_is_returned_as_it_is(self):
        value = {"status": "ok", "data": {"items": rows(20, 500)}}

        bounded, notice = bound(value)

        self.assertIs(bounded, value)
        self.assertIsNone(notice)

    def test_a_long_list_loses_its_tail_and_says_how_much(self):
        value = {"status": "ok", "data": {"items": rows(100, 2000), "next_cursor": "after-100"}}

        bounded, notice = bound(value, narrowing=("limit", "cursor", "business_id"))

        self.assertLessEqual(self.sent(bounded), RESULT_CHARACTER_BUDGET)
        kept = bounded["data"]["items"]
        self.assertEqual(kept, value["data"]["items"][:len(kept)])
        self.assertGreater(len(kept), 5)
        cut = bounded["result_cut"]
        self.assertEqual(cut["lists"], [{"path": "data.items", "returned": len(kept), "of": 100}])
        self.assertEqual(cut["narrow_with"], ["limit", "cursor"])
        self.assertEqual(cut["retry"], {"limit": len(kept)})
        self.assertEqual(cut["full_characters"], self.sent(value))
        self.assertIn("cut to fit", cut["note"])
        # The notice is read before the shortened data, and the caller's own value is not changed.
        self.assertEqual(list(bounded)[:2], ["status", "result_cut"])
        self.assertEqual(len(value["data"]["items"]), 100)
        self.assertIs(notice, cut)

    def test_one_long_text_loses_its_tail_and_is_marked_where_it_was_cut(self):
        value = {"status": "ok", "data": {"message": {"subject": "Receipt", "body": {"content": "word " * 30_000}}}}

        bounded, _ = bound(value, narrowing=("message_id",))

        self.assertLessEqual(self.sent(bounded), RESULT_CHARACTER_BUDGET)
        content = bounded["data"]["message"]["body"]["content"]
        self.assertTrue(content.startswith("word word"))
        self.assertTrue(content.endswith("[cut: see result_cut]"))
        self.assertEqual(bounded["data"]["message"]["subject"], "Receipt")
        text = bounded["result_cut"]["texts"][0]
        self.assertEqual(text["path"], "data.message.body.content")
        self.assertEqual(text["of_characters"], 150_000)
        self.assertGreater(text["returned_characters"], 30_000)
        self.assertNotIn("narrow_with", bounded["result_cut"])
        self.assertNotIn("retry", bounded["result_cut"])

    def test_a_few_huge_rows_keep_one_row_and_then_shorten_inside_it(self):
        value = {"status": "ok", "data": {"items": [{"id": index, "task": {"notes": rows(40, 3000)}} for index in range(5)],
                                          "unowned": {"tasks": rows(50, 300)}}}

        bounded, _ = bound(value, narrowing=("limit", "projection"))

        self.assertLessEqual(self.sent(bounded), RESULT_CHARACTER_BUDGET)
        self.assertEqual(len(bounded["data"]["items"]), 1)
        paths = {row["path"]: row for row in bounded["result_cut"]["lists"]}
        self.assertEqual(paths["data.items"]["of"], 5)
        self.assertEqual(paths["data.items[0].task.notes"]["of"], 40)
        self.assertEqual(bounded["result_cut"]["narrow_with"], ["limit", "projection"])

    def test_attachment_and_image_bytes_are_never_measured_or_cut(self):
        payload = base64.b64encode(b"x" * 90_000).decode()
        value = {"status": "ok", "data": {"content_base64": payload, "next_offset": 90_000,
                                          "_mcp_images": [{"data": payload, "mime_type": "image/png"}]}}

        bounded, notice = bound(value)

        self.assertIs(bounded, value)
        self.assertIsNone(notice)

    def test_text_detail_is_measured_as_the_text_that_is_sent(self):
        value = {"response_detail": "text", "status": "ok", "data": {"tasks": rows(100, 1500)}}

        bounded, _ = bound(value, narrowing=("limit",))

        sent = serialize(bounded)
        self.assertTrue(sent.startswith("response_detail: text\n"))
        self.assertLessEqual(len(sent), RESULT_CHARACTER_BUDGET)
        self.assertEqual(bounded["result_cut"]["lists"][0]["of"], 100)

    def test_a_key_with_a_dot_in_it_is_still_reported(self):
        value = {"status": "ok", "data": {"rows.v2": rows(100, 2000)}}

        bounded, _ = bound(value)

        self.assertEqual(bounded["result_cut"]["lists"][0]["of"], 100)
        self.assertEqual(bounded["result_cut"]["lists"][0]["returned"], len(bounded["data"]["rows.v2"]))

    def test_many_small_fields_that_cannot_be_shortened_become_an_outline(self):
        value = {"status": "ok", "data": {f"field_{index}": "v" * 300 for index in range(400)}}

        bounded, notice = bound(value, narrowing=("limit",))

        self.assertLessEqual(self.sent(bounded), RESULT_CHARACTER_BUDGET)
        self.assertEqual(bounded["data"], {})
        self.assertEqual(notice["outline"]["field_0"], {"characters": 302})
        self.assertEqual(json.loads(serialize(bounded))["result_cut"]["reason"], "result_too_large")


if __name__ == "__main__":
    unittest.main()

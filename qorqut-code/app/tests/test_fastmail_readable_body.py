"""An agent reads the words of an HTML message, not its markup; the source stays one argument away."""
import importlib.util
from pathlib import Path
import unittest

PATH = Path(__file__).resolve().parents[3] / "extensions" / "qorqut-fastmail" / "qorqut_fastmail" / "readable.py"
spec = importlib.util.spec_from_file_location("qorqut_fastmail_readable", PATH)
readable_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(readable_module)
readable, text_of, is_html = readable_module.readable, readable_module.text_of, readable_module.is_html

HTML = """<!DOCTYPE html><html><head><title>Hidden title</title><style>p{color:red}</style></head>
<body><div style="display:none">Preview&zwnj;&nbsp;&zwnj;&nbsp;</div>
<table><tr><td><img src="https://cdn.example/logo.png" alt="Example  logo"><img src="spacer.gif"></td></tr>
<tr><td><h1>Your booking is confirmed</h1><p>Table for 2 &amp; a corner seat,<br>Friday at 8:30&nbsp;PM.</p>
<p><a href="https://example.com/manage?token=abc">Manage your booking</a> or write to
<a href="mailto:help@example.com">help@example.com</a>. <a href="#top">Back to top</a></p>
<ul><li>Bring ID</li><li>Arrive early</li></ul><script>track()</script></td></tr></table></body></html>"""


class ReadableBodyTests(unittest.TestCase):
    def message(self, content, label="Text"):
        # The engine labels an HTML-only message "Text", so the label is not what decides.
        return {"message": {"id": "m1", "subject": "Booking", "bodyTruncated": False, "attachments": [],
                            "body": {"contentType": label, "content": content}}, "expected_state": "s1"}

    def test_html_becomes_its_words_links_and_image_descriptions(self):
        text = text_of(HTML)

        self.assertEqual(text, "\n".join([
            "Preview",
            "",
            "[image: Example logo]",
            "",
            "Your booking is confirmed",
            "",
            "Table for 2 & a corner seat,",
            "Friday at 8:30 PM.",
            "",
            "Manage your booking <https://example.com/manage?token=abc> or write to",
            "help@example.com. Back to top",
            "",
            "- Bring ID",
            "",
            "- Arrive early"]))

    def test_a_message_read_returns_the_text_and_says_the_markup_was_removed(self):
        result = readable(self.message(HTML))

        body = result["message"]["body"]
        self.assertEqual(body["content"], text_of(HTML))
        self.assertEqual(body["contentType"], "Text")
        self.assertEqual((body["markup_removed"], body["source_characters"], body["read_source"]),
                         (True, len(HTML), "body_format=source"))
        self.assertNotIn("<", body["content"].replace("<https://example.com/manage?token=abc>", ""))
        # Everything that is not the body is returned as it was.
        self.assertEqual({key: value for key, value in result["message"].items() if key != "body"},
                         {key: value for key, value in self.message(HTML)["message"].items() if key != "body"})
        self.assertEqual(result["expected_state"], "s1")

    def test_source_format_returns_the_message_untouched(self):
        original = self.message(HTML, "HTML")

        self.assertIs(readable(original, "source"), original)

    def test_plain_text_is_left_alone_even_when_it_mentions_tags(self):
        plain = "Hi,\n\nuse <b>bold</b> or a <p> tag, and 2 < 3.\n\n> quoted line\nThanks"
        original = self.message(plain)

        self.assertFalse(is_html(plain))
        self.assertEqual(readable(original), original)

    def test_every_message_of_a_thread_page_is_converted(self):
        thread = {"thread": {"id": "t1", "messages": [self.message(HTML)["message"], self.message("plain reply")["message"]]}}

        result = readable(thread)

        first, second = result["thread"]["messages"]
        self.assertTrue(first["body"]["markup_removed"])
        self.assertEqual(second["body"], {"contentType": "Text", "content": "plain reply"})


if __name__ == "__main__":
    unittest.main()

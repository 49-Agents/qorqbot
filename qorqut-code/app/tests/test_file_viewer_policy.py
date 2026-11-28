"""The file viewer page's policy lets its PDF frame load: a PDF is shown in Chrome's viewer framed from this origin."""

import unittest

from qorqut.service.web import FILE_VIEWER_POLICY


class FileViewerPolicyTest(unittest.TestCase):
    def directives(self):
        return dict((part.strip().split(" ", 1) + [""])[:2] for part in FILE_VIEWER_POLICY.split(";") if part.strip())

    def test_frames_from_this_origin_are_allowed_so_a_pdf_shows(self):
        self.assertEqual(self.directives()["frame-src"], "'self'")

    def test_everything_else_stays_closed(self):
        policy = self.directives()
        self.assertEqual(policy["default-src"], "'none'")
        self.assertEqual(policy["script-src"], "'self'")
        self.assertEqual(policy["base-uri"], "'none'")
        self.assertEqual(policy["form-action"], "'none'")


if __name__ == "__main__":
    unittest.main()

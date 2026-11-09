"""Attachment bytes survive MCP packaging alongside native browser images."""
import base64
import json
import unittest

from qorqut.transport.mcp import MCPTransport


class ContentBlocksTests(unittest.TestCase):
    def setUp(self):
        self.invitation = b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n"
        self.attachment = {
            "message_id": "message-1", "attachment_id": "attachment-1",
            "total": len(self.invitation), "offset": 0, "next_offset": len(self.invitation),
            "content_base64": base64.b64encode(self.invitation).decode(),
        }
        self.png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
        self.screenshot = {
            "mime_type": "image/png", "encoding": "base64", "content_base64": self.png,
            "_mcp_images": [{"data": self.png, "mime_type": "image/png"}],
        }

    def test_attachment_contents_reach_the_agent(self):
        blocks = MCPTransport._content_blocks({"status": "ok", "data": self.attachment})

        self.assertEqual(len(blocks), 1)
        data = json.loads(blocks[0].text)["data"]
        self.assertEqual(base64.b64decode(data["content_base64"]), self.invitation)
        self.assertEqual(data["next_offset"], len(self.invitation))

    def test_browser_screenshot_is_emitted_once_as_an_image(self):
        blocks = MCPTransport._content_blocks({"status": "ok", "data": self.screenshot})

        self.assertEqual(len(blocks), 2)
        self.assertEqual(json.loads(blocks[0].text)["data"],
                         {"mime_type": "image/png", "encoding": "base64"})
        self.assertEqual(blocks[1].type, "image")
        self.assertEqual(blocks[1].data, self.png)
        self.assertEqual(blocks[1].mimeType, "image/png")

    def test_batch_keeps_attachment_contents_when_another_result_has_an_image(self):
        wire = {"status": "ok", "data": {"results": [
            {"status": "ok", "data": self.attachment},
            {"status": "ok", "data": self.screenshot},
        ]}}

        blocks = MCPTransport._content_blocks(wire)

        self.assertEqual(len(blocks), 2)
        results = json.loads(blocks[0].text)["data"]["results"]
        self.assertEqual(results[0]["data"], self.attachment)
        self.assertNotIn("content_base64", results[1]["data"])
        self.assertEqual(blocks[1].data, self.png)


if __name__ == "__main__":
    unittest.main()

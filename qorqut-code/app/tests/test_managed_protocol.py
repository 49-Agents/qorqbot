"""The managed host's control protocol: framing, request validation and response shapes. No host and no socket server."""
import socket
import unittest
import uuid

from qorqut.managed_runtime import protocol


class FramingTest(unittest.TestCase):
    def test_encode_is_one_line_and_round_trips(self):
        line = protocol.encode({"id": "a", "status": "ok", "text": "café\nline"})
        self.assertTrue(line.endswith(b"\n"))
        self.assertEqual(line.count(b"\n"), 1)
        self.assertEqual(protocol.decode(line)["text"], "café\nline")

    def test_decode_refuses_non_objects_and_garbage(self):
        for bad in (b"[1]", b"7", b"not json", b"\xff\xfe", b'"x"'):
            with self.assertRaises(protocol.ProtocolError) as caught:
                protocol.decode(bad)
            self.assertEqual(caught.exception.code, protocol.ERROR_BAD_REQUEST)

    def test_encode_refuses_an_oversized_message(self):
        with self.assertRaises(protocol.ProtocolError) as caught:
            protocol.encode({"text": "x" * (protocol.MAX_LINE + 1)})
        self.assertEqual(caught.exception.code, protocol.ERROR_TOO_LARGE)

    def test_line_reader_splits_partial_reads_and_ends_cleanly(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        reader = protocol.LineReader(right)
        left.sendall(b'{"a":1}\n{"b"')
        self.assertEqual(reader.readline(), b'{"a":1}')
        left.sendall(b':2}\n')
        self.assertEqual(reader.readline(), b'{"b":2}')
        left.close()
        self.assertIsNone(reader.readline())

    def test_line_reader_refuses_a_line_longer_than_its_limit(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        reader = protocol.LineReader(right, limit=100)
        left.sendall(b"x" * 300)
        with self.assertRaises(protocol.ProtocolError) as caught:
            reader.readline()
        self.assertEqual(caught.exception.code, protocol.ERROR_TOO_LARGE)


class RequestTest(unittest.TestCase):
    def check(self, **message):
        return protocol.validate_request({"id": "r1", "token": "t", **message})

    def refused(self, code=protocol.ERROR_BAD_REQUEST, **message):
        with self.assertRaises(protocol.ProtocolError) as caught:
            self.check(**message)
        self.assertEqual(caught.exception.code, code)

    def test_every_operation_without_arguments(self):
        for op in ("hello", "status", "interrupt"):
            self.assertEqual(self.check(op=op), ("r1", op, {}))

    def test_unknown_operation_and_bad_id(self):
        self.refused(protocol.ERROR_UNKNOWN_OP, op="reboot")
        for bad in (None, "", "has space", "x" * 65, ["a"], True):
            with self.assertRaises(protocol.ProtocolError):
                protocol.validate_request({"id": bad, "op": "hello", "token": "t"})

    def test_send_defaults_and_canonical_uuid(self):
        value = uuid.uuid4()
        _id, op, arguments = self.check(op="send", text="hello", message_uuid=str(value).upper())
        self.assertEqual(arguments, {"text": "hello", "message_uuid": str(value), "steer": False,
                                     "confirm_seconds": protocol.DEFAULT_CONFIRM_SECONDS})

    def test_send_refusals(self):
        good = str(uuid.uuid4())
        self.refused(op="send", text="", message_uuid=good)
        self.refused(op="send", text="   ", message_uuid=good)
        self.refused(op="send", text=5, message_uuid=good)
        self.refused(op="send", text="x", message_uuid="not-a-uuid")
        self.refused(op="send", text="x", message_uuid=good, steer="yes")
        self.refused(op="send", text="x", message_uuid=good, confirm_seconds=999)
        self.refused(op="send", text="x", message_uuid=good, confirm_seconds=True)
        self.refused(protocol.ERROR_TOO_LARGE, op="send", text="x" * (protocol.MAX_TEXT_BYTES + 1), message_uuid=good)

    def test_lookup(self):
        good = str(uuid.uuid4())
        self.assertEqual(self.check(op="lookup", message_uuid=good, fence=True)[2], {"message_uuid": good, "fence": True})
        self.refused(op="lookup", message_uuid=good, fence=1)
        self.refused(op="lookup")

    def test_answer_accepts_bool_string_and_object_decisions(self):
        for decision in (True, False, "allow", {"behavior": "allow", "answers": {"Which?": "A"}}):
            self.assertEqual(self.check(op="answer", request_id="req-1", decision=decision)[2]["decision"], decision)
        self.refused(op="answer", request_id="req-1", decision=3)
        self.refused(op="answer", request_id="", decision=True)
        self.refused(op="answer", request_id="has space", decision=True)
        self.refused(protocol.ERROR_TOO_LARGE, op="answer", request_id="r", decision={"x": "y" * 70000})

    def test_an_answer_decision_object_is_reduced_to_allow_deny_message_interrupt_and_answers(self):
        decision = {"behavior": "Allow", "message": "ok", "interrupt": True, "answers": {"Which?": "A"},
                    "updated_input": {"command": "rm -rf /"}, "updated_permissions": [{"type": "addRules"}], "anything": 1}
        self.assertEqual(self.check(op="answer", request_id="r", decision=decision)[2]["decision"],
                         {"behavior": "Allow", "message": "ok", "interrupt": True, "answers": {"Which?": "A"}})
        self.assertEqual(self.check(op="answer", request_id="r", decision={"updated_input": {"a": 1}})[2]["decision"], {})
        for bad in ({"behavior": "maybe"}, {"behavior": 1}, {"message": 3}, {"interrupt": "yes"}, {"answers": "Blue"}, {"answers": ["x"]}):
            self.refused(op="answer", request_id="r", decision=bad)
        self.assertEqual(protocol.sanitize_decision(True), True)
        self.assertEqual(protocol.sanitize_decision("deny"), "deny")

    def test_close_and_events(self):
        self.assertEqual(self.check(op="close")[2], {"mode": "graceful"})
        self.assertEqual(self.check(op="close", mode="now")[2], {"mode": "now"})
        self.refused(op="close", mode="kill")
        self.assertEqual(self.check(op="events")[2], {"since": 0, "limit": protocol.DEFAULT_EVENT_LIMIT})
        self.assertEqual(self.check(op="events", since=7, limit=5)[2], {"since": 7, "limit": 5})
        self.refused(op="events", since=-1)
        self.refused(op="events", limit=0)
        self.refused(op="events", limit=protocol.MAX_EVENT_LIMIT + 1)

    def test_request_builder_leaves_out_none_arguments(self):
        built = protocol.request("events", "r1", "tok", since=3, limit=None)
        self.assertEqual(built, {"id": "r1", "op": "events", "token": "tok", "since": 3})


class ResponseTest(unittest.TestCase):
    def test_shapes(self):
        self.assertEqual(protocol.ok("a", mode="started"), {"id": "a", "status": "ok", "mode": "started"})
        self.assertEqual(protocol.rejected("a", protocol.REJECT_TURN_RUNNING),
                         {"id": "a", "status": "rejected", "reason": "turn_running"})
        failure = protocol.error("a", protocol.ERROR_UNAUTHORIZED)
        self.assertEqual((failure["status"], failure["error"]), ("error", "unauthorized"))

    def test_check_response(self):
        self.assertEqual(protocol.check_response(protocol.ok("a"), "a")["status"], "ok")
        with self.assertRaises(protocol.ProtocolError):
            protocol.check_response(protocol.ok("b"), "a")
        with self.assertRaises(protocol.ProtocolError):
            protocol.check_response({"id": "a"}, "a")
        # a refusal before the host could read the id carries none
        self.assertEqual(protocol.check_response(protocol.error(None, protocol.ERROR_BAD_REQUEST), "a")["status"], "error")


if __name__ == "__main__":
    unittest.main()

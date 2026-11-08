"""System nudges before and after a verified move keep their original attribution."""
import sqlite3
import unittest

from qorqut.native_chat.store import conversation_markers


class RelocatedMarkersTests(unittest.TestCase):
    def test_both_verified_paths_without_accepting_unrelated_bindings(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE native_sessions(id TEXT, agent_id TEXT, provider TEXT, conversation_id TEXT, transcript_path TEXT);
            CREATE TABLE native_inputs(session_id TEXT, origin TEXT, agent_id TEXT, marker TEXT);
        """)
        cases = [("before", "agent", "conversation", "/original"),
                 ("after", "agent", "conversation", "/copy"),
                 ("wrong-source", "agent", "conversation", "/unrelated"),
                 ("wrong-conversation", "agent", "other", "/copy"),
                 ("wrong-agent", "other", "conversation", "/copy")]
        for marker, agent, conversation, path in cases:
            conn.execute("INSERT INTO native_sessions VALUES(?,?,?,?,?)", (marker, agent, "codex", conversation, path))
            conn.execute("INSERT INTO native_inputs VALUES(?,?,?,?)", (marker, "system", agent, marker))
        markers = [case[0] for case in cases]
        found = conversation_markers(conn, "agent", "codex", "conversation", markers, ("/original", "/copy"))
        self.assertEqual({"before", "after"}, set(found))
        self.assertEqual({"after"}, set(conversation_markers(conn, "agent", "codex", "conversation", markers, "/copy")))


if __name__ == "__main__":
    unittest.main()

"""The Cursor lane: slug decoding and transcript-inferred sessions.

Cursor is the writer that never announces itself -- no session marker, no
claim -- so everything here is inferred from paths and mtimes. These tests
pin the two inferences that can silently go wrong: a slug decoded to the
wrong directory, and a stale transcript counted as a live agent.
"""

import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import henhouse


def _make_state_db(path, rows):
    """A minimal state.vscdb: just the composerHeaders columns henhouse reads.

    ``rows`` are (composerId, isSubagent, isArchived, value-dict).
    """
    con = sqlite3.connect(path)
    try:
        con.execute(
            "CREATE TABLE composerHeaders ("
            "composerId TEXT, lastUpdatedAt INTEGER, createdAt INTEGER, "
            "isSubagent INTEGER, isArchived INTEGER, value TEXT)")
        for cid, is_sub, is_arch, value in rows:
            con.execute(
                "INSERT INTO composerHeaders VALUES (?, ?, ?, ?, ?, ?)",
                (cid, None, None, is_sub, is_arch, json.dumps(value)))
        con.commit()
    finally:
        con.close()


class SlugDecoding(unittest.TestCase):
    """The slug joins path parts with '-', and '-' is legal in a directory
    name. Every hyphenated repo in this estate is a chance to get it wrong."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_hyphenated_directory_beats_the_naive_split(self):
        # dev/heron-ops exists; dev/heron/ops does not. Longest-first must win.
        (self.root / "dev" / "heron-ops").mkdir(parents=True)
        slug = "-".join(str(self.root).replace("\\", "/").strip("/").split("/"))
        if os.name == "nt":
            slug = slug.replace(":", "")
        got = henhouse.cursor_slug_to_cwd(slug + "-dev-heron-ops")
        self.assertTrue(got.replace("\\", "/").endswith("dev/heron-ops"), got)

    def test_unhyphenated_path_still_decodes(self):
        (self.root / "dev" / "blog").mkdir(parents=True)
        slug = "-".join(str(self.root).replace("\\", "/").strip("/").split("/"))
        if os.name == "nt":
            slug = slug.replace(":", "")
        got = henhouse.cursor_slug_to_cwd(slug + "-dev-blog")
        self.assertTrue(got.replace("\\", "/").endswith("dev/blog"), got)

    def test_path_that_exists_nowhere_falls_back_instead_of_raising(self):
        # Another machine's checkout. Wrong, but it must never blow up the view.
        got = henhouse.cursor_slug_to_cwd("c-Users-nobody-nowhere")
        self.assertTrue(got)

    def test_empty_slug_is_empty_not_a_crash(self):
        self.assertEqual(henhouse.cursor_slug_to_cwd(""), "")


class CursorSessions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.projects = Path(self.tmp.name) / "projects"
        self._orig = henhouse.CURSOR_PROJECTS_DIR
        henhouse.CURSOR_PROJECTS_DIR = self.projects
        self.addCleanup(lambda: setattr(
            henhouse, "CURSOR_PROJECTS_DIR", self._orig))
        # Keep the fixture hermetic: without this, tests on a machine that
        # runs Cursor would read the real state.vscdb.
        self._orig_db = henhouse.CURSOR_STATE_DB
        henhouse.CURSOR_STATE_DB = self.projects / "no-such-state.vscdb"
        self.addCleanup(lambda: setattr(
            henhouse, "CURSOR_STATE_DB", self._orig_db))

    def _agent(self, slug, agent_id, age_secs, text=None):
        d = self.projects / slug / "agent-transcripts" / agent_id
        d.mkdir(parents=True)
        f = d / "t.jsonl"
        payload = {"text": text} if text else {"text": "no query here"}
        f.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        when = time.time() - age_secs
        os.utime(f, (when, when))
        return f

    def test_a_recent_agent_is_reported(self):
        self._agent("c-Users-x-dev", "abcdef1234", 10)
        rows = henhouse.load_cursor_sessions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "cursor")
        self.assertEqual(rows[0]["status"], "working")
        # No pid: there is no process to join against, and inventing one would
        # make a weaker signal look like the stronger pid-join.
        self.assertIsNone(rows[0]["pid"])

    def test_a_stale_agent_is_dropped(self):
        self._agent("c-Users-x-dev", "old0000000",
                    henhouse.CURSOR_MAX_IDLE_SECS + 60)
        self.assertEqual(henhouse.load_cursor_sessions(), [])

    def test_idle_between_working_and_the_horizon(self):
        self._agent("c-Users-x-dev", "midaged000", henhouse.WORKING_SECS + 30)
        rows = henhouse.load_cursor_sessions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "idle")

    def test_the_user_query_becomes_the_task(self):
        self._agent("c-Users-x-dev", "withquery0", 5,
                    text="<user_query>fix the release  trigger</user_query>")
        rows = henhouse.load_cursor_sessions()
        self.assertEqual(rows[0]["task"], "fix the release trigger")

    def test_an_agent_dir_with_no_transcript_is_skipped(self):
        (self.projects / "c-Users-x-dev" / "agent-transcripts" / "empty00000"
         ).mkdir(parents=True)
        self.assertEqual(henhouse.load_cursor_sessions(), [])

    def test_missing_projects_root_is_empty_not_an_error(self):
        henhouse.CURSOR_PROJECTS_DIR = self.projects / "does-not-exist"
        self.assertEqual(henhouse.load_cursor_sessions(), [])

    def test_the_lane_can_be_switched_off(self):
        self._agent("c-Users-x-dev", "abcdef1234", 10)
        os.environ["LEGBAR_BACKENDS"] = "claude"
        self.addCleanup(os.environ.pop, "LEGBAR_BACKENDS", None)
        self.assertEqual(henhouse.load_cursor_sessions(), [])

    def test_without_the_db_the_new_cells_are_none_not_absent(self):
        # The transcript-only degradation: keys exist, values honest.
        self._agent("c-Users-x-dev", "abcdef1234", 10)
        row = henhouse.load_cursor_sessions()[0]
        self.assertIsNone(row["ctx_pct"])
        self.assertIsNone(row["model"])


class ComposerHeaders(unittest.TestCase):
    """The state.vscdb path: Cursor's own context meter and composer names.

    Transcripts, measured, carry neither (71/71 live files with no usage),
    so these cells exist only if this read works -- and the read must degrade
    to nothing, never to an exception, because a missing or locked DB is
    normal.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_headers_are_read_and_keyed_by_composer_id(self):
        db = self.root / "state.vscdb"
        _make_state_db(db, [
            ("abc123", 0, 0, {"name": "union TUI mockup",
                              "contextUsagePercent": 41.5,
                              "lastUpdatedAt": 1765000000000}),
        ])
        h = henhouse.read_cursor_composer_headers(db)
        self.assertIn("abc123", h)
        self.assertEqual(h["abc123"]["name"], "union TUI mockup")
        self.assertEqual(h["abc123"]["ctx_pct"], 41.5)
        self.assertEqual(h["abc123"]["last_write"], 1765000000.0)

    def test_subagents_archived_and_drafts_are_skipped(self):
        db = self.root / "state.vscdb"
        _make_state_db(db, [
            ("sub00", 1, 0, {"name": "a subagent"}),
            ("arch0", 0, 1, {"name": "archived"}),
            ("draft", 0, 0, {"name": "draft", "isDraft": True}),
            ("keep0", 0, 0, {"name": "kept"}),
        ])
        h = henhouse.read_cursor_composer_headers(db)
        self.assertEqual(set(h), {"keep0"})

    def test_a_missing_db_is_empty_not_an_error(self):
        self.assertEqual(
            henhouse.read_cursor_composer_headers(self.root / "absent.vscdb"),
            {})

    def test_garbage_db_is_empty_not_an_error(self):
        db = self.root / "state.vscdb"
        db.write_bytes(b"this is not sqlite")
        self.assertEqual(henhouse.read_cursor_composer_headers(db), {})


class HeaderEnrichedSessions(unittest.TestCase):
    """load_cursor_sessions joined against composerHeaders."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.projects = Path(self.tmp.name) / "projects"
        self.db = Path(self.tmp.name) / "state.vscdb"
        for attr, val in (("CURSOR_PROJECTS_DIR", self.projects),
                          ("CURSOR_STATE_DB", self.db)):
            orig = getattr(henhouse, attr)
            setattr(henhouse, attr, val)
            self.addCleanup(setattr, henhouse, attr, orig)

    def _agent(self, agent_id, age_secs, lines):
        d = self.projects / "c-Users-x-dev" / "agent-transcripts" / agent_id
        d.mkdir(parents=True)
        f = d / "t.jsonl"
        f.write_text("".join(json.dumps(x) + "\n" for x in lines),
                     encoding="utf-8")
        when = time.time() - age_secs
        os.utime(f, (when, when))

    def test_ctx_pct_and_name_come_from_the_header(self):
        self._agent("abc123", 10, [{"text": "no query here"}])
        _make_state_db(self.db, [
            ("abc123", 0, 0, {"name": "fix the tap push",
                              "contextUsagePercent": 63}),
        ])
        row = henhouse.load_cursor_sessions()[0]
        self.assertEqual(row["ctx_pct"], 63.0)
        # No user_query in the transcript, so the composer name is the task.
        self.assertEqual(row["task"], "fix the tap push")

    def test_the_transcript_query_still_beats_the_header_name(self):
        self._agent("abc123", 10, [
            {"text": "<user_query>port the headers</user_query>"}])
        _make_state_db(self.db, [
            ("abc123", 0, 0, {"name": "some stale composer name"}),
        ])
        row = henhouse.load_cursor_sessions()[0]
        self.assertEqual(row["task"], "port the headers")

    def test_a_fresh_header_revives_a_stale_transcript(self):
        # The header's lastUpdatedAt can move without the transcript; the
        # fresher of the two is the liveness signal.
        self._agent("abc123", henhouse.CURSOR_MAX_IDLE_SECS + 600,
                    [{"text": "old"}])
        _make_state_db(self.db, [
            ("abc123", 0, 0,
             {"name": "still going",
              "lastUpdatedAt": int((time.time() - 5) * 1000)}),
        ])
        rows = henhouse.load_cursor_sessions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "working")

    def test_the_model_comes_from_a_task_tool_use(self):
        self._agent("abc123", 10, [
            {"text": "<user_query>go</user_query>"},
            {"role": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Task",
                 "input": {"model": "composer-2.5"}}]}},
        ])
        _make_state_db(self.db, [("abc123", 0, 0, {"name": "x"})])
        row = henhouse.load_cursor_sessions()[0]
        self.assertEqual(row["model"], "composer-2.5")


if __name__ == "__main__":
    unittest.main()

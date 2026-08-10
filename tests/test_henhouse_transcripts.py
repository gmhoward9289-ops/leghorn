#!/usr/bin/env python3
"""Tests for the transcript telemetry provider -- henhouse's native replacement
for claudectl.

The failure modes worth pinning are all silent ones. A wrong context window
produces a plausible-looking percentage rather than an error. Summing cache
reads across turns instead of taking the last one inflates context without
ever exceeding 100%. And seeking to a fixed byte offset lands mid-record, so
the first line of the tail window is half a JSON object.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def load(name):
    spec = importlib.util.spec_from_loader(
        name + "_under_test",
        importlib.machinery.SourceFileLoader(name + "_under_test",
                                             str(HERE / (name + ".py"))),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cb = load("henhouse")


def assistant(usage=None, tools=(), model="claude-sonnet-5"):
    content = [{"type": "tool_use", "name": n, "input": {"file_path": p}}
               for n, p in tools]
    msg = {"model": model, "content": content}
    if usage is not None:
        msg["usage"] = usage
    return {"type": "assistant", "message": msg}


def user(text="hi"):
    return {"type": "user", "message": {"content": text}}


USAGE = {"input_tokens": 2, "cache_read_input_tokens": 100_000,
         "cache_creation_input_tokens": 0, "output_tokens": 500}


class ContextWindow(unittest.TestCase):
    def test_known_models(self):
        self.assertEqual(cb.context_window("claude-opus-5"), 1_000_000)
        self.assertEqual(cb.context_window("claude-sonnet-5"), 1_000_000)
        self.assertEqual(cb.context_window("claude-haiku-4-5"), 200_000)

    def test_haiku_is_not_shadowed_by_a_broader_prefix(self):
        # Haiku's 200K window is the one value that differs. If a broader entry
        # ever matched first, its percentages would silently read 5x too low.
        self.assertEqual(cb.context_window("claude-haiku-4-5-20251001"), 200_000)

    def test_unknown_model_falls_through(self):
        self.assertEqual(cb.context_window("claude-nonesuch-9"), cb.DEFAULT_WINDOW)
        self.assertEqual(cb.context_window(None), cb.DEFAULT_WINDOW)


class Summarize(unittest.TestCase):
    def test_context_is_the_last_turn_not_the_sum(self):
        # Cache reads repeat every turn. Summing them would climb without bound.
        recs = [assistant(USAGE), assistant(USAGE), assistant(USAGE)]
        out = cb.summarize(recs, time.time())
        self.assertAlmostEqual(out["context_pct"], 100_002 / 10_000, places=4)

    def test_burn_is_cumulative(self):
        out = cb.summarize([assistant(USAGE), assistant(USAGE)], time.time())
        self.assertEqual(out["burn_tokens"], 1000)

    def test_pct_uses_the_model_from_the_transcript(self):
        haiku = cb.summarize([assistant(USAGE, model="claude-haiku-4-5")], time.time())
        opus = cb.summarize([assistant(USAGE, model="claude-opus-5")], time.time())
        self.assertGreater(haiku["context_pct"], opus["context_pct"])

    def test_write_paths_collected_and_normalized(self):
        recs = [assistant(USAGE, tools=[("Edit", r"C:\Users\x\dev\proj\a.py"),
                                        ("Read", r"C:\Users\x\dev\proj\b.py")])]
        files = cb.summarize(recs, time.time())["files_modified"]
        # Backslashes must become forward slashes or the INFRA filter and
        # split_path both fail to match on Windows.
        self.assertIn("C:/Users/x/dev/proj/a.py", files)
        # A Read says nothing about which project a session is working on.
        self.assertNotIn("C:/Users/x/dev/proj/b.py", files)

    def test_status_waiting_on_a_bare_assistant_turn(self):
        out = cb.summarize([user(), assistant(USAGE)], time.time())
        self.assertEqual(out["status"], "needsinput")
        self.assertIn(out["status"], cb.ATTENTION)

    def test_status_working_while_tools_run(self):
        out = cb.summarize([user(), assistant(USAGE, tools=[("Write", "/tmp/a")])],
                           time.time())
        self.assertEqual(out["status"], "working")

    def test_status_idle_when_the_transcript_is_cold(self):
        stale = time.time() - (cb.WORKING_SECS + 60)
        out = cb.summarize([user(), assistant(USAGE, tools=[("Write", "/tmp/a")])],
                           stale)
        self.assertEqual(out["status"], "idle")

    def test_empty_transcript_yields_no_context(self):
        out = cb.summarize([], time.time())
        self.assertIsNone(out["context_pct"])
        self.assertIsNone(out["cost_usd"])

    def test_cost_is_never_fabricated(self):
        # A token-derived dollar figure is meaningless against a flat
        # subscription; emitting one would read as money that was never spent.
        self.assertIsNone(cb.summarize([assistant(USAGE)], time.time())["cost_usd"])


class ReadTail(unittest.TestCase):
    def _write(self, tmp, records):
        path = tmp / "s.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in records),
                        encoding="utf-8")
        return path

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_reads_every_record_of_a_small_file(self):
        path = self._write(self.tmp, [user(), assistant(USAGE)])
        self.assertEqual(len(cb.read_tail(path)), 2)

    def test_drops_the_partial_first_line_of_a_large_file(self):
        # Pad past TAIL_BYTES so the seek lands mid-record, then confirm the
        # tail still parses and keeps the final turn.
        pad = [user("x" * 4000) for _ in range(120)]
        path = self._write(self.tmp, pad + [assistant(USAGE)])
        self.assertGreater(path.stat().st_size, cb.TAIL_BYTES)
        recs = cb.read_tail(path)
        self.assertTrue(recs)
        self.assertEqual(recs[-1]["type"], "assistant")

    def test_truncated_trailing_line_is_skipped_not_fatal(self):
        # A transcript being appended to while we read it ends mid-object.
        path = self.tmp / "s.jsonl"
        path.write_text(json.dumps(user()) + "\n" + '{"type": "assis',
                        encoding="utf-8")
        self.assertEqual(len(cb.read_tail(path)), 1)

    def test_missing_file_is_empty_not_an_exception(self):
        self.assertEqual(cb.read_tail(self.tmp / "nope.jsonl"), [])


if __name__ == "__main__":
    unittest.main()

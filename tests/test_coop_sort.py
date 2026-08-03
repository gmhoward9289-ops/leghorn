#!/usr/bin/env python3
"""Tests for coop.sort_key.

The attention sort compares status against ATTENTION, whose entries are
space-stripped ("needsinput"); live statuses arrive as "Needs Input". Before
2026-08-01 sort_key skipped the strip, so the one sort mode that exists to
surface blocked sessions never did.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "coop.py"


def load():
    loader = importlib.machinery.SourceFileLoader("coop_sort_under_test", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


cb = load()


def row(status, ctx=50, contested=False, git=None):
    return {"status": status, "context_pct": ctx, "contested": contested,
            "git": git or {}}


class SortKeyTest(unittest.TestCase):
    def test_spaced_status_counts_as_attention(self):
        # "Needs Input" is how the status actually renders; it must sort
        # ahead of an idle session exactly like "needsinput" would.
        blocked = row("Needs Input")
        idle = row("Idle", ctx=90)
        self.assertEqual([blocked, idle],
                         sorted([idle, blocked], key=cb.sort_key))

    def test_attention_states_beat_context(self):
        for status in ("Waiting", "Error", "Failed", "needs input"):
            with self.subTest(status=status):
                self.assertLess(cb.sort_key(row(status, ctx=0)),
                                cb.sort_key(row("Processing", ctx=99)))

    def test_contested_still_wins_overall(self):
        self.assertLess(cb.sort_key(row("Idle", contested=True)),
                        cb.sort_key(row("Needs Input")))


if __name__ == "__main__":
    unittest.main()

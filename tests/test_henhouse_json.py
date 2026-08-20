#!/usr/bin/env python3
"""--json is a wrapper object; --legacy-json keeps the old bare list."""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import json
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "henhouse.py"


def load():
    spec = importlib.util.spec_from_loader(
        "henhouse_json_under_test",
        importlib.machinery.SourceFileLoader("henhouse_json_under_test", str(SCRIPT)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cb = load()


class JsonShape(unittest.TestCase):
    def setUp(self):
        self._orig = {
            "load_sessions": cb.load_sessions,
            "load_transcripts": cb.load_transcripts,
            "load_registry": cb.load_registry,
            "build": cb.build,
        }
        cb.load_sessions = lambda: []
        cb.load_transcripts = lambda sessions: ({}, None)
        cb.load_registry = lambda: ({}, {})
        cb.build = lambda *a, **k: []

    def tearDown(self):
        for name, fn in self._orig.items():
            setattr(cb, name, fn)

    def _ns(self, **extra):
        kw = dict(github=None, log=None, json=False, legacy_json=False,
                  no_git=True, wide=False)
        kw.update(extra)
        return argparse.Namespace(**kw)

    def test_json_includes_henhouse_session_schema(self):
        out = json.loads(cb.render(self._ns(json=True), 80)[0])
        self.assertEqual(out["schema"], "henhouse.session.v1")
        self.assertEqual(out["rows"], [])

    def test_legacy_json_is_a_bare_list(self):
        out = json.loads(cb.render(self._ns(legacy_json=True), 80)[0])
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()

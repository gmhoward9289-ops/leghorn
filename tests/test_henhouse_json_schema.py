"""henhouse --json emits a versioned wrapper by default."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HENHOUSE = ROOT / "henhouse.py"


class HenhouseJsonSchemaTest(unittest.TestCase):
    def test_session_json_includes_schema(self):
        proc = subprocess.run(
            [sys.executable, str(HENHOUSE), "--json", "--no-git"],
            capture_output=True,
            text=True,
            check=True,
            cwd=ROOT,
        )
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["schema"], "henhouse.session.v1")
        self.assertIsInstance(payload["rows"], list)

    def test_legacy_json_emits_bare_list(self):
        proc = subprocess.run(
            [sys.executable, str(HENHOUSE), "--json", "--legacy-json", "--no-git"],
            capture_output=True,
            text=True,
            check=True,
            cwd=ROOT,
        )
        payload = json.loads(proc.stdout)
        self.assertIsInstance(payload, list)


if __name__ == "__main__":
    unittest.main()

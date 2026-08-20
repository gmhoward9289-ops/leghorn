"""henhouse --json emits a versioned wrapper by default."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HENHOUSE = ROOT / "henhouse.py"


def test_session_json_includes_schema():
    proc = subprocess.run(
        [sys.executable, str(HENHOUSE), "--json", "--no-git"],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    )
    payload = json.loads(proc.stdout)
    assert payload["schema"] == "henhouse.session.v1"
    assert isinstance(payload["rows"], list)


def test_legacy_json_emits_bare_list():
    proc = subprocess.run(
        [sys.executable, str(HENHOUSE), "--json", "--legacy-json", "--no-git"],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    )
    payload = json.loads(proc.stdout)
    assert isinstance(payload, list)

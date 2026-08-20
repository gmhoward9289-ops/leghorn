# henhouse naming — leghorn vs PyPI

Two different programs share the name **henhouse**. They are related in purpose (agent
session data) but not the same codebase.

| | **leghorn `henhouse.py`** | **PyPI `henhouse` (0.1.2+)** |
| --- | --- | --- |
| Role | Live session table for COOPER (`henhouse` / `leghorn` TUI) | Library: parse JSONL transcripts and tool envelopes |
| JSON schema | `henhouse.session.v1` on `--json` | `henhouse.tools.v1` for tool-call exports |
| Install | `python henhouse.py` in this repo | `pip install henhouse` |
| Transcript parsing | Inline in `henhouse.py` | `henhouse.transcripts` (`load_tool_calls`, `iter_tool_calls`, …) |

**Parity rule:** leghorn does **not** vendor the PyPI package. Session-row JSON and
transcript-tool JSON are separate contracts (`session.v1` vs `tools.v1`). When the
package adds transcript helpers, leghorn only needs updates if we choose to call
`pip install henhouse` for shared parsing — today it does not.

**Downstream:** [pytest-session-trace](https://github.com/gmhoward9289-ops/pytest-session-trace)
depends on the PyPI package, not this file. [roost](https://github.com/gmhoward9289-ops/git-roost)
emits `roost.snapshot.v1`; that is a third schema in the same proof stack.

To verify leghorn session JSON shape: `pytest tests/test_henhouse_json_schema.py`.

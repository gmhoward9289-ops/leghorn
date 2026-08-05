# Leghorn under-load lag check

**Verdict: ruled out**

Leghorn is **not** a meaningful lag contributor under load. With 40 repos, 24 live sessions, and all 4 CPU cores pegged by busy loops, the live TUI averaged **0.5% CPU** (p95/max 4%), held ~20 MB RSS, and finished every refresh well inside its 5s / 75s budgets. Stripping commits/github/git only dropped CPU by **0.3 percentage points**. Machine lag under similar conditions is elsewhere.

## Conditions

- Platform: linux, 4 CPUs, Python 3.12.3
- Synthetic fleet: **40 repos** (20 github.com), **24 live sessions** under `/tmp/leghorn-load-home`
- CPU load: 4 busy-loop processes (all cores pegged) during sampling; loadavg rose to ~3–4
- Model clocks: session/git/commits every 5s, github every 75s
- Measurement script: [`run_check.py`](run_check.py) (observational; not part of the product)

## 1. Live TUI sampling (~150s each, under CPU load)

| Mode | CPU avg | CPU p95 | CPU max | RSS avg | Exit |
|------|---------|---------|---------|---------|------|
| full (all panes) | **0.5%** | 4.0% | 4.0% | 19.7 MB | 0 |
| light (`--no-commits --no-github --no-git`) | **0.2%** | 2.0% | 4.0% | 18.4 MB | 0 |

Pass heuristic from the plan: low single-digit CPU average, brief spikes only on refresh, memory flat — **all met**.

## 2. One-shot path timings (under same CPU load)

| Path | Seconds | Notes |
|------|---------|-------|
| `sessions_plus_git` | **0.279** | 24 rows (transcripts + registry + git-roost stub) |
| `commit_feed` | **0.049** | 40 commits across 40 repos, 8 workers |
| `github_feed` | **0.784** | 20 github.com repos × stub `gh`, 6 workers |
| `sessions_no_git` | **0.004** | same sessions, git skipped |
| `alive` POSIX `os.kill(0)` | **0.000** | 24 pids |
| Windows-style proxy `ps -p` | **0.150** | 24 pids — lower bound vs real `tasklist` |

Fast collect total (sessions+git + commit_feed) = **0.328s** vs **5s** interval.  
`github_feed` = **0.784s** vs **75s** interval.

## 3. A/B: full vs stripped

Full features vs `--no-commits --no-github --no-git`: CPU avg **+0.3 pp** (0.5% → 0.2%).

That delta is noise relative to a loaded machine. Quitting Leghorn entirely would not move the needle on whole-box lag.

## 4. Windows-specific note

On Linux, `alive()` is `os.kill(pid, 0)` (effectively free). Windows uses one `tasklist` subprocess per session pid every refresh. A `ps -p` proxy for 24 pids cost **0.15s** — real `tasklist` is typically slower, but even at 5–10× that cost it would stay inside the 5s interval at this fleet size.

At much larger session counts (hundreds) the Windows `tasklist` loop is the only path worth revisiting — and only on Windows. It was **not** implicated here.

## Verdict detail

- Live full-mode CPU avg 0.5% (p95 4.0%, max 4.0%) over 150s while the machine was CPU-starved.
- Fast collect 0.328s vs 5s interval; github_feed 0.784s vs 75s interval — no overlap/backlog risk.
- A/B CPU delta full→light: +0.3 pp.
- RSS flat at ~19–20 MB — no leak signal in the sample window.
- **No code change recommended.** Leghorn is ruled out as a cause of system lag under load.

## Limitations

- Ran on a Linux cloud agent, not the Windows desktop where the concern was felt; Windows `tasklist` cost estimated only.
- `gh` and `git-roost` were local stubs (still real subprocess fan-out; not real GitHub network RTT).
- Secondary in-process Model sample was abandoned once live-TUI numbers were conclusive.

Raw data: [`results.json`](results.json) · run log: [`run.log`](run.log)

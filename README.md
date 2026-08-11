# leghorn

[![ci](https://github.com/gmhoward9289-ops/leghorn/actions/workflows/ci.yml/badge.svg)](https://github.com/gmhoward9289-ops/leghorn/actions/workflows/ci.yml)
[![discussions](https://img.shields.io/github/discussions/gmhoward9289-ops/leghorn)](https://github.com/gmhoward9289-ops/leghorn/discussions)

**What did this agent actually do?**

That is the question leghorn exists to answer. A session tells you what it
*intended* — the task it was given, the model it is on, that it finished. Git
tells you what landed. Only one of those two can be wrong, and it is never git.

So leghorn is a full-screen live dashboard you leave open on a second monitor
while many Claude Code sessions work: every live session joined to its worktree
and **real git state**, your **GitHub CI and open PRs** with failures pinned
until they go green, and a commit feed across every repo, newest first. Every
pane is there to close the gap between what an agent reported and what is
actually in the tree.

![leghorn watching a fleet: contested sessions, red CI, a session detail overlay, and the git toggle](demo/leghorn-demo.gif)

The short ambient loop below is the same program, idling:

![leghorn's ambient loop, sessions ticking quietly](demo/leghorn-loop.gif)

```
 leghorn  20 sessions · 2 shared · 1 uncommitted · 3 ci red        sort:attention  filter:all  15:26:53
╭─ SESSIONS ───────────────────────────────────────────────╮ ╭─ COMMITS ──────────────────────────╮
│◉  leghorn-fea  my-mcp-server        Needs Input  72%     │ │   9m tools  fix/build-cwd          │
│●  session-a3   api-refactor         Idle         58%     │ │       fix(api): resolve paths agai…│
│●  session-d8   web-frontend  ~2 ↑1  Processing   50%     │ │  25m web-frontend  origin/ci/pub   │
╰──────────────────────────────────────────────────────────╯ │       Automate publishing on relea…│
╭─ GITHUB ─────────────────────────────────────────────────╮ │  29m api-refactor                  │
│ 17h ✗ my-mcp-server  Required PR Sections (fix/fields)   │ │       Add an in-program help panel │
│ 17h ✗ my-mcp-server  #593 Add field selection ← sections │ │  41m tools                         │
│ 17h ◍ tools          Graph Update (queued 17h)           │ │       Add a LOCAL MODELS panel     │
│ 23m ✓ web-frontend   ci (ci/npm-and-tap-publish)         │ │   1h api-refactor  origin/feat/rel │
│ 24m ✓ web-frontend   #18 Automate publishing  APPROVED   │ │       teach release_check to diff  │
╰──────────────────────────────────────────────────────────╯ ╰────────────────────────────────────╯
 q quit  r refresh  s sort  f filter  g git  tab pane  enter detail  ? help
```

Sessions that share a working tree are marked contested (◉) — two sessions in
one checkout silently destroy each other's uncommitted work, and seeing it
happen is the first step to not doing it. The GitHub pane ranks by what it
costs to ignore: running first, then red (a failure must not scroll away),
then stuck-in-queue, then everything else by freshness.

## What each pane is for

Every pane answers one part of *what did this agent actually do*, and none of
them takes the session's word for it.

**SESSIONS** — who is running, on what model, how much context they have burned,
and **the worktree each one is standing in**. This is the only pane that reads
the agent's own account of itself, and it is joined to real git state precisely
so the account can be checked. A session reporting `Idle` in a tree with
uncommitted changes is a different situation from one reporting `Idle` in a
clean tree, and the session cannot tell you which it is.

**GITHUB** — the CI runs and open PRs the work is supposed to end in. An agent
that says it finished and a build that is red are both true statements about the
same change; this is where they meet.

**COMMITS** — what landed, across every repo, newest first. The plainest
possible answer to the question: not a status, not a summary, the actual commit
subjects in the order they happened.

Two files, stdlib only, Python 3.9+. macOS and Linux need nothing beyond the
standard library; Windows needs `windows-curses`, which is the only thing
curses is not already there for. Read-only by construction: it reads session
transcripts, runs `gh` and read-only git plumbing, and never writes to a tree,
a registry or a session. Every data source is optional — missing pieces degrade
to a labelled gap, never an error.

## Install

Homebrew (macOS and Linux):

```bash
brew install gmhoward9289-ops/tap/leghorn
```

npm:

```bash
npm install -g leghorn
```

pipx / pip:

```bash
pipx install leghorn
```

Debian and Ubuntu, from the signed apt repo — this also gets you `apt upgrade`:

```bash
curl -fsSL https://gmhoward9289-ops.github.io/leghorn/leghorn-archive-keyring.asc | sudo gpg --dearmor -o /usr/share/keyrings/leghorn-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/leghorn-archive-keyring.gpg] https://gmhoward9289-ops.github.io/leghorn stable main" | sudo tee /etc/apt/sources.list.d/leghorn.list
sudo apt update
sudo apt install leghorn
```

Or grab `leghorn_<version>_all.deb` from the
[latest release](https://github.com/gmhoward9289-ops/leghorn/releases/latest)
and `sudo apt install ./leghorn_<version>_all.deb`.

## What it needs

- **git** — for the per-tree state and the commit feed.
- **gh** (authenticated) — for the GITHUB pane. Unauthenticated, the pane says
  "cannot see github" rather than pretending nothing is happening; an empty
  feed and a feed that cannot see are different facts.
Session status, context and burn are read from the JSONL transcripts Claude
Code already writes under `~/.claude/projects` (override:
`CLAUDE_PROJECTS_DIR`) — no extra binary, and the same numbers on every
platform. A session with no transcript yet degrades to a labelled gap.

Clones are discovered under `~/GitHub` (override: `LEGHORN_ROOT`).

## Usage

```bash
leghorn                  # three panes, refreshing every 5s
leghorn -i 2             # ...every 2s
leghorn --speed slow     # 5m session refresh, 6h gh sweep (also: ultra, fast, normal)
leghorn --no-github      # hide the GitHub pane (no gh calls at all)
leghorn --no-commits     # hide the commit feed; sessions and github go side by side
leghorn --github-interval 120   # slower gh sweeps
```

Keys: `q` quit · `r` refresh (including a gh sweep) · `s`/`f` cycle
sort/filter · `p` cycle speed · `c` toggle commits · `tab` cycle panes ·
`j`/`k` move · `enter` detail · `?` help — which explains what each screen and symbol *means*, not just the
keys.

The data layer is also a command of its own:

```bash
python3 -m henhouse            # one table of sessions, for a hook or a pipe
python3 -m henhouse --github   # the CI/PR feed as plain text
python3 -m henhouse --json     # records, for piping somewhere else
```

## Name

The Leghorn is the chicken breed that spots everything first and is never
quiet about it.

## Questions or ideas?

→ [Discussions](https://github.com/gmhoward9289-ops/leghorn/discussions)

## License

MIT

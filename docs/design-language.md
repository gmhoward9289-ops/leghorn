# The leghorn design language

This document is the written standard for a look and feel shared across the
flock of terminal dashboards (leghorn, legbar, roost, git-roost) and, in a
translated dialect, the web surfaces of the same estate. Most of it was
extracted from leghorn, which is the oldest of the four and the one whose
habits the others already copy; some of it (principles 11, 13, 15, 16) was
extracted from the siblings, and leghorn does not yet meet those rules
itself. No product in the flock conforms fully today — the
[known gaps in leghorn](#appendix-known-gaps-in-leghorn-2026-09-01) are listed
at the end, and the sibling audits carry their own.

It is a spec, not a library — every product in the flock ships as a single
stdlib-only file on purpose, so each implements these rules in its own idiom
(curses color pairs, raw SGR strings, or CSS custom properties).

When this document and any product's behavior disagree, one of them has a
bug; neither automatically wins, and the disagreement goes in that product's
gap list until it is settled.

## Principles

These are the rules that make the look feel right. They outrank any specific
color or glyph below.

Principles 11, 13, 15 and 16 are marked with the surface they come from and
apply to. An *attention-list* surface ranks things a person must act on
(legbar, leghorn's SESSIONS pane); an *infra* surface reports whether
services are up (roost). The other principles apply everywhere.

1. **"Cannot see X" and "nothing is happening" must never render identically.**
   Loading states animate. Empty states name what is empty — a real string,
   not a placeholder: `no sessions match filter 'all'`, `no commits found`,
   `no CI runs or open PRs`. Failures get a labelled warning line (`cannot
   see github: …`). A pane that cannot know something says so; it never sits
   quietly looking up to date.
2. **Silent truncation is a lie.** Any list cut short ends with an explicit,
   attention-colored notice (`… N more below`) that names the key or remedy
   where one is not obvious — leghorn's own notice names none, because `j`
   on a focused list is the obvious thing to try; a notice on a list the
   user cannot scroll must say what does reach the rest.
3. **A wall display must always answer "when did this last update."** The
   clock and the data ages are the last things a header or footer sheds under
   width pressure — before mode labels, before the version stamp.
4. **Chrome that reports state must not lie.** If a flag overrides a preset's
   numbers, the chrome says `custom`, never the preset's name.
5. **Rank by cost of ignoring, not by recency.** Running and queued first
   (they tie — both are "in flight", and a queued run turns running without
   moving), then failed (a failure must not scroll away), then stuck, then
   everything else by freshness.
6. **One honest line per row.** Every column yields to the payload — the
   commit subject, the task text. Detail lives behind `enter`, not in wrapped
   rows.
7. **Degradation is designed, not overflowed.** Columns, key hints, and header
   chips each drop in a stated order as the window narrows. `q quit` and
   `? help` survive every tier, because they have no other way to be
   discovered. Test at 40 columns, not 80.
8. **Every data source is optional.** A missing tool or endpoint degrades to a
   labelled gap, never an error, never a crash. A crash is worse than a wrong
   pixel.
9. **Help is a glossary first, keys second.** The first question a viewer has
   is "what is this symbol telling me," not "what does j do."
10. **Read-only by construction** for anything that watches other tools' state.
11. **Inherit the terminal's theme.** *(All TUIs; from legbar and roost —
    leghorn gap.)* No hex values in a TUI — foreground roles on the
    terminal's own background, so the product looks native in every palette
    the user already chose. Honor `NO_COLOR`; wherever possible derive color
    from a colorless marker already on the line, so the display reads the
    same story without it.
12. **A number whose denominator changed must say so.** A filtered view
    renders `7 of 41 tree(s) [filter: uncommitted]`, and fleet-wide counts
    are computed over the unfiltered fleet — a count must never silently
    change meaning. (leghorn's header shows `N shown` but computes its other
    chips over the filtered rows — see the gaps appendix.)
13. **Attention has a grace period.** *(Attention-list surfaces: legbar —
    leghorn gap.)* A signal that is real but young is listed quietly; only
    past a stated age does it take the attention color. Anything contested
    or failing is loud from frame one. This is what keeps an attention color
    from becoming wallpaper.
14. **Loading is stable.** Cells fill in place in discovery order — a loading
    table must not regroup rows as results land. And loading *animates*,
    on interactive surfaces only: one-shot/pipe output stays byte-stable so
    it can be snapshot-tested.
15. **One problem, one row.** *(Attention-list surfaces: legbar — leghorn
    gap.)* N instances of the same underlying problem (three sessions in one
    contested tree) collapse into one ranked row, and a count that aggregates
    carries its worst age: `3 need you (12m)`.
16. **"Unseen" is not "down."** *(Infra surfaces: roost. Attention-list
    surfaces apply the same distinction to their data sources — leghorn gap.)*
    A source that was never configured or never answered here renders as a
    dim unknown (`off?`), distinct from a failure that was up and stopped
    answering (`DOWN`). Unknown must not collapse into either ok or failure.

## Semantic color roles

Six roles, named by meaning. TUIs map them to ANSI colors; the web dialect
maps them to tokens. No product invents a seventh role without updating this
document.

| Role       | TUI color                  | Web token   | Used for |
|------------|----------------------------|-------------|----------|
| ok / live  | green                      | `--ok`      | processing, fresh (< 5 min), a run in flight, a PR whose checks are green |
| attention  | yellow                     | `--warn`    | needs input, dirty tree, context ≥ 70%, truncation notices |
| failure    | red                        | `--crit`    | errors, failed runs |
| chrome     | cyan                       | `--chrome`  | borders, pane titles, the product's own name, session names |
| identity   | bright blue, always bold   | `--ident`   | repo and project names |
| divergence | magenta                    | `--alt`     | behind base, shared state |
| secondary  | dim/default                | `--fg3`     | everything at rest |

Notes that carry meaning beyond hue:

- **Bold is live, dim is at rest.** Attributes do as much work as color. A
  *finished* success is at rest: a completed run renders `✓` in secondary/dim,
  not green — only something still worth watching (a PR with green checks,
  waiting to be merged) earns the ok color.
- **Freshness threshold is 5 minutes** (`FRESH = 300`): younger renders ok/bold,
  older renders secondary/dim.
- **Blue needs help.** Plain ANSI blue is illegible on common dark palettes;
  request the bright variant when 16+ colors exist, and always pair blue with
  bold so 8-color terminals brighten it. How you ask matters: through SGR
  escapes, `38;5;12` means xterm bright blue because the *terminal* owns the
  palette. Through curses, ask for `COLOR_BLUE + 8`, never a literal index —
  windows-curses (PDCurses) numbers colors in Windows-console order
  (BLUE=1, RED=4), so a literal 12 there is bright *red*. Every repo name in
  the flock rendered red on Windows until this was found (2026-09-05).
- Selection is the one painted background: black on cyan.

## Glyph vocabulary

| Glyph | Codepoint | Meaning |
|-------|-----------|---------|
| `●`   | U+25CF | live / running (also the busy tick beside the clock). A session row always carries `●`, colored by status — dim when idle, not a different glyph |
| `◉`   | U+25C9 | contested — two live sessions in one tree (shape carries the meaning; the dot is colored by status, not red) |
| `○`   | U+25CB | queued / pending — a run waiting to start, checks not yet reported |
| `✓`   | U+2713 | success (dim for a finished run; green only for a PR whose checks are green) |
| `✗`   | U+2717 | failure |
| `◍`   | U+25CD | stuck in queue (> 2 h) |
| `·`   | U+00B7 | separator; no-data fallback |
| `↑ ↓` | U+2191/93 | ahead / behind base (`~2 ↑1 ↓3` is the git cell) |
| `←`   | U+2190 | prefix for a failing check name |
| `…`   | U+2026 | elision — keep both ends around a `/` so long names stay distinct |

Pipe-safe surfaces (plain CLIs, `--json` companions) use ASCII only:
`+staged ~dirty ?untracked`, `^ahead vbehind`, `=` for clean.

### Two dialects, one vocabulary — chosen by the terminal, not the product

The vocabulary ships in two glyph dialects. Which one renders is a runtime
capability decision, the same way color inherits the terminal's theme:

- **Unicode dialect**: the full table above, plus rounded frames — the
  preferred rendering wherever it can display.
- **ASCII dialect**: the pipe-safe set everywhere, `...` for elision,
  `[###---]` bars — the fallback, and *always* the dialect of pipe-safe
  output (`--once`, `--json`, anything non-interactive).

At startup an interactive surface probes its terminal once and holds the
answer for the whole session. The probe is, in order:

1. An explicit override wins: `--ascii` (or the product's equivalent) and an
   environment variable such as `LEGHORN_ASCII=1` force the ASCII dialect;
   the matching `--unicode` forces the other way for a terminal the probe
   misjudges.
2. stdout must be a TTY. Pipes, files and `--once`/`--json` output are
   always ASCII.
3. `sys.stdout.encoding` must be UTF-8 (case-insensitive, `utf-8`/`utf8`).
4. On Windows, additionally require Windows Terminal: `WT_SESSION` set in
   the environment. **The stdout encoding alone is not a usable signal on
   Windows** — since PEP 528 (Python 3.6) every Windows console reports
   `utf-8` regardless of the host, and the bare conhost window that reports
   it still renders box drawing and `…` as mojibake in common fonts and
   codepages. A bare conhost keeps ASCII; Windows Terminal (and any
   `TERM_PROGRAM`-announcing emulator such as VS Code's) gets Unicode.

The historical "block drawing mojibakes on Windows" stance in the siblings
was about conhost; Windows Terminal renders the Unicode tier correctly, as
leghorn demonstrates daily on the same machines. A frame must never mix
dialects — a lone `…` in an ASCII frame is a bug. The *semantics* (what a
glyph slot means, what color it takes) are identical across dialects; only
the codepoints differ.

Honesty note: leghorn itself does **not** implement this probe today. It
emits the Unicode dialect unconditionally — curses frames, `●`/`◉`, `…`
notices — with no ASCII fallback and no override flag. That is a known gap
(see the appendix), not a licence for the siblings to skip the probe.

## Chrome

- **Frame** (multi-pane products): rounded light box drawing (`╭─╮ │ ╰─╯`) in
  chrome color; bold border when focused, dim when not — focus is signalled
  purely by border weight. Title inset two columns, uppercase, padded:
  `╭─ SESSIONS ─…`. Single-pane layouts, and any surface rendering the ASCII
  fallback, draw the same slot as a bold uppercase title (plus a rule where
  one helps); focus, where
  it exists, is a marker or reverse-video row, and reverse must be re-armed
  after every inner reset.
- **Header:** product name in chrome bold at column 1, `·`-separated stat
  chips, right-aligned modes and clock. Sheds fields in a fixed order: mode
  labels first, then stat chips right-to-left, and the clock goes last — a
  header that keeps `3 uncommitted` but drops the clock has the order
  backwards. Stat chips that count the fleet count the *whole* fleet
  (principle 12).
- **Footer:** key hints left, data ages middle-right (`updated 4s ago · gh 1m`),
  `v<version>` in the true corner. Under pressure the version drops whole
  before the ages truncate.
- **Overlays:** centered modal reusing the pane frame, right-justified chrome
  labels, dim `any key to close`.
- **Never write into the last column.** A glyph in the final cell wraps onto
  the next row's border and persists. In SGR-dialect products, enforce this
  with a visible-length primitive (`visible_len`/`clip_ansi`) — `len()` on a
  colored string is the recurring bug.
- **Chrome must not move when it changes.** This is about *mode indicators*
  — `ARMED` / `off  `, `speed:fast` / `speed:slow`, a filter label — which
  keep a fixed width in every state so a state flip never reflows the line
  around them. It is not a rule about data columns: a table column *may* fit
  its width to its content (leghorn's content-fit columns, #46) provided the
  width changes with hysteresis rather than per frame, and provided *which*
  columns are present depends only on the terminal width — a column set that
  appears and disappears as data churns is the reflow this rule forbids.

## Text conventions

- **Ages:** `45s`, `12m`, `3h`, `2d` — one unit, no padding words. Commit ages
  use author date, not committer date (rebases rewrite `%ct`).
- **Status casing:** constants are lowercase and unspaced (`needsinput`); the
  UI renders spaced title case (`Needs Input`); every comparison normalizes
  with `.lower().replace(" ", "")`.
- **Elision** keeps both ends around a `/` so `repo` and `repo/branch` never
  collapse to the same string.
- Loading animates by deriving dots from the clock
  (`"collecting" + "." * (int(time.time()*2) % 3 + 1)`) — visible motion for
  free, because a static label in dim color is indistinguishable from a dead
  one.

## Conformance checklist (TUI flock)

A product conforms when:

- [ ] All six semantic roles map as above, and no color is used outside its role.
- [ ] The Unicode dialect renders on interactive UTF-8 terminals that pass
      the probe (isatty + utf-8 + Windows Terminal on Windows), falling back
      to ASCII elsewhere with an explicit override; pipe-safe output is
      always ASCII; no frame ever mixes dialects.
- [ ] `NO_COLOR` is honored, and the display reads the same story without
      color.
- [ ] Titles are uppercase; multi-pane products frame with rounded box drawing
      and signal focus by border weight; flat layouts and the ASCII fallback
      use bold titles and a marked or reversed row.
- [ ] Loading, empty, and failed states are three visibly different things,
      and loading fills in place without regrouping.
- [ ] Truncated lists end in an attention-colored `… N more` notice that
      names the key or remedy where one is not obvious.
- [ ] Filtered counts state both numerators and fleet-wide chips count the
      unfiltered fleet; aggregate counts (attention-list surfaces) carry
      their worst age.
- [ ] Header and footer degrade in a designed order; quit and help hints
      survive every tier; the clock and data ages survive everything —
      verified at 40 and 60 columns, not just 80.
- [ ] Attention-list surfaces: young signals wait out a stated grace period
      before taking the attention color; N instances of one problem collapse
      to one row. Infra surfaces: unseen renders distinct from down.
- [ ] Ages use the `45s/12m/3h/2d` format.
- [ ] Nothing writes into the last column; layouts are tested at 40 columns.
- [ ] Missing data sources degrade to labelled gaps, never errors.

## The web dialect

Web surfaces cannot inherit a terminal theme, so the roles become tokens with
two explicit themes (a light and a dark terminal profile, in effect). The
canonical token file lives in heron-ops (`heron_ops/ui/css/tokens.css`), which
already names `--crit/--warn/--ok` and documents its contrast ratios; other
web surfaces adopt those names and values rather than inventing palettes.
Products with a deliberate standalone art direction (counting-chicken-wings)
are exempt by decision, not by drift.

## Appendix: known gaps in leghorn (2026-09-01)

Where leghorn's own behavior falls short of the charter as of this date,
verified against `leghorn.py`. Each is a bug or a feature to file, not a
reason to soften the rule. Items are removed here as they land.

| # | Gap | Principle | Detail |
|---|-----|-----------|--------|
| 1 | **Header never sheds stat chips; the clock is lost below 80 columns.** | 3, 7 | `draw_header` drops mode labels before the clock, but only stops *adding* chips when the next one would not fit — it never removes a chip to make room for the clock. With one dirty, one behind and one shared tree, the header at 40 and 60 columns shows `leghorn  3 sessions · 1 shared …` and no clock at all. Fix: shed chips right-to-left until the bare clock fits. (PR: `fix/header-clock-and-fleet-chips`.) |
| 2 | **Header chips are computed over the filtered rows.** | 12 | `draw_header` receives the filtered `rows`, so `N shared / N uncommitted / N behind` change meaning the moment `f` cycles the filter; only `N sessions` (from `len(rows_all)`) is fleet-wide. Fix: compute chips over `rows_all`. (Same PR.) |
| 3 | **No `NO_COLOR` support.** | 11 | Color pairs are always initialised; nothing reads the environment. |
| 4 | **No grace period for attention.** | 13 | `Needs Input` and `Waiting` take yellow from the first frame regardless of age. |
| 5 | **No unseen/down distinction for data sources.** | 16 | `cannot see github: <reason>` is the only degraded state; a `gh` that was never installed and a `gh` that just started failing render identically. Same for `git`. |
| 6 | **No ASCII dialect, no probe, no override.** | Two dialects | Unicode is emitted unconditionally; a bare conhost gets mojibake and there is no `--ascii`. |
| 7 | **The truncation notice names no key.** | 2 | `… N more below` is acceptable under the softened wording (j on a focused list is obvious), but the notice appears on unfocused panes too, where `tab` is the remedy and is not named. |
| 8 | **No one-problem-one-row collapse.** | 15 | Three sessions in one contested tree are three rows, each marked `◉`; the header's `N shared` counts sessions, not trees, and carries no age. |

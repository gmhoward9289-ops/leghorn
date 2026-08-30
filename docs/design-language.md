# The leghorn design language

leghorn is the reference implementation of a look and feel shared across the
flock of terminal dashboards (leghorn, legbar, roost, git-roost) and, in a
translated dialect, the web surfaces of the same estate. This document is the
standard those products conform to. It is a spec, not a library — every product
in the flock ships as a single stdlib-only file on purpose, so each implements
these rules in its own idiom (curses color pairs, raw SGR strings, or CSS
custom properties).

When this document and leghorn's behavior disagree, one of them has a bug;
neither automatically wins.

## Principles

These are the rules that make the look feel right. They outrank any specific
color or glyph below.

1. **"Cannot see X" and "nothing is happening" must never render identically.**
   Loading states animate. Empty states name what is empty (`no sessions
   found`). Failures get a labelled warning line. A pane that cannot know
   something says so; it never sits quietly looking up to date.
2. **Silent truncation is a lie.** Any list cut short ends with an explicit,
   attention-colored notice: `… N more below`.
3. **A wall display must always answer "when did this last update."** The
   clock and the data ages are the last things a header or footer sheds under
   width pressure — before mode labels, before the version stamp.
4. **Chrome that reports state must not lie.** If a flag overrides a preset's
   numbers, the chrome says `custom`, never the preset's name.
5. **Rank by cost of ignoring, not by recency.** Running first, then failed
   (a failure must not scroll away), then stuck, then everything else by
   freshness.
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
11. **Inherit the terminal's theme.** No hex values in a TUI — foreground
    roles on the terminal's own background, so the product looks native in
    every palette the user already chose.

## Semantic color roles

Six roles, named by meaning. TUIs map them to ANSI colors; the web dialect
maps them to tokens. No product invents a seventh role without updating this
document.

| Role       | TUI color                  | Web token   | Used for |
|------------|----------------------------|-------------|----------|
| ok / live  | green                      | `--ok`      | processing, fresh (< 5 min), passing CI |
| attention  | yellow                     | `--warn`    | needs input, dirty tree, context ≥ 70%, truncation notices |
| failure    | red                        | `--crit`    | errors, failed runs |
| chrome     | cyan                       | `--chrome`  | borders, pane titles, the product's own name, session names |
| identity   | bright blue, always bold   | `--ident`   | repo and project names |
| divergence | magenta                    | `--alt`     | behind base, shared state |
| secondary  | dim/default                | `--fg3`     | everything at rest |

Notes that carry meaning beyond hue:

- **Bold is live, dim is at rest.** Attributes do as much work as color.
- **Freshness threshold is 5 minutes** (`FRESH = 300`): younger renders ok/bold,
  older renders secondary/dim.
- **Blue needs help.** Plain ANSI blue (4) is illegible on common dark
  palettes; request xterm-256 index 12 when 16+ colors exist, and always pair
  blue with bold so 8-color terminals brighten it.
- Selection is the one painted background: black on cyan.

## Glyph vocabulary

| Glyph | Codepoint | Meaning |
|-------|-----------|---------|
| `●`   | U+25CF | live / running (also the busy tick beside the clock) |
| `◉`   | U+25C9 | contested — two live sessions in one tree (shape carries the meaning; the dot is colored by status, not red) |
| `○`   | U+25CB | queued / pending / idle |
| `✓`   | U+2713 | success |
| `✗`   | U+2717 | failure |
| `◍`   | U+25CD | stuck in queue (> 2 h) |
| `·`   | U+00B7 | separator; no-data fallback |
| `↑ ↓` | U+2191/93 | ahead / behind base (`~2 ↑1 ↓3` is the git cell) |
| `←`   | U+2190 | prefix for a failing check name |
| `…`   | U+2026 | elision — keep both ends around a `/` so long names stay distinct |

Pipe-safe surfaces (plain CLIs, `--json` companions) use ASCII only:
`+staged ~dirty ?untracked`, `^ahead vbehind`, `=` for clean.

## Chrome

- **Frame:** rounded light box drawing (`╭─╮ │ ╰─╯`) in chrome color; bold
  border when focused, dim when not — focus is signalled purely by border
  weight. Title inset two columns, uppercase, padded: `╭─ SESSIONS ─…`.
- **Header:** product name in chrome bold at column 1, `·`-separated stat
  chips, right-aligned modes and clock. Sheds fields in a fixed order; the
  clock goes last.
- **Footer:** key hints left, data ages middle-right (`updated 4s ago · gh 1m`),
  `v<version>` in the true corner. Under pressure the version drops whole
  before the ages truncate.
- **Overlays:** centered modal reusing the pane frame, right-justified chrome
  labels, dim `any key to close`.
- **Never write into the last column.** A glyph in the final cell wraps onto
  the next row's border and persists.

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
- [ ] The glyph vocabulary matches, including the ASCII set on pipe-safe output.
- [ ] Frames are rounded, titles uppercase and inset, focus = border weight.
- [ ] Loading, empty, and failed states are three visibly different things.
- [ ] Truncated lists end in an attention-colored `… N more` notice.
- [ ] Header and footer degrade in a designed order; quit and help hints
      survive every tier; the clock and data ages survive everything.
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

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
   attention-colored notice that names the way to the rest: `… N more below
   (j)`. Cell-level cuts get a marker too (`~`) where the dialect has one.
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
    every palette the user already chose. Honor `NO_COLOR`; wherever possible
    derive color from a colorless marker already on the line, so the display
    reads the same story without it.
12. **A number whose denominator changed must say so.** A filtered view
    renders `7 of 41 tree(s) [filter: uncommitted]`, and fleet-wide counts
    are computed over the unfiltered fleet — a count must never silently
    change meaning.
13. **Attention has a grace period.** A signal that is real but young is
    listed quietly; only past a stated age does it take the attention color.
    Anything contested or failing is loud from frame one. This is what keeps
    an attention color from becoming wallpaper.
14. **Loading is stable.** Cells fill in place in discovery order — a loading
    table must not regroup rows as results land. And loading *animates*,
    on interactive surfaces only: one-shot/pipe output stays byte-stable so
    it can be snapshot-tested.
15. **One problem, one row.** N instances of the same underlying problem
    (three sessions in one contested tree) collapse into one ranked row, and
    a count that aggregates carries its worst age: `3 need you (12m)`.
16. **"Unseen" is not "down."** A source that was never configured or never
    answered here renders as a dim unknown (`off?`), distinct from a failure
    that was up and stopped answering (`DOWN`). Unknown must not collapse
    into either ok or failure.

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

### Two dialects, one vocabulary

The flock ships in two glyph dialects, both conforming:

- **Unicode dialect** (leghorn): the full table above, plus rounded frames.
- **ASCII dialect** (legbar, roost, git-roost): the pipe-safe set everywhere,
  `...` for elision, `[###---]` bars — a stated position, not drift; block
  drawing and dot glyphs mojibake in the Windows console these tools live in.

A product picks one dialect and holds it everywhere — a lone `…` in an
otherwise ASCII file violates its own stance. The *semantics* (what a glyph
slot means, what color it takes) are shared across dialects; only the
codepoints differ.

## Chrome

- **Frame** (multi-pane products): rounded light box drawing (`╭─╮ │ ╰─╯`) in
  chrome color; bold border when focused, dim when not — focus is signalled
  purely by border weight. Title inset two columns, uppercase, padded:
  `╭─ SESSIONS ─…`. Single-pane and ASCII-dialect products render the same
  slot as a bold uppercase title (plus a rule where one helps); focus, where
  it exists, is a marker or reverse-video row, and reverse must be re-armed
  after every inner reset.
- **Header:** product name in chrome bold at column 1, `·`-separated stat
  chips, right-aligned modes and clock. Sheds fields in a fixed order; the
  clock goes last.
- **Footer:** key hints left, data ages middle-right (`updated 4s ago · gh 1m`),
  `v<version>` in the true corner. Under pressure the version drops whole
  before the ages truncate.
- **Overlays:** centered modal reusing the pane frame, right-justified chrome
  labels, dim `any key to close`.
- **Never write into the last column.** A glyph in the final cell wraps onto
  the next row's border and persists. In SGR-dialect products, enforce this
  with a visible-length primitive (`visible_len`/`clip_ansi`) — `len()` on a
  colored string is the recurring bug.
- **Chrome must not move when it changes.** A mode indicator keeps a fixed
  width in every state (`ARMED` / `off  `), so a state flip never reflows the
  line around it.

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
- [ ] The glyph vocabulary matches the product's declared dialect (Unicode or
      ASCII), held consistently everywhere, with the ASCII set on pipe-safe
      output.
- [ ] Titles are uppercase; multi-pane products frame with rounded box drawing
      and signal focus by border weight; flat products use bold titles and a
      marked or reversed row.
- [ ] Loading, empty, and failed states are three visibly different things,
      and loading fills in place without regrouping.
- [ ] Truncated lists end in an attention-colored `… N more` notice that
      names the key or remedy that reaches the rest.
- [ ] Filtered counts state both numerators; aggregate counts carry their
      worst age.
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

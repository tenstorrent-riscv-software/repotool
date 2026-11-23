# repotool design

`repotool` (CLI: `rt`) is a lightweight meta-repo coordinator. TOML manifests
committed to a parent repo declare a set of child Git repos, each with its own
placement `path`, tracked `branch`, and pinned `rev`. `repotool` clones and
aligns those children and provides visibility, reset, fast-forward and
snapshot commands. It is positioned as a lighter-weight alternative to Google's
`repo` and Zephyr's `west`; see [How repotool compares](comparison.md).

This document records the design decisions and the reasoning behind them — the
things a rewrite, or a significant change, has to preserve. It is not a
command reference: user-facing behaviour is documented in the README, and
implementation detail in `AGENTS.md`. The code is the source of truth for
both.

---

## Implementation decisions

These are locked in. Revisit only with explicit reason.

| Area | Choice |
|---|---|
| CLI framework | `argparse` (stdlib) |
| Third-party deps | **None at runtime.** A vanilla Python 3.12 install runs `rt` with zero `pip install`. Dev extras (`pytest`, `ruff`, `mypy`) live under a `[dev]` optional dependency. |
| Minimum Python | 3.12 (Ubuntu 24.04 system Python) |
| TOML parsing | `tomllib` (stdlib) |
| Git interaction | `subprocess` + `git` CLI. No `GitPython`. |
| Colour rendering | Bare ANSI escape codes, suppressed when stdout isn't a TTY or `NO_COLOR` is set. No `rich`. |
| Project layout | `pyproject.toml` for dev scaffolding, but the **shipped artifact is a single-file `repotool.py`** that can be copied into any meta-repo and run without install. |
| Test framework | `pytest`. Tests build real on-disk git repos in `tmp_path`; no mocks. |
| Concurrency | `concurrent.futures.ThreadPoolExecutor`. Output across threads is serialised with a lock. |
| Manifest files | `.repotool.toml` and/or `*.toml` files in `.repotool/`, all loaded together (`.repotool.toml` first, then the directory's files by name). |

### Anchoring

All commands resolve the meta-repo via `git rev-parse --show-toplevel`. This
is what makes worktrees of the meta-repo work: each worktree gets its own
isolated child clones at its own root. Do **not** walk upward from CWD looking
for a parent manifest; do **not** anchor to CWD.

### No global state

No `~/.config` database, no daemon, no lock file. Everything lives in the
manifest files, so the meta-repo's state is versioned alongside the code that
depends on it.

---

## Manifest model

- **`branch` + `rev`.** `rev` is the exact revision `sync` and `reset` align
  to (SHA, tag, or branch name); `branch` is the context other commands need —
  the target of `fast-forward` and `bump`, and the branch `snapshot` checks
  remote reachability against. A pin alone can't be fast-forwarded; a branch
  alone isn't reproducible. Together they're both.
- **Loose and unpinned modes.** A branch name in `rev` (loose) or no `rev` at
  all (unpinned) is allowed, so a manifest can start out tracking tips and be
  pinned later. `snapshot` replaces either with a real SHA.
- **Shallow is not a manifest property.** `sync --depth` exists for CI, where
  full history is too expensive, but it isn't a per-repo manifest key: shallow
  is a tactic of a particular run, not a property of the project.
- **Manifest `reference` is strict.** A `reference = "<other>"` clone uses
  `git clone --reference`, not `--reference-if-able`: the manifest author
  declared the relationship, so a missing referenced clone is an error, not a
  silent fallback. `sync` clones in waves so referenced repos land first.
- **Submodules are opt-in per repo** (`submodules = true`), so children that
  don't use them don't pay the recursion cost.

---

## Command design

### `sync` is checkout-only

`sync` fetches and checks out `rev`. It never pulls, fast-forwards, resets or
moves a local branch, so it is always safe to run. Advancing is the job of
`fast-forward` (non-destructive) or `reset` (destructive).

The one refinement: when `rev` resolves to exactly the tip of
`origin/<branch>`, HEAD is attached to `<branch>` rather than left detached —
but only when that doesn't move an existing local branch (create `<branch>` at
the tip if absent, plain-attach if it already points there, else stay
detached).

An `origin` URL that differs from the manifest `url` produces a warning, never
an automatic `remote set-url`: the user may have repointed it deliberately.

**Rev overrides** (`--rev NAME=REF`, `RT_REVS`) let a CI job build a specific
ref of a child without editing the manifest. They are honoured only by `sync`,
check out `FETCH_HEAD` detached, never move a branch and never touch the
manifest — an override is a property of one run. An `RT_REVS` entry for a repo
outside the sync set is ignored, because CI jobs sync subsets while the
pipeline variable applies to all of them; the same entry on the command line
is an error, since it can only be a mistake.

### `status`

Informational: never fetches, always exits 0.

The comparison columns are headed `vs Manifest` and `vs Upstream` because they
name what is being compared against. Earlier framings (`Drift`, `Manifest`,
`Upstream`) were ambiguous — `Manifest: 377 ahead` parses as "the manifest is
377 ahead", whereas `vs Manifest: 377 ahead` reads unambiguously as "current is
377 ahead of the manifest". Each row reads left to right as a sentence: *the
manifest pins this branch at this rev; my HEAD is at this rev; compared to the
manifest that's X; compared to upstream it's Y.*

The two comparisons are independent, so common workflow states are visible at
a glance:

| State | vs Manifest | vs Upstream |
|---|---|---|
| Fast-forwarded but not snapshotted | `N ahead` | `in-sync` |
| Stale pin | `in-sync` | `N behind` |
| Local commits not on origin | `N ahead` | `N ahead` |
| True divergence | ahead/behind | `diverged` |

The `Status` cell ranks `dirty` above `untracked files` above `local branches`.
Untracked files outrank local-only branches because they block `snapshot`,
whereas local-only branches are informational; showing the latter while hiding
the former would mask why a snapshot was refused.

### `reset`

Destructive by design: `reset --hard` + `clean -fd` + `checkout -B <branch>`.
The one protection is against losing *commits*: if HEAD (or, with
`submodules = true`, any submodule's HEAD) has commits not on the target, the
repo is skipped unless `--force` is given. Uncommitted changes are not
protected — discarding them is the point of the command. `--force` is a flag,
not an interactive prompt, so `reset` stays scriptable.

### `fast-forward`

Non-destructive complement to `reset`. A detached HEAD is allowed, and
advancing it also attaches it to `<branch>` — detached is the normal state
after `sync` against an older `rev`, so the common case heals itself. It
refuses to move a local `<branch>` that has commits not on origin: silently
rewriting a branch the user may have work on is never the right call.

**Named-strict rule** (shared with `bump`): in bulk mode a skipped repo is
informational; when repos are named explicitly, a skip is a failure of intent
and the run exits non-zero.

### `bump`

`fast-forward` + `snapshot` fused, for an automated update-proposal job. It
exists so manifest knowledge — which pins are behind, whether the move is a
fast-forward, what changed — lives in `rt` rather than being reassembled by CI
shell from `sync` + `ffwd` + `snapshot` + hand-rolled `git log` /
`merge-base`. It deliberately knows nothing about CI or forges: no branch
names, no commit messages, no pushing. It shares `fast-forward`'s preflight so
the two sets of safety rules can't drift.

Two preflight skips — HEAD ahead of the tip, and attaching would rewrite a
local `<branch>` — guard only the *advance*, so `bump` holds them rather than
returning immediately:

1. An upstream rebase produces both: a freshly-synced clone on the
   now-orphaned pin is indistinguishable from one carrying local commits. The
   pin/tip ancestry test therefore runs first and `diverged` takes precedence
   — otherwise a nightly job reports a yellow skip and exits 0 while the pin
   falls further behind.
2. A clone already at the tip needs no advance, so the guard protects nothing:
   the manifest bump proceeds and neither HEAD nor the local branch is
   touched. `fast-forward` must still skip that case (attaching would move the
   branch); `bump` need not.

Only `rev` is rewritten: `branch` is this command's input, whereas for
`snapshot` it's an output.

The `--json` document is a **compatibility surface** with **no version
field**, deliberately: an in-band marker buys detection, not migration, and
buys nothing when the consumer vendors `repotool.py` beside the script that
parses it — they upgrade together. The contract is *additive changes only*
(new keys; consumers ignore unknown ones). A genuinely different shape must
arrive as a new flag, the way git versioned `--porcelain=v2`. Do not introduce
a `format` key: it invites bumping it as a substitute for that new flag.

### `snapshot`

Pins each child to its current `(branch, HEAD SHA)`. Intended workflow:
`rt fast-forward` → `rt snapshot` → commit the manifest.

- **All or nothing.** If any selected repo fails a check, no manifest change
  is written.
- **Remote reachability.** By default HEAD must be contained in
  `origin/<branch>`, so a pin never names a commit only one person has.
  `--no-verify-remote` is the explicit opt-out.
- **Detached at the tip is allowed.** Besides an attached HEAD, `snapshot`
  accepts a HEAD detached at exactly `origin/<branch>` — the state after a
  force-push followed by `git checkout origin/<branch>`.
- **Line-based rewrite.** The manifest is edited by substituting `branch` and
  `rev` values in place, preserving comments, whitespace, key order and quote
  style. Round-tripping through a TOML writer would lose all of those.

### `check`

A CI preflight that must work before anything is cloned: it validates the
manifest and runs one `git ls-remote` per repo, never cloning or fetching.
`ls-remote` only advertises ref tips, so a SHA pinned below the branch tip
can't be confirmed and is reported `unverified` rather than failing — that's
the normal case, not an error. `check = false` excludes repos CI can't reach
(typically private ones); naming such a repo explicitly overrides the
exclusion.

### Shell completion

The completion scripts are static shims that call a hidden `rt __complete`
entry point, which introspects the argparse parser. New commands and options
therefore complete without completion-specific changes. Completion must never
break the shell: `__complete` always exits 0 and prints nothing on error.

---

## Submodules

With `submodules = true`, submodules follow the same checkout-only,
pin-following model one level down: they land on the SHAs the child's
checked-out commit pins (`submodule update`, never `--remote`).

- `git submodule sync` runs before the update so that a checked-out commit
  that repoints a submodule at a new URL is honoured.
- For the same reason, the superproject fetch passes
  `--no-recurse-submodules`: git's on-demand recursion would otherwise fetch
  newly-referenced submodule commits using the stale pre-sync URL. Submodule
  fetching is deferred to the update.

---

## Output and failure reporting

- Concurrent writes are serialised by a single lock; lines never interleave.
- Commands that fail for some repos end with an `error: <command> failed for
  N of M repo(s): <names>` line on stderr. Per-repo detail lines can look
  innocuous (an SSH-auth failure starts with git's `Cloning into '...'`), so
  the summary makes the verdict unmissable.
- The per-repo error detail prefers git's `fatal:` / `error:` lines, then
  SSH-auth markers, over the first stderr line, which is usually progress
  chatter.
- On `SIGINT`, pending work is cancelled but in-flight `git` subprocesses run
  to completion — terminating mid-clone leaves junk directories. Exit status
  is 130.

---

## Worktrees of the meta-repo

Because the tool anchors to `git rev-parse --show-toplevel`:

1. Inside `/home/user/project`, children land at their declared `path`s under
   `/home/user/project/`.
2. Inside a worktree at `/tmp/fix-bug`, `rt sync` detects the new root and
   clones a fresh, isolated set of children under `/tmp/fix-bug/`.
3. `sync` looks at the primary worktree via `git worktree list --porcelain`
   and clones each missing child with `--reference-if-able
   <primary>/<repo.path>`, so a secondary worktree bootstraps quickly without
   re-downloading objects already present in the primary. It never references
   a clone in another secondary worktree — those can be removed at any time;
   the primary is the stable choice.

**Caveat (the standard `--reference` tradeoff):** if the primary worktree's
child is later removed, clones that referenced it can lose object data. To
make a child self-sufficient, run `git -C <child> repack -a -d`. Accepted to
avoid full duplication.

`--depth` suppresses all object sharing for the run: shallow clones and
alternates work against each other.

---

## Out of scope

- Pruning child directories when a `[[repo]]` is dropped from the manifest
  (`status` lists them as orphans; the user removes them).
- Auto-rewriting `origin` when `url` changes in the manifest (warn only).
- Manifest-editing subcommands beyond `snapshot` and `bump`; hand-edit the
  TOML for anything else.
- Pushing a tag at snapshot time to protect pinned SHAs from garbage
  collection on the remote. Branch deletion or force-push on the remote can
  make a pinned SHA unreachable later, which no SHA-only pinning scheme
  avoids.

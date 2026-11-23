# AGENTS.md

This file provides guidance to coding agents working with code in this
repository.

## Repository status

Implemented and shipping. The tool is `rt` (single-file `repotool.py`), with
nine subcommands: `sync`, `status`, `reset`, `fast-forward`, `bump`, `snapshot`,
`check`, `show-changes`, `completions`. Tests live in `tests/` and use real
on-disk git repos (no mocks). For design decisions and the reasoning behind
them, see `docs/design.md`. The code is the source of truth for
implementation; `docs/design.md` is the rationale a rewrite would have to
preserve. When a change alters a design decision, update it there.

## What `repotool` is meant to do

A lightweight meta-repo coordinator: TOML manifests (`.repotool.toml` and/or
`.repotool/*.toml`) committed to a parent repo declare a set of child Git repos,
each with its own placement `path`, tracked `branch`, and pinned `rev`.
`repotool` (invoked as `rt`) clones/aligns those children into the meta-repo and
provides visibility + reset commands. Positioned as a lighter-weight
alternative to Google's `repo` and Zephyr's `west` (see
`docs/comparison.md`).

The `branch` + `rev` pair ties the meta-repo's state to specific revisions of
each child: `rev` is what `sync`/`reset` align to (SHA, tag, or branch name);
`branch` is the context for `fast-forward` (target of `merge --ff-only`) and
`snapshot` (remote-reachability check is against `origin/<branch>`). A branch
name in `rev` is allowed (loose mode); `snapshot` replaces it with a real SHA
the first time you run that flow.

### Nine commands

Every command's positional repo arguments accept either a repo's `name` or its
manifest `path` (`rt sync dtc` ≡ `rt sync src/dtc`) — paths are unique by
validation, and they're what build scripts and tab completion naturally produce.
On the unlikely cross-repo collision (one repo's name equals another's path),
the name wins. Resolution lives in `_filter_repos`; shell completion offers both
forms.

- `sync` — fetch and check out the manifest's `rev` for each child.
  Checkout-only (never moves local branches). When the resolved `rev` (a
  SHA/tag) sits exactly at the tip of `origin/<branch>`, HEAD is attached to
  `<branch>` instead of left detached — but only when that's free of moving an
  existing local branch (no local `<branch>` → create it at the tip tracking
  origin; local `<branch>` already at the target → plain attach; local
  `<branch>` elsewhere → stay detached). A `rev` below the branch tip, or a
  tag/SHA off the branch, still lands on a detached HEAD. By default operates on
  already-cloned children only; pass `--all` to clone manifest entries that
  aren't yet on disk, or name them explicitly. Exits non-zero with a helpful
  message when the filtered set would be empty (e.g. fresh checkout, no
  `--all`). Parallelized via `ThreadPoolExecutor`. CI-oriented flags: `--depth
  N` passes through to `git clone --depth N` (also adds `--branch <repo.branch>`
  so non-default-branch revs resolve) — first-clone only, suppresses manifest
  `reference` and peer auto-discovery for that run; `--https` rewrites all repo
  URLs to `https://` in memory for the run (manifest on disk is untouched).
  **Rev overrides:** `--rev NAME=REF` (repeatable) and the `RT_REVS` env var
  (`NAME=REF` entries separated by whitespace/commas; CLI wins per repo) replace
  a repo's manifest `rev` for this run only. Only `sync` honours them;
  `status`/`reset`/`fast-forward`/`bump`/`snapshot`/`check`/`show-changes`
  ignore `RT_REVS`. The ref is fetched by name (`git fetch [--depth N] origin
  <REF>`, plus `--no-recurse-submodules` for `submodules = true`; `--depth` only
  when the clone is already shallow, so an existing full clone is never
  truncated) and checked out as `FETCH_HEAD` **detached**; it never goes through
  `checkout <rev>`, never attaches via `_attach_to_branch_if_at_tip`, never
  moves a local branch, and never touches the manifest. Validation (malformed
  entry, unknown name, duplicate within one source, abbreviated SHA, leading
  `-`) runs before any git op; an `RT_REVS` entry for a repo outside the sync
  set is ignored, while a `--rev` one errors. Overridden repos print an
  `overrides:` line and
  `(override)` in their detail. When the pinned `rev` isn't in what was cloned
  (the SHA sits below a `--depth` window, or an upstream rebase moved it
  deeper), the failed checkout triggers a targeted `git fetch origin <rev>` +
  re-checkout — this recovers any rev still reachable from *some* remote ref
  without deepening the whole history. If that fetch also fails (the rev is
  unreachable from every ref — a rebase/force-push orphaned and GC'd it), sync
  fails with a `rev <sha> not found on origin … likely rebased away; update the
  manifest` diagnosis instead of a raw checkout error. **Single-branch refspec
  widening:** on an existing clone, before the fetch `_ensure_branch_tracked`
  checks the clone's `remote.origin.fetch`; if it's single-branch (no
  `refs/heads/*` wildcard — the state left by an earlier `--depth` clone, which
  adds `--branch <repo.branch> --single-branch`) and doesn't already track the
  manifest `branch`, it runs `git remote set-branches --add origin <branch>` so
  the fetch populates `origin/<branch>`. This heals `status` showing `no
  remote` under `vs Upstream` after the manifest `branch` changes (a plain
  fetch on a single-branch clone would never see the new branch). No-op on a
  wildcard clone or when the branch is already tracked. `reset` and
  `fast-forward` share the same pre-fetch call.
- `status` — the default when no subcommand is given. Table with columns
  `Repository | Manifest Branch | Manifest Rev | Current Rev | vs Manifest | vs
  Upstream | Status`, with a leading `meta` row for the meta-repo itself (when
  no repos are named). `vs Manifest` = HEAD vs manifest `rev` (`in-sync`, `N
  ahead`, `N behind`, both, or the undetermined `rev absent` / `diverged`, each
  with a stderr warning); `vs Upstream` = HEAD vs `origin/<branch>` as of the
  last fetch (`no remote` when that ref doesn't exist). Unpinned entries show
  `unpinned`. Followed by an `Orphan directories:` list of git checkouts not
  declared in the manifest. The Status cell (`_state_cell`) has a precedence,
  highest first: `dirty` (modified/staged tracked files) → `untracked files`
  → `local branches` → `clean`. Untracked files outrank `local branches`
  because they block `snapshot` (its clean check rejects untracked files)
  while local-only branches are only informational. Informational, always
  exits 0.
- `reset` — destructive realignment. `fetch` + resolve `rev` (prefer
  `origin/<rev>` then local) + `reset --hard` + `clean -fd` + `checkout -B
  <branch>` (moves the branch label to the target). Refuses to discard local
  commits ahead of the target without `--force`. Like `sync`, defaults to
  already-cloned children only; `--all` includes not-yet-cloned entries, which
  then surface as yellow `skipped: not synced` lines (not failures — uncloned
  children are a normal state in the partial-checkout workflow). Also like
  `sync`, exits non-zero when the filtered set would be empty. Shares `sync`'s
  pre-fetch single-branch refspec widening (`_ensure_branch_tracked`), so a
  changed manifest `branch` resolves against `origin/<branch>` after the fetch.
- `fast-forward` — `git fetch` (preceded by the same single-branch refspec
  widening as `sync`/`reset`, which matters on the detached-HEAD path where a
  changed manifest `branch` is otherwise untracked) then advance to
  `origin/<branch>` per child when safe. Attached + same branch → `merge
  --ff-only`. Detached HEAD that's an ancestor of `origin/<branch>` → `checkout
  -B <branch> origin/<branch>` (advances *and* attaches, so the common "manifest
  pinned an older SHA, sync left me detached" case heals itself). Skipped (not
  failed) when: the child isn't cloned, tree is dirty, current branch differs
  from the manifest branch, HEAD has commits not on `origin/<branch>`, or HEAD
  is detached but a local `<branch>` ref has commits not on origin (would
  silently rewrite the branch).
  After a successful advance, output reminds the user to `rt snapshot`.
  **Named-strict rule:** in bulk mode (`rt ffwd` with no names) skips don't
  fail; when repos are named explicitly, skips count as failures of intent and
  the run exits non-zero.
- `bump` — `fast-forward` + `snapshot` fused for an automated update-proposal
  job ("is any child behind its branch tip? bump the manifest so CI can open an
  MR"). Defaults to already-cloned children (never clones — that's `sync`); runs
  the shared `_ff_preflight` (factored out of `fast-forward`, so the safety
  rules can't drift), then compares the manifest `rev` with `origin/<branch>`:
  resolves to the tip → `up-to-date` (manifest untouched); resolves but isn't an
  ancestor of the tip → `diverged` (upstream rebased; clone and manifest
  untouched, exit non-zero); otherwise → `updated`, advancing the clone via the
  shared `_ff_advance` and rewriting `[[repo]].rev` to the new tip. A pin
  missing locally is fetched by name (`git fetch origin <rev>`, `_resolve_pin`)
  so the ancestry verdict is authoritative; if it's still absent the update
  proceeds with a `log unavailable` note (shallow CI clones must not fail). The
  two preflight skips that guard only the advance — `ahead` and
  `local-branch-ahead` (`_ADVANCE_GUARD_REASONS`) — are held, not returned,
  because each can fire on a repo that should still be acted on: (a) an upstream
  rebase produces them (a freshly-synced clone on the orphaned pin looks like it
  carries local commits), so the ancestry test runs first and `diverged`
  **overrides** them; (b) when `head_sha == target_sha` there is no advance to
  protect, so the manifest bump proceeds and neither HEAD nor the local branch
  label is touched — the "detached at `origin/<branch>` while a local branch
  holds unrelated work" shape, which `fast-forward` must still skip (attaching
  would move that label) but `bump` need not. A held skip only becomes a real
  `skipped` when the pin is fine *and* the clone would actually have to move.
  Only `rev` is rewritten — `branch` is this command's input, unlike `snapshot`
  where it's an output — via `_apply_manifest_updates` (also extracted from
  `snapshot`), once, on the main thread after the pool joins. `-n/--dry-run`
  reports without touching clone or manifest; `--json` emits a single document
  (`{dry_run, manifest_updated, repos:[…]}`, repos in manifest order, keys
  always present, absent values `null`) that **is a compatibility surface parsed
  by CI** — **additive changes only** (new keys), and there is intentionally
  **no `format`/version field**: a marker only buys detection rather than
  migration, and buys nothing when the consumer vendors `repotool.py` next to
  the script parsing its output (they upgrade together). A genuinely
  incompatible shape must come as a new flag (git's `--porcelain=v2` precedent),
  never as a bumped marker — don't reintroduce one. `--json --verbose` is a
  usage error. Skips follow `fast-forward`'s named-strict rule. Superproject
  only: a `submodules = true` child needs a follow-up `rt sync`.
- `check` — CI preflight: validate the manifest and contact every declared
  remote without cloning or fetching anything. `load_manifest` runs the full
  structural validation, then one `git ls-remote <url>` per repo (parallelized)
  proves the URL is reachable/authorized and lists the advertised refs.
  Existence is then checked locally: the manifest `branch` must be a remote
  head; the `rev` is verified when it's a tag or branch name (must be present as
  a tag/head) and when it's a SHA that happens to sit at the branch tip. A SHA
  `rev` pinned *below* the tip (the common case) can't be confirmed without a
  fetch, so it's reported `unverified` (yellow) — not a failure, exit stays 0.
  Failures: unreachable URL, missing branch, or a named (tag/branch) rev absent
  from the remote → non-zero exit with the usual `error: check failed for N of M
  repo(s): <names>` summary. Operates on all manifest entries regardless of
  on-disk state (nothing may be cloned in CI); name repos to narrow. Repos
  marked `check = false` in the manifest (e.g. private repos CI can't reach) are
  excluded from bulk runs and reported as dim `excluded` lines; naming such a
  repo explicitly overrides the exclusion. Honors `--https` (in-memory URL
  rewrite, same as `sync`), `-j/--jobs`, `-v/--verbose`.
- `show-changes` — show the commits between each child's manifest `rev` and its
  current HEAD. Per repo: a `rev..head (N ahead, M behind)` header, then one
  line per commit from a single `git log --left-right` over the symmetric
  difference — `+` marks commits on HEAD but not the manifest rev, `-` the
  reverse. `-s/--shortlog` swaps the per-commit lines for `git shortlog
  --no-merges` output (split into ahead/behind sections when diverged). Purely
  local — no fetch: a `rev` that doesn't resolve locally is reported (yellow),
  an unpinned entry as nothing-to-compare (dim). Like `status`, informational:
  per-repo problems print as colored lines, exit stays 0; the only non-zero exit
  is the empty-filtered-set case shared with `sync`/`reset`. Defaults to
  already-cloned children; a repo named explicitly but not cloned prints `not
  synced` rather than failing.
- `completions` — print a tab-completion script for `bash`, `zsh`, or `fish` on
  stdout (install one-liners in the subcommand's `--help` epilog and
  `docs/workflow.md`).
  The scripts are static shims: at completion time they call the hidden `rt
  __complete -- <words…>` entry point, which computes candidates by
  introspecting the argparse parser built by `_build_parser()` — subcommands
  (aliases included), per-command options, option values — so new commands and
  flags complete without any completion-specific changes. Repo-name positionals
  (`names`) complete from the manifest, with already-typed names excluded.
  `__complete` is intercepted in `main()` before argparse, so it never appears
  in `--help`; it always exits 0 and emits nothing on any error (no git repo, no
  manifest, bad TOML) — completion must never break the shell. Protocol: the
  shell passes all command-line words with the token being completed last;
  candidates come back one per line as `value<TAB>help`, and a bare `:file` line
  requests the shell's filename completion (triggered by `metavar="PATH"`,
  currently only `sync --reference`).
- `snapshot` — pin each child to its current branch and HEAD SHA. Requires a
  clean tree and either attached HEAD or detached at exactly
  `origin/<repo.branch>` (the post-force-push workflow). Verifies HEAD is
  contained in `refs/remotes/origin/<current_branch>` specifically (skip with
  `--no-verify-remote`). Rewrites both `branch` and `rev` lines in the manifest
  via line-based substitution (preserves comments). Uncloned children are
  skipped rather than aborting the run.

## Architectural decisions already locked in

These are non-obvious choices from `docs/design.md` that future changes should
respect unless explicitly revisiting:

- **Anchor to `git rev-parse --show-toplevel`, not CWD.** This is what makes
  worktrees of the meta-repo work: each worktree gets its own isolated child
  clones. Don't replace this with `Path.cwd()` or a config-file search. Don't
  walk upward from CWD looking for a parent manifest.
- **Shell out to `git` via `subprocess`; do not use `GitPython`.** Rationale:
  keep deps light and avoid fighting a library abstraction.
- **Standard-library TOML (`tomllib`).** Python 3.12 minimum. No `tomli` /
  `toml` third-party deps.
- **No runtime third-party deps.** Colors are emitted via ANSI escape codes when
  stdout is a TTY (respects `NO_COLOR`); when piped, markup is stripped. Earlier
  iterations used `rich` as an optional extra — dropped because it was overkill
  for the small color/markup surface we actually need.
- **No global state.** No `~/.config` database — everything lives in the
  meta-repo's manifest files (`.repotool.toml` and/or `.repotool/*.toml`) so
  the manifest is versioned alongside the code that depends on it.
- **Single-file shippable artifact.** `repotool.py` must remain a self-contained
  file that can be copied into any meta-repo and run without `pip install`. The
  `pyproject.toml`, `justfile`, `tests/`, and `.pre-commit-config.yaml` are dev
  scaffolding, not part of what users see.
- **`--reference` clone optimization landed.** Three paths: (1) automatic peer
  discovery against the *primary* worktree only (the main working tree, always
  listed first by `git worktree list --porcelain`) → `--reference-if-able
  <primary>/<repo.path>`; never references a clone in another secondary
  worktree, which could be removed and break the referencing clone's alternates;
  (2) explicit manifest `reference = "<other-name>"` → strict `--reference
  <other>` with wave-based clone ordering; (3) ad-hoc CLI `--reference PATH`
  (single-repo only). All three suppressed by `--depth` (shallow + alternates
  don't mix).
- **Failure reporting.** `sync` / `reset` / `fast-forward` / `bump` / `check`
  end with an `error: <command> failed for N of M repo(s): <names>` line on
  stderr when anything failed — the per-repo `detail` lines can look
  innocuous (an SSH-auth failure starts with "Cloning into '...'"), so the
  summary makes the verdict unmissable. `snapshot` already prints `aborting;
  manifest not modified` for its analogous failure case.
- **`GitError.error_line`** prefers `fatal:` / `error:` lines, then SSH-auth
  markers (`Permission denied`, `ssh:` prefix), then the last non-progress /
  non-`remote:` line — not the first stderr line, which is usually git's
  "Cloning into '...'" progress chatter.

## Manifest shape

```toml
[[repo]]
name   = "api-service"
url    = "git@github.com:org/api-service.git"
path   = "services/api"     # relative to the meta-repo root
branch = "main"             # the branch this rev lives on
rev    = "abc1234…"         # SHA, tag, or branch name

[[repo]]
name   = "web-ui"
url    = "git@github.com:org/web-ui.git"
path   = "ui"
branch = "release-2.x"
rev    = "v2.4.0"
```

Each `[[repo]].path` is the placement relative to the meta-repo root — there is
no shared parent directory. Validation rejects absolute paths, `..` traversal,
paths that escape the root, duplicate paths, and paths that are ancestors of one
another (e.g. `api` + `api/foo`).

Manifest entries may also live in `.repotool/*.toml` (each file declaring one or
more `[[repo]]`), loaded alongside `.repotool.toml` if present. Names and paths
are unique across all files; `load_manifest` returns repos in load order, each
with `Repo.source` set to its file. Rewrites (`snapshot`, `bump`) address
`[[repo]]` blocks per file via `_apply_manifest_updates`.

`rev` is optional. When omitted, the entry is *unpinned*: `sync` clones at
`branch` (attached HEAD, no SHA pin), `reset` realigns to `origin/<branch>`,
`fast-forward` advances `<branch>` as usual, and `snapshot` writes a `rev` line
into the manifest pointing at the current HEAD. `branch` remains required.

`reference = "<other-name>"` (optional) makes the initial clone of this repo
share git objects with another repo in the same manifest via `git clone
--reference`. Validated for existence, self-reference, and cycles at manifest
load. `sync` clones the referenced repo first, then the referrer.

`submodules = true` (optional, default false) makes `sync` and `reset` align
this child's git submodules after the checkout, via `git submodule sync
--recursive` then `git submodule update --init --recursive` (no `--remote` —
submodules land on the SHAs the child's checked-out commit pins, mirroring
repotool's checkout-only, pin-following model one level down). The `submodule
sync` copies each submodule's `.gitmodules` URL into `.git/config` first, so a
checked-out commit that repoints a submodule at a new remote is honored before
the update fetches — without it the update would fetch the stale configured URL
and fail. For the same reason the superproject `git fetch origin` on a
submodule-bearing repo passes `--no-recurse-submodules` (`_fetch_args`): git's
default on-demand recursion would otherwise try to fetch newly-referenced
submodule commits mid-fetch using the pre-sync URL; submodule fetching is
deferred to `_update_submodules` after the sync. Applied in both the clone and
existing-clone sync paths; a submodule failure fails that repo's sync (`sync`
uses a non-`--force` update, so a submodule with conflicting uncommitted changes
makes git refuse loudly rather than clobber). `reset` additionally passes
`--force` and runs `git submodule foreach --recursive git clean -fd` (its
top-level `clean -fd` doesn't descend into submodules). Under `--depth`, the
depth is passed through to the submodule fetch. **Commits-ahead guard extends
into submodules:** before the forced submodule update, `reset` checks each
initialized submodule for commits on its current HEAD not reachable from the SHA
the target superproject commit pins for it (`_submodules_ahead`); if any exist
it skips the repo with `ok=False` (`submodule(s) have commits not on the pinned
rev: … pass --force to discard`), mirroring the superproject's own
no-lose-commits protection. Uncommitted submodule changes are not guarded — like
the superproject, `reset` discards those without `--force`; only local *commits*
are protected. Only children that actually use submodules should set the key, so
others don't pay the recursion cost. Validated as a boolean at manifest load.

`check = true` (optional, default true) controls whether `check` contacts this
repo's remote. Set `check = false` to exclude a repo from the CI preflight —
typically a private repo that CI has no credentials for and would otherwise fail
on. In bulk mode (`rt check` with no names) excluded repos are reported as dim
`excluded (check = false)` lines (not run, don't affect the exit code); the run
can be all-excluded and still exit 0. Naming a repo explicitly (`rt check
<name>`) **overrides** the exclusion so you can still contact it on demand. The
key affects only `check` — `sync`/`reset`/`fast-forward`/`snapshot` ignore it.
Structural manifest validation still applies to `check = false` repos; only the
network contact is skipped. Validated as a boolean at manifest load.

`ignore = ["<path>", …]` is the one **top-level** key (not part of `[[repo]]`,
so in TOML it must precede the first `[[repo]]` table). It lists directory
subtrees to exclude from the orphan walk that `status`/`sync` print under
`Orphan directories:` — trees whose git checkouts are managed by another tool
(e.g. a west workspace mounted inside the meta-repo) and would otherwise be
flagged as orphans on every run. Each entry follows the same path rules as a
repo `path` (relative, no `..`, a strict subdirectory of the root). Validation
lives in `_parse_ignores`, called from both `load_manifest` (so `check` catches
a malformed `ignore` in CI, not lazily during the walk) and `load_ignores`
(which the two orphan-walk call sites pass into `_walk_orphans` — the walk
itself stays pure over its arguments rather than re-reading the manifest). The
walk prunes at each ignored directory (no descent), so nested clones under it
are skipped too. Affects only the orphan walk; every other command ignores it.

## Implementation choices

- **CLI:** `argparse` (stdlib).
- **License headers:** every source file starts with these SPDX headers,
  after the shebang if there is one, and `.github/workflows/spdx.yml` checks
  them on every pull request. Add them to new files:

  ```
  # SPDX-License-Identifier: Apache-2.0
  # SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
  ```

- **Tests:** `pytest`, in `tests/`, against real on-disk git repos built in
  `tmp_path` (no mocks).
- **Packaging:** `pyproject.toml` with a `[dev]` extra (`pytest`, `ruff`,
  `mypy`); no runtime extras. The distribution is named `tt-repotool`, because
  `repotool` on PyPI is an unrelated project; the module stays `repotool` and
  the command `rt`. Console-script entry point `rt = "repotool:main"`
  for `pip install -e .` development; the script remains directly runnable.
- **Pinned dependencies:** the `[dev]` tools are pinned to exact versions, and
  the GitHub Actions in `.github/workflows/` to full commit SHAs with a version
  comment, so a new upstream release can't change CI results on its own.
  `.github/dependabot.yml` proposes updates weekly. Keep new dependencies
  pinned the same way.
- **Dev workflow:** `just venv` to bootstrap, then `just test` to run all the
  checks, or `just lint` / `just typecheck` / `just pytest` individually.
  `just install_hooks` wires up `prek` (uses `.pre-commit-config.yaml`, which
  runs lint, typecheck and pytest on commit). The hooks require prek: the
  config uses its `repo: builtin`, which `pre-commit` doesn't support. Always
  run the `just` targets, not bare `python3 -m mypy` etc. — those use the
  system Python which doesn't have the dev tools installed. The venv lives in
  `.venv` unless `REPOTOOL_VENV` names another directory (one venv per Python
  version when a tree is shared between interpreters, e.g.
  `REPOTOOL_VENV=.venv-3.14 just venv test`).

## Documentation

`README.md` is a short overview meant to be read top to bottom; keep it that
way and put detail in `docs/`: `manifest.md` (file format and keys),
`commands.md` (per-command reference), `workflow.md` (usage, worktrees,
completion), `comparison.md` (vs submodules and other tools), and `design.md`
(decisions and rationale). A behaviour change should update the relevant page
in `docs/`, and the README only if the overview itself changes. Files in
`docs/` are licensed CC BY 4.0 (`LICENSE-DOCS`); everything else, including
the top-level Markdown files, is Apache 2.0.

## Markdown style

Wrap markdown docs (this file, README.md, `docs/`) at 80 columns where
feasible — long URLs, tables and code blocks can run over. Applies to new and
edited prose only; don't rewrap existing paragraphs you aren't otherwise
touching, it just adds diff noise.

## Commit messages

Use a scoped subject, `<scope>: <description>`, where the scope is the command
or area touched (`sync`, `status`, `manifest`, `docs`, `tests`, …), followed by
an optional body and footer separated by blank lines:

```
<scope>: <description>

[optional body]

[optional footer]
```

Don't add `Signed-off-by` lines, that is only for humans.

# Commands

Every command's `names...` arguments accept a repo's `name` or its manifest
`path`. See the [manifest reference](manifest.md) for the keys mentioned here.

## `rt sync [names...] [-a] [-j N] [-v] [--reference PATH] [--depth N] [--https] [--rev NAME=REF ...]`

Clone missing children, align existing ones to their `rev`. With one or more
positional `names`, only those repos are processed — each argument may be a
repo's `name` or its manifest `path` (`rt sync dtc` and `rt sync src/dtc` select
the same repo), so build scripts can point at the path they already know.
Without names, already-cloned children are synced; pass `--all` to include the
rest. If that leaves nothing to sync — a fresh checkout without `--all` — sync
exits non-zero and says so.

`--reference PATH` passes `--reference PATH` through to `git clone`, sharing
objects with a pre-existing reference clone. Only valid when exactly one repo is
being synced. (Without it, sync still shares objects with the meta-repo's
primary worktree via `--reference-if-able`.)

For each repo:
- **Missing**: `git clone` (using the primary worktree's matching clone as
  `--reference-if-able` when one exists — see
  [Worktrees](workflow.md#worktrees-of-the-meta-repo)) → `git checkout <rev>`.
- **Exists and is a git repo**: `git fetch origin` → `git checkout <rev>`. Sync
  is **checkout-only**: it never moves local branches and never advances HEAD
  beyond the requested rev. Use `rt fast-forward` or `rt reset` to change branch
  tips. If the clone is single-branch (from an earlier `--depth` sync) and the
  manifest `branch` has since changed, sync first widens the clone's fetch
  refspec (`git remote set-branches --add`) so the fetch picks up the new branch
  instead of leaving it stuck on `no remote`.
- **Exists but is not a git repo**: reported as failed, the rest continue.

A `rev` that is a SHA or tag usually leaves the child on a detached HEAD, as
`git checkout <rev>` would. When it sits exactly at the tip of
`origin/<branch>`, sync attaches HEAD to `<branch>` instead — creating the local
branch if it doesn't exist — unless a local `<branch>` already points somewhere
else, which sync never moves.

If the checkout fails because the pinned commit isn't in the clone (below a
`--depth` window, or moved deeper by an upstream rebase), sync fetches it by
name and retries. When that fails too, the commit is no longer reachable from
any ref on the remote, and sync reports it as likely rebased away so the
manifest can be updated.

Children with `submodules = true` have their submodules aligned after the
checkout; see [Optional keys](manifest.md#optional-keys).

If the manifest's `url` doesn't match the existing clone's `origin`, `sync`
warns and continues without rewriting the remote — fix it manually with `git
remote set-url`.

Flags:
- `-a`, `--all` — include manifest entries that aren't cloned yet, cloning
  them.
- `--depth N` — pass `--depth N` (and `--branch <branch>`) to `git clone`, for
  shallow clones in CI. Applies only to repos cloned by this run, and disables
  `reference` and automatic object sharing for the run, since shallow clones
  and alternates don't mix. Can't be combined with `--reference`.
- `--https` — rewrite every repo URL to `https://` for this run (in memory;
  the manifest on disk is untouched). For CI runners without SSH keys.
  Handles `git@host:path`, `ssh://`, `git://` and `http://` URLs.
- `--rev NAME=REF` — check out `REF` instead of the manifest `rev` for one
  repo; see [Rev overrides](#rev-overrides---rev-and-rt_revs).
- `-j N` — concurrent git operations (default 8).
- `-v` — print every `$ git …` invocation per repo with stdout/stderr.

Exits non-zero if any repo failed, ending with an `error: sync failed for N of
M repo(s): …` line on stderr.

### Rev overrides: `--rev` and `RT_REVS`

`--rev NAME=REF` (repeatable) checks out `REF` for repo `NAME` in place of its
manifest `rev`, for this run only. `NAME` is a repo name or path, as for
positional `names`. `REF` is anything `git fetch origin <REF>` accepts: a full
SHA (the usual CI case), a branch name (for example, a scheduled pipeline
testing a child's tip), a tag, or a full ref such as
`refs/merge-requests/12/head`. Abbreviated SHAs are rejected, since a remote
can't resolve them; that's any 7–39 character hex string containing a letter, so
an all-digit tag or branch such as `20261102` is still accepted.

The same overrides can come from the `RT_REVS` environment variable, formatted
as `NAME=REF` entries separated by whitespace and/or commas. CI pipelines set it
once, and every job that runs `rt sync` picks it up. The CLI wins over `RT_REVS`
for the same repo.

- An overridden repo is fetched by name and left on a **detached HEAD**. Sync
  never attaches it to a local branch and never moves one, and the manifest on
  disk is never modified.
- `RT_REVS` entries for repos outside this run's sync set are ignored, so jobs
  that sync a subset of repos can share one variable. A `--rev` entry for such a
  repo is an error.
- Malformed entries, unknown names, duplicate entries for one repo within a
  source, and short SHAs are all rejected before any git operation.
- Each run that applies overrides prints an `overrides: …` line, and the
  per-repo detail is marked `(override)`, so logs make it obvious when a run did
  not use the manifest revs.
- **Only `sync` honours overrides.** `status`, `reset`, `fast-forward`, `bump`,
  `snapshot`, `check` and `show-changes` ignore `RT_REVS` and don't accept
  `--rev`.

## `rt status [names...]` (default)

Informational dashboard. Never fetches; always exits 0. This is the default
action — running `rt` with no subcommand prints the status table. With
positional `names`, the table is restricted to those repos and the `meta` row +
`Orphan directories:` footer are omitted (they're global context that's
off-topic for a focused view).

```
            Manifest  Manifest  Current  vs        vs
Repository  Branch    Rev       Rev      Manifest  Upstream  Status
----------  --------  --------  -------  --------  --------  ------
meta                            a1b2c3d                      clean
linux       master    v6.6      4f1e2d7  in-sync   in-sync   clean
opensbi     master    v1.4      8a9b0c1  in-sync   5 behind  dirty
```

Each row reads left-to-right as: the manifest pins this `Branch` at this `Rev`;
my current HEAD is at this `Rev`; comparing that to the manifest gives `vs
Manifest`; comparing it to upstream gives `vs Upstream`.

- **Manifest Branch / Rev** — what the manifest says.
- **Current Rev** — the short SHA of HEAD. Always a SHA (never a branch name) so
  it's directly comparable to `Manifest Rev`.
- **vs Manifest** — Current vs manifest `rev`: `in-sync`, `N ahead`, `N behind`,
  `N ahead / M behind`, or one of two undetermined states (each also printed as
  a warning): `rev absent` when the rev doesn't resolve locally (HEAD can't be
  compared — sync state genuinely unknown), `diverged` when it resolves and
  differs from HEAD but the ahead/behind counts can't be computed. Tells you
  whether your checkout matches the pin.
- **vs Upstream** — Current vs `origin/<branch>` (using whatever was fetched
  last — `status` itself does not fetch): `in-sync`, `N behind`, `N ahead`,
  `diverged`, or `no remote`. Tells you whether your checkout matches the
  upstream tip. `no remote` means `origin/<branch>` doesn't resolve locally —
  usually a single-branch clone (from `--depth`) whose fetch refspec doesn't
  track the manifest `branch`, most often after the manifest `branch` changed.
  The next `rt sync` / `rt reset` / `rt fast-forward` heals it: they widen the
  clone's refspec to include the branch before fetching.
- **Status** — `clean` / `untracked files` / `dirty` / `local branches`.
  Precedence, highest first: dirty (any modified tracked file) so unsaved work
  isn't hidden, then untracked files (because they could modify behaviour of a
  build/test), then local branches. Untracked files outrank `local branches`
  because they block `snapshot` while local-only branches are only informational
  — hiding untracked behind `local branches` would mask why a snapshot was
  refused. `local branches` flags the presence of any local branch with no
  `origin/<same-name>` counterpart — a heads-up that there's local work that
  hasn't been pushed or snapshotted.

The two comparison columns answer independent questions, so together they
distinguish common workflow states. *Fast-forwarded but forgot to snapshot*: `vs
Manifest: 4 ahead, vs Upstream: in-sync`. *Pin is stale, upstream has moved*:
`vs Manifest: in-sync, vs Upstream: 4 behind`. *Local commits not on origin*:
`vs Manifest: 4 ahead, vs Upstream: 4 ahead`. *True divergence*: `vs Manifest:
ahead/behind, vs Upstream: diverged`.

If any directories under the meta-repo contain a `.git` entry but aren't
referenced by the manifest, they appear under "Orphan directories:".

## `rt show-changes [names...] [-s]`

Where `status` gives a one-line-per-child ahead/behind summary, `show-changes`
prints the actual commits between each child's manifest `rev` and its current
HEAD — the "what changed?" companion to the counts. Read-only, no fetch.
Defaults to already-cloned children; name repos to narrow (a named-but-uncloned
repo prints a `not synced` line rather than failing).

For each child it resolves the manifest `rev` locally and, over the symmetric
difference with HEAD, prints one compact line per commit: `+` (green) for
commits on HEAD but not the pinned rev (ahead), `-` (red) for commits on the
pinned rev but not HEAD (behind), each with the short SHA, the author email's
local-part (domain dropped), and the subject. In-sync, unpinned, and
not-locally-resolvable revs are reported and skipped; the command always exits 0
(an unknown repo name is the only error).

- `-s`, `--shortlog` — show `git shortlog --no-merges` output (commits grouped
  by author, with counts) instead of the per-commit lines. When a child has
  diverged both ways, the ahead and behind ranges are shown as separate labeled
  blocks.

## `rt reset [names...] [-a] [-f] [-j N] [-v]`

Destructive. Realigns each already-cloned child (or just the named subset) to
its manifest `rev`, with the `branch` label pointing at that revision:

1. `git fetch origin`
2. Resolve `<target>` from `rev`: prefer `origin/<rev>` when it resolves (so a
   branch-name rev follows the remote tip), otherwise `<rev>` resolved locally
   (tag/SHA case)
3. `git reset --hard <target>`
4. `git clean -fd`
5. `git checkout -B <branch>` — moves the branch label to `<target>` and
   attaches HEAD

**Safety check**: if HEAD is ahead of `<target>` (i.e. you have local commits
that would be discarded), `reset` refuses and prints the commits. Pass `-f` /
`--force` to discard anyway. No interactive prompt — `--force` is the
confirmation. With `submodules = true`, the same check covers commits in each
submodule, and submodules are force-updated and cleaned after the reset.

Flags:
- `-a`, `--all` — also include manifest entries that aren't cloned yet. They
  are reported as `skipped: not synced` rather than cloned or failed.
- `-f`, `--force` — discard local commits ahead of the target.
- `-j N` — concurrent git operations (default 8).
- `-v` — print every `$ git …` invocation per repo with stdout/stderr.

Exits non-zero if any repo failed or was refused by the safety check.

## `rt fast-forward [names...] [-j N] [-v]` (alias: `rt ffwd`)

`git fetch` then advance to `origin/<branch>` per child — the non-destructive
complement to `reset`. Advances each child to the remote tip when, and only
when, doing so cannot lose work. After a successful advance, the manifest `rev`
is now stale; the output reminds you to run `rt snapshot`.

When HEAD is attached to `branch`, fast-forwards in place via `git merge
--ff-only origin/<branch>`. When HEAD is detached but is an ancestor of
`origin/<branch>`, advances *and* attaches via `git checkout -B <branch>
origin/<branch>` — this is what heals the very common "manifest pinned an older
SHA, `sync` left me detached" state in one step. If HEAD is detached at exactly
`origin/<branch>`, fast-forward still attaches (HEAD doesn't move, but you end
up on `branch`).

A repo is **skipped** (not failed) if any of:
- working tree is dirty,
- HEAD is attached to a branch that isn't `branch` (the user is on a feature
  branch),
- HEAD has commits not on `origin/<branch>` (would require a real merge or
  rebase),
- HEAD is detached but a local `<branch>` ref has commits not on
  `origin/<branch>` (advancing would silently rewrite that branch).

Skips print a yellow per-repo line explaining why, and uncloned children are
skips too. Without `names`, skips don't affect the exit code: the command exits
non-zero only when a `git fetch` fails or the `merge --ff-only` / `checkout -B`
itself errors. When repos are named explicitly, a skip is a failure too — you
asked for those repos and they didn't move.

Flags:
- `-j N` — concurrent git operations (default 8).
- `-v` — print every `$ git …` invocation per repo with stdout/stderr.

## `rt bump [names...] [-n] [--json] [--log-limit N] [-j N] [-v]`

Try to advance each child's manifest pin to the tip of its manifest `branch`.
This is `fast-forward` and `snapshot` fused together, and it also produces a
summary of what changed that can be incorporated into a change log.

Per child, in order:

1. The same safety pre-checks as `fast-forward` (clean tree, on the manifest
   branch or detached, no local commits ahead, no local `<branch>` that would be
   rewritten) — a failure here is a yellow `skipped`. Uncloned children are
   skipped, never cloned: that's `rt sync`'s job, so sync first. The two checks
   that exist purely to protect the *advance* — local commits ahead of the tip,
   or a local `<branch>` that attaching would rewrite — don't apply when the
   clone is already sitting at the tip: there's nothing to move, so the manifest
   bump goes ahead and the checkout (and that local branch) are left exactly as
   they are.
2. `git fetch origin`, then compare the manifest `rev` with `origin/<branch>`:
   - resolves to the tip → `up-to-date`, manifest untouched (the clone is still
     fast-forwarded if it lags behind the pin).
   - resolves but isn't an ancestor of the tip → `diverged` (red): upstream
     rebased or force-pushed, so the move isn't a fast-forward. Nothing is
     touched and the command exits non-zero — a human has to decide.
   - otherwise → `updated`: the clone is fast-forwarded (`merge --ff-only`, or
     `checkout -B <branch> origin/<branch>` when detached, exactly as
     `fast-forward` does) and the new tip SHA is written to `[[repo]].rev`.
3. A pin that doesn't resolve locally — a `--depth` clone whose SHA is below the
   shallow window, say — is fetched by name (`git fetch origin <rev>`) so the
   ancestry check stays authoritative. If it still can't be found, the update is
   reported with a `manifest rev not present locally; log unavailable` note
   instead of a changelog, and no commit count.

Only `rev` is ever rewritten: unlike `snapshot`, which adopts whichever branch
you have checked out, here the manifest `branch` is the *input* naming the tip
to follow. A loose (branch-name) `rev` that already resolves to the tip stays
loose — use `rt snapshot` to pin it to a SHA. A child with `submodules = true`
has only its superproject moved (as with `fast-forward`); follow up with `rt
sync` before building it.

Skips are informational in bulk mode, but — like `fast-forward` — a repo you
named explicitly and that got skipped exits non-zero: you asked for that repo
and it didn't move.

```
$ rt bump
Bumping 2 repo(s)…
  linux    fetching
  opensbi  fetching
  linux    0c986d8 → 9f2ab41 (17 commit(s))
      a1b2c3d  riscv: Fix hart mask handling
      d4e5f6a  riscv: Add foo dt bindings
      … and 15 more
  opensbi  already pinned at 8a05d33
Updated manifest (1 repo(s))
```

Flags:
- `-n`, `--dry-run` — report only: still fetches, but neither moves the clone
  nor rewrites the manifest.
- `--json` — emit one machine-readable JSON document on stdout and nothing else
  (see below). Mutually exclusive with `-v`.
- `--log-limit N` — commit subjects to report per repo (default 20; `0` for
  unlimited). Commits beyond the limit are counted, not listed.
- `-j N` — concurrent git operations (default 8).
- `-v` — print every `$ git …` invocation per repo with stdout/stderr.

### `--json` output

A single document, repos in manifest order. Every documented key is always
present; absent values are `null`.

```console
$ rt bump --json --log-limit 1 linux | jq
{
  "dry_run": false,
  "manifest_updated": 1,
  "repos": [
    {
      "name": "linux",
      "path": "linux",
      "branch": "master",
      "status": "updated",
      "old": "0c986d800710e5fe667ac24ad522cfb7f9ef64d2",
      "old_sha": "0c986d800710e5fe667ac24ad522cfb7f9ef64d2",
      "new": "9f2ab41de0035c1f0f7ec1b5f4b1c0f2b0a11d02",
      "count": 17,
      "detail": null,
      "note": null,
      "truncated": 16,
      "log": [
        {"sha": "a1b2c3d…", "subject": "riscv: Fix hart mask handling"}
      ]
    }
  ]
}
```

- `status` — `updated` | `up-to-date` | `diverged` | `skipped` | `failed`.
- `old` — the manifest `rev` as written (may be a tag or branch name); `old_sha`
  is it resolved, or `null` when it couldn't be. `new` is always a 40-hex SHA
  (the branch tip).
- `count` — commits in `old_sha..new`, `null` when it can't be computed (no
  local `old_sha`, or an unpinned entry).
- `detail` — the skip / failure / divergence reason; `null` for `updated` and
  `up-to-date`.
- `log` — newest-first, at most `--log-limit` entries, with `truncated` counting
  the rest.
- `manifest_updated` — how many `[[repo]]` blocks were rewritten (always `0`
  under `--dry-run`).

Diagnostics stay on stderr, so stdout is safe to pipe into `jq`.

Because CI parses it, this document is a compatibility surface, and there is
deliberately **no version field**: a marker would only let a consumer detect a
break it can't do anything about, and it's redundant when you vendor
`repotool.py` next to the script that parses its output. The contract is instead
that changes are **additive** — new keys only, so write consumers to ignore keys
they don't know. If the document ever needs a genuinely different shape, that
will come as a new flag (as git did with `--porcelain=v2`), leaving deployed
pipelines working unchanged.

## `rt check [names...] [-j N] [-v] [--https]`

CI preflight: validate the manifest and prove every declared remote is
reachable, without cloning or fetching anything. Run it as the first job in a
pipeline so a typo'd URL or a deleted branch fails in seconds instead of after
a full `rt sync`.

Unlike `sync` / `reset` / `show-changes`, it operates on **all** manifest
entries regardless of what's on disk — in CI nothing is cloned yet. Name repos
to narrow.

Per repo, one `git ls-remote <url>` round-trip (parallelized) both proves the
URL is reachable and authorized and lists every advertised ref; branch and rev
existence are then checked against that listing:

- `branch` must be present as a remote head — absent is a failure.
- A `rev` that's a tag or branch name must be present as a tag/head — absent is
  a failure.
- A `rev` that's a SHA can only be confirmed when it happens to sit at the
  branch tip. Pinned *below* the tip (the common case) it can't be checked
  without fetching, so it's reported `unverified` (yellow) — not a failure.

```
$ rt check
Checking 3 repo(s)… (skipping 1 with check = false)
  linux     reachable; branch 'master' present; rev 4f1e2d7 is a SHA below the tip (needs fetch to verify)
  opensbi   reachable; branch + rev (tag 'v1.4') present
  u-boot    unreachable: fatal: Could not read from remote repository.
  private   excluded (check = false)
error: check failed for 1 of 3 repo(s): u-boot
```

Exits non-zero on an unreachable URL, a missing branch, or a named (tag/branch)
rev that isn't on the remote. `unverified` and `excluded` results leave the
exit code at 0 — an all-excluded run still exits 0.

Flags:
- `--https` — rewrite every repo URL to `https://` for this run (in memory; the
  manifest on disk is untouched). For CI runners without SSH keys installed.
  Same flag as `rt sync`.
- `-j N` — concurrent git operations (default 8).
- `-v` — print every `$ git …` invocation per repo with stdout/stderr.

See [`check = false`](manifest.md#optional-keys) for excluding a repo CI can't
reach.

## `rt snapshot [names...] [--no-verify-remote] [-j N]`

Pin each child to its current branch + HEAD SHA in the manifest. Useful for
capturing a known-good state of the meta-repo + children at a specific revision
so it can be reproduced later with `rt sync` or `rt reset`.

For each repo:
- Skips children that aren't cloned, leaving their manifest entries alone.
- Verifies the clone is clean (no modifications, staged changes, or untracked
  files).
- Verifies HEAD is attached to a branch, or detached at exactly
  `origin/<branch>` for the manifest `branch` — the state after a force-push
  followed by `git checkout origin/<branch>`. Any other detached HEAD is
  refused: there's no branch to record.
- `git fetch origin`, then checks that HEAD is contained in
  `refs/remotes/origin/<current_branch>` — guarding against pinning a SHA that
  only exists locally and can't be fetched back.
- Writes the current branch (the manifest `branch` in the detached case) into
  `[[repo]].branch` and the current HEAD SHA into `[[repo]].rev`.

Atomic: if any selected repo fails any check, *no* changes are written and the
command exits non-zero. The rewrite is a line-based substitution — comments,
whitespace, and key order in the manifest are preserved.

Flags:
- `--no-verify-remote` — skip the fetch and remote-reachability check. Use when
  you intentionally want to pin a local-only SHA.
- `-j N` — concurrent git operations (default 8).

## `rt completions <bash|zsh|fish>`

Print a tab-completion script for the given shell on stdout. See [Shell
completion](workflow.md#shell-completion) for how to install it.

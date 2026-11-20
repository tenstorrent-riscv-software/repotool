# Manifest

The manifest is `.repotool.toml` at the root of the parent repository,
and/or `*.toml` files in a `.repotool/` directory there (see [Multiple manifest
files](#multiple-manifest-files)). Each `[[repo]]` table declares one child:

```toml
ignore = ["..."]      # directory subtrees to leave out of the orphan walk    (optional)

[[repo]]
url        = "..."    # git URL                                              (required)
path       = "..."    # placement relative to meta-repo root                 (required)
branch     = "..."    # the branch this rev lives on / fast-forward target   (required)
rev        = "..."    # the exact revision to pin: SHA, tag, or branch name  (optional)
name       = "..."    # display name (defaults to `path` if omitted)
reference  = "..."    # share objects with another manifest repo on clone    (optional)
submodules = false    # align this child's git submodules after checkout     (optional)
check      = true     # contact this remote during `rt check`                (optional)
```

Omit `rev` to leave the entry **unpinned** — `sync` will check out `branch`
directly (attached HEAD, no SHA pin), `reset` realigns to `origin/<branch>`, and
`rt snapshot` will fill in the `rev` line pointing at the current HEAD the next
time you run it. Useful when you want to track a branch's latest tip and only
pin at known-good points.

Every `[[repo]].path` is relative to the meta-repo root — there is no shared
parent directory. Place children wherever makes sense.

`branch` and `rev` together tie the state of the meta-repo to specific revisions
of the children: `rev` is what `sync` and `reset` align to (typically a SHA or
tag for reproducibility), and `branch` is the context other commands use — what
`fast-forward` advances, and what `snapshot` verifies against `origin/<branch>`.
Putting a branch name in `rev` is allowed (loose mode — checkout will follow the
remote tip) but `snapshot` will replace it with a real SHA.

## Optional keys

- **`reference = "<other-repo-name>"`** — the initial clone of this repo shares
  git objects with another repo in the same manifest, via `git clone
  --reference`. `sync` orders clones so the referenced repo lands first.
  Validated at load time for existence, self-reference, and cycles. Suppressed
  by `--depth` (shallow clones and alternates don't mix). This is the explicit,
  strict form of the automatic object sharing described under
  [Worktrees](workflow.md#worktrees-of-the-meta-repo).
- **`submodules = true`** (default `false`) — after checking this child out,
  `sync` and `reset` align its git submodules: `git submodule sync
  --recursive` then `git submodule update --init --recursive`. No `--remote`:
  submodules land on the SHAs the checked-out commit pins, mirroring
  repotool's own checkout-only, pin-following model one level down. `reset`
  additionally passes `--force` and cleans inside each submodule, but still
  refuses (without `--force`) when a submodule carries commits that aren't
  reachable from the pinned SHA. Set it only on children that actually use
  submodules, so the rest don't pay the recursion cost.
- **`check = false`** (default `true`) — exclude this repo from bulk `rt check`
  runs. Intended for a private repo that CI has no credentials for and would
  otherwise fail the preflight on. Excluded repos print as dim `excluded (check
  = false)` lines and don't affect the exit code; naming the repo explicitly
  (`rt check <name>`) overrides the exclusion and contacts it anyway. The key
  affects only `rt check` — every other command ignores it, and structural
  validation still applies.
- **`ignore = ["<path>", …]`** — the one **top-level** key, so in TOML it must
  appear *before* the first `[[repo]]` table. Lists directory subtrees to leave
  out of the `Orphan directories:` walk that `status` and `sync` print — for
  trees whose checkouts are managed by another tool (a west workspace mounted
  inside the meta-repo, say) and would otherwise be flagged on every run.
  Entries follow the same path rules as a repo `path`, and the walk prunes at
  each one (nested clones underneath are skipped too). Affects only the orphan
  walk.

## Validation

Validation rejects:
- absolute paths, paths containing `..`, or paths that resolve outside the
  meta-repo root
- paths that are ancestors of each other (e.g. `api` and `api/sub`)
- duplicate names or paths
- non-string `url` / `path` / `branch` / `rev` / `name` / `reference`,
  non-boolean `submodules` / `check`
- a `reference` that doesn't name another repo, names itself, or forms a cycle

Unknown `[[repo]]` keys are a warning, not an error — a manifest written for a
newer `repotool` still loads.

## Multiple manifest files

To split a large manifest, put `*.toml` files in a `.repotool/` directory at the
root of the meta-repo. Each file declares one or more `[[repo]]` entries (and
may carry its own top-level `ignore`). All files are loaded together, in name
order after `.repotool.toml` if that also exists, so names and paths must be
unique across them. `rt snapshot` and `rt bump` rewrite each repo in the file
it came from.

# Workflow

The intended day-to-day loop is:

```
$ rt sync            # clone/align children to the manifest
$ …                  # edit, build, test, commit, push from inside the child repos
$ rt fast-forward    # when you want upstream changes, advance children to their origin tips
$ rt snapshot        # re-pin the manifest to each child's current HEAD
$ git add .repotool.toml && git commit && git push
```

After the snapshot, the parent repo records the new pinned state of every child.
The bumped `rev` values show up as ordinary line diffs in `.repotool.toml`,
reviewable in any PR tool — no special "subproject" diff format. Once a teammate
pulls and runs `rt sync`, their children land on the same SHAs.

A few notes on the loop:

- **`rt status`** is read-only and never fetches — run it any time to see how
  each child sits relative to its manifest pin (`vs Manifest`) and relative to
  `origin/<branch>` (`vs Upstream`, using whatever the last fetch saw). It's the
  fastest way to answer "did I forget to snapshot?" or "has upstream moved past
  my pin?".
- **Children are normal git repos.** Use `git` inside any of them to create
  branches, commit and push as usual. `repotool` doesn't wrap or hide git; it
  only coordinates the parent-level pinning.
- **`rt fast-forward` is conservative.** It skips dirty trees, repos on a
  feature branch, repos with local commits not on origin, and any case where
  moving the manifest branch would silently rewrite local work. Skips are yellow
  lines in the output; in a bulk run they aren't failures and don't change the
  exit code, but a repo you named explicitly that gets skipped fails the run.
- **`rt snapshot` is a manifest edit, not a git operation.** It rewrites
  each manifest file in place (line-based, comments and formatting preserved)
  once it's verified each child's HEAD is reachable from `origin/<branch>` (pass
  `--no-verify-remote` to skip that check for intentionally local-only pins).
- **For a destructive realignment** — discarding local work to match a
  newly-pushed upstream, for example — reach for `rt reset` instead of `rt
  fast-forward`. Reset force-aligns each child to the manifest `rev`;
  fast-forward only ever advances when it cannot lose work.
- **Working on a subset?** `rt sync` and `rt reset` operate only on
  already-cloned children by default, so you can keep some manifest entries
  un-cloned and the day-to-day loop above won't try to touch them. Pass `--all`
  (or name them explicitly) when you do want them.
- **Running this in CI?** Start the pipeline with `rt check`: it validates the
  manifest and `ls-remote`s every declared URL without cloning anything, so a
  bad URL, a missing branch, or a rev that no longer exists upstream fails in
  seconds. Mark repos the runner has no credentials for with `check = false`.
- **Automating the bump?** `rt bump` is the `fast-forward` + `snapshot` pair
  collapsed into one command, for a CI job that proposes child updates: it
  advances each child to its branch tip *and* rewrites the manifest, refuses
  anything that isn't a fast-forward, and with `--json` reports the old/new SHAs
  plus the commit log for a merge request description.

## Shell completion

`rt completions <shell>` prints a tab-completion script for bash, zsh, or fish.
The scripts delegate to `rt` itself at completion time, so subcommands, options,
and repo names and paths from the manifest complete without the script ever
going stale. Enable it in your shell's rc file:

```bash
# ~/.bashrc
eval "$(rt completions bash)"

# ~/.zshrc — must come after compinit
eval "$(rt completions zsh)"

# ~/.config/fish/config.fish
rt completions fish | source
```

Or install as static files (regenerate after upgrading rt):

```
$ rt completions bash > ~/.local/share/bash-completion/completions/rt
$ rt completions zsh  > ~/.zsh/completions/_rt      # any directory on $fpath
$ rt completions fish > ~/.config/fish/completions/rt.fish
```

Known limitation: in bash, `--reference=<TAB>` (the `=` form) doesn't complete
paths; use `--reference <TAB>`.

## Worktrees of the meta-repo

`repotool` anchors to `git rev-parse --show-toplevel`, so each worktree gets its
own children directory. Create a worktree with `git worktree add` and run `rt
sync` inside it — you get a clean, isolated set of child clones.

To avoid re-downloading and re-storing git objects, `sync` finds the primary
worktree (the main working tree) via `git worktree list` and passes
`--reference-if-able <primary>/<repo.path>` to `git clone`. Secondary worktrees
share objects with the primary, never with each other: a secondary worktree can
be removed at any time, which would break clones that referenced it.

**Caveat**: if a child in the primary worktree is later removed, clones that
referenced it can lose object data. To make a child self-sufficient before
detaching, run `git -C <child> repack -a -d`.

## Behaviours worth knowing

- **No global state.** Everything is in the manifest files. No `~/.config`
  database, no daemon, no lock file.
- **No automatic branch tracking.** `sync` is checkout-only for branch refs. Use
  `reset` for forced realignment.
- **No pruning.** Removing a `[[repo]]` leaves the child's directory on disk.
  `status` will list it as an orphan.
- **No URL rewriting.** Mismatched `origin` → warning, no action.
- **No upward lookup.** Running `rt` from inside a child repo errors out
  clearly; it does not walk upward looking for a parent manifest.
- **Colour.** Output is coloured with ANSI escapes when stdout is a TTY, and
  plain when piped or when `NO_COLOR` is set.
- **Concurrent runs.** Don't run two `rt` processes against the same meta-repo
  at once; git's own locks will produce confusing failures.

## Limitations

- Linux is the only supported platform. macOS may work but is untested.
- Submodules are only aligned for children that opt in with `submodules =
  true`, and `rt fast-forward` and `rt bump` move only the superproject; run
  `rt sync` afterwards to bring the submodules along.
- No `init` / `add` / `remove` subcommands — hand-edit the TOML.

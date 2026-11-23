# repotool

repotool keeps a set of git repositories checked out at pinned revisions
inside a parent repository. A TOML manifest committed to the parent lists each
child: where it goes, which branch it follows, and which revision it's pinned
to.

For brevity, repotool is commonly installed as `rt`, the name used throughout
this documentation. `rt sync` clones the children and checks each one out at
its pin, `rt status` shows how they've moved since, and `rt fast-forward` plus
`rt snapshot` advance them and record the new pins in the manifest.

It's a single Python file with no dependencies beyond the standard library:
copy it into your repository and run it.

- **Pins are plain text.** Bumping a child is a one-line diff in a TOML file,
  reviewable like any other change.
- **Every pin carries its branch**, so children can be fast-forwarded and
  re-pinned, not just checked out.
- **Children are ordinary clones.** Use git in them as usual, and clone only
  the ones you need.
- **No workspace setup.** No init step, no state directory, nothing to install:
  the parent repository is the workspace.

See [How repotool compares](docs/comparison.md) for the differences from git
submodules, Google's `repo` and Zephyr's `west`.

> 🤖 repotool is developed using large language model (LLM) coding tools. The
> guidance they work from is in [AGENTS.md](AGENTS.md).

## Install

repotool runs on Linux and needs Python 3.12 or later and git. macOS may work
but is untested.

Copy `repotool.py` into your parent repository as `rt`:

```
$ cp repotool.py rt
$ chmod +x rt
$ ./rt --help
```

`python3 repotool.py sync` works too, without the rename. Or install it as a
package, which provides an `rt` command:

```
$ pip install -e .
```

The package is named `tt-repotool`. It isn't published on PyPI, where
`repotool` is an unrelated project.

For tab completion add `eval "$(rt completions bash)"` to `~/.bashrc`; zsh and
fish are covered in [Shell completion](docs/workflow.md#shell-completion).

## Quick start

Create `.repotool.toml` at the root of the parent repository:

```toml
[[repo]]
name   = "linux"
url    = "https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git"
path   = "linux"
branch = "master"
rev    = "v6.6"

[[repo]]
name   = "opensbi"
url    = "https://github.com/riscv-software-src/opensbi.git"
path   = "opensbi"
branch = "master"
rev    = "v1.4"
```

Clone the children and check them out at their pins:

```
$ rt sync --all
Syncing 2 repo(s)…
  linux    cloning https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git
  opensbi  cloning https://github.com/riscv-software-src/opensbi.git
  opensbi  cloned @ v1.4 (master, detached)
  linux    cloned @ v6.6 (master, detached)
```

The pins are tags below each branch's tip, so the children are left on a
detached HEAD, like `git checkout v6.6` would. Add `linux/` and `opensbi/` to
the parent's `.gitignore` so git doesn't report them as untracked.

Then see where each one stands:

```
$ rt status
            Manifest  Manifest  Current  vs        vs
Repository  Branch    Rev       Rev      Manifest  Upstream      Status
----------  --------  --------  -------  --------  ------------  ------
meta                            a1b2c3d                          clean
linux       master    v6.6      4f1e2d7  in-sync   86410 behind  clean
opensbi     master    v1.4      8a9b0c1  in-sync   612 behind    dirty
```

Both children match their pins, upstream has moved on since, and `opensbi` has
uncommitted changes.

## Day to day

```
$ rt sync            # align children to the manifest
$ …                  # work in the children with plain git
$ rt fast-forward    # advance children to their upstream branch tips
$ rt snapshot        # pin the manifest to each child's current HEAD
$ git commit .repotool.toml
```

Once a teammate pulls that commit, `rt sync` puts their children on the same
revisions.

## Commands

| Command | Does |
|---|---|
| `rt sync` | Clone missing children and check each out at its pin. Never moves local branches. |
| `rt status` | Show each child against the manifest and upstream. The default command. |
| `rt show-changes` | List the commits between each child's pin and its HEAD. |
| `rt fast-forward` | Advance children to `origin/<branch>`, only when no work can be lost. |
| `rt snapshot` | Record each child's current branch and HEAD in the manifest. |
| `rt bump` | Fast-forward and snapshot in one step, for automated update jobs. |
| `rt reset` | Force children back to their pins, discarding local changes. |
| `rt check` | Validate the manifest and contact every remote without cloning. |
| `rt completions` | Print a bash, zsh or fish completion script. |

Commands accept repo names or paths to limit what they act on. By default
`sync` and `reset` only touch children that are already cloned; pass `--all` to
include the rest.

## Documentation

- [Manifest](docs/manifest.md) — the file format and its keys.
- [Commands](docs/commands.md) — full reference for each command.
- [Workflow](docs/workflow.md) — the day-to-day loop, worktrees, shell
  completion, and behaviour worth knowing.
- [How repotool compares](docs/comparison.md) — vs git submodules, `repo` and
  `west`.
- [Design](docs/design.md) — design decisions and the reasoning behind them.

## Development

```
$ just venv          # create .venv with the dev dependencies
$ just test          # all the checks: lint, typecheck and pytest
$ just lint          # ruff
$ just typecheck     # mypy
$ just pytest        # the test suite
```

Tests build real git repositories on disk; there are no mocks. Without
[`just`](https://github.com/casey/just), `pip install -e ".[dev]"` into a venv
and run `pytest`, `ruff check` and `mypy repotool.py` directly.

## Contributing

Bugs and feature requests go in [GitHub
Issues](https://github.com/tenstorrent-riscv-software/repotool/issues), and
changes are submitted as pull requests, which are reviewed weekly. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the requirements, and report security
vulnerabilities as described in [SECURITY.md](SECURITY.md).

## License

- [LICENSE](LICENSE) — overall license for this project, except where
  specified: the Apache License 2.0.
- [LICENSE-DOCS](LICENSE-DOCS) — license for all documentation and images
  only: Creative Commons Attribution 4.0 (CC BY 4.0), covering the files in
  `docs/`.
- [LICENSE_understanding.txt](LICENSE_understanding.txt) — Tenstorrent's
  clarification of how the license applies.
- [NOTICE](NOTICE) — copyright and attribution notices.

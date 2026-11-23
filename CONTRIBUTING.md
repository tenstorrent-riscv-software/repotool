# Contributing to repotool

Thanks for your interest in contributing. Bug reports, fixes and new features
are all welcome.

Everyone participating in this project is expected to follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Reporting bugs

Report bugs and request features through [GitHub
Issues](https://github.com/tenstorrent-riscv-software/repotool/issues). For a
bug, include:

- the output of `rt --version`, `git --version` and `python3 --version`
- the relevant `[[repo]]` entries from your manifest
- the command you ran and its output; rerunning with `-v`, where the command
  supports it, shows every git invocation and its output

Do not report security vulnerabilities through public issues; follow the
[security policy](SECURITY.md) instead.

## Submitting changes

Bug fixes and new functionality are submitted as pull requests. Pull requests
are reviewed weekly. For a large change, consider opening an issue first to
discuss the approach.

### Development setup

You need Python 3.12 or later, git, and [`just`](https://github.com/casey/just).

```
$ just venv            # create .venv with the dev dependencies
$ just install_hooks   # optional: run lint, typecheck and tests on commit
```

The hooks require [`prek`](https://github.com/j178/prek), which must be
installed separately. The configuration uses prek's built-in hooks (`repo:
builtin`), which the `pre-commit` tool doesn't support.

### Requirements for changes

- **Keep `repotool.py` self-contained.** It is designed to be copied into
  other repositories and run on its own, so it must stay a single file that
  depends only on the Python 3.12 standard library.
- **Add tests.** Behaviour changes need tests in `tests/`. Tests build real git
  repositories in pytest's `tmp_path`; don't use mocks.
- **Pass the checks.** `just test`, which runs lint, typecheck and pytest, must
  pass.
- **Update the documentation.** Document user-visible changes in the relevant
  page under `docs/`, and in `README.md` only if the overview changes. Record
  changes to a design decision in [`docs/design.md`](docs/design.md).
- **Add SPDX headers** to new source files:

  ```
  # SPDX-License-Identifier: Apache-2.0
  # SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
  ```

### Commit messages

Use a scoped subject in the imperative mood, `<scope>: <description>`, where
the scope is the command or area changed, for example
`sync: Fetch a missing pinned rev by name`. Use the body to explain why the
change is needed, not just what it does.

### Coding agents

repotool is developed with the help of large language model (LLM) coding
tools. If you use one, point it at [`AGENTS.md`](AGENTS.md), which describes
the architecture, the decisions that changes must respect, and the conventions
above.

## License

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE), the license that covers this project, except
for contributions to the documentation in `docs/`, which are licensed under
[CC BY 4.0](LICENSE-DOCS).

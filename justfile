# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.

# Where the dev venv lives. A venv is tied to the exact Python that built it
# (its packages sit in <venv>/lib/python<X.Y>/site-packages), so a tree shared
# between interpreters of different versions -- a container and its host, say --
# needs one venv each. Point REPOTOOL_VENV at a second directory in that case:
#
#   REPOTOOL_VENV=.venv-3.14 just venv test
venv_dir := env_var_or_default("REPOTOOL_VENV", ".venv")

# List the available recipes.
[private]
help:
    @just --list

# Remove the venv, caches, and build artifacts.
clean:
    rm -rf {{venv_dir}} .pytest_cache build dist
    rm -rf *.egg-info
    find . -type d -name __pycache__ -prune -exec rm -rf {} +
    find . -type f -name '*.pyc' -delete

# (Re)create the venv and install the dev extra.
venv:
    python3 -m venv {{venv_dir}}
    {{venv_dir}}/bin/pip install --upgrade pip --quiet
    {{venv_dir}}/bin/pip install -e ".[dev]"

# Run all the checks: lint, type check and the test suite.
test: lint typecheck pytest

# Run the test suite. Extra args pass through to pytest, e.g. `just pytest -k status`.
pytest *args:
    {{venv_dir}}/bin/pytest {{args}}

# Lint with ruff. Extra args pass through, e.g. `just lint --fix`.
lint *args:
    {{venv_dir}}/bin/ruff check {{args}}

# Type-check repotool.py with mypy. Extra args pass through.
typecheck *args:
    {{venv_dir}}/bin/mypy {{args}} repotool.py

# Install the prek pre-commit hooks (runs lint, typecheck, pytest on commit).
install_hooks:
    prek install

# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.

"""Shared pytest fixtures. Build real on-disk git repos in tmp_path."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def _git_identity(tmp_path_factory):
    """Point the whole suite at a throwaway, isolated global git config.

    Tests must never depend on the developer's ambient ~/.gitconfig: CI's
    pristine image has none, so a `git commit` in any repo that lacks a
    per-repo identity fails there (exit 128) while passing locally. This gives
    every git process a guaranteed user.name/email and isolates the run from
    real global/system config, so local behavior matches CI.
    """
    cfg = tmp_path_factory.mktemp("gitconfig") / "config"
    cfg.write_text("[user]\n\tname = Test User\n\temail = test@example.com\n")
    mp = pytest.MonkeyPatch()
    mp.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    mp.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    yield
    mp.undo()


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args, cwd=cwd, check=True, capture_output=True, text=True,
    )


def _init_repo(path: Path, branch: str = "main") -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", branch], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test User"], path)


def _commit(path: Path, message: str, files: dict | None = None) -> str:
    files = files if files is not None else {"f.txt": message}
    for name, content in files.items():
        (path / name).write_text(content)
    _git(["add", "."], path)
    _git(["commit", "-m", message], path)
    return _git(["rev-parse", "HEAD"], path).stdout.strip()


@pytest.fixture
def make_remote(tmp_path_factory):
    """Returns a callable that creates a bare 'remote' repo with main + v1.0 tag."""

    def _make(name: str | None = None) -> Path:
        local_name = name or "remote"
        work = tmp_path_factory.mktemp(f"{local_name}-work")
        _init_repo(work)
        _commit(work, "initial")
        _git(["tag", "v1.0"], work)
        # Pair the bare path to the unique work dir name so it doesn't collide
        # across tests sharing the session basetemp.
        bare = work.parent / f"{work.name}.bare.git"
        _git(["clone", "--bare", str(work), str(bare)], work.parent)
        return bare

    return _make


@pytest.fixture
def advance_remote():
    """Push a new commit to a bare remote's main."""

    def _advance(bare: Path, message: str = "more") -> str:
        with tempfile.TemporaryDirectory() as td:
            work = Path(td) / "work"
            _git(["clone", str(bare), str(work)], Path(td))
            _git(["config", "user.email", "test@example.com"], work)
            _git(["config", "user.name", "Test User"], work)
            sha = _commit(work, message, {f"new-{message}.txt": message})
            _git(["push", "origin", "main"], work)
        return sha

    return _advance


@pytest.fixture
def meta_repo(tmp_path: Path) -> Path:
    """An initialized meta-repo at tmp_path with one commit."""
    _init_repo(tmp_path)
    _commit(tmp_path, "initial", {".gitkeep": ""})
    return tmp_path

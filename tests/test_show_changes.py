# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.

"""Tests for the show-changes command."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from repotool import RepoToolError, show_changes, sync


@pytest.fixture(autouse=True)
def chdir_to_meta(meta_repo, monkeypatch):
    monkeypatch.chdir(meta_repo)


def _write_manifest(meta_repo: Path, repos: list) -> None:
    """4-tuple (name, url, path, rev), branch defaults to "main". rev may be
    None to omit the line (unpinned)."""
    parts: list[str] = []
    for name, url, path, rev in repos:
        entry = (
            f'\n[[repo]]\nname = "{name}"\nurl = "{url}"\npath = "{path}"\n'
            f'branch = "main"\n'
        )
        if rev is not None:
            entry += f'rev = "{rev}"\n'
        parts.append(entry)
    (meta_repo / ".repotool.toml").write_text("".join(parts))


def _head(p: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=p, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _commit_in_child(child: Path, filename: str) -> str:
    subprocess.run(["git", "config", "user.email", "alice@example.com"], cwd=child, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Alice"], cwd=child, check=True, capture_output=True)
    (child / filename).write_text("local")
    subprocess.run(["git", "add", "."], cwd=child, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", f"add {filename}"], cwd=child, check=True, capture_output=True)
    return _head(child)


def test_show_changes_ahead(meta_repo, make_remote, capsys):
    """HEAD has commits beyond the pinned rev: they show as '+' / 'ahead'."""
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "v1.0")])
    assert sync(jobs=2, include_missing=True) == 0
    child = meta_repo / "ext"
    _commit_in_child(child, "one.txt")
    _commit_in_child(child, "two.txt")

    assert show_changes(names=["ext"]) == 0
    out = capsys.readouterr().out
    assert "2 ahead" in out
    assert "+ " in out
    assert "add one.txt" in out
    assert "add two.txt" in out
    # Author email local-part shows; the domain is dropped.
    assert "alice" in out
    assert "example.com" not in out


def test_show_changes_shortlog(meta_repo, make_remote, capsys):
    """--shortlog shows shortlog output: author line with count, no + markers."""
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "v1.0")])
    assert sync(jobs=2, include_missing=True) == 0
    child = meta_repo / "ext"
    _commit_in_child(child, "one.txt")
    _commit_in_child(child, "two.txt")

    assert show_changes(names=["ext"], shortlog=True) == 0
    out = capsys.readouterr().out
    assert "2 ahead" in out
    assert "Alice (2):" in out
    # shortlog output, not the per-commit log
    assert "+ " not in out


def test_show_changes_behind(meta_repo, make_remote, capsys):
    """HEAD sits below the pinned rev: those commits show as '-' / 'behind'."""
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "v1.0")])
    assert sync(jobs=2, include_missing=True) == 0
    child = meta_repo / "ext"
    ahead_sha = _commit_in_child(child, "one.txt")
    # Pin the manifest at the newer commit, then move HEAD back to v1.0.
    _write_manifest(meta_repo, [("ext", str(bare), "ext", ahead_sha)])
    subprocess.run(["git", "checkout", "v1.0"], cwd=child, check=True, capture_output=True)

    assert show_changes(names=["ext"]) == 0
    out = capsys.readouterr().out
    assert "1 behind" in out
    assert "- " in out
    assert "add one.txt" in out


def test_show_changes_in_sync(meta_repo, make_remote, capsys):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "v1.0")])
    assert sync(jobs=2, include_missing=True) == 0

    assert show_changes(names=["ext"]) == 0
    out = capsys.readouterr().out
    assert "in-sync at" in out


def test_show_changes_unpinned(meta_repo, make_remote, capsys):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", None)])
    assert sync(jobs=2, include_missing=True) == 0

    assert show_changes(names=["ext"]) == 0
    out = capsys.readouterr().out
    assert "unpinned" in out


def test_show_changes_named_not_synced(meta_repo, make_remote, capsys):
    """A named-but-uncloned repo prints 'not synced', not an error."""
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "v1.0")])
    # Don't sync.

    assert show_changes(names=["ext"]) == 0
    out = capsys.readouterr().out
    assert "not synced" in out


def test_show_changes_default_no_clones_errors(meta_repo, make_remote, capsys):
    """No names and nothing cloned: exit non-zero with a helpful message."""
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "v1.0")])

    assert show_changes() == 1
    err = capsys.readouterr().err
    assert "no synced repos" in err


def test_show_changes_rev_not_resolvable(meta_repo, make_remote, capsys):
    """A pinned rev absent from the local clone is reported, not fatal."""
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "v1.0")])
    assert sync(jobs=2, include_missing=True) == 0
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "deadbeef" * 5)])

    assert show_changes(names=["ext"]) == 0
    out = capsys.readouterr().out
    assert "does not resolve locally" in out


def test_show_changes_unknown_name_errors(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "v1.0")])
    assert sync(jobs=2, include_missing=True) == 0

    with pytest.raises(RepoToolError):
        show_changes(names=["nope"])

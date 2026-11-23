# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.

"""Tests for the status command."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from repotool import Repo, _manifest_status, _status_meta, _status_one, status, sync


@pytest.fixture(autouse=True)
def chdir_to_meta(meta_repo, monkeypatch):
    monkeypatch.chdir(meta_repo)


def _write_manifest(meta_repo: Path, repos: list) -> None:
    """4-tuple (name, url, path, rev) — branch defaults to "main" — or 5-tuple."""
    parts: list[str] = []
    for entry in repos:
        if len(entry) == 4:
            name, url, path, rev = entry
            branch = "main"
        else:
            name, url, path, branch, rev = entry
        parts.append(
            f'\n[[repo]]\nname = "{name}"\nurl = "{url}"\npath = "{path}"\n'
            f'branch = "{branch}"\nrev = "{rev}"\n'
        )
    (meta_repo / ".repotool.toml").write_text("".join(parts))


def _git_in(child: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=child, capture_output=True, text=True, check=True
    ).stdout.strip()


def _mkrepo(bare: Path, rev: str = "main", branch: str = "main", name: str = "ext") -> Repo:
    return Repo(name=name, url=str(bare), path=name, branch=branch, rev=rev)


def _bare_tip(bare: Path) -> str:
    return _git_in(bare, "rev-parse", "main")


def test_status_returns_zero_when_aligned(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    sync(jobs=2, include_missing=True)
    assert status() == 0


def test_status_returns_zero_even_when_drifted(meta_repo, make_remote, advance_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    sync(jobs=2, include_missing=True)
    advance_remote(bare)
    _git_in(meta_repo / "ext", "fetch", "origin")
    assert status() == 0


def test_status_filters_to_named_repos(meta_repo, make_remote):
    a = make_remote("a")
    b = make_remote("b")
    _write_manifest(
        meta_repo,
        [("a", str(a), "a", "main"), ("b", str(b), "b", "main")],
    )
    sync(jobs=2, include_missing=True)
    assert status(names=["a"]) == 0


def test_status_unknown_name_errors(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    sync(jobs=2, include_missing=True)
    from repotool import RepoToolError
    with pytest.raises(RepoToolError, match="unknown repo"):
        status(names=["nope"])


def test_status_no_crash_on_missing_child(meta_repo):
    _write_manifest(meta_repo, [("ext", "file:///nowhere.git", "ext", "main")])
    assert status() == 0


def test_status_one_aligned_branch(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    sync(jobs=2, include_missing=True)
    repo = _mkrepo(bare)
    name, branch_cell, rev_cell, current, drift, upstream, dirty = _status_one(repo, meta_repo)
    assert name == "ext"
    assert branch_cell == "main"
    assert rev_cell == "main"
    # Current Rev is always a short SHA, never a branch name.
    assert len(current) == 7
    assert "in-sync" in drift
    assert "in-sync" in upstream
    assert "clean" in dirty


def test_status_one_remote_advanced_shows_behind_in_upstream(meta_repo, make_remote, advance_remote):
    """When origin/<branch> advances and local HEAD has not, Upstream shows 'N behind'."""
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    sync(jobs=2, include_missing=True)
    advance_remote(bare)
    _git_in(meta_repo / "ext", "fetch", "origin")
    repo = _mkrepo(bare)
    _, _, _, _, drift, upstream, _ = _status_one(repo, meta_repo)
    # HEAD still matches manifest rev locally — Manifest reads in-sync.
    assert "in-sync" in drift
    # HEAD is behind origin/main — upstream shows 'N behind'.
    assert "behind" in upstream


def test_status_one_locally_advanced_but_in_sync_with_origin(
    meta_repo, make_remote, advance_remote,
):
    """The case where a user fast-forwarded locally but forgot to snapshot.
    Drift shows the ahead-of-rev count, Upstream shows in-sync — the two columns
    together say 'snapshot the manifest'."""
    bare = make_remote()
    initial_sha = _bare_tip(bare)
    # Pin to the initial SHA so 'drift' is fixed once we advance.
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main", initial_sha)])
    sync(jobs=2, include_missing=True)
    # Advance remote, then fast-forward local to match.
    advance_remote(bare)
    child = meta_repo / "ext"
    _git_in(child, "fetch", "origin")
    _git_in(child, "checkout", "-B", "main")
    _git_in(child, "merge", "--ff-only", "origin/main")
    repo = _mkrepo(bare, rev=initial_sha)
    _, _, _, _, drift, upstream, _ = _status_one(repo, meta_repo)
    assert "ahead" in drift           # HEAD has moved past rev
    assert "in-sync" in upstream      # HEAD matches origin/main


def test_status_one_ahead_of_rev(meta_repo, make_remote):
    """A local commit makes HEAD ahead of the manifest rev — drift shows 'N ahead'.
    The manifest rev must be a SHA so it doesn't follow the local branch."""
    bare = make_remote()
    initial_sha = _bare_tip(bare)
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main", initial_sha)])
    sync(jobs=2, include_missing=True)
    child = meta_repo / "ext"
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=child, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=child, check=True, capture_output=True)
    (child / "local.txt").write_text("local")
    subprocess.run(["git", "add", "."], cwd=child, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "local"], cwd=child, check=True, capture_output=True)
    repo = _mkrepo(bare, rev=initial_sha)
    _, _, _, _, drift, _, _ = _status_one(repo, meta_repo)
    assert "ahead" in drift


def test_status_one_untracked_only(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    sync(jobs=2, include_missing=True)
    (meta_repo / "ext" / "scratch.txt").write_text("untracked")
    repo = _mkrepo(bare)
    *_, dirty = _status_one(repo, meta_repo)
    assert "untracked" in dirty
    assert "dirty" not in dirty


def test_status_one_dirty(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    sync(jobs=2, include_missing=True)
    (meta_repo / "ext" / "f.txt").write_text("modified content")
    repo = _mkrepo(bare)
    *_, dirty = _status_one(repo, meta_repo)
    assert "dirty" in dirty
    assert "untracked" not in dirty


def test_status_one_dirty_with_untracked_is_dirty(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    sync(jobs=2, include_missing=True)
    (meta_repo / "ext" / "f.txt").write_text("modified content")
    (meta_repo / "ext" / "scratch.txt").write_text("untracked")
    repo = _mkrepo(bare)
    *_, dirty = _status_one(repo, meta_repo)
    assert "dirty" in dirty


def test_status_one_local_branch(meta_repo, make_remote):
    """A local branch with no origin/<same> counterpart flips the Status cell."""
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    sync(jobs=2, include_missing=True)
    child = meta_repo / "ext"
    _git_in(child, "branch", "feature")
    repo = _mkrepo(bare)
    *_, status_cell = _status_one(repo, meta_repo)
    assert "local branches" in status_cell


def test_status_one_dirty_overrides_local_branch(meta_repo, make_remote):
    """When both dirty and a local branch are present, dirty wins."""
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    sync(jobs=2, include_missing=True)
    child = meta_repo / "ext"
    _git_in(child, "branch", "feature")
    (child / "f.txt").write_text("modified content")
    repo = _mkrepo(bare)
    *_, status_cell = _status_one(repo, meta_repo)
    assert "dirty" in status_cell
    assert "local branches" not in status_cell


def test_status_one_untracked_overrides_local_branch(meta_repo, make_remote):
    """When both untracked files and a local branch are present, untracked wins.

    Untracked files block `snapshot`; local-only branches don't. Showing
    'local branches' while hiding untracked files would mask why a snapshot
    was refused."""
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    sync(jobs=2, include_missing=True)
    child = meta_repo / "ext"
    _git_in(child, "branch", "feature")
    (child / "scratch.txt").write_text("untracked")
    repo = _mkrepo(bare)
    *_, status_cell = _status_one(repo, meta_repo)
    assert "untracked" in status_cell
    assert "local branches" not in status_cell


def test_status_unpinned_rev_shows_unpinned(meta_repo, make_remote):
    """When `rev` is missing, Manifest Rev shows '(unpinned)' and vs Manifest
    reads 'unpinned' — there's no pin to compare against."""
    bare = make_remote()
    (meta_repo / ".repotool.toml").write_text(
        f'\n[[repo]]\nname = "ext"\nurl = "{bare}"\npath = "ext"\nbranch = "main"\n'
    )
    sync(jobs=2, include_missing=True)
    repo = Repo(name="ext", url=str(bare), path="ext", branch="main", rev="")
    _, _, rev_cell, _, drift, _, _ = _status_one(repo, meta_repo)
    assert "unpinned" in rev_cell
    assert "unpinned" in drift


def test_status_meta_row_shape(meta_repo):
    """The meta row reports name 'meta', blank manifest fields and statuses,
    and a short SHA in the Current Rev column."""
    name, branch_cell, rev_cell, current, drift, upstream, _ = _status_meta(meta_repo)
    assert name == "meta"
    assert branch_cell == ""
    assert rev_cell == ""
    assert len(current) == 7
    assert drift == ""
    assert upstream == ""


def test_status_meta_dirty_when_modified(meta_repo):
    (meta_repo / ".gitkeep").write_text("modified")
    *_, dirty = _status_meta(meta_repo)
    assert "dirty" in dirty


def test_status_one_missing_dir(meta_repo):
    repo = Repo(name="ext", url="x", path="ext", branch="main", rev="main")
    _name, _, _, current, drift, upstream, dirty = _status_one(repo, meta_repo)
    assert "not synced" in current
    assert drift == "-"
    assert upstream == "-"
    assert dirty == "-"


def test_status_one_current_rev_is_always_short_sha(meta_repo, make_remote):
    """Current Rev is always a short SHA — branch-rev (attached HEAD) or
    tag-rev (detached HEAD), the column always shows the revision, not
    the branch name."""
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main", "v1.0")])
    sync(jobs=2, include_missing=True)
    repo = _mkrepo(bare, rev="v1.0", branch="main")
    _, _, _, current, drift, _, _ = _status_one(repo, meta_repo)
    assert current != "main"
    assert len(current) == 7
    assert "in-sync" in drift


def test_manifest_status_unknown_when_rev_unresolvable(tmp_path):
    """If the manifest rev doesn't resolve, the Manifest column reads 'rev absent'."""
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "i"], cwd=tmp_path, check=True, capture_output=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout.strip()
    drift = _manifest_status(tmp_path, "nonexistent-tag", head)
    assert "rev absent" in drift


def test_manifest_status_warns_when_rev_unresolvable(tmp_path, capsys):
    """An unresolvable manifest rev also emits a warning naming the repo — the
    'rev absent' cell alone can't distinguish it from a genuinely in-sync repo."""
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "i"], cwd=tmp_path, check=True, capture_output=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout.strip()
    _manifest_status(tmp_path, "nonexistent-tag", head, "myrepo")
    err = capsys.readouterr().err
    assert "warning:" in err
    assert "myrepo" in err
    assert "nonexistent-tag" in err

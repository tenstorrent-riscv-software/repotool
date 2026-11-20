# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.

"""Tests for the snapshot command."""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

from repotool import RepoToolError, snapshot, sync


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


def _head(p: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=p, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _commit_in_child(child: Path, filename: str = "local-work.txt") -> str:
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=child, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=child, check=True, capture_output=True)
    (child / filename).write_text("local-only commit")
    subprocess.run(["git", "add", "."], cwd=child, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "local"], cwd=child, check=True, capture_output=True)
    return _head(child)


def _load_refs(meta_repo: Path) -> dict[str, str]:
    """Return name → rev (for assertions that only care about the pinned revision)."""
    with open(meta_repo / ".repotool.toml", "rb") as f:
        data = tomllib.load(f)
    return {r["name"]: r["rev"] for r in data["repo"]}


def _load_repos(meta_repo: Path) -> dict[str, tuple[str, str]]:
    """Return name → (branch, rev)."""
    with open(meta_repo / ".repotool.toml", "rb") as f:
        data = tomllib.load(f)
    return {r["name"]: (r["branch"], r["rev"]) for r in data["repo"]}


def test_snapshot_pins_branch_to_current_sha(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    assert sync(jobs=2, include_missing=True) == 0
    sha = _head(meta_repo / "ext")

    assert snapshot() == 0

    refs = _load_refs(meta_repo)
    assert refs["ext"] == sha


def test_snapshot_aborts_when_repo_is_dirty(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    assert sync(jobs=2, include_missing=True) == 0
    (meta_repo / "ext" / "f.txt").write_text("modified")

    assert snapshot() == 1
    # Manifest untouched
    refs = _load_refs(meta_repo)
    assert refs["ext"] == "main"


def test_snapshot_aborts_on_untracked_files(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    assert sync(jobs=2, include_missing=True) == 0
    (meta_repo / "ext" / "untracked.txt").write_text("hi")

    assert snapshot() == 1
    refs = _load_refs(meta_repo)
    assert refs["ext"] == "main"


def test_snapshot_fills_in_missing_rev(meta_repo, make_remote):
    """An entry without a `rev` line has one inserted by snapshot, pointing at
    the current HEAD. Other fields are untouched."""
    bare = make_remote()
    (meta_repo / ".repotool.toml").write_text(
        f'\n[[repo]]\nname = "ext"\nurl = "{bare}"\npath = "ext"\nbranch = "main"\n'
    )
    assert sync(jobs=2, include_missing=True) == 0
    child = meta_repo / "ext"
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=child,
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    assert snapshot() == 0
    contents = (meta_repo / ".repotool.toml").read_text()
    assert f'rev = "{head}"' in contents
    # Other fields are still there.
    assert 'branch = "main"' in contents
    assert 'name = "ext"' in contents


def test_snapshot_skips_missing_clone(meta_repo, make_remote, capsys):
    """Uncloned children are skipped, not failures: snapshot still succeeds and
    leaves their manifest entries untouched."""
    a = make_remote("a")
    b = make_remote("b")
    _write_manifest(
        meta_repo,
        [("a", str(a), "a", "main"), ("b", str(b), "b", "main")],
    )
    # Only clone `a`. `b` is left in the manifest but uncloned.
    assert sync(names=["a"], jobs=2) == 0

    assert snapshot() == 0
    out = capsys.readouterr().out
    assert "not synced" in out
    refs = _load_refs(meta_repo)
    # `a` was snapshotted; `b` is untouched.
    assert refs["a"] != "main"
    assert refs["b"] == "main"


def test_snapshot_atomic_one_dirty_blocks_all(meta_repo, make_remote):
    a = make_remote("a")
    b = make_remote("b")
    _write_manifest(
        meta_repo,
        [("a", str(a), "a", "main"), ("b", str(b), "b", "main")],
    )
    assert sync(jobs=2, include_missing=True) == 0
    (meta_repo / "a" / "dirty.txt").write_text("x")

    assert snapshot() == 1
    refs = _load_refs(meta_repo)
    # Neither was updated — atomic.
    assert refs["a"] == "main"
    assert refs["b"] == "main"


def test_snapshot_filters_to_named_repos(meta_repo, make_remote):
    a = make_remote("a")
    b = make_remote("b")
    _write_manifest(
        meta_repo,
        [("a", str(a), "a", "main"), ("b", str(b), "b", "main")],
    )
    assert sync(jobs=2, include_missing=True) == 0

    assert snapshot(names=["a"]) == 0
    refs = _load_refs(meta_repo)
    assert refs["a"] == _head(meta_repo / "a")
    assert refs["b"] == "main"


def test_snapshot_unknown_name_errors(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    assert sync(jobs=2, include_missing=True) == 0
    with pytest.raises(RepoToolError, match="unknown repo"):
        snapshot(names=["nope"])


def test_snapshot_idempotent_when_already_pinned(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    assert sync(jobs=2, include_missing=True) == 0
    sha = _head(meta_repo / "ext")

    # First snapshot: pin to sha.
    assert snapshot() == 0
    assert _load_refs(meta_repo)["ext"] == sha
    mtime1 = (meta_repo / ".repotool.toml").stat().st_mtime_ns

    # Second snapshot: already pinned, no updates needed.
    assert snapshot() == 0
    assert _load_refs(meta_repo)["ext"] == sha
    # File should not have been rewritten (mtime unchanged).
    mtime2 = (meta_repo / ".repotool.toml").stat().st_mtime_ns
    assert mtime1 == mtime2


def test_snapshot_preserves_comments_and_formatting(meta_repo, make_remote):
    bare = make_remote()
    manifest = (
        "# top-of-file comment\n"
        "\n"
        "[[repo]]\n"
        "name = \"ext\"  # inline comment on name\n"
        f"url    = \"{bare}\"\n"
        "path   = \"ext\"\n"
        "branch = \"main\"  # branch context\n"
        "rev    = \"main\"  # pin me later\n"
        "\n"
        "# trailing comment\n"
    )
    (meta_repo / ".repotool.toml").write_text(manifest)
    assert sync(jobs=2, include_missing=True) == 0
    sha = _head(meta_repo / "ext")

    assert snapshot() == 0
    text = (meta_repo / ".repotool.toml").read_text()
    assert "# top-of-file comment" in text
    assert "# inline comment on name" in text
    assert "# branch context" in text
    assert "# pin me later" in text
    assert "# trailing comment" in text
    assert sha in text
    assert "rev    = \"main\"" not in text


def test_snapshot_fails_on_unpushed_local_commit(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    assert sync(jobs=2, include_missing=True) == 0
    _commit_in_child(meta_repo / "ext")

    assert snapshot() == 1
    # Manifest untouched — the local commit isn't on the remote.
    assert _load_refs(meta_repo)["ext"] == "main"


def test_snapshot_no_verify_remote_allows_unpushed_commit(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    assert sync(jobs=2, include_missing=True) == 0
    local_sha = _commit_in_child(meta_repo / "ext")

    assert snapshot(verify_remote=False) == 0
    assert _load_refs(meta_repo)["ext"] == local_sha


def test_snapshot_succeeds_when_head_matches_remote_branch(meta_repo, make_remote):
    """HEAD is at origin/main right after sync — the default verify check should pass."""
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    assert sync(jobs=2, include_missing=True) == 0
    sha = _head(meta_repo / "ext")

    assert snapshot() == 0
    assert _load_refs(meta_repo)["ext"] == sha


def test_snapshot_detached_at_origin_branch_tip(meta_repo, make_remote, advance_remote):
    """Detached HEAD at origin/<branch> tip is allowed — common after a force-
    push followed by `git checkout origin/<branch>`. The snapshot adopts the
    manifest branch."""
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main", "main")])
    assert sync(jobs=2, include_missing=True) == 0
    new_sha = advance_remote(bare, "force-push-result")
    # Simulate the user's workflow: fetch and checkout the remote tip detached.
    subprocess.run(
        ["git", "fetch", "origin"], cwd=meta_repo / "ext",
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", "origin/main"], cwd=meta_repo / "ext",
        check=True, capture_output=True,
    )
    assert _head(meta_repo / "ext") == new_sha
    # HEAD is detached but at origin/main tip — snapshot should succeed.
    assert snapshot() == 0
    repos = _load_repos(meta_repo)
    assert repos["ext"] == ("main", new_sha)


def test_snapshot_detached_not_at_origin_branch_tip_fails(meta_repo, make_remote, advance_remote):
    """Detached HEAD pointing somewhere other than origin/<manifest-branch> tip
    is rejected — the user has to explicitly checkout a branch."""
    bare = make_remote()
    advance_remote(bare, "after-v1")  # origin/main now ahead of v1.0
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main", "main")])
    assert sync(jobs=2, include_missing=True) == 0
    child = meta_repo / "ext"
    # v1.0 was tagged at the initial commit by make_remote; origin/main is one
    # commit beyond. Detach onto v1.0 — not at origin/main tip.
    subprocess.run(
        ["git", "checkout", "v1.0"], cwd=child, check=True, capture_output=True,
    )
    assert snapshot() != 0


def test_snapshot_detached_with_no_verify_remote(meta_repo, make_remote):
    """--no-verify-remote still accepts a detached HEAD at the locally-known
    origin/<branch> tip (without re-fetching)."""
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main", "main")])
    assert sync(jobs=2, include_missing=True) == 0
    child = meta_repo / "ext"
    subprocess.run(
        ["git", "checkout", "origin/main"], cwd=child, check=True, capture_output=True,
    )
    head_sha = _head(child)
    assert snapshot(verify_remote=False) == 0
    repos = _load_repos(meta_repo)
    assert repos["ext"] == ("main", head_sha)


def test_snapshot_atomic_one_unpushed_blocks_all(meta_repo, make_remote):
    a = make_remote("a")
    b = make_remote("b")
    _write_manifest(
        meta_repo,
        [("a", str(a), "a", "main"), ("b", str(b), "b", "main")],
    )
    assert sync(jobs=2, include_missing=True) == 0
    _commit_in_child(meta_repo / "a")

    assert snapshot() == 1
    refs = _load_refs(meta_repo)
    # Neither was updated — atomic abort.
    assert refs["a"] == "main"
    assert refs["b"] == "main"


def test_snapshot_multiple_repos_each_get_their_own_sha(meta_repo, make_remote):
    a = make_remote("a")
    b = make_remote("b")
    _write_manifest(
        meta_repo,
        [("a", str(a), "a", "main"), ("b", str(b), "b", "main")],
    )
    assert sync(jobs=2, include_missing=True) == 0
    sha_a = _head(meta_repo / "a")
    sha_b = _head(meta_repo / "b")

    assert snapshot() == 0
    refs = _load_refs(meta_repo)
    assert refs["a"] == sha_a
    assert refs["b"] == sha_b

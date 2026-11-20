# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.

"""Tests for the reset command."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

from repotool import Repo, _reset_one, reset, sync


def _push_branch(bare: Path, branch: str, message: str = "feature") -> str:
    """Create `branch` on a bare remote with one commit; return its SHA."""
    with tempfile.TemporaryDirectory() as td:
        work = Path(td) / "work"
        subprocess.run(["git", "clone", str(bare), str(work)], check=True,
                       capture_output=True)
        for k, v in {"user.email": "test@example.com", "user.name": "t"}.items():
            subprocess.run(["git", "-C", str(work), "config", k, v], check=True)
        subprocess.run(["git", "-C", str(work), "checkout", "-b", branch],
                       check=True, capture_output=True)
        (work / f"{branch}.txt").write_text(message)
        subprocess.run(["git", "-C", str(work), "add", "."], check=True)
        subprocess.run(["git", "-C", str(work), "commit", "-m", message],
                       check=True, capture_output=True)
        sha = subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"],
                             check=True, capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "-C", str(work), "push", "origin", branch],
                       check=True, capture_output=True)
    return sha


def _branch_of(p: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=p,
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_reset_widens_refspec_when_branch_changes(meta_repo, make_remote):
    """A single-branch clone (from `sync --depth`) tracks only one branch. When
    the manifest `branch` changes, reset must widen the fetch refspec so
    origin/<new branch> is fetched and the realignment resolves it."""
    bare = make_remote()
    feat_sha = _push_branch(bare, "feature")

    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main", "main")])
    assert sync(jobs=2, include_missing=True, depth=1) == 0

    _write_manifest(meta_repo, [("ext", str(bare), "ext", "feature", feat_sha)])
    assert reset(jobs=2) == 0
    assert _branch_of(meta_repo / "ext") == "feature"
    assert _head(meta_repo / "ext") == feat_sha


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
    (child / filename).write_text("important local work")
    subprocess.run(["git", "add", "."], cwd=child, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "local"], cwd=child, check=True, capture_output=True)
    return _head(child)


def test_reset_aligns_to_origin_main_after_remote_advances(meta_repo, make_remote, advance_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    sync(jobs=2, include_missing=True)
    head_before = _head(meta_repo / "ext")

    advance_remote(bare)
    assert reset(jobs=2) == 0
    head_after = _head(meta_repo / "ext")
    assert head_after != head_before, "reset should advance local HEAD to origin/main"


def test_reset_removes_untracked_files(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    sync(jobs=2, include_missing=True)
    (meta_repo / "ext" / "stuff.txt").write_text("untracked")
    assert reset(jobs=2) == 0
    assert not (meta_repo / "ext" / "stuff.txt").exists()


def test_reset_refuses_when_local_commits_ahead(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    sync(jobs=2, include_missing=True)
    head_with_local = _commit_in_child(meta_repo / "ext")

    rc = reset(jobs=2)
    assert rc != 0
    # Local commit preserved
    assert _head(meta_repo / "ext") == head_with_local
    assert (meta_repo / "ext" / "local-work.txt").exists()


def test_reset_emits_summary_error_on_failure(meta_repo, make_remote, capsys):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    sync(jobs=2, include_missing=True)
    _commit_in_child(meta_repo / "ext")
    capsys.readouterr()  # discard prior output

    rc = reset(jobs=2)
    assert rc != 0
    err = capsys.readouterr().err
    assert "reset failed" in err
    assert "ext" in err


def test_reset_truncates_long_discard_list(meta_repo, make_remote, capsys):
    """When many commits would be discarded, the log is capped at 20 lines with
    a '... and N more' summary."""
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    sync(jobs=2, include_missing=True)
    child = meta_repo / "ext"
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=child, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=child, check=True, capture_output=True)
    for i in range(25):
        (child / f"f{i}.txt").write_text(str(i))
        subprocess.run(["git", "add", "."], cwd=child, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", f"local {i}"], cwd=child, check=True, capture_output=True)

    rc = reset(jobs=2)
    assert rc != 0
    out = capsys.readouterr().out
    # 20 commit lines max + the '... and N more' summary.
    commit_lines = [line for line in out.splitlines() if "local " in line]
    assert len(commit_lines) == 20
    assert "... and 5 more" in out


def test_reset_force_discards_local_commits(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    sync(jobs=2, include_missing=True)
    _commit_in_child(meta_repo / "ext")

    assert reset(force=True, jobs=2) == 0
    assert not (meta_repo / "ext" / "local-work.txt").exists()


def test_reset_tag_ref(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "v1.0")])
    sync(jobs=2, include_missing=True)
    assert reset(jobs=2) == 0


def test_reset_idempotent_when_already_aligned(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    sync(jobs=2, include_missing=True)
    head_before = _head(meta_repo / "ext")
    assert reset(jobs=2) == 0
    assert _head(meta_repo / "ext") == head_before


def test_reset_skips_missing_child(meta_repo, capsys):
    """An uncloned child is reported as skipped, not failed."""
    _write_manifest(meta_repo, [("ext", "file:///nowhere.git", "ext", "main")])
    assert reset(jobs=2, include_missing=True) == 0
    out = capsys.readouterr().out
    assert "not synced" in out


def test_reset_errors_when_nothing_to_do(meta_repo, make_remote):
    """`rt reset` with no clones present and no --all is an error, not a no-op."""
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    from repotool import RepoToolError
    with pytest.raises(RepoToolError, match="no repos to reset"):
        reset(jobs=2)


def test_reset_skips_uncloned_by_default(meta_repo, make_remote):
    """Without --all, reset operates only on already-cloned repos."""
    a = make_remote("a")
    b = make_remote("b")
    _write_manifest(
        meta_repo,
        [("a", str(a), "a", "main"), ("b", str(b), "b", "main")],
    )
    sync(names=["a"], jobs=2)
    # `b` is in the manifest but not cloned. Default reset should silently skip it.
    assert reset(jobs=2) == 0
    # With --all, the missing `b` is surfaced as a yellow `skipped` line but
    # still doesn't fail the run.
    assert reset(jobs=2, include_missing=True) == 0


def test_reset_one_verbose_captures_git_invocations(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    sync(jobs=2, include_missing=True)
    repo = Repo(name="ext", url=str(bare), path="ext", branch="main", rev="main")
    result = _reset_one(repo, meta_repo, force=False, verbose=True)
    assert result.ok
    joined = "\n".join(result.verbose_log)
    assert "git fetch" in joined
    assert "git reset --hard" in joined
    assert "git checkout" in joined


def test_reset_filters_to_named_repos(meta_repo, make_remote, advance_remote):
    a = make_remote("a")
    b = make_remote("b")
    _write_manifest(
        meta_repo,
        [("a", str(a), "a", "main"), ("b", str(b), "b", "main")],
    )
    sync(jobs=2, include_missing=True)
    head_a_before = _head(meta_repo / "a")
    head_b_before = _head(meta_repo / "b")

    advance_remote(a)
    advance_remote(b)

    assert reset(names=["a"], jobs=2) == 0
    # `a` realigned to the new tip; `b` left alone.
    assert _head(meta_repo / "a") != head_a_before
    assert _head(meta_repo / "b") == head_b_before


def test_reset_unknown_name_errors(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    sync(jobs=2, include_missing=True)
    from repotool import RepoToolError
    with pytest.raises(RepoToolError, match="unknown repo"):
        reset(names=["nope"], jobs=2)


def test_reset_failed_on_unresolvable_ref(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    sync(jobs=2, include_missing=True)
    # Edit the manifest to point at a ref that doesn't exist
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "nope-not-a-ref")])
    rc = reset(jobs=2)
    assert rc != 0

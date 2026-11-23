# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.

"""Tests for the fast-forward command."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

from repotool import RepoToolError, fast_forward, sync


def _push_branch(bare: Path, branch: str, message: str = "feature") -> str:
    """Create `branch` on a bare remote (branched from main) with one extra
    commit; return its SHA. main's tip stays an ancestor of `branch`."""
    with tempfile.TemporaryDirectory() as td:
        work = Path(td) / "work"
        subprocess.run(["git", "clone", str(bare), str(work)], check=True,
                       capture_output=True)
        for k, v in {"user.email": "t@t", "user.name": "t"}.items():
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


def test_ffwd_widens_refspec_when_branch_changes(meta_repo, make_remote):
    """Detached HEAD in a single-branch clone (from `sync --depth`), manifest
    `branch` changed to an untracked branch. fast-forward must widen the fetch
    refspec so origin/<new branch> exists, then advance-and-attach to it rather
    than skipping with `no remote branch`."""
    bare = make_remote()
    feat_sha = _push_branch(bare, "feature")

    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main", "main")])
    assert sync(jobs=2, include_missing=True, depth=1) == 0
    child = meta_repo / "ext"
    # Detach at main's tip (an ancestor of feature) so ffwd proceeds past its
    # "current branch != manifest branch" guard.
    subprocess.run(["git", "-C", str(child), "checkout", "--detach", "HEAD"],
                   check=True, capture_output=True)

    _write_manifest(meta_repo, [("ext", str(bare), "ext", "feature", feat_sha)])
    assert fast_forward(jobs=2) == 0
    assert _branch_of(child) == "feature"
    assert _head(child) == feat_sha


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


def _commit_in_child(child: Path, filename: str = "local.txt") -> str:
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=child, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=child, check=True, capture_output=True)
    (child / filename).write_text("local")
    subprocess.run(["git", "add", "."], cwd=child, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "local"], cwd=child, check=True, capture_output=True)
    return _head(child)


def test_fast_forward_advances_when_behind(meta_repo, make_remote, advance_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    assert sync(jobs=2, include_missing=True) == 0
    head_before = _head(meta_repo / "ext")

    advance_remote(bare)
    assert fast_forward(jobs=2) == 0
    head_after = _head(meta_repo / "ext")
    assert head_after != head_before


def test_fast_forward_up_to_pinned_rev_is_not_stale(
    meta_repo, make_remote, advance_remote, capsys,
):
    """When HEAD is behind the manifest rev and origin/<branch> has advanced
    *to* that same rev, fast-forward catches HEAD up to the pinned rev — the
    manifest is now in-sync, not stale, so it must not tell the user to
    snapshot."""
    bare = make_remote()
    # Pin rev to the future tip: sync at branch name (loose), then advance
    # origin and rewrite the manifest to the new tip SHA before ffwd.
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    assert sync(jobs=2, include_missing=True) == 0
    head_before = _head(meta_repo / "ext")

    tip = advance_remote(bare)
    _write_manifest(meta_repo, [("ext", str(bare), "ext", tip)])

    assert fast_forward(jobs=2) == 0
    assert _head(meta_repo / "ext") == tip
    assert _head(meta_repo / "ext") != head_before
    out = capsys.readouterr().out
    assert "matches manifest rev" in out
    assert "stale" not in out


def test_fast_forward_up_to_date_when_aligned(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    assert sync(jobs=2, include_missing=True) == 0
    head_before = _head(meta_repo / "ext")

    assert fast_forward(jobs=2) == 0
    assert _head(meta_repo / "ext") == head_before


def test_fast_forward_named_skip_is_a_failure(meta_repo, make_remote, advance_remote, capsys):
    """`rt ffwd <name>` on a repo that can't be advanced (here: on a feature
    branch) must exit non-zero — the user asked for that repo specifically."""
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    assert sync(jobs=2, include_missing=True) == 0
    advance_remote(bare, "tip")
    # Move onto a feature branch — fast-forward will skip it (wrong branch).
    subprocess.run(
        ["git", "checkout", "-b", "feature"], cwd=meta_repo / "ext",
        check=True, capture_output=True,
    )

    # Bulk `rt ffwd`: a skip is fine, exit 0.
    assert fast_forward(jobs=2) == 0

    # Named `rt ffwd ext`: same skip, now exit non-zero with summary error.
    capsys.readouterr()  # clear prior output
    rc = fast_forward(names=["ext"], jobs=2)
    assert rc != 0
    err = capsys.readouterr().err
    assert "fast-forward failed" in err
    assert "ext" in err


def test_fast_forward_emits_summary_error_on_failure(meta_repo, make_remote, capsys):
    """A non-repo directory at the manifest path is a hard failure; the run
    must end with an `error: fast-forward failed ...` line on stderr."""
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    (meta_repo / "ext").mkdir()
    (meta_repo / "ext" / "junk.txt").write_text("hi")

    rc = fast_forward(jobs=2)
    assert rc != 0
    err = capsys.readouterr().err
    assert "fast-forward failed" in err
    assert "ext" in err


def test_fast_forward_skips_dirty_repo(meta_repo, make_remote, advance_remote, capsys):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    assert sync(jobs=2, include_missing=True) == 0
    head_before = _head(meta_repo / "ext")
    (meta_repo / "ext" / "f.txt").write_text("modified")
    advance_remote(bare)

    assert fast_forward(jobs=2) == 0  # skip is not failure
    # Local HEAD untouched.
    assert _head(meta_repo / "ext") == head_before
    out = capsys.readouterr().out
    assert "dirty" in out


def test_fast_forward_skips_diverged(meta_repo, make_remote, advance_remote, capsys):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    assert sync(jobs=2, include_missing=True) == 0
    local_sha = _commit_in_child(meta_repo / "ext")
    advance_remote(bare)

    assert fast_forward(jobs=2) == 0
    # Local commit preserved — no merge happened.
    assert _head(meta_repo / "ext") == local_sha
    out = capsys.readouterr().out
    assert "ahead" in out


def test_fast_forward_attaches_when_detached_at_origin_tip(
    meta_repo, make_remote, capsys,
):
    """Detached at the SHA that's already origin/<branch>: HEAD doesn't
    move, but we still attach to <branch> so the user lands in the same
    state they'd be in after a clean sync."""
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "v1.0")])
    assert sync(jobs=2, include_missing=True) == 0
    child = meta_repo / "ext"
    # v1.0 == origin/main, so sync attaches to main; detach to set up the case.
    subprocess.run(["git", "checkout", "--detach"], cwd=child, check=True,
                   capture_output=True, text=True)
    head_before = _head(child)

    assert fast_forward(jobs=2) == 0
    # HEAD didn't move…
    assert _head(child) == head_before
    # …but we're attached to main now.
    cur_branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=child,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert cur_branch == "main"
    out = capsys.readouterr().out
    assert "attached to main" in out


def test_fast_forward_attaches_when_detached_and_behind(
    meta_repo, make_remote, advance_remote, capsys,
):
    """The common case the manifest causes: rev is an older SHA, sync left
    HEAD detached, then origin/<branch> has moved on. Fast-forward should
    advance HEAD *and* attach it to the manifest branch."""
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "v1.0")])
    assert sync(jobs=2, include_missing=True) == 0
    child = meta_repo / "ext"
    # sync attaches to main at the tip; detach so HEAD is behind once origin moves.
    subprocess.run(["git", "checkout", "--detach"], cwd=child, check=True,
                   capture_output=True, text=True)
    head_before = _head(child)
    advance_remote(bare)

    assert fast_forward(jobs=2) == 0
    # HEAD moved forward.
    assert _head(child) != head_before
    # And we're attached to the manifest branch now.
    cur_branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=child,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert cur_branch == "main"
    out = capsys.readouterr().out
    assert "attached to main" in out


def test_fast_forward_skips_detached_when_local_branch_has_work(
    meta_repo, make_remote, advance_remote, capsys,
):
    """Detached HEAD plus a local <branch> with commits not on origin/<branch>:
    advancing would silently rewrite the local branch, so skip and complain."""
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "v1.0")])
    assert sync(jobs=2, include_missing=True) == 0
    child = meta_repo / "ext"
    # Put a commit on the local 'main' branch ref that isn't on origin/main.
    subprocess.run(["git", "checkout", "main"], cwd=child, check=True, capture_output=True)
    _commit_in_child(child, filename="branch-local.txt")
    # Detach again to recreate the scenario.
    subprocess.run(["git", "checkout", "v1.0"], cwd=child, check=True, capture_output=True)
    advance_remote(bare)
    head_before = _head(child)

    assert fast_forward(jobs=2) == 0  # skip is not a failure
    assert _head(child) == head_before
    out = capsys.readouterr().out
    assert "not safely advancable" in out


def test_fast_forward_skips_when_on_different_branch(meta_repo, make_remote, capsys):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    assert sync(jobs=2, include_missing=True) == 0
    # Switch to a local branch that isn't the manifest ref.
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=meta_repo / "ext",
                   check=True, capture_output=True)
    head_before = _head(meta_repo / "ext")

    assert fast_forward(jobs=2) == 0
    assert _head(meta_repo / "ext") == head_before
    out = capsys.readouterr().out
    assert "on branch 'feature'" in out


def test_fast_forward_skips_missing_clone(meta_repo, make_remote, capsys):
    """An uncloned child fits the partial-checkout workflow: skipped, not failed."""
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    # Don't sync.
    assert fast_forward(jobs=2) == 0
    out = capsys.readouterr().out
    assert "not synced" in out


def test_fast_forward_filters_to_named_repos(meta_repo, make_remote, advance_remote):
    a = make_remote("a")
    b = make_remote("b")
    _write_manifest(
        meta_repo,
        [("a", str(a), "a", "main"), ("b", str(b), "b", "main")],
    )
    assert sync(jobs=2, include_missing=True) == 0
    head_a_before = _head(meta_repo / "a")
    head_b_before = _head(meta_repo / "b")

    advance_remote(a)
    advance_remote(b)

    assert fast_forward(names=["a"], jobs=2) == 0
    # a advanced; b untouched.
    assert _head(meta_repo / "a") != head_a_before
    assert _head(meta_repo / "b") == head_b_before


def test_fast_forward_unknown_name_errors(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    assert sync(jobs=2, include_missing=True) == 0
    with pytest.raises(RepoToolError, match="unknown repo"):
        fast_forward(names=["nope"], jobs=2)

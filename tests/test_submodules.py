# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.

"""Tests for the per-repo `submodules = true` manifest key (sync + reset)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from repotool import ManifestError, load_manifest, reset, sync


@pytest.fixture(autouse=True)
def chdir_to_meta(meta_repo, monkeypatch):
    monkeypatch.chdir(meta_repo)


@pytest.fixture(autouse=True)
def allow_file_submodules(monkeypatch):
    # Modern git refuses the file:// transport for submodules; re-enable it for
    # every git process (including those repotool spawns) via env-injected
    # config so the local bare remotes in these tests work.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + args, cwd=cwd, check=True, capture_output=True, text=True)


SUBPATH = "vendor/lib"


@pytest.fixture
def child_with_submodule(tmp_path_factory, make_remote, allow_file_submodules):
    """A bare remote whose main branch embeds `make_remote`'s repo as a
    submodule at SUBPATH. Returns the child bare path."""
    sub_bare = make_remote("subm")
    work = tmp_path_factory.mktemp("child-work")
    _git(["init", "-b", "main"], work)
    _git(["config", "user.email", "t@e.com"], work)
    _git(["config", "user.name", "T"], work)
    (work / "top.txt").write_text("top")
    _git(["add", "."], work)
    _git(["commit", "-m", "initial"], work)
    _git(["submodule", "add", str(sub_bare), SUBPATH], work)
    _git(["commit", "-m", "add submodule"], work)
    bare = work.parent / f"{work.name}.bare.git"
    _git(["clone", "--bare", str(work), str(bare)], work.parent)
    return bare


def _write_manifest(meta_repo: Path, url: str, *, submodules: bool) -> None:
    sub = "\nsubmodules = true" if submodules else ""
    (meta_repo / ".repotool.toml").write_text(
        f'[[repo]]\nname = "child"\nurl = "{url}"\npath = "child"\n'
        f'branch = "main"{sub}\n'
    )


# --- manifest validation -------------------------------------------------

def test_submodules_key_loads_as_bool(meta_repo, child_with_submodule):
    _write_manifest(meta_repo, str(child_with_submodule), submodules=True)
    (repo,) = load_manifest(meta_repo)
    assert repo.submodules is True


def test_submodules_defaults_false_when_omitted(meta_repo, make_remote):
    _write_manifest(meta_repo, str(make_remote()), submodules=False)
    (repo,) = load_manifest(meta_repo)
    assert repo.submodules is False


def test_submodules_must_be_bool(meta_repo):
    (meta_repo / ".repotool.toml").write_text(
        '[[repo]]\nname = "x"\nurl = "u"\npath = "x"\nbranch = "main"\n'
        'submodules = "yes"\n'
    )
    with pytest.raises(ManifestError):
        load_manifest(meta_repo)


# --- sync ----------------------------------------------------------------

def test_sync_without_key_leaves_submodule_empty(meta_repo, child_with_submodule):
    _write_manifest(meta_repo, str(child_with_submodule), submodules=False)
    assert sync(jobs=2, include_missing=True) == 0
    subdir = meta_repo / "child" / SUBPATH
    assert subdir.exists()                    # gitlink dir is created
    assert not (subdir / "f.txt").exists()    # but not populated


def test_sync_with_key_populates_submodule(meta_repo, child_with_submodule):
    _write_manifest(meta_repo, str(child_with_submodule), submodules=True)
    assert sync(jobs=2, include_missing=True) == 0
    assert (meta_repo / "child" / SUBPATH / "f.txt").read_text() == "initial"


def test_sync_existing_populates_after_key_added(meta_repo, child_with_submodule):
    # First sync without the key: submodule stays empty.
    _write_manifest(meta_repo, str(child_with_submodule), submodules=False)
    assert sync(jobs=2, include_missing=True) == 0
    assert not (meta_repo / "child" / SUBPATH / "f.txt").exists()
    # Turn the key on and re-sync the existing clone: now it populates.
    _write_manifest(meta_repo, str(child_with_submodule), submodules=True)
    assert sync(jobs=2) == 0
    assert (meta_repo / "child" / SUBPATH / "f.txt").read_text() == "initial"


def test_sync_syncs_submodule_url_change(meta_repo, tmp_path_factory, make_remote):
    """When an upstream commit repoints a submodule at a new URL, sync must
    `git submodule sync` so the update fetches from the new remote. Without it
    the update fetches the stale configured URL and fails on the missing SHA."""
    sub_a = make_remote("sub-a")   # submodule's original remote (has c_s1)

    # sub_b: a moved remote carrying an extra commit c_s2 that exists ONLY there.
    sub_work = tmp_path_factory.mktemp("sub-work") / "w"
    _git(["clone", str(sub_a), str(sub_work)], sub_work.parent)
    (sub_work / "moved.txt").write_text("only on the new remote")
    _git(["add", "."], sub_work)
    _git(["commit", "-m", "c_s2"], sub_work)
    c_s2 = _git(["rev-parse", "HEAD"], sub_work).stdout.strip()
    sub_b = sub_work.parent / "sub-b.git"
    _git(["clone", "--bare", str(sub_work), str(sub_b)], sub_work.parent)

    # Child: c1 pins the submodule at sub_a; c2 repoints it at sub_b and bumps
    # the gitlink to c_s2 (reachable only from sub_b).
    child = tmp_path_factory.mktemp("child-url") / "w"
    child.mkdir(parents=True)
    _git(["init", "-b", "main"], child)
    _git(["config", "user.email", "t@e.com"], child)
    _git(["config", "user.name", "T"], child)
    _git(["submodule", "add", str(sub_a), SUBPATH], child)
    _git(["commit", "-m", "c1"], child)
    c1 = _git(["rev-parse", "HEAD"], child).stdout.strip()
    _git(["submodule", "set-url", "--", SUBPATH, str(sub_b)], child)
    _git(["-C", SUBPATH, "fetch", str(sub_b), "main"], child)
    _git(["-C", SUBPATH, "checkout", c_s2], child)
    _git(["add", ".gitmodules", SUBPATH], child)
    _git(["commit", "-m", "c2: move submodule url"], child)
    c2 = _git(["rev-parse", "HEAD"], child).stdout.strip()
    bare = child.parent / "child-url.git"
    _git(["clone", "--bare", str(child), str(bare)], child.parent)

    def _pin(rev: str) -> None:
        (meta_repo / ".repotool.toml").write_text(
            f'[[repo]]\nname = "child"\nurl = "{bare}"\npath = "child"\n'
            f'branch = "main"\nrev = "{rev}"\nsubmodules = true\n'
        )

    # First sync lands c1: submodule configured against sub_a.
    _pin(c1)
    assert sync(jobs=2, include_missing=True) == 0
    subdir = meta_repo / "child" / SUBPATH
    assert not (subdir / "moved.txt").exists()

    # Re-pin to c2 (repoints the submodule at sub_b) and re-sync the existing
    # clone across the URL change.
    _pin(c2)
    assert sync(jobs=2) == 0
    assert _git(["rev-parse", "HEAD"], subdir).stdout.strip() == c_s2
    assert (subdir / "moved.txt").read_text() == "only on the new remote"


# --- reset ---------------------------------------------------------------

def test_reset_restores_submodule_tracked_and_untracked(meta_repo, child_with_submodule):
    _write_manifest(meta_repo, str(child_with_submodule), submodules=True)
    assert sync(jobs=2, include_missing=True) == 0
    subdir = meta_repo / "child" / SUBPATH
    # Dirty the submodule: modify a tracked file and drop an untracked one.
    (subdir / "f.txt").write_text("tampered")
    (subdir / "junk.txt").write_text("junk")
    assert reset(jobs=2) == 0
    assert (subdir / "f.txt").read_text() == "initial"   # tracked restored
    assert not (subdir / "junk.txt").exists()            # untracked cleaned


def _commit_in_submodule(subdir: Path, filename: str = "ahead.txt") -> str:
    # No per-repo identity needed: the session `_git_identity` fixture provides
    # a global one (the submodule working tree is a fresh clone without its own).
    (subdir / filename).write_text("local work")
    _git(["add", "."], subdir)
    _git(["commit", "-m", "submodule local commit"], subdir)
    return _git(["rev-parse", "HEAD"], subdir).stdout.strip()


def test_reset_refuses_submodule_commits_ahead_without_force(meta_repo, child_with_submodule):
    _write_manifest(meta_repo, str(child_with_submodule), submodules=True)
    assert sync(jobs=2, include_missing=True) == 0
    subdir = meta_repo / "child" / SUBPATH
    ahead = _commit_in_submodule(subdir)
    # The submodule now has a commit not on the pinned gitlink; reset must
    # refuse (like the superproject guard) rather than silently orphan it.
    assert reset(jobs=2) == 1
    assert _git(["rev-parse", "HEAD"], subdir).stdout.strip() == ahead
    assert (subdir / "ahead.txt").exists()


def test_reset_force_discards_submodule_commits_ahead(meta_repo, child_with_submodule):
    _write_manifest(meta_repo, str(child_with_submodule), submodules=True)
    assert sync(jobs=2, include_missing=True) == 0
    subdir = meta_repo / "child" / SUBPATH
    ahead = _commit_in_submodule(subdir)
    assert reset(jobs=2, force=True) == 0
    # Submodule moved back to the pinned SHA; the ahead commit is gone from HEAD.
    assert _git(["rev-parse", "HEAD"], subdir).stdout.strip() != ahead
    assert not (subdir / "ahead.txt").exists()


# --- rev overrides ---------------------------------------------------------

@pytest.fixture
def child_with_two_submodule_pins(tmp_path_factory, make_remote, advance_remote,
                                  allow_file_submodules):
    """A child whose history pins the submodule at two different SHAs: commit
    c1 pins sub1, and a later commit c2 bumps the submodule to sub2. Returns
    (bare, c1, sub1, c2, sub2)."""
    sub_bare = make_remote("subm-pins")
    sub1 = _git(["rev-parse", "main"], sub_bare).stdout.strip()
    work = tmp_path_factory.mktemp("pins-work") / "w"
    work.mkdir()
    _git(["init", "-b", "main"], work)
    _git(["config", "user.email", "t@e.com"], work)
    _git(["config", "user.name", "T"], work)
    (work / "top.txt").write_text("top")
    _git(["add", "."], work)
    _git(["commit", "-m", "initial"], work)
    _git(["submodule", "add", str(sub_bare), SUBPATH], work)
    _git(["commit", "-m", "add submodule"], work)
    c1 = _git(["rev-parse", "HEAD"], work).stdout.strip()

    sub2 = advance_remote(sub_bare, "sub2")
    sub_clone = work / SUBPATH
    _git(["fetch", "origin"], sub_clone)
    _git(["checkout", sub2], sub_clone)
    _git(["add", SUBPATH], work)
    _git(["commit", "-m", "bump submodule"], work)
    c2 = _git(["rev-parse", "HEAD"], work).stdout.strip()

    bare = work.parent / f"{work.name}.bare.git"
    _git(["clone", "--bare", str(work), str(bare)], work.parent)
    return bare, c1, sub1, c2, sub2


def _sub_head(child: Path) -> str:
    return _git(["rev-parse", "HEAD"], child / SUBPATH).stdout.strip()


def test_override_moves_submodules_to_overridden_gitlinks(
    meta_repo, child_with_two_submodule_pins,
):
    bare, c1, sub1, c2, sub2 = child_with_two_submodule_pins
    _write_manifest(meta_repo, str(bare), submodules=True)
    child = meta_repo / "child"

    assert sync(jobs=2, include_missing=True) == 0
    assert _git(["rev-parse", "HEAD"], child).stdout.strip() == c2
    assert _sub_head(child) == sub2

    assert sync(jobs=2, revs=[f"child={c1}"]) == 0
    assert _git(["rev-parse", "HEAD"], child).stdout.strip() == c1
    assert _sub_head(child) == sub1

    # Without the override the manifest's checkout (c2) and gitlink come back.
    assert sync(jobs=2) == 0
    assert _sub_head(child) == sub2

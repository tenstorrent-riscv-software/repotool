# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.

"""Tests for the check command. Uses real on-disk git repos in tmp_path."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from repotool import ManifestError, check


@pytest.fixture(autouse=True)
def chdir_to_meta(meta_repo, monkeypatch):
    monkeypatch.chdir(meta_repo)


def _write_manifest(meta_repo: Path, repos: list) -> None:
    """Each entry: (name, url, path, branch, rev). Empty rev => unpinned line omitted."""
    parts: list[str] = []
    for name, url, path, branch, rev in repos:
        block = (
            f'\n[[repo]]\nname = "{name}"\nurl = "{url}"\npath = "{path}"\n'
            f'branch = "{branch}"\n'
        )
        if rev:
            block += f'rev = "{rev}"\n'
        parts.append(block)
    (meta_repo / ".repotool.toml").write_text("".join(parts))


def _remote_sha(bare: Path, ref: str = "main") -> str:
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=bare, capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_check_sha_at_tip(meta_repo, make_remote):
    bare = make_remote()
    sha = _remote_sha(bare)
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main", sha)])
    assert check(jobs=2) == 0
    # Nothing is cloned — check is network-only.
    assert not (meta_repo / "ext").exists()


def test_check_tag_rev(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main", "v1.0")])
    assert check(jobs=2) == 0


def test_check_branch_name_rev(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main", "main")])
    assert check(jobs=2) == 0


def test_check_unpinned(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main", "")])
    assert check(jobs=2) == 0


def test_check_sha_below_tip_is_unverified_not_failure(meta_repo, make_remote, advance_remote):
    bare = make_remote()
    old = _remote_sha(bare)
    advance_remote(bare, "second")
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main", old)])
    # A SHA below the tip can't be verified without fetching, but that's not a
    # failure — check still exits 0.
    assert check(jobs=2) == 0


def test_check_missing_branch_fails(meta_repo, make_remote):
    bare = make_remote()
    sha = _remote_sha(bare)
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "nonexistent", sha)])
    assert check(jobs=2) == 1


def test_check_missing_named_rev_fails(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main", "v9.9")])
    assert check(jobs=2) == 1


def test_check_unreachable_fails(meta_repo, tmp_path):
    missing = tmp_path / "does-not-exist.git"
    _write_manifest(meta_repo, [("ext", str(missing), "ext", "main", "main")])
    assert check(jobs=2) == 1


def test_check_https_rewrite_is_noop_for_file_url(meta_repo, make_remote):
    # file:// / bare-path URLs are a no-op through _to_https, so --https still
    # exercises the rewrite path without breaking reachability.
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main", "v1.0")])
    assert check(jobs=2, https=True) == 0


def test_check_named_filter_and_mixed_results(meta_repo, make_remote):
    good = make_remote("good")
    bad = make_remote("bad")
    _write_manifest(meta_repo, [
        ("good", str(good), "good", "main", "v1.0"),
        ("bad", str(bad), "bad", "nonexistent", "v1.0"),
    ])
    # Whole manifest: the bad branch makes the run fail.
    assert check(jobs=2) == 1
    # Narrow to the healthy repo only: passes.
    assert check(names=["good"], jobs=2) == 0


def test_check_invalid_manifest_raises(meta_repo):
    (meta_repo / ".repotool.toml").write_text('[[repo]]\nname = "x"\n')
    with pytest.raises(ManifestError):
        check(jobs=2)


# --- check = false exclusion ---------------------------------------------

def _manifest_with_excluded(meta_repo: Path, good: Path, unreachable: Path) -> None:
    (meta_repo / ".repotool.toml").write_text(
        f'[[repo]]\nname = "pub"\nurl = "{good}"\npath = "pub"\n'
        f'branch = "main"\nrev = "v1.0"\n'
        f'\n[[repo]]\nname = "priv"\nurl = "{unreachable}"\npath = "priv"\n'
        f'branch = "main"\nrev = "main"\ncheck = false\n'
    )


def test_check_false_repo_excluded_in_bulk(meta_repo, make_remote, tmp_path):
    good = make_remote("good")
    unreachable = tmp_path / "nope.git"   # would fail if it were checked
    _manifest_with_excluded(meta_repo, good, unreachable)
    # The unreachable repo is check=false, so bulk check ignores it and passes.
    assert check(jobs=2) == 0


def test_check_false_repo_checked_when_named(meta_repo, make_remote, tmp_path):
    good = make_remote("good")
    unreachable = tmp_path / "nope.git"
    _manifest_with_excluded(meta_repo, good, unreachable)
    # Naming it explicitly overrides the exclusion — now it's contacted and fails.
    assert check(names=["priv"], jobs=2) == 1


def test_check_all_excluded_exits_zero(meta_repo, tmp_path):
    unreachable = tmp_path / "nope.git"
    (meta_repo / ".repotool.toml").write_text(
        f'[[repo]]\nname = "priv"\nurl = "{unreachable}"\npath = "priv"\n'
        f'branch = "main"\ncheck = false\n'
    )
    assert check(jobs=2) == 0


def test_check_flag_must_be_bool(meta_repo):
    (meta_repo / ".repotool.toml").write_text(
        '[[repo]]\nname = "x"\nurl = "u"\npath = "x"\nbranch = "main"\n'
        'check = "no"\n'
    )
    with pytest.raises(ManifestError):
        check(jobs=2)

# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.

"""Tests for the sync command. Uses real on-disk git repos in tmp_path."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

from repotool import (
    REV_OVERRIDE_ENV,
    ManifestError,
    Repo,
    RepoToolError,
    _parse_rev_entries,
    _sync_one,
    _walk_orphans,
    reset,
    status,
    sync,
)


@pytest.fixture(autouse=True)
def chdir_to_meta(meta_repo, monkeypatch):
    monkeypatch.chdir(meta_repo)


@pytest.fixture(autouse=True)
def no_ambient_rev_overrides(monkeypatch):
    # A developer's shell (or a CI job) may export RT_REVS; tests set it
    # explicitly when they want it.
    monkeypatch.delenv(REV_OVERRIDE_ENV, raising=False)


def _write_manifest(meta_repo: Path, repos: list) -> None:
    """repos entries are either:
       - (name, url, path, rev)             — branch defaults to "main"
       - (name, url, path, branch, rev)     — both explicit
    """
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


def _branch(p: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=p, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _head(p: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=p, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _push_branch(bare: Path, branch: str, message: str = "feature",
                 start: str | None = None) -> str:
    """Create `branch` on a bare remote with one commit; return its SHA.
    `start` (a rev) is the commit the branch forks from; default is HEAD."""
    with tempfile.TemporaryDirectory() as td:
        work = Path(td) / "work"
        subprocess.run(["git", "clone", str(bare), str(work)], check=True,
                       capture_output=True)
        for k, v in {"user.email": "test@example.com",
                     "user.name": "Test User"}.items():
            subprocess.run(["git", "-C", str(work), "config", k, v], check=True)
        checkout = ["checkout", "-b", branch] + ([start] if start else [])
        subprocess.run(["git", "-C", str(work), *checkout],
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


def test_sync_widens_refspec_when_branch_changes(meta_repo, make_remote):
    """A single-branch clone (from `sync --depth`) tracks only one branch in
    its fetch refspec. When the manifest `branch` changes, sync must widen the
    refspec so `git fetch origin` populates origin/<new branch> — otherwise the
    checkout fails and status shows `no remote`."""
    bare = make_remote()
    feat_sha = _push_branch(bare, "feature")

    # First sync shallow → single-branch clone tracking only main.
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main", "main")])
    assert sync(jobs=2, include_missing=True, depth=1) == 0
    fetch_spec = subprocess.run(
        ["git", "-C", str(meta_repo / "ext"), "config", "--get-all",
         "remote.origin.fetch"], check=True, capture_output=True, text=True,
    ).stdout
    assert "feature" not in fetch_spec  # not tracked yet

    # Manifest branch changes to feature; a plain fetch alone wouldn't see it.
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "feature", feat_sha)])
    assert sync(jobs=2) == 0
    assert _branch(meta_repo / "ext") == "feature"
    assert _head(meta_repo / "ext") == feat_sha


def test_first_sync_clones(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    assert sync(jobs=2, include_missing=True) == 0
    assert (meta_repo / "ext" / ".git").exists()
    assert _branch(meta_repo / "ext") == "main"


def test_sync_idempotent(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    assert sync(jobs=2, include_missing=True) == 0
    assert sync(jobs=2, include_missing=True) == 0


def _remote_sha(bare: Path, ref: str = "main") -> str:
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=bare, capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_sync_tag_ref_at_branch_tip_attaches(meta_repo, make_remote):
    """A pinned rev (here the v1.0 tag) that resolves to the tip of
    origin/<branch> attaches HEAD to that branch rather than leaving it
    detached. In the fixture v1.0 sits at main's tip."""
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "v1.0")])
    assert sync(jobs=2, include_missing=True) == 0
    assert _branch(meta_repo / "ext") == "main"


def test_sync_pinned_below_tip_stays_detached(meta_repo, make_remote, advance_remote):
    """A pinned SHA that is *not* the tip of origin/<branch> leaves HEAD
    detached — there's no branch label that honestly points there."""
    bare = make_remote()
    base = _remote_sha(bare, "main")  # the initial commit, soon to be behind
    advance_remote(bare, "more")      # origin/main now ahead of `base`
    _write_manifest(meta_repo, [("ext", str(bare), "ext", base)])
    assert sync(jobs=2, include_missing=True) == 0
    child = meta_repo / "ext"
    assert _branch(child) == "HEAD"
    assert _head(child) == base


def test_sync_at_tip_does_not_move_existing_local_branch(
    meta_repo, make_remote, advance_remote,
):
    """checkout-only contract: when the pinned rev sits at origin/<branch> tip
    but a local <branch> exists pointing *behind* it, sync must not advance the
    local branch. HEAD stays detached at the rev."""
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    assert sync(jobs=2, include_missing=True) == 0
    child = meta_repo / "ext"
    local_main = _head(child)  # local main sits at the initial commit

    tip = advance_remote(bare, "more")  # origin/main advances; local main stale
    _write_manifest(meta_repo, [("ext", str(bare), "ext", tip)])
    assert sync(jobs=2, include_missing=True) == 0

    assert _head(child) == tip, "HEAD should be checked out at the pinned tip"
    assert _branch(child) == "HEAD", "must stay detached rather than move main"
    local_main_after = subprocess.run(
        ["git", "rev-parse", "refs/heads/main"], cwd=child,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert local_main_after == local_main, "local main must not have moved"


def test_sync_nested_path(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "services/api", "main")])
    assert sync(jobs=2, include_missing=True) == 0
    assert (meta_repo / "services" / "api" / ".git").exists()


def test_sync_does_not_fast_forward_branch(meta_repo, make_remote, advance_remote):
    """Locked-in semantic: branch ref is checkout-only; sync does not move local branch."""
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    assert sync(jobs=2, include_missing=True) == 0
    head_before = _head(meta_repo / "ext")

    advance_remote(bare, "more")
    assert sync(jobs=2, include_missing=True) == 0
    head_after = _head(meta_repo / "ext")
    assert head_before == head_after, "sync must not fast-forward local main"


def test_sync_emits_summary_error_on_failure(meta_repo, make_remote, capsys):
    """After per-repo lines, sync must emit a single `error: sync failed ...`
    line so a glance at the bottom of the log makes the failure unmissable."""
    bare = make_remote()
    _write_manifest(
        meta_repo,
        [("good", str(bare), "good", "main"),
         ("bad", str(bare), "bad", "nope-not-a-ref")],
    )
    rc = sync(jobs=2, include_missing=True)
    assert rc != 0
    err = capsys.readouterr().err
    assert "sync failed" in err
    assert "1 of 2" in err
    assert "bad" in err
    # Successful repos must not appear in the summary.
    assert "good," not in err and ": good" not in err


def test_sync_no_summary_on_success(meta_repo, make_remote, capsys):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    assert sync(jobs=2, include_missing=True) == 0
    assert "sync failed" not in capsys.readouterr().err


def test_sync_invalid_ref_fails_but_leaves_clone(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "nope-not-a-ref")])
    rc = sync(jobs=2, include_missing=True)
    assert rc != 0
    # The clone landed; only the checkout failed. Dir stays.
    assert (meta_repo / "ext" / ".git").exists()


def test_sync_existing_non_git_dir_fails(meta_repo, make_remote):
    bare = make_remote()
    (meta_repo / "ext").mkdir()
    (meta_repo / "ext" / "junk.txt").write_text("hi")
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    assert sync(jobs=2, include_missing=True) != 0


def test_sync_filters_to_named_repos(meta_repo, make_remote):
    a = make_remote("a")
    b = make_remote("b")
    _write_manifest(
        meta_repo,
        [("a", str(a), "a", "main"), ("b", str(b), "b", "main")],
    )
    assert sync(names=["a"], jobs=2) == 0
    assert (meta_repo / "a" / ".git").exists()
    assert not (meta_repo / "b").exists()


def test_sync_strips_trailing_slash_from_name(meta_repo, make_remote):
    """Tab-completion may append a slash to the repo name; it's tolerated."""
    a = make_remote("a")
    b = make_remote("b")
    _write_manifest(
        meta_repo,
        [("a", str(a), "a", "main"), ("b", str(b), "b", "main")],
    )
    assert sync(names=["a/"], jobs=2) == 0
    assert (meta_repo / "a" / ".git").exists()
    assert not (meta_repo / "b").exists()


def test_sync_unpinned_checks_out_branch(meta_repo, make_remote):
    """When `rev` is omitted, sync clones and lands on the manifest `branch`
    (attached HEAD)."""
    bare = make_remote()
    # Manifest without rev.
    (meta_repo / ".repotool.toml").write_text(
        f'\n[[repo]]\nname = "ext"\nurl = "{bare}"\npath = "ext"\nbranch = "main"\n'
    )
    assert sync(jobs=2, include_missing=True) == 0
    child = meta_repo / "ext"
    cur_branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=child,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert cur_branch == "main"


def test_sync_errors_when_nothing_to_do(meta_repo, make_remote):
    """`rt sync` with no clones present and no --all is an error, not a no-op."""
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    from repotool import RepoToolError
    with pytest.raises(RepoToolError, match="no repos to sync"):
        sync(jobs=2)


def test_sync_skips_uncloned_by_default(meta_repo, make_remote):
    """Without --all, sync operates only on already-cloned repos."""
    a = make_remote("a")
    b = make_remote("b")
    _write_manifest(
        meta_repo,
        [("a", str(a), "a", "main"), ("b", str(b), "b", "main")],
    )
    assert sync(names=["a"], jobs=2) == 0
    # Default sync skips `b` because it's not cloned.
    assert sync(jobs=2) == 0
    assert not (meta_repo / "b").exists()
    # --all picks `b` up and clones it.
    assert sync(jobs=2, include_missing=True) == 0
    assert (meta_repo / "b" / ".git").exists()


def test_sync_filtered_does_not_report_other_declared_paths_as_orphans(
    meta_repo, make_remote, capsys,
):
    """Regression: filtered sync used to compute the declared-paths set from the
    filtered subset, mislabeling every other manifest entry as an orphan."""
    a = make_remote("a")
    b = make_remote("b")
    c = make_remote("c")
    _write_manifest(
        meta_repo,
        [("a", str(a), "a", "main"),
         ("b", str(b), "b", "main"),
         ("c", str(c), "c", "main")],
    )
    # Pre-sync a and b so they're real on-disk repos that the orphan walk
    # would otherwise see.
    assert sync(jobs=2, include_missing=True) == 0
    capsys.readouterr()  # discard

    assert sync(names=["c"], jobs=2) == 0
    out = capsys.readouterr().out
    assert "Orphan directories" not in out
    assert "\n  a\n" not in out
    assert "\n  b\n" not in out


def test_sync_with_reference(meta_repo, make_remote, tmp_path_factory):
    """`--reference PATH` is passed to git clone and shares objects."""
    bare = make_remote()
    # A standalone reference clone of the same upstream, not in any worktree.
    ref_dir = tmp_path_factory.mktemp("ref") / "ext"
    subprocess.run(["git", "clone", str(bare), str(ref_dir)], check=True, capture_output=True)

    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    assert sync(names=["ext"], jobs=1, reference=str(ref_dir)) == 0

    alternates = meta_repo / "ext" / ".git" / "objects" / "info" / "alternates"
    assert alternates.exists(), "alternates file should exist (object sharing)"
    assert str((ref_dir / ".git" / "objects").resolve()) in alternates.read_text()


def test_sync_reference_requires_single_repo(meta_repo, make_remote):
    a = make_remote("a")
    b = make_remote("b")
    _write_manifest(
        meta_repo,
        [("a", str(a), "a", "main"), ("b", str(b), "b", "main")],
    )
    from repotool import RepoToolError
    # No names selected → applies to all → not exactly one.
    with pytest.raises(RepoToolError, match="exactly one repo"):
        sync(jobs=1, reference="/tmp/whatever")
    # Multiple names also rejected.
    with pytest.raises(RepoToolError, match="exactly one repo"):
        sync(names=["a", "b"], jobs=1, reference="/tmp/whatever")


def test_sync_unknown_name_errors(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    from repotool import RepoToolError
    with pytest.raises(RepoToolError, match="unknown repo"):
        sync(names=["nope"], jobs=2)


def test_sync_by_manifest_path(meta_repo, make_remote):
    """A repo can be named by its manifest path — what build scripts know."""
    bare = make_remote()
    _write_manifest(meta_repo, [("dtc", str(bare), "src/dtc", "main")])
    assert sync(names=["src/dtc"], jobs=1) == 0
    assert (meta_repo / "src" / "dtc" / ".git").exists()


def test_filter_repos_paths_and_names():
    from repotool import _filter_repos
    repos = (
        Repo(name="dtc", url="u", path="src/dtc", branch="main", rev=""),
        Repo(name="ui", url="u", path="ui", branch="main", rev=""),
    )
    assert [r.name for r in _filter_repos(repos, ["src/dtc"])] == ["dtc"]
    # Trailing slash from tab completion is tolerated on paths too.
    assert [r.name for r in _filter_repos(repos, ["src/dtc/"])] == ["dtc"]
    # Name and path of the same repo don't select it twice.
    assert [r.name for r in _filter_repos(repos, ["dtc", "src/dtc"])] == ["dtc"]


def test_filter_repos_name_beats_path_collision():
    from repotool import _filter_repos
    repos = (
        Repo(name="a", url="u", path="x/a", branch="main", rev=""),
        Repo(name="b", url="u", path="a", branch="main", rev=""),
    )
    # "a" is repo a's name and repo b's path: the name wins.
    assert [r.name for r in _filter_repos(repos, ["a"])] == ["a"]


def test_sync_missing_manifest_raises(meta_repo):
    with pytest.raises(ManifestError):
        sync(jobs=1)


def test_walk_orphans_finds_undeclared(meta_repo, make_remote):
    extra = make_remote("extra")
    subprocess.run(
        ["git", "clone", str(extra), str(meta_repo / "junk")],
        check=True, capture_output=True,
    )
    orphans = _walk_orphans(meta_repo, declared_paths={"ext"})
    rels = {str(o.relative_to(meta_repo)) for o in orphans}
    assert rels == {"junk"}


def test_walk_orphans_excludes_declared(meta_repo, make_remote):
    bare = make_remote()
    subprocess.run(
        ["git", "clone", str(bare), str(meta_repo / "ext")],
        check=True, capture_output=True,
    )
    assert _walk_orphans(meta_repo, declared_paths={"ext"}) == []


def test_walk_orphans_excludes_meta_root(meta_repo):
    # Meta repo itself has .git but is not a child of itself.
    assert _walk_orphans(meta_repo, declared_paths=set()) == []


def test_walk_orphans_does_not_descend_into_repos(meta_repo, make_remote):
    """If a child has nested directories, we don't list those as orphans."""
    bare = make_remote()
    subprocess.run(
        ["git", "clone", str(bare), str(meta_repo / "ext")],
        check=True, capture_output=True,
    )
    (meta_repo / "ext" / "subdir").mkdir()
    # subdir doesn't have a .git, so even if we descended it wouldn't show
    # as orphan. But we want to verify the walk PRUNES at the git boundary.
    orphans = _walk_orphans(meta_repo, declared_paths={"ext"})
    assert orphans == []


def test_walk_orphans_skips_ignored_subtree(meta_repo, make_remote):
    """Clones under an ignored path (and the ignored dir itself) aren't orphans."""
    extra = make_remote("extra")
    # A nested clone under an ignored subtree, plus a top-level orphan clone.
    subprocess.run(
        ["git", "clone", str(extra), str(meta_repo / "west" / "repos" / "foo")],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "clone", str(extra), str(meta_repo / "junk")],
        check=True, capture_output=True,
    )
    orphans = _walk_orphans(
        meta_repo, declared_paths=set(), ignores={"west"},
    )
    rels = {str(o.relative_to(meta_repo)) for o in orphans}
    assert rels == {"junk"}


def test_walk_orphans_ignores_default_to_none(meta_repo, make_remote):
    """Omitting `ignores` walks everything (backward-compatible default)."""
    extra = make_remote("extra")
    subprocess.run(
        ["git", "clone", str(extra), str(meta_repo / "west" / "repos" / "foo")],
        check=True, capture_output=True,
    )
    orphans = _walk_orphans(meta_repo, declared_paths=set())
    rels = {str(o.relative_to(meta_repo)) for o in orphans}
    assert rels == {"west/repos/foo"}


def test_sync_one_verbose_captures_git_invocations(meta_repo, make_remote):
    """With verbose=True the result carries every git invocation's output."""
    bare = make_remote()
    repo = Repo(name="ext", url=str(bare), path="ext", branch="main", rev="main")
    result = _sync_one(repo, meta_repo, verbose=True)
    assert result.ok
    assert result.verbose_log, "verbose_log should be populated"
    joined = "\n".join(result.verbose_log)
    assert "git clone" in joined
    assert "git checkout" in joined


def test_sync_one_quiet_leaves_verbose_log_empty(meta_repo, make_remote):
    bare = make_remote()
    repo = Repo(name="ext", url=str(bare), path="ext", branch="main", rev="main")
    result = _sync_one(repo, meta_repo, verbose=False)
    assert result.ok
    assert result.verbose_log == []


def test_sync_one_verbose_captures_failed_run(meta_repo, make_remote):
    """A failed git command's stderr should still be captured under verbose."""
    bare = make_remote()
    repo = Repo(name="ext", url=str(bare), path="ext", branch="main", rev="nope-not-a-ref")
    result = _sync_one(repo, meta_repo, verbose=True)
    assert not result.ok
    joined = "\n".join(result.verbose_log)
    assert "git clone" in joined
    assert "git checkout nope-not-a-ref" in joined


def test_sync_depth_produces_shallow_clone(meta_repo, make_remote, advance_remote):
    """--depth N produces a shallow clone (the `shallow` file exists)."""
    bare = make_remote()
    # Two extra commits so depth=1 visibly truncates history.
    advance_remote(bare, "second")
    advance_remote(bare, "third")
    # `file://` URL bypasses git's local-path optimization that would
    # otherwise silently ignore --depth.
    _write_manifest(meta_repo, [("ext", f"file://{bare}", "ext", "main")])
    assert sync(jobs=2, include_missing=True, depth=1) == 0
    assert (meta_repo / "ext" / ".git" / "shallow").exists()
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=meta_repo / "ext",
        capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    assert len(log) == 1, f"depth=1 should leave exactly 1 commit, got {len(log)}"


def test_sync_depth_only_affects_fresh_clones(meta_repo, make_remote, advance_remote):
    """A repo already cloned full-depth stays full-depth even when --depth is passed."""
    bare = make_remote()
    advance_remote(bare, "second")
    _write_manifest(meta_repo, [("ext", f"file://{bare}", "ext", "main")])
    assert sync(jobs=2, include_missing=True) == 0  # full clone
    assert not (meta_repo / "ext" / ".git" / "shallow").exists()
    # Now sync again with --depth — already-cloned path takes over (no clone).
    assert sync(jobs=2, depth=1) == 0
    assert not (meta_repo / "ext" / ".git" / "shallow").exists()


def test_sync_depth_reaches_rev_on_non_default_branch(
    meta_repo, make_remote, tmp_path_factory,
):
    """Regression: a shallow clone without --branch fetches only the default
    branch's history, so a pinned SHA on a side branch isn't reachable and
    `checkout` fails with `reference is not a tree`. The fix passes
    `--branch <repo.branch>` alongside `--depth`."""
    bare = make_remote()

    # Add a side branch with a unique commit and push it.
    work = tmp_path_factory.mktemp("side-work")
    subprocess.run(["git", "clone", str(bare), str(work)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=work, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=work, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-b", "side"], cwd=work, check=True, capture_output=True)
    (work / "side.txt").write_text("on side branch")
    subprocess.run(["git", "add", "."], cwd=work, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "side commit"], cwd=work, check=True, capture_output=True)
    side_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=work,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    subprocess.run(["git", "push", "origin", "side"], cwd=work, check=True, capture_output=True)

    _write_manifest(meta_repo, [("ext", f"file://{bare}", "ext", "side", side_sha)])
    assert sync(jobs=2, include_missing=True, depth=1) == 0
    assert (meta_repo / "ext" / ".git" / "shallow").exists()
    assert _head(meta_repo / "ext") == side_sha


def test_sync_depth_recovers_rev_below_shallow_window(
    meta_repo, make_remote, advance_remote,
):
    """A pinned SHA that is still reachable on the branch but sits below the
    `--depth` window isn't in the shallow clone, so the initial checkout fails.
    sync recovers by fetching the rev directly by SHA rather than failing."""
    bare = make_remote()
    deep = advance_remote(bare, "second")   # pinned, then buried
    advance_remote(bare, "third")
    advance_remote(bare, "fourth")
    _write_manifest(meta_repo, [("ext", f"file://{bare}", "ext", "main", deep)])
    assert sync(jobs=2, include_missing=True, depth=1) == 0
    assert _head(meta_repo / "ext") == deep


def test_sync_depth_rebased_away_rev_reports_diagnosis(
    meta_repo, make_remote, tmp_path_factory, capsys,
):
    """When the pinned SHA has been rebased away on the remote — unreachable
    from every ref and GC'd — no fetch can recover it. sync must fail with a
    diagnosis that points at the manifest, not a raw `checkout` error."""
    bare = make_remote()
    work = tmp_path_factory.mktemp("rebase-work")
    subprocess.run(["git", "clone", str(bare), str(work)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=work, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=work, check=True, capture_output=True)
    # Commit we will pin, push, then orphan.
    (work / "old.txt").write_text("doomed")
    subprocess.run(["git", "add", "."], cwd=work, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "doomed"], cwd=work, check=True, capture_output=True)
    orphan = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=work, capture_output=True, text=True, check=True,
    ).stdout.strip()
    subprocess.run(["git", "push", "origin", "main"], cwd=work, check=True, capture_output=True)
    # Rewrite history so `orphan` is unreachable, force-push, then GC the bare
    # so the object is genuinely gone (mirrors a server's reachability refusal).
    subprocess.run(["git", "reset", "--hard", "HEAD~1"], cwd=work, check=True, capture_output=True)
    (work / "replacement.txt").write_text("new")
    subprocess.run(["git", "add", "."], cwd=work, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "replacement"], cwd=work, check=True, capture_output=True)
    subprocess.run(["git", "push", "-f", "origin", "main"], cwd=work, check=True, capture_output=True)
    subprocess.run(["git", "reflog", "expire", "--all", "--expire=now"], cwd=bare, check=True, capture_output=True)
    subprocess.run(["git", "gc", "--prune=now"], cwd=bare, check=True, capture_output=True)

    _write_manifest(meta_repo, [("ext", f"file://{bare}", "ext", "main", orphan)])
    rc = sync(jobs=2, include_missing=True, depth=1)
    assert rc != 0
    cap = capsys.readouterr()
    combined = cap.out + cap.err
    assert "rebased away" in combined
    assert orphan[:7] in combined


def test_sync_depth_skips_manifest_reference(meta_repo, make_remote):
    """When --depth is set, the manifest `reference` clause is ignored (shallow
    and alternates don't mix)."""
    a_bare = make_remote("a")
    b_bare = make_remote("b")
    (meta_repo / ".repotool.toml").write_text(
        f'\n[[repo]]\nname = "a"\nurl = "file://{a_bare}"\npath = "a"\nbranch = "main"\nrev = "main"\n'
        f'\n[[repo]]\nname = "b"\nurl = "file://{b_bare}"\npath = "b"\nbranch = "main"\nrev = "main"\n'
        f'reference = "a"\n'
    )
    assert sync(jobs=4, include_missing=True, depth=1) == 0
    assert not (meta_repo / "b" / ".git" / "objects" / "info" / "alternates").exists()
    assert (meta_repo / "b" / ".git" / "shallow").exists()


def test_sync_depth_rejects_with_reference_flag(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    from repotool import RepoToolError
    with pytest.raises(RepoToolError, match="mutually exclusive"):
        sync(names=["ext"], jobs=1, reference="/tmp/whatever", depth=1)


def test_sync_depth_rejects_non_positive(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    from repotool import RepoToolError
    with pytest.raises(RepoToolError, match="positive"):
        sync(jobs=1, include_missing=True, depth=0)


def test_sync_manifest_reference_shares_objects(meta_repo, make_remote):
    """A manifest `reference = "<other>"` makes the second repo clone with
    `--reference` pointed at the first's on-disk clone — they share storage."""
    a_bare = make_remote("a")
    b_bare = make_remote("b")
    (meta_repo / ".repotool.toml").write_text(
        f'\n[[repo]]\nname = "a"\nurl = "{a_bare}"\npath = "a"\nbranch = "main"\nrev = "main"\n'
        f'\n[[repo]]\nname = "b"\nurl = "{b_bare}"\npath = "b"\nbranch = "main"\nrev = "main"\n'
        f'reference = "a"\n'
    )
    assert sync(jobs=4, include_missing=True) == 0
    assert (meta_repo / "a" / ".git").exists()
    assert (meta_repo / "b" / ".git").exists()
    alternates = meta_repo / "b" / ".git" / "objects" / "info" / "alternates"
    assert alternates.exists(), "b should share objects with a via --reference"
    assert str((meta_repo / "a" / ".git" / "objects").resolve()) in alternates.read_text()


def test_sync_manifest_reference_uses_existing_clone(meta_repo, make_remote):
    """When the referenced repo is already cloned, syncing only the referrer
    still wires up object sharing."""
    a_bare = make_remote("a")
    b_bare = make_remote("b")
    (meta_repo / ".repotool.toml").write_text(
        f'\n[[repo]]\nname = "a"\nurl = "{a_bare}"\npath = "a"\nbranch = "main"\nrev = "main"\n'
        f'\n[[repo]]\nname = "b"\nurl = "{b_bare}"\npath = "b"\nbranch = "main"\nrev = "main"\n'
        f'reference = "a"\n'
    )
    assert sync(names=["a"], jobs=2) == 0
    assert (meta_repo / "a" / ".git").exists()
    assert not (meta_repo / "b").exists()

    assert sync(names=["b"], jobs=2) == 0
    alternates = meta_repo / "b" / ".git" / "objects" / "info" / "alternates"
    assert alternates.exists()


def test_reference_clone_across_worktrees(meta_repo, make_remote, tmp_path_factory, monkeypatch):
    """When syncing in a peer worktree, --reference-if-able shares objects."""
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    assert sync(jobs=2, include_missing=True) == 0
    assert (meta_repo / "ext" / ".git").exists()

    # Create a linked worktree of the meta-repo. The manifest is untracked, so
    # the new worktree's checkout won't have it — write it in directly.
    wt = tmp_path_factory.mktemp("peer") / "wt"
    subprocess.run(
        ["git", "-C", str(meta_repo), "worktree", "add", "-b", "wt", str(wt)],
        check=True, capture_output=True,
    )
    _write_manifest(wt, [("ext", str(bare), "ext", "main")])

    monkeypatch.chdir(wt)
    assert sync(jobs=2, include_missing=True) == 0
    assert (wt / "ext" / ".git").exists()

    # The alternates file proves --reference-if-able actually shared objects.
    alternates = wt / "ext" / ".git" / "objects" / "info" / "alternates"
    assert alternates.exists(), "alternates file should exist (object sharing)"
    text = alternates.read_text()
    assert str((meta_repo / "ext" / ".git" / "objects").resolve()) in text or \
        str((meta_repo / "ext").resolve()) in text


def test_reference_peer_only_from_primary_worktree(
    meta_repo, make_remote, tmp_path_factory, monkeypatch
):
    """Peer discovery never references a clone in a secondary worktree.

    With the child cloned only in a secondary worktree (not the primary),
    syncing in a third worktree must fall back to a plain clone rather than
    sharing objects with the fragile secondary clone.
    """
    bare = make_remote()
    # Primary worktree: manifest present, but do NOT clone the child here.
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    assert not (meta_repo / "ext" / ".git").exists()

    # Secondary worktree A: clone the child here.
    wt_a = tmp_path_factory.mktemp("peer_a") / "wt"
    subprocess.run(
        ["git", "-C", str(meta_repo), "worktree", "add", "-b", "wt_a", str(wt_a)],
        check=True, capture_output=True,
    )
    _write_manifest(wt_a, [("ext", str(bare), "ext", "main")])
    monkeypatch.chdir(wt_a)
    assert sync(jobs=2, include_missing=True) == 0
    assert (wt_a / "ext" / ".git").exists()

    # Secondary worktree B: sync here. The only existing clone is in wt_a (a
    # secondary worktree), so peer discovery must decline it -> plain clone.
    wt_b = tmp_path_factory.mktemp("peer_b") / "wt"
    subprocess.run(
        ["git", "-C", str(meta_repo), "worktree", "add", "-b", "wt_b", str(wt_b)],
        check=True, capture_output=True,
    )
    _write_manifest(wt_b, [("ext", str(bare), "ext", "main")])
    monkeypatch.chdir(wt_b)
    assert sync(jobs=2, include_missing=True) == 0
    assert (wt_b / "ext" / ".git").exists()

    alternates = wt_b / "ext" / ".git" / "objects" / "info" / "alternates"
    assert not alternates.exists(), \
        "must not reference a clone living in a secondary worktree"


# --- rev overrides (--rev / RT_REVS) --------------------------------------

def test_parse_rev_entries_splits_on_whitespace_and_commas():
    assert _parse_rev_entries(["a=main, b=v1.0\nc=refs/x/y"], "--rev") == [
        ("a", "main"), ("b", "v1.0"), ("c", "refs/x/y"),
    ]
    assert _parse_rev_entries([""], "RT_REVS") == []
    assert _parse_rev_entries([" , \t "], "RT_REVS") == []


def test_override_sha_fresh_clone_detaches_and_leaves_manifest(meta_repo, make_remote, advance_remote):
    bare = make_remote()
    base = _remote_sha(bare)
    tip = advance_remote(bare, "more")
    _write_manifest(meta_repo, [("ext", str(bare), "ext", base)])
    manifest_before = (meta_repo / ".repotool.toml").read_bytes()

    assert sync(jobs=2, include_missing=True, revs=[f"ext={tip}"]) == 0

    child = meta_repo / "ext"
    assert _branch(child) == "HEAD"
    assert _head(child) == tip
    assert (meta_repo / ".repotool.toml").read_bytes() == manifest_before


def test_override_existing_clone_then_plain_sync_restores(meta_repo, make_remote, advance_remote):
    bare = make_remote()
    base = _remote_sha(bare)
    tip = advance_remote(bare, "more")
    _write_manifest(meta_repo, [("ext", str(bare), "ext", base)])
    child = meta_repo / "ext"
    assert sync(jobs=2, include_missing=True) == 0
    assert _head(child) == base

    assert sync(jobs=2, revs=[f"ext={tip}"]) == 0
    assert _branch(child) == "HEAD"
    assert _head(child) == tip

    assert sync(jobs=2) == 0
    assert _head(child) == base


def test_override_sha_below_shallow_window_on_other_branch(meta_repo, make_remote, advance_remote):
    bare = make_remote()
    base = _remote_sha(bare)
    advance_remote(bare, "more")  # main moves on; depth 1 excludes `base`
    feat = _push_branch(bare, "feature", start=base)
    # `file://` so git honours --depth (a local path clone ignores it).
    _write_manifest(meta_repo, [("ext", f"file://{bare}", "ext", "main", _remote_sha(bare))])

    assert sync(jobs=2, include_missing=True, depth=1, revs=[f"ext={feat}"]) == 0

    child = meta_repo / "ext"
    assert _head(child) == feat
    assert _branch(child) == "HEAD"
    shallow = subprocess.run(
        ["git", "-C", str(child), "rev-parse", "--is-shallow-repository"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert shallow == "true"


def test_override_with_depth_keeps_existing_full_clone_full(meta_repo, make_remote, advance_remote):
    """--depth is first-clone only: an override fetch on an existing full
    clone must not make it shallow."""
    bare = make_remote()
    base = _remote_sha(bare)
    advance_remote(bare, "more")
    tip = advance_remote(bare, "tip")
    _write_manifest(meta_repo, [("ext", f"file://{bare}", "ext", "main", tip)])
    child = meta_repo / "ext"
    assert sync(jobs=2, include_missing=True) == 0  # full clone
    assert not (child / ".git" / "shallow").exists()

    assert sync(jobs=2, depth=1, revs=[f"ext={base}"]) == 0
    assert _head(child) == base
    assert not (child / ".git" / "shallow").exists()


def test_override_branch_name_lands_on_origin_tip_not_stale_local(meta_repo, make_remote, advance_remote):
    bare = make_remote()
    old_main = _remote_sha(bare)
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    child = meta_repo / "ext"
    assert sync(jobs=2, include_missing=True) == 0
    new_tip = advance_remote(bare, "more")  # local main is now stale

    assert sync(jobs=2, revs=["ext=main"]) == 0

    assert _head(child) == new_tip
    assert _branch(child) == "HEAD"
    local_main = subprocess.run(
        ["git", "-C", str(child), "rev-parse", "refs/heads/main"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert local_main == old_main  # the local branch was not moved


def test_override_full_ref(meta_repo, make_remote, advance_remote):
    bare = make_remote()
    base = _remote_sha(bare)
    _write_manifest(meta_repo, [("ext", str(bare), "ext", base)])
    mr = _push_branch(bare, "mr-source", start=base)
    subprocess.run(["git", "update-ref", "refs/merge-requests/12/head", mr],
                   cwd=bare, check=True, capture_output=True)

    assert sync(jobs=2, include_missing=True, revs=["ext=refs/merge-requests/12/head"]) == 0

    assert _head(meta_repo / "ext") == mr
    assert _branch(meta_repo / "ext") == "HEAD"


def test_rt_revs_env_separators_and_cli_precedence(meta_repo, make_remote, advance_remote, monkeypatch):
    bare = make_remote()
    base = _remote_sha(bare)
    tip = advance_remote(bare, "more")
    other = _push_branch(bare, "other", start=base)
    _write_manifest(meta_repo, [("a", str(bare), "a", base), ("b", str(bare), "b", base)])
    assert sync(jobs=2, include_missing=True) == 0

    monkeypatch.setenv(REV_OVERRIDE_ENV, f"a={tip},\n  b={other}")
    assert sync(jobs=2) == 0
    assert _head(meta_repo / "a") == tip
    assert _head(meta_repo / "b") == other

    # The CLI overrides the environment for the same repo; the other entry
    # from RT_REVS still applies.
    assert sync(jobs=2, revs=[f"a={other}"]) == 0
    assert _head(meta_repo / "a") == other
    assert _head(meta_repo / "b") == other


def test_rt_revs_empty_means_no_overrides(meta_repo, make_remote, advance_remote, monkeypatch):
    bare = make_remote()
    base = _remote_sha(bare)
    advance_remote(bare, "more")
    _write_manifest(meta_repo, [("ext", str(bare), "ext", base)])
    monkeypatch.setenv(REV_OVERRIDE_ENV, "")
    assert sync(jobs=2, include_missing=True) == 0
    assert _head(meta_repo / "ext") == base


def test_override_line_printed_and_marks_detail(meta_repo, make_remote, advance_remote, capsys):
    bare = make_remote()
    base = _remote_sha(bare)
    tip = advance_remote(bare, "more")
    _write_manifest(meta_repo, [("ext", str(bare), "ext", base)])
    assert sync(jobs=2, include_missing=True) == 0
    capsys.readouterr()

    assert sync(jobs=2, revs=[f"ext={tip}"]) == 0
    out = capsys.readouterr().out
    assert f"overrides: ext={tip[:7]}… (--rev)" in out
    assert f"updated {base[:7]} → {tip[:7]} (override)" in out


@pytest.mark.parametrize("revs, match", [
    (["ext"], "malformed rev override 'ext' in --rev"),
    (["=abc1234def"], "malformed"),
    (["ext="], "malformed"),
    (["nope=main"], "unknown repo in --rev: 'nope'"),
    (["ext=main", "ext=v1.0"], "overridden more than once"),
    (["ext=abc1234"], "abbreviated SHA"),
    (["ext=-x"], "must not start with '-'"),
])
def test_override_validation_errors_before_any_git(meta_repo, make_remote, revs, match):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    with pytest.raises(RepoToolError, match=match):
        sync(jobs=2, include_missing=True, revs=revs)
    assert not (meta_repo / "ext").exists()


@pytest.mark.parametrize("env, match", [
    ("ext", "malformed rev override 'ext' in RT_REVS"),
    ("nope=main", "unknown repo in RT_REVS"),
    ("ext=main ext=v1.0", "overridden more than once"),
    ("ext=abc1234", "abbreviated SHA"),
])
def test_rt_revs_validation_errors(meta_repo, make_remote, monkeypatch, env, match):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    monkeypatch.setenv(REV_OVERRIDE_ENV, env)
    with pytest.raises(RepoToolError, match=match):
        sync(jobs=2, include_missing=True)
    assert not (meta_repo / "ext").exists()


def test_override_all_digit_tag_is_not_a_short_sha(meta_repo, make_remote, advance_remote):
    """An all-digit ref such as a date tag isn't rejected as an abbreviated
    SHA, only refs with a hex letter are."""
    bare = make_remote()
    base = _remote_sha(bare)
    advance_remote(bare, "more")
    subprocess.run(["git", "-C", str(bare), "tag", "20261102", base], check=True)
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])

    assert sync(jobs=2, include_missing=True, revs=["ext=20261102"]) == 0
    assert _head(meta_repo / "ext") == base


def test_override_nonexistent_ref_fails_with_summary(meta_repo, make_remote, capsys):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "main")])
    rc = sync(jobs=2, include_missing=True, revs=["ext=refs/nope/head"])
    assert rc != 0
    captured = capsys.readouterr()
    assert "sync failed for 1 of 1" in captured.err
    assert "fetch override refs/nope/head" in captured.out


def test_rt_revs_outside_sync_set_ignored_cli_errors(meta_repo, make_remote, advance_remote,
                                                     monkeypatch, capsys):
    bare = make_remote()
    base = _remote_sha(bare)
    tip = advance_remote(bare, "more")
    _write_manifest(meta_repo, [("a", str(bare), "a", base), ("b", str(bare), "b", base)])
    assert sync(jobs=2, include_missing=True, names=["a"]) == 0
    assert sync(jobs=2, include_missing=True, names=["b"]) == 0
    capsys.readouterr()

    # b is cloned, but this run only syncs a: RT_REVS's entry for b is ignored.
    monkeypatch.setenv(REV_OVERRIDE_ENV, f"b={tip}")
    assert sync(jobs=2, names=["a"]) == 0
    assert "overrides" not in capsys.readouterr().out
    assert _head(meta_repo / "b") == base

    # The same entry on the command line is a mistake and errors.
    monkeypatch.delenv(REV_OVERRIDE_ENV)
    with pytest.raises(RepoToolError, match="not in the sync set"):
        sync(jobs=2, names=["a"], revs=[f"b={tip}"])


def test_rev_for_uncloned_repo_errors_without_names(meta_repo, make_remote, advance_remote):
    bare = make_remote()
    base = _remote_sha(bare)
    tip = advance_remote(bare, "more")
    _write_manifest(meta_repo, [("a", str(bare), "a", base), ("b", str(bare), "b", base)])
    assert sync(jobs=2, include_missing=True, names=["a"]) == 0  # b stays uncloned

    with pytest.raises(RepoToolError, match="not in the sync set"):
        sync(jobs=2, revs=[f"b={tip}"])


def test_rt_revs_ignored_by_status_and_reset(meta_repo, make_remote, advance_remote,
                                             monkeypatch, capsys):
    bare = make_remote()
    base = _remote_sha(bare)
    tip = advance_remote(bare, "more")
    _write_manifest(meta_repo, [("ext", str(bare), "ext", base)])
    assert sync(jobs=2, include_missing=True) == 0
    capsys.readouterr()

    monkeypatch.setenv(REV_OVERRIDE_ENV, f"ext={tip}")
    assert status() == 0
    assert "override" not in capsys.readouterr().out
    assert _head(meta_repo / "ext") == base

    assert reset() == 0
    assert _head(meta_repo / "ext") == base
    assert _branch(meta_repo / "ext") == "main"

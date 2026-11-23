# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.

"""Tests for the bump command."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

import pytest

from repotool import RepoToolError, bump, sync


@pytest.fixture(autouse=True)
def chdir_to_meta(meta_repo, monkeypatch):
    monkeypatch.chdir(meta_repo)


def _write_manifest(meta_repo: Path, repos: list) -> None:
    """3-tuple (name, url, path) leaves the entry unpinned; 4-tuple adds a rev
    (branch defaults to "main"); 5-tuple is (name, url, path, branch, rev)."""
    parts: list[str] = []
    for entry in repos:
        branch, rev = "main", None
        if len(entry) == 3:
            name, url, path = entry
        elif len(entry) == 4:
            name, url, path, rev = entry
        else:
            name, url, path, branch, rev = entry
        block = (
            f'\n[[repo]]\nname = "{name}"\nurl = "{url}"\npath = "{path}"\n'
            f'branch = "{branch}"\n'
        )
        if rev is not None:
            block += f'rev = "{rev}"\n'
        parts.append(block)
    (meta_repo / ".repotool.toml").write_text("".join(parts))


def _manifest_rev(meta_repo: Path, name: str) -> str | None:
    """The `rev` value of the named [[repo]] block, or None when unpinned."""
    block = ""
    for chunk in (meta_repo / ".repotool.toml").read_text().split("[[repo]]"):
        if re.search(rf'name\s*=\s*"{re.escape(name)}"', chunk):
            block = chunk
    m = re.search(r'rev\s*=\s*"([^"]*)"', block)
    return m.group(1) if m else None


def _head(p: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=p, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _branch_of(p: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=p,
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _rev_parse(p: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=p, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _commit_in_child(child: Path, filename: str = "local.txt") -> str:
    (child / filename).write_text("local")
    subprocess.run(["git", "add", "."], cwd=child, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "local"], cwd=child, check=True,
                   capture_output=True)
    return _head(child)


def _force_push_unrelated(bare: Path, message: str = "rebased") -> str:
    """Force-push a commit onto the remote's main that is *not* a descendant of
    the current tip — the upstream-rebase case. Returns the new tip."""
    with tempfile.TemporaryDirectory() as td:
        work = Path(td) / "work"
        subprocess.run(["git", "clone", str(bare), str(work)], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(work), "checkout", "--orphan", "rebased"],
                       check=True, capture_output=True)
        (work / f"{message}.txt").write_text(message)
        subprocess.run(["git", "-C", str(work), "add", "."], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(work), "commit", "-m", message], check=True,
                       capture_output=True)
        sha = _head(work)
        subprocess.run(["git", "-C", str(work), "push", "--force", "origin",
                        "rebased:main"], check=True, capture_output=True)
    return sha


def _run_json(capsys, **kwargs) -> tuple[int, dict]:
    """Run bump in --json mode, returning (exit code, parsed document).

    Clears any prior captured output first: the document must be the *entire*
    stdout of the run, which is the contract a CI `jq` parse depends on.
    """
    capsys.readouterr()
    rc = bump(as_json=True, **kwargs)
    out = capsys.readouterr().out
    assert "\033[" not in out  # no ANSI escapes, ever
    return rc, json.loads(out)


def _repo_record(doc: dict, name: str) -> dict:
    return next(r for r in doc["repos"] if r["name"] == name)


# --- Core update flow -----------------------------------------------------

def test_bump_advances_pin_and_clone(meta_repo, make_remote, advance_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext")])
    assert sync(jobs=2, include_missing=True) == 0
    child = meta_repo / "ext"
    pinned = _head(child)
    _write_manifest(meta_repo, [("ext", str(bare), "ext", pinned)])

    tip = advance_remote(bare)
    assert bump(jobs=2) == 0
    assert _manifest_rev(meta_repo, "ext") == tip
    assert _head(child) == tip


def test_bump_up_to_date_leaves_manifest_byte_identical(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext")])
    assert sync(jobs=2, include_missing=True) == 0
    _write_manifest(meta_repo, [("ext", str(bare), "ext", _head(meta_repo / "ext"))])
    before = (meta_repo / ".repotool.toml").read_bytes()

    assert bump(jobs=2) == 0
    assert (meta_repo / ".repotool.toml").read_bytes() == before


def test_bump_bumps_stale_manifest_without_moving_clone(
    meta_repo, make_remote, advance_remote, capsys,
):
    """Clone already at the tip, manifest pinned behind it: the pin still needs
    the bump even though no merge is required."""
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext")])
    assert sync(jobs=2, include_missing=True) == 0
    old = _head(meta_repo / "ext")
    _write_manifest(meta_repo, [("ext", str(bare), "ext", old)])
    tip = advance_remote(bare)
    # Bring the clone (only) up to the new tip.
    subprocess.run(["git", "pull", "--ff-only"], cwd=meta_repo / "ext", check=True,
                   capture_output=True)
    assert _head(meta_repo / "ext") == tip

    assert bump(jobs=2) == 0
    assert _manifest_rev(meta_repo, "ext") == tip
    assert "1 commit(s)" in capsys.readouterr().out


def test_bump_advances_and_attaches_detached_clone(
    meta_repo, make_remote, advance_remote,
):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "v1.0")])
    assert sync(jobs=2, include_missing=True) == 0
    child = meta_repo / "ext"
    subprocess.run(["git", "checkout", "--detach"], cwd=child, check=True,
                   capture_output=True)
    tip = advance_remote(bare)

    assert bump(jobs=2) == 0
    assert _head(child) == tip
    assert _branch_of(child) == "main"
    assert _manifest_rev(meta_repo, "ext") == tip


def test_bump_pins_unpinned_entry(meta_repo, make_remote, advance_remote, capsys):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext")])  # no rev
    assert sync(jobs=2, include_missing=True) == 0
    tip = advance_remote(bare)

    assert bump(jobs=2) == 0
    assert _manifest_rev(meta_repo, "ext") == tip
    out = capsys.readouterr().out
    assert "unpinned" in out
    assert "no manifest rev pinned" in out


# --- Divergence and failures ---------------------------------------------

def test_bump_reports_diverged_after_upstream_rebase(
    meta_repo, make_remote, capsys,
):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext")])
    assert sync(jobs=2, include_missing=True) == 0
    child = meta_repo / "ext"
    pinned = _head(child)
    _write_manifest(meta_repo, [("ext", str(bare), "ext", pinned)])
    _force_push_unrelated(bare)

    assert bump(jobs=2) == 1
    assert _manifest_rev(meta_repo, "ext") == pinned
    assert _head(child) == pinned
    captured = capsys.readouterr()
    assert "upstream rebased" in captured.out
    assert "bump failed" in captured.err


def test_bump_fails_on_unreachable_remote(meta_repo, make_remote, capsys):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext")])
    assert sync(jobs=2, include_missing=True) == 0
    subprocess.run(["git", "remote", "set-url", "origin", str(meta_repo / "nope.git")],
                   cwd=meta_repo / "ext", check=True, capture_output=True)

    assert bump(jobs=2) == 1
    assert "bump failed" in capsys.readouterr().err


# --- Skips ---------------------------------------------------------------

@pytest.mark.parametrize("scenario", ["dirty", "ahead", "wrong-branch"])
def test_bump_skips_unsafe_clones(
    meta_repo, make_remote, advance_remote, capsys, scenario,
):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext")])
    assert sync(jobs=2, include_missing=True) == 0
    child = meta_repo / "ext"
    pinned = _head(child)
    _write_manifest(meta_repo, [("ext", str(bare), "ext", pinned)])
    if scenario == "dirty":
        (child / "f.txt").write_text("modified")
    elif scenario == "ahead":
        _commit_in_child(child)
    else:
        subprocess.run(["git", "checkout", "-b", "feature"], cwd=child, check=True,
                       capture_output=True)
    advance_remote(bare)

    # Bulk mode: a skip is informational.
    assert bump(jobs=2) == 0
    assert _manifest_rev(meta_repo, "ext") == pinned
    assert "skipped" not in capsys.readouterr().err

    # Named explicitly: the same skip is a failure of intent.
    assert bump(names=["ext"], jobs=2) == 1
    assert "bump failed" in capsys.readouterr().err


def test_bump_bumps_when_detached_at_tip_despite_local_branch(
    meta_repo, make_remote, advance_remote, capsys,
):
    """Detached HEAD sitting exactly at origin/<branch>, plus a local <branch>
    carrying commits of its own, and a manifest pin behind the tip.

    `fast-forward` skips this (it would have to rewrite the local branch label
    to attach), but bump needs no advance at all: the checkout is already
    where the pin should point. The guard must not block the manifest bump, and
    neither HEAD nor the local branch may move."""
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext")])
    assert sync(jobs=2, include_missing=True) == 0
    child = meta_repo / "ext"
    old = _head(child)
    _write_manifest(meta_repo, [("ext", str(bare), "ext", old)])

    # Local 'main' gets work of its own; HEAD then detaches at the remote tip.
    tip = advance_remote(bare)
    local_sha = _commit_in_child(child, filename="branch-local.txt")
    subprocess.run(["git", "fetch", "origin"], cwd=child, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "--detach", "origin/main"], cwd=child,
                   check=True, capture_output=True)
    assert _head(child) == tip

    assert bump(names=["ext"], jobs=2) == 0
    assert _manifest_rev(meta_repo, "ext") == tip
    # Neither HEAD nor the local branch moved.
    assert _head(child) == tip
    assert _branch_of(child) == "HEAD"
    assert _rev_parse(child, "refs/heads/main") == local_sha
    assert "not safely advancable" not in capsys.readouterr().out


def test_bump_skips_when_advance_would_rewrite_local_branch(
    meta_repo, make_remote, advance_remote, capsys,
):
    """Same shape, but HEAD is *behind* the tip: now reaching it means moving
    the local branch label, so the guard stands and the repo is skipped."""
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext", "v1.0")])
    assert sync(jobs=2, include_missing=True) == 0
    child = meta_repo / "ext"
    subprocess.run(["git", "checkout", "main"], cwd=child, check=True, capture_output=True)
    local_sha = _commit_in_child(child, filename="branch-local.txt")
    subprocess.run(["git", "checkout", "--detach", "v1.0"], cwd=child, check=True,
                   capture_output=True)
    head_before = _head(child)
    advance_remote(bare)

    assert bump(jobs=2) == 0  # bulk mode: a skip is informational
    assert _manifest_rev(meta_repo, "ext") == "v1.0"
    assert _head(child) == head_before
    assert _rev_parse(child, "refs/heads/main") == local_sha
    assert "not safely advancable" in capsys.readouterr().out


def test_bump_does_not_clone(meta_repo, make_remote, capsys):
    """An uncloned child is skipped, never cloned — that's `rt sync`'s job."""
    bare = make_remote()
    _write_manifest(meta_repo, [("a", str(bare), "a"), ("b", str(bare), "b")])
    assert sync(names=["a"], jobs=2, include_missing=True) == 0

    assert bump(jobs=2) == 0  # 'b' isn't an existing clone: not selected
    assert not (meta_repo / "b").exists()

    assert bump(names=["b"], jobs=2) == 1
    captured = capsys.readouterr()
    assert "not synced" in captured.out
    assert not (meta_repo / "b").exists()


def test_bump_errors_when_nothing_is_synced(meta_repo, make_remote, capsys):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext")])
    assert bump(jobs=2) == 1
    assert "no synced repos" in capsys.readouterr().err


def test_bump_unknown_name_errors(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext")])
    assert sync(jobs=2, include_missing=True) == 0
    with pytest.raises(RepoToolError, match="unknown repo"):
        bump(names=["nope"], jobs=2)


# --- Dry run -------------------------------------------------------------

def test_bump_dry_run_touches_nothing(
    meta_repo, make_remote, advance_remote, capsys,
):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext")])
    assert sync(jobs=2, include_missing=True) == 0
    child = meta_repo / "ext"
    pinned = _head(child)
    _write_manifest(meta_repo, [("ext", str(bare), "ext", pinned)])
    before = (meta_repo / ".repotool.toml").read_bytes()
    advance_remote(bare)

    assert bump(dry_run=True, jobs=2) == 0
    assert (meta_repo / ".repotool.toml").read_bytes() == before
    assert _head(child) == pinned
    out = capsys.readouterr().out
    assert "not applied" in out
    assert "manifest not modified (--dry-run)" in out


def test_bump_dry_run_json_reports_the_update(
    meta_repo, make_remote, advance_remote, capsys,
):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext")])
    assert sync(jobs=2, include_missing=True) == 0
    _write_manifest(meta_repo, [("ext", str(bare), "ext", _head(meta_repo / "ext"))])
    tip = advance_remote(bare)

    rc, doc = _run_json(capsys, dry_run=True, jobs=2)
    assert rc == 0
    assert doc["dry_run"] is True
    assert doc["manifest_updated"] == 0
    rec = _repo_record(doc, "ext")
    assert rec["status"] == "updated"
    assert rec["new"] == tip


# --- Shallow clones ------------------------------------------------------

def test_bump_shallow_clone_without_the_pin(
    meta_repo, make_remote, advance_remote, capsys,
):
    """A `--depth 1` clone whose pin sits below the graft point: the update
    still lands, with a note instead of a changelog when the pin can't be
    recovered from the remote."""
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext")])
    assert sync(jobs=2, include_missing=True, depth=1) == 0
    # Pin a rev the shallow clone has no object for.
    _write_manifest(
        meta_repo, [("ext", str(bare), "ext", "0" * 39 + "1")],
    )
    tip = advance_remote(bare)

    rc, doc = _run_json(capsys, jobs=2)
    assert rc == 0
    rec = _repo_record(doc, "ext")
    assert rec["status"] == "updated"
    assert rec["old_sha"] is None
    assert rec["count"] is None
    assert rec["log"] == []
    assert rec["note"] == "manifest rev not present locally; log unavailable"
    assert _manifest_rev(meta_repo, "ext") == tip


# --- Changelog -----------------------------------------------------------

def test_bump_log_limit_truncates(meta_repo, make_remote, advance_remote, capsys):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext")])
    assert sync(jobs=2, include_missing=True) == 0
    _write_manifest(meta_repo, [("ext", str(bare), "ext", _head(meta_repo / "ext"))])
    for i in range(4):
        advance_remote(bare, f"c{i}")

    rc, doc = _run_json(capsys, log_limit=2, jobs=2)
    assert rc == 0
    rec = _repo_record(doc, "ext")
    assert rec["count"] == 4
    assert len(rec["log"]) == 2
    assert rec["truncated"] == 2
    # Newest first, subjects carried through.
    assert [e["subject"] for e in rec["log"]] == ["c3", "c2"]
    assert all(len(e["sha"]) == 40 for e in rec["log"])


def test_bump_log_limit_zero_is_unlimited(
    meta_repo, make_remote, advance_remote, capsys,
):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext")])
    assert sync(jobs=2, include_missing=True) == 0
    _write_manifest(meta_repo, [("ext", str(bare), "ext", _head(meta_repo / "ext"))])
    for i in range(3):
        advance_remote(bare, f"c{i}")

    rc, doc = _run_json(capsys, log_limit=0, jobs=2)
    assert rc == 0
    rec = _repo_record(doc, "ext")
    assert len(rec["log"]) == 3
    assert rec["truncated"] == 0


def test_bump_negative_log_limit_errors(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext")])
    assert sync(jobs=2, include_missing=True) == 0
    with pytest.raises(RepoToolError, match="--log-limit"):
        bump(log_limit=-1, jobs=2)


# --- JSON contract -------------------------------------------------------

_REPO_KEYS = {
    "name", "path", "branch", "status", "old", "old_sha", "new", "count",
    "detail", "note", "truncated", "log",
}


def test_bump_json_contract(meta_repo, make_remote, advance_remote, capsys):
    a = make_remote("a")
    b = make_remote("b")
    _write_manifest(meta_repo, [("a", str(a), "a"), ("b", str(b), "b")])
    assert sync(jobs=2, include_missing=True) == 0
    b_pin = _head(meta_repo / "b")
    _write_manifest(
        meta_repo,
        [("a", str(a), "a", _head(meta_repo / "a")), ("b", str(b), "b", b_pin)],
    )
    tip_a = advance_remote(a)
    (meta_repo / "b" / "f.txt").write_text("dirty")

    rc, doc = _run_json(capsys, jobs=2)
    assert rc == 0

    assert set(doc) == {"dry_run", "manifest_updated", "repos"}
    assert doc["dry_run"] is False
    assert doc["manifest_updated"] == 1
    # Manifest order, not completion order.
    assert [r["name"] for r in doc["repos"]] == ["a", "b"]

    for rec in doc["repos"]:
        assert set(rec) == _REPO_KEYS

    rec_a = _repo_record(doc, "a")
    assert rec_a["status"] == "updated"
    assert rec_a["path"] == "a"
    assert rec_a["branch"] == "main"
    assert rec_a["new"] == tip_a
    assert rec_a["old"] == rec_a["old_sha"] != tip_a
    assert rec_a["count"] == 1
    assert rec_a["detail"] is None
    assert rec_a["note"] is None

    rec_b = _repo_record(doc, "b")
    assert rec_b["status"] == "skipped"
    assert rec_b["detail"] == "dirty working tree"
    assert rec_b["new"] is None
    assert rec_b["count"] is None
    assert rec_b["log"] == []


def test_bump_json_up_to_date_record(meta_repo, make_remote, capsys):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext")])
    assert sync(jobs=2, include_missing=True) == 0
    pinned = _head(meta_repo / "ext")
    _write_manifest(meta_repo, [("ext", str(bare), "ext", pinned)])

    rc, doc = _run_json(capsys, jobs=2)
    assert rc == 0
    rec = _repo_record(doc, "ext")
    assert rec["status"] == "up-to-date"
    assert rec["old"] == rec["new"] == pinned
    assert rec["count"] == 0
    assert rec["log"] == []


def test_bump_json_with_verbose_is_a_usage_error(meta_repo, make_remote):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext")])
    assert sync(jobs=2, include_missing=True) == 0
    with pytest.raises(RepoToolError, match="mutually exclusive"):
        bump(as_json=True, verbose=True, jobs=2)


def test_bump_json_diverged_record(meta_repo, make_remote, capsys):
    bare = make_remote()
    _write_manifest(meta_repo, [("ext", str(bare), "ext")])
    assert sync(jobs=2, include_missing=True) == 0
    pinned = _head(meta_repo / "ext")
    _write_manifest(meta_repo, [("ext", str(bare), "ext", pinned)])
    tip = _force_push_unrelated(bare)

    rc, doc = _run_json(capsys, jobs=2)
    assert rc == 1
    rec = _repo_record(doc, "ext")
    assert rec["status"] == "diverged"
    assert rec["old_sha"] == pinned
    assert rec["new"] == tip
    assert "rebased" in rec["detail"]
    assert doc["manifest_updated"] == 0

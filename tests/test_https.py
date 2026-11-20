# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.

"""Tests for --https URL rewriting."""

from __future__ import annotations

from repotool import _to_https, sync


def test_scp_style_with_user():
    assert _to_https("git@github.com:org/repo.git") == "https://github.com/org/repo.git"


def test_scp_style_with_subdomain():
    assert (_to_https("git@git.corp.example.com:group/linux.git")
            == "https://git.corp.example.com/group/linux.git")


def test_scp_style_without_user():
    assert _to_https("host.example:org/repo.git") == "https://host.example/org/repo.git"


def test_ssh_scheme_with_user():
    assert _to_https("ssh://git@host.example/org/repo.git") == "https://host.example/org/repo.git"


def test_ssh_scheme_without_user():
    assert _to_https("ssh://host.example/org/repo.git") == "https://host.example/org/repo.git"


def test_git_scheme():
    assert _to_https("git://host.example/org/repo.git") == "https://host.example/org/repo.git"


def test_http_upgraded():
    assert _to_https("http://host.example/org/repo.git") == "https://host.example/org/repo.git"


def test_https_unchanged():
    assert _to_https("https://host.example/org/repo.git") == "https://host.example/org/repo.git"


def test_file_scheme_left_alone():
    """`file://` isn't a remote scheme to be rewritten — leave it for tests / local mirrors."""
    assert _to_https("file:///tmp/foo.git") == "file:///tmp/foo.git"


def test_bare_path_left_alone():
    assert _to_https("/var/cache/repo.git") == "/var/cache/repo.git"


def test_sync_https_rewrites_clone_url(meta_repo, monkeypatch):
    """End-to-end: with --https, git clone is invoked with the rewritten URL.

    We capture the git argv by stubbing `subprocess.run` so the test doesn't
    depend on DNS / a network. The clone will exit nonzero (no real git
    invocation happens), but that's fine — we only assert on what was
    attempted, not whether it succeeded.
    """
    import subprocess

    monkeypatch.chdir(meta_repo)
    (meta_repo / ".repotool.toml").write_text(
        '[[repo]]\nname = "ext"\nurl = "git@host.example:org/repo.git"\n'
        'path = "ext"\nbranch = "main"\nrev = "main"\n'
    )

    captured: list[list[str]] = []
    real_run = subprocess.run

    def fake_run(args, **kwargs):
        if isinstance(args, list) and args and args[0] == "git" and "clone" in args:
            captured.append(args)
            return subprocess.CompletedProcess(args, 128, stdout="", stderr="fatal: stub\n")
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    rc = sync(jobs=1, include_missing=True, https=True)
    assert rc != 0  # clone was stubbed to fail
    assert captured, "git clone was not invoked"
    cmd = captured[0]
    assert "https://host.example/org/repo.git" in cmd
    assert not any("git@host.example" in arg for arg in cmd)


def test_sync_without_https_uses_original_url(meta_repo, monkeypatch):
    """Without --https, the manifest URL is passed through unchanged."""
    import subprocess

    monkeypatch.chdir(meta_repo)
    (meta_repo / ".repotool.toml").write_text(
        '[[repo]]\nname = "ext"\nurl = "git@host.example:org/repo.git"\n'
        'path = "ext"\nbranch = "main"\nrev = "main"\n'
    )

    captured: list[list[str]] = []
    real_run = subprocess.run

    def fake_run(args, **kwargs):
        if isinstance(args, list) and args and args[0] == "git" and "clone" in args:
            captured.append(args)
            return subprocess.CompletedProcess(args, 128, stdout="", stderr="fatal: stub\n")
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    sync(jobs=1, include_missing=True)
    assert any("git@host.example:org/repo.git" in arg for arg in captured[0])

# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.

"""Tests for GitError.error_line — the heuristic that picks the most
informative line of git stderr to surface in failure messages."""

from __future__ import annotations

from repotool import GitError


def _err(stderr: str) -> str:
    return GitError(["clone", "x"], 128, stdout="", stderr=stderr).error_line


def test_empty_stderr():
    assert _err("") == ""


def test_prefers_fatal_over_progress():
    """The regression that prompted this: git's progress 'Cloning into ...'
    is the first stderr line on auth failure, but the real signal is
    further down. error_line must pick the `fatal:` line."""
    stderr = (
        "Cloning into '/builds/meta/busybox'...\n"
        "\n"
        "git@server.com: Permission denied (publickey).\n"
        "fatal: Could not read from remote repository.\n"
        "\n"
        "Please make sure you have the correct access rights\n"
        "and the repository exists.\n"
    )
    assert _err(stderr) == "fatal: Could not read from remote repository."


def test_prefers_error_prefix():
    stderr = "Cloning into 'foo'...\nerror: pathspec 'nope' did not match\n"
    assert _err(stderr) == "error: pathspec 'nope' did not match"


def test_falls_back_to_permission_denied():
    """No fatal/error line — surface the SSH auth message directly."""
    stderr = (
        "Cloning into 'foo'...\n"
        "git@server.com: Permission denied (publickey).\n"
    )
    assert _err(stderr) == "git@server.com: Permission denied (publickey)."


def test_falls_back_to_ssh_prefix():
    stderr = (
        "Cloning into 'foo'...\n"
        "ssh: Could not resolve hostname bogus: Name or service not known\n"
    )
    assert "Could not resolve hostname" in _err(stderr)


def test_skips_progress_and_remote_lines():
    """Without fatal/ssh markers, pick the last meaningful line — skipping
    git progress and `remote:`-prefixed server chatter."""
    stderr = (
        "Cloning into 'foo'...\n"
        "remote: Counting objects: 100% (5/5)\n"
        "the real problem\n"
        "remote: done.\n"
    )
    assert _err(stderr) == "the real problem"


def test_single_progress_line_falls_back_to_it():
    """If literally nothing else is available, the progress line is better than nothing."""
    stderr = "Cloning into 'foo'...\n"
    assert _err(stderr) == "Cloning into 'foo'..."

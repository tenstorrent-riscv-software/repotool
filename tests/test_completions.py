# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.

"""Tests for shell completion (`rt completions` and the hidden `rt __complete`)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from repotool import _build_parser, _subparsers_action, main


def _complete_words(capsys, words: list[str]) -> list[str]:
    """Run `rt __complete -- rt <words...>` and return the output lines."""
    assert main(["__complete", "--", "rt", *words]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    return captured.out.splitlines()


def _values(lines: list[str]) -> set[str]:
    """Candidate values with any tab-separated help text stripped."""
    return {line.split("\t")[0] for line in lines}


def _write_manifest(meta_repo: Path, names: list[str]) -> None:
    parts = [
        f'[[repo]]\nname = "{n}"\nurl = "u"\npath = "{n}"\nbranch = "main"\n'
        for n in names
    ]
    (meta_repo / ".repotool.toml").write_text("\n".join(parts))


def test_all_subcommands_are_completed(capsys):
    """Guard: every subparser (and alias) must appear in completion output,
    so a newly added subcommand can never silently be missing."""
    expected = set(_subparsers_action(_build_parser()).choices)
    assert _values(_complete_words(capsys, [""])) == expected


def test_subcommand_prefix_filter(capsys):
    values = _values(_complete_words(capsys, ["s"]))
    assert {"sync", "status", "show-changes", "snapshot"} <= values
    assert "reset" not in values


def test_subcommands_have_descriptions(capsys):
    by_value = {
        line.split("\t")[0]: line for line in _complete_words(capsys, [""])
    }
    assert "\t" in by_value["sync"]
    # The ffwd alias shares fast-forward's help text.
    assert by_value["ffwd"].split("\t")[1] == by_value["fast-forward"].split("\t")[1]


def test_top_level_options(capsys):
    assert _values(_complete_words(capsys, ["-"])) == {"-h", "--help", "--version"}


def test_subcommand_options(capsys):
    values = _values(_complete_words(capsys, ["sync", "-"]))
    assert {"--all", "--jobs", "--verbose", "--reference", "--depth", "--https"} <= values
    assert _values(_complete_words(capsys, ["reset", "--f"])) == {"--force"}


def test_option_value_contexts(capsys):
    # --jobs takes an int: nothing sensible to offer.
    assert _complete_words(capsys, ["sync", "--jobs", ""]) == []
    # --reference takes a PATH: defer to the shell's filename completion.
    assert _complete_words(capsys, ["sync", "--reference", ""]) == [":file"]
    # A store_true flag does not swallow the next word.
    values = _values(_complete_words(capsys, ["sync", "--https", "-"]))
    assert "--jobs" in values


def test_repo_names_from_manifest(meta_repo, monkeypatch, capsys):
    monkeypatch.chdir(meta_repo)
    _write_manifest(meta_repo, ["alpha", "beta", "gamma"])
    assert _values(_complete_words(capsys, ["sync", ""])) == {"alpha", "beta", "gamma"}
    # Already-typed names are excluded; option values are not mistaken for names.
    lines = _complete_words(capsys, ["sync", "--jobs", "4", "beta", ""])
    assert _values(lines) == {"alpha", "gamma"}


def test_repo_paths_from_manifest(meta_repo, monkeypatch, capsys):
    monkeypatch.chdir(meta_repo)
    (meta_repo / ".repotool.toml").write_text(
        '[[repo]]\nname = "dtc"\nurl = "u"\npath = "src/dtc"\nbranch = "main"\n'
        '\n[[repo]]\nname = "ui"\nurl = "u"\npath = "ui"\nbranch = "main"\n'
    )
    # A path differing from the name is offered as its own candidate.
    assert _values(_complete_words(capsys, ["sync", ""])) == {"dtc", "src/dtc", "ui"}
    # A repo referenced by path is excluded by name too, and vice versa.
    assert _values(_complete_words(capsys, ["sync", "src/dtc", ""])) == {"ui"}
    assert _values(_complete_words(capsys, ["sync", "dtc", ""])) == {"ui"}


def test_repo_names_never_crash(tmp_path, meta_repo, monkeypatch, capsys):
    # No manifest in the meta repo.
    monkeypatch.chdir(meta_repo)
    assert _complete_words(capsys, ["status", ""]) == []
    # Malformed manifest.
    (meta_repo / ".repotool.toml").write_text("[[repo]\nnot toml")
    assert _complete_words(capsys, ["status", ""]) == []
    # Not a git repository at all.
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    assert _complete_words(capsys, ["status", ""]) == []


def test_help_does_not_mention_complete(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    assert "__complete" not in capsys.readouterr().out


def test_choices_positional(capsys):
    assert _values(_complete_words(capsys, ["completions", ""])) == {
        "bash", "zsh", "fish",
    }
    # Already supplied: nothing more to offer.
    assert _complete_words(capsys, ["completions", "bash", ""]) == []


@pytest.mark.parametrize(
    ("shell", "registration"),
    [
        ("bash", "complete -F _rt_complete rt"),
        ("zsh", "compdef _rt rt"),
        ("fish", "complete -c rt -f -a '(__rt_complete)'"),
    ],
)
def test_completions_emits_script(capsys, shell, registration):
    assert main(["completions", shell]) == 0
    out = capsys.readouterr().out
    assert registration in out
    assert "rt __complete -- " in out


def test_completions_help_shows_usage(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["completions", "--help"])
    assert exc.value.code == 0
    assert 'eval "$(rt completions bash)"' in capsys.readouterr().out


def test_completions_rejects_unknown_shell(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["completions", "tcsh"])
    assert exc.value.code == 2


@pytest.mark.parametrize(
    ("shell", "check_args"),
    [
        ("bash", ["-n"]),
        ("zsh", ["-n"]),
        ("fish", ["--no-execute"]),
    ],
)
def test_script_syntax(tmp_path, capsys, shell, check_args):
    if shutil.which(shell) is None:
        pytest.skip(f"{shell} not installed")
    assert main(["completions", shell]) == 0
    script = tmp_path / f"completion.{shell}"
    script.write_text(capsys.readouterr().out)
    res = subprocess.run(
        [shell, *check_args, str(script)], capture_output=True, text=True, check=False,
    )
    assert res.returncode == 0, res.stderr

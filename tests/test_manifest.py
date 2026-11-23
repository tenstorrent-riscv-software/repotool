# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.

"""Tests for manifest loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from repotool import (
    ManifestError,
    _apply_manifest_updates,
    load_ignores,
    load_manifest,
)


def _write(tmp_path: Path, content: str) -> Path:
    (tmp_path / ".repotool.toml").write_text(content)
    return tmp_path


def test_minimal_manifest(tmp_path):
    _write(tmp_path, """
[[repo]]
name = "api"
url = "git@example.com:org/api.git"
path = "services/api"
branch = "main"
rev = "main"
""")
    m = load_manifest(tmp_path)
    assert len(m) == 1
    repo = m[0]
    assert repo.name == "api"
    assert repo.url == "git@example.com:org/api.git"
    assert repo.path == "services/api"
    assert repo.branch == "main"
    assert repo.rev == "main"


def test_rev_is_optional_and_defaults_to_empty(tmp_path):
    """A `[[repo]]` may omit `rev` — that's the unpinned mode."""
    _write(tmp_path, """
[[repo]]
name = "api"
url = "x"
path = "api"
branch = "main"
""")
    m = load_manifest(tmp_path)
    assert m[0].rev == ""
    assert m[0].branch == "main"


def test_multiple_repos_preserve_order(tmp_path):
    _write(tmp_path, """
[[repo]]
name = "a"
url = "x"
path = "a"
branch = "main"
rev = "main"

[[repo]]
name = "b"
url = "y"
path = "b"
branch = "main"
rev = "v1.0"
""")
    m = load_manifest(tmp_path)
    assert [r.name for r in m] == ["a", "b"]


def test_missing_file(tmp_path):
    with pytest.raises(ManifestError, match="not found"):
        load_manifest(tmp_path)


def test_malformed_toml(tmp_path):
    _write(tmp_path, "this is not [[ valid")
    with pytest.raises(ManifestError, match="Failed to parse"):
        load_manifest(tmp_path)


def test_no_repos(tmp_path):
    _write(tmp_path, "# empty manifest\n")
    with pytest.raises(ManifestError, match="at least one"):
        load_manifest(tmp_path)


def test_missing_required_field(tmp_path):
    _write(tmp_path, """
[[repo]]
name = "a"
url = "x"
path = "p"
""")
    with pytest.raises(ManifestError, match="missing required"):
        load_manifest(tmp_path)


def test_name_optional_defaults_to_path(tmp_path):
    _write(tmp_path, """
[[repo]]
url = "git@example.com:org/api.git"
path = "services/api"
branch = "main"
rev = "main"
""")
    m = load_manifest(tmp_path)
    assert m[0].name == "services/api"
    assert m[0].path == "services/api"


def test_name_optional_when_missing_other_fields_uses_path_in_error(tmp_path):
    _write(tmp_path, """
[[repo]]
path = "services/api"
""")
    with pytest.raises(ManifestError, match="'services/api'"):
        load_manifest(tmp_path)


def test_absolute_path_rejected(tmp_path):
    _write(tmp_path, """
[[repo]]
name = "a"
url = "x"
path = "/abs"
branch = "main"
rev = "main"
""")
    with pytest.raises(ManifestError, match="must be relative"):
        load_manifest(tmp_path)


def test_parent_traversal_rejected(tmp_path):
    _write(tmp_path, """
[[repo]]
name = "a"
url = "x"
path = "../escape"
branch = "main"
rev = "main"
""")
    with pytest.raises(ManifestError, match=r"\.\."):
        load_manifest(tmp_path)


def test_dot_path_rejected(tmp_path):
    _write(tmp_path, """
[[repo]]
name = "a"
url = "x"
path = "."
branch = "main"
rev = "main"
""")
    with pytest.raises(ManifestError, match="strict subdirectory"):
        load_manifest(tmp_path)


def test_empty_path_rejected(tmp_path):
    _write(tmp_path, """
[[repo]]
name = "a"
url = "x"
path = ""
branch = "main"
rev = "main"
""")
    with pytest.raises(ManifestError, match="strict subdirectory"):
        load_manifest(tmp_path)


def test_duplicate_paths_rejected(tmp_path):
    _write(tmp_path, """
[[repo]]
name = "a"
url = "x"
path = "p"
branch = "main"
rev = "main"

[[repo]]
name = "b"
url = "y"
path = "p"
branch = "main"
rev = "main"
""")
    with pytest.raises(ManifestError, match="duplicates"):
        load_manifest(tmp_path)


def test_ancestor_overlap_rejected(tmp_path):
    _write(tmp_path, """
[[repo]]
name = "parent"
url = "x"
path = "api"
branch = "main"
rev = "main"

[[repo]]
name = "child"
url = "y"
path = "api/sub"
branch = "main"
rev = "main"
""")
    with pytest.raises(ManifestError, match="ancestor"):
        load_manifest(tmp_path)


def test_paths_normalized(tmp_path):
    _write(tmp_path, """
[[repo]]
name = "a"
url = "x"
path = "foo//bar"
branch = "main"
rev = "main"
""")
    m = load_manifest(tmp_path)
    assert m[0].path == "foo/bar"


def test_reference_field_loaded(tmp_path):
    _write(tmp_path, """
[[repo]]
name = "a"
url = "x"
path = "a"
branch = "main"
rev = "main"

[[repo]]
name = "b"
url = "y"
path = "b"
branch = "main"
rev = "main"
reference = "a"
""")
    m = load_manifest(tmp_path)
    assert m[0].reference == ""
    assert m[1].reference == "a"


def test_reference_unknown_target_rejected(tmp_path):
    _write(tmp_path, """
[[repo]]
name = "b"
url = "y"
path = "b"
branch = "main"
rev = "main"
reference = "nope"
""")
    with pytest.raises(ManifestError, match="reference = 'nope'"):
        load_manifest(tmp_path)


def test_reference_self_rejected(tmp_path):
    _write(tmp_path, """
[[repo]]
name = "a"
url = "x"
path = "a"
branch = "main"
rev = "main"
reference = "a"
""")
    with pytest.raises(ManifestError, match="points to itself"):
        load_manifest(tmp_path)


def test_reference_cycle_rejected(tmp_path):
    _write(tmp_path, """
[[repo]]
name = "a"
url = "x"
path = "a"
branch = "main"
rev = "main"
reference = "b"

[[repo]]
name = "b"
url = "y"
path = "b"
branch = "main"
rev = "main"
reference = "a"
""")
    with pytest.raises(ManifestError, match="reference cycle"):
        load_manifest(tmp_path)


def test_reference_non_string_rejected(tmp_path):
    _write(tmp_path, """
[[repo]]
name = "a"
url = "x"
path = "a"
branch = "main"
rev = "main"
reference = 5
""")
    with pytest.raises(ManifestError, match="must be a string"):
        load_manifest(tmp_path)


def test_non_string_path_rejected(tmp_path):
    _write(tmp_path, """
[[repo]]
name = "a"
url = "x"
path = 123
branch = "main"
rev = "main"
""")
    with pytest.raises(ManifestError, match="must be a string"):
        load_manifest(tmp_path)


# A minimal repo block. `ignore` is a top-level key, so in TOML it must precede
# any [[repo]] table header — prepend it to this block.
_REPO = """
[[repo]]
name = "a"
url = "x"
path = "a"
branch = "main"
rev = "main"
"""


def test_ignore_defaults_to_empty(tmp_path):
    """No `ignore` key is valid; load_ignores returns an empty set."""
    _write(tmp_path, _REPO)
    assert load_manifest(tmp_path)  # does not raise
    assert load_ignores(tmp_path) == set()


def test_ignore_loaded_and_normalized(tmp_path):
    _write(tmp_path, 'ignore = ["west/repos", "./tools/", "vendor"]\n' + _REPO)
    # load_manifest validates without raising; load_ignores normalizes paths.
    load_manifest(tmp_path)
    assert load_ignores(tmp_path) == {"west/repos", "tools", "vendor"}


def test_ignore_must_be_array(tmp_path):
    """`check` runs load_manifest, so a bad `ignore` must fail there, not just
    lazily during the orphan walk."""
    _write(tmp_path, 'ignore = "west"\n' + _REPO)
    with pytest.raises(ManifestError, match="`ignore` must be an array"):
        load_manifest(tmp_path)


def test_ignore_entries_must_be_strings(tmp_path):
    _write(tmp_path, "ignore = [123]\n" + _REPO)
    with pytest.raises(ManifestError, match="entries must be strings"):
        load_manifest(tmp_path)


def test_ignore_rejects_absolute_path(tmp_path):
    _write(tmp_path, 'ignore = ["/etc"]\n' + _REPO)
    with pytest.raises(ManifestError, match="must be relative"):
        load_manifest(tmp_path)


def test_ignore_rejects_parent_traversal(tmp_path):
    _write(tmp_path, 'ignore = ["../escape"]\n' + _REPO)
    with pytest.raises(ManifestError, match="must not contain"):
        load_manifest(tmp_path)


def test_ignore_rejects_root(tmp_path):
    _write(tmp_path, 'ignore = ["."]\n' + _REPO)
    with pytest.raises(ManifestError, match="strict subdirectory"):
        load_manifest(tmp_path)


# --- Multiple manifest files under .repotool/ ------------------------------

_REPO_A = """
[[repo]]
name = "a"
url = "x"
path = "a"
branch = "main"
rev = "main"
"""

_REPO_B = """
[[repo]]
name = "b"
url = "y"
path = "b"
branch = "main"
rev = "main"
"""


def _write_dir(tmp_path: Path, files: dict[str, str]) -> Path:
    d = tmp_path / ".repotool"
    d.mkdir(exist_ok=True)
    for name, content in files.items():
        (d / name).write_text(content)
    return tmp_path


def test_manifest_dir_alone_is_enough(tmp_path):
    _write_dir(tmp_path, {"only.toml": _REPO_A})
    assert [r.name for r in load_manifest(tmp_path)] == ["a"]


def test_manifest_dir_files_load_in_name_order(tmp_path):
    _write_dir(tmp_path, {"b.toml": _REPO_B, "a.toml": _REPO_A})
    m = load_manifest(tmp_path)
    assert [r.name for r in m] == ["a", "b"]
    assert [r.source for r in m] == [".repotool/a.toml", ".repotool/b.toml"]


def test_single_file_and_dir_are_both_loaded(tmp_path):
    _write(tmp_path, _REPO_A)
    _write_dir(tmp_path, {"b.toml": _REPO_B})
    m = load_manifest(tmp_path)
    assert [r.name for r in m] == ["a", "b"]
    assert [r.source for r in m] == [".repotool.toml", ".repotool/b.toml"]


def test_non_toml_files_in_dir_are_ignored(tmp_path):
    _write_dir(tmp_path, {"a.toml": _REPO_A, "notes.txt": "junk", "b.toml.bak": "x"})
    assert [r.name for r in load_manifest(tmp_path)] == ["a"]


def test_empty_manifest_dir_is_not_a_manifest(tmp_path):
    (tmp_path / ".repotool").mkdir()
    with pytest.raises(ManifestError, match="not found"):
        load_manifest(tmp_path)


def test_file_without_repo_is_rejected(tmp_path):
    _write_dir(tmp_path, {"a.toml": _REPO_A, "empty.toml": 'ignore = ["x"]\n'})
    with pytest.raises(ManifestError, match="empty.toml.*must declare at least one"):
        load_manifest(tmp_path)


def test_duplicate_name_across_files_is_rejected(tmp_path):
    _write_dir(tmp_path, {"a.toml": _REPO_A, "b.toml": _REPO_A.replace('path = "a"', 'path = "c"')})
    with pytest.raises(ManifestError, match="duplicates the name"):
        load_manifest(tmp_path)


def test_duplicate_path_across_files_is_rejected(tmp_path):
    _write_dir(tmp_path, {"a.toml": _REPO_A, "b.toml": _REPO_B.replace('path = "b"', 'path = "a"')})
    with pytest.raises(ManifestError, match="duplicates path"):
        load_manifest(tmp_path)


def test_ancestor_path_across_files_is_rejected(tmp_path):
    _write_dir(tmp_path, {"a.toml": _REPO_A, "b.toml": _REPO_B.replace('path = "b"', 'path = "a/sub"')})
    with pytest.raises(ManifestError, match="ancestor"):
        load_manifest(tmp_path)


def test_reference_resolves_across_files(tmp_path):
    _write_dir(tmp_path, {
        "a.toml": _REPO_A,
        "b.toml": _REPO_B.rstrip() + '\nreference = "a"\n',
    })
    m = load_manifest(tmp_path)
    assert m[1].reference == "a"


def test_ignore_is_unioned_across_files(tmp_path):
    _write_dir(tmp_path, {
        "a.toml": 'ignore = ["west"]\n' + _REPO_A,
        "b.toml": 'ignore = ["vendor"]\n' + _REPO_B,
    })
    load_manifest(tmp_path)
    assert load_ignores(tmp_path) == {"west", "vendor"}


def test_apply_updates_routes_each_repo_to_its_own_file(tmp_path):
    """snapshot's rewrite must hit the right file and the right block within
    it, not index into one concatenated list."""
    _write(tmp_path, _REPO_A.replace('rev = "main"', 'rev = "old-a"'))
    _write_dir(tmp_path, {
        "b.toml": _REPO_B.replace('rev = "main"', 'rev = "old-b1"') + _REPO_B.replace(
            'name = "b"', 'name = "c"').replace('path = "b"', 'path = "c"').replace(
            'rev = "main"', 'rev = "old-c"'),
    })
    all_repos = load_manifest(tmp_path)
    changed = _apply_manifest_updates(
        tmp_path, all_repos,
        {"b": {"rev": "new-b"}, "c": {"rev": "new-c"}},
    )
    assert changed == 2
    assert 'rev = "old-a"' in (tmp_path / ".repotool.toml").read_text()
    b_text = (tmp_path / ".repotool" / "b.toml").read_text()
    assert 'rev = "new-b"' in b_text
    assert 'rev = "old-b1"' not in b_text
    assert 'rev = "new-c"' in b_text
    assert 'rev = "old-c"' not in b_text

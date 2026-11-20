#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.

"""repotool — lightweight meta-repo coordinator with TOML manifests.

TOML manifests committed to a parent repo declare a set of child Git
repos, each with its own placement path, tracked branch, and pinned
revision. The manifests are `.repotool.toml` and/or `*.toml` files in a
`.repotool/` directory. repotool clones/aligns those children and
provides visibility + reset commands. The CLI is `rt`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import tomllib
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from dataclasses import replace as _dc_replace
from pathlib import Path, PurePosixPath

__version__ = "0.1.0"


# --- Color rendering ------------------------------------------------------
#
# Output strings interpolate `COLORS['<name>']` and `RESET` directly. When
# stdout isn't a TTY (or `NO_COLOR` is set), every value is the empty
# string, so the markup vanishes without any per-string processing.

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")
_ANSI_COLORS: dict[str, str] = {
    "red":    "\033[31m",
    "green":  "\033[32m",
    "yellow": "\033[33m",
    "blue":   "\033[34m",
    "cyan":   "\033[36m",
    "bold":   "\033[1m",
    "dim":    "\033[2m",
}
_ANSI_RESET = "\033[0m"

_use_color = not os.environ.get("NO_COLOR") and sys.stdout.isatty()

COLORS: dict[str, str] = _ANSI_COLORS if _use_color else {k: "" for k in _ANSI_COLORS}
RESET: str = _ANSI_RESET if _use_color else ""


def _visible_len(s: str) -> int:
    """Length without ANSI escape codes."""
    return len(_ANSI_RE.sub("", s)) if _use_color else len(s)


# --- Exceptions -----------------------------------------------------------

class RepoToolError(Exception):
    """Base for repotool-specific errors."""


class ManifestError(RepoToolError):
    """The manifest is missing, malformed, or fails validation."""


class GitError(RepoToolError):
    """A git subprocess exited non-zero."""

    def __init__(
        self,
        args: list[str],
        returncode: int,
        stdout: str | None,
        stderr: str | None,
    ) -> None:
        # NB: avoid `self.args` — that's a reserved tuple slot on `Exception`.
        self.cmd = args
        self.returncode = returncode
        self.stdout = stdout or ""
        self.stderr = stderr or ""
        first = self.error_line or "(no stderr)"
        super().__init__(f"git {' '.join(args)} exited {returncode}: {first}")

    @property
    def error_line(self) -> str:
        """The most informative single line of stderr.

        git prefixes its own error with `fatal:` / `error:`. SSH-auth failures
        report on a line containing `Permission denied`. Picking the first
        non-empty line (as an earlier version did) tended to surface git's
        progress chatter ("Cloning into '...'"), which masked real failures.
        """
        lines = [s.strip() for s in self.stderr.splitlines() if s.strip()]
        if not lines:
            return ""
        for line in lines:
            if line.startswith(("fatal:", "error:")):
                return line
        for line in lines:
            if "Permission denied" in line or line.startswith("ssh:"):
                return line
        # Filter out git's progress chatter; take the last meaningful line.
        non_progress = [
            line for line in lines
            if not line.startswith(("Cloning into", "remote:"))
        ]
        return non_progress[-1] if non_progress else lines[-1]


# --- Output ---------------------------------------------------------------

_print_lock = threading.Lock()


def emit(line: str, *, err: bool = False) -> None:
    stream = sys.stderr if err else sys.stdout
    with _print_lock:
        print(line, file=stream, flush=True)


def emit_warn(message: str) -> None:
    emit(f"{COLORS['yellow']}warning:{RESET} {message}", err=True)


def emit_error(message: str) -> None:
    emit(f"{COLORS['red']}error:{RESET} {message}", err=True)


def _render_table(
    headers: list[str],
    rows: Sequence[Sequence[str]],
    title: str | None = None,
) -> None:
    """Fixed-width ASCII table. Headers may span multiple lines via `\\n`
    (bottom-aligned). Column widths are computed from visible text (ANSI
    escapes are ignored for width)."""
    header_lines = [h.split("\n") for h in headers]
    max_header_lines = max(len(hl) for hl in header_lines)
    # Bottom-align: pad shorter headers with leading blank lines.
    header_lines = [[""] * (max_header_lines - len(hl)) + hl for hl in header_lines]

    widths = [max(_visible_len(line) for line in hl) for hl in header_lines]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], _visible_len(cell))

    with _print_lock:
        if title:
            print(title)
        for line_idx in range(max_header_lines):
            line_cells = [hl[line_idx] for hl in header_lines]
            print("  ".join(c.ljust(w) for c, w in zip(line_cells, widths)))
        print("  ".join("-" * w for w in widths))
        for row in rows:
            cells = [cell + " " * (w - _visible_len(cell)) for cell, w in zip(row, widths)]
            print("  ".join(cells))


# --- Data classes ---------------------------------------------------------

@dataclass(frozen=True)
class Repo:
    name: str
    url: str
    path: str
    branch: str
    rev: str
    reference: str = ""
    submodules: bool = False
    check: bool = True
    # Manifest file this entry came from, relative to the meta-repo root.
    source: str = ""


# --- Manifest loading -----------------------------------------------------

_REQUIRED_REPO_KEYS = frozenset({"url", "path", "branch"})
_KNOWN_REPO_KEYS = _REQUIRED_REPO_KEYS | {"name", "rev", "reference", "submodules", "check"}


MANIFEST_NAME = ".repotool.toml"
MANIFEST_DIR = ".repotool"


def _manifest_files(root: Path | str) -> list[Path]:
    """Manifest files under <root>, in load order: `.repotool.toml` if present,
    then every `*.toml` in the `.repotool/` directory sorted by name. Raises
    ManifestError when there are none."""
    root = Path(root)
    files: list[Path] = []
    single = root / MANIFEST_NAME
    if single.is_file():
        files.append(single)
    directory = root / MANIFEST_DIR
    if directory.is_dir():
        files.extend(sorted(p for p in directory.glob("*.toml") if p.is_file()))
    if not files:
        raise ManifestError(
            f"{MANIFEST_NAME} or {MANIFEST_DIR}/*.toml not found at {root}; "
            "repotool must run from the meta-repo, not a child"
        )
    return files


def _load_toml(manifest_path: Path) -> dict[str, object]:
    """Read and parse the manifest TOML. Raises ManifestError on a parse error."""
    try:
        with open(manifest_path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ManifestError(f"Failed to parse {manifest_path.name}: {e}") from e


def _parse_ignores(data: dict[str, object]) -> set[str]:
    """Validate and normalize the manifest's top-level `ignore` array: directory
    trees whose git checkouts are managed by another tool (e.g. the west
    workspace) and must not be reported as orphans. Same path rules as a repo
    `path` (relative, no `..`, a strict subdirectory of the meta-repo root)."""
    ignore = data.get("ignore", [])
    if not isinstance(ignore, list):
        raise ManifestError("`ignore` must be an array of paths")
    result: set[str] = set()
    for p in ignore:
        if not isinstance(p, str):
            raise ManifestError(f"`ignore` entries must be strings; got {p!r}")
        if p.startswith("/"):
            raise ManifestError(f"`ignore` path must be relative; got {p!r}")
        if ".." in PurePosixPath(p).parts:
            raise ManifestError(f"`ignore` path must not contain '..'; got {p!r}")
        norm = str(PurePosixPath(p)) if p else ""
        if norm in ("", "."):
            raise ManifestError(
                f"`ignore` path must be a strict subdirectory of the "
                f"meta-repo root; got {p!r}"
            )
        result.add(norm)
    return result


def _repos_from_data(data: dict[str, object], source: str) -> list[Repo]:
    """Validate one manifest file's `[[repo]]` tables. Checks that span files
    (duplicate names and paths, ancestry, `reference` targets) are done by
    `load_manifest`."""
    raw_repos = data.get("repo", [])
    if not isinstance(raw_repos, list):
        raise ManifestError("[[repo]] must be an array of tables")
    if not raw_repos:
        raise ManifestError("must declare at least one [[repo]]")

    repos: list[Repo] = []
    for i, entry in enumerate(raw_repos):
        if not isinstance(entry, dict):
            raise ManifestError(f"[[repo]] entry #{i + 1} is not a table")
        # Label used in error messages — prefer explicit name, then path, then index.
        label = entry.get("name") or entry.get("path") or f"#{i + 1}"
        missing = _REQUIRED_REPO_KEYS - entry.keys()
        if missing:
            raise ManifestError(
                f"[[repo]] '{label}' missing required fields: {sorted(missing)}"
            )
        unknown = set(entry.keys()) - _KNOWN_REPO_KEYS
        if unknown:
            emit_warn(f"[[repo]] '{label}' has unknown keys: {sorted(unknown)}")

        for field_name in ("url", "path", "branch"):
            if not isinstance(entry[field_name], str):
                raise ManifestError(
                    f"[[repo]] '{label}'.{field_name} must be a string"
                )
        for opt_name in ("name", "rev", "reference"):
            if opt_name in entry and not isinstance(entry[opt_name], str):
                raise ManifestError(f"[[repo]] '{label}'.{opt_name} must be a string")
        for bool_name in ("submodules", "check"):
            if bool_name in entry and not isinstance(entry[bool_name], bool):
                raise ManifestError(f"[[repo]] '{label}'.{bool_name} must be a boolean")

        path = entry["path"]
        if path.startswith("/"):
            raise ManifestError(
                f"[[repo]] '{label}'.path must be relative; got {path!r}"
            )
        if ".." in PurePosixPath(path).parts:
            raise ManifestError(
                f"[[repo]] '{label}'.path must not contain '..'; got {path!r}"
            )
        norm = str(PurePosixPath(path)) if path else ""
        if norm in ("", "."):
            raise ManifestError(
                f"[[repo]] '{label}'.path must resolve to a strict "
                f"subdirectory of the meta-repo root; got {path!r}"
            )
        # `name` defaults to the normalized path when omitted.
        name = entry.get("name") or norm

        repos.append(Repo(
            name=name, url=entry["url"], path=norm,
            branch=entry["branch"], rev=entry.get("rev", ""),
            reference=entry.get("reference", ""),
            submodules=entry.get("submodules", False),
            check=entry.get("check", True),
            source=source,
        ))
    return repos


def load_manifest(root: Path | str) -> tuple[Repo, ...]:
    """Load and validate every manifest file under <root>, returning the repos
    in load order. Raises ManifestError on any issue."""
    root = Path(root)
    repos: list[Repo] = []
    seen_names: dict[str, str] = {}
    seen_paths: dict[str, str] = {}

    for manifest_path in _manifest_files(root):
        source = manifest_path.relative_to(root).as_posix()
        data = _load_toml(manifest_path)
        try:
            file_repos = _repos_from_data(data, source)
            # Validate `ignore` here so `check` catches a bad array.
            _parse_ignores(data)
        except ManifestError as e:
            raise ManifestError(f"{source}: {e}") from e
        for repo in file_repos:
            if repo.name in seen_names:
                raise ManifestError(
                    f"[[repo]] '{repo.name}' in {source} duplicates the name "
                    f"of a repo in {seen_names[repo.name]}"
                )
            seen_names[repo.name] = source
            if repo.path in seen_paths:
                raise ManifestError(
                    f"[[repo]] '{repo.name}' in {source}.path duplicates path of "
                    f"'{seen_paths[repo.path]}': {repo.path}"
                )
            seen_paths[repo.path] = repo.name
        repos.extend(file_repos)

    paths_sorted = sorted(seen_paths)
    for i, a in enumerate(paths_sorted):
        for b in paths_sorted[i + 1:]:
            if _is_ancestor(a, b):
                raise ManifestError(
                    f"path '{a}' (repo '{seen_paths[a]}') is an ancestor of "
                    f"path '{b}' (repo '{seen_paths[b]}'); "
                    f"one would clone inside the other"
                )

    _validate_references(repos)
    return tuple(repos)


def _validate_references(repos: list[Repo]) -> None:
    """Validate `reference` fields: target exists, no self-reference, no cycles."""
    by_name = {r.name: r for r in repos}
    for r in repos:
        if not r.reference:
            continue
        if r.reference == r.name:
            raise ManifestError(
                f"[[repo]] '{r.name}'.reference points to itself"
            )
        if r.reference not in by_name:
            raise ManifestError(
                f"[[repo]] '{r.name}'.reference = {r.reference!r} "
                f"but no repo with that name exists"
            )
    # Detect cycles by walking each chain.
    for start in repos:
        seen = [start.name]
        cur = start.reference
        while cur:
            if cur in seen:
                seen.append(cur)
                raise ManifestError(
                    f"reference cycle: {' -> '.join(seen)}"
                )
            seen.append(cur)
            cur = by_name[cur].reference


_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
# SCP-style URL: `[user@]host:path`. Reject anything that already contains `://`.
_SCP_URL_RE = re.compile(r"^(?:[^@/:]+@)?(?P<host>[^@/:]+):(?P<path>.+)$")


def _short(s: str) -> str:
    """Abbreviate a SHA to 7 chars; leave tags/branches/short refs alone."""
    return s[:7] if _SHA_RE.match(s) and len(s) > 7 else s


def _to_https(url: str) -> str:
    """Convert a git URL to its `https://` form.

    Handles git's three remote-URL syntaxes: scp-like `git@host:path`,
    full URLs (`ssh://`, `git://`, `http://`, `https://`), and is a no-op
    on schemes it doesn't recognize (e.g. `file://`, bare paths).
    """
    if url.startswith("https://"):
        return url
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    for scheme in ("ssh://", "git://"):
        if url.startswith(scheme):
            rest = url[len(scheme):]
            # Strip a leading user@ from the authority component only.
            authority = rest.split("/", 1)[0]
            if "@" in authority:
                rest = rest.split("@", 1)[1]
            return "https://" + rest
    if "://" not in url:
        m = _SCP_URL_RE.match(url)
        if m:
            return f"https://{m['host']}/{m['path']}"
    return url


def _is_ancestor(a: str, b: str) -> bool:
    a_parts = PurePosixPath(a).parts
    b_parts = PurePosixPath(b).parts
    return len(a_parts) < len(b_parts) and b_parts[:len(a_parts)] == a_parts


# --- Git wrapper ----------------------------------------------------------

_GitLogEntries = list[tuple[list[str], "subprocess.CompletedProcess[str]"]]


class _GitLogContext(threading.local):
    """Thread-local capture buffer for `--verbose`. `entries=None` disables capture."""
    entries: _GitLogEntries | None = None


_git_log_local = _GitLogContext()


def _run_git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    res = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, check=False)
    log = _git_log_local.entries
    if log is not None:
        log.append((args, res))
    return res


def _format_git_log(entries: _GitLogEntries) -> list[str]:
    """Format captured git invocations for --verbose output."""
    lines: list[str] = []
    for args, res in entries:
        lines.append(f"    {COLORS['dim']}$ git {' '.join(args)}{RESET}")
        for line in (res.stdout or "").rstrip("\n").splitlines():
            lines.append(f"      {line}")
        for line in (res.stderr or "").rstrip("\n").splitlines():
            lines.append(f"      {COLORS['dim']}{line}{RESET}")
    return lines


def _check_git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    res = _run_git(args, cwd=cwd)
    if res.returncode != 0:
        raise GitError(args, res.returncode, res.stdout, res.stderr)
    return res


def _git_root() -> Path:
    try:
        res = _check_git(["rev-parse", "--show-toplevel"])
    except GitError:
        raise RepoToolError("not inside a git repository") from None
    return Path(res.stdout.strip())


# --- Worktree peer discovery ---------------------------------------------

def _list_worktrees(root: Path) -> list[Path]:
    try:
        res = _check_git(["worktree", "list", "--porcelain"], cwd=root)
    except (GitError, RepoToolError):
        return [root]
    paths: list[Path] = []
    for line in res.stdout.splitlines():
        if line.startswith("worktree "):
            paths.append(Path(line[len("worktree "):]))
    return paths or [root]


def _find_reference_peer(repo: Repo, root: Path) -> Path | None:
    """Find an existing clone of `repo` to share objects with via --reference.

    Only the primary worktree is ever considered — never a clone living in a
    secondary worktree. A secondary worktree can be removed out from under us,
    which would break the alternates of any clone that referenced it; the
    primary repo is the stable choice. `git worktree list` always lists the
    main working tree first.
    """
    worktrees = _list_worktrees(root)
    if not worktrees:
        return None
    primary = worktrees[0]
    try:
        if primary.resolve() == root.resolve():
            return None
    except OSError:
        return None
    candidate = primary / repo.path
    if (candidate / ".git").exists():
        return candidate
    return None


# --- Sync (per-repo) ------------------------------------------------------

@dataclass
class SyncResult:
    repo: Repo
    label: str
    detail: str
    warning: str = ""
    ok: bool = True
    verbose_log: list[str] = field(default_factory=list)


def _sync_one(
    repo: Repo,
    root: Path,
    name_w: int = 0,
    verbose: bool = False,
    reference: str | Path | None = None,
    depth: int | None = None,
    override: RevOverride | None = None,
) -> SyncResult:
    log_entries: _GitLogEntries | None = [] if verbose else None
    _git_log_local.entries = log_entries
    try:
        target = root / repo.path
        if not target.exists():
            emit(f"  {COLORS['cyan']}{repo.name:<{name_w}}{RESET}  cloning {repo.url}")
            result = _sync_clone(repo, root, target, reference, depth, override)
        elif not (target / ".git").exists():
            result = SyncResult(
                repo, "failed",
                detail=f"{target.relative_to(root)} exists but is not a git repo",
                ok=False,
            )
        else:
            emit(f"  {COLORS['cyan']}{repo.name:<{name_w}}{RESET}  fetching")
            result = _sync_existing(repo, root, target, depth, override)
    finally:
        _git_log_local.entries = None
    if verbose and log_entries is not None:
        result.verbose_log = _format_git_log(log_entries)
    return result


def _fetch_missing_rev(target: Path, rev: str) -> bool:
    """Checking out the pinned `rev` failed because the object isn't present
    locally — common with `--depth` (the SHA sits outside the shallow window),
    or after an upstream rebase moved it deeper in history. Fetch the rev
    directly by name: git delivers it as long as it is still reachable from
    *some* ref on the server (the same branch deeper down, or a different
    branch/tag it was rebased onto), even into a shallow clone. Returns True
    if the rev is now checked out. Returns False when the object is genuinely
    gone (unreachable from every remote ref — a rebase/force-push orphaned it),
    which no fetch can recover."""
    if _run_git(["fetch", "origin", rev], cwd=target).returncode != 0:
        return False
    return _run_git(["checkout", rev], cwd=target).returncode == 0


def _rebased_away_detail(rev: str) -> str:
    return (f"rev {_short(rev)} not found on origin — unreachable from any "
            f"remote ref (likely rebased away); update the manifest with "
            f"'rt snapshot' or pin a current rev")


def _fetch_args(repo: Repo) -> list[str]:
    """The superproject `git fetch origin` args. For a submodule-bearing repo,
    add `--no-recurse-submodules`: git's default on-demand recursion would try
    to fetch newly-referenced submodule commits *during this fetch*, using the
    submodule's currently-configured (possibly stale) URL — which fails when an
    incoming commit repoints the submodule at a new remote. Submodule fetching
    is instead left to `_update_submodules`, which runs `submodule sync` first
    so it uses the corrected URL."""
    args = ["fetch", "origin"]
    if repo.submodules:
        args.append("--no-recurse-submodules")
    return args


def _update_submodules(target: Path, *, depth: int | None = None, force: bool = False) -> None:
    """Align this repo's submodules to the gitlinks recorded at the checked-out
    commit. Checkout-only at the submodule level too: `--init` populates newly
    added submodules, `--recursive` descends nested ones, and the absence of
    `--remote` means each submodule lands on the exact SHA the parent commit
    pins (never a moving branch tip). Raises GitError on failure.

    `submodule sync` first copies each submodule's `.gitmodules` URL into
    `.git/config`, so an upstream URL change (the checked-out commit points a
    submodule at a new remote) is picked up before the update fetches from it —
    otherwise the update would fetch from the stale configured URL and fail."""
    _check_git(["submodule", "sync", "--recursive"], cwd=target)
    args = ["submodule", "update", "--init", "--recursive"]
    if force:
        args.append("--force")
    if depth is not None:
        # Keep submodule history shallow to match a shallow parent clone.
        args += ["--depth", str(depth)]
    _check_git(args, cwd=target)


def _sync_clone(
    repo: Repo, root: Path, target: Path,
    reference: str | Path | None = None,
    depth: int | None = None,
    override: RevOverride | None = None,
) -> SyncResult:
    target.parent.mkdir(parents=True, exist_ok=True)
    clone_args: list[str] = ["clone"]
    if depth is not None:
        # Shallow clones and --reference / alternates are mutually
        # antagonistic (alternates undermine the point of shallow). When
        # --depth is in effect, ignore both manifest references and
        # peer auto-discovery. Also pin the clone to `repo.branch`: a
        # plain shallow clone fetches only the default branch's history,
        # so a pinned `rev` on any other branch would be unreachable.
        clone_args += ["--depth", str(depth), "--branch", repo.branch]
    elif reference is not None:
        # User-supplied reference. Use strict --reference so the clone fails
        # loudly if the path isn't a valid repo.
        clone_args += ["--reference", str(reference)]
    else:
        peer = _find_reference_peer(repo, root)
        if peer is not None:
            clone_args += ["--reference-if-able", str(peer)]
    clone_args += [repo.url, str(target)]
    try:
        _check_git(clone_args)
    except GitError as e:
        return SyncResult(repo, "failed", detail=f"clone: {e.error_line}", ok=False)
    if override is not None:
        fail = _checkout_override(repo, target, override, depth)
        if fail:
            return SyncResult(repo, "failed", detail=f"clone OK; {fail}", ok=False)
    else:
        checkout_ref = repo.rev or repo.branch
        try:
            _check_git(["checkout", checkout_ref], cwd=target)
        except GitError as e:
            # The rev isn't in what we cloned. With --depth the pinned SHA can
            # sit outside the shallow window; try to fetch it directly first.
            if not (repo.rev and _fetch_missing_rev(target, repo.rev)):
                detail = (_rebased_away_detail(repo.rev) if repo.rev
                          else f"clone OK; checkout {checkout_ref}: {e.error_line}")
                return SyncResult(repo, "failed", detail=detail, ok=False)
        _attach_to_branch_if_at_tip(repo, target)
    if repo.submodules:
        try:
            _update_submodules(target, depth=depth)
        except GitError as e:
            return SyncResult(repo, "failed", detail=f"clone OK; submodules: {e.error_line}", ok=False)
    if override is not None:
        head = _safe_rev_parse(target, "HEAD")
        return SyncResult(
            repo, "cloned",
            detail=f"cloned @ {head[:7] or '?'} (override, detached)",
        )
    if repo.rev:
        where = repo.branch if _safe_branch(target) == repo.branch \
            else f"{repo.branch}, detached"
        detail = f"cloned @ {_short(repo.rev)} ({where})"
    else:
        head = _safe_rev_parse(target, "HEAD")
        detail = f"cloned @ {head[:7] if head else '?'} ({repo.branch}, unpinned)"
    return SyncResult(repo, "cloned", detail=detail)


def _ensure_branch_tracked(target: Path, branch: str) -> None:
    """Widen a single-branch clone's fetch refspec to include `branch`.

    A single-branch clone (e.g. from `sync --depth`, which adds
    `--branch <branch> --single-branch`, or a hand `git clone --single-branch`)
    tracks only one branch in `remote.origin.fetch`, so a plain `git fetch
    origin` never populates origin/<branch> for any other branch. That's the
    state left behind when the manifest `branch` changes: the fetch in
    sync/reset/fast-forward can't see the new branch and status shows `no
    remote`. Add the branch to the tracked set so the following fetch creates
    the ref. A no-op on a normal wildcard clone, or when the branch is already
    tracked."""
    res = _run_git(["config", "--get-all", "remote.origin.fetch"], cwd=target)
    if res.returncode != 0:
        return
    specs = res.stdout.split()
    if any("refs/heads/*" in s for s in specs):
        return  # wildcard clone already fetches every branch
    dst = f"refs/remotes/origin/{branch}"
    if any(s.rsplit(":", 1)[-1] == dst for s in specs):
        return  # already tracked
    _run_git(["remote", "set-branches", "--add", "origin", branch], cwd=target)


def _sync_existing(
    repo: Repo, root: Path, target: Path,
    depth: int | None = None,
    override: RevOverride | None = None,
) -> SyncResult:
    pre_head = _safe_rev_parse(target, "HEAD")
    pre_branch = _safe_branch(target)

    warning = ""
    cur_url = _safe_remote_url(target, "origin")
    if cur_url and cur_url != repo.url:
        shown: Path = target
        try:
            shown = target.relative_to(root)
        except ValueError:
            pass
        warning = (
            f"origin URL differs from manifest (have {cur_url!r}, manifest {repo.url!r}); "
            f"run 'git -C {shown} remote set-url origin {repo.url}' to fix"
        )

    _ensure_branch_tracked(target, repo.branch)
    try:
        _check_git(_fetch_args(repo), cwd=target)
    except GitError as e:
        return SyncResult(repo, "failed", detail=f"fetch: {e.error_line}",
                          warning=warning, ok=False)
    if override is not None:
        fail = _checkout_override(repo, target, override, depth)
        if fail:
            return SyncResult(repo, "failed", detail=fail, warning=warning, ok=False)
    else:
        checkout_ref = repo.rev or repo.branch
        try:
            _check_git(["checkout", checkout_ref], cwd=target)
        except GitError as e:
            # An upstream rebase can move the pinned SHA out of the fetched
            # history; try to fetch it directly before failing.
            if not (repo.rev and _fetch_missing_rev(target, repo.rev)):
                detail = (_rebased_away_detail(repo.rev) if repo.rev
                          else f"checkout {checkout_ref}: {e.error_line}")
                return SyncResult(repo, "failed", detail=detail,
                                  warning=warning, ok=False)
        _attach_to_branch_if_at_tip(repo, target)
    if repo.submodules:
        try:
            _update_submodules(target)
        except GitError as e:
            return SyncResult(repo, "failed", detail=f"submodules: {e.error_line}",
                              warning=warning, ok=False)

    post_head = _safe_rev_parse(target, "HEAD")
    post_branch = _safe_branch(target)

    if override is not None:
        if pre_head and post_head and pre_head != post_head:
            detail = f"updated {pre_head[:7]} → {post_head[:7]} (override)"
            return SyncResult(repo, "updated", detail=detail, warning=warning)
        detail = f"unchanged @ {(post_head or '?')[:7]} (override)"
        return SyncResult(repo, "unchanged", detail=detail, warning=warning)

    if (pre_branch and post_branch and pre_branch != post_branch
            and pre_branch != "HEAD" and post_branch != "HEAD"):
        label = "switched"
        detail = f"switched {pre_branch} → {post_branch}"
    elif pre_head and post_head and pre_head != post_head:
        label = "updated"
        detail = f"updated {pre_head[:7]} → {post_head[:7]}"
    else:
        label = "unchanged"
        rev_display = _short(repo.rev) if repo.rev else (post_head[:7] if post_head else "?")
        detail = f"unchanged @ {rev_display}"

    return SyncResult(repo, label, detail=detail, warning=warning)


def _safe_rev_parse(target: Path, ref: str) -> str:
    res = _run_git(["rev-parse", ref], cwd=target)
    return res.stdout.strip() if res.returncode == 0 else ""


def _safe_branch(target: Path) -> str:
    res = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=target)
    return res.stdout.strip() if res.returncode == 0 else ""


def _is_shallow(target: Path) -> bool:
    res = _run_git(["rev-parse", "--is-shallow-repository"], cwd=target)
    return res.returncode == 0 and res.stdout.strip() == "true"


def _safe_remote_url(target: Path, remote: str) -> str:
    res = _run_git(["remote", "get-url", remote], cwd=target)
    return res.stdout.strip() if res.returncode == 0 else ""


def _attach_to_branch_if_at_tip(repo: Repo, target: Path) -> bool:
    """If HEAD is detached at the tip of origin/<repo.branch>, attach it to a
    local <repo.branch>. Returns True if HEAD ended up on repo.branch.

    sync is checkout-only: this never *moves* an existing local branch. It
    attaches only when that's free — the local branch is absent (create it at
    the tip, tracking origin) or already points at the target. If a local
    <branch> exists pointing elsewhere, attaching would rewrite it, so HEAD
    stays detached (the user can advance it with `rt fast-forward`)."""
    if _safe_branch(target) != "HEAD":
        return False  # already attached (rev was a branch name, or unpinned)
    head_sha = _safe_rev_parse(target, "HEAD")
    origin_sha = _safe_rev_parse(target, f"origin/{repo.branch}")
    if not head_sha or not origin_sha or head_sha != origin_sha:
        return False  # not sitting at the branch tip — leave detached
    local_sha = _safe_rev_parse(target, f"refs/heads/{repo.branch}")
    if not local_sha:
        # No local branch yet: create it at the tip, attached and tracking.
        res = _run_git(
            ["checkout", "-B", repo.branch, f"origin/{repo.branch}"], cwd=target,
        )
        return res.returncode == 0
    if local_sha == head_sha:
        # Local branch already points here: just attach, no movement.
        res = _run_git(["checkout", repo.branch], cwd=target)
        return res.returncode == 0
    return False  # local branch points elsewhere; attaching would move it


# --- Rev overrides (sync only) --------------------------------------------
#
# `rt sync --rev NAME=REF` and the RT_REVS environment variable replace a
# repo's manifest `rev` for one run, so CI can test a child's commit against
# the rest of the manifest without editing it. The manifest on disk is never
# touched. An overridden repo is always left on a detached HEAD: the ref is
# fetched by name and checked out as FETCH_HEAD, never routed through
# `checkout <rev>`, which could pick a stale local branch.

REV_OVERRIDE_ENV = "RT_REVS"

# An abbreviated SHA: 7-39 hex chars with at least one letter. All-digit
# strings are let through, since they're as likely to be a tag or branch
# (a date such as 20261102) as a SHA.
_SHORT_SHA_RE = re.compile(r"^(?=.*[a-fA-F])[0-9a-fA-F]{7,39}$")
_FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


@dataclass(frozen=True)
class RevOverride:
    ref: str
    source: str  # where it came from: "--rev" or "RT_REVS"


def _parse_rev_entries(texts: Sequence[str], source: str) -> list[tuple[str, str]]:
    """Split `NAME=REF` entries out of `texts`. Each text may hold several
    entries separated by whitespace and/or commas. Validates syntax only; the
    names are resolved against the manifest by the caller."""
    entries: list[tuple[str, str]] = []
    for text in texts:
        for item in re.split(r"[\s,]+", text.strip()):
            if not item:
                continue
            name, sep, ref = item.partition("=")
            if not sep or not name or not ref:
                raise RepoToolError(
                    f"malformed rev override {item!r} in {source}: expected NAME=REF"
                )
            if ref.startswith("-"):
                raise RepoToolError(
                    f"rev override {item!r} in {source}: ref must not start with '-'"
                )
            if _SHORT_SHA_RE.match(ref):
                raise RepoToolError(
                    f"rev override {item!r} in {source}: {ref!r} looks like an "
                    f"abbreviated SHA; give the full 40-character SHA (remotes "
                    f"can't resolve short SHAs)"
                )
            entries.append((name, ref))
    return entries


def _resolve_override_name(repos: tuple[Repo, ...], name: str, source: str) -> str:
    """Resolve an override's repo name or path the same way positional `names`
    are resolved, returning the repo's name."""
    try:
        (repo,) = _filter_repos(repos, [name])
    except RepoToolError:
        raise RepoToolError(f"unknown repo in {source}: {name!r}") from None
    return repo.name


def _collect_rev_overrides(
    repos: tuple[Repo, ...],
    cli_revs: Sequence[str],
    env_value: str,
    sync_set: set[str],
) -> dict[str, RevOverride]:
    """Merge `RT_REVS` and `--rev` into one override per repo (keyed by repo
    name), validating all of it before any git operation.

    Every entry must name a real manifest repo. An `RT_REVS` entry for a repo
    outside `sync_set` is ignored: CI jobs sync subsets, and the pipeline
    variable applies to all of them. A `--rev` entry for such a repo is an
    error, since it can only be a mistake on the command line. CLI entries win
    over `RT_REVS` for the same repo."""
    merged: dict[str, RevOverride] = {}
    sources: list[tuple[str, Sequence[str]]] = [
        (REV_OVERRIDE_ENV, [env_value] if env_value else []),
        ("--rev", cli_revs),
    ]
    for source, texts in sources:
        seen: set[str] = set()
        for name, ref in _parse_rev_entries(texts, source):
            repo_name = _resolve_override_name(repos, name, source)
            if repo_name in seen:
                raise RepoToolError(f"{source}: repo {repo_name!r} overridden more than once")
            seen.add(repo_name)
            if repo_name not in sync_set:
                if source == "--rev":
                    raise RepoToolError(
                        f"--rev {name}: {repo_name!r} is not in the sync set "
                        f"(name it on the command line, or clone it first)"
                    )
                continue
            merged[repo_name] = RevOverride(ref, source)  # --rev runs last, so it wins
    return merged


def _display_ref(ref: str) -> str:
    """Abbreviate a full SHA for the `overrides:` line; leave other refs alone."""
    short = _short(ref)
    return f"{short}…" if short != ref else ref


def _strip_fatal(line: str) -> str:
    return line.removeprefix("fatal: ")


def _checkout_override(
    repo: Repo, target: Path, override: RevOverride, depth: int | None,
) -> str:
    """Fetch `override.ref` and check it out detached. Returns "" on success,
    otherwise the failure detail for the repo's SyncResult.

    The ref is fetched by name (`git fetch origin <ref>`) and checked out as
    FETCH_HEAD. That works for SHAs, branch and tag names, and full refs such as
    refs/merge-requests/N/head, and it doesn't depend on a local branch of that
    name existing. HEAD is left detached: an override is ephemeral, so this
    never creates or attaches a local branch."""
    ref = override.ref
    args = ["fetch"]
    if repo.submodules:
        args.append("--no-recurse-submodules")
    if depth is not None and _is_shallow(target):
        # A plain fetch into a shallow clone can pull far more history than
        # the run asked for; keep it to the same depth as the clone. Only for
        # a clone that's already shallow: like the clone itself, --depth must
        # never truncate the history of an existing full clone.
        args += ["--depth", str(depth)]
    args += ["origin", ref]
    try:
        _check_git(args, cwd=target)
    except GitError as e:
        return f"fetch override {ref}: {_strip_fatal(e.error_line)}"
    try:
        _check_git(["checkout", "--detach", "FETCH_HEAD"], cwd=target)
    except GitError as e:
        return f"checkout override {ref}: {_strip_fatal(e.error_line)}"
    if _FULL_SHA_RE.match(ref):
        head = _safe_rev_parse(target, "HEAD")
        if head != ref.lower():
            return f"override {ref}: HEAD landed on {head[:7] or '?'}"
    return ""


# --- Orphan walk ----------------------------------------------------------

def load_ignores(root: Path | str) -> set[str]:
    """Validated, normalized top-level `ignore` paths, unioned across every
    manifest file. Shares validation with `load_manifest` via `_parse_ignores`,
    so the orphan walk and the `check` preflight agree on what a well-formed
    `ignore` array is."""
    ignores: set[str] = set()
    for manifest_path in _manifest_files(root):
        ignores |= _parse_ignores(_load_toml(manifest_path))
    return ignores


def _walk_orphans(
    root: Path, declared_paths: set[str], ignores: set[str] | None = None,
) -> list[Path]:
    """Walk meta-repo for dirs containing .git that aren't in declared_paths.
    Subtrees named in `ignores` (the manifest's top-level `ignore` list) are
    skipped. Pure over its arguments — callers supply `ignores` from
    `load_ignores` rather than having the walk re-read the manifest."""
    orphans: list[Path] = []
    root_resolved = root.resolve()
    declared_abs: set[Path] = set()
    for p in declared_paths:
        try:
            declared_abs.add((root / p).resolve())
        except OSError:
            pass
    ignored_abs: set[Path] = set()
    for p in ignores or set():
        try:
            ignored_abs.add((root / p).resolve())
        except OSError:
            pass

    for dirpath, dirnames, _ in os.walk(root):
        d = Path(dirpath)
        dirnames[:] = [n for n in dirnames if n != ".git"]

        try:
            d_resolved = d.resolve()
        except OSError:
            continue
        if d_resolved == root_resolved:
            continue
        if d_resolved in ignored_abs:
            dirnames[:] = []
            continue

        if (d / ".git").exists():
            if d_resolved not in declared_abs:
                orphans.append(d)
            dirnames[:] = []

    orphans.sort()
    return orphans


# --- Sync orchestration --------------------------------------------------

def _filter_repos(
    repos: tuple[Repo, ...],
    names: list[str] | None,
    *,
    root: Path | None = None,
    existing_only: bool = False,
) -> tuple[Repo, ...]:
    """Filter to repos matching `names`, each a repo name or manifest path.
    Raises on any unknown name.

    When `names` is empty and `existing_only` is True, restrict the result to
    repos with a clone already present under `root`.
    """
    if names:
        # Tab-completion against an on-disk path may append a trailing slash;
        # strip it so a name typed as `web-ui/` still matches the `web-ui` repo.
        names = [n.rstrip("/") for n in names]
        by_name = {r.name: r for r in repos}
        # Manifest paths are validated unique, so they identify a repo just as
        # well as names do — and they're what build scripts and tab completion
        # naturally produce. Names win the (unlikely) cross-repo collision.
        by_path = {r.path: r for r in repos}
        unknown = [n for n in names if n not in by_name and n not in by_path]
        if unknown:
            raise RepoToolError(f"unknown repo(s): {', '.join(unknown)}")
        wanted = {(by_name[n] if n in by_name else by_path[n]).name for n in names}
        return tuple(r for r in repos if r.name in wanted)
    if existing_only and root is not None:
        return tuple(r for r in repos if (root / r.path / ".git").exists())
    return repos


def _compute_sync_waves(repos: tuple[Repo, ...]) -> list[list[Repo]]:
    """Group `repos` into clone-order waves so that any repo with a manifest
    `reference` pointing to another repo in `repos` runs in a later wave than
    its target. References to repos *not* in this filtered set are treated as
    wave-0 (the target must already be on disk, or sync_one will fail)."""
    included = {r.name for r in repos}
    wave_for: dict[str, int] = {}

    def wave_of(r: Repo) -> int:
        if r.name in wave_for:
            return wave_for[r.name]
        if not r.reference or r.reference not in included:
            wave_for[r.name] = 0
            return 0
        target = next(t for t in repos if t.name == r.reference)
        w = wave_of(target) + 1
        wave_for[r.name] = w
        return w

    for r in repos:
        wave_of(r)
    max_wave = max(wave_for.values(), default=0)
    return [[r for r in repos if wave_for[r.name] == w] for w in range(max_wave + 1)]


def sync(
    *,
    names: list[str] | None = None,
    jobs: int = 8,
    verbose: bool = False,
    reference: str | Path | None = None,
    depth: int | None = None,
    https: bool = False,
    include_missing: bool = False,
    revs: Sequence[str] = (),
) -> int:
    root = _git_root()
    all_repos = load_manifest(root)
    repos = _filter_repos(
        all_repos, names, root=root, existing_only=not include_missing,
    )
    focused = bool(names)
    if reference is not None and len(repos) != 1:
        raise RepoToolError("--reference requires exactly one repo argument")
    if depth is not None and depth < 1:
        raise RepoToolError("--depth must be a positive integer")
    if depth is not None and reference is not None:
        raise RepoToolError("--depth and --reference are mutually exclusive")
    if https:
        repos = tuple(_dc_replace(r, url=_to_https(r.url)) for r in repos)
    if not repos:
        raise RepoToolError(
            f"no repos to sync — none of the {len(all_repos)} manifest entries "
            f"are cloned yet; pass --all to clone them"
        )
    overrides = _collect_rev_overrides(
        all_repos, revs, os.environ.get(REV_OVERRIDE_ENV, ""),
        {r.name for r in repos},
    )
    name_w = max((len(r.name) for r in repos), default=10)

    skipped = len(all_repos) - len(repos)
    suffix = ""
    if not names and not include_missing and skipped > 0:
        suffix = (
            f" {COLORS['dim']}(skipping {skipped} not yet cloned; "
            f"pass --all to include){RESET}"
        )
    emit(f"{COLORS['bold']}Syncing {len(repos)} repo(s)…{RESET}{suffix}")
    if overrides:
        shown = ", ".join(
            f"{r.name}={_display_ref(overrides[r.name].ref)} ({overrides[r.name].source})"
            for r in repos if r.name in overrides
        )
        emit(f"{COLORS['yellow']}overrides: {shown}{RESET}")

    by_name = {r.name: r for r in all_repos}

    def ref_for(r: Repo) -> str | Path | None:
        if reference is not None:
            return reference
        if not r.reference:
            return None
        return root / by_name[r.reference].path

    results: list[SyncResult] = []
    interrupted = False
    waves = _compute_sync_waves(repos)
    for wave in waves:
        if interrupted:
            break
        with ThreadPoolExecutor(max_workers=max(1, jobs)) as executor:
            futures = {
                executor.submit(
                    _sync_one, r, root, name_w, verbose, ref_for(r), depth,
                    overrides.get(r.name),
                ): r
                for r in wave
            }
            try:
                for future in as_completed(futures):
                    r = future.result()
                    results.append(r)
                    if r.verbose_log:
                        emit(f"{COLORS['bold']}{r.repo.name}:{RESET}")
                        for line in r.verbose_log:
                            emit(line)
                    color = "red" if not r.ok else ("yellow" if r.label == "switched" else "green")
                    emit(f"  {COLORS[color]}{r.repo.name:<{name_w}}{RESET}  {r.detail}")
                    if r.warning:
                        emit_warn(f"{r.repo.name}: {r.warning}")
            except KeyboardInterrupt:
                emit(f"{COLORS['yellow']}Interrupted. Waiting for in-flight git operations to finish...{RESET}", err=True)
                executor.shutdown(cancel_futures=True)
                interrupted = True

    if not focused:
        declared = {r.path for r in all_repos}
        orphans = _walk_orphans(root, declared, load_ignores(root))
        if orphans:
            emit(f"\n{COLORS['bold']}Orphan directories:{RESET}")
            for o in orphans:
                rel: Path = o
                try:
                    rel = o.relative_to(root)
                except ValueError:
                    pass
                emit(f"  {rel}")

    if interrupted:
        return 130
    failed = [r for r in results if not r.ok]
    if failed:
        failed_names = ", ".join(r.repo.name for r in failed)
        emit_error(
            f"sync failed for {len(failed)} of {len(results)} repo(s): {failed_names}"
        )
        return 1
    return 0


# --- Status --------------------------------------------------------------

_StatusRow = tuple[str, str, str, str, str, str, str]


def _status_one(repo: Repo, root: Path) -> _StatusRow:
    """Return one status row: (name, branch, rev, current, manifest, upstream, dirty)."""
    target = root / repo.path
    branch_cell = repo.branch
    rev_cell = _short(repo.rev) if repo.rev else f"{COLORS['dim']}(unpinned){RESET}"
    if not target.exists():
        return (repo.name, branch_cell, rev_cell,
                f"{COLORS['dim']}(not synced){RESET}", "-", "-", "-")
    if not (target / ".git").exists():
        return (repo.name, branch_cell, rev_cell,
                f"{COLORS['red']}(not a repo){RESET}", "-", "-", "-")

    head_sha = _safe_rev_parse(target, "HEAD")
    current = head_sha[:7] if head_sha else "?"

    if repo.rev:
        manifest_cell = _manifest_status(target, repo.rev, head_sha, repo.name)
    else:
        manifest_cell = f"{COLORS['dim']}unpinned{RESET}"
    upstream = _upstream_status(target, repo, head_sha)

    return (repo.name, branch_cell, rev_cell, current, manifest_cell, upstream, _state_cell(target))


def _has_local_only_branches(target: Path) -> bool:
    """True if the repo has any local branch with no `origin/<same-name>` counterpart.

    Skipped when the repo has no `origin` remote configured — without a remote
    the concept doesn't apply.
    """
    if not _safe_remote_url(target, "origin"):
        return False
    res = _run_git(["for-each-ref", "--format=%(refname:short)", "refs/heads/"], cwd=target)
    if res.returncode != 0:
        return False
    locals_ = [b for b in res.stdout.splitlines() if b]
    if not locals_:
        return False
    res2 = _run_git(
        ["for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/"], cwd=target,
    )
    if res2.returncode != 0:
        return False
    remotes = {
        line[len("origin/"):] for line in res2.stdout.splitlines()
        if line.startswith("origin/")
    }
    return any(b not in remotes for b in locals_)


def _state_cell(target: Path) -> str:
    """Render the Status cell: clean / untracked files / dirty / local branches / ?"""
    porcelain = _run_git(["status", "--porcelain"], cwd=target)
    if porcelain.returncode != 0:
        return f"{COLORS['dim']}?{RESET}"
    lines = porcelain.stdout.splitlines()
    if lines and not all(line.startswith("??") for line in lines):
        return f"{COLORS['yellow']}dirty{RESET}"
    # Untracked files rank above local branches: they block `snapshot`, whereas
    # local-only branches are just an informational heads-up. Showing the latter
    # while hiding untracked files would mask why a snapshot was refused.
    if lines:
        return f"{COLORS['cyan']}untracked files{RESET}"
    if _has_local_only_branches(target):
        return f"{COLORS['blue']}local branches{RESET}"
    return f"{COLORS['green']}clean{RESET}"


def _status_meta(root: Path) -> _StatusRow:
    """Status row for the meta-repo itself (no manifest fields, no manifest/upstream)."""
    head_sha = _safe_rev_parse(root, "HEAD")
    current = head_sha[:7] if head_sha else "?"
    return ("meta", "", "", current, "", "", _state_cell(root))


def _manifest_status(target: Path, rev: str, head_sha: str, name: str = "") -> str:
    """HEAD vs manifest rev: in-sync / N ahead / N behind / N ahead / M behind / rev absent / diverged.

    Two distinct undetermined states, kept apart — both emit a warning:
    - `rev absent`: the manifest rev doesn't resolve to a local object/ref (a SHA
      below a --depth window or GC'd, or a tag/branch name never fetched), so
      HEAD can't be compared against it. Sync state is genuinely undetermined
      (HEAD could even be on the pinned commit).
    - `diverged`: the rev resolves and differs from HEAD, but the ahead/behind
      counts couldn't be computed — we know it's out of sync, only the
      direction/magnitude is unavailable.
    """
    prefix = f"{name}: " if name else ""
    res = _run_git(["rev-parse", "--verify", f"{rev}^{{commit}}"], cwd=target)
    if res.returncode != 0:
        emit_warn(
            f"{prefix}manifest rev '{_short(rev)}' does not resolve locally; "
            "sync state undetermined"
        )
        return f"{COLORS['yellow']}rev absent{RESET}"
    rev_sha = res.stdout.strip()
    if rev_sha == head_sha:
        return f"{COLORS['green']}in-sync{RESET}"

    def _diverged() -> str:
        emit_warn(
            f"{prefix}HEAD differs from manifest rev '{_short(rev)}' but ahead/behind "
            "counts are unavailable"
        )
        return f"{COLORS['yellow']}diverged{RESET}"

    behind_res = _run_git(["rev-list", "--count", f"HEAD..{rev_sha}"], cwd=target)
    ahead_res = _run_git(["rev-list", "--count", f"{rev_sha}..HEAD"], cwd=target)
    if behind_res.returncode != 0 or ahead_res.returncode != 0:
        return _diverged()
    try:
        behind = int(behind_res.stdout.strip())
        ahead = int(ahead_res.stdout.strip())
    except ValueError:
        return _diverged()
    parts: list[str] = []
    if ahead:
        parts.append(f"{ahead} ahead")
    if behind:
        parts.append(f"{behind} behind")
    return f"{COLORS['yellow']}" + " / ".join(parts) + RESET


def _upstream_status(target: Path, repo: Repo, head_sha: str) -> str:
    """HEAD vs origin/<branch>: in-sync / N behind / N ahead / diverged / no remote."""
    origin_ref = f"origin/{repo.branch}"
    if _run_git(["rev-parse", "--verify", origin_ref], cwd=target).returncode != 0:
        return f"{COLORS['dim']}no remote{RESET}"
    origin_sha = _safe_rev_parse(target, origin_ref)
    if head_sha == origin_sha:
        return f"{COLORS['green']}in-sync{RESET}"

    behind_res = _run_git(["rev-list", "--count", f"HEAD..{origin_ref}"], cwd=target)
    ahead_res = _run_git(["rev-list", "--count", f"{origin_ref}..HEAD"], cwd=target)
    if behind_res.returncode != 0 or ahead_res.returncode != 0:
        return f"{COLORS['dim']}unknown{RESET}"
    try:
        behind = int(behind_res.stdout.strip())
        ahead = int(ahead_res.stdout.strip())
    except ValueError:
        return f"{COLORS['dim']}unknown{RESET}"
    if behind and ahead:
        return f"{COLORS['red']}diverged{RESET}"
    if behind:
        return f"{COLORS['yellow']}{behind} behind{RESET}"
    return f"{COLORS['yellow']}{ahead} ahead{RESET}"


def status(*, names: list[str] | None = None) -> int:
    """Show manifest/upstream/dirty state per child. Always exits 0."""
    root = _git_root()
    all_repos = load_manifest(root)
    repos = _filter_repos(all_repos, names)
    focused = bool(names)

    rows: list[_StatusRow] = []
    if not focused:
        rows.append(_status_meta(root))
    rows += [_status_one(r, root) for r in repos]
    _render_table(
        headers=[
            "Repository",
            "Manifest\nBranch",
            "Manifest\nRev",
            "Current\nRev",
            "vs\nManifest",
            "vs\nUpstream",
            "Status",
        ],
        rows=rows,
    )

    if not focused:
        declared = {r.path for r in all_repos}
        orphans = _walk_orphans(root, declared, load_ignores(root))
        if orphans:
            emit(f"\n{COLORS['bold']}Orphan directories:{RESET}")
            for o in orphans:
                rel: Path = o
                try:
                    rel = o.relative_to(root)
                except ValueError:
                    pass
                emit(f"  {rel}")

    return 0


# --- Reset ---------------------------------------------------------------

@dataclass
class ResetResult:
    repo: Repo
    label: str          # 'reset' | 'skipped' | 'failed'
    detail: str
    ok: bool = True
    verbose_log: list[str] = field(default_factory=list)


def _reset_one(
    repo: Repo, root: Path, name_w: int = 0, force: bool = False, verbose: bool = False,
) -> ResetResult:
    log_entries: _GitLogEntries | None = [] if verbose else None
    _git_log_local.entries = log_entries
    try:
        emit(f"  {COLORS['cyan']}{repo.name:<{name_w}}{RESET}  resetting")
        result = _reset_one_inner(repo, root, force)
    finally:
        _git_log_local.entries = None
    if verbose and log_entries is not None:
        result.verbose_log = _format_git_log(log_entries)
    return result


def _submodules_ahead(target: Path, target_sha: str) -> list[tuple[str, int]]:
    """For each initialized submodule, count commits on its current HEAD that
    aren't reachable from the SHA the target superproject commit (`target_sha`)
    pins for it. A forced `submodule update` would orphan those commits, so
    this mirrors the superproject's own commits-ahead guard one level down.

    Uncommitted submodule changes are intentionally not counted — `reset`
    discards uncommitted work in the superproject too; only local *commits*
    are protected without `--force`."""
    res = _run_git(["submodule", "status"], cwd=target)
    if res.returncode != 0:
        return []
    ahead: list[tuple[str, int]] = []
    for line in res.stdout.splitlines():
        # Status char: ' ' in sync, '+' checked-out differs, 'U' conflicts,
        # '-' not initialized (nothing local to lose — skip).
        if not line or line[0] == "-":
            continue
        parts = line[1:].split()
        if len(parts) < 2:
            continue
        subpath = parts[1]
        # The commit this submodule is pinned to *in the target tree*. Reading
        # it from the tree doesn't require the object to be present locally.
        pinned = _run_git(["rev-parse", f"{target_sha}:{subpath}"], cwd=target)
        if pinned.returncode != 0:
            continue
        cnt = _run_git(
            ["rev-list", "--count", f"{pinned.stdout.strip()}..HEAD"],
            cwd=target / subpath,
        )
        if cnt.returncode != 0:
            continue
        try:
            n = int(cnt.stdout.strip())
        except ValueError:
            continue
        if n > 0:
            ahead.append((subpath, n))
    return ahead


def _reset_one_inner(repo: Repo, root: Path, force: bool) -> ResetResult:
    target = root / repo.path
    if not target.exists():
        return ResetResult(repo, "skipped", detail="not synced")
    if not (target / ".git").exists():
        return ResetResult(
            repo, "failed",
            detail=f"{target.relative_to(root)} exists but is not a git repo",
            ok=False,
        )

    _ensure_branch_tracked(target, repo.branch)
    try:
        _check_git(_fetch_args(repo), cwd=target)
    except GitError as e:
        return ResetResult(repo, "failed", detail=f"fetch: {e.error_line}", ok=False)

    # Pick the realignment target:
    # - If rev is set, prefer origin/<rev> (so rev="main" follows the remote
    #   tip, loose-mode), otherwise resolve <rev> locally (tag/SHA case).
    # - If rev is empty (unpinned), the target is origin/<branch>.
    if repo.rev:
        origin_rev = f"origin/{repo.rev}"
        res = _run_git(["rev-parse", "--verify", f"{origin_rev}^{{commit}}"], cwd=target)
        if res.returncode == 0:
            target_sha = res.stdout.strip()
        else:
            res = _run_git(["rev-parse", "--verify", f"{repo.rev}^{{commit}}"], cwd=target)
            if res.returncode != 0:
                return ResetResult(
                    repo, "failed",
                    detail=f"rev {repo.rev!r} does not resolve",
                    ok=False,
                )
            target_sha = res.stdout.strip()
    else:
        origin_branch = f"origin/{repo.branch}"
        res = _run_git(["rev-parse", "--verify", f"{origin_branch}^{{commit}}"], cwd=target)
        if res.returncode != 0:
            return ResetResult(
                repo, "failed",
                detail=f"{origin_branch} does not resolve (unpinned rev)",
                ok=False,
            )
        target_sha = res.stdout.strip()

    pre_head = _safe_rev_parse(target, "HEAD")

    if not force:
        ahead_res = _run_git(["rev-list", "--count", f"{target_sha}..HEAD"], cwd=target)
        if ahead_res.returncode == 0:
            try:
                ahead = int(ahead_res.stdout.strip())
            except ValueError:
                ahead = 0
            if ahead > 0:
                max_shown = 20
                log_res = _run_git(
                    ["log", "--oneline", f"-n{max_shown}", f"{target_sha}..HEAD"],
                    cwd=target,
                )
                detail = f"would discard {ahead} commit(s); pass --force to proceed"
                if log_res.returncode == 0 and log_res.stdout.strip():
                    lines = log_res.stdout.strip().splitlines()
                    if ahead > max_shown:
                        lines.append(f"... and {ahead - max_shown} more")
                    detail += "\n      " + "\n      ".join(lines)
                return ResetResult(repo, "skipped", detail=detail, ok=False)

        # Submodules are aligned with a forced update below; extend the same
        # commits-ahead protection to them so `reset` never silently orphans
        # local submodule commits without --force.
        if repo.submodules:
            sub_ahead = _submodules_ahead(target, target_sha)
            if sub_ahead:
                listed = ", ".join(f"{p} (+{n})" for p, n in sub_ahead)
                return ResetResult(
                    repo, "skipped",
                    detail=f"submodule(s) have commits not on the pinned rev: "
                           f"{listed}; pass --force to discard",
                    ok=False,
                )

    try:
        _check_git(["reset", "--hard", target_sha], cwd=target)
        _check_git(["clean", "-fd"], cwd=target)
        _check_git(["checkout", "-B", repo.branch], cwd=target)
        if repo.submodules:
            # `--force` resets submodule working trees to the pinned gitlinks;
            # the foreach clean extends the superproject's `clean -fd` into each
            # submodule, which `clean -fd` alone doesn't touch.
            _update_submodules(target, force=True)
            _check_git(["submodule", "foreach", "--recursive", "git", "clean", "-fd"], cwd=target)
    except GitError as e:
        return ResetResult(repo, "failed", detail=e.error_line, ok=False)

    post_head = _safe_rev_parse(target, "HEAD")
    return ResetResult(repo, "reset", detail=f"reset {pre_head[:7]} → {post_head[:7]} ({repo.branch})")


def reset(
    *,
    names: list[str] | None = None,
    force: bool = False,
    jobs: int = 8,
    verbose: bool = False,
    include_missing: bool = False,
) -> int:
    root = _git_root()
    all_repos = load_manifest(root)
    repos = _filter_repos(
        all_repos, names, root=root, existing_only=not include_missing,
    )
    if not repos:
        raise RepoToolError(
            f"no repos to reset — none of the {len(all_repos)} manifest entries "
            f"are cloned yet; pass --all to include them"
        )
    name_w = max((len(r.name) for r in repos), default=10)

    skipped = len(all_repos) - len(repos)
    suffix = ""
    if not names and not include_missing and skipped > 0:
        suffix = (
            f" {COLORS['dim']}(skipping {skipped} not yet cloned; "
            f"pass --all to include){RESET}"
        )
    emit(f"{COLORS['bold']}Resetting {len(repos)} repo(s)…{RESET}{suffix}")

    results: list[ResetResult] = []
    interrupted = False
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as executor:
        futures = {
            executor.submit(_reset_one, r, root, name_w, force, verbose): r
            for r in repos
        }
        try:
            for future in as_completed(futures):
                r = future.result()
                results.append(r)
                if r.verbose_log:
                    emit(f"{COLORS['bold']}{r.repo.name}:{RESET}")
                    for line in r.verbose_log:
                        emit(line)
                if r.label == "reset":
                    color = "green"
                elif r.label == "skipped":
                    color = "yellow"
                else:
                    color = "red"
                emit(f"  {COLORS[color]}{r.repo.name:<{name_w}}{RESET}  {r.detail}")
        except KeyboardInterrupt:
            emit(f"{COLORS['yellow']}Interrupted. Waiting for in-flight git operations to finish...{RESET}", err=True)
            executor.shutdown(cancel_futures=True)
            interrupted = True

    if interrupted:
        return 130
    failed = [r for r in results if not r.ok]
    if failed:
        failed_names = ", ".join(r.repo.name for r in failed)
        emit_error(
            f"reset failed for {len(failed)} of {len(results)} repo(s): {failed_names}"
        )
        return 1
    return 0


# --- Fast-forward --------------------------------------------------------

@dataclass
class FastForwardResult:
    repo: Repo
    label: str          # 'advanced' | 'up-to-date' | 'skipped' | 'failed'
    detail: str
    ok: bool = True
    verbose_log: list[str] = field(default_factory=list)


def _fast_forward_one(
    repo: Repo, root: Path, name_w: int = 0, verbose: bool = False,
) -> FastForwardResult:
    log_entries: _GitLogEntries | None = [] if verbose else None
    _git_log_local.entries = log_entries
    try:
        result = _fast_forward_one_inner(repo, root, name_w)
    finally:
        _git_log_local.entries = None
    if verbose and log_entries is not None:
        result.verbose_log = _format_git_log(log_entries)
    return result


@dataclass
class _FFPreflight:
    """Outcome of the shared "is this clone safely advancable" checks.

    `label` is empty when the clone can be fast-forwarded to `target_sha`;
    otherwise it is `skipped` or `failed`, `detail` says why in prose and
    `reason` names the check that fired (callers that treat some checks
    specially match on that, not on the prose). Shared by `fast-forward` and
    `bump` so the two can never drift apart on what counts as safe.
    """
    label: str = ""
    detail: str = ""
    reason: str = ""
    branch: str = ""        # current branch; "" or "HEAD" when detached
    detached: bool = False
    head_sha: str = ""
    target_sha: str = ""    # origin/<repo.branch>

    @property
    def ok(self) -> bool:
        return not self.label


def _ff_preflight(
    repo: Repo, root: Path, *, on_fetch: Callable[[], None] | None = None,
) -> _FFPreflight:
    """Fetch `repo` and decide whether its clone can be advanced to
    origin/<repo.branch> without losing anything. `on_fetch` is called just
    before the fetch (progress reporting; omit it for machine-readable output).
    """
    target = root / repo.path
    if not target.exists():
        return _FFPreflight("skipped", "not synced", "not-synced")
    if not (target / ".git").exists():
        return _FFPreflight(
            "failed", f"{target.relative_to(root)} exists but is not a git repo",
            "not-a-repo",
        )

    porcelain = _run_git(["status", "--porcelain"], cwd=target)
    if porcelain.returncode != 0:
        return _FFPreflight("failed", "git status failed", "status")
    if porcelain.stdout.strip():
        return _FFPreflight("skipped", "dirty working tree", "dirty")

    branch = _safe_branch(target)
    detached = (not branch) or branch == "HEAD"
    if not detached and branch != repo.branch:
        return _FFPreflight(
            "skipped",
            f"on branch '{branch}', manifest branch is '{repo.branch}'",
            "wrong-branch",
            branch=branch,
        )

    if on_fetch is not None:
        on_fetch()
    _ensure_branch_tracked(target, repo.branch)
    try:
        _check_git(["fetch", "origin"], cwd=target)
    except GitError as e:
        return _FFPreflight("failed", f"fetch: {e.error_line}", "fetch")

    origin_ref = f"origin/{repo.branch}"
    if _run_git(["rev-parse", "--verify", origin_ref], cwd=target).returncode != 0:
        return _FFPreflight(
            "skipped", f"no remote branch {origin_ref}", "no-remote-branch",
        )

    head_sha = _safe_rev_parse(target, "HEAD")
    target_sha = _safe_rev_parse(target, origin_ref)
    if not head_sha or not target_sha:
        return _FFPreflight("failed", "could not resolve HEAD or remote tip", "resolve")

    ahead_res = _run_git(["rev-list", "--count", f"{target_sha}..HEAD"], cwd=target)
    if ahead_res.returncode != 0:
        return _FFPreflight("failed", "could not compute ahead count", "ahead-count")
    try:
        ahead = int(ahead_res.stdout.strip())
    except ValueError:
        return _FFPreflight("failed", "could not parse ahead count", "ahead-count")
    if ahead > 0:
        # NB: head_sha / target_sha are carried on the skip so a caller can
        # still reason about the remote tip (bump checks whether the pin
        # diverged, which is the usual cause of an "ahead" HEAD after a sync).
        return _FFPreflight(
            "skipped",
            f"{ahead} local commit(s) ahead of {origin_ref} — not fast-forwardable",
            "ahead",
            branch=branch, detached=detached,
            head_sha=head_sha, target_sha=target_sha,
        )

    if detached:
        # Refuse to silently move a local <branch> that has work not on origin.
        local_branch_ref = f"refs/heads/{repo.branch}"
        if _run_git(["rev-parse", "--verify", local_branch_ref], cwd=target).returncode == 0:
            branch_ahead_res = _run_git(
                ["rev-list", "--count", f"{origin_ref}..{local_branch_ref}"], cwd=target,
            )
            try:
                branch_ahead = int(branch_ahead_res.stdout.strip()) if branch_ahead_res.returncode == 0 else 0
            except ValueError:
                branch_ahead = 0
            if branch_ahead > 0:
                return _FFPreflight(
                    "skipped",
                    (
                        f"detached HEAD; local '{repo.branch}' has {branch_ahead} "
                        f"commit(s) not on {origin_ref} — not safely advancable"
                    ),
                    "local-branch-ahead",
                    branch=branch, detached=detached,
                    head_sha=head_sha, target_sha=target_sha,
                )

    return _FFPreflight(
        branch=branch, detached=detached, head_sha=head_sha, target_sha=target_sha,
    )


def _ff_advance(repo: Repo, target: Path, *, detached: bool) -> str:
    """Move the clone to origin/<repo.branch>. Returns "" on success, else a
    failure detail. A detached HEAD is advanced *and* attached to the branch.
    Only call this after `_ff_preflight` reported the move safe."""
    origin_ref = f"origin/{repo.branch}"
    if detached:
        try:
            _check_git(["checkout", "-B", repo.branch, origin_ref], cwd=target)
        except GitError as e:
            return f"checkout: {e.error_line}"
    else:
        try:
            _check_git(["merge", "--ff-only", origin_ref], cwd=target)
        except GitError as e:
            return f"merge --ff-only: {e.error_line}"
    return ""


def _fast_forward_one_inner(repo: Repo, root: Path, name_w: int) -> FastForwardResult:
    target = root / repo.path
    pre = _ff_preflight(
        repo, root,
        on_fetch=lambda: emit(f"  {COLORS['cyan']}{repo.name:<{name_w}}{RESET}  fetching"),
    )
    if not pre.ok:
        return FastForwardResult(
            repo, pre.label, detail=pre.detail, ok=pre.label != "failed",
        )

    head_sha, target_sha, detached = pre.head_sha, pre.target_sha, pre.detached
    if head_sha == target_sha and not detached:
        return FastForwardResult(repo, "up-to-date", detail=f"already at {head_sha[:7]}")

    failure = _ff_advance(repo, target, detached=detached)
    if failure:
        return FastForwardResult(repo, "failed", detail=failure, ok=False)

    new_head = _safe_rev_parse(target, "HEAD")
    if detached and new_head == head_sha:
        detail = f"attached to {repo.branch} @ {new_head[:7]}"
    else:
        # The manifest only goes stale if the new HEAD differs from the pinned
        # rev. Advancing *up to* the pinned rev (HEAD was behind it, e.g. rev
        # already sits at origin/<branch>'s tip) leaves the manifest in-sync.
        rev_sha = None
        if repo.rev:
            rp = _run_git(["rev-parse", "--verify", f"{repo.rev}^{{commit}}"], cwd=target)
            if rp.returncode == 0:
                rev_sha = rp.stdout.strip()
        if repo.rev and rev_sha == new_head:
            detail = f"{head_sha[:7]} → {new_head[:7]} (matches manifest rev)"
        else:
            detail = f"{head_sha[:7]} → {new_head[:7]} (manifest rev now stale; run 'rt snapshot')"
        if detached:
            detail = f"attached to {repo.branch}; " + detail
    return FastForwardResult(repo, "advanced", detail=detail)


def fast_forward(
    *,
    names: list[str] | None = None,
    jobs: int = 8,
    verbose: bool = False,
) -> int:
    root = _git_root()
    repos = _filter_repos(load_manifest(root), names)
    name_w = max((len(r.name) for r in repos), default=10)

    emit(f"{COLORS['bold']}Fast-forwarding {len(repos)} repo(s)…{RESET}")

    results: list[FastForwardResult] = []
    interrupted = False
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as executor:
        futures = {
            executor.submit(_fast_forward_one, r, root, name_w, verbose): r
            for r in repos
        }
        try:
            for future in as_completed(futures):
                r = future.result()
                results.append(r)
                if r.verbose_log:
                    emit(f"{COLORS['bold']}{r.repo.name}:{RESET}")
                    for line in r.verbose_log:
                        emit(line)
                if r.label == "advanced":
                    color = "green"
                elif r.label == "up-to-date":
                    color = "dim"
                elif r.label == "skipped":
                    color = "yellow"
                else:
                    color = "red"
                emit(f"  {COLORS[color]}{r.repo.name:<{name_w}}{RESET}  {r.detail}")
        except KeyboardInterrupt:
            emit(
                f"{COLORS['yellow']}Interrupted. Waiting for in-flight git "
                f"operations to finish...{RESET}",
                err=True,
            )
            executor.shutdown(cancel_futures=True)
            interrupted = True

    if interrupted:
        return 130
    # When repos were named explicitly, a `skipped` outcome is a failure of
    # intent — the user asked for those specific repos and we didn't move
    # them. Bulk `rt ffwd` keeps the looser "skips are informational" rule.
    strict = bool(names)
    failed = [
        r for r in results
        if not r.ok or (strict and r.label == "skipped")
    ]
    if failed:
        failed_names = ", ".join(r.repo.name for r in failed)
        emit_error(
            f"fast-forward failed for {len(failed)} of {len(results)} repo(s): {failed_names}"
        )
        return 1
    return 0


# --- Snapshot ------------------------------------------------------------

@dataclass
class SnapshotResult:
    repo: Repo
    label: str          # 'snapshot' | 'unchanged' | 'skipped' | 'failed'
    detail: str
    new_branch: str = ""
    new_rev: str = ""
    ok: bool = True


def _snapshot_one(
    repo: Repo, root: Path, name_w: int = 0, verify_remote: bool = True,
) -> SnapshotResult:
    target = root / repo.path
    if not target.exists():
        return SnapshotResult(repo, "skipped", detail="not synced")
    if not (target / ".git").exists():
        return SnapshotResult(
            repo, "failed",
            detail=f"{target.relative_to(root)} exists but is not a git repo",
            ok=False,
        )

    porcelain = _run_git(["status", "--porcelain"], cwd=target)
    if porcelain.returncode != 0:
        return SnapshotResult(repo, "failed", detail="git status failed", ok=False)
    if porcelain.stdout.strip():
        return SnapshotResult(
            repo, "failed",
            detail="repo is not clean — commit, stash, or clean before snapshotting",
            ok=False,
        )

    cur_branch = _safe_branch(target)
    detached = (not cur_branch) or cur_branch == "HEAD"

    sha = _safe_rev_parse(target, "HEAD")
    if not sha:
        return SnapshotResult(repo, "failed", detail="could not resolve HEAD", ok=False)

    if verify_remote:
        emit(f"  {COLORS['cyan']}{repo.name:<{name_w}}{RESET}  fetching")
        try:
            _check_git(["fetch", "origin"], cwd=target)
        except GitError as e:
            return SnapshotResult(
                repo, "failed", detail=f"fetch: {e.error_line}", ok=False,
            )

    if detached:
        # Detached HEAD is allowed iff it exactly matches `origin/<repo.branch>`.
        # Common after a force-push: user does `git fetch && git checkout
        # origin/<branch>` and wants to pin to the new tip. The manifest already
        # names the branch, so adopt it as the snapshot's branch.
        origin_ref = f"origin/{repo.branch}"
        if _run_git(["rev-parse", "--verify", origin_ref], cwd=target).returncode != 0:
            return SnapshotResult(
                repo, "failed",
                detail=(
                    f"detached HEAD and no remote branch {origin_ref} to anchor to "
                    f"— checkout a branch before snapshotting"
                ),
                ok=False,
            )
        origin_sha = _safe_rev_parse(target, origin_ref)
        if sha != origin_sha:
            return SnapshotResult(
                repo, "failed",
                detail=(
                    f"detached HEAD at {sha[:7]} does not match {origin_ref} @ "
                    f"{origin_sha[:7]} — checkout a branch before snapshotting"
                ),
                ok=False,
            )
        cur_branch = repo.branch
    elif verify_remote:
        origin_ref = f"origin/{cur_branch}"
        if _run_git(["rev-parse", "--verify", origin_ref], cwd=target).returncode != 0:
            return SnapshotResult(
                repo, "failed",
                detail=(
                    f"no remote branch {origin_ref} — push it first "
                    f"(or pass --no-verify-remote)"
                ),
                ok=False,
            )
        res = _run_git(["merge-base", "--is-ancestor", sha, origin_ref], cwd=target)
        if res.returncode != 0:
            return SnapshotResult(
                repo, "failed",
                detail=(
                    f"{sha[:7]} not contained in {origin_ref} — push it first "
                    f"(or pass --no-verify-remote)"
                ),
                ok=False,
            )

    if sha == repo.rev and cur_branch == repo.branch:
        return SnapshotResult(
            repo, "unchanged",
            detail=f"already pinned @ {sha[:7]} ({cur_branch})",
            new_branch=cur_branch, new_rev=sha,
        )
    prev = f"{_short(repo.rev)} ({repo.branch})" if repo.rev else f"(unpinned, {repo.branch})"
    return SnapshotResult(
        repo, "snapshot",
        detail=f"{prev} → {sha[:7]} ({cur_branch})",
        new_branch=cur_branch, new_rev=sha,
    )


_REPO_HEADER_RE = re.compile(r"^\s*\[\[\s*repo\s*\]\]")
_TABLE_HEADER_RE = re.compile(r"^\s*\[")


def _key_line_re(key: str) -> re.Pattern[str]:
    return re.compile(
        rf'^(?P<lead>\s*{re.escape(key)}\s*=\s*)(?P<quote>["\'])[^"\']*(?P=quote)(?P<tail>.*)$'
    )


def _rewrite_manifest_keys(
    manifest_path: Path, updates_by_index: dict[int, dict[str, str]],
) -> None:
    """Rewrite `<key> = "..."` values in each [[repo]] block by source-order index.

    Preserves comments, whitespace, key order, and the original quote style.
    Keys not yet present in the block are *inserted* before any trailing
    blank lines of that block. Indices not present in the mapping are left
    untouched.
    """
    text = manifest_path.read_text()
    lines = text.splitlines(keepends=True)

    all_keys = {k for d in updates_by_index.values() for k in d}
    key_res = {k: _key_line_re(k) for k in all_keys}

    out: list[str] = []
    block_index = -1
    written: dict[int, set[str]] = {}

    def flush_missing(idx: int) -> None:
        """Insert any not-yet-written keys for block `idx` before any trailing
        blank lines that have already been emitted to `out`."""
        if idx < 0 or idx not in updates_by_index:
            return
        block_updates = updates_by_index[idx]
        block_written = written.get(idx, set())
        missing = [(k, v) for k, v in block_updates.items() if k not in block_written]
        if not missing:
            return
        trailing: list[str] = []
        while out and out[-1].strip() == "":
            trailing.append(out.pop())
        for key, value in missing:
            out.append(f'{key} = "{value}"\n')
            block_written.add(key)
        out.extend(reversed(trailing))

    for line in lines:
        if _REPO_HEADER_RE.match(line):
            flush_missing(block_index)
            block_index += 1
            written[block_index] = set()
            out.append(line)
            continue
        if _TABLE_HEADER_RE.match(line):
            flush_missing(block_index)
            block_index = -1
            out.append(line)
            continue
        if block_index >= 0 and block_index in updates_by_index:
            block_updates = updates_by_index[block_index]
            block_written = written[block_index]
            replaced = False
            for key, new_value in block_updates.items():
                if key in block_written:
                    continue
                m = key_res[key].match(line)
                if m:
                    q = m.group("quote")
                    new_line = f"{m.group('lead')}{q}{new_value}{q}{m.group('tail')}"
                    if line.endswith("\n") and not new_line.endswith("\n"):
                        new_line += "\n"
                    out.append(new_line)
                    block_written.add(key)
                    replaced = True
                    break
            if replaced:
                continue
        out.append(line)

    flush_missing(block_index)

    manifest_path.write_text("".join(out))


def _apply_manifest_updates(
    root: Path, all_repos: tuple[Repo, ...], updates_by_name: dict[str, dict[str, str]],
) -> int:
    """Rewrite manifest keys for the named repos; returns the number of repos
    changed. Each manifest file numbers its own [[repo]] blocks, so a repo's
    index is taken within its source file, which is what
    `_rewrite_manifest_keys` addresses. `all_repos` is in load order, so the
    running count per source gives that index. Not thread-safe: call it once,
    on the main thread, after any worker pool has joined."""
    per_source: dict[str, dict[int, dict[str, str]]] = {}
    seen_in_source: dict[str, int] = {}
    for repo in all_repos:
        source = repo.source or MANIFEST_NAME
        index = seen_in_source.get(source, 0)
        seen_in_source[source] = index + 1
        changes = updates_by_name.get(repo.name)
        if changes:
            per_source.setdefault(source, {})[index] = changes
    if not per_source:
        return 0
    for source, updates in per_source.items():
        _rewrite_manifest_keys(root / source, updates)
    return sum(len(updates) for updates in per_source.values())


def snapshot(
    *,
    names: list[str] | None = None,
    verify_remote: bool = True,
    jobs: int = 8,
) -> int:
    root = _git_root()
    all_repos = load_manifest(root)
    repos = _filter_repos(all_repos, names)
    name_w = max((len(r.name) for r in repos), default=10)

    emit(f"{COLORS['bold']}Snapshotting {len(repos)} repo(s)…{RESET}")

    results: list[SnapshotResult] = []
    interrupted = False
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as executor:
        futures = {
            executor.submit(_snapshot_one, r, root, name_w, verify_remote): r
            for r in repos
        }
        try:
            for future in as_completed(futures):
                r = future.result()
                results.append(r)
                if r.label == "snapshot":
                    color = "green"
                elif r.label == "unchanged":
                    color = "dim"
                elif r.label == "skipped":
                    color = "yellow"
                else:
                    color = "red"
                emit(f"  {COLORS[color]}{r.repo.name:<{name_w}}{RESET}  {r.detail}")
        except KeyboardInterrupt:
            emit(
                f"{COLORS['yellow']}Interrupted. Waiting for in-flight git "
                f"operations to finish...{RESET}",
                err=True,
            )
            executor.shutdown(cancel_futures=True)
            interrupted = True

    if interrupted:
        return 130
    if not all(r.ok for r in results):
        emit_error("aborting; manifest not modified")
        return 1

    updates_by_name: dict[str, dict[str, str]] = {}
    for r in results:
        if r.label != "snapshot":
            continue
        changes: dict[str, str] = {}
        if r.new_branch and r.new_branch != r.repo.branch:
            changes["branch"] = r.new_branch
        if r.new_rev and r.new_rev != r.repo.rev:
            changes["rev"] = r.new_rev
        if changes:
            updates_by_name[r.repo.name] = changes

    changed = _apply_manifest_updates(root, all_repos, updates_by_name)
    if not changed:
        emit(f"{COLORS['dim']}manifest unchanged{RESET}")
        return 0
    emit(f"{COLORS['bold']}Updated manifest ({changed} repo(s)){RESET}")
    return 0


# --- Bump ----------------------------------------------------------------
#
# `bump` answers the question a scheduled "propose child updates" CI job
# asks: is the manifest pin behind the tip of its branch, is the move a
# fast-forward, what changed, and can the manifest please be bumped for me.
# It is deliberately free of any CI/forge knowledge — no branches, no commit
# messages, no pushing; the caller does that with the manifest we rewrote.

# The --json document is a compatibility surface (CI parses it) and carries no
# version marker on purpose: a marker only buys detection, not migration, and
# it is worthless here because consumers vendor this script alongside the code
# that parses it, so the two never drift. The contract instead is that changes
# are *additive* — new keys only, consumers ignore what they don't know. If the
# document ever needs a genuinely different shape, add a new flag (as git did
# with `--porcelain=v2`) so deployed pipelines keep working unchanged.

_NO_LOCAL_REV_NOTE = "manifest rev not present locally; log unavailable"
_UNPINNED_NOTE = "no manifest rev pinned; log unavailable"

# Preflight skips that only guard the *advance* — moving HEAD, or moving a local
# branch label. bump looks past them and asks its own questions first,
# because either can be reported for a repo it should still act on:
#   - an upstream rebase explains them (a freshly-synced clone sitting on the
#     now-orphaned pin looks exactly like one carrying local commits), and
#     `diverged` is the useful verdict;
#   - a clone already at the branch tip needs no advance at all, so the guard
#     protects nothing and must not block the manifest bump.
_ADVANCE_GUARD_REASONS = frozenset({"ahead", "local-branch-ahead"})


@dataclass
class BumpResult:
    repo: Repo
    label: str          # 'updated' | 'up-to-date' | 'diverged' | 'skipped' | 'failed'
    detail: str = ""    # skip / failure / divergence reason ('' for progress)
    ok: bool = True
    old_rev: str = ""   # manifest rev as written (may be a tag or branch name)
    old_sha: str = ""   # ... resolved, when it resolves locally
    new_rev: str = ""   # origin/<branch> tip
    count: int | None = None
    log: list[tuple[str, str]] = field(default_factory=list)
    truncated: int = 0
    note: str = ""
    verbose_log: list[str] = field(default_factory=list)


def _resolve_pin(target: Path, rev: str) -> str:
    """Resolve the manifest `rev` in the clone, fetching it by name if it isn't
    present. A pin can be missing from a `--depth` clone (it sits below the
    shallow window) or after an upstream rebase pushed it deeper; `git fetch
    origin <rev>` recovers it as long as it is still reachable from some remote
    ref, which is what lets the ancestry check below be authoritative. Returns
    "" when the object is genuinely unavailable."""
    sha = _safe_rev_parse(target, f"{rev}^{{commit}}")
    if sha:
        return sha
    if _run_git(["fetch", "origin", rev], cwd=target).returncode != 0:
        return ""
    return _safe_rev_parse(target, f"{rev}^{{commit}}")


def _commit_log(
    target: Path, old_sha: str, new_sha: str, limit: int,
) -> tuple[list[tuple[str, str]], int, int | None]:
    """(log entries, commits not listed, total count) for `old_sha..new_sha`.
    `limit` 0 means unlimited. Count is None when git can't produce it."""
    rng = f"{old_sha}..{new_sha}"
    count: int | None = None
    res = _run_git(["rev-list", "--count", rng], cwd=target)
    if res.returncode == 0:
        try:
            count = int(res.stdout.strip())
        except ValueError:
            count = None

    args = ["log", "--pretty=format:%H%x09%s"]
    if limit > 0:
        args.append(f"--max-count={limit}")
    args.append(rng)
    res = _run_git(args, cwd=target)
    entries: list[tuple[str, str]] = []
    if res.returncode == 0:
        for line in res.stdout.splitlines():
            if not line:
                continue
            sha, _, subject = line.partition("\t")
            entries.append((sha, subject))
    truncated = max(0, (count if count is not None else len(entries)) - len(entries))
    return entries, truncated, count


def _bump_one(
    repo: Repo,
    root: Path,
    *,
    name_w: int = 0,
    verbose: bool = False,
    dry_run: bool = False,
    log_limit: int = 20,
    progress: bool = True,
) -> BumpResult:
    log_entries: _GitLogEntries | None = [] if verbose else None
    _git_log_local.entries = log_entries
    try:
        result = _bump_one_inner(
            repo, root, name_w, dry_run=dry_run, log_limit=log_limit, progress=progress,
        )
    finally:
        _git_log_local.entries = None
    if verbose and log_entries is not None:
        result.verbose_log = _format_git_log(log_entries)
    return result


def _bump_one_inner(
    repo: Repo,
    root: Path,
    name_w: int,
    *,
    dry_run: bool,
    log_limit: int,
    progress: bool,
) -> BumpResult:
    target = root / repo.path

    def report_fetch() -> None:
        emit(f"  {COLORS['cyan']}{repo.name:<{name_w}}{RESET}  fetching")

    pre = _ff_preflight(repo, root, on_fetch=report_fetch if progress else None)
    held_skip = (
        not pre.ok and pre.reason in _ADVANCE_GUARD_REASONS and bool(pre.target_sha)
    )
    if not pre.ok and not held_skip:
        return BumpResult(
            repo, pre.label, detail=pre.detail, ok=pre.label != "failed",
            old_rev=repo.rev,
        )

    tip = pre.target_sha
    old_sha = _resolve_pin(target, repo.rev) if repo.rev else ""
    note = ""
    if not repo.rev:
        note = _UNPINNED_NOTE
    elif not old_sha:
        note = _NO_LOCAL_REV_NOTE

    if old_sha and old_sha != tip and _run_git(
        ["merge-base", "--is-ancestor", old_sha, tip], cwd=target,
    ).returncode != 0:
        # Upstream rebased or force-pushed away from the pin: the move isn't a
        # fast-forward, so neither the clone nor the manifest is touched.
        return BumpResult(
            repo, "diverged",
            detail=(
                f"pinned {_short(repo.rev)} is not an ancestor of "
                f"origin/{repo.branch} @ {tip[:7]} — upstream rebased; "
                f"resolve by hand"
            ),
            ok=False, old_rev=repo.rev, old_sha=old_sha, new_rev=tip,
        )
    if held_skip and pre.head_sha != tip:
        # The pin is fine and the clone genuinely has to move to reach the tip,
        # so the guard stands: local work would be lost. (When HEAD is already
        # at the tip there is nothing to move, and the guard is moot — fall
        # through and bump the manifest without touching the checkout.)
        return BumpResult(
            repo, "skipped", detail=pre.detail,
            old_rev=repo.rev, old_sha=old_sha, new_rev=tip,
        )

    if old_sha and old_sha == tip:
        label = "up-to-date"
        count: int | None = 0
        log: list[tuple[str, str]] = []
        truncated = 0
    else:
        label = "updated"
        if old_sha:
            log, truncated, count = _commit_log(target, old_sha, tip, log_limit)
        else:
            log, truncated, count = [], 0, None

    # Both `updated` and `up-to-date` leave the clone on the branch tip: an
    # up-to-date pin can still sit ahead of a clone nobody has synced lately.
    if not dry_run and pre.head_sha != tip:
        failure = _ff_advance(repo, target, detached=pre.detached)
        if failure:
            return BumpResult(
                repo, "failed", detail=failure, ok=False,
                old_rev=repo.rev, old_sha=old_sha, new_rev=tip,
            )

    return BumpResult(
        repo, label, old_rev=repo.rev, old_sha=old_sha, new_rev=tip,
        count=count, log=log, truncated=truncated, note=note,
    )


def _bump_row(r: BumpResult, *, dry_run: bool) -> str:
    """The human-readable detail cell for one repo."""
    if r.label == "updated":
        old = _short(r.old_rev) if r.old_rev else "unpinned"
        if r.count is None:
            bits = "count unknown"
        else:
            bits = f"{r.count} commit(s)"
        if dry_run:
            bits += ", not applied"
        return f"{old} → {r.new_rev[:7]} ({bits})"
    if r.label == "up-to-date":
        return f"already pinned at {r.new_rev[:7]}"
    return r.detail


def _emit_bump(r: BumpResult, name_w: int, *, dry_run: bool) -> None:
    if r.verbose_log:
        emit(f"{COLORS['bold']}{r.repo.name}:{RESET}")
        for line in r.verbose_log:
            emit(line)
    color = {
        "updated": "green",
        "up-to-date": "dim",
        "skipped": "yellow",
    }.get(r.label, "red")
    emit(f"  {COLORS[color]}{r.repo.name:<{name_w}}{RESET}  {_bump_row(r, dry_run=dry_run)}")
    if r.note:
        emit(f"      {COLORS['dim']}{r.note}{RESET}")
    for sha, subject in r.log:
        emit(f"      {sha[:7]}  {subject}")
    if r.truncated:
        emit(f"      {COLORS['dim']}… and {r.truncated} more{RESET}")


def _bump_json(
    results: list[BumpResult], *, dry_run: bool, manifest_updated: int,
) -> str:
    """The `--json` document: one object for the whole run, repos in manifest
    order. Every documented key is always present; absent values are null.
    Additive changes only — see the section comment above."""
    return json.dumps(
        {
            "dry_run": dry_run,
            "manifest_updated": manifest_updated,
            "repos": [
                {
                    "name": r.repo.name,
                    "path": r.repo.path,
                    "branch": r.repo.branch,
                    "status": r.label,
                    "old": r.old_rev or None,
                    "old_sha": r.old_sha or None,
                    "new": r.new_rev or None,
                    "count": r.count,
                    "detail": r.detail or None,
                    "note": r.note or None,
                    "truncated": r.truncated,
                    "log": [{"sha": sha, "subject": subject} for sha, subject in r.log],
                }
                for r in results
            ],
        },
        indent=2,
    )


def bump(
    *,
    names: list[str] | None = None,
    dry_run: bool = False,
    as_json: bool = False,
    log_limit: int = 20,
    jobs: int = 8,
    verbose: bool = False,
) -> int:
    """Advance each child's manifest pin to the tip of its manifest branch when
    that move is a safe fast-forward, reporting what changed.

    Checkout-only in spirit: children are fetched and fast-forwarded (never
    cloned — that's `rt sync`), and the manifest `rev` is rewritten for every
    repo that moved. A pin that isn't an ancestor of the branch tip is reported
    `diverged` and left alone."""
    if as_json and verbose:
        raise RepoToolError("--json and --verbose are mutually exclusive")
    if log_limit < 0:
        raise RepoToolError("--log-limit must be 0 (unlimited) or a positive count")

    root = _git_root()
    all_repos = load_manifest(root)
    repos = _filter_repos(all_repos, names, root=root, existing_only=True)
    if not repos:
        emit_error(
            "no synced repos to bump; run 'rt sync' first or name repos explicitly"
        )
        return 1

    name_w = max((len(r.name) for r in repos), default=10)
    if not as_json:
        emit(f"{COLORS['bold']}Bumping {len(repos)} repo(s)…{RESET}")

    by_name: dict[str, BumpResult] = {}
    interrupted = False
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as executor:
        futures = {
            executor.submit(
                _bump_one, r, root, name_w=name_w, verbose=verbose,
                dry_run=dry_run, log_limit=log_limit, progress=not as_json,
            ): r
            for r in repos
        }
        try:
            for future in as_completed(futures):
                r = future.result()
                by_name[r.repo.name] = r
                if not as_json:
                    _emit_bump(r, name_w, dry_run=dry_run)
        except KeyboardInterrupt:
            emit(
                f"{COLORS['yellow']}Interrupted. Waiting for in-flight git "
                f"operations to finish...{RESET}",
                err=True,
            )
            executor.shutdown(cancel_futures=True)
            interrupted = True

    if interrupted:
        return 130

    # Print/report in manifest order, not completion order.
    results = [by_name[r.name] for r in repos if r.name in by_name]

    # Only `rev` is rewritten: unlike `snapshot`, which adopts whatever branch
    # the user checked out, here the manifest `branch` is the *input* that says
    # which tip to follow, so it is never rewritten. A loose (branch-name) rev
    # that already resolves to the tip stays loose — use `rt snapshot` to pin it.
    updates = {
        r.repo.name: {"rev": r.new_rev}
        for r in results
        if r.label == "updated" and r.new_rev and r.new_rev != r.repo.rev
    }
    changed = 0
    write_error = ""
    if updates and not dry_run:
        try:
            changed = _apply_manifest_updates(root, all_repos, updates)
        except OSError as e:
            write_error = f"could not rewrite the manifest: {e}"

    if as_json:
        emit(_bump_json(results, dry_run=dry_run, manifest_updated=changed))
    elif dry_run:
        emit(
            f"{COLORS['dim']}{len(results)} repo(s) checked; manifest not "
            f"modified (--dry-run){RESET}"
        )
    elif changed:
        emit(
            f"{COLORS['bold']}Updated manifest ({changed} repo(s)){RESET}"
        )
    else:
        emit(f"{COLORS['dim']}manifest unchanged{RESET}")

    if write_error:
        emit_error(write_error)

    # Same named-strict rule as fast-forward: in bulk mode a skip is
    # informational, but a repo the user named and we didn't move is a failure
    # of intent.
    strict = bool(names)
    failed = [r for r in results if not r.ok or (strict and r.label == "skipped")]
    if failed:
        failed_names = ", ".join(r.repo.name for r in failed)
        emit_error(
            f"bump failed for {len(failed)} of {len(results)} repo(s): {failed_names}"
        )
    return 1 if (failed or write_error) else 0


# --- Check ---------------------------------------------------------------

@dataclass
class CheckResult:
    repo: Repo
    label: str          # 'ok' | 'unverified' | 'failed'
    detail: str
    ok: bool = True
    verbose_log: list[str] = field(default_factory=list)


def _parse_ls_remote(stdout: str) -> dict[str, str]:
    """Parse `git ls-remote` output into a {ref: sha} map."""
    refs: dict[str, str] = {}
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            sha, ref = parts
            refs[ref] = sha
    return refs


def _check_one(repo: Repo, verbose: bool) -> CheckResult:
    log_entries: _GitLogEntries | None = [] if verbose else None
    _git_log_local.entries = log_entries
    try:
        result = _check_one_inner(repo)
    finally:
        _git_log_local.entries = None
    if verbose and log_entries is not None:
        result.verbose_log = _format_git_log(log_entries)
    return result


def _check_one_inner(repo: Repo) -> CheckResult:
    """Contact `repo`'s remote (no clone) and confirm branch + rev exist.

    One `git ls-remote` round-trip lists every advertised ref: success proves
    the URL is reachable and we're authorized, and branch/rev existence is then
    checked locally against those refs.
    """
    res = _run_git(["ls-remote", repo.url])
    if res.returncode != 0:
        err = GitError(["ls-remote", repo.url], res.returncode, res.stdout, res.stderr)
        return CheckResult(repo, "failed", f"unreachable: {err.error_line}", ok=False)

    refs = _parse_ls_remote(res.stdout)
    heads = {r[len("refs/heads/"):] for r in refs if r.startswith("refs/heads/")}
    # A tag's peeled `^{}` entry names the same tag; strip it before comparing.
    tags = {
        r[len("refs/tags/"):].removesuffix("^{}")
        for r in refs if r.startswith("refs/tags/")
    }

    if repo.branch not in heads:
        return CheckResult(
            repo, "failed", f"branch {repo.branch!r} not on remote", ok=False,
        )

    rev = repo.rev
    if not rev:
        return CheckResult(
            repo, "ok", f"reachable; branch {repo.branch!r} present (unpinned)",
        )
    if _SHA_RE.match(rev):
        # A raw SHA is only confirmable via ls-remote when it's an advertised
        # ref tip; a rev pinned below the branch tip (the common case) can't be
        # verified without fetching, so report it as unverified rather than a
        # failure.
        tip = refs.get(f"refs/heads/{repo.branch}", "")
        if tip and (tip == rev or tip.startswith(rev)):
            return CheckResult(
                repo, "ok", f"reachable; rev at tip of {repo.branch!r}",
            )
        return CheckResult(
            repo, "unverified",
            f"reachable; branch {repo.branch!r} present; "
            f"rev {_short(rev)} is a SHA below the tip (needs fetch to verify)",
        )
    # Tag or branch-name rev — verifiable directly against the advertised refs.
    if rev in heads or rev in tags:
        kind = "branch" if rev in heads else "tag"
        return CheckResult(
            repo, "ok", f"reachable; branch + rev ({kind} {rev!r}) present",
        )
    return CheckResult(
        repo, "failed",
        f"rev {rev!r} not found as a branch or tag on remote", ok=False,
    )


def check(
    *,
    names: list[str] | None = None,
    jobs: int = 8,
    verbose: bool = False,
    https: bool = False,
) -> int:
    """Validate the manifest and contact each remote without cloning.

    A CI preflight: `load_manifest` performs full structural validation, then
    every declared repo is checked over the network (independent of what's
    cloned on disk). Nothing is fetched or written.
    """
    root = _git_root()
    all_repos = load_manifest(root)
    repos = _filter_repos(all_repos, names)
    # Repos marked `check = false` (e.g. private repos CI can't reach) are
    # skipped in bulk mode; naming a repo explicitly overrides the exclusion.
    if names:
        to_check = list(repos)
        excluded: list[Repo] = []
    else:
        to_check = [r for r in repos if r.check]
        excluded = [r for r in repos if not r.check]
    if https:
        to_check = [_dc_replace(r, url=_to_https(r.url)) for r in to_check]
    name_w = max((len(r.name) for r in repos), default=10)

    suffix = ""
    if excluded:
        suffix = f" {COLORS['dim']}(skipping {len(excluded)} with check = false){RESET}"
    emit(f"{COLORS['bold']}Checking {len(to_check)} repo(s)…{RESET}{suffix}")

    results: list[CheckResult] = []
    interrupted = False
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as executor:
        futures = {executor.submit(_check_one, r, verbose): r for r in to_check}
        try:
            for future in as_completed(futures):
                r = future.result()
                results.append(r)
                if r.verbose_log:
                    emit(f"{COLORS['bold']}{r.repo.name}:{RESET}")
                    for line in r.verbose_log:
                        emit(line)
                color = "red" if not r.ok else ("yellow" if r.label == "unverified" else "green")
                emit(f"  {COLORS[color]}{r.repo.name:<{name_w}}{RESET}  {r.detail}")
        except KeyboardInterrupt:
            emit(
                f"{COLORS['yellow']}Interrupted. Waiting for in-flight git "
                f"operations to finish...{RESET}",
                err=True,
            )
            executor.shutdown(cancel_futures=True)
            interrupted = True

    for ex in excluded:
        emit(f"  {COLORS['dim']}{ex.name:<{name_w}}  excluded (check = false){RESET}")

    if interrupted:
        return 130
    failed = [r for r in results if not r.ok]
    if failed:
        failed_names = ", ".join(r.repo.name for r in failed)
        emit_error(
            f"check failed for {len(failed)} of {len(results)} repo(s): {failed_names}"
        )
        return 1
    unverified = sum(1 for r in results if r.label == "unverified")
    if unverified:
        emit(
            f"{COLORS['dim']}{unverified} repo(s) pin a SHA rev that can't be "
            f"verified without fetching; branch and reachability are OK.{RESET}"
        )
    return 0


# --- Show changes --------------------------------------------------------

def _show_changes_one(repo: Repo, root: Path, name_w: int, shortlog: bool = False) -> None:
    """Print the changes between a child's manifest rev and its HEAD — a compact
    per-commit log, or `git shortlog --no-merges` output when `shortlog`."""
    target = root / repo.path
    label = f"{COLORS['cyan']}{repo.name:<{name_w}}{RESET}"
    if not target.exists():
        emit(f"  {label}  {COLORS['dim']}not synced{RESET}")
        return
    if not (target / ".git").exists():
        emit(f"  {label}  {COLORS['red']}not a git repo{RESET}")
        return

    head_sha = _safe_rev_parse(target, "HEAD")
    if not head_sha:
        emit(f"  {label}  {COLORS['red']}could not resolve HEAD{RESET}")
        return
    if not repo.rev:
        emit(f"  {label}  {COLORS['dim']}unpinned — no manifest rev to compare{RESET}")
        return

    rev_sha = _safe_rev_parse(target, f"{repo.rev}^{{commit}}")
    if not rev_sha:
        emit(
            f"  {label}  {COLORS['yellow']}manifest rev {_short(repo.rev)} "
            f"does not resolve locally{RESET}"
        )
        return
    if rev_sha == head_sha:
        emit(f"  {label}  {COLORS['green']}in-sync at {head_sha[:7]}{RESET}")
        return

    behind, ahead = _ahead_behind(target, rev_sha)
    parts = []
    if ahead:
        parts.append(f"{ahead} ahead")
    if behind:
        parts.append(f"{behind} behind")
    emit(f"  {label}  {rev_sha[:7]}..{head_sha[:7]}  ({', '.join(parts)})")

    if shortlog:
        _emit_shortlog(target, "ahead", f"{rev_sha}..HEAD", show_label=bool(ahead and behind))
        _emit_shortlog(target, "behind", f"HEAD..{rev_sha}", show_label=bool(ahead and behind))
        return

    # One log over the symmetric difference: `>` marks commits on HEAD but not
    # the manifest rev (ahead), `<` marks commits on the manifest rev but not
    # HEAD (behind). Covers ahead / behind / diverged in a single call.
    # `%al` is the author email's local-part (before @) — the domain is dropped.
    res = _run_git(
        ["log", "--left-right", "--pretty=format:%m%x09%h%x09%al%x09%s", f"{rev_sha}...HEAD"],
        cwd=target,
    )
    if res.returncode != 0:
        emit(f"  {label}  {COLORS['red']}git log failed{RESET}")
        return
    for ln in (x for x in res.stdout.splitlines() if x):
        marker, sha, author, subject = ln.split("\t", 3)
        sign = f"{COLORS['green']}+{RESET}" if marker == ">" else f"{COLORS['red']}-{RESET}"
        emit(f"      {sign} {sha}  {COLORS['dim']}{author}{RESET}  {subject}")


def _ahead_behind(target: Path, rev_sha: str) -> tuple[int, int]:
    """(behind, ahead) commit counts of HEAD relative to `rev_sha`."""
    res = _run_git(
        ["rev-list", "--left-right", "--count", f"{rev_sha}...HEAD"], cwd=target,
    )
    if res.returncode != 0:
        return (0, 0)
    try:
        behind, ahead = (int(n) for n in res.stdout.split())
    except ValueError:
        return (0, 0)
    return (behind, ahead)


def _emit_shortlog(target: Path, tag: str, rev_range: str, *, show_label: bool) -> None:
    """Emit `git shortlog --no-merges` for `rev_range`, indented under the repo."""
    res = _run_git(["shortlog", "--no-merges", rev_range], cwd=target)
    if res.returncode != 0:
        return
    body = res.stdout.rstrip("\n")
    if not body:
        return
    if show_label:
        emit(f"    {COLORS['dim']}{tag}:{RESET}")
    for ln in body.splitlines():
        emit(f"    {ln}")


def show_changes(*, names: list[str] | None = None, shortlog: bool = False) -> int:
    """Show the changes between each child's manifest rev and its HEAD — a
    compact per-commit log, or `git shortlog --no-merges` output if `shortlog`.

    Defaults to already-cloned children; name repos to narrow (a named-but-
    uncloned repo prints a `not synced` line rather than failing)."""
    root = _git_root()
    all_repos = load_manifest(root)
    repos = _filter_repos(all_repos, names, root=root, existing_only=True)
    if not repos:
        emit_error(
            "no synced repos to show; run 'rt sync' first or name repos explicitly"
        )
        return 1

    name_w = max((len(r.name) for r in repos), default=10)
    emit(f"{COLORS['bold']}Showing changes for {len(repos)} repo(s)…{RESET}")
    for r in repos:
        _show_changes_one(r, root, name_w, shortlog=shortlog)
    return 0


# --- CLI -----------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rt",
        description="repotool — lightweight meta-repo coordinator with a TOML manifest.",
    )
    p.add_argument("--version", action="version", version=f"rt {__version__}")
    sub = p.add_subparsers(dest="cmd")

    sp_sync = sub.add_parser("sync", help="clone/align children to the manifest")
    sp_sync.add_argument("names", nargs="*", help="repo name(s) or path(s) to sync; default: existing clones")
    sp_sync.add_argument("-a", "--all", dest="include_missing", action="store_true",
                         help="include manifest repos not yet cloned (clones them)")
    sp_sync.add_argument("-j", "--jobs", type=int, default=8, help="max concurrent git operations (default: 8)")
    sp_sync.add_argument("-v", "--verbose", action="store_true", help="print full git output per repo")
    sp_sync.add_argument("--reference", metavar="PATH", help="pass --reference PATH to git clone (single-repo sync only)")
    sp_sync.add_argument("--depth", type=int, metavar="N",
                         help="pass --depth N to git clone for shallow clones "
                              "(CI use; first-clone only, disables --reference / alternates)")
    sp_sync.add_argument("--https", action="store_true",
                         help="rewrite all repo URLs to https:// (CI use when SSH keys aren't installed)")
    sp_sync.add_argument("--rev", action="append", default=[], metavar="NAME=REF",
                         help="check out REF (SHA, branch, tag, or full ref) for repo NAME in place of "
                              "its manifest rev, for this run only; repeatable. Also read from the "
                              "RT_REVS environment variable (the CLI wins). Only sync honours it.")

    sp_status = sub.add_parser("status", help="show manifest/upstream/dirty state per child (informational)")
    sp_status.add_argument("names", nargs="*", help="repo name(s) or path(s) to show; default: all")

    sp_reset = sub.add_parser("reset", help="destructively realign children to the manifest")
    sp_reset.add_argument("names", nargs="*", help="repo name(s) or path(s) to reset; default: existing clones")
    sp_reset.add_argument("-a", "--all", dest="include_missing", action="store_true",
                          help="include manifest repos not yet cloned (shown as skipped)")
    sp_reset.add_argument("-f", "--force", action="store_true", help="discard local commits ahead of the target")
    sp_reset.add_argument("-j", "--jobs", type=int, default=8, help="max concurrent git operations (default: 8)")
    sp_reset.add_argument("-v", "--verbose", action="store_true", help="print full git output per repo")

    sp_ff = sub.add_parser(
        "fast-forward",
        aliases=["ffwd"],
        help="fast-forward each child's branch to origin/<ref> if safely possible",
    )
    sp_ff.add_argument("names", nargs="*", help="repo name(s) or path(s) to fast-forward; default: all")
    sp_ff.add_argument("-j", "--jobs", type=int, default=8, help="max concurrent git operations (default: 8)")
    sp_ff.add_argument("-v", "--verbose", action="store_true", help="print full git output per repo")

    sp_bump = sub.add_parser(
        "bump",
        help="advance each child's pin to the tip of its manifest branch",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="modifies both the clones and the manifest: fetches and\n"
               "fast-forwards each child, then rewrites the manifest 'rev' for\n"
               "every repo that moved (pass -n to report without touching\n"
               "either). Never clones (run 'rt sync' first) and never proposes a\n"
               "non-fast-forward: a pin the branch tip has diverged from is\n"
               "reported and left alone. A child with submodules = true only has\n"
               "its superproject moved — follow up with 'rt sync' before\n"
               "building it.",
    )
    sp_bump.add_argument("names", nargs="*",
                         help="repo name(s) or path(s) to bump; default: existing clones")
    sp_bump.add_argument("-n", "--dry-run", action="store_true",
                         help="report only: fetch, but don't move the clone or rewrite the manifest")
    sp_bump.add_argument("--json", action="store_true",
                         help="emit one machine-readable JSON document on stdout")
    sp_bump.add_argument("--log-limit", type=int, default=20, metavar="N",
                         help="commit subjects to report per repo (0 = unlimited; default: 20)")
    sp_bump.add_argument("-j", "--jobs", type=int, default=8,
                         help="max concurrent git operations (default: 8)")
    sp_bump.add_argument("-v", "--verbose", action="store_true",
                         help="print full git output per repo")

    sp_check = sub.add_parser(
        "check",
        help="validate the manifest and contact each remote without cloning (CI preflight)",
    )
    sp_check.add_argument("names", nargs="*", help="repo name(s) or path(s) to check; default: all")
    sp_check.add_argument("-j", "--jobs", type=int, default=8, help="max concurrent git operations (default: 8)")
    sp_check.add_argument("-v", "--verbose", action="store_true", help="print full git output per repo")
    sp_check.add_argument("--https", action="store_true",
                          help="rewrite all repo URLs to https:// (CI use when SSH keys aren't installed)")

    sp_show = sub.add_parser(
        "show-changes",
        help="show the commit log between each child's manifest rev and its current HEAD",
    )
    sp_show.add_argument("names", nargs="*", help="repo name(s) or path(s) to show; default: existing clones")
    sp_show.add_argument("-s", "--shortlog", action="store_true",
                         help="show 'git shortlog --no-merges' output instead of per-commit lines")

    sp_snapshot = sub.add_parser(
        "snapshot",
        help="pin each repo's ref to its current HEAD SHA in the manifest",
    )
    sp_snapshot.add_argument("names", nargs="*", help="repo name(s) or path(s) to snapshot; default: all")
    sp_snapshot.add_argument(
        "--no-verify-remote",
        action="store_true",
        help="skip the fetch + check that HEAD is reachable from a remote-tracking branch",
    )
    sp_snapshot.add_argument(
        "-j", "--jobs", type=int, default=8,
        help="max concurrent git operations (default: 8)",
    )

    sp_completions = sub.add_parser(
        "completions",
        help="print a tab-completion script for the given shell",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='enable in your shell\'s rc file:\n'
               '  eval "$(rt completions bash)"   # ~/.bashrc\n'
               '  eval "$(rt completions zsh)"    # ~/.zshrc, after compinit\n'
               '  rt completions fish | source    # ~/.config/fish/config.fish',
    )
    sp_completions.add_argument("shell", choices=["bash", "zsh", "fish"],
                                help="shell to emit a script for")

    return p


# --- Shell completion -------------------------------------------------------
#
# Tab completion delegates to the tool itself: the (static) shell scripts call
# the hidden entry point `rt __complete -- <words...>` and the candidates are
# computed here by introspecting the argparse parser, so new subcommands and
# options are completed without touching the scripts.
#
# Protocol: the shell passes every word of the command line (including the
# program name), with the token being completed last — "" when the cursor
# follows a space. One candidate per stdout line as `value<TAB>help`; a line
# that is exactly `:file` asks the shell to fall back to filename completion.
# Always exits 0 and stays silent on error: a broken TAB is worse than none.

def _subparsers_action(
    p: argparse.ArgumentParser,
) -> argparse._SubParsersAction[argparse.ArgumentParser]:
    for action in p._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise RuntimeError("parser has no subparsers")


def _manifest_repo_candidates(pos_words: list[str]) -> list[tuple[str, str]]:
    """Repo name and path candidates for completion; empty on any failure.

    Paths are accepted by `_filter_repos` as repo identifiers, so offer them
    too (when they differ from the name). A repo already referenced by a
    prior word — by either name or path — is excluded entirely.
    """
    try:
        repos = load_manifest(_git_root())
    except Exception:  # noqa: BLE001 - completion must never break the shell
        return []
    taken = {w.rstrip("/") for w in pos_words}
    out: list[tuple[str, str]] = []
    for r in repos:
        if r.name in taken or r.path in taken:
            continue
        out.append((r.name, ""))
        if r.path != r.name:
            out.append((r.path, ""))
    return out


def _option_completions(p: argparse.ArgumentParser) -> list[tuple[str, str]]:
    return [
        (s, a.help or "")
        for a in p._actions
        if a.option_strings and a.help != argparse.SUPPRESS
        for s in a.option_strings
    ]


def _completion_candidates(words: list[str]) -> tuple[list[tuple[str, str]], bool]:
    """Candidates for the last word in `words` (the program name excluded).

    Returns (candidates, want_files): candidates as (value, help) pairs not
    yet filtered by the current prefix; want_files asks the shell to complete
    filenames instead.
    """
    parser = _build_parser()
    sub = _subparsers_action(parser)
    prefix = words[-1]
    prior = words[:-1]

    cmd = next((w for w in prior if w in sub.choices), None)
    if cmd is None:
        if prefix.startswith("-"):
            return _option_completions(parser), False
        # Subcommand help lives on _ChoicesPseudoAction entries keyed by the
        # canonical name; map via parser identity so aliases share it.
        help_by_parser = {
            id(sub.choices[ca.dest]): ca.help or "" for ca in sub._choices_actions
        }
        return [
            (name, help_by_parser.get(id(sp), "")) for name, sp in sub.choices.items()
        ], False

    sp = sub.choices[cmd]
    options = {s: a for a in sp._actions for s in a.option_strings}

    # Completing the value of the preceding option?
    prev = options.get(prior[-1])
    if prev is not None and prev.nargs != 0:
        if prev.choices is not None:
            return [(str(c), "") for c in prev.choices], False
        if prev.metavar == "PATH":
            return [], True
        return [], False  # e.g. --jobs N: nothing sensible to offer

    if prefix.startswith("-"):
        return _option_completions(sp), False

    # Words after the subcommand already consumed by its positional(s).
    pos_words: list[str] = []
    skip_value = False
    for w in prior[prior.index(cmd) + 1:]:
        if skip_value:
            skip_value = False
        elif w.startswith("-"):
            a = options.get(w)
            skip_value = a is not None and a.nargs != 0
        else:
            pos_words.append(w)

    # Every rt subcommand has at most one positional.
    positionals = [a for a in sp._actions if not a.option_strings]
    if not positionals:
        return [], False
    action = positionals[0]
    if action.choices is not None:
        if pos_words:  # single-value positional already supplied
            return [], False
        return [(str(c), "") for c in action.choices], False
    if action.dest == "names":
        return _manifest_repo_candidates(pos_words), False
    return [], False


def _complete(argv: list[str]) -> int:
    """Handle the hidden `rt __complete -- <words...>` entry point."""
    words = argv[1:] if argv[:1] == ["--"] else argv
    words = words[1:]  # drop the program name
    if not words:
        words = [""]
    try:
        candidates, want_files = _completion_candidates(words)
    except Exception:  # noqa: BLE001 - completion must never break the shell
        return 0
    for value, help_text in candidates:
        if value.startswith(words[-1]):
            emit(f"{value}\t{help_text}" if help_text else value)
    if want_files:
        emit(":file")
    return 0


# The static per-shell scripts printed by `rt completions <shell>`. They only
# implement the __complete protocol above and never need updating when
# commands change. Install with e.g. `eval "$(rt completions bash)"`.

_COMPLETION_SCRIPTS: dict[str, str] = {
    "bash": """\
_rt_complete() {
    local cur="${COMP_WORDS[COMP_CWORD]}" line
    COMPREPLY=()
    while IFS= read -r line; do
        if [[ "$line" == ":file" ]]; then
            compopt -o default 2>/dev/null
        else
            COMPREPLY+=("${line%%$'\\t'*}")
        fi
    done < <(rt __complete -- "${COMP_WORDS[@]:0:COMP_CWORD}" "$cur" 2>/dev/null)
}
complete -F _rt_complete rt
""",
    # Works both eval'd from .zshrc (after compinit) and dropped on $fpath
    # as `_rt`, where the #compdef line makes compinit autoload it.
    "zsh": """\
#compdef rt
_rt() {
    local -a pairs
    local line
    for line in "${(@f)$(rt __complete -- "${(@)words[1,CURRENT]}" 2>/dev/null)}"; do
        [[ -n "$line" ]] || continue
        if [[ "$line" == ":file" ]]; then
            _files
        else
            pairs+=("${line/$'\\t'/:}")
        fi
    done
    (( ${#pairs} )) && _describe 'rt' pairs
}
if [[ ${funcstack[1]} == _rt ]]; then
    _rt "$@"
elif (( ${+functions[compdef]} )); then
    compdef _rt rt
fi
""",
    "fish": """\
function __rt_complete
    set -l ct (commandline -ct)
    rt __complete -- (commandline -opc) "$ct" 2>/dev/null | while read -l line
        if test "$line" = ":file"
            __fish_complete_path "$ct"
        else
            printf '%s\\n' $line
        end
    end
end
complete -c rt -f -a '(__rt_complete)'
""",
}


def completions(*, shell: str) -> int:
    emit(_COMPLETION_SCRIPTS[shell].rstrip("\n"))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "__complete":
        return _complete(argv[1:])
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.cmd == "sync":
            return sync(
                names=args.names, jobs=args.jobs, verbose=args.verbose,
                reference=args.reference, depth=args.depth, https=args.https,
                include_missing=args.include_missing, revs=args.rev,
            )
        if args.cmd == "reset":
            return reset(
                names=args.names, force=args.force, jobs=args.jobs,
                verbose=args.verbose, include_missing=args.include_missing,
            )
        if args.cmd in ("fast-forward", "ffwd"):
            return fast_forward(names=args.names, jobs=args.jobs, verbose=args.verbose)
        if args.cmd == "bump":
            return bump(
                names=args.names, dry_run=args.dry_run, as_json=args.json,
                log_limit=args.log_limit, jobs=args.jobs, verbose=args.verbose,
            )
        if args.cmd == "show-changes":
            return show_changes(names=args.names, shortlog=args.shortlog)
        if args.cmd == "check":
            return check(
                names=args.names, jobs=args.jobs,
                verbose=args.verbose, https=args.https,
            )
        if args.cmd == "snapshot":
            return snapshot(
                names=args.names,
                verify_remote=not args.no_verify_remote,
                jobs=args.jobs,
            )
        if args.cmd == "completions":
            return completions(shell=args.shell)
        # `status` is the default — no subcommand also lands here.
        return status(names=getattr(args, "names", None))
    except RepoToolError as e:
        emit_error(str(e))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

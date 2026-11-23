# How repotool compares

## vs. git submodules

Submodules solve a similar problem but bake the child pointers into the parent's
git tree as gitlink entries, which has well-known ergonomic costs. The main
differences:

- **Pointer state is a plain TOML file**, not gitlink entries in the parent's
  tree. Bumping a child's rev shows up as an ordinary line diff in
  `.repotool.toml`, reviewable in any PR tool, rather than as a
  `Subproject commit abc123…def456` change to a tree object.
- **Branch + rev are pinned together.** Submodules pin a SHA only; the branch
  context is a separate `.gitmodules` setting and not what `git submodule
  update` follows by default. `repotool` keeps both in one entry: `rev` for
  reproducibility, `branch` for what `fast-forward` and `snapshot` operate
  against.
- **Children can be on a branch.** By default `git submodule update` leaves
  each submodule on a detached HEAD. `rt sync` attaches a child to `<branch>`
  when its `rev` is the tip of `origin/<branch>`, and `rt fast-forward`
  attaches it as it advances; a `rev` below the tip still leaves it detached.
- **No submodule commands.** No `git submodule init` or `update --init`, and
  no `--recursive` flags: `rt sync` is the one command that clones and aligns
  the children. Children are independent clones; standard git commands work on
  each child unchanged.
- **Children can already exist on disk.** A previously-cloned working repo at
  the right path is adopted by the next `rt sync`; you don't have to re-clone
  through submodule machinery.
- **Children you don't need can be left uncloned.** `rt sync` and `rt reset`
  default to operating only on children that are already on disk, so you can
  work on a subset of the manifest without pulling everything down. `rt sync
  --all` clones the rest when you want them, and naming a specific repo (`rt
  sync foo`) always clones it regardless.

The tradeoff: `repotool` is a separate tool and a separate file. If
submodules work well for you, there's no need to switch. If their coupling to
the parent's tree or their commands cause you problems, `repotool` is a smaller
alternative.

## vs. Google's `repo` and Zephyr's `west`

Google's `repo` and Zephyr's `west` solve the same multi-repo problem at a
larger scale, and are good fits for the ecosystems they were built for.
`repotool` targets the smaller case where their structure is more than you
need.

- **The parent is an ordinary git repo.** `repo` and `west` keep the manifest
  in a manifest repository and build a workspace around it (`repo init -u`,
  `west init -m`), with workspace state in `.repo/` or `.west/`. `repotool`'s
  manifest is committed in the parent repo the children are cloned into: no
  init step, no workspace state directory. Clone the parent, `rt sync --all`.
  Each worktree of the parent is an isolated workspace with no extra setup.
- **Pins are edited in place.** `repo manifest -r` and `west manifest
  --freeze` emit a separate pinned manifest; `rt snapshot` and `rt bump`
  rewrite the `rev` lines of the committed manifest, preserving comments, so a
  pin bump is a small reviewable diff to the file everyone already uses.
- **Branch + rev, always.** Every entry names its branch as well as its pin,
  so the pinned SHA keeps its branch context: it's what `fast-forward` and
  `bump` advance along and what `snapshot` checks reachability against.
  `west`'s `revision` is a single field; `repo` has an optional `upstream`
  attribute for the purpose.
- **Children stay on branches.** `repo sync` and `west update` leave projects
  on detached HEADs. `rt sync` attaches HEAD to the branch when the pin is at
  its tip, and `rt fast-forward` re-attaches as it advances.
- **Zero install.** `repo` is a launcher that downloads the rest of the tool;
  `west` is a pip package with dependencies. `repotool.py` is a single
  stdlib-only file that can be vendored into the parent repo, so the tool's
  version is pinned alongside the manifest it reads.
- **Deliberately smaller.** No code-review upload, manifest imports, remotes
  and defaults inheritance, project groups, or extension commands. Partial
  checkouts come from operating on whatever is already cloned rather than from
  groups.

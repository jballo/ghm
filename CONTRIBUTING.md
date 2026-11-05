# Contributing

## Read this first

ghm is a small personal project with limited review time. Opening a PR doesn't oblige anyone to
review or merge it. A PR may be closed, deferred, or reimplemented differently.

Bug fixes, reliability fixes, and performance improvements are the most likely to be merged.
Unrequested features, rewrites, and unrelated cleanup are not.

The project aims to stay small enough that one person can audit all of it. Changes that add
dependencies, shell out, or widen what the tool touches on your Mac need a good reason.

## Start with an issue

- **Bugs:** open an issue with what you ran, what you expected, what happened, and the relevant
  lines from `~/Library/Logs/gh-heatmap.log`.
- **Features:** open an issue (or comment on an existing one) and agree on the approach before you
  write code. The open feature issues are good places to start.
- **Small, obvious fixes** such as typos or a clear one-line bug can go straight to a PR. Say what
  was wrong.

## Workflow

```
your fork: your-branch  →  PR into jballo/ghm:dev  →  PR dev into main (maintainer)
```

1. Fork [jballo/ghm](https://github.com/jballo/ghm) on GitHub.
2. Clone your fork and create a branch from `dev` (see Setup below).
3. Push the branch to your fork and open a PR against **`dev`** on `jballo/ghm`.
4. Once changes on `dev` are ready to ship, the maintainer opens a PR from `dev` into `main`.
   Never open a PR against `main` yourself.

Keep your branch up to date with `upstream/dev` before asking for review.

## One problem per PR

A PR should fix one underlying problem. If you find a second issue along the way, open another
PR. Include the code, `SPEC.md`, and `README.md` changes that problem needs in the same PR.

## Setup

Requires macOS and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/<you>/ghm.git      # your fork
cd ghm
git remote add upstream https://github.com/jballo/ghm.git
git fetch upstream
git switch -c my-change upstream/dev
uv sync --locked
uv run heatmap.py --dry-run --user <your-github-username>   # writes a preview; doesn't touch your wallpaper
```

The preview is written to `~/Library/Application Support/gh-heatmap/preview.png`.

## Before you open a PR

- `uv run python -m py_compile heatmap.py install.py uninstall.py` passes.
- `--dry-run` still produces a correct preview. Attach it to the PR if the change affects rendering.
- If you changed rendering output, bump `STYLE_VERSION` in `heatmap.py`.
- If you changed behaviour, update `SPEC.md` to match.
- If you added or changed a dependency, explain why in the PR and commit the updated `uv.lock`
  (`uv lock`). Don't commit `.venv/`.
- In the PR description, say what you tested and on which macOS version.

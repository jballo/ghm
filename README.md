# gh-heatmap

Your public GitHub contribution graph as your macOS desktop wallpaper: green squares on
black by default, or one of five built-in themes, refreshed every 10 minutes by a per-user
launchd job.
No GitHub token; it reads only `https://github.com/users/<username>/contributions`.
Design contract: [SPEC.md](SPEC.md).

![gh-heatmap as the macOS desktop wallpaper](docs/screenshot.png)

## Install / uninstall

Requires [uv](https://docs.astral.sh/uv/) on your `PATH`.

```sh
python3 install.py --user YOUR_GITHUB_NAME     # optional: --theme nord, --width 40%, --interval 600
python3 uninstall.py
```

`install.py` runs `uv sync --locked`, writes your config, runs the tool once by hand
(this changes your wallpaper and saves the original), then loads the LaunchAgent. It
schedules nothing unless that first run succeeds and the original wallpaper was saved.
`uninstall.py` unloads and deletes the agent, then runs `heatmap.py --uninstall`. That
restores the original wallpaper and deletes the files below (except the empty lock file), holding the lock the
whole time. It deletes nothing while any display still shows a heatmap image. In that
case it names those displays, opens Wallpaper settings, prints your original wallpaper
paths and exits 1. Re-pick a wallpaper there, then run `uninstall.py` again. That is
always safe to repeat. Without `.venv` it deletes nothing and asks you to run
`uv sync --locked` first.

To try it without changing anything:

```sh
uv run heatmap.py --dry-run --user YOUR_GITHUB_NAME
open ~/Library/Application\ Support/gh-heatmap/preview.png
```

## Themes

`dracula`, `gruvbox`, `nord`, `panda` and `tokyo-night`, each built from that palette's
official colours. With no theme set you get the GitHub green look. Pick one when
installing (`install.py --theme nord`) or change it any time:

```sh
uv run heatmap.py --list-themes
uv run heatmap.py --dry-run --theme dracula     # preview only; saves nothing
uv run heatmap.py --theme dracula               # save it and apply it now
uv run heatmap.py --theme default               # back to green
```

Names are case-insensitive. The choice is saved in `config.json`, so the scheduled runs
keep it, and a reinstall keeps it unless you pass a different `--theme`.
Only the desktop you are on changes at once. Other desktops (Spaces) switch the next
time a run happens while you are on them; run `uv run heatmap.py` there to switch now
(during a GitHub backoff it skips, like any other run).

## Size

By default the grid fills about 62% of the screen's width. `--width` and `--height` set the
largest it may be, as a percentage of the screen (10% to 100%, the `%` is optional):

```sh
uv run heatmap.py --dry-run --width 40%          # preview only; saves nothing
uv run heatmap.py --width 40%                    # at most 40% of the screen wide
uv run heatmap.py --height 15%                   # at most 15% of the screen tall
uv run heatmap.py --width 80% --height 40%       # fits inside both
uv run heatmap.py --width default                # forget the saved width
```

The squares stay square, so the grid grows until it reaches either limit. A year of
contributions is about 7.5 times wider than tall, so usually the width wins: `--width 80%
--height 40%` gives a grid about 80% wide and 16% tall. Each flag is saved on its own, so
`--width 80%` keeps a height you saved earlier; a dimension you never set (or set back to
`default`) is unlimited. So `--height` on its own can make the grid bigger than the
default: `--height 25%` hits the screen's width first and runs nearly edge to edge (about
20% tall on a 16:10 screen); pair it with `--width` to keep it smaller. Like the theme, the size is saved in `config.json`, applies to every display
in proportion to its own size, and is kept on a reinstall unless you pass the flag again.

## Other flags

`--force` (skip the ETag and up-to-date checks and re-render on every display;
it still respects a GitHub backoff and still falls back to cached data when offline) and
`--restore` (put the saved wallpaper back; deletes nothing). Exit codes: 0 means OK,
1 means error (including an unknown theme, a bad size or a bad flag), 2 means skipped (no screens, backoff, or another run in progress), and 3
means `--restore`/`--uninstall` found the lock busy.

## Files it creates

```
~/Library/Application Support/gh-heatmap/   config.json, state.json, backup.json,
                                            heatmap-<hash>.png, preview.png
~/Library/Application Support/gh-heatmap.lock   empty lock file; uninstall leaves it on
                                            purpose, so every run shares one lock
~/Library/Logs/gh-heatmap.log               (plus rotated .1-.3, 512 KiB each)
~/Library/Logs/gh-heatmap.launchd.log       launchd stdout/stderr; should stay empty
~/Library/LaunchAgents/local.gh-heatmap.plist
```

Plus `.venv/` in this folder. Nothing outside `$HOME`, no sudo, no shell-rc edits.

## Limitations

- **Current Space only.** macOS has no public API to set the wallpaper on every
  Space, so each screen's *current* Space is updated. Other Spaces, and displays that
  aren't connected, keep the heatmap they last showed. They catch up on the next
  successful run while you are on them (normally within 10 minutes; a GitHub backoff,
  sleep or a failed run can delay it), or at once if you run `uv run heatmap.py` there
  (unless GitHub asked us to back off, or another run is in progress).
  Recent PNGs (up to 50 rendered in the last 14 days, ~32 KB each) are kept so those
  Spaces still have their file; one left alone longer may point at a deleted image
  until a run happens on it.
- **Uninstall restores the Spaces you are on.** Other Spaces can still point at a
  deleted heatmap afterwards; open each one and pick a wallpaper in System Settings.
- Restore is best-effort for static images. A dynamic wallpaper, a slideshow or a
  missing file can't be put back automatically, so the Wallpaper settings pane
  opens and you re-pick it there.
- If you change the wallpaper by hand, the next run (within 10 minutes) sets the
  heatmap again. That is the tool's job. Uninstall to stop it.

## Recovery

The `.venv` interpreter is managed by uv and lives under `~/.local/share/uv/python`. If
it disappears, or `heatmap.py` stops running (check `~/Library/Logs/gh-heatmap.launchd.log`
and `~/Library/Logs/gh-heatmap.log`), run this in the project folder:

```sh
uv sync --locked
python3 install.py --user YOUR_GITHUB_NAME
```

A reinstall never overwrites an existing `backup.json`, so your real original wallpaper
is kept. It also keeps your saved theme and size unless you pass `--theme`, `--width` or
`--height`.

## Auditing

- All the logic is in `heatmap.py`, one file that imports only the standard
  library plus Pillow (for drawing) and PyObjC's AppKit/Foundation (for setting the
  wallpaper). It makes no subprocess calls. Its only network request is the one GET above,
  and it refuses any redirect off `https://github.com/`.
- `install.py` and `uninstall.py` are standard-library only. Their only subprocess
  calls are `uv sync --locked`, `/bin/launchctl bootout|bootstrap`, and
  `.venv/bin/python heatmap.py [--uninstall]`. `uninstall.py` deletes only the plist; the support dir and logs are deleted only by `heatmap.py --uninstall`.
- Dependencies are `pillow` and `pyobjc-framework-Cocoa` (which pulls in `pyobjc-core`).
  `uv.lock` pins every wheel with a sha256 hash, and `uv sync --locked` refuses to run
  if the lock is out of date with `pyproject.toml`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Fork the repo, branch from `dev`, and open PRs against `dev`; `dev` is merged into `main` by the maintainer.

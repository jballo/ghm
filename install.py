#!/usr/bin/env python3
"""Install gh-heatmap for the current user (SPEC §10).

Usage: python3 install.py --user NAME [--mode NAME] [--theme NAME] [--width PCT] [--height PCT] [--interval 600]
                          [--watch-spaces | --no-watch-spaces]

Runs `uv sync --locked`, writes config.json (keeping a saved mode, theme and size unless the flag is given),
does one run by hand (which must succeed
and leave a wallpaper backup), then writes and loads the per-user LaunchAgent. With --watch-spaces it
also loads a second, resident agent that re-applies the graph when you switch Spaces (SPEC §13).
Everything lives under $HOME; no sudo. Uses only the
standard library so it runs on any Python 3.9+ (including macOS's /usr/bin/python3).
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# These paths, the regexes, the size range and the theme names must match heatmap.py
# (`heatmap.py --list-themes`).
USERNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
THEME_NAMES = ("dracula", "gruvbox", "nord", "panda", "tokyo-night")
RESET_THEME = "default"
MODE_GITHUB, MODE_T3CODE = "github", "t3code"
MODES = (MODE_GITHUB, MODE_T3CODE)
SETTING_KEYS = ("mode", "theme", "width", "height")
SIZE_RE = re.compile(r"^([0-9]{1,3})%?$")
SIZE_MIN, SIZE_MAX = 10, 100
RESET_SIZE = "default"
SIZE_KEYS = ("width", "height")
LABEL = "local.gh-heatmap"
PROJECT_DIR = Path(__file__).resolve().parent
VENV_PYTHON = PROJECT_DIR / ".venv" / "bin" / "python"
HEATMAP = PROJECT_DIR / "heatmap.py"
SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "gh-heatmap"
CONFIG_FILE = SUPPORT_DIR / "config.json"
STATE_FILE = SUPPORT_DIR / "state.json"
BACKUP_FILE = SUPPORT_DIR / "backup.json"
LOCK_FILE = SUPPORT_DIR.parent / "gh-heatmap.lock"   # heatmap.py's lock; outside SUPPORT_DIR
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
WATCH_LABEL = "local.gh-heatmap.spaces"   # opt-in Space watcher (SPEC §13); uninstall.py matches
WATCH_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{WATCH_LABEL}.plist"
LAUNCHD_LOG = Path.home() / "Library" / "Logs" / "gh-heatmap.launchd.log"
LAUNCHCTL = "/bin/launchctl"
LOCK_ATTEMPTS = 15   # ...retried every LOCK_RETRY_SECONDS, about 30 s in total (as uninstall.py)
LOCK_RETRY_SECONDS = 2


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write data to a temp file in the same directory, then os.replace it into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)  # mkstemp creates 0600; launchd and the user may read these
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def acquire_lock() -> int:
    """Take heatmap.py's flock, retrying while a run holds it; return the fd. Holding it while
    config.json is read and rewritten means `heatmap.py --theme` can't lose our update or we its."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    for attempt in range(1, LOCK_ATTEMPTS + 1):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if attempt < LOCK_ATTEMPTS:
                print(f"    another gh-heatmap run holds the lock; retrying in {LOCK_RETRY_SECONDS} s "
                      f"({attempt}/{LOCK_ATTEMPTS})")
                time.sleep(LOCK_RETRY_SECONDS)
    os.close(fd)
    sys.exit("gave up waiting for the gh-heatmap lock; config.json not written, nothing scheduled")


def parse_size(text: str, flag: str) -> int | None:
    """A --width/--height value: 'N' or 'N%' with SIZE_MIN <= N <= SIZE_MAX -> N; 'default' -> None.
    Exits 1 on anything else (same rule as heatmap.py)."""
    if text.lower() == RESET_SIZE:
        return None
    match = SIZE_RE.fullmatch(text)
    if match is None or not SIZE_MIN <= int(match[1]) <= SIZE_MAX:
        sys.exit(f"{flag}: invalid size {text!r}; expected a whole percentage from "
                 f"{SIZE_MIN}% to {SIZE_MAX}%, or {RESET_SIZE!r}")
    return int(match[1])


def saved_settings(overridden: set[str]) -> dict[str, Any]:
    """The theme, width and height in an existing config.json, so a reinstall keeps them
    (SPEC §10). Keys in `overridden` (given on the command line) are not read. Anything
    unusable is dropped with a warning instead of failing the install; absent = default."""
    wanted = set(SETTING_KEYS) - overridden
    if not wanted:
        return {}
    try:
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        print(f"warning: could not read {CONFIG_FILE} ({exc}); not keeping its settings")
        return {}
    if not isinstance(config, dict):
        print(f"warning: {CONFIG_FILE} is not a JSON object; not keeping its settings")
        return {}
    kept: dict[str, Any] = {}
    theme = config.get("theme")
    if "theme" not in wanted or theme is None:
        pass
    elif not isinstance(theme, str):
        print(f"warning: saved theme {theme!r} is not a name; using the default")
    elif theme.lower() in THEME_NAMES:
        kept["theme"] = theme.lower()
    elif theme.lower() != RESET_THEME:
        print(f"warning: saved theme {theme!r} is unknown; using the default")
    mode = config.get("mode")
    if "mode" not in wanted or mode is None:
        pass
    elif not isinstance(mode, str):
        print(f"warning: saved mode {mode!r} is not a name; using {MODE_GITHUB}")
    elif mode.lower() == MODE_T3CODE:
        kept["mode"] = MODE_T3CODE
    elif mode.lower() != MODE_GITHUB:
        print(f"warning: saved mode {mode!r} is unknown; using {MODE_GITHUB}")
    for key in SIZE_KEYS:
        if key not in wanted or key not in config:
            continue
        value = config[key]
        if isinstance(value, int) and not isinstance(value, bool) and SIZE_MIN <= value <= SIZE_MAX:
            kept[key] = value
        else:
            print(f"warning: saved {key} {value!r} is not a whole number from {SIZE_MIN} to {SIZE_MAX}; "
                  "using the default")
    return kept


def describe(config: dict[str, Any]) -> str:
    """'mode: github, theme: nord, width: 40%, height: default' for the install summary."""
    parts = [f"mode: {config.get('mode', MODE_GITHUB)}", f"theme: {config.get('theme', RESET_THEME)}"]
    parts += [f"{key}: {config[key]}%" if key in config else f"{key}: {RESET_SIZE}" for key in SIZE_KEYS]
    return ", ".join(parts)


def backup_ok() -> bool:
    """True if backup.json exists, or heatmap.py recorded that no backup was needed
    (every screen's wallpaper was already one of our PNGs, so there was no original)."""
    if BACKUP_FILE.exists():
        return True
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(state, dict) and state.get("backup_not_needed") is True


def build_plist(interval: int) -> dict:
    """The LaunchAgent definition. No KeepAlive: the script exits on purpose."""
    return {
        "Label": LABEL,
        "ProgramArguments": [str(VENV_PYTHON), str(HEATMAP)],
        "WorkingDirectory": str(PROJECT_DIR),
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin"},
        # Fires missed while asleep are dropped; the next run is <= interval after wake.
        "StartInterval": interval,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "StandardOutPath": str(LAUNCHD_LOG),
        "StandardErrorPath": str(LAUNCHD_LOG),
    }


class ArgumentParser(argparse.ArgumentParser):
    """argparse, except usage errors exit 1 like every other bad input here (argparse uses 2,
    e.g. for `--width -10%`, which it reads as an option rather than a value)."""

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


def build_watch_plist() -> dict:
    """The opt-in Space watcher: resident, restarted by launchd only if it crashes."""
    return {
        "Label": WATCH_LABEL,
        "ProgramArguments": [str(VENV_PYTHON), str(HEATMAP), "--watch-spaces"],
        "WorkingDirectory": str(PROJECT_DIR),
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin"},
        "RunAtLoad": True,
        # It exits 0 on purpose when uninstalled (no config.json): don't restart that.
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 60,   # at most one restart a minute if it keeps crashing
        "StandardOutPath": str(LAUNCHD_LOG),
        "StandardErrorPath": str(LAUNCHD_LOG),
    }


def is_loaded(domain: str, label: str) -> bool:
    """True if launchd still has the job (`launchctl print` succeeds only for a loaded job)."""
    return subprocess.run([LAUNCHCTL, "print", f"{domain}/{label}"], check=False,
                          capture_output=True).returncode == 0


def remove_watcher(domain: str) -> None:
    """Stop the Space watcher and delete its plist; exit if it is still loaded afterwards.

    bootout fails harmlessly when the job isn't loaded, so its exit code alone can't tell
    "already gone" from "failed to stop"; `launchctl print` decides. The plist is kept if the
    job is still loaded, so the state stays visible and a re-run can try again.
    """
    subprocess.run([LAUNCHCTL, "bootout", f"{domain}/{WATCH_LABEL}"], check=False, capture_output=True)
    if is_loaded(domain, WATCH_LABEL):
        sys.exit(f"could not stop {WATCH_LABEL} (still loaded); try `launchctl bootout "
                 f"{domain}/{WATCH_LABEL}`, then run install.py again")
    if WATCH_PLIST_PATH.exists():
        WATCH_PLIST_PATH.unlink()
        print(f"==> stopped {WATCH_LABEL} and removed {WATCH_PLIST_PATH}")


def main() -> int:
    """Run the install steps in order; stop at the first failure."""
    parser = ArgumentParser(description="Install the gh-heatmap LaunchAgent.")
    parser.add_argument("--user", required=True, metavar="NAME", help="GitHub username")
    parser.add_argument("--mode", metavar="NAME",
                        help=f"what the squares show: {MODE_GITHUB} (your contribution graph) or {MODE_T3CODE} "
                             "(T3 Code runs per day). Omitted: keep the saved mode on a reinstall, else github")
    parser.add_argument("--theme", metavar="NAME",
                        help=f"colour theme: {', '.join(THEME_NAMES)}, or {RESET_THEME} (the green look). "
                             "Omitted: keep the saved theme on a reinstall, else default")
    parser.add_argument("--width", metavar="PCT",
                        help="largest grid width as a percentage of the screen (10%%-100%%), or "
                             f"{RESET_SIZE}. Omitted: keep the saved width on a reinstall, else default")
    parser.add_argument("--height", metavar="PCT",
                        help="largest grid height as a percentage of the screen (10%%-100%%), or "
                             f"{RESET_SIZE}. Omitted: keep the saved height on a reinstall, else default")
    parser.add_argument("--interval", type=int, default=600, help="seconds between runs (default 600)")
    parser.add_argument("--watch-spaces", action=argparse.BooleanOptionalAction, default=None,
                        help="also run a small resident process that re-applies the graph when you "
                             "switch Spaces (SPEC §13). Omitted: keep whatever a previous install chose")
    args = parser.parse_args()

    # 1. Validate inputs and find uv.
    if not USERNAME_RE.fullmatch(args.user):
        sys.exit(f"invalid GitHub username: {args.user!r}")
    if args.interval < 60:
        sys.exit("--interval must be at least 60 seconds")
    # Checked before uv sync, so an unknown name or bad size changes nothing at all.
    if args.theme is not None and args.theme.lower() not in THEME_NAMES + (RESET_THEME,):
        sys.exit(f"unknown theme {args.theme!r}; valid: {', '.join(THEME_NAMES)} (or {RESET_THEME!r})")
    if args.mode is not None and args.mode.lower() not in MODES:
        sys.exit(f"unknown mode {args.mode!r}; valid: {', '.join(MODES)}")
    given: dict[str, Any] = {}  # flags given on the command line; None = back to the default
    if args.mode is not None:
        given["mode"] = None if args.mode.lower() == MODE_GITHUB else args.mode.lower()
    if args.theme is not None:
        given["theme"] = None if args.theme.lower() == RESET_THEME else args.theme.lower()
    for key in SIZE_KEYS:
        if getattr(args, key) is not None:
            given[key] = parse_size(getattr(args, key), f"--{key}")
    uv = shutil.which("uv")
    if uv is None:
        sys.exit("uv not found on PATH; install it first (https://docs.astral.sh/uv/)")

    # 1a. --no-watch-spaces is honoured first, so turning the watcher off never depends on
    #     the steps below (uv sync, the first run, GitHub, the main agent) succeeding.
    domain = f"gui/{os.getuid()}"
    if args.watch_spaces is False:
        remove_watcher(domain)

    # 2. Install exactly the hash-pinned wheels from uv.lock. --locked fails if uv.lock is
    #    missing or out of date with pyproject.toml (--frozen would silently accept a stale lock).
    print("==> uv sync --locked")
    subprocess.run([uv, "sync", "--locked"], cwd=PROJECT_DIR, check=True)

    # 3. Support dir + config.json, read and written under heatmap.py's lock. The lock is
    #    released before the first run, which takes it itself.
    SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = acquire_lock()
    try:
        # Each setting independently: the flag if given, else the saved value, else default.
        settings = {**saved_settings(set(given)), **given}
        config: dict[str, Any] = {"username": args.user}
        config.update({key: settings[key] for key in SETTING_KEYS if settings.get(key) is not None})
        atomic_write_bytes(CONFIG_FILE, (json.dumps(config, indent=2) + "\n").encode("utf-8"))
    finally:
        os.close(lock_fd)
    print(f"==> wrote {CONFIG_FILE} ({describe(config)})")

    # 4. First run by hand: captures backup.json and surfaces errors before scheduling.
    #    heatmap.py exits 0 only if every screen was applied or already up to date;
    #    2 means it skipped (no screens, GitHub backoff, lock busy); 1 is an error.
    print("==> first run (sets the wallpaper)")
    code = subprocess.run([str(VENV_PYTHON), str(HEATMAP)], cwd=PROJECT_DIR, check=False).returncode
    t3code = config.get("mode") == MODE_T3CODE
    if code == 2:
        reasons = "no screens, or another run in progress" if t3code else \
            "no screens, GitHub backoff, or another run in progress"
        sys.exit(f"first run was skipped ({reasons}); nothing scheduled - try again later")
    if code != 0:
        sys.exit(f"first run failed (exit {code}); see ~/Library/Logs/gh-heatmap.log - nothing scheduled")
    if not backup_ok():
        sys.exit(f"first run left no {BACKUP_FILE}; refusing to schedule without a wallpaper backup")

    # 5. Write the LaunchAgent plist.
    LAUNCHD_LOG.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(PLIST_PATH, plistlib.dumps(build_plist(args.interval)))
    print(f"==> wrote {PLIST_PATH}")

    # 6. (Re)load it: bootout may fail if it was not loaded, which is fine.
    subprocess.run([LAUNCHCTL, "bootout", f"{domain}/{LABEL}"], check=False, capture_output=True)
    subprocess.run([LAUNCHCTL, "bootstrap", domain, str(PLIST_PATH)], check=True)

    # 6a. The opt-in Space watcher: on with --watch-spaces; with no flag, as a previous
    #     install left it (its plist exists or not); --no-watch-spaces was handled in 1a.
    watch = WATCH_PLIST_PATH.exists() if args.watch_spaces is None else args.watch_spaces
    if watch:
        subprocess.run([LAUNCHCTL, "bootout", f"{domain}/{WATCH_LABEL}"], check=False, capture_output=True)
        if is_loaded(domain, WATCH_LABEL):  # the old one didn't stop: don't touch its plist
            sys.exit(f"{LABEL} is installed, but the running {WATCH_LABEL} could not be stopped "
                     f"to reload it; try `launchctl bootout {domain}/{WATCH_LABEL}`, then run install.py again")
        atomic_write_bytes(WATCH_PLIST_PATH, plistlib.dumps(build_watch_plist()))
        loaded = subprocess.run([LAUNCHCTL, "bootstrap", domain, str(WATCH_PLIST_PATH)], check=False)
        if loaded.returncode != 0:
            WATCH_PLIST_PATH.unlink(missing_ok=True)
            sys.exit(f"{LABEL} is installed, but loading {WATCH_LABEL} failed "
                     f"(launchctl exit {loaded.returncode}); the Space watcher is not installed")
        print(f"==> wrote and loaded {WATCH_PLIST_PATH}")

    # 7. Report.
    source = "showing T3 Code usage" if t3code else f"for GitHub user {args.user}"
    print(f"\nInstalled {LABEL}: runs every {args.interval} s {source} ({describe(config)}).")
    if watch:
        print(f"Installed {WATCH_LABEL}: re-applies the graph when you switch Spaces "
              f"(turn off: python3 install.py --user {args.user} --no-watch-spaces).")
    other = MODE_GITHUB if t3code else MODE_T3CODE
    print(f"  mode:      cd {PROJECT_DIR} && uv run heatmap.py --mode {other}   (--mode {MODE_GITHUB} or {MODE_T3CODE})")
    print(f"  theme:     cd {PROJECT_DIR} && uv run heatmap.py --theme NAME   (--list-themes for names)")
    print(f"  size:      cd {PROJECT_DIR} && uv run heatmap.py --width 40% --height 30%   ({RESET_SIZE} resets)")
    print(f"  check:     launchctl print {domain}/{LABEL}")
    print("  log:       ~/Library/Logs/gh-heatmap.log")
    print(f"  uninstall: python3 {PROJECT_DIR / 'uninstall.py'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

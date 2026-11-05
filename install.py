#!/usr/bin/env python3
"""Install gh-heatmap for the current user (SPEC §10).

Usage: python3 install.py --user NAME [--theme NAME] [--interval 600]

Runs `uv sync --locked`, writes config.json (keeping a saved theme unless --theme is given),
does one run by hand (which must succeed
and leave a wallpaper backup), then writes and loads the per-user LaunchAgent. Everything lives under $HOME; no sudo. Uses only the
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

# These paths, the regex and the theme names must match heatmap.py (`heatmap.py --list-themes`).
USERNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
THEME_NAMES = ("dracula", "gruvbox", "nord", "panda", "tokyo-night")
RESET_THEME = "default"
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


def saved_theme() -> str | None:
    """The theme in an existing config.json, so a reinstall keeps it (SPEC §10). Anything
    unusable is dropped with a warning instead of failing the install; None = default."""
    try:
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        print(f"warning: could not read {CONFIG_FILE} ({exc}); not keeping its theme")
        return None
    theme = config.get("theme") if isinstance(config, dict) else None
    if not isinstance(config, dict):
        print(f"warning: {CONFIG_FILE} is not a JSON object; not keeping its theme")
    elif theme is not None and not isinstance(theme, str):
        print(f"warning: saved theme {theme!r} is not a name; using the default")
    elif isinstance(theme, str):
        name = theme.lower()
        if name in THEME_NAMES:
            return name
        if name != RESET_THEME:
            print(f"warning: saved theme {theme!r} is unknown; using the default")
    return None


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


def main() -> int:
    """Run the install steps in order; stop at the first failure."""
    parser = argparse.ArgumentParser(description="Install the gh-heatmap LaunchAgent.")
    parser.add_argument("--user", required=True, metavar="NAME", help="GitHub username")
    parser.add_argument("--theme", metavar="NAME",
                        help=f"colour theme: {', '.join(THEME_NAMES)}, or {RESET_THEME} (the green look). "
                             "Omitted: keep the saved theme on a reinstall, else default")
    parser.add_argument("--interval", type=int, default=600, help="seconds between runs (default 600)")
    args = parser.parse_args()

    # 1. Validate inputs and find uv.
    if not USERNAME_RE.fullmatch(args.user):
        sys.exit(f"invalid GitHub username: {args.user!r}")
    if args.interval < 60:
        sys.exit("--interval must be at least 60 seconds")
    # Checked before uv sync, so an unknown name changes nothing at all.
    if args.theme is not None and args.theme.lower() not in THEME_NAMES + (RESET_THEME,):
        sys.exit(f"unknown theme {args.theme!r}; valid: {', '.join(THEME_NAMES)} (or {RESET_THEME!r})")
    uv = shutil.which("uv")
    if uv is None:
        sys.exit("uv not found on PATH; install it first (https://docs.astral.sh/uv/)")

    # 2. Install exactly the hash-pinned wheels from uv.lock. --locked fails if uv.lock is
    #    missing or out of date with pyproject.toml (--frozen would silently accept a stale lock).
    print("==> uv sync --locked")
    subprocess.run([uv, "sync", "--locked"], cwd=PROJECT_DIR, check=True)

    # 3. Support dir + config.json, read and written under heatmap.py's lock. The lock is
    #    released before the first run, which takes it itself.
    SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = acquire_lock()
    try:
        if args.theme is None:
            theme = saved_theme()
        else:
            theme = None if args.theme.lower() == RESET_THEME else args.theme.lower()
        config = {"username": args.user}
        if theme is not None:
            config["theme"] = theme
        atomic_write_bytes(CONFIG_FILE, (json.dumps(config, indent=2) + "\n").encode("utf-8"))
    finally:
        os.close(lock_fd)
    print(f"==> wrote {CONFIG_FILE} (theme: {theme or RESET_THEME})")

    # 4. First run by hand: captures backup.json and surfaces errors before scheduling.
    #    heatmap.py exits 0 only if every screen was applied or already up to date;
    #    2 means it skipped (no screens, GitHub backoff, lock busy); 1 is an error.
    print("==> first run (sets the wallpaper)")
    code = subprocess.run([str(VENV_PYTHON), str(HEATMAP)], cwd=PROJECT_DIR, check=False).returncode
    if code == 2:
        sys.exit("first run was skipped (no screens, GitHub backoff, or another run in progress); "
                 "nothing scheduled - try again later")
    if code != 0:
        sys.exit(f"first run failed (exit {code}); see ~/Library/Logs/gh-heatmap.log - nothing scheduled")
    if not backup_ok():
        sys.exit(f"first run left no {BACKUP_FILE}; refusing to schedule without a wallpaper backup")

    # 5. Write the LaunchAgent plist.
    LAUNCHD_LOG.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(PLIST_PATH, plistlib.dumps(build_plist(args.interval)))
    print(f"==> wrote {PLIST_PATH}")

    # 6. (Re)load it: bootout may fail if it was not loaded, which is fine.
    domain = f"gui/{os.getuid()}"
    subprocess.run([LAUNCHCTL, "bootout", f"{domain}/{LABEL}"], check=False, capture_output=True)
    subprocess.run([LAUNCHCTL, "bootstrap", domain, str(PLIST_PATH)], check=True)

    # 7. Report.
    print(f"\nInstalled {LABEL}: runs every {args.interval} s for GitHub user {args.user} "
          f"(theme: {theme or RESET_THEME}).")
    print(f"  theme:     cd {PROJECT_DIR} && uv run heatmap.py --theme NAME   (--list-themes for names)")
    print(f"  check:     launchctl print {domain}/{LABEL}")
    print("  log:       ~/Library/Logs/gh-heatmap.log")
    print(f"  uninstall: python3 {PROJECT_DIR / 'uninstall.py'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Uninstall gh-heatmap for the current user (SPEC §11).

Usage: python3 uninstall.py

Unloads the LaunchAgent(s) (the scheduled one and the opt-in Space watcher) and deletes
their plists, then runs `heatmap.py --uninstall`. That
mode holds the tool's lock throughout: it restores the original wallpaper (best-effort),
checks every screen, and deletes the files under ~/Library only if no screen still shows
one of our PNGs. This script never deletes those files itself. Re-running it is safe and
idempotent. The source folder, .venv, and the empty lock file
~/Library/Application Support/gh-heatmap.lock are left alone.
Uses only the standard library so it runs on any Python 3.9+.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# These paths must match heatmap.py / install.py.
LABEL = "local.gh-heatmap"
PROJECT_DIR = Path(__file__).resolve().parent
VENV_PYTHON = PROJECT_DIR / ".venv" / "bin" / "python"
HEATMAP = PROJECT_DIR / "heatmap.py"
SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "gh-heatmap"
BACKUP_FILE = SUPPORT_DIR / "backup.json"
LOCK_FILE = SUPPORT_DIR.parent / "gh-heatmap.lock"   # outside SUPPORT_DIR; never deleted
LOGS_DIR = Path.home() / "Library" / "Logs"
LOG_FILES = [LOGS_DIR / "gh-heatmap.log"] + [LOGS_DIR / f"gh-heatmap.log.{i}" for i in (1, 2, 3)]
LOG_FILES.append(LOGS_DIR / "gh-heatmap.launchd.log")
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
WATCH_LABEL = "local.gh-heatmap.spaces"   # opt-in Space watcher (SPEC §13)
WATCH_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{WATCH_LABEL}.plist"
LAUNCHCTL = "/bin/launchctl"

LOCK_BUSY = 3        # heatmap.py --uninstall exit code when another run holds the lock
ATTEMPTS = 15        # ...retried every RETRY_SECONDS, about 30 s in total
RETRY_SECONDS = 2


def print_saved_originals() -> None:
    """Print the original wallpaper paths recorded in backup.json (plain JSON read)."""
    print("Saved original wallpaper(s):")
    try:
        for entry in json.loads(BACKUP_FILE.read_text(encoding="utf-8")):
            print(f"  display {entry.get('display')}: {entry.get('path')}")
    except (OSError, ValueError, TypeError, AttributeError):
        print("  (no readable backup.json)")


def print_cleanup_paths() -> None:
    """List every path uninstall deals with and whether it still exists."""
    print("Cleanup may be incomplete; check these paths:")
    for path in [LOCK_FILE, SUPPORT_DIR] + LOG_FILES:
        note = "  (kept on purpose)" if path == LOCK_FILE else ""
        print(f"  {'exists ' if path.exists() else 'missing'}  {path}{note}")


def run_heatmap_uninstall() -> int:
    """Run `heatmap.py --uninstall`, retrying while another run holds the lock; its exit code."""
    for attempt in range(1, ATTEMPTS + 1):
        code = subprocess.run([str(VENV_PYTHON), str(HEATMAP), "--uninstall"], cwd=PROJECT_DIR,
                              check=False).returncode
        if code != LOCK_BUSY:
            return code
        if attempt < ATTEMPTS:  # no pointless wait after the final attempt
            print(f"    another gh-heatmap run holds the lock; retrying in {RETRY_SECONDS} s "
                  f"({attempt}/{ATTEMPTS})")
            time.sleep(RETRY_SECONDS)
    print("    gave up waiting for the lock")
    return LOCK_BUSY


def main() -> int:
    """Unschedule, then hand restore + verify + delete to heatmap.py; propagate its result."""
    # 1-2. Stop the scheduled job and the Space watcher (ignore failure: they may not be
    #      loaded) and remove their plists, whatever happens next, so nothing runs again.
    #      The watcher must be gone before restoring, or a Space switch would re-apply.
    for label, plist in ((LABEL, PLIST_PATH), (WATCH_LABEL, WATCH_PLIST_PATH)):
        subprocess.run([LAUNCHCTL, "bootout", f"gui/{os.getuid()}/{label}"], check=False, capture_output=True)
        plist.unlink(missing_ok=True)
        print(f"==> unloaded {label} (if it was loaded) and deleted {plist}")

    # 3. Without the venv we can neither restore nor check the screens: delete nothing.
    if not VENV_PYTHON.exists():
        print("==> .venv is missing, so the wallpaper can't be restored or verified safely.")
        print(f"Nothing under {SUPPORT_DIR} or ~/Library/Logs was deleted.")
        print_saved_originals()
        print(f"Run `uv sync --locked` in {PROJECT_DIR}, then run uninstall.py again.")
        return 1

    # 4. Restore, verify and delete, all under heatmap.py's lock.
    print("==> restoring the original wallpaper and removing files")
    code = run_heatmap_uninstall()
    if code != 0:
        print(f"\nheatmap.py --uninstall exited {code} (see the messages above).")
        print_cleanup_paths()
        print_saved_originals()
        print("Fix the problem (e.g. re-pick your wallpaper in System Settings > Wallpaper), then")
        print("run uninstall.py again; that is safe to repeat.")
        return code

    print(f"\nUninstalled. The source folder {PROJECT_DIR} and its .venv were left alone;")
    print("delete that folder to remove everything. The empty lock file")
    print(f"{LOCK_FILE} is kept on purpose so every gh-heatmap run always shares one lock.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

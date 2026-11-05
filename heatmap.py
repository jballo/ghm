#!/usr/bin/env python3
"""gh-heatmap: your public GitHub contribution graph as your macOS wallpaper (see SPEC.md).

Each run: fetch -> parse -> render -> set wallpaper -> exit. The one exception is the opt-in
`--watch-spaces` mode (SPEC §13), which stays running and re-applies cached data whenever the
active Space changes, without any network request. Only the standard library is
imported at module level; Pillow and AppKit/Foundation are imported inside the functions
that need them, so `--restore` and `--uninstall` never import Pillow.

`--uninstall` (called by uninstall.py) holds the lock throughout: it restores backup.json,
re-checks every screen, and deletes our files only if no screen still shows one of our
PNGs. Re-running it is safe and idempotent: already-restored paths are simply set again,
and the check then passes.

Exit codes (SPEC §3):
  0  success: applied or already up to date on every screen; --restore restored everything;
     --uninstall deleted everything; --watch-spaces stopped, or found nothing installed
  1  error, including: --dry-run wrote no preview; --restore could not restore every screen;
     --uninstall kept the files because a screen still shows our image; an unknown theme;
     any command-line usage error
  2  skipped gracefully: no screens, inside a GitHub backoff window, or the lock is busy
  3  --restore / --uninstall only: the lock is busy (uninstall.py waits and retries)
"""

import argparse
import datetime as dt
import fcntl
import hashlib
import http.client
import io
import json
import logging
import logging.handlers
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

# --- Constants -------------------------------------------------------------

USERNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
USER_AGENT = "gh-heatmap/1 (personal wallpaper script)"
TIMEOUT_SECONDS = 15
MAX_BODY_BYTES = 2 * 1024 * 1024          # 2 MiB
RETRY_AFTER_DEFAULT = 1800                # seconds, when Retry-After is absent/unparseable
RETRY_AFTER_FLOOR = 60
RETRY_AFTER_CEILING = 24 * 3600           # policy: never pause longer than a day (SPEC §4)
STYLE_VERSION = 1                         # bump whenever render output changes
STATE_VERSION = 1
GITHUB_PREFIX = "https://github.com/"     # every URL we fetch or follow must start with this

EXIT_OK, EXIT_ERROR, EXIT_SKIPPED, EXIT_LOCK_BUSY = 0, 1, 2, 3

SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "gh-heatmap"
CONFIG_FILE = SUPPORT_DIR / "config.json"
STATE_FILE = SUPPORT_DIR / "state.json"
BACKUP_FILE = SUPPORT_DIR / "backup.json"
# The lock lives NEXT TO the support dir, never inside it, and is never deleted: if
# --uninstall's rmtree unlinked it, a new run could lock a fresh inode while we still hold
# the old one.
LOCK_FILE = SUPPORT_DIR.parent / "gh-heatmap.lock"
PREVIEW_FILE = SUPPORT_DIR / "preview.png"
LOG_FILE = Path.home() / "Library" / "Logs" / "gh-heatmap.log"
# Everything --uninstall deletes besides SUPPORT_DIR: the log, its rotated copies, launchd's log.
LOG_FILES = [LOG_FILE] + [LOG_FILE.with_name(f"gh-heatmap.log.{i}") for i in (1, 2, 3)]
LOG_FILES.append(LOG_FILE.with_name("gh-heatmap.launchd.log"))
WALLPAPER_SETTINGS_URL = "x-apple.systempreferences:com.apple.Wallpaper-Settings.extension"

# Themes (SPEC §7): a background plus the colours of contribution levels 0-4. For the built-ins,
# the background, level 0 and level 4 are each palette's official published colours; levels
# 1-3 are level 4 blended into the background at 35 / 55 / 78 % (precomputed here).
DEFAULT_THEME = {"background": "#000000",  # GitHub greens on black; used when no theme is set
                 "levels": ["#1b2a1f", "#0e4429", "#006d32", "#26a641", "#39d353"]}
THEMES = {
    # Background / Current Line / Purple
    "dracula": {"background": "#282a36", "levels": ["#44475a", "#5c4f7a", "#7a64a1", "#9c7cce", "#bd93f9"]},
    # bg0 / bg1 / bright orange (dark)
    "gruvbox": {"background": "#282828", "levels": ["#3c3836", "#734723", "#9e5820", "#cf6d1c", "#fe8019"]},
    # nord0 / nord1 / nord8
    "nord": {"background": "#2e3440", "levels": ["#3b4252", "#4e6572", "#60818f", "#74a1b0", "#88c0d0"]},
    # background-light / gray / pink (panda-syntax-vscode themes/colors.yaml)
    "panda": {"background": "#292a2b", "levels": ["#373b41", "#74445b", "#9f5377", "#d06497", "#ff75b5"]},
    # bg / bg_highlight / blue (night)
    "tokyo-night": {"background": "#1a1b26", "levels": ["#292e42", "#3c4a6f", "#4f6599", "#6584c9", "#7aa2f7"]},
}
RESET_THEME = "default"                   # `--theme default` clears the saved theme
HEX_COLOUR_RE = re.compile(r"^#[0-9a-f]{6}$")
FALLBACK_SIZE = (2880, 1800)              # dry-run size when no screen is available

# Pruning (SPEC §6): besides the protected PNGs, keep up to RETAIN_EXTRA others rendered in
# the last RETAIN_DAYS, so a Space you are not on rarely points at a deleted file (~32 KB each).
RETAIN_DAYS = 14
RETAIN_EXTRA = 50

# macOS applies a wallpaper change asynchronously: desktopImageURLForScreen_ can keep
# returning the OLD path for hundreds of ms after a successful set. We poll (SPEC §8).
RESTORE_WAIT_SECONDS = 5.0                # after a restore, and before uninstall calls a display "ours"
APPLY_WAIT_SECONDS = 2.0                  # after setting a heatmap, so the next run sees the right path
POLL_INTERVAL_SECONDS = 0.25

log = logging.getLogger("gh-heatmap")


class FetchError(Exception):
    """The response was unusable (wrong host after redirects, too large, odd status)."""


# --- Small helpers: logging, files, time -----------------------------------

def setup_logging() -> None:
    """Log to the rotating log file, and also to stderr when stderr is a terminal."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=512 * 1024, backupCount=3)
    file_handler.setFormatter(fmt)
    log.addHandler(file_handler)
    if sys.stderr.isatty():
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(fmt)
        log.addHandler(console)
    log.setLevel(logging.INFO)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write data to a temp file in the same directory, then os.replace it into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, obj: Any) -> None:
    """Serialise obj as pretty JSON and write it atomically."""
    atomic_write_bytes(path, (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def read_json(path: Path) -> Any:
    """Return the parsed JSON in path, or None if missing (ValueError if invalid JSON)."""
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def iso_utc(moment: dt.datetime) -> str:
    """Format an aware datetime like 2026-11-04T17:00:00Z."""
    return moment.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_username(name: Any) -> str:
    """Return name if it is a valid GitHub username, else raise ValueError."""
    # fullmatch, so a trailing newline cannot slip past the `$` anchor.
    if not isinstance(name, str) or not USERNAME_RE.fullmatch(name):
        raise ValueError(f"invalid GitHub username {name!r}")
    return name


def validate_theme(theme: Any) -> dict[str, Any]:
    """Return theme if it is {"background": "#rrggbb", "levels": [5 x "#rrggbb"]}, else raise ValueError."""
    if not isinstance(theme, dict) or set(theme) != {"background", "levels"}:
        raise ValueError("a theme needs exactly the keys 'background' and 'levels'")
    levels = theme["levels"]
    if not isinstance(levels, list) or len(levels) != 5:
        raise ValueError("a theme's 'levels' must be a list of 5 colours")
    for colour in [theme["background"], *levels]:
        if not isinstance(colour, str) or not HEX_COLOUR_RE.fullmatch(colour):
            raise ValueError(f"invalid theme colour {colour!r}; expected lowercase #rrggbb")
    return theme


def normalize_theme_name(name: Any) -> str | None:
    """Lowercase and check a theme name; None means the default look (no theme, or 'default')."""
    if name is None:
        return None
    if not isinstance(name, str):
        raise ValueError(f"theme must be a string, not {name!r}")
    key = name.lower()
    if key == RESET_THEME:
        return None
    if key not in THEMES:
        raise ValueError(f"unknown theme {name!r}; valid: {', '.join(sorted(THEMES))} (or {RESET_THEME!r})")
    return key


def resolve_theme(name: str | None) -> dict[str, Any]:
    """The validated colours for a normalized theme name (None = the default look)."""
    return validate_theme(DEFAULT_THEME if name is None else THEMES[name])


# --- Parse and layout (SPEC §5) --------------------------------------------

class CellParser(HTMLParser):
    """Collect (data-date, data-level) from every <td> that has both attributes."""

    def __init__(self) -> None:
        """Start with an empty list of found cells."""
        super().__init__()
        self.found: list[tuple[str | None, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Record a cell when a <td> carries both data-date and data-level."""
        if tag != "td":
            return
        values = dict(attrs)
        if "data-date" in values and "data-level" in values:
            self.found.append((values["data-date"], values["data-level"]))


def validate_cells(cells: Any, today: dt.date) -> dict[str, int]:
    """Check the SPEC §5 rules and return the cells sorted by date; raise ValueError."""
    if not isinstance(cells, dict):
        raise ValueError("cells must be a mapping of date -> level")
    if not 7 <= len(cells) <= 378:
        raise ValueError(f"expected 7-378 cells, got {len(cells)}")
    days = []
    for text, level in cells.items():
        if not isinstance(level, int) or isinstance(level, bool) or not 0 <= level <= 4:
            raise ValueError(f"bad level {level!r} for {text!r}")
        try:
            day = dt.date.fromisoformat(text)
        except (TypeError, ValueError):
            raise ValueError(f"bad date {text!r}") from None
        if day.isoformat() != text:
            raise ValueError(f"date not in YYYY-MM-DD form: {text!r}")
        days.append(day)
    days.sort()
    for prev, cur in zip(days, days[1:]):
        if cur - prev != dt.timedelta(days=1):
            raise ValueError(f"dates not contiguous: {prev} -> {cur}")
    if days[-1] > today + dt.timedelta(days=1):
        raise ValueError(f"latest date {days[-1]} is in the future")
    return {day.isoformat(): cells[day.isoformat()] for day in days}


def parse_cells(html_text: str, today: dt.date) -> dict[str, int]:
    """Parse the contributions page into {ISO date: level}; raise ValueError on bad data."""
    parser = CellParser()
    parser.feed(html_text)
    parser.close()
    cells: dict[Any, int] = {}
    for date_text, level_text in parser.found:
        if date_text in cells:
            raise ValueError(f"duplicate date {date_text!r}")
        try:
            cells[date_text] = int(level_text)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise ValueError(f"bad level {level_text!r} for {date_text!r}") from None
    return validate_cells(cells, today)


def layout(cells: dict[str, int]) -> tuple[dict[str, tuple[int, int]], int]:
    """Return ({date: (col, row)}, ncols); col = week, row 0 = Sunday ... 6 = Saturday.

    Layout is by date, never by document order (GitHub lists cells weekday-major).
    """
    first = min(dt.date.fromisoformat(text) for text in cells)
    # weekday(): Monday=0 ... Sunday=6, so this steps back to the Sunday on/before `first`.
    start_sunday = first - dt.timedelta(days=(first.weekday() + 1) % 7)
    positions = {}
    for text in cells:
        days = (dt.date.fromisoformat(text) - start_sunday).days
        positions[text] = (days // 7, days % 7)
    ncols = max(col for col, _row in positions.values()) + 1
    return positions, ncols


# --- Render (SPEC §7) ------------------------------------------------------

def render_hash(cells: dict[str, int], width: int, height: int, theme: dict[str, Any]) -> str:
    """Hash of everything that affects the rendered PNG (SPEC §6), including the theme's colours."""
    payload = json.dumps({"cells": cells, "w": width, "h": height, "style": STYLE_VERSION, "theme": theme},
                         sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def render_png(cells: dict[str, int], width: int, height: int, theme: dict[str, Any]) -> bytes:
    """Draw the heatmap centred on the theme's background and return the PNG bytes."""
    from PIL import Image, ImageDraw  # lazy: only runs when a render is needed

    positions, ncols = layout(cells)
    pitch = 0.62 * width / ncols
    pitch = min(pitch, 0.62 * height / 7)  # guard: very short histories must still fit vertically
    side = round(0.76 * pitch)             # square size in whole pixels, same for every cell
    radius = round(0.22 * side)
    grid_w = (ncols - 1) * pitch + side
    grid_h = 6 * pitch + side
    left = (width - grid_w) / 2
    top = (height - grid_h) / 2

    image = Image.new("RGB", (width, height), theme["background"])
    draw = ImageDraw.Draw(image)
    for text, (col, row) in positions.items():
        x0 = round(left + col * pitch)
        y0 = round(top + row * pitch)
        # Pillow boxes include the end pixel, hence the -1.
        box = (x0, y0, x0 + side - 1, y0 + side - 1)
        draw.rounded_rectangle(box, radius=radius, fill=theme["levels"][cells[text]])
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


# --- Fetch (SPEC §4) -------------------------------------------------------

class GitHubOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only if its target starts with https://github.com/."""

    def redirect_request(self, req: urllib.request.Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> urllib.request.Request | None:
        """Raise FetchError for an off-host target; otherwise defer to the stock handler."""
        # urllib has already made `newurl` absolute (urljoin) before calling this.
        if not newurl.startswith(GITHUB_PREFIX):
            raise FetchError(f"refusing redirect to {newurl!r}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(username: str, etag: str | None) -> tuple[int, str, str | None, str | None]:
    """GET the page; return (status, body, etag, retry_after) for status 200/304/429/503.

    Any other outcome raises OSError, http.client.HTTPException or FetchError.
    """
    url = f"https://github.com/users/{validate_username(username)}/contributions"
    headers = {"User-Agent": USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag
    request = urllib.request.Request(url, headers=headers)
    # Passing our subclass makes build_opener use it instead of the default redirect handler.
    opener = urllib.request.build_opener(GitHubOnlyRedirectHandler)
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            if not response.geturl().startswith(GITHUB_PREFIX):  # belt and braces
                raise FetchError(f"redirected off GitHub to {response.geturl()!r}")
            if response.status != 200:
                raise FetchError(f"unexpected HTTP status {response.status}")
            body = response.read(MAX_BODY_BYTES + 1)
            if len(body) > MAX_BODY_BYTES:
                raise FetchError("response larger than 2 MiB")
            return 200, body.decode("utf-8", errors="replace"), response.headers.get("ETag"), None
    except urllib.error.HTTPError as exc:
        # urllib reports 304 and 4xx/5xx as HTTPError; these three are expected,
        # but only trust them if they came from GitHub itself.
        if not exc.geturl().startswith(GITHUB_PREFIX):
            raise FetchError(f"HTTP {exc.code} from non-GitHub URL {exc.geturl()!r}") from None
        if exc.code in (304, 429, 503):
            return exc.code, "", None, exc.headers.get("Retry-After")
        raise


def retry_after_seconds(header: str | None) -> int:
    """Seconds to wait from a Retry-After header (seconds form only), clamped."""
    try:
        seconds = int(header) if header else RETRY_AFTER_DEFAULT
    except ValueError:
        seconds = RETRY_AFTER_DEFAULT  # e.g. the HTTP-date form, which we do not parse
    return min(max(seconds, RETRY_AFTER_FLOOR), RETRY_AFTER_CEILING)


def get_cells(username: str, state: dict[str, Any], use_etag: bool = True) -> dict[str, int] | None:
    """Fetch and parse cells, updating `state` in memory; None means "back off" (429/503).

    Network errors fall back to cached cells; parse/validation errors raise ValueError.
    """
    today = dt.date.today()
    cached = state.get("cells") or None
    etag = state.get("etag") if (cached and use_etag) else None
    try:
        status, body, new_etag, retry_after = fetch(username, etag)
    except (OSError, http.client.HTTPException, FetchError) as exc:
        if cached:
            log.warning("fetch failed (%s); using cached cells", exc)
            return validate_cells(cached, today)
        raise
    if status == 304:
        log.info("not modified; using cached cells")
        return validate_cells(cached, today)
    if status in (429, 503):
        seconds = retry_after_seconds(retry_after)
        state["retry_until"] = iso_utc(dt.datetime.now(dt.UTC) + dt.timedelta(seconds=seconds))
        log.warning("HTTP %d from GitHub; backing off %d s", status, seconds)
        return None
    cells = parse_cells(body, today)
    state.update(etag=new_etag, cells=cells, fetched_at=iso_utc(dt.datetime.now(dt.UTC)), retry_until=None)
    return cells


# --- State (SPEC §6) -------------------------------------------------------

def load_state(username: str) -> dict[str, Any]:
    """Read state.json (or start fresh), dropping cached data for a different user."""
    try:
        state = read_json(STATE_FILE)
    except ValueError:
        log.warning("state.json unreadable; starting fresh")
        state = None
    if not isinstance(state, dict):
        state = {}
    if state.get("username") != username:
        for key in ("etag", "cells", "fetched_at"):
            state.pop(key, None)
    for key in ("applied", "applied_prev"):  # display id -> current / previous render hash
        if not isinstance(state.get(key), dict):
            state[key] = {}
    state.setdefault("retry_until", None)
    state["username"] = username
    state["version"] = STATE_VERSION
    return state


def rate_limited(state: dict[str, Any]) -> bool:
    """True while we are inside a Retry-After window recorded in state."""
    until = state.get("retry_until")
    if not until:
        return False
    try:
        return dt.datetime.now(dt.UTC) < dt.datetime.fromisoformat(until)
    except (TypeError, ValueError):
        return False


def acquire_lock() -> int | None:
    """Take an exclusive non-blocking flock on LOCK_FILE; return the fd, or None if busy."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


# --- Wallpaper via PyObjC (SPEC §8) ----------------------------------------

def screens_and_workspace() -> tuple[list[Any], Any]:
    """The connected screens (possibly none) and the shared NSWorkspace."""
    from AppKit import NSScreen, NSWorkspace

    return list(NSScreen.screens() or []), NSWorkspace.sharedWorkspace()


def display_id(screen: Any) -> int:
    """The NSScreenNumber of a screen, used as its key in state and backup."""
    return int(screen.deviceDescription()["NSScreenNumber"])


def backing_size(screen: Any) -> tuple[int, int]:
    """The screen's size in physical pixels."""
    rect = screen.convertRectToBacking_(screen.frame())
    return int(rect.size.width), int(rect.size.height)


def wallpaper_path(workspace: Any, screen: Any) -> str | None:
    """Filesystem path of the screen's current wallpaper, or None."""
    url = workspace.desktopImageURLForScreen_(screen)
    if url is None or not url.path():
        return None
    return str(url.path())


def poll_wallpaper(workspace: Any, screen: Any, done: Callable[[str | None], bool],
                   timeout: float, interval: float) -> bool:
    """Re-read the screen's wallpaper path until done(path) is true; False after `timeout` s."""
    deadline = time.monotonic() + timeout
    while True:
        if done(wallpaper_path(workspace, screen)):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def wait_for_wallpaper(workspace: Any, screen: Any, expected_path: str,
                       timeout: float = RESTORE_WAIT_SECONDS, interval: float = POLL_INTERVAL_SECONDS) -> bool:
    """Wait until macOS reports expected_path (compared by realpath) for this screen."""
    target = os.path.realpath(expected_path)
    return poll_wallpaper(workspace, screen, lambda path: path is not None and os.path.realpath(path) == target,
                          timeout, interval)


def wait_until_not_ours(workspace: Any, screen: Any,
                        timeout: float = RESTORE_WAIT_SECONDS, interval: float = POLL_INTERVAL_SECONDS) -> bool:
    """Wait until this screen's wallpaper is no longer one of ours (see is_ours)."""
    return poll_wallpaper(workspace, screen, lambda path: path is None or not is_ours(path), timeout, interval)


def set_wallpaper(workspace: Any, screen: Any, path: str, options: dict[str, Any]) -> None:
    """Set the wallpaper for the current Space on one screen; raise on failure."""
    from Foundation import NSURL

    ok, err = workspace.setDesktopImageURL_forScreen_options_error_(
        NSURL.fileURLWithPath_(path), screen, options, None
    )
    if not ok:
        raise RuntimeError(f"setDesktopImageURL failed: {err}")


def path_forms(path: str | Path) -> set[str]:
    """Two spellings of a path: fully resolved, and with only its directory resolved.

    The second keeps a symlink's own name, e.g. SUPPORT_DIR/heatmap-x.png -> /elsewhere.
    """
    absolute = os.path.abspath(path)
    parent = os.path.realpath(os.path.dirname(absolute))
    return {os.path.realpath(absolute), os.path.join(parent, os.path.basename(absolute))}


def is_ours(path: str) -> bool:
    """True if path is inside our support dir, either as written or after resolving symlinks."""
    support = Path(os.path.realpath(SUPPORT_DIR))
    return any(Path(form).is_relative_to(support) for form in path_forms(path))


def json_safe_options(options: Any) -> dict[str, Any]:
    """Keep only int/float/bool/str values from desktopImageOptionsForScreen_."""
    safe: dict[str, Any] = {}
    for key in options or {}:
        value = options[key]
        for kind in (bool, int, float, str):  # bool first: it is a subclass of int
            if isinstance(value, kind):
                safe[str(key)] = kind(value)  # plain Python value; NSColor etc. are dropped
                break
    return safe


def validate_backup(obj: Any) -> list[dict[str, Any]]:
    """Check the shape of backup.json (SPEC §9) and return it; raise ValueError if wrong."""
    if not isinstance(obj, list):
        raise ValueError(f"backup.json must be a list, not {type(obj).__name__}")
    seen = set()
    for index, entry in enumerate(obj):
        where = f"backup.json entry {index}"
        if not isinstance(entry, dict):
            raise ValueError(f"{where} is not an object")
        did = entry.get("display")
        if not isinstance(did, int) or isinstance(did, bool):
            raise ValueError(f"{where}: 'display' must be an integer")
        if did in seen:
            raise ValueError(f"{where}: duplicate display {did}")
        seen.add(did)
        if "path" not in entry or not (entry["path"] is None or isinstance(entry["path"], str)):
            raise ValueError(f"{where}: 'path' must be a string or null")
        options = entry.get("options")
        if not isinstance(options, dict) or not all(isinstance(v, (int, float, bool, str)) for v in options.values()):
            raise ValueError(f"{where}: 'options' must be an object of int/float/bool/str values")
    return obj


def load_backup() -> list[dict[str, Any]] | None:
    """The validated backup.json, or None if the file does not exist (raises if invalid)."""
    if not BACKUP_FILE.exists():
        return None
    return validate_backup(read_json(BACKUP_FILE))


def ensure_backup(workspace: Any, screens: list[Any], state: dict[str, Any]) -> None:
    """Add the original wallpaper of each connected screen not yet in backup.json (SPEC §9).

    Existing entries are never modified (reinstalls keep the real original). Screens
    already showing one of our PNGs are skipped. Raises if a screen can't be read or
    backup.json is invalid; the caller then changes nothing.
    """
    entries = load_backup() or []
    known = {entry["display"] for entry in entries}
    added = []
    for screen in screens:
        did = display_id(screen)
        path = wallpaper_path(workspace, screen)
        if did in known or (path is not None and is_ours(path)):
            continue
        options = json_safe_options(workspace.desktopImageOptionsForScreen_(screen))
        added.append({"display": did, "path": path, "options": options})
    if added:
        atomic_write_json(BACKUP_FILE, entries + added)
        log.info("saved original wallpaper(s) of display(s) %s to %s", [e["display"] for e in added], BACKUP_FILE)
    # Never write an empty backup. True only when there is no backup at all because every
    # connected screen already shows one of our PNGs (install.py checks this).
    state["backup_not_needed"] = not entries and not added


def pngs_to_prune(existing: list[tuple[Path, float | None]], state: dict[str, Any], live_paths: list[str],
                  now: float) -> list[Path]:
    """Return the PNGs to delete, given (path, mtime) pairs; mtime is None if it couldn't be read.

    Protected, never deleted and never counted against the cap: every PNG that is current or
    previous for a display, and every one live on a screen. Paths are compared in both forms
    from path_forms(), on both sides: a symlinked alias of a live PNG protects its target, and
    a live symlink in our dir protects itself.

    Of the rest, the RETAIN_EXTRA newest rendered within RETAIN_DAYS are kept too, because a
    Space you are not on may still point at one (SPEC §6). A PNG whose mtime couldn't be read
    is kept. Ties on mtime are broken by name, so the result doesn't depend on input order.
    """
    digests = list(state["applied"].values()) + list(state["applied_prev"].values())
    protected: set[str] = set()
    for path in [SUPPORT_DIR / f"heatmap-{digest}.png" for digest in digests] + list(live_paths):
        protected |= path_forms(path)
    others = [(png, mtime) for png, mtime in existing if not (path_forms(png) & protected)]
    cutoff = now - RETAIN_DAYS * 86400
    recent = sorted(((png, mtime) for png, mtime in others if mtime is not None and mtime >= cutoff),
                    key=lambda item: (-item[1], item[0].name))
    retained = {png for png, _mtime in recent[:RETAIN_EXTRA]}
    return [png for png, mtime in others if mtime is not None and png not in retained]


def png_mtime(png: Path) -> float | None:
    """The PNG's own modification time (symlinks not followed), or None if it can't be read."""
    try:
        return os.lstat(png).st_mtime
    except OSError:
        return None


def prune(state: dict[str, Any], live_before: list[str] | None) -> None:
    """Delete unneeded heatmap-*.png files (see pngs_to_prune); skip if the screens can't be read."""
    if live_before is None:
        log.warning("could not read wallpapers before this run; not pruning")
        return
    try:
        screens, workspace = screens_and_workspace()
        live_now = [wallpaper_path(workspace, s) for s in screens]
    except Exception as exc:
        log.warning("could not read current wallpapers (%s); not pruning", exc)
        return
    live = [p for p in live_before + live_now if p]
    existing = [(png, png_mtime(png)) for png in sorted(SUPPORT_DIR.glob("heatmap-*.png"))]
    for png in pngs_to_prune(existing, state, live, time.time()):
        png.unlink(missing_ok=True)
        log.info("pruned %s", png.name)


def apply_to_screen(workspace: Any, screen: Any, cells: dict[str, int], state: dict[str, Any],
                    force: bool, theme: dict[str, Any]) -> None:
    """Render (if needed) and set one screen's wallpaper, then record it; raise on failure."""
    key = str(display_id(screen))
    width, height = backing_size(screen)
    digest = render_hash(cells, width, height, theme)
    png = SUPPORT_DIR / f"heatmap-{digest}.png"
    current = wallpaper_path(workspace, screen)
    up_to_date = (state["applied"].get(key) == digest and png.exists() and current is not None
                  and os.path.realpath(current) == os.path.realpath(png))
    if up_to_date and not force:
        log.info("display %s: up to date; skipped", key)
        return
    if force or not png.exists():
        atomic_write_bytes(png, render_png(cells, width, height, theme))
    set_wallpaper(workspace, screen, str(png), {})
    # Recorded only after the set succeeded. The hash being replaced becomes "previous".
    old = state["applied"].get(key)
    if old is not None and old != digest:
        state["applied_prev"][key] = old
    state["applied"][key] = digest
    atomic_write_json(STATE_FILE, state)
    log.info("display %s: set %s (%dx%d)", key, png.name, width, height)
    # Give macOS a moment to report the new path, so a run seconds later sees it as up to date.
    # The set already succeeded and is recorded, so a failure here is only worth a log line.
    try:
        if not wait_for_wallpaper(workspace, screen, str(png), APPLY_WAIT_SECONDS, POLL_INTERVAL_SECONDS):
            log.debug("display %s: macOS has not reported the new wallpaper yet", key)
    except Exception as exc:  # noqa: BLE001 - read-back only; never undo a successful set
        log.warning("display %s: could not read the wallpaper back after setting it (%s)", key, exc)


def apply_to_screens(cells: dict[str, int], state: dict[str, Any], force: bool, theme: dict[str, Any]) -> int:
    """Update every screen that needs it; return EXIT_OK, EXIT_ERROR or EXIT_SKIPPED."""
    screens, workspace = screens_and_workspace()
    if not screens:
        log.info("no screens available (login window or display asleep); nothing to do")
        return EXIT_SKIPPED
    try:
        ensure_backup(workspace, screens, state)
    except Exception as exc:
        log.error("could not save the original wallpapers (%s: %s); changing nothing", type(exc).__name__, exc)
        return EXIT_ERROR
    try:
        live_before: list[str] | None = [p for p in (wallpaper_path(workspace, s) for s in screens) if p]
    except Exception as exc:
        log.warning("could not read current wallpapers (%s)", exc)
        live_before = None
    failed = False
    for index, screen in enumerate(screens):
        try:
            apply_to_screen(workspace, screen, cells, state, force, theme)
        except Exception as exc:  # this screen's state stays unchanged; carry on with the others
            log.error("screen #%d: %s: %s", index, type(exc).__name__, exc)
            failed = True
    atomic_write_json(STATE_FILE, state)  # also persists backup_not_needed
    prune(state, live_before)
    return EXIT_ERROR if failed else EXIT_OK


# --- Restore and uninstall (SPEC §9, §11) ---------------------------------

def restore_from_backup(screens: list[Any], workspace: Any) -> int:
    """Set each display in backup.json back to its saved image; return the number of failures.

    Best-effort: only an existing static image outside our support dir can be restored.
    """
    entries = load_backup()  # raises on an invalid file, before any screen is touched
    if entries is None:
        log.info("no backup.json; nothing to restore")
        return 0
    by_id = {display_id(s): s for s in screens}
    failures = 0
    for entry in entries:
        did, path = entry["display"], entry["path"]
        screen = by_id.get(did)
        if screen is None and len(screens) == 1 and len(entries) == 1:
            screen = screens[0]  # same single display, new id (never guess with several entries)
        if screen is not None and isinstance(path, str) and os.path.isfile(path) and not is_ours(path):
            try:
                set_wallpaper(workspace, screen, path, entry["options"])
            except RuntimeError as exc:
                log.error("display %s: %s", did, exc)
            else:
                # The set succeeded; macOS may just be slow to report it, or the read-back
                # itself may fail. Neither undoes the restore, so warn and carry on.
                try:
                    if not wait_for_wallpaper(workspace, screen, path, RESTORE_WAIT_SECONDS, POLL_INTERVAL_SECONDS):
                        log.warning("display %s: macOS has not reported the restored path after %.0f s",
                                    did, RESTORE_WAIT_SECONDS)
                except Exception as exc:  # noqa: BLE001 - read-back only; never undo a successful set
                    log.warning("display %s: could not read the wallpaper back after restoring it (%s)", did, exc)
                log.info("display %s: restored %s", did, path)
                continue
        else:
            log.warning("display %s: cannot restore %r automatically", did, path)
        failures += 1
    return failures


def open_wallpaper_settings(workspace: Any) -> None:
    """Open System Settings > Wallpaper so the user can re-pick by hand."""
    from Foundation import NSURL

    workspace.openURL_(NSURL.URLWithString_(WALLPAPER_SETTINGS_URL))


def restore() -> int:
    """--restore: put back the wallpapers saved in backup.json; files are kept either way."""
    screens, workspace = screens_and_workspace()
    if restore_from_backup(screens, workspace):
        open_wallpaper_settings(workspace)
        log.warning("restore is best-effort for static images; opened Wallpaper settings - "
                    "please re-pick your wallpaper (backup.json kept)")
        return EXIT_ERROR
    return EXIT_OK


def delete_our_files() -> None:
    """Close the log file, then delete SUPPORT_DIR and every log file (the caller holds the lock)."""
    for handler in list(log.handlers):
        handler.close()
        log.removeHandler(handler)
    if SUPPORT_DIR.exists():
        shutil.rmtree(SUPPORT_DIR)  # LOCK_FILE is outside this tree, so the lock stays valid
    for path in LOG_FILES:
        path.unlink(missing_ok=True)


def print_cleanup_paths() -> None:
    """List every path uninstall deals with and whether it still exists."""
    print("Cleanup may be incomplete; check these paths:")
    for path in [LOCK_FILE, SUPPORT_DIR] + LOG_FILES:
        note = "  (kept on purpose)" if path == LOCK_FILE else ""
        print(f"  {'exists ' if path.exists() else 'missing'}  {path}{note}")


def displays_still_ours(screens: list[Any], workspace: Any) -> list[tuple[int, str]]:
    """(display id, path) for every screen that still shows one of our images.

    A screen that reads as ours gets up to RESTORE_WAIT_SECONDS to change first, because
    macOS reports a just-restored wallpaper late.
    """
    still_ours = []
    for screen in screens:
        path = wallpaper_path(workspace, screen)
        if path is None or not is_ours(path):  # as written or resolved, see is_ours
            continue
        if not wait_until_not_ours(workspace, screen, RESTORE_WAIT_SECONDS, POLL_INTERVAL_SECONDS):
            still_ours.append((display_id(screen), wallpaper_path(workspace, screen) or path))
    return still_ours


def uninstall() -> int:
    """--uninstall: restore, verify no screen still shows our image, then delete our files."""
    screens, workspace = screens_and_workspace()
    failures = restore_from_backup(screens, workspace)
    screens, workspace = screens_and_workspace()  # re-query after restoring
    if not screens:
        print("No screens are connected, so it can't be verified that none shows our image; nothing deleted.")
        log.warning("uninstall: no screens to verify; nothing deleted")
        return EXIT_ERROR
    still_ours = displays_still_ours(screens, workspace)
    if failures or still_ours:
        open_wallpaper_settings(workspace)  # once, for everything that needs re-picking
    if still_ours:
        for did, path in still_ours:
            print(f"Display {did} still shows {path}")
        print("Re-pick a wallpaper on those displays, then run uninstall.py again. Nothing was deleted.")
        log.warning("uninstall: %d display(s) still show our image; nothing deleted", len(still_ours))
        return EXIT_ERROR
    if failures:
        print(f"Note: {failures} of the saved wallpapers could not be restored (e.g. their display "
              "is not connected).")
    try:
        delete_our_files()
    except OSError as exc:  # e.g. a log unlink failed after the support dir was removed
        print(f"Deleting files failed: {exc}")
        print_cleanup_paths()
        return EXIT_ERROR
    print(f"Deleted {SUPPORT_DIR} and ~/Library/Logs/gh-heatmap*.log* "
          f"(kept the empty lock file {LOCK_FILE})")
    return EXIT_OK


# --- Space watcher (SPEC §13; opt-in, the only resident mode) -------------

WATCH_DEBOUNCE_SECONDS = 1.0     # let a swipe across several Spaces settle before refreshing
WATCH_RETRY_SECONDS = 5.0        # lock busy: try again this much later...
WATCH_RETRY_ATTEMPTS = 12        # ...this many times (about a minute), then wait for the next change
REFRESH_DONE, REFRESH_RETRY, REFRESH_STOP = "done", "retry", "stop"


def refresh_from_cache() -> str:
    """Re-apply the cached graph to every screen's current Space; never makes a network request.

    Takes the lock for the duration. Returns REFRESH_RETRY if another run holds it,
    REFRESH_STOP if config.json is gone (uninstalled), else REFRESH_DONE. Errors are logged,
    never raised (lock errors included), so one bad refresh can't stop the watcher.
    """
    lock_fd = None
    try:
        lock_fd = acquire_lock()
        if lock_fd is None:
            return REFRESH_RETRY
        config = load_config()
        if config is None:
            log.info("watch: config.json is gone (uninstalled?); stopping")
            return REFRESH_STOP
        username = resolve_username(None, config)
        theme = resolve_theme(saved_theme_name(config))
        state = load_state(username)
        if not state.get("cells"):
            log.info("watch: no cached graph yet; the next scheduled run will set it")
            return REFRESH_DONE
        cells = validate_cells(state["cells"], dt.date.today())
        apply_to_screens(cells, state, force=False, theme=theme)
        return REFRESH_DONE
    except Exception as exc:  # noqa: BLE001 - logged; the watcher keeps running
        log.error("watch: refresh failed: %s: %s", type(exc).__name__, exc)
        return REFRESH_DONE
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


class SpaceChangeDebouncer:
    """Turns a burst of Space-change notifications into one refresh, retrying while the lock is busy.

    `schedule(delay, fn)` runs fn on the main thread after `delay` seconds; `refresh()` returns a
    REFRESH_* value; `stop()` ends the event loop. Kept free of AppKit so it can be tested.
    """

    def __init__(self, schedule: Callable[[float, Callable[[], None]], None],
                 refresh: Callable[[], str], stop: Callable[[], None]) -> None:
        self.schedule, self.refresh, self.stop = schedule, refresh, stop
        self.generation = 0      # bumped by every change; a callback from an older one does nothing
        self.attempts = 0

    def space_changed(self) -> None:
        self.attempts = 0
        self._arm(WATCH_DEBOUNCE_SECONDS)

    def _arm(self, delay: float) -> None:
        self.generation += 1
        generation = self.generation
        self.schedule(delay, lambda: self._fire(generation))

    def _fire(self, generation: int) -> None:
        if generation != self.generation:
            return  # superseded by a later change or retry
        result = self.refresh()
        if result == REFRESH_STOP:
            self.stop()
        elif result == REFRESH_RETRY:
            self.attempts += 1
            if self.attempts < WATCH_RETRY_ATTEMPTS:
                self._arm(WATCH_RETRY_SECONDS)
            else:
                log.warning("watch: lock stayed busy; waiting for the next Space change")
                self.attempts = 0
        else:
            self.attempts = 0


def watch_spaces() -> int:
    """Stay resident; after each active-Space change, refresh from cache (SPEC §13).

    Uses only public AppKit API: NSWorkspace's NSWorkspaceActiveSpaceDidChangeNotification.
    Exits 0 when not installed (no config.json) or when asked to stop (SIGTERM from launchctl
    bootout, SIGINT), so launchd's KeepAlive {SuccessfulExit: false} does not restart it.
    Exits 1 if the event loop ends on its own, so launchd does restart it.
    """
    if load_config() is None:
        log.info("watch: not installed (no config.json); exiting")
        return EXIT_OK
    import signal

    from AppKit import (NSApplication, NSApplicationActivationPolicyProhibited, NSEvent,
                        NSEventTypeApplicationDefined, NSWorkspace,
                        NSWorkspaceActiveSpaceDidChangeNotification)
    from PyObjCTools import AppHelper, MachSignals

    # macOS delivers the Space-change notification only to a process running NSApplication's
    # own event loop (tested on macOS 27.2: a plain NSRunLoop, as PyObjC's runConsoleEventLoop
    # gives, receives other NSWorkspace notifications but never this one). Prohibited policy:
    # no Dock icon, no menu bar, never activated.
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyProhibited)
    stopping = False

    def stop() -> None:
        """Make app.run() return (terminate_ would exit() without cleanup)."""
        nonlocal stopping
        stopping = True
        app.stop_(None)
        # stop_ takes effect after the next event, so post one to wake the loop now.
        app.postEvent_atStart_(NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            NSEventTypeApplicationDefined, (0, 0), 0, 0, 0, None, 0, 0, 0), True)

    # Delivered through a mach port on the run loop, so the handler runs between refreshes
    # (a refresh in progress finishes first) and wakes the idle loop at once.
    for signum in (signal.SIGTERM, signal.SIGINT):
        MachSignals.signal(signum, lambda _signum: stop())
    debouncer = SpaceChangeDebouncer(schedule=lambda delay, fn: AppHelper.callLater(delay, fn),
                                     refresh=refresh_from_cache, stop=stop)
    center = NSWorkspace.sharedWorkspace().notificationCenter()
    token = center.addObserverForName_object_queue_usingBlock_(
        NSWorkspaceActiveSpaceDidChangeNotification, None, None, lambda _note: debouncer.space_changed())
    log.info("watch: watching for Space changes (pid %d)", os.getpid())
    try:
        app.run()  # sleeps until a notification, timer, signal or event arrives
    finally:
        center.removeObserver_(token)
    if not stopping:
        log.error("watch: event loop ended unexpectedly; exiting so launchd restarts it")
        return EXIT_ERROR
    log.info("watch: stopped")
    return EXIT_OK


# --- Entry points (SPEC §3) ------------------------------------------------

def load_config() -> dict[str, Any] | None:
    """config.json as a dict, or None if it does not exist; ValueError if it is not a JSON object."""
    config = read_json(CONFIG_FILE)
    if config is not None and not isinstance(config, dict):
        raise ValueError(f"{CONFIG_FILE} is not a JSON object")
    return config


def resolve_username(cli_user: str | None, config: dict[str, Any] | None) -> str:
    """Username from --user, else from config.json; always validated."""
    if cli_user is not None:
        return validate_username(cli_user)
    if config is None or "username" not in config:
        raise ValueError("no username: pass --user NAME or run install.py")
    return validate_username(config["username"])


def saved_theme_name(config: dict[str, Any] | None) -> str | None:
    """The normalized theme saved in config.json (None = default); ValueError if it is unknown."""
    return normalize_theme_name(config.get("theme")) if config is not None else None


def save_theme(config: dict[str, Any] | None, name: str | None) -> None:
    """Persist `--theme` in config.json, keeping every other key. Call only under the lock."""
    if config is None or "username" not in config:
        raise ValueError("not installed: run install.py --user NAME --theme THEME instead")
    validate_username(config["username"])
    updated = {key: value for key, value in config.items() if key != "theme"}
    if name is not None:
        updated["theme"] = name
    atomic_write_json(CONFIG_FILE, updated)
    log.info("theme set to %s", name or RESET_THEME)


def screens_available() -> bool:
    """True if at least one screen is connected (false at the login window or when asleep)."""
    screens, _workspace = screens_and_workspace()
    return bool(screens)


def update(username: str, theme: dict[str, Any], force: bool, dry: bool) -> int:
    """Fetch, then refresh stale screens (or, with dry=True, only write preview.png).

    force=True ignores the ETag and up-to-date checks and re-renders and re-applies on
    every display, but still honours a recorded GitHub backoff and falls back to cached
    data if the network fails.
    """
    state = load_state(username)
    if rate_limited(state):
        if dry:
            log.error("dry run: no preview written: GitHub backoff until %s", state["retry_until"])
            return EXIT_ERROR
        log.info("rate limited until %s; skipping", state["retry_until"])
        return EXIT_SKIPPED
    # Checked before fetching, so no request is made when nothing could be set.
    if not dry and not screens_available():
        log.info("no screens available (login window or display asleep); nothing to do")
        return EXIT_SKIPPED
    cells = get_cells(username, state, use_etag=not force)
    if not dry:
        atomic_write_json(STATE_FILE, state)  # persist etag / cells / retry_until
    if cells is None:  # GitHub answered 429/503
        if dry:
            log.error("dry run: no preview written: GitHub asked us to back off")
            return EXIT_ERROR
        return EXIT_SKIPPED
    if not dry:
        return apply_to_screens(cells, state, force, theme)
    from AppKit import NSScreen  # dry run: render at the main screen's size, touch nothing else

    screen = NSScreen.mainScreen()
    width, height = backing_size(screen) if screen is not None else FALLBACK_SIZE
    atomic_write_bytes(PREVIEW_FILE, render_png(cells, width, height, theme))
    log.info("dry run: wrote %s (%dx%d, %d cells); wallpaper unchanged", PREVIEW_FILE, width, height, len(cells))
    return EXIT_OK


def resolve_run(args: argparse.Namespace) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
    """Read config.json once and validate the username and theme for this run, before any
    fetch or state write. Returns (username, theme colours, config)."""
    config = load_config()
    username = resolve_username(args.user, config)
    name = args.theme_name if args.theme is not None else saved_theme_name(config)
    return username, resolve_theme(name), config


def run(args: argparse.Namespace) -> int:
    """Dispatch to the requested mode, holding the lock where state is touched."""
    if args.dry_run:  # writes only preview.png (and the log), so no lock is needed; saves nothing
        username, theme, _config = resolve_run(args)
        return update(username, theme, force=False, dry=True)
    if args.watch_spaces:  # resident; takes the lock only around each refresh
        return watch_spaces()
    lock_fd = acquire_lock()
    if lock_fd is None:
        log.info("another run holds the lock; exiting")
        return EXIT_LOCK_BUSY if (args.restore or args.uninstall) else EXIT_SKIPPED
    try:  # the lock is held for the whole mode, including --uninstall's deletion
        if args.restore:  # restore and uninstall never read config.json, so a bad theme can't block them
            return restore()
        if args.uninstall:
            return uninstall()
        username, theme, config = resolve_run(args)
        if args.theme is not None:
            # Saved before fetching: config.json records the choice, state.json's `applied`
            # records progress, so a skipped or failed run is completed by a later one.
            save_theme(config, args.theme_name)
        code = update(username, theme, force=args.force, dry=False)
        if args.theme is not None and code == EXIT_SKIPPED:
            log.info("theme saved; it will be applied on the next successful run")
        return code
    finally:
        os.close(lock_fd)


class ArgumentParser(argparse.ArgumentParser):
    """argparse, except usage errors exit 1: exit 2 means "skipped" (SPEC §3)."""

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(EXIT_ERROR, f"{self.prog}: error: {message}\n")


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, run, and turn any error into one log line and exit code 1."""
    parser = ArgumentParser(description="GitHub contribution graph as your macOS wallpaper.")
    parser.add_argument("--user", metavar="NAME", help="override the username in config.json")
    parser.add_argument("--theme", metavar="NAME",
                        help="change the colour theme: saved to config.json and applied now; with "
                             "--dry-run, only previewed; 'default' is the green look (see --list-themes)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="render preview.png only; do not set the wallpaper")
    mode.add_argument("--force", action="store_true",
                      help="ignore ETag and up-to-date checks; re-render and re-apply on every display; "
                           "still honours a recorded GitHub backoff and falls back to cached data "
                           "if the network fails")
    mode.add_argument("--restore", action="store_true", help="restore the wallpaper(s) saved in backup.json")
    mode.add_argument("--uninstall", action="store_true",
                      help="restore, verify no screen shows our image, then delete all our files "
                           "(used by uninstall.py)")
    mode.add_argument("--list-themes", action="store_true", help="print the built-in theme names and exit")
    mode.add_argument("--watch-spaces", action="store_true",
                      help="stay running and re-apply the cached graph whenever the active Space "
                           "changes; no network requests (install.py --watch-spaces sets this up)")
    args = parser.parse_args(argv)
    if args.watch_spaces and (args.user is not None or args.theme is not None):
        parser.error("--watch-spaces takes no other options; it uses config.json")
    # Handled before logging starts, so these touch no files at all.
    if args.list_themes:
        print("\n".join(sorted(THEMES)))
        return EXIT_OK
    if args.theme is not None:
        if args.restore or args.uninstall:
            parser.error("--theme cannot be used with --restore or --uninstall")
        try:
            args.theme_name = normalize_theme_name(args.theme)
        except ValueError as exc:
            parser.error(str(exc))
    setup_logging()
    try:
        return run(args)
    except Exception as exc:
        log.error("failed: %s: %s", type(exc).__name__, exc)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())

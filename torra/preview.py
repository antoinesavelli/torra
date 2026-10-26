"""What a step will act on, looked up on disk, for the tick prompt to show.

Depends on: torra.core (Step), torra.path_warnings (named_paths). Read-only and advisory, like
`path_warnings`: it names each absolute local path a step uses (and any it declares in `touches`)
and says what is there, so the operator ticking `rm -r <path>` sees "directory, 41 files, 229 MiB"
and not just a command line. It never follows a symlink, never reads a file's contents, and stops
counting a huge tree at a limit, so it cannot hang the prompt. It describes the paths named in the
plan; it cannot see what an earlier step will discover or what a variable will expand to.
"""

from __future__ import annotations

import os
import stat
import time
from collections.abc import Callable

from .core import Step
from .path_warnings import named_paths

__all__ = ["describe_path", "preview_targets"]

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB")
_SPECIAL = (
    (stat.S_ISBLK, "block device"),
    (stat.S_ISCHR, "character device"),
    (stat.S_ISFIFO, "named pipe"),
    (stat.S_ISSOCK, "socket"),
)


def _size(count: float) -> str:
    unit = 0
    while count >= 1024 and unit < len(_UNITS) - 1:
        count /= 1024
        unit += 1
    return f"{int(count)} B" if unit == 0 else f"{count:.1f} {_UNITS[unit]}"


def _day(stamp: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(stamp))


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _walk(
    root: str, limit: int, seconds: float, clock: Callable[[], float]
) -> tuple[int, int, int, float, bool, bool]:
    """(files, folders, bytes, newest file mtime, stopped early, some folder unreadable) under
    `root`. Symlinks count as entries and are never entered."""
    files = folders = size = 0
    newest = 0.0
    stopped = unreadable = False
    started = clock()
    pending = [root]
    while pending and not stopped:
        try:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    if files + folders >= limit or clock() - started > seconds:
                        stopped = True
                        break
                    if entry.is_dir(follow_symlinks=False):
                        folders += 1
                        pending.append(entry.path)
                        continue
                    files += 1
                    info = entry.stat(follow_symlinks=False)
                    size += info.st_size
                    newest = max(newest, info.st_mtime)
        except OSError:
            unreadable = True
    return files, folders, size, newest, stopped, unreadable


def describe_path(
    path: str, limit: int = 5000, seconds: float = 1.0, clock: Callable[[], float] = time.monotonic
) -> str:
    """One line on what is at `path`, without following a symlink."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "does not exist"
    if stat.S_ISLNK(info.st_mode):
        return f"symlink to {os.readlink(path)}"
    for is_kind, name in _SPECIAL:
        if is_kind(info.st_mode):
            return name
    if not stat.S_ISDIR(info.st_mode):
        return f"file, {_size(info.st_size)}, modified {_day(info.st_mtime)}"
    return _describe_dir(path, limit, seconds, clock)


def _describe_dir(path: str, limit: int, seconds: float, clock: Callable[[], float]) -> str:
    """What the directory at `path` holds, as counted by `_walk`."""
    files, folders, size, newest, stopped, unreadable = _walk(path, limit, seconds, clock)
    if not files and not folders and not stopped:
        return "empty directory" + (", some folders unreadable" if unreadable else "")
    more = "at least " if stopped else ""
    text = f"directory, {more}{_plural(files, 'file')}"
    text += f" in {_plural(folders, 'folder')}" if folders else ""
    text += f", {more}{_size(size)}" + (f", newest change {_day(newest)}" if newest else "")
    return (
        text
        + (", stopped counting" if stopped else "")
        + (", some folders unreadable" if unreadable else "")
    )


def preview_targets(
    step: Step, describe: Callable[[str], str] = describe_path, limit: int = 8
) -> list[str]:
    """`path: what is there` for every local path the step names or declares, cut at `limit`."""
    if step.kind in ("remote_pull", "remote_push"):
        return []
    paths = named_paths(step.command, step.touches)
    lines = []
    for path in paths[:limit]:
        try:
            lines.append(f"{path}: {describe(path)}")
        except OSError as exc:
            lines.append(f"{path}: cannot be inspected ({exc})")
    if len(paths) > limit:
        lines.append(f"... and {len(paths) - limit} more")
    return lines

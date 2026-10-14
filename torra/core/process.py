"""How a step's command actually runs: bash in its own process group, with errors that stop it.

Split out of tasks.py when the wrapper, group kill and output cap pushed it past the 500-line cap
(HANDOFF.md §2.61).

Depends on: the standard library only; `tasks.shell()` calls `run_in_group`.
"""

from __future__ import annotations

import collections
import os
import re
import threading
from collections.abc import Callable
from typing import Any

#: Runs when any command fails outside a condition, so a step stops at the first real error
#: instead of reporting the exit of its last line (`false; true` used to succeed). It applies the
#: same rule as the closing check in `pipefail_wrapped`: a SIGPIPE (141) upstream of a stage
#: that exited 0 is forgiven, anything else exits with the rightmost non-zero status.
_ERR_TRAP = (
    '_ps=("${PIPESTATUS[@]}"); _l=$((${#_ps[@]}-1)); _ec=0; '
    'for _i in "${_ps[@]}"; do if [ "$_i" -ne 0 ]; then _ec=$_i; fi; done; '
    'if [ "${_ps[$_l]}" -eq 0 ]; then _ok=1; for _i in "${_ps[@]:0:$_l}"; do '
    'if [ "$_i" -ne 0 ] && [ "$_i" -ne 141 ]; then _ok=0; fi; done; '
    'if [ "$_ok" -eq 1 ]; then _ec=0; fi; fi; '
    'if [ "$_ec" -ne 0 ]; then exit "$_ec"; fi'
)


def pipefail_wrapped(command: str) -> str:
    """`command`, prefixed with `set -o pipefail` and suffixed with a check
    that forgives a `SIGPIPE` (exit 128+13=141) on any non-final pipeline
    stage when the final stage genuinely exited 0.

    `pipefail` alone is exactly right for its original purpose (`sudo
    something | sort | head` reporting success with empty output when
    `sudo` alone fails, undetectable without it) -- but it is *also* right
    for a case nobody wanted caught: `du -ah /home | sort -rh | head -20`,
    an entirely ordinary, benign command, fails under plain `pipefail`
    because `head` closes its input after 20 lines, `sort`/`du` upstream
    get killed by `SIGPIPE` for writing to a closed pipe, and `pipefail`
    reports that as the pipeline's own failure even though `head` already
    produced exactly the right output. Found live: a real operator hit
    this exact command shape four times in a row, including retrying with
    a different phrasing and asking torra to diagnose its own bug,
    getting the identical failure every time.

    Distinguishing the two needs per-stage exit codes (`$PIPESTATUS`), not
    just the pipeline's own overall `$?` -- and capturing `$PIPESTATUS`
    has its own trap: bash resets it after *any* subsequent simple
    command, including a bare variable assignment, so `ec=$?` immediately
    destroys it before it can be read. The array capture below has to be
    the first statement after `command` runs, with nothing (not even
    reading `$?`) in between; `_ec` is then computed by hand from that
    array using the identical rule `pipefail` itself uses (the rightmost
    non-zero stage, or 0 if all stages succeeded), so this doesn't depend
    on bash's own already-reset `$?` at all. `_ec` is only overridden back
    to 0 when the final stage is 0 *and* every earlier non-zero stage is
    specifically 141 -- a real failure anywhere in the pipe (auth failure,
    missing command, wrong flag) is never forgiven, verified against the
    exact `sudo | sort | head` scenario `pipefail` exists for in the first
    place, not just the new benign case.
    """
    return (
        "set -o pipefail\n"
        "set -E\n"
        f"trap '{_ERR_TRAP}' ERR\n"
        + command + "\n"
        "_rc=$? _ps=(\"${PIPESTATUS[@]}\")\n"
        "if [ \"$_rc\" -eq 0 ]; then exit 0; fi\n"
        "_last=$((${#_ps[@]}-1))\n"
        "_ec=0\n"
        "for _i in \"${_ps[@]}\"; do\n"
        "  if [ \"$_i\" -ne 0 ]; then _ec=$_i; fi\n"
        "done\n"
        "if [ \"$_ec\" -ne 0 ] && [ \"${_ps[$_last]}\" -eq 0 ]; then\n"
        "  _ok=1\n"
        "  for _i in \"${_ps[@]:0:$_last}\"; do\n"
        "    if [ \"$_i\" -ne 0 ] && [ \"$_i\" -ne 141 ]; then _ok=0; fi\n"
        "  done\n"
        "  if [ \"$_ok\" -eq 1 ]; then _ec=0; fi\n"
        "fi\n"
        "exit \"$_ec\"\n"
    )


def shell_syntax_error(command: str, timeout: float = 5.0) -> str | None:
    """Why bash cannot parse `command`, in bash's own words, or None when it can.

    Steps run as `bash -c`, so `bash -n -c` is the same parser answering without running
    anything. None also means "no opinion" (bash missing, or too slow): this is a courtesy ahead
    of the real gates, never one of them. The environment is cut down so a `BASH_ENV` file is not
    sourced by the checker, and `LC_ALL=C` keeps the wording the same on every machine.
    """
    import subprocess

    try:
        done = subprocess.run(
            ["/bin/bash", "-n", "-c", command], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = done.stderr.strip().splitlines()
    if done.returncode == 0 or not lines:
        return None
    return re.sub(r"^\S*bash: -c: ", "", lines[0])


#: Variable names `cli` loaded from the repo's `.env` and the operator never exported. Torra
#: reads them in-process; a step's subprocess does not inherit them (HANDOFF.md §2.68).
WITHHELD_ENV: set[str] = set()

_OUTPUT_HEAD = 256 * 1024
_OUTPUT_TAIL = 256 * 1024


class _CappedText:
    """The first and last stretch of a stream, so a runaway command cannot fill memory."""

    def __init__(self) -> None:
        self.head: list[str] = []
        self.head_size = 0
        self.tail: collections.deque[str] = collections.deque()
        self.tail_size = 0
        self.dropped = 0

    def add(self, line: str) -> None:
        if self.head_size < _OUTPUT_HEAD:
            self.head.append(line)
            self.head_size += len(line)
            return
        self.tail.append(line)
        self.tail_size += len(line)
        while self.tail_size > _OUTPUT_TAIL:
            gone = self.tail.popleft()
            self.tail_size -= len(gone)
            self.dropped += len(gone)

    def text(self) -> str:
        middle = f"\n[... {self.dropped} characters omitted ...]\n" if self.dropped else ""
        return "".join(self.head) + middle + "".join(self.tail)


def _kill_group(proc: Any) -> None:
    """Stop a step and everything it started: TERM its process group, then KILL what is left."""
    import signal
    import subprocess
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def _consume(lines: Any, kept: dict[str, _CappedText], open_streams: int, deadline: float,
             command: str, timeout: float | None,
             on_output: Callable[[str], None] | None) -> None:
    """Drain both reader threads' queue until each stream has closed, or raise on the deadline."""
    import queue
    import subprocess
    import time as _time

    while open_streams > 0:
        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(command, timeout or 3600)
        try:
            item = lines.get(timeout=min(remaining, 0.5))
        except queue.Empty:
            continue
        if item is None:
            open_streams -= 1
            continue
        tag, line = item
        kept[tag].add(line)
        if on_output is not None:
            on_output(line)


def run_in_group(command: str, cwd: str | None, timeout: float | None,
                  on_output: Callable[[str], None] | None) -> str:
    """Run `command` under bash in its own process group; raise on a non-zero exit.

    The group is what makes a timeout or Ctrl-C stop the step's children too (a bare
    `proc.kill()` left an `apt` running while rollback started), and stdin is closed so a
    step that wants a prompt gets EOF instead of hanging a background process. Output is kept
    to its head and tail. `on_output`, when given, sees each line as it arrives.

    stdout and stderr are captured into two separate pipes, so the return value on success
    and the exception message on failure mean the same thing whether or not a caller watched
    live. Reading two live pipes without deadlocking (a full stderr pipe while
    blocked reading stdout, or vice versa) needs one reader thread per
    pipe; both feed one queue so `on_output` still sees lines in something
    close to real arrival order rather than "all of stdout, then all of
    stderr". The timeout is enforced by the queue-read loop itself
    (`subprocess.run`'s own `timeout=` has no equivalent when reading
    incrementally), not by `Popen.wait`, so a process that never closes its
    output pipes still gets killed on schedule.
    """
    import queue
    import subprocess
    import time as _time

    # Explicit bash with pipefail, not the default /bin/sh (dash here): dash's pipeline exit
    # status is only the *last* command's, so `sudo something | sort | head` reports success
    # with empty output when sudo alone fails. See pipefail_wrapped for the rest.
    proc = subprocess.Popen(
        # Live wire: HANDOFF.md §5.16
        ["/bin/bash", "-c", pipefail_wrapped(command)],
        cwd=cwd, env={k: v for k, v in os.environ.items() if k not in WITHHELD_ENV},
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", start_new_session=True)
    assert proc.stdout is not None and proc.stderr is not None

    lines: queue.Queue[tuple[str, str] | None] = queue.Queue(maxsize=4096)

    def pump(stream: Any, tag: str) -> None:
        # The end-of-stream marker is sent even if reading fails: without it the consumer waits
        # for a stream that will never close, and a finished command reports a timeout.
        try:
            for line in iter(lambda: stream.readline(65536), ""):
                lines.put((tag, line))
        finally:
            stream.close()
            lines.put(None)

    readers = [
        threading.Thread(target=pump, args=(proc.stdout, "out"), daemon=True),
        threading.Thread(target=pump, args=(proc.stderr, "err"), daemon=True),
    ]
    for reader in readers:
        reader.start()

    kept = {"out": _CappedText(), "err": _CappedText()}
    try:
        _consume(lines, kept, len(readers), _time.monotonic() + (timeout or 3600),
                 command, timeout, on_output)
    except BaseException:
        # A timeout, Ctrl-C or a failing callback: leave nothing of the step running.
        _kill_group(proc)
        for reader in readers:
            reader.join(timeout=1)
        raise

    for reader in readers:
        reader.join()
    try:
        returncode = proc.wait(timeout=5)  # both pipes closed: reap it
    except subprocess.TimeoutExpired:
        _kill_group(proc)  # it closed its pipes but is still running
        raise

    stdout, stderr = kept["out"].text(), kept["err"].text()
    if returncode != 0:
        raise RuntimeError(f"exit {returncode}: {(stderr or stdout).strip()[:400]}")
    return stdout.strip()

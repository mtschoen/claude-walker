"""Sanctioned subprocess wrapper - THE ONLY module allowed to call subprocess.

Vendored from schoen-lab `packages/process_safe/src/process_safe/process.py`
at commit `32a52ba6d52158f3b39bebdfbd4df0282aff226a`. agent-walker is a
standalone repo (not a schoen-lab workspace member), so this is a file copy
rather than a path dependency; re-sync manually if the upstream module
changes.

The reason it exists is bpo-31935: ``subprocess.run(capture_output=True,
timeout=...)`` can wedge *forever* on Windows when the child spawns a
grandchild that inherits the stdout pipe - CPython's timeout kills only the
direct child, the grandchild holds the pipe open, and the call never returns.
See ``~/.claude/notes/reference_python_subprocess_timeout_leak.md`` for the
incident history (projdash's `add_task` hung ~4h on this before moving to
pygit2 for its git write path).

``run_captured`` dodges the whole class by reading the child's pipes in a
daemon thread we can *abandon*: on timeout we kill the child and raise, rather
than blocking on the read.

Deviation from upstream: this copy adds ``encoding``/``errors`` passthrough to
``run_captured`` (upstream always uses the platform-default text codec). The
walker binary's stdout/stderr must decode as UTF-8 regardless of the calling
process's locale/codepage - critical on Windows, where the default text mode
codec is often a legacy codepage, not UTF-8.

New code in this repo that needs to shell out must call ``run_captured`` /
``run_inherit`` here - never ``subprocess`` directly.
"""

from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass


def _windows_hidden_kwargs() -> dict[str, object]:
    """creationflags + startupinfo that keep a child's console window hidden.

    ``CREATE_NO_WINDOW`` alone is not enough on Windows 11 / Windows Terminal,
    which frequently ignore it and still flash a console for ``python.exe`` /
    ``git.exe`` children; a ``STARTUPINFO`` carrying ``SW_HIDE`` is layered on
    top. ``getattr`` guards keep the ``nt`` arm exercisable on any host.
    """
    kwargs: dict[str, object] = {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }
    startupinfo_type = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_type is not None:
        startupinfo = startupinfo_type()
        startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
        startupinfo.wShowWindow = 0  # SW_HIDE
        kwargs["startupinfo"] = startupinfo
    return kwargs


@dataclass(frozen=True)
class ProcessResult:
    """Captured outcome of a finished command."""

    returncode: int
    stdout: str
    stderr: str


class ProcessTimeout(Exception):
    """Raised when a captured command does not finish within its timeout.

    Carries ``command`` and ``timeout`` so callers can build their own messages.
    """

    def __init__(self, command: list[str], timeout: float) -> None:
        self.command = command
        self.timeout = timeout
        super().__init__(f"command timed out after {timeout}s: {command}")


def run_captured(
    command: list[str],
    *,
    cwd: str | None = None,
    timeout: float,
    text: bool = True,
    encoding: str | None = None,
    errors: str | None = None,
    env: dict[str, str] | None = None,
    check: bool = False,
) -> ProcessResult:
    """Run *command*, capturing stdout+stderr, with a timeout that can't wedge.

    Reads the child's pipes in a daemon thread; on timeout the child is killed
    and ``ProcessTimeout`` is raised rather than blocking on the read
    (bpo-31935). Launch failures (``OSError`` / ``FileNotFoundError`` from
    ``Popen``) propagate to the caller, matching plain ``subprocess`` semantics.

    *encoding*/*errors*, when given, are passed straight to ``Popen`` (see
    module docstring - this repo's callers need explicit UTF-8 decoding).
    *env*, when given, replaces the child's environment (same semantics as
    ``subprocess``); ``None`` inherits the parent's. *check*, when ``True``,
    raises ``subprocess.CalledProcessError`` on a non-zero exit (with stdout/
    stderr attached) so callers that relied on ``subprocess.run(check=True)``
    can keep their existing ``except CalledProcessError`` handling.
    """
    hide_kwargs: dict[str, object] = {}
    if os.name == "nt":
        hide_kwargs = _windows_hidden_kwargs()
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        encoding=encoding,
        errors=errors,
        env=env,
        close_fds=True,
        **hide_kwargs,
    )

    captured: dict[str, str] = {}

    def reader() -> None:
        try:
            out, err = process.communicate()
            captured["out"] = out or ""
            captured["err"] = err or ""
        except Exception:  # pragma: no cover - defensive; pipe vanished mid-read
            pass

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    thread.join(timeout)

    if thread.is_alive():
        try:
            process.kill()
        except OSError:  # pragma: no cover - child already gone
            pass
        raise ProcessTimeout(command, timeout)

    result = ProcessResult(
        returncode=process.returncode,
        stdout=captured.get("out", ""),
        stderr=captured.get("err", ""),
    )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            command,
            output=result.stdout,
            stderr=result.stderr,
        )
    return result


def run_inherit(command: list[str]) -> int:
    """Run *command* with inherited stdio (streams to the console); return rc.

    For interactive/administrative commands where the operator should see
    live output and nothing is captured - so the bpo-31935 pipe-inheritance
    hang does not apply.
    """
    return subprocess.run(command).returncode

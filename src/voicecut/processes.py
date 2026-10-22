"""Process trees that stop together, on macOS, Linux and Windows.

VoiceCut runs its stages as child processes (Whisper, CTC alignment, MFA,
FFmpeg) and the editor runs each Voice Cut as a worker process.  Stopping one
must stop everything it started, and a process whose parent died must not
keep running.  POSIX does this with process groups and signals; Windows has
neither, so there a whole tree is ended with ``taskkill /T`` and a parent is
watched through a handle to it.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

WINDOWS = sys.platform == "win32"
PARENT_POLL_SECONDS = 1.0


def own_group() -> dict[str, Any]:
    """``Popen`` arguments that make the child the root of its own tree."""

    if WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def terminate_tree(process: subprocess.Popen[Any], *, force: bool = False) -> None:
    """End ``process`` and everything it started.

    On POSIX the process group gets SIGTERM (SIGKILL with ``force``), so its
    members can clean up.  Windows cannot ask politely: the tree is killed.
    """

    if process.poll() is not None:
        return
    if WINDOWS:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        return
    signum = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.killpg(os.getpgid(process.pid), signum)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(OSError):
            if force:
                process.kill()
            else:
                process.terminate()


def _windows_wait_for_exit(pid: int) -> Callable[[], None] | None:
    """A blocking wait for the process ``pid`` to end, or None if it is gone."""

    import ctypes

    synchronize = 0x00100000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        return None

    def wait() -> None:
        kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 0xFFFFFFFF)
        kernel32.CloseHandle(ctypes.c_void_p(handle))

    return wait


def when_parent_exits(
    parent_pid: int, callback: Callable[[], None]
) -> threading.Thread:
    """Call ``callback`` (on a daemon thread) once ``parent_pid`` has exited."""

    def watch() -> None:
        if WINDOWS:
            # Windows keeps reporting the original parent id after it exits.
            wait = _windows_wait_for_exit(parent_pid)
            if wait is not None:
                wait()
        else:
            while os.getppid() == parent_pid:
                time.sleep(PARENT_POLL_SECONDS)
        callback()

    thread = threading.Thread(target=watch, daemon=True, name="watch-parent")
    thread.start()
    return thread


def keep_awake() -> Any:
    """Keep the computer from idle sleep while this process runs.

    macOS: ``caffeinate -w``; Linux: ``systemd-inhibit`` when systemd runs;
    Windows: ``SetThreadExecutionState`` for this (main) thread.  Each lets go
    when the process exits, however it exits.
    """

    if WINDOWS:
        import ctypes

        continuous, system_required = 0x80000000, 0x00000001
        with contextlib.suppress(OSError, AttributeError):
            ctypes.windll.kernel32.SetThreadExecutionState(continuous | system_required)
        return None
    pid = str(os.getpid())
    if sys.platform == "darwin":
        command = ["caffeinate", "-i", "-w", pid]
    else:
        command = [
            "systemd-inhibit", "--what=idle:sleep", "--who=VoiceCut",
            "--why=Voice Cut is editing a recording", "--mode=block",
            "tail", f"--pid={pid}", "-f", "/dev/null",
        ]  # fmt: skip
    with contextlib.suppress(OSError):
        return subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    return None

"""Process trees that stop together, on every system (voicecut.processes)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from voicecut import processes

WORKER = """
import signal, subprocess, sys
from voicecut import processes
from voicecut.editor import worker

def stop(*_):
    raise worker.WorkerCancelled

signal.signal(signal.SIGTERM, stop)
worker.cancel_on_stdin()
stage = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                         **processes.own_group())
worker._registry.set(stage)
try:
    stage.wait()
    sys.exit(1)
except worker.WorkerCancelled:
    sys.exit(7)
"""


def _worker() -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", WORKER],
        stdin=subprocess.PIPE,
        text=True,
        **processes.own_group(),
    )


def test_a_worker_stops_when_told_on_stdin() -> None:
    process = _worker()
    time.sleep(1.5)
    process.stdin.write("cancel\n")
    process.stdin.flush()
    assert process.wait(timeout=20) == 7


def test_a_worker_stops_when_the_editor_is_gone() -> None:
    process = _worker()
    time.sleep(1.5)
    process.stdin.close()  # what the editor's exit does to the pipe
    assert process.wait(timeout=20) == 7


# The parent passes its own process id: on Windows a venv's python.exe is a
# launcher that runs the real interpreter as its child, so neither the
# launcher's Popen nor the child's getppid() is the parent's interpreter.
PARENT = """
import os, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", sys.argv[1], sys.argv[2], str(os.getpid())])
print(os.getpid(), flush=True)
time.sleep(60)
"""

CHILD = """
import os, sys, time
from voicecut import processes
processes.when_parent_exits(
    int(sys.argv[2]), lambda: (open(sys.argv[1], "w").close(), os._exit(0))
)
time.sleep(60)
"""


def test_a_child_notices_its_parent_exit(tmp_path: Path) -> None:
    flag = tmp_path / "parent-gone"
    parent = subprocess.Popen(
        [sys.executable, "-c", PARENT, CHILD, str(flag)],
        stdout=subprocess.PIPE,
        text=True,
    )
    parent_pid = int(parent.stdout.readline())
    time.sleep(1.5)
    os.kill(parent_pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    parent.wait(timeout=10)
    deadline = time.monotonic() + 15
    while not flag.exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert flag.exists()


def test_terminate_tree_ends_the_children_too(tmp_path: Path) -> None:
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        text=True,
        **processes.own_group(),
    )
    child_pid = int(process.stdout.readline())
    processes.terminate_tree(process, force=True)
    process.wait(timeout=10)
    deadline = time.monotonic() + 10
    while _alive(child_pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not _alive(child_pid)


def _alive(pid: int) -> bool:
    if processes.WINDOWS:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie (exited, not reaped by its dead parent yet) counts as gone.
    status = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return bool(status) and not status.startswith("Z")

"""In-memory background jobs and their Server-Sent Events stream.

A job is one VoiceCut run, export or preview preparation.  It records an
ordered list of JSON events (``stage``, ``progress``, ``log`` and exactly one
terminal ``complete``/``error``/``cancelled``); clients replay them from any
index, so a reconnecting or reloaded browser never misses one.
"""

from __future__ import annotations

import json
import secrets
import subprocess
import threading
import time
from collections.abc import Iterator
from typing import Any

TERMINAL_EVENTS = frozenset({"complete", "error", "cancelled"})
STATUS_BY_EVENT = {"complete": "complete", "error": "failed", "cancelled": "cancelled"}
SSE_WAIT_SECONDS = 0.5
SSE_KEEPALIVE_SECONDS = 15.0
MAX_LOG_EVENTS = 4000


class Job:
    """One background task with its ordered progress events."""

    def __init__(self, job_id: str, kind: str, project_id: str) -> None:
        self.id = job_id
        self.kind = kind
        self.project_id = project_id
        self.status = "running"
        self.result: dict[str, Any] | None = None
        self.error: str | None = None
        self.process: subprocess.Popen[str] | None = None
        self.cancel_requested = False
        self.created_at = time.time()
        self._events: list[dict[str, Any]] = []
        self._logs = 0
        self._condition = threading.Condition()

    def append(self, event: dict[str, Any]) -> None:
        with self._condition:
            if self.status != "running":
                return  # nothing follows a terminal event
            if event.get("type") == "log":
                self._logs += 1
                if self._logs > MAX_LOG_EVENTS:
                    return
            event.setdefault("at", time.time())
            self._events.append(event)
            kind = event.get("type")
            if kind in TERMINAL_EVENTS:
                self.status = STATUS_BY_EVENT[str(kind)]
                if kind == "complete":
                    result = event.get("result")
                    self.result = result if isinstance(result, dict) else None
                elif kind == "error":
                    self.error = str(event.get("message") or "unknown error")
            self._condition.notify_all()

    def stage(self, stage: str, status: str, label: str, **extra: Any) -> None:
        self.append(
            {"type": "stage", "stage": stage, "status": status, "label": label, **extra}
        )

    def events_since(self, index: int, timeout: float = 0.0) -> list[dict[str, Any]]:
        with self._condition:
            if len(self._events) <= index and self.status == "running" and timeout:
                self._condition.wait(timeout)
            return list(self._events[max(0, index) :])

    @property
    def finished(self) -> bool:
        return self.status != "running"

    def to_dict(self) -> dict[str, Any]:
        with self._condition:
            count = len(self._events)
        return {
            "job_id": self.id,
            "kind": self.kind,
            "project_id": self.project_id,
            "status": self.status,
            "events": count,
            "result": self.result,
            "error": self.error,
        }


class JobRegistry:
    """Thread-safe registry of the jobs of this server process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}

    def create(self, kind: str, project_id: str) -> Job:
        with self._lock:
            job = Job(f"{kind}-{secrets.token_hex(6)}", kind, project_id)
            self._jobs[job.id] = job
            return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def running(self, project_id: str, kinds: frozenset[str]) -> Job | None:
        with self._lock:
            return next(
                (
                    job
                    for job in self._jobs.values()
                    if job.project_id == project_id
                    and job.kind in kinds
                    and not job.finished
                ),
                None,
            )


def sse_stream(job: Job, since: int) -> Iterator[str]:
    """Replay a job's events from ``since``, then follow it to the end."""

    index = max(0, since)
    quiet_since = time.monotonic()
    while True:
        events = job.events_since(index, SSE_WAIT_SECONDS)
        for event in events:
            index += 1
            yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            if event.get("type") in TERMINAL_EVENTS:
                return
        if events:
            quiet_since = time.monotonic()
        elif job.finished:
            return
        elif time.monotonic() - quiet_since >= SSE_KEEPALIVE_SECONDS:
            yield ": keepalive\n\n"
            quiet_since = time.monotonic()

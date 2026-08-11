"""The daemon's single persistent-database writer.

All daemon mutations enter this module as a transaction closure.  The worker
owns its SQLite connection and never shares it with request or model threads.
"""

from __future__ import annotations

import queue
import sqlite3
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Callable, Generic, Literal, TypeVar

import logging

try:
    import sqlite_vec
except ImportError:
    sqlite_vec = None

from saltmdb.db.connection import (
    _enter_coordinator_connection,
    _leave_coordinator_connection,
    close_connection,
    open_writer_connection,
    write_transaction_retrying,
)

T = TypeVar("T")
Priority = Literal["foreground", "background"]
logger = logging.getLogger(__name__)


class CoordinatorUsageError(RuntimeError):
    pass


@dataclass
class _Job(Generic[T]):
    label: str
    fn: Callable[[sqlite3.Connection], T]
    future: Future[T]
    enqueued_at: float


class DbWriteCoordinator:
    """Serialize writes with fair foreground/background FIFO lanes."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._foreground: queue.Queue[_Job] = queue.Queue()
        self._background: queue.Queue[_Job] = queue.Queue()
        self._admission_lock = threading.Lock()
        self._wakeup = threading.Condition(self._admission_lock)
        self._stopping = False
        self._started = False
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._active_label: str | None = None
        self._active_transaction = False
        self._conn: sqlite3.Connection | None = None
        self._completed = 0
        self._failed = 0
        self._stats_lock = threading.Lock()

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(target=self._run, name="saltmdb-db-writer", daemon=True)
        self._thread.start()

    @property
    def writer_thread_id(self) -> int | None:
        return self._thread_id

    def in_transaction(self) -> bool:
        return threading.get_ident() == self._thread_id and self._active_transaction

    def submit(
        self,
        label: str,
        fn: Callable[[sqlite3.Connection], T],
        *,
        priority: Priority = "foreground",
        wait: bool = True,
    ) -> T | Future[T]:
        if self.in_transaction():
            if self._conn is None:
                raise CoordinatorUsageError("writer transaction has no connection")
            return fn(self._conn)
        if not self._started:
            raise CoordinatorUsageError("DbWriteCoordinator has not been started")
        with self._admission_lock:
            if self._stopping:
                raise CoordinatorUsageError("DAEMON_SHUTTING_DOWN")
            future: Future[T] = Future()
            job = _Job(label, fn, future, time.monotonic())
            (self._foreground if priority == "foreground" else self._background).put(job)
        with self._wakeup:
            self._wakeup.notify()
        return future.result() if wait else future

    def submit_maintenance(
        self, label: str, fn: Callable[[sqlite3.Connection], T], *, wait: bool = True
    ) -> T | Future[T]:
        """Serialize explicit maintenance which SQLite forbids in a transaction.

        ``wal_checkpoint`` is the important example.  It still runs on the
        sole writer thread, but deliberately after all prior transactions.
        """
        if self.in_transaction():
            raise CoordinatorUsageError("maintenance cannot run inside a transaction")
        if not self._started:
            raise CoordinatorUsageError("DbWriteCoordinator has not been started")
        with self._admission_lock:
            if self._stopping:
                raise CoordinatorUsageError("DAEMON_SHUTTING_DOWN")
            future: Future[T] = Future()
            self._background.put(_Job(label, _Maintenance(fn), future, time.monotonic()))
        with self._wakeup:
            self._wakeup.notify()
        return future.result() if wait else future

    def telemetry(self) -> dict[str, object]:
        with self._admission_lock:
            queued = list(self._foreground.queue) + list(self._background.queue)
        now = time.monotonic()
        with self._stats_lock:
            return {
                "foreground_queue_depth": self._foreground.qsize(),
                "background_queue_depth": self._background.qsize(),
                "queue_depth": len(queued),
                "oldest_queued_age_s": max((now - j.enqueued_at for j in queued), default=0.0),
                "completed": self._completed,
                "failed": self._failed,
                "active_label": self._active_label,
                "writer_thread_id": self._thread_id,
            }

    def begin_draining(self) -> None:
        jobs_to_cancel = []
        with self._admission_lock:
            self._stopping = True
            # In-memory queue entries are not durable.  Their source work is
            # either a caller request (foreground) or already represented by a
            # durable embedding row (background), so resolve both promptly.
            for lane in (self._foreground, self._background):
                while True:
                    try:
                        jobs_to_cancel.append(lane.get_nowait())
                    except queue.Empty:
                        break
        for job in jobs_to_cancel:
            job.future.set_exception(CoordinatorUsageError("DAEMON_SHUTTING_DOWN"))
        with self._wakeup:
            self._wakeup.notify_all()

    def shutdown(self, timeout: float | None = None) -> None:
        self.begin_draining()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout)

    def _next_job(self, foreground_run: int) -> tuple[_Job | None, int]:
        # Fairness: no more than eight foreground jobs can overtake waiting background work.
        with self._admission_lock:
            if foreground_run >= 8 and not self._background.empty():
                return self._background.get_nowait(), 0
            if not self._foreground.empty():
                return self._foreground.get_nowait(), foreground_run + 1
            if not self._background.empty():
                return self._background.get_nowait(), 0
        return None, foreground_run

    def _load_vector_extension(self) -> None:
        """Best-effort sqlite-vec extension load on the writer connection.
        DBs without vector support still need a reliable scalar writer, so failures are logged and
        swallowed. Extension loading is always disabled again after a successful enable, including
        when the extension itself fails to load.
        """
        if sqlite_vec is None or self._conn is None:
            return
        extension_loading_enabled = False
        try:
            self._conn.enable_load_extension(True)
            extension_loading_enabled = True
            sqlite_vec.load(self._conn)
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            logger.warning(
                "sqlite_vec extension load unavailable; scalar writes remain enabled: %s", exc
            )
        finally:
            if extension_loading_enabled:
                try:
                    self._conn.enable_load_extension(False)
                except (sqlite3.Error, OSError, RuntimeError) as exc:
                    logger.warning(
                        "Could not disable sqlite extension loading after vector setup: %s", exc
                    )

    def _execute_job(self, job: "_Job") -> None:
        """Run one job's fn inside a write transaction (or directly if it's a _Maintenance job),
        then settle its Future. Called only from the writer thread."""
        self._active_label = job.label
        self._active_transaction = True
        try:
            if isinstance(job.fn, _Maintenance):
                result = job.fn.fn(self._conn)  # type: ignore[arg-type]
            else:
                # Register the connection only *inside* the outer transaction callback.
                # Nested legacy helpers then reuse it, while this top-level call still
                # opens the required BEGIN IMMEDIATE transaction.
                def _in_transaction(conn):
                    token = _enter_coordinator_connection(conn)
                    try:
                        return job.fn(conn)
                    finally:
                        _leave_coordinator_connection(token)

                result = write_transaction_retrying(self._conn, _in_transaction)  # type: ignore[arg-type]
        except BaseException as exc:
            with self._stats_lock:
                self._failed += 1
            # Future callbacks may submit more work.  Mark this job complete before invoking
            # them so they enqueue normally, never as a bogus nested autocommit operation.
            self._active_transaction = False
            job.future.set_exception(exc)
            if not isinstance(exc, Exception):
                raise
        else:
            with self._stats_lock:
                self._completed += 1
            self._active_transaction = False
            job.future.set_result(result)
        finally:
            self._active_transaction = False
            self._active_label = None

    def _run(self) -> None:
        self._thread_id = threading.get_ident()
        try:
            self._conn = open_writer_connection(self.db_path)
        except Exception:
            self.begin_draining()
            self._started = False
            return
        try:
            self._load_vector_extension()
            fg_run = 0
            while True:
                if self._stopping:
                    # Durable background work is already represented in
                    # embedding_jobs; don't drain it during shutdown.
                    break
                job, fg_run = self._next_job(fg_run)
                if job is None:
                    if self._stopping:
                        break
                    with self._wakeup:
                        if (
                            self._foreground.empty()
                            and self._background.empty()
                            and not self._stopping
                        ):
                            self._wakeup.wait()
                    continue
                if job.future.cancelled():
                    continue
                self._execute_job(job)
        finally:
            self.begin_draining()
            if self._conn is not None:
                close_connection(self._conn)
            self._conn = None


class _Maintenance(Generic[T]):
    def __init__(self, fn: Callable[[sqlite3.Connection], T]):
        self.fn = fn

    def __call__(self, conn: sqlite3.Connection) -> T:
        return self.fn(conn)

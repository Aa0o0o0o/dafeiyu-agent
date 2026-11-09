"""Opt-in cancellation fence for synthetic import research; not live wiring.

Revocation request signals local tokens immediately. Completion is acknowledged
only after a separate persistent control transaction and cleanup both succeed.
Control-lock acquisition serializes a publisher's final authorization check with
revocation across cooperating processes. No model or QQ interface is exposed.
"""

from __future__ import annotations

import sqlite3
import threading
import weakref
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class ImportCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class ImportToken:
    generation: int
    control_path: str
    cancelled: threading.Event

    def cancel(self):
        self.cancelled.set()


class ImportFence:
    """Separate control DB; callers must integrate visibility and cleanup guards."""

    def __init__(self, project_root, *, create=True):
        root = Path(project_root).resolve(strict=True)
        directory = root / "runtime" / "owner-private"
        for part in (root / "runtime", directory):
            if part.exists() and (part.is_symlink() or part.is_junction()):
                raise ValueError("invalid control directory")
            if create:
                part.mkdir(exist_ok=True)
            elif not part.is_dir():
                raise FileNotFoundError("control directory unavailable")
        if not directory.resolve().is_relative_to(root):
            raise ValueError("control path outside project")
        self.path = directory / "import-control.sqlite3"
        if self.path.is_symlink() or self.path.is_junction():
            raise ValueError("invalid control file")
        self._events = weakref.WeakSet()
        self._lock = threading.Lock()
        self._cancel_serial = 0
        if not create:
            if not self.path.is_file():
                raise FileNotFoundError("control file unavailable")
            return
        with self._connect(create=True) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS import_control(id INTEGER PRIMARY KEY CHECK(id=1),generation INTEGER NOT NULL,enabled INTEGER NOT NULL,pending INTEGER NOT NULL)"
            )
            db.execute("INSERT OR IGNORE INTO import_control VALUES(1,0,0,0)")

    @contextmanager
    def _connect(self, *, create=False, readonly=False):
        mode = "ro" if readonly else "rwc" if create else "rw"
        db = sqlite3.connect(self.path.as_uri() + "?mode=" + mode, uri=True, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _row(db):
        row = db.execute(
            "SELECT generation,enabled,pending FROM import_control WHERE id=1"
        ).fetchone()
        if row is None:
            raise ImportCancelled("control unavailable")
        return row

    def enable(self):
        """Trusted operator only. Never re-enable while cleanup is pending."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            gen, _, pending = self._row(db)
            if pending:
                raise ImportCancelled("cleanup pending")
            db.execute(
                "UPDATE import_control SET generation=?,enabled=1 WHERE id=1",
                (gen + 1,),
            )
            return gen + 1

    def token(self):
        with self._lock:
            serial = self._cancel_serial
        with self._connect(readonly=True) as db:
            generation, enabled, pending = self._row(db)
        if enabled != 1 or pending:
            raise ImportCancelled("source disabled")
        event = threading.Event()
        with self._lock:
            if serial != self._cancel_serial:
                event.set()
            self._events.add(event)
        return ImportToken(generation, str(self.path), event)

    def valid(self, token):
        if (
            not isinstance(token, ImportToken)
            or token.control_path != str(self.path)
            or token.cancelled.is_set()
        ):
            return False
        try:
            with self._connect(readonly=True) as db:
                gen, enabled, pending = self._row(db)
            return not token.cancelled.is_set() and (gen, enabled, pending) == (
                token.generation,
                1,
                0,
            )
        except (sqlite3.Error, OSError, ImportCancelled):
            return False

    def check(self, token):
        if not self.valid(token):
            raise ImportCancelled("import cancelled or access changed")

    def publish(self, token, commit):
        """Final check under cross-process control lock; caller owns dest txn.

        The successful check under the lock is the publish linearization point.
        A revoke ordered afterwards disables access and then cleans up the data.
        """
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                if (
                    not isinstance(token, ImportToken)
                    or token.control_path != str(self.path)
                    or token.cancelled.is_set()
                    or self._row(db) != (token.generation, 1, 0)
                ):
                    raise ImportCancelled("import cancelled before publish")
                commit()
            finally:
                db.rollback()

    def request_revoke(self, cleanup):
        """Signal locally without waiting; Future succeeds only after cleanup.

        An unacknowledged request lost on process crash is not durable. A crash
        after disabled/pending was persisted remains fail-closed until cleanup.
        cleanup must be trusted, idempotent, and use its own destination connection.
        """
        future = Future()
        with self._lock:
            self._cancel_serial += 1
            for event in self._events:
                event.set()
            self._events.clear()

        def worker():
            try:
                with self._connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    gen, _, _ = self._row(db)
                    gen += 1
                    db.execute(
                        "UPDATE import_control SET generation=?,enabled=0,pending=1 WHERE id=1",
                        (gen,),
                    )
                cleanup()
                with self._connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    if self._row(db) != (gen, 0, 1):
                        raise ImportCancelled("control changed during cleanup")
                    db.execute("UPDATE import_control SET pending=0 WHERE id=1")
                future.set_result(None)
            except BaseException as exc:
                future.set_exception(exc)

        threading.Thread(
            target=worker, name="wechat-import-revoke", daemon=True
        ).start()
        return future

"""Bounded one-page import diagnosis; never logs messages, IDs, paths, SQL or keys.

Runs in its own trusted local process. It always rolls back destination writes
and never publishes the binding. Results are fixed stage and failure categories.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from unittest.mock import patch

from src import wechat_real_import as imp
from src.wechat_import_fence import ImportCancelled
from src.wechat_private_store import AccessDenied
from . import real_source_session as source
from . import import_operator, secure_input

_ALLOWED = {
    "ordered read requires a suitable source index": "SOURCE_ORDER_INDEX",
    "unsupported message table schema": "MESSAGE_SCHEMA",
    "target conversation not found": "TARGET_TABLE_MISSING",
    "invalid message identifier or ordering field": "MESSAGE_ID_TYPE",
    "invalid shard-local sender identifier": "SENDER_ID_TYPE",
    "unsupported content type": "MESSAGE_CONTENT_TYPE",
    "conflicting cross-shard server identifier": "DUPLICATE_CONFLICT",
    "duplicate sender identity unresolved or conflicting": "DUPLICATE_SENDER",
}


class _ProbeStop(Exception):
    pass


def _category(exc):
    if type(exc) is ValueError:
        return _ALLOWED.get(str(exc), "VALUE_ERROR")
    if isinstance(exc, ImportCancelled):
        return "CANCELLED"
    if isinstance(exc, AccessDenied):
        return "ACCESS_DENIED"
    if isinstance(exc, sqlite3.OperationalError) or type(exc).__name__ == "OperationalError":
        return "SQLITE_OPERATIONAL"
    if isinstance(exc, sqlite3.DatabaseError) or type(exc).__name__ == "DatabaseError":
        return "SQLITE_DATABASE"
    if isinstance(exc, OSError):
        return "OS_ERROR"
    return "OTHER_ERROR"


def _sql_stage(query):
    q = str(query).upper()
    if q.startswith("EXPLAIN QUERY PLAN"):
        return "QUERY_PLAN"
    if "NAME2ID" in q:
        return "SENDER_LOOKUP"
    if "CONTACT" in q:
        return "CONTACT_LOOKUP"
    if "TABLE_INFO" in q:
        return "MESSAGE_SCHEMA"
    if "SQLITE_SCHEMA" in q or "SQLITE_MASTER" in q:
        return "SOURCE_METADATA"
    if "FROM MAIN.\"MSG_" in q:
        return "MESSAGE_QUERY"
    return "SOURCE_SQL"


class Trace:
    def __init__(self):
        self.events = []
    def add(self, stage, outcome):
        event = (stage, outcome)
        if event not in self.events and len(self.events) < 48:
            self.events.append(event)
    def result(self, raw):
        return {"status": raw.get("status", "unknown"),
                "probe": "rolled_back_no_binding",
                "events": [list(e) for e in self.events]}


class _Connection:
    def __init__(self, conn, trace):
        self._conn, self._trace = conn, trace
    def __getattr__(self, name):
        return getattr(self._conn, name)
    def execute(self, sql, *args, **kwargs):
        stage = _sql_stage(sql)
        try:
            return self._conn.execute(sql, *args, **kwargs)
        except Exception as exc:
            self._trace.add(stage, _category(exc))
            raise
    def set_key(self, value):
        try:
            return self._conn.set_key(value)
        except Exception as exc:
            self._trace.add("SOURCE_KEY_SETUP", _category(exc))
            raise


def diagnose(action):
    """Execute action with an unconditionally rolled-back one-page destination."""
    import sqlcipher3
    trace = Trace()
    original_connect = sqlcipher3.connect
    original_scan = source._scan_multi
    original_source_session = source.with_real_sources
    original_profile = imp._resolve_profiles
    original_directory = imp._check_directory_owner
    original_begin = imp._RealStage.begin
    original_write = imp._RealStage.write_page
    original_stage_all = imp.stage_all

    def connect(*args, **kwargs):
        try:
            conn = original_connect(*args, **kwargs)
            trace.add("SOURCE_CONNECT", "OPENED")
            return _Connection(conn, trace)
        except Exception as exc:
            trace.add("SOURCE_CONNECT", _category(exc))
            raise

    def scan(reader, pages, consumer, *args, **kwargs):
        trace.add("PAGE_DISCOVERY", "READY")
        def wrapped(keys):
            trace.add("KEY_SCAN", "ALL_MATCHED")
            try:
                return consumer(keys)
            except Exception as exc:
                trace.add("SOURCE_CONSUMER", _category(exc))
                raise
        result = original_scan(reader, pages, wrapped, *args, **kwargs)
        trace.add("KEY_SCAN_RESULT", result if result in {
            "MATCH", "SCAN_ERROR", "SCAN_LIMIT", "SCAN_INCOMPLETE", "KEYS_NOT_VERIFIED"
        } else "OTHER")
        return result

    def source_session(*args, **kwargs):
        try:
            trace.add("SOURCE_SESSION", "STARTED")
            return original_source_session(*args, **kwargs)
        except source.SourceSessionError as exc:
            allowed = {"SOURCE_NOT_VERIFIED", "SOURCE_CHANGED", "SOURCE_INPUT_INVALID",
                       "SOURCE_LIST_INVALID", "SCAN_ERROR", "SCAN_LIMIT",
                       "SCAN_INCOMPLETE", "KEYS_NOT_VERIFIED"}
            code = exc.args[0] if len(exc.args) == 1 and exc.args[0] in allowed else "OTHER"
            trace.add("SOURCE_SESSION", code)
            raise
        except Exception as exc:
            trace.add("SOURCE_SESSION", _category(exc))
            raise

    def profile(*args, **kwargs):
        try:
            result = original_profile(*args, **kwargs)
            trace.add("CONTACT_MATCH", "OK")
            return result
        except Exception as exc:
            trace.add("CONTACT_MATCH", _category(exc))
            raise

    def directory(*args, **kwargs):
        try:
            result = original_directory(*args, **kwargs)
            trace.add("ACCOUNT_DIRECTORY", "MATCHED" if result == "matched" else "NOT_COMPARABLE")
            return result
        except Exception as exc:
            trace.add("ACCOUNT_DIRECTORY", _category(exc))
            raise

    def begin(stage):
        try:
            result = original_begin(stage)
            trace.add("STAGING", "STARTED")
            return result
        except Exception as exc:
            trace.add("STAGING", _category(exc))
            raise

    def write(stage, page):
        try:
            original_write(stage, page)
            trace.add("FIRST_PAGE", "DECODED_AND_STAGED")
        except Exception as exc:
            trace.add("FIRST_PAGE", _category(exc))
            raise
        raise _ProbeStop()

    def stop_before_commit(stage):
        trace.add("FIRST_PAGE", "NO_ROWS")
        raise _ProbeStop()

    def stage_all(*args, **kwargs):
        try:
            return original_stage_all(*args, **kwargs)
        except _ProbeStop:
            trace.add("ROLLBACK", "EXPECTED")
            raise
        except Exception as exc:
            trace.add("SOURCE_READER", _category(exc))
            raise

    with patch.object(sqlcipher3, "connect", connect), \
         patch.object(source, "_scan_multi", scan), \
         patch.object(source, "with_real_sources", source_session), \
         patch.object(imp, "_resolve_profiles", profile), \
         patch.object(imp, "_check_directory_owner", directory), \
         patch.object(imp._RealStage, "begin", begin), \
         patch.object(imp._RealStage, "write_page", write), \
         patch.object(imp._RealStage, "commit", stop_before_commit), \
         patch.object(imp, "stage_all", stage_all):
        try:
            result = action()
        except Exception as exc:
            trace.add("OUTER", _category(exc))
            result = {"status": "unknown"}
    return trace.result(result if isinstance(result, dict) else {})


def main():
    if len(sys.argv) != 2:
        print(json.dumps({"status": "invalid_input"}))
        return 2
    try:
        hwnd = int(sys.argv[1])
        owner, target = secure_input.read_two()
        result = diagnose(lambda: import_operator.execute(
            hwnd=hwnd, owner_display_id=owner, target_display_id=target, confirmed=True
        ))
        owner = target = None
        print(json.dumps(result, ensure_ascii=True))
    except BaseException:
        print(json.dumps({"status": "diagnostic_failed"}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""Offline, staged cutover after the verified instance has stopped."""

import json
import socket
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.memory_migrate import migrate  # noqa: E402
from src.memory_v2.config import validate_config  # noqa: E402


def activate():
    for port in [6199, 6200]:
        with socket.socket() as connection:
            if connection.connect_ex(("127.0.0.1", port)) == 0:
                raise ValueError("Stop the project backend/UI before offline cutover")
    current = (ROOT / "runtime/state.sqlite3").resolve()
    staged = (ROOT / ".tmp/memory-v2-activation/state.sqlite3").resolve()
    backup = (
        ROOT
        / "backups"
        / ("memory-v2-switch-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    ).resolve()
    if (
        not all(p.is_relative_to(ROOT) for p in [current, staged, backup])
        or current == staged
    ):
        raise ValueError("Cutover path outside project")
    registry = ROOT / "runtime/memory-deletions.jsonl"
    registry.touch(exist_ok=True)
    report = migrate(
        current,
        staged,
        report_path=ROOT / "evidence/memory-v2-production-migration.json",
    )
    if (
        report["integrity"] != "ok"
        or not report["usage_preserved"]
        or report["foreign_key_violations"]
    ):
        raise ValueError("Migration validation failed; source database untouched")
    backup.mkdir(parents=True, exist_ok=False)
    settings = ROOT / "config/settings.json"
    old = settings.read_text(encoding="utf-8-sig")
    (backup / "settings.json").write_text(old, encoding="utf-8")
    for database in [current, staged]:
        with closing(sqlite3.connect(database)) as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    with (
        closing(sqlite3.connect(current)) as source,
        closing(sqlite3.connect(backup / "state.sqlite3")) as destination,
    ):
        source.backup(destination)
    cfg = json.loads(old)
    cfg["memory_v2"] = validate_config(
        {"enabled": True, "mode": "active", "graph_enabled": False}
    )
    cfg["group_context_seconds"] = 1800
    cfg["group_random_probability"] = 0.125
    cfg.pop("group_hot_probability", None)
    cfg.pop("group_followup_probability", None)
    temporary = settings.with_suffix(".v2.tmp")
    temporary.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    try:
        staged.replace(current)
        temporary.replace(settings)
    except BaseException:
        with (
            closing(sqlite3.connect(backup / "state.sqlite3")) as original,
            closing(sqlite3.connect(current)) as restored,
        ):
            original.backup(restored)
        settings.write_text(old, encoding="utf-8")
        temporary.unlink(missing_ok=True)
        raise
    (backup / "MANIFEST.json").write_text(
        json.dumps(
            {
                "memory_v2_managed": True,
                "expires_at": time.time() + 7 * 86400,
                "restore_requires_latest_deletion_registry": True,
                "contains_private_data": True,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    report.update(
        activated_database=True,
        backend_started=False,
        backup=str(backup),
        single_injection="V2_only",
        legacy_raw_and_writers="archived_disabled",
    )
    (ROOT / "evidence/memory-v2-cutover.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "status": "PASS",
        "activated_database": True,
        "backend_started": False,
        "backup": str(backup),
    }


if __name__ == "__main__":
    print(json.dumps(activate()))

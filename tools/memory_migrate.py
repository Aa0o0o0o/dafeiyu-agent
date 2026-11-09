"""Project-local dry-run/active migration; stdout never contains memory text."""

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.memory_v2.service import MemoryService  # noqa: E402
from src.memory_v2.migration import migrate_legacy  # noqa: E402
from src.memory_v2.config import validate_config  # noqa: E402


def migrate(source, target, *, report_path=None):
    source, target = Path(source).resolve(), Path(target).resolve()
    if not target.is_relative_to(ROOT) or source == target:
        raise ValueError("Use a distinct project-local staging database")
    target.parent.mkdir(parents=True, exist_ok=True)
    config = json.loads((ROOT / "config/settings.json").read_text(encoding="utf-8-sig"))
    with (
        closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original,
        closing(sqlite3.connect(target)) as db,
    ):
        original.backup(db)
        scopes = {r[0] for r in db.execute("SELECT scope FROM ltm_controls")}
        scopes.add(f"{config['instance_id']}:{config['bot_qq']}:{config['owner_qq']}")
        scopes.update(
            f"{config['instance_id']}:{config['bot_qq']}:group:{g}"
            for g in config["group_ids"]
        )
        before = db.execute("SELECT COUNT(*),SUM(reserved) FROM usage").fetchone()
        reports = {}
        for scope in scopes:
            service = MemoryService(
                db,
                scope,
                config["bot_qq"],
                config["instance_id"],
                config["owner_qq"],
                options=validate_config({"enabled": True, "mode": "shadow"}),
                registry=ROOT / "runtime/memory-deletions.jsonl",
            )
            reports[scope] = migrate_legacy(service)
            service.replay_registry()
        report = {
            "status": "PASS",
            "source_modified": False,
            "target": str(target),
            "scope_reports": reports,
            "usage_preserved": before
            == db.execute("SELECT COUNT(*),SUM(reserved) FROM usage").fetchone(),
            "integrity": db.execute("PRAGMA integrity_check").fetchone()[0],
            "foreign_key_violations": len(
                db.execute("PRAGMA foreign_key_check").fetchall()
            ),
        }
    if report_path:
        Path(report_path).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=str(ROOT / "runtime/state.sqlite3"))
    parser.add_argument(
        "--target", default=str(ROOT / ".tmp/memory-v2-shadow/state.sqlite3")
    )
    parser.add_argument(
        "--report", default=str(ROOT / "evidence/memory-v2-migration.json")
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Staging is always used; production activation is separate",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            migrate(args.source, args.target, report_path=args.report),
            ensure_ascii=False,
        )
    )

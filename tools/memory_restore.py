"""Never open a restored memory snapshot before replaying current deletion policy."""

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


def restore(snapshot, target, registry, config, current_db=None):
    snapshot, target, registry = (
        Path(snapshot).resolve(),
        Path(target).resolve(),
        Path(registry).resolve(),
    )
    if (
        not target.is_relative_to(ROOT)
        or target == ROOT / "runtime/state.sqlite3"
        or snapshot == target
    ):
        raise ValueError("Restore only to a distinct project-local staging path")
    if not registry.exists():
        raise ValueError("Current deletion registry required; do not silently omit it")
    target.parent.mkdir(parents=True, exist_ok=True)
    with (
        closing(sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True)) as src,
        closing(sqlite3.connect(target)) as db,
    ):
        src.backup(db)
        current_db = Path(current_db or ROOT / "runtime/state.sqlite3").resolve()
        with closing(
            sqlite3.connect(current_db.as_uri() + "?mode=ro", uri=True)
        ) as current:
            usage = current.execute("SELECT * FROM usage").fetchall()
            with db:
                db.execute("DELETE FROM usage")
                db.executemany("INSERT INTO usage VALUES(?,?,?,?,?,?,?)", usage)
                for row in current.execute("SELECT * FROM events"):
                    db.execute("INSERT OR REPLACE INTO events VALUES(?,?,?)", row)
        scopes = {r[0] for r in db.execute("SELECT scope FROM ltm_controls")}
        for line in registry.read_text(encoding="utf-8").splitlines():
            if line:
                scopes.add(json.loads(line)["scope_id"])
        for scope in scopes:
            memory = MemoryService(
                db,
                scope,
                config["bot_qq"],
                config["instance_id"],
                config["owner_qq"],
                options=validate_config({"enabled": True, "mode": "shadow"}),
                registry=registry,
            )
            migrate_legacy(memory)
            memory.replay_registry()
            memory.expire()
            memory.repo.rebuild_index(scope)
        # Rolling back a data snapshot must not roll back daily learning charges.
        with closing(
            sqlite3.connect(current_db.as_uri() + "?mode=ro", uri=True)
        ) as current:
            for table in ["mv2_learning_usage", "mv2_tasks"]:
                if current.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone():
                    rows = current.execute("SELECT * FROM " + table).fetchall()
                    if rows:
                        with db:
                            db.executemany(
                                "INSERT OR REPLACE INTO "
                                + table
                                + " VALUES("
                                + ",".join("?" for _ in rows[0])
                                + ")",
                                rows,
                            )
        return {
            "status": "PASS",
            "integrity": db.execute("PRAGMA integrity_check").fetchone()[0],
            "deletion_registry_replayed": True,
            "activated": False,
            "latest_usage_and_dedup_preserved": True,
            "target": str(target),
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument(
        "--registry", default=str(ROOT / "runtime/memory-deletions.jsonl")
    )
    args = parser.parse_args()
    cfg = json.loads((ROOT / "config/settings.json").read_text(encoding="utf-8-sig"))
    print(json.dumps(restore(args.snapshot, args.target, args.registry, cfg)))

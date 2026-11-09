"""Explicit synthetic browser fixture, separate from every production database."""

import argparse
import asyncio
import hashlib
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hypercorn.asyncio import serve  # noqa: E402
from hypercorn.config import Config  # noqa: E402
from src.memory_v2.service import MemoryService  # noqa: E402
from src.memory_v2.config import validate_config  # noqa: E402
from src.memory_v2.graph_api import create_app  # noqa: E402


async def run(nodes):
    db = sqlite3.connect(":memory:")
    service = MemoryService(
        db,
        "fixture:bot:group:111111",
        "1111111111",
        "fixture",
        "12345001",
        options=validate_config({"enabled": True, "mode": "active"}),
    )
    for index in range(nodes):
        service.remember(
            "12345001", f"演示记忆{index:03}：偏好无糖茶，合成内容。", str(index)
        )
    ids = [r[0] for r in db.execute("SELECT id FROM mv2_memories ORDER BY id")]
    for index, mid in enumerate(ids):
        for hop in (1, 2, 3):
            target = ids[(index + hop) % len(ids)]
            db.execute(
                "INSERT OR IGNORE INTO mv2_edges VALUES(?,?,?,'same_topic',.3,?,30,NULL,NULL)",
                (service.scope, mid, target, service.now()),
            )
    db.commit()
    salt = "00" * 16
    auth = {
        "viewer_id": "12345001",
        "salt": salt,
        "hash": hashlib.pbkdf2_hmac(
            "sha256", b"offline-ui-only", bytes.fromhex(salt), 300000
        ).hex(),
    }
    app = create_app(
        {service.scope: service}, auth, ROOT / "memory_ui", port=6201, demo_mode=True
    )
    cfg = Config()
    cfg.bind = ["127.0.0.1:6201"]
    cfg.accesslog = cfg.errorlog = None
    await serve(app, cfg)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--nodes", type=int, default=300)
    args = parser.parse_args()
    if not 1 <= args.nodes <= 300:
        raise SystemExit("Fixture nodes must be 1..300")
    asyncio.run(run(args.nodes))

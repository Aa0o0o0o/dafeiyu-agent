"""Synthetic SQLite load and injected-clock time advancement; no model or QQ."""

import json
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.memory_v2.service import MemoryService  # noqa: E402
from src.memory_v2.config import validate_config  # noqa: E402
from src.memory_v2.decay import TestClock, state_at, strength  # noqa: E402

ACTOR = "12345001"


def create(db, clock):
    return MemoryService(
        db,
        "benchmark:bot:group:111111",
        "1111111111",
        "benchmark",
        ACTOR,
        clock=clock,
        options=validate_config({"enabled": True, "mode": "active"}),
    )


def seed(memory, count, temporal="stable_preference", protected=False):
    ids = []
    with memory.repo.transaction():
        for index in range(count):
            item = dict(
                kind="episode" if "episode" in temporal else "semantic",
                subject_ids=[ACTOR],
                value=f"合成项目{index:04}：偏好无糖茶，讨论数据库和程序设计",
                assertion_mode="self_report",
                confidence_score=0.95,
                importance=0.6,
                temporal_class=temporal,
            )
            mid = memory._new_memory(
                item, f"fixture_{index}", memory.now(), protected=protected
            )
            memory._changed(mid, "synthetic_fixture")
            ids.append(mid)
    return ids


def benchmark(path, count):
    db = sqlite3.connect(path)
    try:
        clock = TestClock()
        memory = create(db, clock)
        seed(memory, count)
        times = []
        for index in range(45):
            started = time.perf_counter()
            packet = memory.recall(
                f"我在合成项目{index % count:04}讨论什么数据库",
                ACTOR,
                graph_enabled=False,
            )
            elapsed = (time.perf_counter() - started) * 1000
            if index >= 5:
                times.append(elapsed)
            assert memory.validate_packet(packet)
        return dict(
            rows=count,
            queries=40,
            warmup_queries=5,
            p50_ms=statistics.median(times),
            p95_ms=sorted(times)[int(0.95 * (len(times) - 1))],
            max_ms=max(times),
            graph_enabled=False,
            storage="temporary_file_sqlite_real_index",
            model_calls=0,
        )
    finally:
        db.close()


def advance(path):
    db = sqlite3.connect(path)
    try:
        clock = TestClock()
        memory = create(db, clock)
        original = {}
        for temporal in [
            "working_summary",
            "temporary_state",
            "episode",
            "important_episode",
            "stable_preference",
            "style",
        ]:
            original[temporal] = seed(memory, 10, temporal)
        original["core"] = seed(memory, 10, protected=True)
        source = memory.observe(
            dict(
                event_id="raw",
                sender_id=ACTOR,
                subject_ids=[ACTOR],
                text="合成原文保留期限检查",
            )
        )
        task = memory.register_pending_action(
            ACTOR, "合成尚未完成的授权任务", "fixture-receipt"
        )
        anchors = {
            mid: memory.get(mid)["anchor_at"]
            for ids in original.values()
            for mid in ids
        }
        result = {}
        for day in range(1, 181):
            clock.advance(days=1)
            memory.maintain(memory.business_day())
            if day not in [30, 180]:
                continue
            counts = {}
            for temporal, ids in original.items():
                rows = [memory.get(mid) for mid in ids]
                counts[temporal] = dict(
                    Counter(state_at(row, clock.now()) for row in rows)
                )
            all_rows = memory.repo.rows(
                "SELECT * FROM mv2_memories WHERE scope_id=?", (memory.scope,)
            )
            layers = {}
            for row in all_rows:
                projection = memory.project(row)
                states = layers.setdefault(projection["layer"], {})
                states[projection["status_now"]] = (
                    states.get(projection["status_now"], 0) + 1
                )
            result[str(day)] = dict(
                original_type_states=counts,
                all_layer_states=layers,
                total_rows=len(all_rows),
                raw_body_present=memory.source(source)["body"] is not None,
                pending_task_state=db.execute(
                    "SELECT state FROM mv2_pending_actions WHERE id=?", (task,)
                ).fetchone()[0],
                anchors_unchanged=all(
                    memory.get(mid)["anchor_at"] == value
                    for mid, value in anchors.items()
                ),
                curve_sample={
                    kind: strength(memory.get(ids[0]), clock.now())
                    for kind, ids in original.items()
                },
            )
            assert not result[str(day)]["raw_body_present"]
            assert result[str(day)]["anchors_unchanged"]
            assert result[str(day)]["pending_task_state"] == "pending"
        return result
    finally:
        db.close()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as folder:
        folder = Path(folder)
        report = dict(
            environment=dict(
                platform=platform.platform(),
                python=platform.python_version(),
                sqlite=sqlite3.sqlite_version,
                processor=platform.processor(),
            ),
            load=[benchmark(folder / f"load-{n}.sqlite3", n) for n in [300, 3000]],
            time_advancement=advance(folder / "timeline.sqlite3"),
            fixture_only=True,
            no_provider_or_qq_requests=True,
        )
    (ROOT / "evidence/memory-v2-benchmark.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "load": report["load"],
                "time_advancement_days": list(report["time_advancement"]),
            }
        )
    )

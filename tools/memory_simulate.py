"""Reproducible Chinese retrieval/operation evaluation; no LLM grading or fees."""

import argparse
import json
import platform
import random
import re
import sqlite3
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.memory_v2.service import MemoryService  # noqa: E402
from src.memory_v2.decay import TestClock, DAY  # noqa: E402
from src.memory_v2.config import validate_config  # noqa: E402


def build_fixture(seed=20261029):
    rng = random.Random(seed)
    kinds = ["current", "history", "no_answer", "isolation", "correction", "deletion"]
    cases = []
    for index in range(180):
        cases.append(
            {
                "id": f"ZH-{index:03}",
                "split": "test" if index % 5 == 0 else "development",
                "kind": kinds[index % 6],
                "actor": str(12345000 + index % 10),
                "scope": "instance:bot:"
                + (f"group:{index % 3 + 111111}" if index % 4 else "private:12345000"),
                "offset_days": rng.choice([30, 30.5, 31]),
                "provenance": "synthetic_program_checked_not_human_labelled",
            }
        )
    return cases


def evaluate(seed=20261029):
    cases = build_fixture(seed)
    results, timings, failures = [], [], []
    base_time = 1780000000.0
    for case in cases:
        db = sqlite3.connect(":memory:")
        clock = TestClock(base_time)
        actor = case["actor"] if ":group:" in case["scope"] else "12345000"
        service = MemoryService(
            db,
            case["scope"],
            "1111111111",
            "instance",
            actor,
            clock=clock,
            options=validate_config({"enabled": True, "mode": "active"}),
        )
        old = service.remember(actor, "平时喝茉莉茶", "initial", protected=False)
        # Adopt the same factual slot for an explicit changed preference.
        service.db.execute(
            "UPDATE mv2_memories SET predicate='drink_habit' WHERE id=?", (old,)
        )
        service.db.commit()
        clock.advance(days=14)
        source = service.observe(
            {
                "event_id": "changed",
                "sender_id": actor,
                "text": "我现在平时喝柚子茶",
                "subject_ids": [actor],
            }
        )
        item = {
            "kind": "semantic",
            "subject_ids": [actor],
            "predicate": "drink_habit",
            "value": "喝柚子茶",
            "assertion_mode": "self_report",
            "temporal_class": "stable_preference",
            "source_event_ids": [source],
            "confidence_score": 0.95,
            "importance": 0.6,
        }
        current = service.apply({"candidates": [item]}, service.snapshot())["ids"][0]
        clock.advance(days=case["offset_days"] - 14)
        fixed_facts = ["平时喝茉莉茶"]
        recent_window = [(base_time + 14 * DAY, "喝柚子茶")]
        query, mode, query_time, expected = "我平时喝什么茶", "direct", None, current
        kind = case["kind"]
        if kind == "history":
            query, mode, query_time, expected = (
                "我以前喝什么茶",
                "history",
                base_time + 1,
                old,
            )
        elif kind == "no_answer":
            query, expected = "从未提及的星际暗号", None
        elif kind == "isolation":
            other = MemoryService(
                db,
                "instance:bot:group:999999",
                "1111111111",
                "instance",
                actor,
                clock=clock,
            )
            self_only = other.remember(actor, "私域荔枝酒", "other-scope")
            query, expected = "私域荔枝酒", None
            assert service.get(self_only) is None
        elif kind == "correction":
            expected = service.correct(actor, current, "喝温水", "fix")
            query = "我喝什么水"
            fixed_facts = ["喝温水"]
            recent_window.append((clock.now(), "喝温水"))
        elif kind == "deletion":
            service.forget(actor, all_mine=True)
            expected = None
            fixed_facts, recent_window = [], []
        context = [
            value for stamp, value in recent_window if clock.now() - stamp < 1800
        ] or fixed_facts
        baseline_values = [
            value
            for value in context
            if re.search(r"我|偏好|喜欢|喝", query)
            or any(term in value for term in query.split())
        ]
        expected_value = service.get(expected)["value"] if expected else None
        baseline_a = (
            expected_value in baseline_values if expected else not baseline_values
        )
        variants = {}
        for name, graph in [("B", False), ("C", True)]:
            started = time.perf_counter()
            packet = service.recall(
                query, actor, mode=mode, query_time=query_time, graph_enabled=graph
            )
            timings.append((time.perf_counter() - started) * 1000)
            ids = set(packet["revisions"])
            ok = expected in ids if expected else not ids
            variants[name] = bool(ok)
            if not ok:
                failures.append(
                    {
                        "case": case["id"],
                        "variant": name,
                        "kind": kind,
                        "selected_count": len(ids),
                    }
                )
        results.append(
            {
                "id": case["id"],
                "split": case["split"],
                "kind": kind,
                "A": baseline_a,
                **variants,
            }
        )
        db.close()
    simulation = {}
    for day in (30, 180):
        simulation[str(day)] = {
            kind: {
                "strength": 2 ** (-day / half),
                "state": "active"
                if 2 ** (-day / half) >= 0.35
                else "dormant"
                if 2 ** (-day / half) >= 0.15
                else "frozen",
            }
            for kind, half in [
                ("temporary", 3),
                ("episode", 14),
                ("important_episode", 120),
                ("stable_preference", 180),
            ]
        }
    ordered = sorted(timings)
    return {
        "seed": seed,
        "cases": len(cases),
        "development_cases": 144,
        "held_out_cases": 36,
        "subjects": 10,
        "group_scopes": 3,
        "private_scopes": 1,
        "timeline_days": 31,
        "human_labelled": False,
        "scope": "retrieval_and_operations_only_not_final_chat_answer_quality",
        "model_calls": 0,
        "threshold_tuning": "none_held_out_not_used_for_tuning",
        "variant_correct": {
            name: sum(row[name] for row in results) for name in ["A", "B", "C"]
        },
        "by_category": {
            kind: {
                name: sum(row[name] for row in results if row["kind"] == kind)
                for name in ["A", "B", "C"]
            }
            for kind in sorted({c["kind"] for c in cases})
        },
        "graph_gain": sum(row["C"] - row["B"] for row in results),
        "graph_default_enabled": False,
        "retrieval_latency_ms": {
            "p50": statistics.median(timings),
            "p95": ordered[int(0.95 * (len(ordered) - 1))],
            "max": max(timings),
            "memory_rows_per_query": "2_to_3_small_fixture",
        },
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
        },
        "time_simulation": simulation,
        "failures": failures,
        "human_blind_review_20_conversations": "BLOCKED_requires_human_review",
        "real_group_and_mobile_acceptance": "BLOCKED_not_claimed",
        "results": results,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=20261029)
    parser.add_argument(
        "--output", default=str(ROOT / "evidence/memory-v2-simulation.json")
    )
    args = parser.parse_args()
    fixture_path = ROOT / "fixtures/memory_eval_zh/cases.json"
    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    fixture_path.write_text(
        json.dumps(build_fixture(args.seed), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    result = evaluate(args.seed)
    Path(args.output).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                key: result[key]
                for key in [
                    "cases",
                    "variant_correct",
                    "graph_gain",
                    "retrieval_latency_ms",
                    "model_calls",
                ]
            }
        )
    )

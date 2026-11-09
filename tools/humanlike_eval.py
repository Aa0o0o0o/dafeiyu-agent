"""Prepare and score blinded, locally generated comparison answers.

This script never calls a model, reads production chat data, or sends messages.
Provide two JSON objects mapping synthetic scenario IDs to responses.
"""

import argparse
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "fixtures/humanlike_scenarios_20261104.json"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_new(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def prepare(baseline, candidate, scenarios, seed=20261104):
    if not isinstance(baseline, dict) or not isinstance(candidate, dict):
        raise ValueError("Answer files must be JSON objects keyed by scenario ID")
    if not baseline or baseline.keys() != candidate.keys():
        raise ValueError("Both answer files must contain the same nonempty IDs")
    known = {case["id"]: case for case in scenarios}
    if not set(baseline) <= set(known):
        raise ValueError("Unknown scenario ID")
    rng = random.Random(seed)
    cases, key = [], {}
    for scenario_id in sorted(baseline):
        old, new = baseline[scenario_id], candidate[scenario_id]
        if not isinstance(old, str) or not old.strip() or not isinstance(new, str) or not new.strip():
            raise ValueError("Each answer must be a nonempty string")
        candidate_first = bool(rng.getrandbits(1))
        case = known[scenario_id]
        cases.append({
            "id": scenario_id,
            "category": case["category"],
            "context": case["context"],
            "input": case["input"],
            "criteria": case["criteria"],
            "option_a": new if candidate_first else old,
            "option_b": old if candidate_first else new,
        })
        key[scenario_id] = "a" if candidate_first else "b"
    rng.shuffle(cases)
    return {"cases": cases, "total_scenarios": len(known)}, key


def score(ratings, key):
    if not isinstance(ratings, list) or not isinstance(key, dict):
        raise ValueError("Ratings must be a list and key must be an object")
    seen = set()
    counts = {"candidate_win": 0, "baseline_win": 0, "tie": 0}
    for rating in ratings:
        if not isinstance(rating, dict):
            raise ValueError("Invalid rating")
        scenario_id, preference = rating.get("id"), rating.get("preference")
        if scenario_id in seen or scenario_id not in key or preference not in {"a", "b", "tie"}:
            raise ValueError("Duplicate/unknown scenario or invalid preference")
        if key[scenario_id] not in {"a", "b"}:
            raise ValueError("Invalid blind key")
        seen.add(scenario_id)
        outcome = "tie" if preference == "tie" else "candidate_win" if preference == key[scenario_id] else "baseline_win"
        counts[outcome] += 1
    total = len(seen)
    return {
        **counts,
        "rated": total,
        "available": len(key),
        "candidate_win_fraction_including_ties": counts["candidate_win"] / total if total else None,
        "notice": "Preference is not a measure of factual accuracy or statistical significance; report ties and individual safety/factual checks separately.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("prepare")
    create.add_argument("--baseline", required=True)
    create.add_argument("--candidate", required=True)
    create.add_argument("--blind", required=True)
    create.add_argument("--key", required=True)
    create.add_argument("--seed", type=int, default=20261104)
    report = commands.add_parser("score")
    report.add_argument("--ratings", required=True)
    report.add_argument("--key", required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        if Path(args.blind).resolve() == Path(args.key).resolve():
            raise ValueError("Blind file and key must have distinct paths")
        blind, key = prepare(read_json(args.baseline), read_json(args.candidate), read_json(SCENARIOS), args.seed)
        # Refuse to overwrite either destination; leave no partial key/answer pair.
        if Path(args.blind).exists() or Path(args.key).exists():
            raise FileExistsError("Choose two unused output paths")
        write_new(args.key, key)
        try:
            write_new(args.blind, blind)
        except BaseException:
            Path(args.key).unlink(missing_ok=True)
            raise
        print(f"Prepared {len(blind['cases'])}/{blind['total_scenarios']} blinded cases.")
    else:
        print(json.dumps(score(read_json(args.ratings), read_json(args.key)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

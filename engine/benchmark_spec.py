"""Repeatable cold quote-cache benchmark of the normal specification research path."""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from .app import _research_row
from .run_spec_research import _save
from .schemas import PositionIn


def summarize(report: dict) -> dict:
    rows = [row for row in report["rows"] if row is not None]
    stages = defaultdict(lambda: {"calls": 0, "seconds": 0.0, "errors": 0})
    usage = defaultdict(lambda: {"responses": 0, "input_tokens": 0,
                                 "output_tokens": 0, "cache_hit_tokens": 0})
    for row in rows:
        for key, item in row.get("metrics", {}).get("stages", {}).items():
            for field in stages[key]:
                stages[key][field] += item[field]
        for key, item in row.get("metrics", {}).get("llm_usage", {}).items():
            for field in usage[key]:
                usage[key][field] += item[field]
    for item in stages.values():
        item["seconds"] = round(item["seconds"], 2)
    return {"statuses": dict(Counter(row["status"] for row in rows)),
            "completed": len(rows), "stages": dict(stages), "llm_usage": dict(usage),
            "cost_usd": None,
            "cost_note": "Provider tokens measured; billing rates/account charges are not inferred."}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--draft", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--numbers", default="", help="comma-separated specification numbers; empty means all")
    parser.add_argument("--workers", type=int, default=3, choices=range(1, 5))
    args = parser.parse_args()
    if args.report.exists():
        parser.error("use a new report path; existing benchmark evidence is never overwritten")
    draft = json.loads(args.draft.read_text(encoding="utf-8"))
    numbers = {int(value) for value in args.numbers.split(",") if value.strip()}
    positions = [PositionIn.model_validate(raw) for raw in draft["positions"]
                 if not numbers or raw["number"] in numbers]
    if not positions or numbers - {p.number for p in positions}:
        parser.error("unknown or empty position selection")
    report = {"status": "running", "mode": "cold_quote_cache", "workers": args.workers,
              "started_at": datetime.now(timezone.utc).isoformat(),
              "rows": [None] * len(positions), "total": len(positions)}
    started = time.monotonic()
    _save(args.report, report)
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(_research_row, position, use_cache=False): index
                       for index, position in enumerate(positions)}
            for future in as_completed(futures):
                index = futures[future]
                report["rows"][index] = future.result()
                report["wall_seconds"] = round(time.monotonic() - started, 2)
                report["summary"] = summarize(report)
                if (report["rows"][index].get("found")
                        and "first_result_with_offers_seconds" not in report):
                    report["first_result_with_offers_seconds"] = report["wall_seconds"]
                _save(args.report, report)
                row = report["rows"][index]
                print(f"[{report['summary']['completed']}/{len(positions)}] №{row['number']} "
                      f"{row['status']} {row['found']}/{row['target']} "
                      f"{row['duration_seconds']}s", flush=True)
    except BaseException:
        report["status"] = "interrupted"
        _save(args.report, report)
        raise
    report["status"] = "done"
    report["wall_seconds"] = round(time.monotonic() - started, 2)
    report["summary"] = summarize(report)
    _save(args.report, report)
    print(json.dumps(report["summary"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

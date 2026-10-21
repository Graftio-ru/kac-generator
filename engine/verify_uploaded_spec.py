"""Measure real multipart parsing of original draft images without duplicate searches."""
import argparse
import base64
import json
import time
from contextlib import nullcontext
from collections import Counter
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from .app import app
from .run_spec_research import _save
from .telemetry import capture_metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--draft", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--research", action="store_true", help="run the real automatic research job")
    args = parser.parse_args()
    if args.report.exists():
        parser.error("existing verification reports are never overwritten")
    draft = json.loads(args.draft.read_text())
    files = [("specs", (Path(s["name"]).name,
                         base64.b64decode(s["data"].split(",", 1)[1]), s["type"]))
             for s in draft["sources"]]
    started = time.monotonic()
    _save(args.report, {"status": "parsing", "source_files": len(files)})
    # Search is deliberately a subsequent stage; do not start a duplicate job
    # while measuring and comparing the original uploaded images.
    suppression = nullcontext() if args.research else patch("engine.app._start_research_job", return_value=None)
    with capture_metrics() as metrics, suppression:
        response = TestClient(app).post("/parse", files=files,
                                        data={"object_name": "ЭЗС А24 — проверка исходников"})
    report = {"status": "parsed" if response.status_code == 200 else "error",
              "http_status": response.status_code, "source_files": len(files),
              "seconds": round(time.monotonic() - started, 2),
              "metrics": metrics.summary(), "result": response.json()}
    if response.status_code == 200:
        actual = response.json()["positions"]
        reference = {p["number"]: p for p in draft["positions"]}
        counts = Counter(p["number"] for p in actual)
        report["missing_numbers"] = sorted(set(reference) - set(counts))
        report["duplicate_numbers"] = sorted(n for n, count in counts.items() if count > 1)
        report["unexpected_numbers"] = sorted(set(counts) - set(reference))
        report["field_differences"] = [
            {"number": p["number"], "field": field, "parsed": p.get(field),
             "reference": reference[p["number"]].get(field)}
            for p in actual if p["number"] in reference
            for field in ("type_mark", "unit", "qty", "is_unique")
            if p.get(field) != reference[p["number"]].get(field)]
        report["total"] = len(actual)
    _save(args.report, report)
    print(json.dumps({k: v for k, v in report.items() if k not in {"result", "metrics"}},
                     ensure_ascii=False), flush=True)
    if args.research and response.status_code == 200:
        job_id = response.json()["research_job_id"]
        client = TestClient(app)
        last = -1
        while True:
            job = client.get(f"/research/{job_id}").json()
            report["research"] = job
            report["status"] = job["status"]
            report["workflow_seconds"] = round(time.monotonic() - started, 2)
            _save(args.report, report)
            if job["completed"] != last:
                last = job["completed"]
                print(f"research {last}/{job['total']} {job['status']}", flush=True)
            if job["status"] in {"done", "cancelled", "error"}:
                break
            time.sleep(2)
        payload = {"object_name": "ЭЗС А24 — проверка исходных изображений", "positions": [
            {**p, "offers": r.get("offers", []) if r else [],
             "candidates": r.get("candidates", r.get("offers", [])) if r else [],
             "research_status": r.get("status", "error") if r else "error",
             "research_reason": r.get("reason", "Поиск не завершён") if r else "Поиск не завершён",
             "research_target": r.get("target", 5) if r else 5,
             "diagnostics": r.get("diagnostics", []) if r else []}
            for p, r in zip(report["result"]["positions"], job["rows"])]}
        export = client.post("/research/report", json=payload)
        report["summary_http_status"] = export.status_code
        if export.status_code == 200:
            output = args.report.with_suffix(".xlsx")
            output.write_bytes(export.content)
            report["summary_path"] = str(output.resolve())
        build = client.post("/build", json={
            "object_name": payload["object_name"], "skip_search": True, "use_tsn": False,
            "positions": payload["positions"]})
        report["full_build_http_status"] = build.status_code
        report["full_build_result"] = build.json()
        # Never fabricate OCR review acceptance or prices to make documents pass.
        _save(args.report, report)


if __name__ == "__main__":
    main()

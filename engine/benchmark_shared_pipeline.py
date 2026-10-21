"""Bounded live trial of the same shared queues used by automatic uploads."""
import argparse
import hashlib
import json
import time
import uuid
from pathlib import Path
from . import app as api
from .schemas import PositionIn


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--controls', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--numbers', default='23,15,84')
    parser.add_argument('--seconds', type=float, default=60)
    args = parser.parse_args()
    if args.report.exists() or args.seconds <= 0:
        parser.error('Use a fresh report and a positive job budget')
    raw = args.controls.read_bytes()
    controls = json.loads(raw)
    numbers = {int(n) for n in args.numbers.split(',')}
    positions = [PositionIn.model_validate(c['input']) for c in controls['cases']
                 if c['number'] in numbers]
    if not positions or {p.number for p in positions} != numbers:
        parser.error('Every requested number must belong to the frozen control set')
    job_id = uuid.uuid4().hex[:12]
    api.RESEARCH_JOBS[job_id] = {'job_id':job_id,'status':'pending','total':len(positions),
                                'completed':0,'rows':[None]*len(positions)}
    api._RESEARCH_INPUTS[job_id] = positions
    previous = api.settings.research_job_seconds
    started = time.monotonic()
    try:
        api.settings.research_job_seconds = args.seconds
        api._run_research_job(job_id)
    finally:
        api.settings.research_job_seconds = previous
    report = {'stage':'shared-upload-pipeline','controls_sha256':hashlib.sha256(raw).hexdigest(),
              'normal_local_cache':True,'job_budget_seconds':args.seconds,
              'seconds':round(time.monotonic()-started,3),'job':api.RESEARCH_JOBS[job_id],
              'provisional':True}
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'status':report['job']['status'],'seconds':report['seconds'],
                      'rows':[{k:r.get(k) for k in ('number','status','found','new_cards_checked')}
                              for r in report['job']['rows'] if r]},ensure_ascii=False))

if __name__ == '__main__':
    main()

"""Explicit original-job acquisition continuation; dry-run never imports consumers."""
import argparse,json,time,sys
from pathlib import Path

def original_frontiers(research,catalogue,job_id,context):
    """Audit every original reservation before any claim, including queued rows."""
    from types import SimpleNamespace
    from .search.catalogue import identity_for_position,CatalogueConflict
    from .equipment_output_policy import is_graftio_equipment
    loaded=[(index,research.load_acquisition_frontier(job_id,index)['frontier']) for index in context['indices']]
    represented={value['product_id'] for index,value in loaded}
    for raw in context['positions']:
        if raw.get('is_unique') or is_graftio_equipment(SimpleNamespace(**raw)):continue
        identity=identity_for_position(raw);product=catalogue.lookup(identity)
        if not product or product.get('identity_ambiguous'):continue
        if catalogue.acquisition_owner_matches(product['product_id'],owner=job_id,expected_identity=identity) and product['product_id'] not in represented:
            raise CatalogueConflict('catalogue_acquisition_frontier_incomplete')
    return loaded

def inspect_resume(research, catalogue, job_id):
    from .search.catalogue import identity_for_position
    context=research.acquisition_context(job_id);rows=[]
    loaded=original_frontiers(research,catalogue,job_id,context)
    from .search.acquisition_frontier import original_search_route,candidate_count
    route=original_search_route([value for _,value in loaded])
    for index,value in loaded:
        product=catalogue.lookup(identity_for_position(context['positions'][index]));allocation=product.get('acquisition') or {}
        if not catalogue.acquisition_owner_matches(value['product_id'],owner=job_id,expected_identity=identity_for_position(context['positions'][index]),statuses=('running','done') if value['frontier_complete'] else ('running',)):
            from .search.catalogue import CatalogueConflict
            raise CatalogueConflict('Original acquisition owner differs')
        if product.get('identity_ambiguous') or product.get('product_id')!=value['product_id'] or allocation.get('generation',0)<value['acquisition_generation']:
            from .search.catalogue import CatalogueConflict
            raise CatalogueConflict('Original acquisition identity or generation differs')
        rows.append(dict(index=index,product_id=value['product_id'],frontier_complete=value['frontier_complete'],
            pending_queries=sum(q['state']=='pending' for q in value['queries']),
            uncertain_queries=sum(q['state']=='running' for q in value['queries']),
            remaining_cards=sum(candidate_count(q)-q['cursor']-len(q.get('settled_cards',{})) for q in value['queries'] if q['response']),
            needs_finalization=value['frontier_complete'] and allocation.get('status')=='running',claimable=allocation.get('status')=='running' and allocation.get('lease_until',0)<=time.time()))
    return dict(job_id=job_id,mode='dry_run',original_input_sha256=context['input_sha256'],
                search_route=route,rows=rows)

def main(argv=None):
    parser=argparse.ArgumentParser(description='Inspect or explicitly resume an original unfinished catalogue job')
    parser.add_argument('--research-dir',type=Path,required=True);parser.add_argument('--catalogue',type=Path,required=True);parser.add_argument('--job',required=True)
    parser.add_argument('--resume',action='store_true',help='Explicitly claim original expired leases and continue; default is dry-run')
    parser.add_argument('--cache-dir',type=Path,help='Original owned cache directory; required for explicit resume')
    parser.add_argument('--slice-seconds',type=float,help='Optional soft slice; in-flight operations finish before parking')
    arguments=parser.parse_args(argv)
    import math
    if arguments.slice_seconds is not None and (not math.isfinite(arguments.slice_seconds) or arguments.slice_seconds<=0):parser.error('Positive finite --slice-seconds required')
    if arguments.resume:
        if arguments.cache_dir is None:parser.error('Explicit original --cache-dir required for resume')
        if any(name in sys.modules for name in ('engine.app','engine.browser','engine.search.agent','engine.llm','engine.search.catalogue_producer')):
            parser.error('Resume requires a fresh consumer composition')
        if arguments.cache_dir.is_symlink() or not arguments.cache_dir.is_dir():parser.error('Existing owned cache directory required')
    if not (arguments.research_dir/'research.sqlite3').is_file() or not arguments.catalogue.is_file():parser.error('Existing research and catalogue stores required')
    from .research_publication import IntegrationStore
    from .search.catalogue import CatalogueStore
    research=IntegrationStore(arguments.research_dir);catalogue=CatalogueStore(arguments.catalogue)
    try:
        report=inspect_resume(research,catalogue,arguments.job)
        if arguments.resume:
            from . import config
            config.CACHE_DIR=arguments.cache_dir.resolve()
            from . import app
            app.RESEARCH_JOB_DIR=arguments.research_dir.resolve()
            report=app.resume_catalogue_acquisition(arguments.job,store=catalogue,slice_seconds=arguments.slice_seconds)
        print(json.dumps(report,ensure_ascii=False));return 0
    finally:research.db.close()

if __name__=='__main__':raise SystemExit(main())

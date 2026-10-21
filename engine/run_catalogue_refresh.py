"""Explicit background service invocation; no scheduler installation or discovery."""
import argparse
import json
from pathlib import Path

def main(argv=None, *, image_store=None, browser_factory=None):
    parser=argparse.ArgumentParser(description='Refresh only recorded catalogue card URLs')
    parser.add_argument('--catalogue',type=Path,required=True)
    parser.add_argument('--proof-job',help='Canonical server job supplying native selection positions')
    parser.add_argument('--jobs-dir',type=Path,help='Owned research job store for --proof-job')
    arguments=parser.parse_args(argv)
    if bool(arguments.proof_job)!=bool(arguments.jobs_dir):parser.error('--proof-job and --jobs-dir are required together')
    if not arguments.catalogue.is_file():
        parser.error('Existing catalogue file required')
    from .config import CACHE_DIR
    from .browser import Browser
    from .search.catalogue import CatalogueStore
    from .search.catalogue_storage import create_image_store
    from .search.catalogue_producer import refresh_recorded
    from .search.card_attachments import collect_current
    from .search.selected_proofs import refresh_prices_and_selected_proofs
    positions=None
    if arguments.proof_job:
        from .research_publication import IntegrationStore
        from .schemas import PositionIn
        canonical=IntegrationStore(arguments.jobs_dir).materialize(arguments.proof_job,include_events=False)
        positions=[PositionIn.model_validate(p).to_result().position for p in canonical['positions']]
        from .equipment_output_policy import is_graftio_equipment
        positions=[p for p in positions if not p.is_unique and not is_graftio_equipment(p)]
    from .search.session import research_session
    store=CatalogueStore(arguments.catalogue)
    if not any(product['heads'] for product in store.all_snapshot()['products'].values()):
        print(json.dumps(dict(recorded_cards=0,results=[])))
        return 0
    # Deployment composition injects its object-store client into this service.
    # Standalone invocation honestly reports local-only, never invents S3 success.
    images=image_store or create_image_store(CACHE_DIR)
    with research_session(),(browser_factory or Browser)() as browser:
        if positions is not None:
            report=refresh_prices_and_selected_proofs(store,positions,browser=browser,image_store=images)
        else:
            report=dict(prices=refresh_recorded(store,browser=browser,image_store=images,capture=collect_current),proofs=dict(paused=True,reason='Выбранные снимки требуют канонической спецификации --proof-job'))
    print(json.dumps(dict(recorded_cards=len(report['prices']),results=report),ensure_ascii=False))
    return 0

if __name__=='__main__':
    raise SystemExit(main())

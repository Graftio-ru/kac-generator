"""One final-document boundary: contextual admissibility and three independent sellers."""
from .offer_selection import independent_offers, offer_is_comparable
from .equipment_output_policy import is_graftio_equipment, letter_offers


def prepare_final_results(results):
    prepared = []
    missing = []
    for result in results:
        pos = result.position
        from .search.base_db import apply_refresh
        # An open draft may predate a failed refresh; client flags cannot
        # override the newest trusted request-context receipt.
        pool = result.all_offers or result.offers
        catalogue = [o for o in pool if o.catalogue_ref]
        legacy = [o for o in pool if not o.catalogue_ref]
        if legacy:
            # Legacy request receipts must never overwrite an immutable
            # catalogue observation. Mixed requests retain both guarded pools.
            retained = result.model_copy(update={'all_offers': legacy,
                'offers': [o for o in result.offers if not o.catalogue_ref]})
            apply_refresh(pos, retained)
            result.all_offers = list(retained.all_offers or retained.offers) + catalogue
        # Only the existing unique equipment workflow may use its letter price.
        unique_letters = [o for o in result.offers if pos.is_unique and o.from_letter]
        pool = result.all_offers or result.offers
        eligible = independent_offers(pool, pos)
        from .search.selected_proofs import proof_ready
        eligible = [o for o in eligible if not o.catalogue_ref or proof_ready(o,pos)]
        own_equipment = is_graftio_equipment(pos)
        if own_equipment:
            eligible = sorted(letter_offers(pos, pool), key=lambda o: o.price_with_vat)
        elif unique_letters:
            eligible = sorted((o for o in unique_letters
                               if offer_is_comparable(o, pos, require_seller=False)),
                              key=lambda o: o.price_with_vat)
            if not eligible:
                missing.append(f'{pos.number}: нет допустимой цены из письма')
        elif len(eligible) < 3:
            missing.append(f'{pos.number}: {len(eligible)} из 3 независимых допустимых поставщиков')
        prepared.append((result,eligible if unique_letters or own_equipment else eligible[-3:],pool))
    if missing:
        raise ValueError('Итоговые документы не готовы: '+ '; '.join(missing)+
                         '. Сохраните предварительный отчёт и дополните предложения.')
    # Do not mutate any row until every row is eligible.
    for result,offers,pool in prepared:
        result.all_offers = list(pool)
        result.offers = offers
    return results


def coverage_notice(product):
    """Coverage is disclosure, never financial authority or absence evidence."""
    if product.get('acquisition_completion'):
        return ''
    return ('Полнота проверки всех источников не подтверждена. Документы формируются только '
            'по проверенным предложениям; ошибки, неопределённые и непроверенные источники '
            'сохранены в сводке. Неизвестные причины не восстановлены.')


def require_terminal_research(job,*,local_terminals=None):
    """Read durable lifecycle at both final boundaries; client readiness is irrelevant."""
    from .search.session import bounded_timeout
    bounded_timeout(0)
    from .search.local_terminal import VerifiedLocalTerminal,_ISSUER,REASON
    if type(job) is not dict:raise ValueError('Invalid research lifecycle')
    local_terminals=local_terminals or {}
    def local_row(row):
        proof=local_terminals.get(row.get('catalogue_product_id'))
        return (type(proof) is VerifiedLocalTerminal and proof._issuer is _ISSUER
                and proof.owner==job.get('job_id') and row.get('catalogue_acquisition_error')==proof.error)
    local_error=(job.get('status')=='error' and job.get('error_code')=='catalogue_acquisition_failed'
                 and job.get('error')==REASON and any(local_row(r) for r in job.get('rows',[]) if r))
    if (type(job) is not dict or (job.get('status')!='done' and not local_error)
            or job.get('cancel_requested') or job.get('first_stop') is not None
            or (not local_error and (job.get('error') or job.get('error_code')))
            or any(job.get(k) for k in ('awaiting_provider','awaiting_supplier','awaiting_slice',
                                      'catalogue_awaiting_products'))
            or type(job.get('completed')) is not int or job['completed']!=job.get('total')
            or not job.get('rows') or any(not r or r.get('status') in
                {'pending','running','stale','error'} or r.get('catalogue_acquisition_running')
                or (r.get('catalogue_acquisition_error') and not local_row(r)) for r in job['rows'])):
        raise ValueError('Итоговые документы не готовы: поиск не завершён или остановлен')

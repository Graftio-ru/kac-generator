"""Visible main-card purchase evidence; no metadata or model-derived quantities."""
from __future__ import annotations

from .search.failure_policy import ResearchControlError
import math
import re
import time
from pathlib import Path

from .purchase_scope import DOM_SCOPE, DOM_ARTICLE_FACTS, scope_review, scope_diagnostic

REVIEW = "Цена/единица продажи не подтверждены читаемым снимком основной карточки"

# A visible currency text node must share the main heading's smallest purchase
# container. Hidden data, related cards, navigation and cart summaries are excluded.
DOM_SNAPSHOT = r"""documentHint => {
 const identityOnly=!!(documentHint && typeof documentHint==='object' && documentHint.identity_only===true);
 const hint=typeof documentHint==='string'?documentHint:documentHint?.document_url;
 const document_url=location.href==='about:blank' && typeof hint==='string' ? hint : location.href;
 const visible = e => { const r=e.getBoundingClientRect();
   if(r.width<=0 || r.height<=0) return false;
   for(let p=e;p;p=p.parentElement) { const s=getComputedStyle(p);
     if(s.display==='none' || s.visibility!=='visible' || Number(s.opacity)===0) return false; }
   const s=getComputedStyle(e); return !/^rgba\([^)]*,\s*0\)$/.test(s.color); };
 const excluded = e => !!e.closest('nav,header,aside,[class*="recommend"],[class*="related"],[class*="cross-sell"],[class*="cart-summary"],[class*="cart-total"],[class*="mini-cart"],[class*="basket-total"]');
 const hs=[...document.querySelectorAll('h1')].filter(visible);
 const empty = reason => {let u='';try{const v=new URL(document_url);u=v.origin+v.pathname;}catch{}
   return {_snapshot_diagnostic:{reason,document_url:u,ready_state:document.readyState,visible_heading_count:hs.length}};};
 if(hs.length!==1) return empty(hs.length?'heading_ambiguous':'heading_missing');
 const h=hs[0]; let root=h.parentElement;
 // Identity observes rendered main-card facts only: no monetary discovery,
 // purchase provenance, per-price geometry or page-wide overlay inventory.
 if(identityOnly) {
   if(excluded(h))return empty('heading_excluded');
   root=h.closest('main,article,form,[itemscope][itemtype*="Product"]');
   if(!root) {root=h.parentElement;while(root?.parentElement && root.parentElement!==document.body && root.parentElement!==document.documentElement)root=root.parentElement;}
   if(!root || root===document.body || root===document.documentElement)return empty('identity_root_missing');
__IDENTITY_ARTICLE_FACTS__
   const facts=articleFacts(root);
   if(facts.length>64)return empty('identity_article_fact_limit');
   const article_facts={version:2,owner:nodeKey(root),facts:facts.map(({rect,...fact})=>fact),values:[...new Set(facts.map(f=>f.value))]};
   let text=root.innerText||'';
   text=text.split(/(?:Рекомендуем|Рекомендованные товары|С этим товаром|Похожие товары|Вы смотрели|Ваша корзина)/i)[0];
   for(const e of root.querySelectorAll('*')) {
     if(excluded(e)||!visible(e)) {const t=e.innerText;if(t)text=text.replace(t,'');}
   }
   return {url:location.href,title:h.innerText.trim(),text:text.slice(0,22000),article_facts};
 }
 let top_overlay_bottom=0;
 for(const e of document.querySelectorAll('body *')) {
   const s=getComputedStyle(e);if(!['fixed','sticky'].includes(s.position) || !visible(e)) continue;
   const r=e.getBoundingClientRect();
   if(r.top<=2 && r.bottom>0 && r.width>=innerWidth/2)
     top_overlay_bottom=Math.max(top_overlay_bottom,Math.min(innerHeight,r.bottom));
 }

 const currency=/(?:₽|(?<![\p{L}])(?:руб(?:\.|лей|ля|ль)?|RUB)(?![\p{L}]))/iu;
 const monetaryLiteral=/(?<![\d.,])(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,]\d{1,2})?\s*(?:₽|руб(?:\.|лей|ля|ль)?|RUB)(?![\p{L}])/iu;
 const hasMoney = container => {const w=document.createTreeWalker(container,NodeFilter.SHOW_TEXT);let n;
   while(n=w.nextNode()){const e=n.parentElement,t=(n.textContent||'').trim();
     if(!e || !visible(e) || excluded(e) || !currency.test(t))continue;
     const p=/\d/.test(t)?e:e.parentElement;
     if(p && visible(p) && !excluded(p) && (p.innerText||'').length<=240 && monetaryLiteral.test(p.innerText||''))return true;
   }return false;};
 while(root && root!==document.body && !hasMoney(root)) root=root.parentElement;
 if(identityOnly && (!root || root===document.body || root===document.documentElement))root=h.closest('main,article,form,[itemscope][itemtype*="Product"]');
 if(!root || root===document.body || root===document.documentElement) return empty('monetary_root_missing');
__PURCHASE_SCOPE__
 const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT); let node,prices=[],fragments=[];
 while(node=walker.nextNode()) {
   const e=node.parentElement,t=(node.textContent||'').trim();
   if(!t || !visible(e) || excluded(e)) continue;
   const er=e.getBoundingClientRect(),et=e.innerText||'';
   if(et.length<=240) fragments.push({text:et,x:er.x+scrollX,y:er.y+scrollY,width:er.width,height:er.height,purchase_scope_id:purchaseScope(e)?.owner_id||'',purchase_basis_id:purchaseScope(e)?.basis_owner_id||nodeKey(e)});
   // Labels assembled from sibling spans have no single text-node owner.
   // Retain their real, bounded visible parent geometry without synthesizing text.
   for(let a=e.parentElement;a && root.contains(a);a=a.parentElement) {
     const at=a.innerText||''; if(at.length>350) break;
     if(visible(a) && !excluded(a)) { const ar=a.getBoundingClientRect();
       fragments.push({text:at,x:ar.x+scrollX,y:ar.y+scrollY,width:ar.width,height:ar.height,purchase_scope_id:purchaseScope(a)?.owner_id||'',purchase_basis_id:purchaseScope(a)?.basis_owner_id||nodeKey(a)}); }
     if(a===root) break;
   }
   if(!currency.test(t)) continue;
   // A detached currency symbol is legitimate only beside visible numeric text.
   const p=currency.test(t)&&/\d/.test(t)?e:e.parentElement;
   if(!p || !visible(p) || excluded(p)) continue;
   const text=p.innerText.trim(); if(text.length>240 || !/\d/.test(text)) continue;
   const r=p.getBoundingClientRect(),parent=p.parentElement;
   const unit_context=parent && visible(parent) && !excluded(parent) && parent.innerText.length<=350 ? parent.innerText : text;
   prices.push({text,unit_context,purchase_scope:purchaseScope(p),purchase_scope_observation:panelTrace(p),purchase_diagnostics:purchaseDiagnostics(p),x:r.x+scrollX,y:r.y+scrollY,width:r.width,height:r.height});
 }
 const hr=h.getBoundingClientRect();
 let text=root.innerText||'';
 text=text.split(/(?:Рекомендуем|Рекомендованные товары|С этим товаром|Похожие товары|Вы смотрели|Ваша корзина)/i)[0];
 const related=[...root.querySelectorAll('*')].filter(e=>identityOnly?(excluded(e)||!visible(e)):e.matches('[class*="recommend"],[class*="related"],[class*="cross-sell"]'));
 for(const e of related) { const t=e.innerText; if(t) text=text.replace(t,''); }
 return {url:location.href,title:h.innerText.trim(),text:text.slice(0,22000),prices,fragments,
   heading:{x:hr.x+scrollX,y:hr.y+scrollY,width:hr.width,height:hr.height},
   viewport:{width:innerWidth,height:innerHeight},scroll:{x:scrollX,y:scrollY},top_overlay_bottom};
}"""

DOM_SNAPSHOT=DOM_SNAPSHOT.replace('__PURCHASE_SCOPE__',DOM_SCOPE).replace('__IDENTITY_ARTICLE_FACTS__',DOM_ARTICLE_FACTS)


def _unit(value):
    from .search.agent import _sale_unit
    return _sale_unit(value)


def _sale_unit_token(sale):
    return {'м':r'(?:м\b|метр\b)', 'шт':r'(?:шт\b|штук)',
            'уп':r'уп(?:ак|аковк)?', 'компл':r'компл',
            'кг':r'(?:кг\b|килограмм\w*)', 'л':r'(?:л\b|литр\w*)'}.get(
                sale, re.escape(sale)+r'(?!\w)' if sale else '')


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError):
        return None


def _sale_lot(sale):
    """An explicit price denominator, never a pack or order counter."""
    match = re.fullmatch(r'(\d+(?:[.,]\d+)?)\s*(м|шт)\.?', sale)
    if not match:
        return None
    quantity = _number(match[1].replace(',', '.'))
    if not quantity or (match[2] == 'шт' and not quantity.is_integer()):
        return None
    return quantity, match[2], match[1]


def _snapshot(page):
    try:
        snapshot=page.evaluate(DOM_SNAPSHOT,getattr(page,"_kac_document_url",None))
    except Exception as error:
        page._kac_snapshot_diagnostic={'reason':'evaluation_error','exception_type':type(error).__name__}
        raise
    page._kac_snapshot_diagnostic={}
    if isinstance(snapshot,dict) and isinstance(snapshot.get('_snapshot_diagnostic'),dict):
        diagnostic=snapshot['_snapshot_diagnostic']
        page._kac_snapshot_diagnostic={k:diagnostic[k] for k in
            ('reason','document_url','ready_state','visible_heading_count') if k in diagnostic}
        status=getattr(page,'_kac_response_status',None)
        if isinstance(status,int) and not isinstance(status,bool):
            page._kac_snapshot_diagnostic['response_status']=status
        return {}
    if not isinstance(snapshot,dict):
        return None
    from .search.product_identity import canonical_card_url
    actual=canonical_card_url(snapshot.get("url"))
    document=canonical_card_url(getattr(page,"_kac_document_url",""))
    if actual and document and actual!=document:
        page._kac_snapshot_diagnostic={'reason':'document_url_mismatch'}
        return {}
    if not actual:
        if snapshot.get("url")!="about:blank" or not document:
            page._kac_snapshot_diagnostic={'reason':'document_url_unconfirmed'}
            return {}
        snapshot={**snapshot,"url":document}
    if isinstance(snapshot.get('prices'),list):
        from .search.product_identity import main_card_article
        text=snapshot.get('text');article=main_card_article(text) if isinstance(text,str) else ''
        snapshot={**snapshot,'prices':[{**p,'purchase_scope_diagnostic':scope_diagnostic(snapshot,p,article=article)}
            if isinstance(p,dict) else p for p in snapshot['prices']]}
    return snapshot


def visible_prices(text):
    """Finite positive currency amounts, never articles or invalid placeholders."""
    candidates = re.findall(
        r"(?<![\d.,])([+−-]?\s*(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,]\d{1,2})?)\s*(?:₽|руб(?:\.|лей|ля|ль)?|RUB)(?![^\W\d_])",
        text, re.I)
    values = (_number(re.sub(r"\s", "", value).replace(",", ".").replace("−", "-"))
              for value in candidates)
    return [value for value in values if value is not None]


def qualify_snapshot(snapshot, *, url, listed, normalized, sale_unit, requested_unit,
                     title, article="", expected_mark="", expected_name="",_candidate=None,_excluded_nodes=()):
    """Return deterministic normalization proof only for literal same-card fields."""
    from .search.product_identity import canonical_card_url, main_card_article
    listed, normalized = _number(listed), _number(normalized)
    if (not isinstance(snapshot, dict) or not listed or not normalized
            or canonical_card_url(snapshot.get("url")) != canonical_card_url(url)
            or not snapshot.get("title") or snapshot["title"] != title):
        return {}, REVIEW + ": адрес/название/цена не совпадают"
    text = re.split(r"(?:Рекомендуем|Рекомендованные товары|С этим товаром|Похожие товары|Вы смотрели|Ваша корзина)",snapshot.get("text", ""),flags=re.I)[0]
    observed_article = main_card_article(text)
    if not observed_article:
        from .purchase_scope import scope_observed_article
        local={scope_observed_article(p.get('purchase_scope')) for p in snapshot.get('prices',[]) if isinstance(p,dict) and any(n and math.isclose(n,listed,rel_tol=1e-8,abs_tol=.005) for n in visible_prices(str(p.get('text',''))))}
        local.discard('')
        if len(local)==1:observed_article=local.pop()
    if expected_mark:
        from .search.product_identity import rendered_identity, card_host
        if not rendered_identity(url,snapshot.get("url"),snapshot["title"],observed_article,
                                 expected_mark,expected_name,card_host(url)):
            return {}, REVIEW + ": идентичность товара на снимке не доказана"
    if article and observed_article != article:
        return {}, REVIEW + ": артикул снимка не совпадает"
    candidates = [p for p in snapshot.get("prices", []) if isinstance(p, dict)
                  and any(n and math.isclose(n, listed, rel_tol=1e-8, abs_tol=.005)
                          for n in visible_prices(str(p.get("text", ""))))
                  and p.get("text") in text]
    if not candidates:
        return {}, REVIEW + ": показанная цена отсутствует"
    scope_diagnostics=[scope_diagnostic(snapshot,p,article=observed_article) for p in candidates]
    refusals=[d['code'] for d in scope_diagnostics]
    candidates=[p for p,r in zip(candidates,refusals) if not r and p['purchase_scope']['price_node'] not in _excluded_nodes]
    if not candidates:
        detail=next((d for d in scope_diagnostics if d['code']),None)
        reason=(detail['code']+': '+detail['subcause']) if detail else ('purchase_occurrences_unreadable' if _excluded_nodes else 'purchase_scope_missing')
        return {}, REVIEW + ': '+reason
    if _candidate is None:
        proofs=[]
        for candidate in candidates:
            proof,why=qualify_snapshot(snapshot,url=url,listed=listed,normalized=normalized,sale_unit=sale_unit,
                requested_unit=requested_unit,title=title,article=article,expected_mark=expected_mark,
                expected_name=expected_name,_candidate=candidate,_excluded_nodes=_excluded_nodes)
            if proof:
                frame,_=_capture_frame(snapshot,proof)
                proofs.append((not bool(frame),frame.get('height',float('inf')),frame.get('width',float('inf')),proof['purchase_scope']['price_node'],proof))
        if not proofs:return {},why
        return min(proofs,key=lambda row:row[:-1])[-1],''
    candidates=[_candidate]

    if re.search(r"(?:цена\s+от|от\s+\d|по запросу|ориентировочн)", " ".join(p["text"] for p in candidates), re.I):
        return {}, REVIEW + ": нет точной цены покупки"
    sale, requested = _unit(sale_unit), _unit(requested_unit)
    lot = _sale_lot(sale)
    unit_pattern = r'(?:/|\bза(?=\s|\d|$)\s*(?:1\s*)?|единица[^\n:]*:\s*)\s*'+_sale_unit_token(sale) if sale else ''
    if lot:
        unit_pattern = r"(?:/|\bза(?=\s|\d|$)\s*)\s*"+re.escape(lot[2])+r"\s*"+re.escape(lot[1])+r"\b"
    # Unit evidence can appear on a separate visible line, but title-only pack
    # wording is insufficient to say the price covers that pack.
    selected = candidates[0]
    scope=selected['purchase_scope']
    structural_unit=scope.get('basis_unit_text','') if scope.get('basis_version')==2 and scope.get('basis_kind') in {'table_unit_cell','labelled_unit_value'} else ''
    if structural_unit and (lot or _unit(structural_unit)!=sale):
        return {}, REVIEW + ': purchase_denominator_conflicting_unit'
    if structural_unit:
        unit_pattern=r'(?<!\w)'+re.escape(structural_unit)+r'(?!\w)'
    from .purchase_scope import price_applicability
    tier_lines=[line for line in scope.get('tier_prices',[]) if visible_prices(line) and price_applicability(line)!='offline_store']
    if scope.get('tier_selected') is True:
        tier_lines=[scope['tier_text']]
    # Different sale denominations are arithmetic bases, not competing tiers.
    # Unlabelled currency remains ambiguous instead of borrowing a sibling unit.
    tier_lines=[line for line in tier_lines if not re.search(r'(?:/|\bза(?=\s|\d|$)\s*)\s*(?:\d+\s*)?(?:м\b|шт\b|уп\w*|компл\w*|кг\b|л\b)',line,re.I) or (unit_pattern and re.search(unit_pattern,line,re.I))]
    tier_values={v for line in tier_lines for v in visible_prices(line) if v}
    tier_signatures={re.sub(r'\s+',' ',line).strip().casefold() for line in tier_lines}
    if len(tier_values)>1 or (scope.get('tier_selected') is not True and len(tier_signatures)>1) or (scope.get('tier_selected') is True and listed not in tier_values):
        return {}, REVIEW + ': purchase_tier_ambiguous'
    purchase_text=scope.get('basis_text','')
    if not isinstance(purchase_text,str) or not purchase_text:
        return {}, REVIEW + ': purchase_scope_missing'
    # Compare identity-proven occurrences across bounded panels as well as
    # within one panel. Equal numbers do not merge different visible tiers.
    cohort=[]
    for occurrence in snapshot.get('prices',[]):
        if not isinstance(occurrence,dict) or scope_review(snapshot,occurrence,article=observed_article):continue
        authority=occurrence['purchase_scope']
        if not (unit_pattern and re.search(unit_pattern,authority['basis_text'],re.I)):continue
        amounts=tuple(visible_prices(occurrence.get('text','')))
        if not amounts:continue
        context=re.sub(r'\s+','',authority['basis_text']).casefold()
        cohort.append((authority,amounts,context))
    chosen=[row for row in cohort if row[0]['tier_selected']]
    active=chosen or cohort
    signatures={(row[1],row[2]) for row in active}
    if len(signatures)>1 or (chosen and not scope['tier_selected']):
        return {}, REVIEW + ': purchase_tier_ambiguous'
    own = selected["text"]
    explicit_unit = r"(?:/|\bза(?=\s|\d|$)\s*(?:1\s*)?)\s*(?:шт\b|м\b|уп\w*|компл\w*)"
    if re.search(explicit_unit,own,re.I) and not (unit_pattern and re.search(unit_pattern,own,re.I)):
        return {}, REVIEW + ": цена относится к другой единице"
    context = scope['basis_text'] if scope.get('basis_version')==2 else (own if unit_pattern and re.search(unit_pattern,own,re.I) else selected.get("unit_context",""))
    if any(n and not math.isclose(n,listed,rel_tol=1e-8,abs_tol=.005) for n in visible_prices(context)):
        return {}, REVIEW + ": единица относится к другой цене"
    if lot:
        denominators = {_number(n.replace(',', '.')) for n in re.findall(
            r'(?:/|\bза(?=\s|\d|$)\s*)\s*(\d+(?:[.,]\d+)?)\s*'+re.escape(lot[1])+r'\b', context, re.I)}
        if denominators != {lot[0]}:
            return {}, REVIEW + ': количество единиц в цене противоречиво'
    unit_text = structural_unit or "\n".join(line for line in context.splitlines()
                          if line in purchase_text and unit_pattern and re.search(unit_pattern,line,re.I))
    if not unit_text and scope.get('basis_version')==2 and unit_pattern:
        match=re.search(unit_pattern,purchase_text,re.I)
        if match:unit_text=match.group(0)
    if not unit_text:
        return {}, REVIEW + ": единица показанной цены не видна"
    quantity, basis = 1.0, unit_text
    if sale != requested:
        if lot and requested == lot[1]:
            quantity = lot[0]
            if not quantity:
                return {}, REVIEW + ": длина партии не доказана"
        elif sale == "шт" and requested == "компл":
            from .search.agent import _piece_is_complete_set, _explicit_piece_pack_size
            if not _piece_is_complete_set(snapshot["title"]) or _explicit_piece_pack_size(purchase_text) not in {None,1}:
                return {}, REVIEW + ": состав комплекта не доказан"
            basis = snapshot["title"]
        elif sale == "уп" and requested in {"м", "шт"}:
            token = "(?:м|метр[аов]*)" if requested == "м" else "шт\\.?"
            packs = re.findall(r"(?:упаковк[аеаи]?|в\s+1\s+упаковке)\s*[:(]?\s*(\d+(?:[.,]\d+)?)\s*"+token+r"\b",purchase_text,re.I)
            quantities = {_number(n.replace(",", ".")) for n in packs}
            if len(quantities) != 1 or None in quantities:
                return {}, REVIEW + ": количество в упаковке не доказано"
            quantity = quantities.pop()
            basis = next(line for line in purchase_text.splitlines() if re.search(r"упаковк",line,re.I) and re.search(r"\d",line))
        elif {sale,requested} == {"шт","м"}:
            from .search.agent import _product_length_metres
            length = _product_length_metres(snapshot["title"])
            if not length:
                return {}, REVIEW + ": длина одной штуки не доказана"
            quantity = length if sale == "шт" else 1/length
            basis = snapshot["title"]
        else:
            return {}, REVIEW + ": соответствие упаковки/комплекта не доказано"
    expected = listed / quantity
    if not math.isclose(normalized, expected, rel_tol=1e-8, abs_tol=.005):
        return {}, REVIEW + ": пересчёт не совпадает с показанной ценой"
    return {"version":1,"url":url,"title":title,"article":observed_article,
            "purchase_scope":scope,"basis_scope_ids":[scope["basis_owner_id"]],
            "listed_price":listed,"normalized_price":normalized,"sale_unit":sale,
            "requested_unit":requested,"quantity":quantity,"basis":basis,
            "unit_text":unit_text,"price_text":candidates[0]["text"],
            "price_rect":{k:candidates[0].get(k) for k in ("x","y","width","height")},
            "heading_rect":snapshot.get("heading",{})}, ""


def ready_price_snapshot(page):
    """Up to20s within parent budget; two stable current observations exit early."""
    from .search.session import bounded_timeout
    deadline=time.monotonic()+bounded_timeout(20)
    previous=None
    while time.monotonic()<deadline:
        try:
            snapshot=_snapshot(page)
            if not isinstance(snapshot,dict):
                return {}
            if time.monotonic() >= deadline:
                return {}
            signature=(snapshot.get("url"),snapshot.get("title"),snapshot.get("text"),
                       tuple(p.get("text","") for p in snapshot.get("prices",[])))
            if snapshot.get("prices") and signature==previous:
                return snapshot
            previous=signature
            page.wait_for_timeout(max(1,int(bounded_timeout(min(.3, max(0,deadline-time.monotonic())))*1000)))
        except ResearchControlError:
            raise
        except Exception:
            return {}
    return {}


def visible_quote_fields(snapshot,requested_unit,*,url,title,expected_mark="",expected_name=""):
    """Prefer literal purchase fields over a model's inferred pack total."""
    if not isinstance(snapshot,dict):
        return {}
    options={}
    for p in snapshot.get("prices",[]):
        prices=[n for n in visible_prices(p.get("text","")) if n]
        if len(prices)!=1:
            continue
        listed=prices[0]
        own=p.get("text","")
        context=own if re.search(r"(?:/|\bза(?=\s|\d|$)\s*)\s*(?:\d+\s*)?(?:м\b|шт\b|уп\w*|компл\w*)",own,re.I) else p.get("unit_context","")
        scope=p.get('purchase_scope') if isinstance(p.get('purchase_scope'),dict) else {}
        structural=scope.get('basis_unit_text','') if scope.get('basis_version')==2 and scope.get('basis_kind') in {'table_unit_cell','labelled_unit_value'} else ''
        if scope.get('basis_kind')=='price_line':context=scope.get('basis_text','')
        units=[structural] if structural else re.findall(r"(?:/|\bза(?=\s|\d|$)\s*)\s*((?:\d+(?:[.,]\d+)?\s*)?(?:м\b|метр\w*|шт\.?|уп(?:ак(?:овк[ауе])?)?\.?|компл\.?))",context,re.I)
        for sold in units:
            sale=_unit(re.sub(r"^1\s+","",sold))
            if sale==_unit(requested_unit):
                normalized=listed
            else:
                # Candidate divisors come only from explicit visible card text;
                # qualify_snapshot independently checks which basis is valid.
                values=re.findall(r"упаковк[аеаи]?\s*[:(]?\s*(\d+(?:[.,]\d+)?)\s*(?:м\b|шт\b)",snapshot.get("text",""),re.I)
                lot=_sale_lot(sale)
                if lot and _unit(requested_unit)==lot[1]:values=[lot[0]]
                if sale=="шт" and _unit(requested_unit)=="м":
                    from .search.agent import _product_length_metres
                    values=[_product_length_metres(title)]
                quantities={_number(str(v).replace(",",".")) for v in values}
                if len(quantities)!=1 or None in quantities:
                    continue
                normalized=listed/quantities.pop()
            proof,reason=qualify_snapshot(snapshot,url=url,listed=listed,normalized=normalized,
                sale_unit=sale,requested_unit=requested_unit,title=title,
                expected_mark=expected_mark,expected_name=expected_name)
            if proof and not reason:
                options[(listed,sale,normalized)]=proof
    direct={key:proof for key,proof in options.items() if key[1]==_unit(requested_unit)}
    if direct:
        options=direct
    if len(options)!=1:
        return {}
    (listed,sale,normalized),proof=next(iter(options.items()))
    return dict(price_with_vat=normalized,listed_price_with_vat=listed,price_unit=sale,
                package_quantity=proof["quantity"],listed_price_covers_multiple_requested_units=sale!=_unit(requested_unit))


def current_literal_quote_fields(page, requested_unit, *, url, title, expected_mark="", expected_name=""):
    """One post-extraction DOM observation; no extra readiness/retry budget."""
    snapshot = _snapshot(page)
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    return snapshot, visible_quote_fields(snapshot, requested_unit, url=url, title=title,
                                         expected_mark=expected_mark, expected_name=expected_name)


def _capture_required_fields(snapshot,evidence):
    rects = [dict(evidence["heading_rect"],role="heading",text=evidence["title"]),
             dict(evidence["price_rect"],role="price",text=evidence["price_text"])]
    article = evidence["article"]
    from .search.product_identity import main_card_article
    primary = re.split(r'(?im)^\s*(?:Рекомендуем|Рекомендации|Похожие товары|С этим товаром|Сопутствующие товары|Вы смотрели)', snapshot.get('text',''))[0]
    printed = {m.group(1) for m in re.finditer(r'(?im)^\s*(?:Артикул(?: производителя| товара)?|арт\.|Код производителя)\s*[:：]?\s*\n?\s*([A-Za-zА-Яа-я0-9._-]{3,})(?=\s|$)',primary)
               if ':' in m.group(0) or '：' in m.group(0) or re.search(r'\d',m.group(1))}
    if article and printed - {article}:
        return {}, REVIEW + ": конфликт артикулов основной карточки"
    # Identity is already qualified against this current main card. Capture its
    # printed article once, rather than every marketing/video repetition.
    if article and not re.search(r"(?<![\w.-])"+re.escape(article)+r"(?![\w.-])",evidence["title"]):
        if main_card_article(primary) != article:
            return {}, REVIEW + ": артикул основной карточки вне снимка"
        related_y = min((f.get('y', math.inf) for f in snapshot.get('fragments', [])
                         if re.match(r'\s*(?:Рекомендуем|Рекомендации|Похожие товары|С этим товаром|Сопутствующие товары|Вы смотрели)', f.get('text',''))), default=math.inf)
        matches = [f for f in snapshot.get("fragments",[])
                   if main_card_article(f.get("text", "")) == article
                   and f.get('y', math.inf) < related_y]
        if not matches:
            return {}, REVIEW + ": артикул основной карточки вне снимка"
        rects.append(dict(min(matches,key=lambda f:f.get("width",0)*f.get("height",0)),role="article"))
    for line in sorted(set([evidence["basis"],*evidence["unit_text"].splitlines()])):
        if line in evidence["title"] or line in evidence["price_text"]:
            continue
        matches=[f for f in snapshot.get("fragments",[]) if line in f.get("text","") and f.get("purchase_scope_id")==evidence.get("purchase_scope",{}).get("owner_id") and f.get('purchase_basis_id')==evidence.get('purchase_scope',{}).get('basis_owner_id')]
        if not matches:
            return {}, REVIEW + ": основание пересчёта вне снимка"
        rects.append(dict(min(matches,key=lambda f:f.get("width",0)*f.get("height",0)),role="basis_or_unit"))
    return rects,""


def _capture_frame(snapshot,evidence):
    rects,reason=_capture_required_fields(snapshot,evidence)
    if not rects:
        return {},reason
    try:
        left=max(0,min(r["x"] for r in rects)-20);top=max(0,min(r["y"] for r in rects)-20)
        required_right=max(r["x"]+r["width"] for r in rects)
        right=required_right+20
        viewport_width = snapshot.get('viewport', {}).get('width')
        if isinstance(viewport_width, (int,float)) and viewport_width > 0:
            # Optional padding must not make an otherwise readable full-width
            # heading exceed the viewport. Never crop a required field.
            right=min(right,max(required_right,viewport_width))
        bottom=max(r["y"]+r["height"] for r in rects)+20
        region=dict(x=left,y=top,width=right-left,height=bottom-top)
        if (region["height"]>1600 or region["width"]>1366 or
                region["height"]<=0 or region["width"]<=0):
            return {}, REVIEW + ": поля карточки не помещаются в читаемый снимок"
    except (KeyError,TypeError,ValueError):
        return {}, REVIEW + ": положение цены не подтверждено"
    return region,""


def _viewport_capture_region(region, visibility):
    """Trim optional frame padding only; every verified glyph must remain captured."""
    try:
        viewport,scroll=visibility['viewport'],visibility['scroll']
        left=max(region['x'],scroll['x']);top=max(region['y'],scroll['y'])
        right=min(region['x']+region['width'],scroll['x']+viewport['width'])
        bottom=min(region['y']+region['height'],scroll['y']+viewport['height'])
        if right<=left or bottom<=top:return {},{}
        for field in visibility['fields']:
            if field.get('uncovered') is not True or not field.get('rects'):return {},{}
            for r in field['rects']:
                if r['x']<left or r['y']<top or r['x']+r['width']>right or r['y']+r['height']>bottom:return {},{}
        adjusted=dict(x=left,y=top,width=right-left,height=bottom-top)
        clip={**adjusted,'x':left-scroll['x'],'y':top-scroll['y']}
        return adjusted,clip
    except (KeyError,TypeError,ValueError):
        return {},{}


def _safe_capture_error(stage, error):
    """Classify in memory; never persist browser messages, URLs or credentials."""
    message = str(error).casefold()
    timeout = 'timeout' in type(error).__name__.casefold() or 'timeout' in message
    if timeout and 'waiting for fonts' in message and 'fonts loaded' not in message:
        cause = 'font_readiness_timeout'
    elif 'clipped area is either empty or outside' in message:
        cause = 'clip_outside_page'
    elif timeout and stage == 'price_screenshot_failed':
        cause = 'screenshot_timeout'
    else:
        cause = 'unknown'
    return dict(capture_stage=stage, capture_error_type=type(error).__name__,
                capture_error_cause=cause)


def reconcile_capture_identity(evidence, identity, snapshot):
    """Bind price proof to its own current card, never an earlier heading."""
    from .search.product_identity import canonical_card_url, valid_identity
    if not evidence:
        return {}, ''
    if not valid_identity(identity):
        return {}, REVIEW + ': актуальная идентичность карточки не подтверждена'
    if (canonical_card_url(evidence.get('url')) != identity.get('canonical_url')
            or evidence.get('title') != identity.get('main_title')
            or evidence.get('article') != identity.get('main_article')):
        return {}, REVIEW + ': карточка после снимка не совпадает с карточкой цены'
    confirmed, reason = qualify_snapshot(snapshot, url=evidence.get('url'),
        title=evidence.get('title'), article=evidence.get('article'),
        listed=evidence.get('listed_price'), normalized=evidence.get('normalized_price'),
        sale_unit=evidence.get('sale_unit'), requested_unit=evidence.get('requested_unit'),
        expected_mark=identity.get('expected_mark'), expected_name=identity.get('expected_name'))
    keys = ('url','title','article','listed_price','normalized_price','sale_unit',
            'requested_unit','quantity','basis','unit_text','price_text')
    if not confirmed or any(confirmed.get(k) != evidence.get(k) for k in keys):
        return {}, reason or REVIEW + ': цена/единица/состав изменились после снимка'
    return evidence, ''


# Observations never establish font identity or change capture eligibility. Exact
# computed shorthand and actual node text stay in-browser; only bounded counts
# and check outcomes are persisted (no text, family names, URLs or font bytes).
FONT_RUN_OBSERVATION = r"""required => {
 const started=performance.now(),limit=24,scanLimit=128;
 const fields=Array.isArray(required)?required.slice(0,64):null;
 const out={version:1,scope:Array.isArray(required)?'qualified_required_fields':'preview_candidates',
   observational_only:true,font_identity:'unproven',runs:[],scanned_nodes:0,truncated:false,
   required_field_count:Array.isArray(required)?Math.min(64,required.length):0,
   coverage:'bounded_observation_not_complete_glyph_inventory',
   faces:{loaded:0,loading:0,unloaded:0,error:0,unknown:0,truncated:false}};
 if(fields && (required.length>64 || fields.some(f=>!f || typeof f.text!=='string' || f.text.length>512)))out.truncated=true;
 const matchFields=fields?fields.filter(f=>f && typeof f.text==='string' && f.text.length<=512):null;
 try {
  if(document.fonts && typeof document.fonts[Symbol.iterator]==='function'){
   let count=0;for(const face of document.fonts){if(count++>=256){out.faces.truncated=true;break;}
    const key=['loaded','loading','unloaded','error'].includes(face.status)?face.status:'unknown';out.faces[key]++;}
  }
  if(!document.body || !document.createTreeWalker){out.unavailable=true;return out;}
  const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);let node;
  while((node=walker.nextNode())){
   if(++out.scanned_nodes>scanLimit || out.runs.length>=limit || performance.now()-started>=2){out.truncated=true;break;}
   const text=node.textContent||'',e=node.parentElement;
   if(text.length>512){out.truncated=true;continue;}
   if(!e || !text.trim())continue;
   const r=e.getBoundingClientRect(),style=getComputedStyle(e);
   if(r.width<=0 || r.height<=0 || style.display==='none' || style.visibility!=='visible')continue;
   let role='preview';
   if(Array.isArray(required)){
    const range=document.createRange();range.selectNodeContents(node);const q=range.getBoundingClientRect();
    const f=matchFields.find(f=>f.text.includes(text.trim()) &&
     q.left+scrollX>=f.x-.1 && q.top+scrollY>=f.y-.1 &&
     q.right+scrollX<=f.x+f.width+.1 && q.bottom+scrollY<=f.y+f.height+.1);
    if(!f)continue;role=f.role;
   }else{
    if(!e.closest('h1,main,article,form,[itemscope]'))continue;
   }
   const descriptor=style.font;let check='unsupported_descriptor';
   if(typeof descriptor==='string' && descriptor && descriptor.length<=512 && document.fonts && typeof document.fonts.check==='function'){
    try{check=document.fonts.check(descriptor,text)?'true':'false';}catch{check='check_error';}
   }
   out.runs.push({role:['heading','title','price','unit','basis','article','identity','preview'].includes(role)?role:'required',
    text_code_units:text.length,descriptor_code_units:typeof descriptor==='string'?descriptor.length:0,check,
    extended_font_settings:!!((style.fontVariationSettings && style.fontVariationSettings!=='normal') ||
      (style.fontFeatureSettings && style.fontFeatureSettings!=='normal'))});
  }
 }catch{out.observation_error=true;}
 out.elapsed_ms=performance.now()-started;return out;
}"""

FONT_READY_SCRIPT = """async input => {
 const observe=__OBSERVATION__;
 const timeout=input,started=performance.now();let cycles=0,timer,stopped=false,phase='initial',frames=0,
   readyWait=0,frameWait=0,phaseStarted=started;
 const preview=observe(null);
 const result=(loaded,outcome)=>{
  const now=performance.now(),pending=now-phaseStarted;
  return {loaded,outcome,cycles,elapsed_ms:now-started,
   status:document.fonts?document.fonts.status:'unavailable',render_frames:cycles?2:0,
   diagnostics:{version:1,phase,ready_wait_ms:readyWait+(phase==='await_ready'?pending:0),
    render_wait_ms:frameWait+(phase==='await_frames'?pending:0),render_frames_observed:frames,
    preview_before:preview,preview_after:observe(null)}};
 };
 if(!document.fonts)return result(false,'unavailable');
 try {
  return await Promise.race([
   (async()=>{
     while(!stopped && performance.now()-started<timeout){
       phase='await_ready';phaseStarted=performance.now();
       await document.fonts.ready;if(stopped)return;
       readyWait+=performance.now()-phaseStarted;phase='await_frames';phaseStarted=performance.now();
       await new Promise(resolve=>requestAnimationFrame(()=>{if(stopped){resolve();return;}frames++;
         requestAnimationFrame(()=>{if(!stopped)frames++;resolve();});}));
       if(stopped)return;frameWait+=performance.now()-phaseStarted;cycles++;
       phase='loaded_status_check';phaseStarted=performance.now();
       if(performance.now()-started>=timeout)return result(false,'deadline_exceeded');
       if(document.fonts.status==='loaded')return result(true,'loaded');
     }
     return result(false,'deadline_exceeded');
   })(),
   new Promise(resolve=>{timer=setTimeout(()=>resolve(result(false,'deadline_exceeded')),Math.max(0,timeout-(performance.now()-started)));})
  ]);
 } finally {stopped=true;clearTimeout(timer);}
}""".replace('__OBSERVATION__',FONT_RUN_OBSERVATION)


def _safe_font_observation(value):
    """Strict bounded diagnostic projection, including untrusted browser results."""
    if not isinstance(value,dict):return {'unavailable':True}
    def count(value,cap=100000):
        return min(cap,max(0,value)) if type(value) in (int,float) and math.isfinite(value) else 0
    out={k:value[k] for k in ('unavailable','truncated','observation_error','observational_only') if type(value.get(k)) is bool}
    if value.get('scope') in ('preview_candidates','qualified_required_fields'):out['scope']=value['scope']
    out['font_identity']='unproven'
    out['coverage']='bounded_observation_not_complete_glyph_inventory'
    out['required_field_count']=count(value.get('required_field_count'),64)
    out['scanned_nodes']=count(value.get('scanned_nodes'),129)
    out['elapsed_ms']=count(value.get('elapsed_ms'))
    faces=value.get('faces',{})
    if isinstance(faces,dict):
        out['faces']={k:count(faces.get(k),256) for k in ('loaded','loading','unloaded','error','unknown')}
        out['faces']['truncated']=faces.get('truncated') is True
    out['runs']=[]
    runs=value.get('runs',[])
    if isinstance(runs,list):
        for run in runs[:24]:
            if not isinstance(run,dict):continue
            out['runs'].append({'role':run.get('role') if run.get('role') in ('heading','title','price','unit','basis','article','identity','preview','required') else 'required',
                'check':run.get('check') if run.get('check') in ('true','false','unsupported_descriptor','check_error') else 'check_error',
                'text_code_units':count(run.get('text_code_units'),512),
                'descriptor_code_units':count(run.get('descriptor_code_units'),512),
                'extended_font_settings':run.get('extended_font_settings') is True})
    return out


def _safe_font_readiness(value):
    out={'loaded':value.get('loaded') is True}
    if value.get('status') in ('loaded','loading','unavailable'):out['status']=value['status']
    if value.get('outcome') in ('loaded','deadline_exceeded','unavailable'):out['outcome']=value['outcome']
    for key in ('cycles','elapsed_ms','render_frames'):
        v=value.get(key)
        if type(v) in (int,float) and math.isfinite(v):out[key]=min(100000,max(0,v))
    return out


def _safe_font_diagnostics(value):
    if not isinstance(value,dict):return {}
    out={'version':1}
    phase=value.get('phase')
    if phase in ('initial','await_ready','await_frames','loaded_status_check'):out['phase']=phase
    for key in ('ready_wait_ms','render_wait_ms','render_frames_observed'):
        v=value.get(key)
        if type(v) in (int,float) and math.isfinite(v):out[key]=min(100000,max(0,v))
    for key in ('preview_before','preview_after'):
        if key in value:out[key]=_safe_font_observation(value[key])
    return out


VISIBILITY_FRAME_SCRIPT = """async timeout => {
 let timer;
 try {return await Promise.race([
   new Promise(resolve=>requestAnimationFrame(()=>resolve(true))),
   new Promise(resolve=>{timer=setTimeout(()=>resolve(false),timeout);})
 ]);} finally {clearTimeout(timer);}
}"""

def capture_price(page, path, *, on_refusal=None, on_error=None, **fields):
    """Owned-page bounded placement; restore viewport even after control errors."""
    original=getattr(page,'viewport_size',None)
    original=dict(original) if isinstance(original,dict) else None
    changed=[False,None,False]
    try:
        from .config import settings
        from .pixel_evidence import load_qualification
        mode=getattr(settings,'capture_evidence_mode','auto')
        qualification=load_qualification() if mode in ('auto','pixel_verified') else {}
        if mode=='pixel_verified' and not qualification:
            if on_refusal:on_refusal('pixel_reader_unqualified')
            return {},REVIEW+': независимый читатель снимка не квалифицирован'
        if mode not in ('auto','strict','pixel_verified'):
            if on_refusal:on_refusal('pixel_mode_invalid')
            return {},REVIEW+': режим доказательства не подтверждён'
        if qualification:return _capture_pixel_impl(page,path,on_refusal,on_error,original,changed,fields,qualification)
        return _capture_price_impl(page,path,on_refusal,on_error,original,changed,fields)
    finally:
        if changed[0] and original:
            try:
                page.set_viewport_size(original)
            except Exception:
                pass  # Owned browser cleanup still runs; never mask original control error.
        if changed[2] and changed[1] is not None:
            try:
                page.evaluate('p => window.scrollTo(p.x,p.y)',changed[1])
            except Exception:
                pass  # Preserve the original refusal/control error during cleanup.


def _capture_semantics(value):
    """Immutable quote/ownership facts, distinct from mandatory pixel geometry.

    Only observed fact rectangles are omitted here. Their current facts are
    independently qualified each time; selected frame, glyphs and visibility
    still participate in exact stabilization and post-screenshot comparison.
    """
    def freeze(v, fact_geometry=False):
        if isinstance(v,dict):
            return tuple((k,freeze(x,fact_geometry or k in ('identity_facts','basis_unit_nodes')))
                         for k,x in sorted(v.items()) if not (fact_geometry and k=='rect'))
        if isinstance(v,list):return tuple(freeze(x,fact_geometry) for x in v)
        return (type(v).__name__,v)
    return tuple(freeze(value.get(k)) for k in ('url','article','title','listed_price','normalized_price',
        'sale_unit','requested_unit','quantity','basis','price_text','unit_text','purchase_scope'))


def _capture_price_impl(page,path,on_refusal,on_error,original,changed,fields):
    from .search.session import bounded_timeout
    from .capture_visibility import VISIBILITY_SCRIPT,write_attestation,attestation_path,visibility_valid
    from .search.product_identity import canonical_card_url
    deadline=time.monotonic()+bounded_timeout(8)
    previous=None;prepared=0
    evidence,reason={},REVIEW
    excluded_nodes=set();last_visibility=None
    verified_semantics=None;hit_waiting=False
    pending_semantics={}
    def refuse(stage,message):
        if on_refusal:on_refusal(stage)
        return {},message
    def failed(stage,error,message):
        detail=_safe_capture_error(stage,error)
        if on_error:on_error(detail)
        return refuse(stage,message+': '+detail['capture_error_type']+': '+detail['capture_error_cause'])
    def visible(required):
        from .native_geometry import native_visibility
        value=native_visibility(page,required,deadline,VISIBILITY_SCRIPT)
        if not isinstance(value,dict):return {}
        actual=canonical_card_url(value.get('url'))
        document=canonical_card_url(getattr(page,'_kac_document_url',''))
        if not actual and value.get('url')=='about:blank' and document:value={**value,'url':document}
        return value
    def semantics(value):
        return _capture_semantics(value)
    def hit_mismatch(value):
        refused=[f for f in value.get('fields',[]) if isinstance(f,dict) and f.get('uncovered') is not True]
        return (value.get('verified') is False and value.get('reason')=='occluded' and bool(refused)
                and all(f.get('witness',{}).get('branch')=='topmost_hit_mismatch' for f in refused))
    try:
        font_readiness=page.evaluate(FONT_READY_SCRIPT,max(1,int(bounded_timeout(max(0,deadline-time.monotonic()))*1000)))
    except ResearchControlError:
        raise
    except Exception as error:
        return failed('price_font_readiness_failed',error,REVIEW+': готовность шрифтов не подтверждена')
    if not isinstance(font_readiness,dict) or font_readiness.get('loaded') is not True:
        if on_error and isinstance(font_readiness,dict):
            on_error({'capture_stage':'price_font_readiness_timeout', 'font_readiness_diagnostics':_safe_font_diagnostics(font_readiness.get('diagnostics',{}))})
        return refuse('price_font_readiness_timeout',REVIEW+': шрифты не готовы в пределах времени снимка')
    if time.monotonic()>=deadline:
        return refuse('price_capture_deadline',REVIEW+': время проверки снимка истекло')
    while time.monotonic()<deadline:
        try:
            snapshot=_snapshot(page)
            if time.monotonic()>=deadline:
                return refuse('price_capture_deadline',REVIEW+': время проверки снимка истекло')
            if not isinstance(snapshot,dict):
                return refuse('price_dom_unreadable',REVIEW+': видимая область не прочитана')
            initial_scroll=snapshot.get('scroll',{})
            if changed[1] is None and isinstance(initial_scroll,dict) and all(type(initial_scroll.get(k)) in (int,float) and math.isfinite(initial_scroll[k]) for k in ('x','y')):
                changed[1]=dict(initial_scroll)
            evidence,reason=qualify_snapshot(snapshot,_excluded_nodes=excluded_nodes,**fields)
            if not evidence and last_visibility and reason.endswith('purchase_occurrences_unreadable'):
                # Trial other occurrences before waiting. Exhausting the trials does not
                # establish that a qualified current price is permanently unreadable.
                excluded_nodes.clear();prepared=0;previous=None;hit_waiting=True
                remaining=max(0,deadline-time.monotonic())
                if remaining<=0 or page.evaluate(VISIBILITY_FRAME_SCRIPT,max(1,int(bounded_timeout(remaining)*1000))) is not True:
                    return refuse(*last_visibility)
                continue
            if not evidence and pending_semantics:
                return refuse('price_fields_changed',reason or REVIEW+': текущие поля цены изменились')
            region,frame_reason=_capture_frame(snapshot,evidence) if evidence else ({},reason)
            if evidence and region:
                viewport=snapshot.get('viewport',{})
                clearance=snapshot.get('top_overlay_bottom')
                if (type(clearance) not in (int,float) or not math.isfinite(clearance) or clearance<0
                        or any(type(viewport.get(k)) not in (int,float) or not math.isfinite(viewport[k]) for k in ('width','height'))):
                    return refuse('price_visibility_unverifiable',REVIEW+': видимость обязательных полей не проверена')
                # A full-viewport layer cannot be cleared by enlarging the same
                # viewport: its measured bottom may grow with every resize.
                # Keep the raw observation; only strict glyph visibility can
                # establish readiness after normal bounded placement.
                placement_clearance=0 if clearance>=viewport['height'] else clearance
                if prepared==0:
                    needed=math.ceil(region['height']+placement_clearance+40)
                    if needed>1600:
                        return refuse('price_visibility_offscreen',REVIEW+': обязательные поля не помещаются в проверяемую область')
                    if needed>viewport['height']:
                        if (not original or original['width']!=viewport['width']
                                or getattr(page,'_kac_capture_resize_allowed',False) is not True):
                            return refuse('price_visibility_unverifiable',REVIEW+': исходная область браузера не подтверждена')
                        changed[0]=True
                        page.set_viewport_size({'width':original['width'],'height':needed})
                    prepared=1
                    continue
                if prepared==1:
                    changed[2]=True
                    page.evaluate('p => window.scrollTo(p.x,p.y)',{'x':0,'y':max(0,region['y']-placement_clearance)})
                    prepared=2
                    continue
                required,field_reason=_capture_required_fields(snapshot,evidence)
                required=[{k:f[k] for k in ('role','text','x','y','width','height')} for f in required]
                node=evidence.get('purchase_scope',{}).get('price_node')
                if node in pending_semantics and pending_semantics[node]!=semantics(evidence):
                    return refuse('price_fields_changed',REVIEW+': текущие поля цены изменились во время ожидания видимости')
                visibility=visible(required)
                if time.monotonic()>=deadline:
                    return refuse('price_capture_deadline',REVIEW+': время проверки снимка истекло')
                if (visibility.get('verified') is not True
                        or canonical_card_url(visibility.get('url'))!=canonical_card_url(evidence['url'])
                        or visibility.get('viewport')!=viewport or visibility.get('scroll')!=snapshot.get('scroll')):
                    if hit_mismatch(visibility) and verified_semantics==semantics(evidence):
                        # A hit interceptor may be transient; never certify it as paint-empty.
                        # Reacquire all current fields and require ordinary strict stability again.
                        hit_waiting=True;previous=None
                        last_visibility=('price_visibility_unconfirmed',REVIEW+': видимость обязательного поля не подтверждена')
                        remaining=max(0,deadline-time.monotonic())
                        if remaining<=0 or page.evaluate(VISIBILITY_FRAME_SCRIPT,max(1,int(bounded_timeout(remaining)*1000))) is not True:
                            return refuse(*last_visibility)
                        continue
                    cause=visibility.get('reason','unverifiable')
                    unconfirmed_hit=hit_mismatch(visibility)
                    stage='price_visibility_unconfirmed' if unconfirmed_hit else 'price_visibility_occluded' if cause=='occluded' else 'price_visibility_offscreen' if cause=='offscreen' else 'price_visibility_unverifiable'
                    visibility_reason=REVIEW+': видимость обязательного поля не подтверждена' if unconfirmed_hit else REVIEW+': обязательное поле перекрыто интерфейсом' if cause=='occluded' else REVIEW+': видимость обязательных полей не подтверждена'
                    if visibility.get('verified') is True:
                        return refuse('price_visibility_unverifiable',REVIEW+': область проверки видимости изменилась')
                    if node and node not in excluded_nodes:
                        last_visibility=(stage,visibility_reason)
                        pending_semantics.setdefault(node,semantics(evidence))
                        hit_waiting=True
                        excluded_nodes.add(node);prepared=0;previous=None
                        continue
                    return refuse(stage,visibility_reason)
                verified_semantics=semantics(evidence);hit_waiting=False
            else:
                visibility={};required=[]
        except ResearchControlError:
            raise
        except Exception as error:
            return failed('price_dom_unreadable',error,REVIEW+': видимая область не прочитана')
        stable=(semantics(evidence),region,visibility,required)
        if evidence and region and visibility and stable==previous:break
        previous=stable if evidence and region and visibility else None
        page.wait_for_timeout(max(1,int(bounded_timeout(.3)*1000)))
    else:
        if hit_waiting and last_visibility:return refuse(*last_visibility)
        return refuse('price_fields_unconfirmed' if not evidence else 'price_frame_unconfirmed' if not region else 'price_stability_timeout',reason or frame_reason or REVIEW+': цена/расположение не стабилизировались')
    stage='price_screenshot_failed'
    def observe_required(required):
        try:
            value=page.evaluate(FONT_RUN_OBSERVATION,required)
            return _safe_font_observation(value) if isinstance(value,dict) and value.get('observational_only') is True else {'unavailable':True}
        except ResearchControlError:
            raise
        except Exception:
            return {'unavailable':True}
    try:
        required_before=observe_required(required)
        attestation_path(path).unlink(missing_ok=True)
        capture_region,clip=_viewport_capture_region(region,visibility)
        if not clip:return refuse('price_visibility_offscreen',REVIEW+': обязательные поля вне проверяемого снимка')
        viewport=visibility['viewport']
        if clip['x']<0 or clip['y']<0 or clip['x']+clip['width']>viewport['width']+.1 or clip['y']+clip['height']>viewport['height']+.1:
            return refuse('price_visibility_offscreen',REVIEW+': область снимка выходит за проверяемую область')
        if time.monotonic()>=deadline:
            return refuse('price_capture_deadline',REVIEW+': время проверки снимка истекло')
        page.screenshot(path=str(path),full_page=False,clip=clip,timeout=max(1,int(bounded_timeout(max(0,deadline-time.monotonic()))*1000)))
        if time.monotonic()>=deadline:
            return refuse('price_capture_deadline',REVIEW+': время проверки снимка истекло')
        stage='price_post_capture_failed'
        after=_snapshot(page)
        confirmed,_=qualify_snapshot(after,_excluded_nodes=excluded_nodes,**fields)
        after_region,_=_capture_frame(after,confirmed) if confirmed else ({},'')
        after_required,_=_capture_required_fields(after,confirmed) if confirmed else ([], '')
        after_required=[{k:f[k] for k in ('role','text','x','y','width','height')} for f in after_required]
        after_visibility=visible(after_required) if after_required else {}
        required_after=observe_required(after_required) if after_required else {'unavailable':True}
        if time.monotonic()>=deadline:
            return refuse('price_capture_deadline',REVIEW+': время проверки снимка истекло')
        if (semantics(confirmed),after_region,after_visibility,after_required)!=stable:
            return refuse('price_post_capture_changed',REVIEW+': цена/расположение/видимость изменились во время снимка')
        if not Path(path).is_file():return refuse('price_capture_file_missing',REVIEW+': файл снимка отсутствует')
        import hashlib
        evidence.update(version=2,screenshot=Path(path).name,sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),capture_rect=capture_region,captured_at=time.time(),provenance='stable_visible_main_card',
            capture_mode='viewport',capture_viewport_rect=clip,required_fields=required,visibility_before=visibility,visibility_after=after_visibility,font_readiness={**_safe_font_readiness(font_readiness),'diagnostics':_safe_font_diagnostics(font_readiness.get('diagnostics',{})), 'required_runs_before':required_before,'required_runs_after':required_after})
        if not visibility_valid(evidence):return refuse('price_visibility_unverifiable',REVIEW+': доказательство видимости не подтверждено')
        stage='price_attestation_failed'
        if time.monotonic()>=deadline:
            return refuse('price_capture_deadline',REVIEW+': время проверки снимка истекло')
        write_attestation(path,evidence)
    except ResearchControlError:
        raise
    except Exception as error:
        return failed(stage,error,REVIEW+': снимок цены не подтверждён')
    return evidence,''



def _capture_pixel_impl(page,path,on_refusal,on_error,original,changed,fields,qualification):
    """Two stable current DOM observations + fresh independent exact-image reading.

    This mode does not infer universal CSS harmlessness. The image reader must
    independently see every selected fact, within current owned field geometry.
    """
    import hashlib
    from .search.session import bounded_timeout
    from .search.product_identity import rendered_identity,canonical_card_url
    from .capture_visibility import attestation_path,write_attestation
    from . import pixel_evidence as pixel
    from .llm import ModelOutputInvalid,LLMOperationFailed
    from urllib.parse import urlsplit
    deadline=time.monotonic()+bounded_timeout(8)
    def refuse(stage,reason):
        if on_refusal:on_refusal(stage)
        return {},REVIEW+': '+reason
    def observe():
        snapshot=_snapshot(page)
        if time.monotonic()>=deadline:return {},{},[],{},'pixel_capture_deadline'
        evidence,reason=qualify_snapshot(snapshot,**fields)
        if not evidence:return snapshot,{},[],{},reason
        identity=rendered_identity(fields['url'],snapshot.get('url'),snapshot.get('title'),evidence['article'],fields.get('expected_mark',''),fields.get('expected_name',''),urlsplit(fields['url']).hostname or '')
        if not identity:return snapshot,{},[],{},'pixel_current_identity_unconfirmed'
        required,reason=_capture_required_fields(snapshot,evidence)
        region,reason=_capture_frame(snapshot,evidence) if required else ({},reason)
        return snapshot,evidence,required,region,reason
    try:
        font=page.evaluate(FONT_READY_SCRIPT,max(1,int(bounded_timeout(max(0,deadline-time.monotonic()))*1000)))
        if not isinstance(font,dict) or font.get('loaded') is not True or time.monotonic()>=deadline:return refuse('price_font_readiness_timeout','шрифты не готовы в пределах времени снимка')
        previous=None;prepared=False
        while time.monotonic()<deadline:
            snapshot,evidence,required,region,reason=observe()
            if not evidence or not required or not region:return refuse('pixel_dom_unqualified',reason or 'поля карточки не подтверждены')
            viewport=snapshot.get('viewport',{});scroll=snapshot.get('scroll',{})
            if changed[1] is None:changed[1]=dict(scroll)
            # Use the selected current frame; full-screen interfaces are read as
            # pixels, not reinterpreted as placement clearance or hidden layers.
            if not prepared:
                needed=math.ceil(region['height']+40)
                if needed>viewport.get('height',0):
                    if needed>1600:return refuse('pixel_frame_offscreen','обязательные поля не помещаются в снимок')
                    page.set_viewport_size({'width':int(viewport['width']),'height':needed});changed[0]=True
                page.evaluate('r => window.scrollTo(0,Math.max(0,r.y-20))',region);prepared=True;previous=None;continue
            x=max(region['x'],scroll.get('x',0));y=max(region['y'],scroll.get('y',0));right=min(region['x']+region['width'],scroll.get('x',0)+viewport.get('width',0));bottom=min(region['y']+region['height'],scroll.get('y',0)+viewport.get('height',0))
            clip_x=math.floor(x-scroll['x']);clip_y=math.floor(y-scroll['y'])
            clip_right=min(int(viewport['width']),math.ceil(right-scroll['x']));clip_bottom=min(int(viewport['height']),math.ceil(bottom-scroll['y']))
            clip={'x':clip_x,'y':clip_y,'width':clip_right-clip_x,'height':clip_bottom-clip_y}
            region={**clip,'x':clip_x+scroll['x'],'y':clip_y+scroll['y']}
            for r in required:pixel._box([r['x']-x,r['y']-y,r['width'],r['height']],region['width'],region['height'])
            state=( _capture_semantics(evidence),required,region,viewport,scroll)
            if state==previous:break
            previous=state
            page.wait_for_timeout(max(1,int(bounded_timeout(min(.3,max(0,deadline-time.monotonic())))*1000)))
        else:return refuse('pixel_capture_deadline','время подготовки снимка истекло')
        if time.monotonic()>=deadline:return refuse('pixel_capture_deadline','время подготовки снимка истекло')
        attestation_path(path).unlink(missing_ok=True)
        page.screenshot(path=str(path),clip=clip,timeout=max(1,int(bounded_timeout(max(0,deadline-time.monotonic()))*1000)),animations='disabled')
        captured_at=time.time();info=pixel.png_info(path)
        after,confirmed,after_required,after_region,reason=observe()
        if not confirmed or _capture_semantics(confirmed)!=state[0] or after_required!=required or after.get('viewport')!=viewport or after.get('scroll')!=scroll:return refuse('pixel_post_capture_changed','карточка изменилась после снимка')
        dom={'url':evidence['url'],'title':evidence['title'],'article':evidence['article'],'semantic_sha256':hashlib.sha256(pixel.canonical(_capture_semantics(evidence))).hexdigest(),'required_fields':required,'viewport':viewport,'scroll':scroll}
        result=pixel.read_png(path,qualification,bounded_timeout(60))
        # Reading consumes its own remaining parent budget, not screenshot8s.
        bounded_timeout(1)
        fresh=_snapshot(page);bounded_timeout(1);current,reason=qualify_snapshot(fresh,**fields)
        fresh_required,_=_capture_required_fields(fresh,current) if current else ([],reason)
        if not current or _capture_semantics(current)!=state[0] or fresh_required!=required or fresh.get('viewport')!=viewport or fresh.get('scroll')!=scroll:return refuse('pixel_current_card_changed','карточка изменилась во время чтения снимка')
        current_identity=rendered_identity(fields['url'],fresh.get('url'),fresh.get('title'),current['article'],fields.get('expected_mark',''),fields.get('expected_name',''),urlsplit(fields['url']).hostname or '')
        if not current_identity or pixel.png_info(path)!=info:return refuse('pixel_current_card_changed','текущая карточка или файл снимка изменились')
        evidence.update(version=3,evidence_mode='pixel_verified',screenshot=Path(path).name,sha256=info['sha256'],captured_at=captured_at,provenance='stable_visible_main_card',capture_mode='viewport',capture_rect=region,capture_viewport_rect=clip,required_fields=required,pixel_evidence={'version':1,'png':info,'qualification':qualification,'reader':result['reader'],'reading':result['reading'],'dom_before':dom,'dom_after':dict(dom)})
        reason=pixel.reading_match(evidence,result['reading'])
        if reason:return refuse(reason,'независимое чтение снимка не совпало с текущей карточкой')
        if not pixel.proof_valid(evidence,path):return refuse('pixel_attestation_invalid','доказательство снимка не подтверждено')
        write_attestation(path,evidence)
        return evidence,''
    except (ModelOutputInvalid,LLMOperationFailed):return refuse('pixel_reader_review','независимое чтение снимка требует проверки')
    except ResearchControlError:raise
    except (OSError,ValueError) as error:return refuse('pixel_capture_invalid','данные текущего снимка не подтверждены')

def capture_review_reason(offer):
    """Shared consumer gate; manual/letter prices have no extracted listed price."""
    field = lambda key, default=None: offer.get(key,default) if isinstance(offer,dict) else getattr(offer,key,default)
    if field("from_letter") or field("listed_price_with_vat") is None:
        return field("price_capture_review_reason","")
    e = field("price_capture_evidence",{}) or {}
    if not isinstance(e,dict):
        return REVIEW + ": повреждены данные снимка"
    scope_reason=scope_review({'url':e.get('url'),'title':e.get('title')},{'purchase_scope':e.get('purchase_scope')},article=e.get('article',''))
    if scope_reason and not field("price_capture_review_reason"):
        return REVIEW + ': '+scope_reason+'; требуется обновление'
    from .search.product_identity import canonical_card_url
    if (field("price_capture_review_reason") or e.get("provenance") != "stable_visible_main_card"
            or canonical_card_url(e.get("url")) != canonical_card_url(field("url"))
            or e.get("screenshot") != Path(field("screenshot_product","") or "").name
            or not isinstance(e.get("sha256"),str) or not re.fullmatch(r"[a-f0-9]{64}",e["sha256"])
            or not _number(e.get("captured_at")) or _number(e["captured_at"]) > time.time()+300):
        return field("price_capture_review_reason") or REVIEW
    for source,target in (("listed_price","listed_price_with_vat"),("normalized_price","price_with_vat")):
        a,b=_number(e.get(source)),_number(field(target))
        if not a or not b or not math.isclose(a,b,rel_tol=1e-8,abs_tol=.005):
            return REVIEW + ": сохранённая цена изменена"
    if e.get("sale_unit") != _unit(field("price_unit")) or e.get("requested_unit") != _unit(field("normalized_price_unit")):
        return REVIEW + ": сохранённая единица изменена"
    quantity = _number(e.get("quantity"))
    if (not quantity or not e.get("basis") or not e.get("unit_text") or not e.get("price_text")
            or not math.isclose(_number(e["listed_price"])/quantity,_number(e["normalized_price"]),rel_tol=1e-8,abs_tol=.005)):
        return REVIEW + ": основание пересчёта утрачено"
    from .config import CACHE_DIR
    path = CACHE_DIR / e["screenshot"]
    if path.suffix.lower() != ".png" or path.name != e["screenshot"]:
        return REVIEW + ": файл снимка не подтверждён"
    try:
        import hashlib
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != e["sha256"]:
            return REVIEW + ": файл снимка отсутствует или изменён"
        from .capture_visibility import verify_attestation
        if not verify_attestation(path,e):
            return REVIEW + ': видимость обязательных полей снимка не подтверждена; требуется обновление'

    except OSError:
        return REVIEW + ": файл снимка недоступен"
    return ""


def require_price_captures(results):
    pending = sorted({r.position.number for r in results for o in r.offers
                      if capture_review_reason(o)})
    if pending:
        raise ValueError(REVIEW + ": позиции " + ", ".join(map(str,pending)))


def unit_capture_review_reason(offer, position=None):
    """Compatibility name: business denomination checks, never image validation."""
    from .source_quote import quote_review_reason
    return quote_review_reason(offer,position)

def _historical_image_unit_capture_review_reason(offer, position=None):
    """Shared current-capture authority; invalid raw proof never falls back to titles."""
    field = lambda key, default=None: offer.get(key, default) if isinstance(offer, dict) else getattr(offer, key, default)
    pos = lambda key, default='': position.get(key, default) if isinstance(position, dict) else getattr(position, key, default)
    evidence = field('price_capture_evidence', {}) or {}
    requested = pos('unit') if position is not None else field('normalized_price_unit', '')
    if not evidence and (field('listed_price_with_vat') is None or field('from_letter')):
        if field('unit_review_required'):
            return REVIEW + ': единица требует проверки'
        if position is None and not requested:
            requested = field('price_unit','')
        from .search.agent import _saved_price_unit_is_verified
        try:
            verified = _saved_price_unit_is_verified(requested, field('price_unit',''),
                field('listed_price_with_vat'), field('price_with_vat'), field('product_title',''),
                field('url',''), field('price_unit_evidence',''))
        except (TypeError, ValueError):
            verified = False
        return '' if verified else REVIEW + ': единица не подтверждена'
    # A provided damaged proof or a raw extracted price is never a manual quote.
    if field('listed_price_with_vat') is None or field('from_letter'):
        return REVIEW + ': доказательство цены не связано с извлечённой ценой'
    reason = capture_review_reason(offer)
    if reason or not evidence:
        return reason or REVIEW + ': доказательство цены отсутствует'
    if field('unit_review_required') or not requested or _unit(requested) != evidence.get('requested_unit'):
        return REVIEW + ': единица спецификации не совпадает с ценой'
    from .search.product_identity import valid_identity, canonical_card_url
    identity = field('identity_evidence', {}) or {}
    if not valid_identity(identity,
            expected_mark=pos('type_mark') if position is not None else None,
            expected_name=pos('name') if position is not None else None):
        return REVIEW + ': идентичность товара не подтверждена'
    if (canonical_card_url(field('url')) != identity.get('canonical_url')
            or evidence.get('title') != identity.get('main_title')
            or evidence.get('article') != identity.get('main_article')):
        return REVIEW + ': снимок цены относится к другой карточке'
    from .config import CACHE_DIR
    import hashlib
    name, digest = identity.get('identity_screenshot',''), identity.get('identity_sha256','')
    try:
        if (not name or Path(name).name != name or Path(name).suffix.lower() != '.png'
                or not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{64}',digest)
                or hashlib.sha256((CACHE_DIR/name).read_bytes()).hexdigest() != digest):
            return REVIEW + ': снимок идентичности не подтверждён'
    except OSError:
        return REVIEW + ': снимок идентичности отсутствует'
    if identity.get('observed_at', 0) < evidence.get('captured_at', 0):
        return REVIEW + ': идентичность не проверена после снимка цены'
    sale, wanted, quantity = evidence['sale_unit'], evidence['requested_unit'], _number(evidence.get('quantity'))
    token = _sale_unit_token(sale)
    lot = _sale_lot(sale)
    if lot:
        token = re.escape(lot[2])+r'\s*'+re.escape(lot[1])+r'\b'
    if (not token or not re.search(r'(?:/|\bза(?=\s|\d|$)\s*(?:1\s*)?|единица[^\n:]*:\s*)\s*'+token,
                                   evidence.get('unit_text',''),re.I)
            or not any(n and math.isclose(n,evidence['listed_price'],rel_tol=1e-8,abs_tol=.005)
                       for n in visible_prices(evidence.get('price_text','')))):
        return REVIEW + ': цена/единица не связаны с видимым текстом снимка'
    if lot:
        denominators = {_number(n.replace(',', '.')) for n in re.findall(
            r'(?:/|\bза(?=\s|\d|$)\s*)\s*(\d+(?:[.,]\d+)?)\s*'+re.escape(lot[1])+r'\b', evidence.get('unit_text',''), re.I)}
        if denominators != {lot[0]}:
            return REVIEW + ': количество единиц в цене противоречиво'
    title, basis = evidence.get('title',''), evidence.get('basis','')
    expected = None
    if sale == wanted:
        expected = 1.0
    elif sale == 'уп' and wanted in {'м','шт'}:
        token = r'(?:м|метр[аов]*)' if wanted == 'м' else r'шт\.?'
        sizes = {_number(n.replace(',','.')) for n in re.findall(
            r'(?:упаковк[аеаи]?|в\s+1\s+упаковке)\s*[:(]?\s*(\d+(?:[.,]\d+)?)\s*'+token+r'\b',basis,re.I)}
        if len(sizes)==1 and None not in sizes:
            expected = sizes.pop()
    elif sale == 'шт' and wanted == 'компл':
        from .search.agent import _piece_is_complete_set, _explicit_piece_pack_size
        if _piece_is_complete_set(title) and _explicit_piece_pack_size(title+' '+basis) in {None,1}:
            expected = 1.0
    elif {sale,wanted} == {'шт','м'}:
        from .search.agent import _product_length_metres
        length = _product_length_metres(title)
        if length:
            expected = length if sale=='шт' else 1/length
    else:
        lot = _sale_lot(sale)
        if lot and wanted==lot[1]:
            expected = lot[0]
    if expected is None or not quantity or not math.isclose(quantity,expected,rel_tol=1e-8,abs_tol=.005):
        return REVIEW + ': состав/количество для пересчёта не подтверждены'
    return ''

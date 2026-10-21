"""Rendered purchase occurrence authority; page identity alone never prices a sibling."""
import re
from .search.product_identity import canonical_card_url

DOM_ARTICLE_FACTS = r'''
 const nodeKey=e=>{let a=[];for(let p=e;p && p!==document.body;p=p.parentElement){
   let n=1;for(let s=p.previousElementSibling;s;s=s.previousElementSibling) if(s.tagName===p.tagName)n++;
   a.push(p.tagName.toLowerCase()+':'+n);}return a.reverse().join('/');};
 const ownText=e=>(e.innerText||'').trim();
 const relatedScope=e=>{for(let a=e;a && a!==root;a=a.parentElement){
   const labels=[a.className,a.getAttribute('aria-label')||'',a.getAttribute('data-section')||''].join(' ');
   if(/recommend|related|cross.sell|analogs?|recommendation|рекоменду|похожие|аналоги|замены/i.test(labels)) return true;
   const label=a.getAttribute('aria-labelledby');if(label){const q=document.getElementById(label);
     if(q && /рекоменду|похожие|аналоги|замены|related|recommend/i.test(ownText(q)))return true;}
 }return false;};
 const articleLabel=/^(?:Артикул(?:\s+(?:производителя|товара))?|арт\.|Код\s+производителя)\s*[:#]?\s*$/iu;
 const labelledArticle=/^(?:Артикул(?:\s+(?:производителя|товара))?|арт\.|Код\s+производителя)\s*[:#]?\s*([\wА-Яа-я./-]{3,100})\s*$/iu;
 const inlineArticle=t=>articleLabel.test(t)?null:t.match(labelledArticle);
 const articleTokens=e=>ownText(e).split(/[\n\r]+/u).map(t=>inlineArticle(t.trim())).filter(Boolean).map(m=>m[1]);
 const articleText=e=>{const sources=[];const walker=document.createTreeWalker(e,NodeFilter.SHOW_TEXT);let n,text='';
   while(n=walker.nextNode()){const q=n.parentElement,value=n.textContent||'';
     if(!value.trim() || !q || !visible(q) || excluded(q) || relatedScope(q) || /^(SCRIPT|STYLE|INPUT|TEXTAREA|SELECT)$/.test(q.tagName))continue;
     text+=value;if(text.length>200 || sources.length>=32)return null;
     sources.push({node:nodeKey(q),text:value});
   }return {text:text.trim(),sources};};
 const articleFacts=e=>{const out=[];for(const q of [e,...e.querySelectorAll('*')]){
   if(!visible(q) || excluded(q) || relatedScope(q) || /^(SCRIPT|STYLE|INPUT|TEXTAREA|SELECT)$/.test(q.tagName))continue;
   const raw=ownText(q);if(raw.length>200)continue;
   const observed=articleText(q);if(!observed)continue;
   const t=observed.text;let m=inlineArticle(t),n=q,sources=observed.sources,field=t;
   if(!m && articleLabel.test(t)){
     const next=q.nextElementSibling,value=next && visible(next) && !excluded(next) && !relatedScope(next)?articleText(next):null;
     if(value && /^[\wА-Яа-я./-]{3,100}$/u.test(value.text) &&
        ((q.tagName==='DT' && next.tagName==='DD') || (q.tagName==='TD' && next.tagName==='TD') || q.parentElement?.innerText.length<=200)){
       m=[t,value.text];n=q.parentElement;sources=[...sources,...value.sources];field=t+' '+value.text;
     }
   }
   if(m){const prior=inlineArticle(ownText(n)),safe=articleText(n);
     if(prior && prior[1]===m[1] && safe && inlineArticle(safe.text)?.[1]===m[1])field=ownText(n);
     const r=n.getBoundingClientRect();out.push({kind:'labelled_article',value:m[1],text:field,sources,node:nodeKey(n),owner_id:nodeKey(e),rect:{x:r.x+scrollX,y:r.y+scrollY,width:r.width,height:r.height}});}
 }return [...new Map(out.map(f=>[f.node+'|'+f.value,f])).values()];};
'''

DOM_SCOPE = DOM_ARTICLE_FACTS + r'''
 const canonical=u=>{try{const v=new URL(u,document_url);if(v.username || v.password)return '';return v.protocol.toLowerCase()+'//'+v.host.toLowerCase()+v.pathname+v.search;}catch{return '';}};
 const buyLink=e=>{if(e.tagName!=='A' || !e.hasAttribute('href'))return false;
   const href=e.getAttribute('href')||'';if(/^#/.test(href) || /^javascript:\s*void\s*\(0\)\s*;?$/i.test(href))return true;
   try{const u=new URL(href,document_url),d=new URL(document_url);return u.origin===d.origin && /(?:^|\/)(?:cart|basket|checkout|add.to.cart)(?:\/|$)/i.test(u.pathname);}catch{return false;}};
 const action=e=>visible(e) && (/^(BUTTON|INPUT)$/.test(e.tagName)||e.getAttribute('role')==='button'||buyLink(e)) &&
   (/в\s*корзину|купить|добавить|заказать|buy|add\s*to\s*cart|purchase/i.test(ownText(e)||e.value||'') ||
    (e.tagName==='INPUT' && e.type==='submit'));
 const controls=e=>[...e.querySelectorAll('button,input,a[href],[role="button"]')].filter(action);
 const metadata=e=> e.matches('[itemscope][itemtype*="Product"],[data-product-id],[data-sku]') ||
   !!(h.id && (e.getAttribute('aria-labelledby')||'').split(/\s+/).includes(h.id));
 const tokenIn=(text,value)=>text.split(/[^\p{L}\p{N}._/-]+/u).some(t=>t.toLocaleLowerCase()===value.toLocaleLowerCase());
 const namePrefix=text=>{const tokens=text.split(/[^\p{L}\p{N}._/-]+/u).filter(Boolean);const index=tokens.findIndex(t=>/\p{L}/u.test(t) && /\d/.test(t));
   if(index<=0 || /\d/.test(tokens[0]))return [];return tokens.slice(0,index).map(t=>t.toLocaleLowerCase()).filter(t=>/\p{L}/u.test(t));};
 const rowNameCompatible=text=>{const head=namePrefix(ownText(h)),row=namePrefix(text);
   return head.length && row.length && head[0]===row[0] && row.every(t=>head.includes(t));};
 const referenceRole=label=>/^(?:Производитель|Бренд|Manufacturer|Brand)$/iu.test(label)?'manufacturer':
   /^(?:Сертификат(?:ы)?|Документ(?:ы)?|Инструкция|Certificate(?:s)?|Document(?:s)?|Manual)$/iu.test(label)?'document':'';
 const propertyReference=(link,property)=>{
   if(!visible(link)||excluded(link)||relatedScope(link)||!ownText(link)||link.querySelector('h1,h2,h3,h4,[itemprop="name"],[itemprop="sku"]')||
      link.closest('[itemscope][itemtype*="Product"]')?.closest('table,dl')===property)return null;
   if(link.matches('[itemprop="url"],[itemprop="name"],[itemprop="sku"],[data-sku],[data-product-id]')||link.querySelector('[data-sku],[data-product-id]'))return null;
   let label,value;
   if(property.tagName==='TABLE'){
     const row=link.closest('tr');if(!row||row.closest('table')!==property)return null;
     const cells=[...row.children];if(cells.length!==2||cells.some(c=>c.colSpan!==1||c.rowSpan!==1)||!cells[1].contains(link))return null;
     [label,value]=cells;
   }else{value=link.closest('dd');label=value?.previousElementSibling;if(!value||value.parentElement!==property||label?.tagName!=='DT')return null;}
   for(let a=link;a&&a!==value.parentElement;a=a.parentElement)if(a.matches('[data-sku],[data-product-id],[itemscope][itemtype*="Product"]'))return null;
   if(!visible(label)||!visible(value)||label.querySelector('a[href]')||articleFacts(value).length)return null;
   const role=referenceRole(ownText(label));if(!role)return null;
   if(role==='manufacturer'&&namePrefix(ownText(link)).length)return null;
   return {role,label:ownText(label),text:ownText(link),label_node:nodeKey(label),value_node:nodeKey(value),node:nodeKey(link),href:canonical(link.href)};
 };
 const ancillaryHeading=(q,article)=>{
   const title=ownText(q),main=ownText(h);
   const prefix=title.endsWith(main)?title.slice(0,-main.length).trim():'';
   if(!/^(?:Полные\s+)?(?:Технические\s+)?характеристики$|^(?:Technical\s+)?specifications$/iu.test(prefix) || q.closest('a[href]') || !article)return false;
   for(let a=q.parentElement;a && !a.contains(h) && ownText(a).length<=3500;a=a.parentElement){
     if(amounts(ownText(a)).length)break;
     const property=a.querySelector('table,dl');if(!property)continue;
     const fs=articleFacts(property);
     const links=[...property.querySelectorAll('a[href]')],references=links.map(link=>propertyReference(link,property));
     if(fs.length && fs.every(f=>f.value.toLocaleLowerCase()===article.toLocaleLowerCase()) &&
        property.querySelectorAll('tr,dt').length>=2 && references.every(Boolean))return {property_node:nodeKey(property),reference_links:references};
   }return false;
 };
 const quoteRole=e=>{
   if(e.closest('s,del,strike'))return 'historical';
   for(let a=e;a && a!==root;a=a.parentElement)if(getComputedStyle(a).textDecorationLine.includes('line-through'))return 'historical';
   for(let a=e;a && a!==root && ownText(a).length<=240;a=a.parentElement)
     if(/^(?:Экономия|Вы\s+экономите|Saving|Discount\s+amount)\s*[:：]/iu.test(ownText(a)))return 'saving';
   return 'current';
 };
 const panelCache=new WeakMap();
 const panel=e=>{if(panelCache.has(e))return panelCache.get(e);let found=null;
   for(let a=e;a && a!==document.body && a!==document.documentElement;a=a.parentElement){
     if(ownText(a).length>3500)break;
     if(currency.test(ownText(a)) && (controls(a).length || articleFacts(a).length)){found=a;break;}
     if(a===root)break;
   }panelCache.set(e,found);return found;};
 const panelTrace=e=>{const rows=[];for(let a=e;a && a!==document.body && a!==document.documentElement;a=a.parentElement){
   const t=ownText(a);rows.push({node:nodeKey(a),text_length:t.length,purchase_controls:controls(a).length,currency:currency.test(t)});if(t.length>3500 || a===root)break;}return rows;};

 const diagnosticHref=e=>{const raw=e.getAttribute('href');if(!raw)return '';if(raw.startsWith('#'))return raw.slice(0,120);
   try{const u=new URL(raw,document_url);if(u.username||u.password)return '[credentials omitted]';return ['http:','https:'].includes(u.protocol)?u.origin+u.pathname:'[non-http action]';}catch{return '[invalid]';}};
 const diagnosticNode=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);
   const value={node:nodeKey(e),tag:e.tagName,role:e.getAttribute('role')||'',type:e.tagName==='INPUT'?e.type:'',
     href:diagnosticHref(e),text:ownText(e).slice(0,160),visible:visible(e),recognized_action:action(e),inline_click_handler:e.hasAttribute('onclick'),
     rect:{x:r.x+scrollX,y:r.y+scrollY,width:r.width,height:r.height},
     styles:{display:s.display,visibility:s.visibility,position:s.position,overflowX:s.overflowX,overflowY:s.overflowY,
       pointerEvents:s.pointerEvents,zIndex:s.zIndex,transform:s.transform,fontSize:s.fontSize,lineHeight:s.lineHeight,opacity:s.opacity},
     attributes:{}};
   for(const name of ['class','id','aria-label','aria-selected','aria-checked','data-selected','data-sku','data-product-id','itemprop'])
     if(e.hasAttribute(name))value.attributes[name]=e.getAttribute(name).slice(0,160);
   if(e.tagName==='INPUT')value.text=''; // Never retain form values or their adjacent private text.
   return value;};
 const purchaseDiagnosticCache=new WeakMap();
 const purchaseDiagnostics=e=>{if(purchaseDiagnosticCache.has(e))return purchaseDiagnosticCache.get(e);
   const ancestry=[];let bounded=e;
   for(let a=e;a && a!==document.body && a!==document.documentElement;a=a.parentElement){
     const t=ownText(a);const candidates=[...a.querySelectorAll('*')].filter(x=>visible(x) && !/^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|IFRAME|OBJECT)$/.test(x.tagName) && (x.matches('button,a[href],input,[role],[tabindex],[onclick]') || (ownText(x).length<=160 && /в\s*корзину|купить|добавить|заказать|buy|add\s*to\s*cart|purchase/i.test(ownText(x))))).slice(0,16).map(diagnosticNode);
     ancestry.push({node:nodeKey(a),text_length:t.length,candidates});if(t.length>3500 || a===root)break;bounded=a;
     if(ancestry.length>=10)break;
   }
   let remaining=64;const subtree=(a,depth)=>{if(!remaining-- || depth>5 || !visible(a) || /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|IFRAME|OBJECT)$/.test(a.tagName))return null;
     const v=diagnosticNode(a);v.direct_text=a.tagName==='INPUT'?'':[...a.childNodes].filter(n=>n.nodeType===3).map(n=>n.textContent).join('').trim().slice(0,160);
     delete v.text;v.children=[...a.children].slice(0,16).map(x=>subtree(x,depth+1)).filter(Boolean);return v;};
   const value={schema:1,diagnostic_only:true,ancestry,bounded_subtree:subtree(bounded,0)};
   purchaseDiagnosticCache.set(e,value);return value;};
 const mainPanels=[...new Set(controls(root).map(panel).filter(Boolean))];
 const scopeCache=new WeakMap();
 const amounts=t=>t.match(/(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,]\d{1,2})?\s*(?:₽|руб(?:\.|лей|ля|ль)?|RUB)(?![\p{L}])/giu)||[];
 const unitLabel=/(?:(?:\/|за\s+)\s*(?:\d+(?:[.,]\d+)?\s*)?|единица\s+(?:продажи|цены)\s*:\s*)\s*(?:шт\.?|м|уп(?:ак(?:овк[ауеи]*)?)?|упаковк[ауеи]*|компл(?:ект)?|кг|л)(?=\s|[.,)]|$)/iu;
 const bareUnit=/^(?:шт\.?|штук[аи]?|м\.?|метр(?:ы|ов)?|кг\.?|килограмм(?:ы|ов)?|л\.?|литр(?:ы|ов)?|уп(?:ак(?:овка)?)?\.?|компл(?:ект)?\.?)$/iu;
 const basisRecord=(e,b)=>{let selected=null;const value=ownText(e);
   for(let a=e;a && b.contains(a);a=a.parentElement){const t=ownText(a);
     if(t.length>3500)break;
     const matching=t.split(/\n/).filter(line=>line.includes(value) && amounts(line).length===1 && unitLabel.test(line));
     if(matching.length===1 && amounts(t).length>1)
       return {node:a,text:matching[0],kind:'price_line',unit_text:'',unit_nodes:[]};
     if(amounts(t).length>1)break;
     if(amounts(t).length===1 && unitLabel.test(t))selected={node:a,text:t,kind:'inline',unit_text:'',unit_nodes:[]};
     if(a===b)break;
   }
   // A table unit column belongs to its own price cell and identity-proven row.
   // Stock, order quantities and arbitrary neighbouring cells are not unit columns.
   const row=e.closest('tr'),table=row?.closest('table');
   if(row===b && table && amounts(ownText(row)).length===1){
     const headers=[...table.querySelectorAll('thead tr:last-child th')];
     const cells=[...row.children];const priceIndexes=cells.flatMap((c,i)=>amounts(ownText(c)).length===1?[i]:[]);
     const priceIndex=priceIndexes.length===1?priceIndexes[0]:-1;
     const unitIndexes=headers.flatMap((h,i)=>/^(?:единица(?:\s+измерения)?|ед\.?\s*(?:изм(?:ерения)?\.?)?|единица\s+продажи|unit)$/iu.test(ownText(h))?[i]:[]);
     if(headers.length===cells.length && [...headers,...cells].every(c=>c.colSpan===1 && c.rowSpan===1) && priceIndex>=0 && /^(?:цена(?:\s+с\s+ндс)?|price)$/iu.test(ownText(headers[priceIndex])) && unitIndexes.length===1){
       const u=cells[unitIndexes[0]];if(visible(u) && bareUnit.test(ownText(u)))
         return {node:row,text:ownText(row),kind:'table_unit_cell',unit_text:ownText(u),unit_nodes:[nodeKey(u)]};
     }
   }
   // A visible definition-list label/value can confirm a single quoted price,
   // only within its own bounded purchase panel and without an explicit contrary unit.
   if(amounts(ownText(b)).length===1){
     const pairs=[...b.querySelectorAll('dt')].filter(visible).filter(d=>/^(?:единица\s+(?:продажи|цены)|цена\s+за|price\s+unit)\s*:?$/iu.test(ownText(d)))
       .map(d=>d.nextElementSibling).filter(d=>d?.tagName==='DD' && visible(d) && bareUnit.test(ownText(d)));
     if(pairs.length===1 && !/(?:(?:\/|за\s+)\s*(?:\d+(?:[.,]\d+)?\s*)?)\s*(?:шт\.?|м|уп\w*|компл\w*|кг|л)(?=\s|[.,)]|$)/iu.test(ownText(b)))
         return {node:b,text:ownText(b),kind:'labelled_unit_value',unit_text:ownText(pairs[0]),unit_nodes:[nodeKey(pairs[0])]};
   }
   return selected;
 };
 const tierScope=(e,b,value)=>{const selected=e.closest('[aria-selected="true"],[aria-checked="true"],[data-selected="true"]');
   const tagged=e.closest('[itemprop="price"]');
   const taggedPrices=[...b.querySelectorAll('[itemprop="price"]')].filter(visible);
   const basis=basisRecord(e,b);
   const chosen=selected && b.contains(selected)?selected:tagged && b.contains(tagged) && taggedPrices.length===1?tagged:null;
   return {...value,quote_role:quoteRole(e),basis_owner_id:basis?nodeKey(basis.node):'',basis_text:basis?basis.text:'',
     basis_version:2,basis_kind:basis?.kind||'',basis_unit_text:basis?.unit_text||'',basis_unit_nodes:basis?.unit_nodes||[],
     basis_price_node:nodeKey(e),price_node:nodeKey(e),tier_selected:!!chosen,tier_key:chosen?nodeKey(chosen):'',tier_text:chosen?ownText(chosen):''};};
 const purchaseScope=e=>{
   const row=e.closest('tr');const b=row && row!==root && currency.test(ownText(row)) ? row : panel(e);
   if(!b)return null;
   if(scopeCache.has(b))return tierScope(e,b,scopeCache.get(b));
   let owner=null;
   for(let a=b;a && a!==document.body && a!==document.documentElement;a=a.parentElement){
     if(a.contains(h) && metadata(a)){owner=a;break;}
     if(a===root)break;
   }
   const facts=[];
   for(let a=b;a && a!==document.body && a!==document.documentElement;a=a.parentElement){
     if(a.contains(h) || a===root){if(a===b)facts.push(a);break;}
     if(ownText(a).length>3500)return null;
     facts.push(a);if(row===b)break;
   }
   let texts=[...new Set(facts.flatMap(a=>[...a.querySelectorAll('h1,h2,h3,h4,[itemprop="name"]')].filter(visible).filter(x=>x.matches('h1,[itemprop="name"]') || x.closest('a[href]') || /(?:[A-Za-zА-Яа-я]{2,}[\w.-]*\d[\w.-]*|\b\d{5,}\b)/u.test(ownText(x))).map(ownText)))];
   const identityFacts=facts.flatMap(articleFacts);
   const articles=[...facts.flatMap(articleTokens),...identityFacts.map(f=>f.value)];
   const identityScope=facts.length?facts[facts.length-1]:b;
   for(let a=b;a && !a.contains(h) && a!==root;a=a.parentElement)if(visible(a) && a.matches('[data-sku]') && a.getAttribute('data-sku'))articles.push(a.getAttribute('data-sku'));
   // A table's visible article header belongs to its corresponding row cell.
   if(row===b){const table=row.closest('table');const heads=table?[...table.querySelectorAll('thead th')]:[];
     for(let i=0;i<heads.length;i++)if(/^(?:артикул(?:\s+производителя)?|код\s+производителя)$/i.test(ownText(heads[i]))) {
       const cell=row.children[i];if(cell && visible(cell) && ownText(cell))articles.push(ownText(cell));
     }}
   const observedValues=[...new Set(identityFacts.map(f=>f.value.toLocaleLowerCase()))];
   const localArticle=observedValues.length===1?identityFacts[0].value:'';
   const ancillary=[];
   for(const a of facts)for(const q of a.querySelectorAll('h2,h3,h4'))if(visible(q)){const properties=ancillaryHeading(q,localArticle);if(properties)ancillary.push({node:nodeKey(q),text:ownText(q),role:'product_properties',article:localArticle,...properties});}
   texts=texts.filter(t=>!ancillary.some(f=>f.text===t));
   if(row===b && !articles.length && !relatedScope(row)){
     const primary=articleFacts(root),values=[...new Set(primary.map(f=>f.value.toLocaleLowerCase()))];
     const cells=[...row.children].filter(visible).filter(c=>!amounts(ownText(c)).length && !bareUnit.test(ownText(c)));
     const codes=ownText(h).split(/[^\p{L}\p{N}._/-]+/u).filter(t=>/\p{L}/u.test(t) && /\d/.test(t));
     if(values.length===1 && codes.length && cells.length){const value=primary[0].value;
       const names=cells.filter(c=>ownText(c).length<=600 && rowNameCompatible(ownText(c)) && tokenIn(ownText(c),value) && codes.every(t=>tokenIn(ownText(c),t)));
       if(names.length===1){const c=names[0];articles.push(value);identityFacts.push({kind:'printed_row_article',value,text:ownText(c),node:nodeKey(c),owner_id:nodeKey(row),primary:primary[0],name_codes:codes});}
     }
   }
   if(owner && owner.matches('[data-sku]') && owner.getAttribute('data-sku'))articles.push(owner.getAttribute('data-sku'));
   const links=[...b.querySelectorAll('a[href]')].filter(visible).filter(a=>a.matches('[itemprop="url"]') ||
      !!a.querySelector('h1,h2,h3,h4') || /\d/.test(ownText(a))).map(a=>canonical(a.href));
   let ancestor=b;while(ancestor && !ancestor.contains(h))ancestor=ancestor.parentElement;
   const simple=ancestor && ancestor!==document.body && ancestor!==document.documentElement &&
      ancestor.querySelectorAll('h1').length===1 && mainPanels.length===1 && ancestor.querySelectorAll('tr').length<=1 &&
      ![...ancestor.querySelectorAll('h2,h3,h4')].filter(visible).some(x=>/\d/.test(ownText(x)) && ownText(x)!==ownText(h));
   const sameLink=links.includes(canonical(document_url));
   const kind=row===b?'variant_row':'main_purchase';
   const ownIdentity=texts.includes(ownText(h)) || articles.length>0 || sameLink;
   const ownerPanels=owner?mainPanels.filter(p=>owner.contains(p)):[];
   const ownerBound=owner && (ownerPanels.length===1 || ownIdentity);
   const explicitPanel=articles.length>0 && !relatedScope(e);
   const association=row===b?(sameLink?'canonical_row':articles.length?'article_row':'unconfirmed_row'):(ownerBound?'product_container':explicitPanel?'article_panel':simple?'unique_simple':'unconfirmed_panel');
   const priceContext=e=>{const a=basisRecord(e,b);return a?a.text:ownText(e);};
   const prices=[...b.querySelectorAll('*')].filter(visible).filter(x=>quoteRole(x)==='current').filter(x=>currency.test(ownText(x)) && ownText(x).length<240 &&
      ![...x.children].some(c=>visible(c) && currency.test(ownText(c)) && /\d/.test(ownText(c))));
   const value={version:1,provenance:'rendered_purchase_scope',document_url,
     owner_id:nodeKey(b),kind,association,main_title:ownText(h),own_titles:texts,own_articles:articles,
     product_links:[...new Set(links)],related:relatedScope(e),purchase_controls:controls(b).map(x=>ownText(x)||x.type),
     identity_owner:row===b && (sameLink || articles.length)?nodeKey(row):owner?nodeKey(owner):explicitPanel?nodeKey(b):simple?nodeKey(ancestor):'',
     identity_heading:owner||simple||explicitPanel?ownText(h):'',
     identity_facts_version:1,identity_scope_id:nodeKey(identityScope),identity_facts:identityFacts,ancillary_titles:ancillary,quote_role:quoteRole(e),
     // Exact price strings distinguish genuinely separate visible price tiers.
     tier_prices:[...new Set(prices.map(priceContext))],scope_text:ownText(b)};
   scopeCache.set(b,value);return tierScope(e,b,value);
 };
'''

def price_applicability(text):
    """Only an explicit physical-store channel excludes a website quotation."""
    if not isinstance(text,str):return 'unknown'
    physical=bool(re.search(r'\bв\s+(?:розничных\s+)?магазинах\b',text,re.I))
    online=bool(re.search(r'\b(?:на\s+сайте|в\s+интернет[- ]магазине)\b',text,re.I))
    conditional=bool(re.search(r'(?:по\s+(?:клубной\s+|дисконтной\s+)?карте|для\s+(?:участников|членов|оптовых)|оптов(?:ая|ый|ые)|цена\s+в\s+корзине|при\s+(?:заказе|покупке)\s+(?:от\s+)?\d)',text,re.I))
    return 'conditional' if conditional else 'ambiguous' if physical and online else 'offline_store' if physical else 'website' if online else 'unspecified'


def _printed_name_prefix(text):
    tokens=[t for t in re.split(r'[^\w./-]+',text) if t]
    index=next((i for i,t in enumerate(tokens) if re.search(r'[^\W\d_]',t) and re.search(r'\d',t)),-1)
    if index<=0 or re.search(r'\d',tokens[0]):return []
    return [t.casefold() for t in tokens[:index] if re.search(r'[^\W\d_]',t)]


def printed_name_compatible(main_title,row_text):
    if not isinstance(main_title,str) or not isinstance(row_text,str):return False
    head,row=_printed_name_prefix(main_title),_printed_name_prefix(row_text)
    return bool(head and row and head[0]==row[0] and all(t in head for t in row))


def labelled_article_value(text):
    """A complete field label is not its own manufacturer-article value."""
    if not isinstance(text,str):return ''
    label=r'(?:Артикул(?:\s+(?:производителя|товара))?|арт\.|Код\s+производителя)'
    if re.fullmatch(label+r'\s*[:#]?\s*',text.strip(),re.I):return ''
    match=re.fullmatch(label+r'\s*[:#]?\s*([\wА-Яа-я./-]{3,100})\s*',text.strip(),re.I)
    return match[1] if match else ''


def property_reference_role(label):
    if not isinstance(label,str):return ''
    if re.fullmatch(r'Производитель|Бренд|Manufacturer|Brand',label,re.I):return 'manufacturer'
    if re.fullmatch(r'Сертификат(?:ы)?|Документ(?:ы)?|Инструкция|Certificate(?:s)?|Document(?:s)?|Manual',label,re.I):return 'document'
    return ''


def labelled_article_sources_valid(fact):
    sources=fact.get('sources') if isinstance(fact,dict) else None
    node=fact.get('node') if isinstance(fact,dict) else None
    if not isinstance(node,str) or not isinstance(sources,list) or not 1<=len(sources)<=32:return False
    texts=[]
    for source in sources:
        if not isinstance(source,dict):return False
        owner=source.get('node');text=source.get('text')
        if not isinstance(owner,str) or not (owner==node or owner.startswith(node+'/')):return False
        if not isinstance(text,str) or not text.strip() or len(text)>200:return False
        texts.append(text)
    return sum(map(len,texts))<=200 and labelled_article_value(''.join(texts))==fact.get('value')


def scope_observed_article(scope):
    """Current DOM facts only; expected SKU and URL never supply an article."""
    if not isinstance(scope,dict) or type(scope.get('identity_facts_version')) is not int or scope['identity_facts_version']!=1:return ''
    owner=scope.get('identity_scope_id');facts=scope.get('identity_facts')
    if not isinstance(owner,str) or not owner or not isinstance(facts,list) or not facts:return ''
    values=[]
    for fact in facts:
        if not isinstance(fact,dict) or fact.get('kind') not in {'labelled_article','printed_row_article'}:return ''
        value=fact.get('value');node=fact.get('node');text=fact.get('text');parent=fact.get('owner_id')
        if not isinstance(value,str) or not re.fullmatch(r'[\wА-Яа-я./-]{3,100}',value):return ''
        if any(not isinstance(v,str) or not v for v in (node,text,parent)):return ''
        if not (node==parent or node.startswith(parent+'/')) or not (node==owner or node.startswith(owner+'/')):return ''
        if value not in text or value not in scope.get('own_articles',[]):return ''
        if fact['kind']=='labelled_article':
            if labelled_article_value(text)!=value:return ''
            if 'sources' in fact and not labelled_article_sources_valid(fact):return ''
        if fact['kind']=='printed_row_article':
            if not printed_name_compatible(scope.get('main_title'),text):return ''
            primary=fact.get('primary');codes=fact.get('name_codes')
            if not isinstance(primary,dict) or primary.get('kind')!='labelled_article' or primary.get('value')!=value:return ''
            if labelled_article_value(primary.get('text'))!=value:return ''
            if 'sources' in primary and not labelled_article_sources_valid(primary):return ''
            if not isinstance(primary.get('owner_id'),str) or not node.startswith(primary['owner_id']+'/'):return ''
            if not isinstance(codes,list) or not codes or any(not isinstance(c,str) or not c or c.casefold() not in text.casefold() for c in codes):return ''
        values.append(value)
    return values[0] if len({v.casefold() for v in values})==1 else ''

def scope_review(snapshot, price, *, article='', ownership_only=False):
    if not isinstance(snapshot,dict) or not isinstance(price,dict) or not isinstance(article,str):return 'purchase_scope_missing'
    if not isinstance(snapshot.get('title'),str) or not snapshot['title'] or not canonical_card_url(snapshot.get('url')):return 'purchase_scope_missing'
    scope=price.get('purchase_scope')
    if not isinstance(scope,dict) or type(scope.get('version')) is not int or scope.get('version')!=1 or scope.get('provenance')!='rendered_purchase_scope':
        return 'purchase_scope_missing'
    if isinstance(snapshot.get('text'),str) and isinstance(scope.get('scope_text'),str):
        normalize=lambda value:re.sub(r'\s+',' ',value).strip()
        visible=normalize(snapshot['text'])
        if any(normalize(line) not in visible for line in scope.get('scope_text','').splitlines() if line.strip()):return 'purchase_scope_missing'
    if 'identity_facts_version' in scope:
        facts=scope.get('identity_facts')
        if not isinstance(facts,list) or any(not isinstance(f,dict) for f in facts):return 'purchase_scope_missing'
        if facts and not scope_observed_article(scope):return 'purchase_scope_conflicting_identity'
        ancillary=scope.get('ancillary_titles')
        if not isinstance(ancillary,list) or any(not isinstance(f,dict) for f in ancillary):return 'purchase_scope_missing'
        for fact in ancillary:
            title=fact.get('text');node=fact.get('node');main=snapshot['title'];owner=scope.get('identity_scope_id','')
            if not isinstance(title,str) or not title.endswith(main) or not isinstance(node,str) or not node.startswith(owner+'/') or fact.get('role')!='product_properties' or fact.get('article')!=scope_observed_article(scope):return 'purchase_scope_conflicting_identity'
            if not re.fullmatch(r'(?:Полные\s+)?(?:Технические\s+)?характеристики|(?:Technical\s+)?specifications',title[:-len(main)].strip(),re.I):return 'purchase_scope_conflicting_identity'
            if 'reference_links' in fact:
                property_node=fact.get('property_node');references=fact['reference_links']
                if not isinstance(property_node,str) or not property_node.startswith(owner+'/') or not isinstance(references,list):return 'purchase_scope_conflicting_identity'
                for reference in references:
                    if not isinstance(reference,dict) or not property_reference_role(reference.get('label')) or property_reference_role(reference['label'])!=reference.get('role'):return 'purchase_scope_conflicting_identity'
                    if not isinstance(reference.get('text'),str) or not reference['text'] or (reference['role']=='manufacturer' and _printed_name_prefix(reference['text'])):return 'purchase_scope_conflicting_identity'
                    label_node=reference.get('label_node');value_node=reference.get('value_node');link_node=reference.get('node')
                    if any(not isinstance(n,str) or not n.startswith(property_node+'/') for n in (label_node,value_node,link_node)):return 'purchase_scope_conflicting_identity'
                    if not (link_node==value_node or link_node.startswith(value_node+'/')) or label_node.rsplit('/',1)[0]!=value_node.rsplit('/',1)[0] or not canonical_card_url(reference.get('href')):return 'purchase_scope_conflicting_identity'
        if not article:article=scope_observed_article(scope)
        if scope.get('quote_role') not in {'current','historical','saving'}:return 'purchase_scope_missing'
        if scope['quote_role']!='current':return 'purchase_price_not_current'
    if scope.get('related') is not False:return 'purchase_scope_related'
    if canonical_card_url(scope.get('document_url'))!=canonical_card_url(snapshot.get('url')) or scope.get('main_title')!=snapshot.get('title'):
        return 'purchase_scope_conflicting_identity'
    if any(not isinstance(scope.get(k),str) or not scope[k] for k in ('owner_id','identity_owner','price_node')):
        return 'purchase_scope_missing'
    if scope.get('kind') not in {'variant_row','main_purchase'}:return 'purchase_scope_missing'
    if type(scope.get('tier_selected')) is not bool or not isinstance(scope.get('tier_key'),str) or not isinstance(scope.get('tier_text'),str):return 'purchase_scope_missing'
    if any(not isinstance(scope.get(k),list) or any(not isinstance(x,str) or not x for x in scope[k]) for k in ('purchase_controls','tier_prices')):return 'purchase_scope_missing'
    if not isinstance(scope.get('scope_text'),str) or not scope['scope_text'] or len(scope['scope_text'])>3500:return 'purchase_scope_missing'
    if scope.get('tier_selected') and (not scope['tier_key'] or not scope['tier_text']):return 'purchase_scope_missing'
    if not ownership_only:
        if not isinstance(scope.get('basis_owner_id'),str) or not scope['basis_owner_id']:return 'purchase_scope_missing'
        if not (scope['basis_owner_id']==scope['owner_id'] or scope['basis_owner_id'].startswith(scope['owner_id']+'/')):return 'purchase_scope_missing'
        if not isinstance(scope.get('basis_text'),str) or not scope['basis_text'] or scope['basis_text'] not in scope['scope_text']:return 'purchase_scope_missing'
        if 'basis_version' in scope:
            if type(scope['basis_version']) is not int or scope['basis_version']!=2:return 'purchase_scope_missing'
            if scope.get('basis_price_node')!=scope['price_node'] or scope.get('basis_kind') not in {'inline','price_line','table_unit_cell','labelled_unit_value'}:return 'purchase_scope_missing'
            nodes=scope.get('basis_unit_nodes');unit=scope.get('basis_unit_text')
            if not isinstance(nodes,list) or any(not isinstance(n,str) or not n.startswith(scope['basis_owner_id']+'/') for n in nodes):return 'purchase_scope_missing'
            if not isinstance(unit,str):return 'purchase_scope_missing'
            if scope['basis_kind'] in {'table_unit_cell','labelled_unit_value'} and (len(nodes)!=1 or not unit or unit not in scope['basis_text']):return 'purchase_scope_missing'
        # A per-price line must not erase a condition rendered elsewhere in its
        # same purchase scope. No customer/cart quantity context is supplied here.
        if price_applicability(scope['scope_text'])=='conditional':return 'purchase_price_condition_unconfirmed'
        applicability=price_applicability(scope['basis_text'])
        if applicability=='offline_store':return 'purchase_price_offline_store'
        if applicability in {'conditional','ambiguous'}:return 'purchase_price_condition_unconfirmed'
    if scope.get('association') not in {'product_container','unique_simple','canonical_row','article_panel','article_row'}:
        return 'purchase_scope_missing'
    titles=scope.get('own_titles');articles=scope.get('own_articles');links=scope.get('product_links')
    if any(not isinstance(v,list) or any(not isinstance(x,str) for x in v) for v in (titles,articles,links)):
        return 'purchase_scope_missing'
    if not scope['purchase_controls'] and not articles and scope['association']!='canonical_row':return 'purchase_scope_missing'
    if scope['association'] in {'article_panel','article_row'} and (not article or not articles or scope['identity_owner']!=scope['owner_id']):return 'purchase_scope_missing'
    if article and any(value.casefold()!=article.casefold() for value in articles):return 'purchase_scope_conflicting_identity'
    if any(title!=snapshot['title'] for title in titles):return 'purchase_scope_conflicting_identity'
    if links and any(canonical_card_url(link)!=canonical_card_url(snapshot['url']) for link in links):return 'purchase_scope_conflicting_identity'
    if scope['association']=='canonical_row' and not links:return 'purchase_scope_missing'
    if scope['association'] not in {'canonical_row','article_row'} and scope.get('identity_heading')!=snapshot.get('title'):
        return 'purchase_scope_missing'
    return ''


def scope_diagnostic(snapshot, price, *, article=''):
    """Separate product authority failures from missing selling-basis evidence."""
    ownership=scope_review(snapshot,price,article=article,ownership_only=True)
    if ownership:
        return {'code':ownership,'subcause':'ownership_missing' if ownership=='purchase_scope_missing' else 'identity_or_applicability_conflict'}
    reason=scope_review(snapshot,price,article=article)
    if not reason:return {'code':'','subcause':''}
    scope=price['purchase_scope']
    basis_missing=not scope.get('basis_owner_id') or not scope.get('basis_text')
    return {'code':reason,'subcause':'selling_basis_missing' if basis_missing else 'selling_basis_invalid' if reason=='purchase_scope_missing' else 'price_applicability_unconfirmed'}


def consumer_scope_valid(proof):
    if not isinstance(proof,dict):return False
    scope=proof.get('purchase_scope');unit_scopes=proof.get('basis_scope_ids')
    if not isinstance(scope,dict) or not isinstance(unit_scopes,list) or not unit_scopes:return False
    snapshot={'url':proof.get('url'),'title':proof.get('title')}
    if scope_review(snapshot,{'purchase_scope':scope},article=proof.get('article','')):return False
    return all(value==scope['basis_owner_id'] for value in unit_scopes)

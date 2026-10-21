"""Paid-free full logical-pair inventory and stable confirmed-first ordering.

Plans describe pending work, not completion, current prices or a service cap.
"""
from .product_identity import canonical_card_url
from .source_registry import host_for

def value(position,key,default=None):return position.get(key,default) if isinstance(position,dict) else getattr(position,key,default)

def order_known_candidates(position,hosts,mappings,candidates,*,refuse=None):
 """Caller supplies revalidated mapping inventory, never presumed proofs."""
 entries=[];seen=set();invalidated={canonical_card_url(item['url']) for v in mappings.values() for item in v.get('mappings',[]) if item.get('state')=='invalidated'}
 for origin,rows in [('confirmed_identity',[{'url':u,'host':h,'observed_at':next((m.get('evidence',{}).get('observed_at') for m in v.get('mappings',[]) if m.get('url')==u and m.get('state')=='confirmed'),None),'source_ref':'source_registry:product_identity'} for h,v in mappings.items() for u in v.get('urls',[])]),('retained_candidate',candidates)]:
  for ordinal,row in enumerate(rows):
   row={'url':row} if isinstance(row,str) else dict(row);url=canonical_card_url(row.get('url'));host=host_for(url,hosts)
   if not url or (url,origin) in seen:continue
   seen.add((url,origin));reason='identity_invalidated' if url in invalidated else 'supplier_not_configured' if not host else refuse(position,url) if refuse else ''
   entries.append({**row,'url':url,'host':host,'origin':origin,'ordinal':ordinal,'pending':not bool(reason),'refusal_reason':reason or ''})
 # Confirmed first, then one head per resource before that resource's backups.
 queued=[];tails=[];seen_hosts=set();seen_urls=set()
 for entry in entries:
  if not entry['pending'] or entry['url'] in seen_urls:continue
  seen_urls.add(entry['url'])
  if entry['host'] not in seen_hosts:queued.append(entry);seen_hosts.add(entry['host'])
  else:tails.append(entry)
 return entries,queued+tails

def build_work_plan(positions,primary_hosts,reserve_hosts,mappings_by_row,candidates_by_row,*,refuse=None):
 hosts=list(dict.fromkeys([*primary_hosts,*reserve_hosts]));primary=set(primary_hosts)
 if not hosts or len({value(p,'number') for p in positions})!=len(positions):raise ValueError('Unique rows and configured resources required')
 rows=[];pairs=[];queues={};skipped=[]
 for index,position in enumerate(positions):
  unique=bool(value(position,'is_unique',False))
  if unique:
   skipped.append(value(position,'number'));rows.append({'index':index,'number':value(position,'number'),'status':'skipped','reason':'unique_position','candidates':[]});continue
  inventory,ordered=order_known_candidates(position,hosts,mappings_by_row.get(index,{}),candidates_by_row.get(index,[]),refuse=refuse)
  queues[index]=ordered;rows.append({'index':index,'number':value(position,'number'),'status':'pending','requested_identity':{k:value(position,k) for k in ('name','type_mark','unit','qty','discipline')},'source_evidence':value(position,'source_evidence',{}),'source_review_fields':value(position,'review_fields',{}),'source_review_accepted':value(position,'review_accepted',False),'candidates':inventory})
  for host in hosts:pairs.append({'row_index':index,'number':value(position,'number'),'host':host,'tier':'primary' if host in primary else 'reserve','status':'pending','product_presence':'unknown','planned_candidates':sum(e['host']==host for e in ordered)})
 # Descriptive full order; no initial-3 cap and no discarded tail.
 order=[]
 for depth in range(max(map(len,queues.values()),default=0)):
  for index,queue in queues.items():
   if depth<len(queue):order.append({'row_index':index,'number':value(positions[index],'number'),**queue[depth]})
 distinct={item['url'] for item in order}
 return {'version':1,'intent':'all_positions_all_configured_resources','positions_total':len(positions),'searchable_rows':len(positions)-len(skipped),'skipped_numbers':skipped,'primary_hosts':list(primary_hosts),'reserve_hosts':list(reserve_hosts),'logical_pairs_total':len(pairs),'pairs':pairs,'rows':rows,'candidate_order':order,'estimates':{'logical_candidate_admissions':len(order),'distinct_candidate_urls':len(distinct),'neutral_same_generation_best_case_documents':len(distinct),'no_reuse_upper_candidate_documents':len(order),'query_work_not_estimated_as_completed':True,'wire_work_unknown':True,'ttl_route_profile_can_increase_physical_work':True},'default_service_card_limit':None,'default_service_time_limit':None,'stop_after_three':False}

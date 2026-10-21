"""Bounded query rotations: primary sweep, reserve sweep, then deeper variants."""
from concurrent.futures import Future
from threading import Lock
from dataclasses import dataclass


@dataclass(frozen=True)
class DiscoveryTask:
    phase: str
    query: str | None = None
    hosts: tuple[str, ...] = ()
    retry: bool = False
    route: str = 'own_or_serp'


def separate_native_article(position):
    """Use the same native-cell binding as query construction, never a guessed SKU."""
    evidence = position.source_evidence or {}
    cells = evidence.get('cells', {})
    clean = lambda value: ' '.join(str(value or '').split())
    return (evidence.get('reader') == 'native_pdf' and isinstance(cells, dict)
            and bool(clean(cells.get('article')))
            and clean(cells.get('name')) == clean(position.name)
            and clean(cells.get('unit')) == clean(position.unit)
            and clean(' / '.join(dict.fromkeys(clean(cells.get(k)) for k in ('model','article')
                                              if clean(cells.get(k))))) == clean(position.type_mark)
            and not any(k in (position.review_fields or {}) for k in
                        ('name','type_mark','model','article','maker')))


class StagedDiscoveryPlan:
    """Group ranking is not a completed individual-domain search obligation."""
    def __init__(self, queries, primary, reserve, *, strong_article=False, batch=8):
        if batch < 1: raise ValueError('Supplier batch must be positive')
        self.queries = list(dict.fromkeys(' '.join(q.split()) for q in queries if q.strip()))[:3]
        self.tiers = []
        seen = set()
        for tier in (primary, reserve):
            hosts = []
            for host in tier:
                host = host.strip().lower().removeprefix('www.')
                if host and host not in seen:
                    seen.add(host); hosts.append(host)
            self.tiers.append(hosts)
        self.hosts = [host for tier in self.tiers for host in tier]
        self.strong_article = strong_article
        self.batch = batch
        self.admitted = set()

    def _tasks(self, phase, queries, hosts, *, batch=1,route='own_or_serp'):
        tasks = []
        wanted = set(hosts)
        for tier in self.tiers:
            for query in queries:
                pending = []
                for host in tier:
                    key = (route,phase == 'group', host, ' '.join(query.split()).casefold())
                    if host in wanted and key not in self.admitted:
                        self.admitted.add(key); pending.append(host)
                for i in range(0, len(pending), batch):
                    group = tuple(pending[i:i+batch])
                    actual_phase = 'individual' if phase == 'group' and len(group) == 1 else phase
                    if actual_phase != phase:
                        self.admitted.discard((route,True, group[0], ' '.join(query.split()).casefold()))
                        self.admitted.add((route,False, group[0], ' '.join(query.split()).casefold()))
                    tasks.append(DiscoveryTask(actual_phase, query, group,route=route))
        return tasks

    def initial(self):
        if not self.queries: return []
        tasks = self._tasks('group', self.queries[:1], self.hosts, batch=self.batch)
        return tasks + [DiscoveryTask('fallback')] if tasks else []

    def fallback(self, observations, confirmed,quote_resolved=()):
        own_hosts=[host for host in self.hosts if observations.get(host,{}).get('own_search_positive') and host not in quote_resolved and not observations.get(host,{}).get('serp_fallback_attempted')]
        hosts = [host for host in self.hosts
                 if host not in quote_resolved and not observations.get(host,{}).get('own_search_positive') and observations.get(host, {}).get('group_success')
                 and (host not in confirmed or observations[host].get('group_window') != 'below_limit')]
        return self._tasks('individual',self.queries[:1],own_hosts,route='serp_only')+self._tasks('individual', self.queries[:1], hosts) + [DiscoveryTask('literal')]

    def escalation_hosts(self, observations, confirmed,quote_resolved=()):
        return [host for host in self.hosts
                if host not in quote_resolved and observations.get(host, {}).get('individual_success')
                and (host not in confirmed or observations[host].get('individual_window') != 'below_limit')
                and (not self.strong_article or observations[host].get('individual_candidates')
                     or observations[host].get('individual_window') != 'below_limit')]

    def literal(self, observations, confirmed,quote_resolved=()):
        hosts = self.escalation_hosts(observations, confirmed,quote_resolved)
        own=[h for h in hosts if observations.get(h,{}).get('own_search_positive')]
        return self._tasks('individual', self.queries[1:], [h for h in hosts if h not in own])+self._tasks('individual',self.queries[1:],own,route='serp_only') + (
            [DiscoveryTask('semantic')] if hosts else [])

    def semantic(self, queries, observations, confirmed,quote_resolved=()):
        hosts=self.escalation_hosts(observations,confirmed,quote_resolved)
        own=[h for h in hosts if observations.get(h,{}).get('own_search_positive')]
        return self._tasks('individual',queries,[h for h in hosts if h not in own])+self._tasks('individual',queries,own,route='serp_only')


class QueryExpansionCache:
    """Job-local single-flight reuse; never share model output across jobs/providers."""
    def __init__(self):
        self._lock = Lock()
        self._values = {}

    def get(self, name, mark, generate):
        key = (" ".join(name.split()), " ".join(mark.split()))
        with self._lock:
            owner = key not in self._values
            future = self._values.setdefault(key, Future())
        if owner:
            try:
                future.set_result(tuple(generate()))
            except BaseException as exc:
                future.set_exception(exc)
                with self._lock:
                    self._values.pop(key, None)
                raise
        return list(future.result())


class DiscoveryObservationCache:
    """Reuse successful job-local discovery outcomes, never another row's quote."""
    def __init__(self):
        self._lock = Lock()
        self._values = {}

    def get(self, query, hosts, limit, discover, *, identity=None, query_step=None,route='own_or_serp'):
        from copy import deepcopy
        from .article_query_plan import QueryStep
        hint=(query_step.query,query_step.kind,query_step.identity_hint) if type(query_step) is QueryStep else None
        from .supplier_discovery import discovery_intent_key
        key = (4,route,discovery_intent_key(),identity, ' '.join(query.split()).casefold(), tuple(sorted(hosts)), limit,hint)
        with self._lock:
            owner = key not in self._values
            future = self._values.setdefault(key, Future())
        if owner:
            try:
                result = discover()
                future.set_result(result)
                if getattr(result, 'origin', 'unknown') == 'unknown' or any(o.get('diagnostics') for o in getattr(result,'host_observations',{}).values()):
                    with self._lock: self._values.pop(key, None)
            except BaseException as exc:
                future.set_exception(exc)
                with self._lock: self._values.pop(key, None)
                raise
        return deepcopy(future.result()), not owner


def query_sweeps(queries, primary, reserve, *, batch=8):
    if batch < 1:
        raise ValueError('Supplier batch must be positive')
    queries = list(dict.fromkeys(q.strip() for q in queries if q.strip()))[:3]
    if not queries:
        return []
    seen = set()
    def groups(hosts):
        unique = []
        for host in hosts:
            host = host.strip().lower().removeprefix('www.')
            if host and host not in seen:
                seen.add(host)
                unique.append(host)
        return [unique[i:i+batch] for i in range(0, len(unique), batch)]
    main, fallback = groups(primary), groups(reserve)
    def sweep(site_groups, rotation):
        return [(queries[(index+rotation) % len(queries)], group)
                for index,group in enumerate(site_groups)]
    tasks = sweep(main, 0) + sweep(fallback, 0)
    for rotation in range(1, len(queries)):
        tasks.extend(sweep(main, rotation))
    for rotation in range(1, len(queries)):
        tasks.extend(sweep(fallback, rotation))
    return tasks


def completion_plan_tasks(plan, phase, observations, confirmed, resolved):
    """Same bounded canonical plan; sentinels are phase transitions, not requests."""
    tasks = (plan.initial() if phase == 'initial' else
             plan.fallback(observations, confirmed, resolved) if phase == 'fallback' else
             plan.literal(observations, confirmed, resolved))
    return [dict(phase=t.phase, query=t.query, hosts=list(t.hosts), strategy=t.route)
            for t in tasks if t.query is not None]


def expand_discovery_tasks(tasks, steps):
    """Fresh v5 physical queue; logical planner admissions stay unchanged."""
    from .supplier_discovery import PROVIDERS, valid_step
    out=[]
    for task in tasks:
        if task.query is None:
            out.append(task);continue
        own=[h for h in task.hosts if h in PROVIDERS] if task.route=='own_or_serp' and valid_step(steps.get(task.query),task.query) else []
        out.extend(DiscoveryTask(task.phase,task.query,(host,),route='own_only') for host in own)
        out.append(DiscoveryTask(task.phase,task.query,task.hosts,route='serp_only') if own else task)
    return out


def own_prefix(queries, task):
    """Deterministic immediately preceding singleton records of this parent."""
    if task['strategy']!='serp_only':return []
    index=next(i for i,q in enumerate(queries) if q['id']==task['id']);out=[]
    for q in reversed(queries[:index]):
        if q['strategy']!='own_only' or q['phase']!=task['phase'] or q['query']!=task['query'] or len(q['hosts'])!=1 or q['hosts'][0] not in task['hosts']:break
        out.append(q)
    return list(reversed(out))

"""Human-owned one-route policy. No Settings/file/network access at import."""
import json,re,sqlite3,time,threading
from contextlib import contextmanager
from urllib.parse import urlsplit
from pathlib import Path
from .search.failure_policy import ResearchControlError

METHODS={'http','browser'}
CAUSES={'http_access','rate_limit','protection','network_timeout','network_error','representation','navigation'}
class MethodInvalid(ValueError):pass
class MethodUnavailable(ValueError):
    def diagnostic(self):
        return dict(code='supplier_method_requires_configuration',reason='Выбранный способ получения данных недоступен; требуется ручная настройка поставщика',failure_source='supplier_method',failure_scope='site',failure_cause='requires_configuration')
class MethodControlError(ResearchControlError):pass

def seeds(path):
    out={}
    for line in Path(path).read_text().splitlines():
        if not line.startswith('|'):continue
        c=[v.strip()for v in line.strip('|').split('|')]
        if len(c)<5 or not c[2].isdigit():continue
        p=urlsplit(c[3]);host=(p.hostname or '').removeprefix('www.').lower()
        if p.scheme!='https' or p.username or p.password or c[4]not in METHODS or host in out:raise MethodInvalid('supplier_method_seed_invalid')
        out[host]={'method':c[4],'name':c[0]}
    if len(out)!=36:raise MethodInvalid('supplier_method_seed_count')
    return out

class MethodStore:
    def __init__(self,path,inventory,*,clock=time.time):
        self.path=Path(path);self.inventory=seeds(inventory);self.clock=clock
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.tx()as db:
            db.execute('CREATE TABLE IF NOT EXISTS methods(host TEXT PRIMARY KEY,method TEXT NOT NULL,revision INTEGER NOT NULL,state TEXT NOT NULL,changed REAL NOT NULL,actor TEXT NOT NULL,reason TEXT NOT NULL,last_failure TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY,host TEXT,revision INTEGER,kind TEXT,record TEXT)')
            for host,row in self.inventory.items():
                db.execute("INSERT OR IGNORE INTO methods VALUES(?,?,1,'configured_unchecked',?,'directory-seed','',NULL)",(host,row['method'],self.clock()))
    @contextmanager
    def tx(self):
        db=sqlite3.connect(self.path,timeout=10);db.row_factory=sqlite3.Row
        try:db.execute('PRAGMA synchronous=FULL');db.execute('BEGIN IMMEDIATE');yield db;db.commit()
        except BaseException:db.rollback();raise
        finally:db.close()
    def host(self,url):
        try:p=urlsplit(url);h=(p.hostname or '').lower().removeprefix('www.')
        except(ValueError,TypeError):return None
        if p.scheme not in {'https','http'}or p.username or p.password:return None
        return next((d for d in self.inventory if h==d or h.endswith('.'+d)),None)
    def snapshot(self):
        with self.tx()as db:
            rows={r['host']:dict(r)for r in db.execute('SELECT * FROM methods')}
        result=[]
        for host in self.inventory:
            row=rows[host];row['name']=self.inventory[host]['name']
            row['last_failure']=json.loads(row['last_failure'])if row['last_failure']else None
            result.append(row)
        return {'version':1,'rows':result}
    def change(self,host,method,revision,actor,reason):
        if host not in self.inventory or method not in METHODS or type(revision)is not int or revision<1 or type(actor)is not str or not actor or type(reason)is not str or not 1<=len(reason.strip())<=512:raise MethodInvalid('supplier_method_change_invalid')
        with self.tx()as db:
            row=db.execute('SELECT * FROM methods WHERE host=?',(host,)).fetchone()
            if row['revision']!=revision:raise MethodInvalid('supplier_method_revision_conflict')
            now=self.clock();record=dict(method=method,actor=actor,reason=reason,changed=now,previous_method=row['method'])
            db.execute("UPDATE methods SET method=?,revision=revision+1,state='configured_unchecked',changed=?,actor=?,reason=? WHERE host=?",(method,now,actor,reason,host))
            db.execute("INSERT INTO history(host,revision,kind,record)VALUES(?,?,'manual',?)",(host,revision+1,json.dumps(record)))
        return self.snapshot()
    def fail(self,host,method,revision,cause,status=None):
        if host not in self.inventory or method not in METHODS or type(revision)is not int or cause not in CAUSES or status is not None and(type(status)is not int or not 100<=status<=599):raise MethodInvalid('supplier_method_failure_invalid')
        with self.tx()as db:
            row=db.execute('SELECT * FROM methods WHERE host=?',(host,)).fetchone()
            if row['revision']!=revision or row['method']!=method:return {'recorded':False,'stale':True}
            record=dict(cause=cause,http_status=status,observed_at=self.clock(),method=method,revision=revision)
            if row['state']!='requires_configuration':
                db.execute("UPDATE methods SET state='requires_configuration',last_failure=? WHERE host=?",(json.dumps(record),host))
                db.execute("INSERT INTO history(host,revision,kind,record)VALUES(?,?,'failure',?)",(host,revision,json.dumps(record)))
        return {'recorded':True,'stale':False}

_REMOTE=None
_REMOTE_LOCK=threading.RLock()
def bind_remote(callback):
    global _REMOTE
    with _REMOTE_LOCK:_REMOTE=callback

def store():
    from .config import OUTPUT_DIR,SUPPLIERS_FILE
    return MethodStore(OUTPUT_DIR/'supplier_methods'/'methods.sqlite3',SUPPLIERS_FILE)

def _call(action,payload=None):
    with _REMOTE_LOCK:callback=_REMOTE
    if callback is not None:
        try:return callback(action,payload)
        except ResearchControlError:raise
        except Exception:raise MethodControlError('supplier_method_control_unavailable')from None
    from .config import settings
    if getattr(settings,'execution_mode','local')=='remote'and getattr(settings,'runner_role','control')=='executor':raise MethodControlError('supplier_method_claim_required')
    native=store()
    return native.snapshot()if action=='snapshot'else native.fail(**payload)

def selected(url,expected=None):
    try:
        p=urlsplit(url);p.port;host=(p.hostname or '').lower().removeprefix('www.')
        if p.scheme not in {'http','https'}or not host or p.username or p.password or re.search(r'[\\\x00-\x20\x7f]',url):raise ValueError()
    except (ValueError,TypeError):raise MethodInvalid('supplier_method_url_invalid')from None
    from .config import SUPPLIERS_FILE
    inventory=seeds(SUPPLIERS_FILE)
    if not any(host==h or host.endswith('.'+h)for h in inventory):return None
    snapshot=_call('snapshot')
    if type(snapshot)is not dict or snapshot.get('version')!=1 or type(snapshot.get('rows'))is not list or len(snapshot['rows'])!=36:raise MethodControlError('supplier_method_snapshot_invalid')
    rows=snapshot['rows'];row=next((dict(r)for r in rows if host==r.get('host')or host.endswith('.'+r.get('host','!'))),None)
    if row is None:raise MethodControlError('supplier_method_host_missing')
    if row.get('method')not in METHODS or type(row.get('revision'))is not int or row['revision']<1 or row.get('state')not in {'configured_unchecked','requires_configuration'}:raise MethodControlError('supplier_method_snapshot_invalid')
    if row['state']=='requires_configuration':raise MethodUnavailable('supplier_method_requires_configuration')
    if expected is not None and row['method']!=expected:raise MethodUnavailable('supplier_method_route_refused')
    return row

def current(grant,url):
    if grant is None:return
    row=selected(url)
    if row is None or (row['method'],row['revision'])!=(grant['method'],grant['revision']):raise MethodUnavailable('supplier_method_revision_changed')

def failure(grant,cause,status=None):
    if grant is not None:
        from .search.session import bounded_timeout
        bounded_timeout(0)  # Global account/cancellation/deadline precedes host mutation.
        return _call('failure',dict(host=grant['host'],method=grant['method'],revision=grant['revision'],cause=cause,status=status))

def response(grant,status,text=''):
    if grant is None:return
    cause='http_access'if status in {401,403,407}else'rate_limit'if status==429 else'network_error'if type(status)is int and status>=500 else None
    if cause:failure(grant,cause,status);raise MethodUnavailable('supplier_method_requires_configuration')

def failed(grant,error,cause='network_error'):
    """Report through the existing revision fence and return its acknowledgement."""
    # Account/global allocation/cancellation are control flow, never host failures.
    if isinstance(error,ResearchControlError):return
    context=getattr(error,'_kac_supplier_failure',None)
    if getattr(context,'scope',None)=='global'or getattr(context,'cause',None)=='proxy_connection_failure':return
    name=type(error).__name__.lower()
    if 'timeout'in name:cause='network_timeout'
    return failure(grant,cause)

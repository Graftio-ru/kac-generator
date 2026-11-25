"""Private, approval-bound reuse of a retained native card, never a capture.

An approved artifact is an authority boundary, not a supplier-provided flag.
Its input and image are revalidated by the ordinary native producer. Historical
capture outcomes remain unknown even when their saved facts prove a quotation.
"""
from pathlib import Path
import copy,hashlib,json,os,re,threading,uuid

_ISSUER=object()

def _exact(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':'))

def _context(operation):
    from .document_context import active_document_operation,_require,_ISSUER as OP_ISSUER
    operation._control()
    _require(operation._issuer is OP_ISSUER and active_document_operation()is operation
        and threading.current_thread()is operation.creator and operation.state=='preparing',
        'document_import_context')

class _PhotoInput:
    def __init__(self,issuer,operation,root,photo,sha256):
        from .document_context import _require
        _require(issuer is _ISSUER,'document_import_issuer')
        self.operation=operation;self.root=root;self.photo=photo;self.sha256=sha256
    def validates(self,path):
        from .document_context import _require,_sha
        _context(self.operation)
        _require(self.operation._retained_importing is self,'document_import_context')
        path=Path(path)
        _require(path.is_absolute() and not any(p.is_symlink()for p in (path,*path.parents))
            and path.resolve()==self.photo and path.resolve().parent==self.root
            and path.stat().st_size<=20*1024*1024 and _sha(path)==self.sha256,
            'document_import_photo_changed')
        return self.root

def _photo_root(capability,path):
    from .document_context import _require
    _require(type(capability)is _PhotoInput,'document_import_issuer')
    return capability.validates(path)

class _RetainingStore:
    """Native source boundary and genuine owned output store are distinct."""
    def __init__(self,capability,delegate):
        self.capability=capability;self.delegate=delegate
    @property
    def root(self):return self.capability.root
    @property
    def max_bytes(self):return self.delegate.max_bytes
    def retain(self,data,**kwargs):
        from .document_context import _require
        self.capability.validates(self.capability.photo)
        _require(hashlib.sha256(data).hexdigest()==self.capability.sha256,'document_import_photo_changed')
        return self.delegate.retain(data,**kwargs)

def _manifest(operation,reference):
    from .document_context import _artifact,_require,_file
    value=_artifact(reference,operation.artifact_root)
    fields={'version','kind','product_id','base_catalogue_sha256','base_revision','url',
        'captured_input','capture_operation_id','capture_reference','source_root',
        'cache_files','accepted_derivation'}
    _require(type(value)is dict and set(value)==fields and type(value['version'])is int
        and value['version']==1 and value['kind']=='retained_native_card','document_import_shape')
    _require(value['product_id']==operation.approval['product_id']
        and value['base_catalogue_sha256']==operation.approval['catalogue']['sha256']
        and type(value['base_revision'])is int and value['base_revision']==operation.approval['revision'],
        'document_import_base_changed')
    from .product_identity import canonical_card_url
    _require(type(value['url'])is str and value['url']==canonical_card_url(value['url'])
        and type(value['capture_operation_id'])is str and re.fullmatch('[a-f0-9]{32}',value['capture_operation_id']),
        'document_import_binding')
    proof=value['accepted_derivation']
    _require(type(proof)is dict and set(proof)=={'result','method','source_manifest'},'document_import_derivation')
    # The operation approval pins these reviewed artifacts. They establish
    # provenance only; no READY/offer/numeric value is admitted from them.
    for role,ref in proof.items():
        _require(type(ref)is dict and set(ref)=={'path','sha256'} and type(ref['path'])is str
            and Path(ref['path']).suffix==('.py'if role=='method'else'.json')
            and not any(p.startswith('.')or any(word in p.casefold()for word in ('secret','credential','.env','profile'))for p in Path(ref['path']).parts),
            'document_import_derivation')
        _file(ref,operation.artifact_root)
    return value

def _inputs(operation,value,output):
    from .document_context import _require,_file,_sha
    root=Path(value['source_root'])
    _require(root.is_absolute() and root.is_dir() and root.resolve().is_relative_to(operation.artifact_root.resolve())
        and not any(p.is_symlink()for p in (root,*root.parents)) and root.resolve()!=output,
        'document_import_source_root')
    root=root.resolve();members=value['cache_files']
    _require(type(members)is dict and 1<=len(members)<=512,'document_import_members')
    total=0;paths=[]
    for name,pin in members.items():
        _require(type(name)is str and not Path(name).is_absolute() and '..'not in Path(name).parts
            and Path(name).suffix in {'.json','.png'}
            and not any(part.startswith('.')or part.casefold()in {'cookies.json','private','profiles','profile','logs'}or any(word in part.casefold()for word in ('credential','secret','.env'))for part in Path(name).parts)
            and type(pin)is dict and set(pin)=={'sha256','bytes'} and type(pin['bytes'])is int
            and 0<=pin['bytes']<=20*1024*1024,'document_import_member')
        source=_file({'path':str(root/name),'sha256':pin['sha256']},operation.artifact_root)
        _require(source.is_relative_to(root) and source.stat().st_size==pin['bytes'],'document_import_member')
        total+=pin['bytes'];_require(total<=64*1024*1024,'document_import_members_large');paths.append((name,source,pin))
    # Verify every source and destination before copying any bytes. Existing
    # different bytes are a conflict, never a reason to overwrite cache facts.
    for name,source,pin in paths:
        destination=output/name
        _require(not any(p.is_symlink()for p in (destination,*destination.parents))
            and destination.resolve().is_relative_to(output),'document_import_destination')
        if destination.exists():_require(_sha(destination)==pin['sha256'],'document_import_destination_changed')
    for name,source,pin in paths:
        destination=output/name
        if not destination.exists():
            destination.parent.mkdir(parents=True,exist_ok=True)
            with destination.open('xb')as writer:
                writer.write(source.read_bytes());writer.flush();os.fsync(writer.fileno())
        _require(_sha(destination)==pin['sha256'],'document_import_destination_changed')
    path=_file(value['captured_input'],operation.artifact_root)
    reference=value['capture_reference']
    _require(type(reference)is dict and set(reference)=={'ref','record_sha256','snapshot_sha256','card_id','url','captured_at'}
        and type(reference['ref'])is str and re.fullmatch('captured_inputs/[a-f0-9]{32}\\.json',reference['ref'])
        and path==root/reference['ref'] and value['captured_input']['sha256']==reference['record_sha256']
        and reference['ref']in members and members[reference['ref']]['sha256']==reference['record_sha256'],
        'document_import_input_binding')
    record=json.loads(path.read_bytes())
    import math
    _require(type(record)is dict and set(record)=={'version','id','payload','sha256','deadline','deadline_unbounded'}
        and type(record['version'])is int and record['version']==1 and type(record['payload'])is str
        and type(record['deadline_unbounded'])is bool
        and (record['deadline']is None or type(record['deadline'])in(int,float)and math.isfinite(record['deadline']))
        and (not record['deadline_unbounded']or record['deadline']is None)
        and record['id']==reference['card_id'] and record['sha256']==reference['snapshot_sha256']
        and hashlib.sha256(record['payload'].encode()).hexdigest()==record['sha256'],
        'document_import_input_changed')
    from .card_batch import CardSnapshot
    snapshot=CardSnapshot(record['id'],record['payload'],record['sha256'],float('inf')if record['deadline_unbounded']else record['deadline'])
    facts=snapshot.facts()
    _require(facts.get('url')==value['url']==reference['url']
        and _exact(facts.get('captured_at'))==_exact(reference['captured_at']),
        'document_import_input_binding')
    photo=Path(facts.get('screenshot_context',''));context=facts.get('context_capture_evidence')or{}
    _require(photo.is_absolute() and photo.parent==root and photo.name in members
        and members[photo.name]['sha256']==context.get('sha256'),'document_import_photo_binding')
    capability=_PhotoInput(_ISSUER,operation,root,photo,members[photo.name]['sha256'])
    return snapshot,capability

def _link(operation,value):
    from .document_context import _require
    from .product_identity import card_host
    states={}
    with operation.store._connection()as db:
        for row in db.execute('SELECT payload FROM attempts WHERE product_id=? AND host=? AND url=? ORDER BY id',
            (operation.approval['product_id'],card_host(value['url']),value['url'])):
            item=json.loads(row['payload']).get('card_operation')
            if item:states[item['operation_id']]=item
    item=states.get(value['capture_operation_id'])
    _require(item is not None and item['state']=='pending'
        and _exact(item.get('evidence',{}).get('captured_input'))==_exact(value['capture_reference']),
        'document_import_capture_link')

def import_retained(operation,url,*,image_store):
    from .document_context import _require,_digest
    from ..config import CACHE_DIR
    from .catalogue_images import CatalogueImageStore
    _context(operation)
    _require(operation._transaction is None and operation._retained_importing is None,'document_import_nested')
    _require(operation.approval['version']==3,'document_import_unapproved')
    values=[(ref,_manifest(operation,ref))for ref in operation.approval['retained_imports']]
    selected=[(ref,value)for ref,value in values if value['url']==url]
    _require(len(selected)==1 and not any(v['url']==url for v in operation.derived_imports),'document_import_scope_or_reused')
    reference,value=selected[0];output=Path(CACHE_DIR).resolve()
    _require(type(image_store)is CatalogueImageStore and image_store.root.resolve()==output
        and image_store.client is None and not image_store.bucket and output.is_relative_to(operation.root.parent)
        and not any(p.is_symlink()for p in (Path(CACHE_DIR),*Path(CACHE_DIR).parents)),
        'document_import_output_store')
    with operation.store._connection(True)as connection:
        operation._transaction=connection;operation._transaction_thread=threading.current_thread()
        try:
            _require(operation.store.read_snapshot(operation.identities)==operation.expected_snapshot,'document_source_changed')
            operation._check_history();_link(operation,value)
            before=operation._raw_history()
            snapshot,capability=_inputs(operation,value,output)
            operation._retained_importing=capability
            # Durable input lineage precedes financial publication. A failed
            # import remains explicit/unknown and cannot be repeated silently.
            entry=dict(url=url,manifest_sha256=reference['sha256'],captured_input=copy.deepcopy(value['capture_reference']),state='input_retained')
            marker=operation.root/('derived-input-'+_digest(reference)+'.json')
            with marker.open('x')as writer:
                json.dump(entry,writer,ensure_ascii=False,allow_nan=False);writer.flush();os.fsync(writer.fileno())
            operation.derived_imports.append(entry)
            directory_fd=os.open(operation.root,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
            try:os.fsync(directory_fd)
            finally:os.close(directory_fd)
            from . import agent,catalogue_producer
            from .http_card import _retained_photo_scope
            from .product_identity import request_alternatives_scope,_resolved_model_role,_original_native_model_role,card_host
            from .factory_reference import request_scope
            identity=operation.identities[0];position=operation.positions[0]
            product=operation.store.lookup(identity)
            current=next((h['current']for h in product['heads']if h['url']==url),{})
            with _retained_photo_scope(capability),request_alternatives_scope(position),_resolved_model_role(current,identity),_original_native_model_role(operation.store,identity,position),request_scope(position):
                data=agent._deterministic_card_data(snapshot)
                _require(data is not None,'document_import_financial_unproven')
                offer=agent._finish_captured_card(None,snapshot,data,enrich_seller=False)
                _require(offer is not None and not offer.unit_review_required and bool(offer.source_quote_evidence),'document_import_financial_unproven')
                operation._control()
                observation=catalogue_producer.publish_snapshot(operation.store,identity,position,snapshot,
                    image_store=_RetainingStore(capability,image_store),offer=offer,
                    observation_id=uuid.uuid4().hex,supplier_host=card_host(url))
            _context(operation);operation._check_history()
            after=operation._raw_history()
            _require(after['attempts']==before['attempts'] and len(after['observations'])==len(before['observations'])+1
                and observation.observation_id in after['observations'],'document_import_transition')
            # The producer can retain source facts while refusing an image.
            # Such a result must not become a successful retained import.
            from .catalogue_projection import project_product,catalogue_scope
            from .selected_proofs import proof_ready
            from ..offer_selection import offer_is_comparable
            with catalogue_scope(operation.store):
                projected=project_product(operation.store,identity,position)
                admitted=[o for o in projected.all_offers if (o.catalogue_ref or {}).get('observation_id')==observation.observation_id]
                _require(len(admitted)==1 and offer_is_comparable(admitted[0],position)
                    and proof_ready(admitted[0],position),'document_import_proof_unconfirmed')
            # Recheck the pinned manifest and all imported immutable bytes at
            # the commit boundary; no newly discovered file can grant access.
            _require(_exact(_manifest(operation,reference))==_exact(value),'document_import_manifest_changed')
            from .document_context import _sha
            for name,pin in value['cache_files'].items():
                source=Path(value['source_root'])/name;destination=output/name
                _require(not any(p.is_symlink()for p in (source,destination,*source.parents,*destination.parents))
                    and _sha(source)==pin['sha256']==_sha(destination),'document_import_input_changed')
            _link(operation,value);_context(operation)
            expected=operation.store.read_snapshot(operation.identities)
            operation.store.validate_document_snapshot(expected,operation.identities,operation)
            # No fallible validation after commit: the writer fence covers all
            # checks and only this verified transition advances expectation.
            connection.commit();operation.expected_snapshot=expected
            entry.update(state='published',observation_id=observation.observation_id)
        finally:
            operation._retained_importing=None;operation._transaction=None;operation._transaction_thread=None
    operation._write_receipt('preparing')
    return observation

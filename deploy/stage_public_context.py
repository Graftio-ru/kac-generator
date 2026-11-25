"""Inert tool: later stage ONLY an externally accepted source manifest, never a working tree."""
import argparse, hashlib, json
from pathlib import Path

def stage(packet, accepted_sha256, output):
    packet = Path(packet).resolve(strict=True)
    manifest = packet / 'source-manifest.json'
    raw = manifest.read_bytes()
    if hashlib.sha256(raw).hexdigest() != accepted_sha256:
        raise ValueError('accepted_manifest_hash_mismatch')
    entries = json.loads(raw)['runtime_source_files']
    root = packet / 'autosmetchik'
    prepared = []
    forbidden = {'data','cache','profiles','.git','.4dt','keys','secrets','__pycache__'}
    for name, pin in entries.items():
        rel = Path(name)
        if rel.is_absolute() or '..' in rel.parts or any(p in forbidden or p.startswith('.env') for p in rel.parts) or rel.suffix in {'.db','.log','.pyc'} or '.sqlite' in rel.name:
            raise ValueError('nonpublic_manifest_member')
        src = root / rel
        if not src.resolve().is_relative_to(root.resolve()) or any(p.is_symlink() for p in (src,*src.parents)):
            raise ValueError('unsafe_source_path')
        data = src.read_bytes()
        if len(data) != pin['bytes'] or hashlib.sha256(data).hexdigest() != pin['sha256']:
            raise ValueError('source_member_hash_mismatch')
        prepared.append((rel,data))
    if not {'engine/requirements.txt','engine/app.py','web/index.html','web/runtime-config.js','deploy/Dockerfile','deploy/.dockerignore'} <= set(entries):
        raise ValueError('incomplete_server_source')
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError('context_must_be_new')
    output.mkdir(parents=True)
    for rel,data in prepared:
        target = output/'public'/rel
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(data)
    by_name = {str(rel):data for rel,data in prepared}
    for name in ['Dockerfile','.dockerignore']:
        (output/name).write_bytes(by_name['deploy/'+name])
    for rel,data in prepared:
        if (output/'public'/rel).read_bytes() != data:
            raise ValueError('staged_member_changed')
    (output/'source-binding.json').write_text(json.dumps({'accepted_manifest_sha256':accepted_sha256,'source_files':len(prepared)},indent=2)+'\n')
    return len(prepared)

if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('--packet',required=True);p.add_argument('--accepted-manifest-sha256',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();print(stage(a.packet,a.accepted_manifest_sha256,a.output))

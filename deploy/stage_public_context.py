"""Stage a Docker context from an explicitly accepted, hash-pinned runtime manifest."""

import argparse
import hashlib
import json
from pathlib import Path
import re

# These application resources are resolved relative to the repository root at runtime.
ROOT_RESOURCES = {
    'supplier-methods.html',
    'Поставщики.md',
    'Поставщики_резерв_Москва.md',
    'ТСН-2001_электро_автоматика.md',
    'индексы.md',
}
DEPLOY_FILES = {
    'deploy/.dockerignore',
    'deploy/Caddyfile',
    'deploy/Dockerfile',
    'deploy/compose.runner-control.yaml',
    'deploy/compose.runner-worker.yaml',
    'deploy/compose.yaml',
    'deploy/operator-inputs.template',
    'deploy/stage_public_context.py',
}
REQUIRED_FILES = {
    'engine/requirements.txt', 'engine/app.py', 'web/index.html',
    'web/runtime-config.js', 'deploy/Dockerfile', 'deploy/.dockerignore',
}


def _application_paths(policy: bytes) -> frozenset[str]:
    """Read exact Docker file exceptions and reject rules that widen the inventory."""
    rules = [line for line in policy.decode('utf-8').splitlines()
             if line and not line.startswith('#')]
    if rules[:3] != ['**', '!Dockerfile', '!.dockerignore']:
        raise ValueError('invalid_runtime_dockerignore')
    paths = set()
    for rule in rules[3:]:
        if not re.fullmatch(r'!public/(?:[\w-][\w.-]*/)*[\w-][\w.-]*', rule):
            raise ValueError('invalid_runtime_dockerignore')
        name = rule.removeprefix('!public/')
        if not name.startswith(('engine/', 'web/')) and name not in ROOT_RESOURCES:
            raise ValueError('invalid_runtime_dockerignore')
        if name in paths:
            raise ValueError('duplicate_runtime_dockerignore_member')
        paths.add(name)
    if not (ROOT_RESOURCES | (REQUIRED_FILES - DEPLOY_FILES)) <= paths:
        raise ValueError('incomplete_runtime_dockerignore')
    return frozenset(paths)


DOCKERIGNORE = Path(__file__).with_name('.dockerignore').read_bytes()
RUNTIME_SOURCE_FILES = _application_paths(DOCKERIGNORE) | DEPLOY_FILES


def is_runtime_source(name: str) -> bool:
    """Tell the release producer and stager whether an exact member path is approved."""
    return name in RUNTIME_SOURCE_FILES


def stage(packet, accepted_sha256, output):
    """Verify manifest and members before creating a fresh, byte-checked context."""
    packet = Path(packet).resolve(strict=True)
    raw = (packet / 'source-manifest.json').read_bytes()
    if hashlib.sha256(raw).hexdigest() != accepted_sha256:
        raise ValueError('accepted_manifest_hash_mismatch')
    entries = json.loads(raw)['runtime_source_files']
    for name in entries:
        if not is_runtime_source(name):
            raise ValueError(f'nonruntime_manifest_member: {name!r}')
    if not (REQUIRED_FILES | ROOT_RESOURCES) <= entries.keys():
        raise ValueError('incomplete_server_source')

    root = packet / 'autosmetchik'
    prepared = {}
    for name, pin in entries.items():
        src = root / name
        if not src.resolve().is_relative_to(root.resolve()) or any(p.is_symlink() for p in (src, *src.parents)):
            raise ValueError('unsafe_source_path')
        data = src.read_bytes()
        if len(data) != pin['bytes'] or hashlib.sha256(data).hexdigest() != pin['sha256']:
            raise ValueError('source_member_hash_mismatch')
        if name == 'deploy/.dockerignore' and data != DOCKERIGNORE:
            raise ValueError('runtime_dockerignore_mismatch')
        prepared[name] = data

    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError('context_must_be_new')
    output.mkdir(parents=True)
    for name, data in prepared.items():
        target = output / 'public' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    for name in ('Dockerfile', '.dockerignore'):
        (output / name).write_bytes(prepared['deploy/' + name])
    for name, data in prepared.items():
        if (output / 'public' / name).read_bytes() != data:
            raise ValueError('staged_member_changed')
    (output / 'source-binding.json').write_text(json.dumps({
        'accepted_manifest_sha256': accepted_sha256,
        'source_files': len(prepared),
    }, indent=2) + '\n')
    return len(prepared)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--packet', required=True)
    parser.add_argument('--accepted-manifest-sha256', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    print(stage(args.packet, args.accepted_manifest_sha256, args.output))

# SPDX-License-Identifier: GPL-3.0-only
"""Reconstruct the frozen candidate from public bytes; do not execute downloads."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import urllib.parse
import urllib.request
import zipfile


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def relative(value):
    p = PurePosixPath(value)
    if not value or '\\' in value or ':' in value or p.is_absolute() or any(
            x in ('', '.', '..') or x.rstrip(' .') != x for x in value.split('/')):
        raise ValueError('Invalid relative path')
    return Path(*p.parts)


def verified(path, row):
    return path.is_file() and not path.is_symlink() and path.stat().st_size == row['bytes'] and digest(path) == row['sha256']


def download(row, cache, offline):
    target = cache / row['sha256']
    if verified(target, row):
        return target
    if offline:
        raise FileNotFoundError('Missing fixed cache object: ' + row['sha256'])
    url = urllib.parse.urlparse(row['url'])
    if url.scheme != 'https' or url.hostname not in {
            'github.com', 'files.pythonhosted.org', 'download.pytorch.org',
            'www.gyan.dev', 'huggingface.co'}:
        raise ValueError('Download origin outside fixed public allowlist')
    tmp = target.with_suffix('.partial')
    request = urllib.request.Request(row['url'], headers={'User-Agent': 'RADAR-CoverLab-Compatibility/0.1'})
    try:
        with urllib.request.urlopen(request, timeout=60) as src, tmp.open('wb') as dst:
            size = 0
            while block := src.read(1024 * 1024):
                size += len(block)
                if size > row['bytes']:
                    raise ValueError('Download exceeds frozen size')
                dst.write(block)
        if not verified(tmp, row):
            raise ValueError('Download failed frozen SHA256/size')
        tmp.replace(target)
        return target
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def validate_plan(rows, plan, overlay):
    expected = {r['path']: r for r in rows}
    if len(expected) != len(rows):
        raise ValueError('Duplicate manifest paths')
    assigned = set()
    for item in plan:
        if item['kind'] == 'file':
            targets = [item['target']]
            if any(item[k] != expected[item['target']][k] for k in ('bytes', 'sha256')):
                raise ValueError('Direct download does not match original manifest')
        elif item['kind'] in ('zip', 'tar'):
            targets = list(item['members'].values())
            for member in item['members']:
                relative(member)
        else:
            raise ValueError('Unknown archive kind')
        for target in targets:
            relative(target)
            if target not in expected or target in assigned:
                raise ValueError('Unknown or duplicate target: ' + target)
            assigned.add(target)
    for name, row in expected.items():
        source = overlay / relative(name)
        if name in assigned:
            if source.exists():
                raise ValueError('Overlay shadows downloaded target: ' + name)
        elif not verified(source, row):
            raise ValueError('Overlay mismatch: ' + name)
    actual = {p.relative_to(overlay).as_posix() for p in overlay.rglob('*') if p.is_file()}
    allowed = (set(expected) - assigned) | {'payload-sha256.json'}
    if actual != allowed or any(p.is_symlink() for p in overlay.rglob('*')):
        raise ValueError('Unexpected overlay contents')
    return expected


def extract_members(archive, entry, target, expected):
    handle = zipfile.ZipFile(archive) if entry['kind'] == 'zip' else tarfile.open(archive, 'r:*')
    with handle:
        for member, name in entry['members'].items():
            row = expected[name]
            info = handle.getinfo(member) if entry['kind'] == 'zip' else handle.getmember(member)
            length = info.file_size if entry['kind'] == 'zip' else info.size
            if length != row['bytes'] or (entry['kind'] == 'tar' and not info.isfile()):
                raise ValueError('Wrong archive member size/type: ' + name)
            source = handle.open(info) if entry['kind'] == 'zip' else handle.extractfile(info)
            output = target / relative(name)
            output.parent.mkdir(parents=True, exist_ok=True)
            with source, output.open('xb') as dst:
                shutil.copyfileobj(source, dst, 1024 * 1024)
            if not verified(output, row):
                raise ValueError('Extracted member failed manifest: ' + name)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--repository', type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument('--target', type=Path, required=True)
    p.add_argument('--cache', type=Path, required=True)
    p.add_argument('--offline', action='store_true')
    args = p.parse_args()
    repo = args.repository.resolve(); overlay = repo / 'candidate-files'
    identity = json.loads((repo / 'candidate-identity.json').read_text(encoding='utf-8'))
    manifest = overlay / 'payload-sha256.json'
    if digest(manifest) != identity['manifest_sha256']:
        raise ValueError('Original candidate manifest changed')
    rows = json.loads(manifest.read_text(encoding='utf-8'))
    plan = json.loads((repo / 'download-plan.json').read_text(encoding='utf-8'))
    expected = validate_plan(rows, plan, overlay)
    if len(expected) != identity['expected_files']:
        raise ValueError('Original file count changed')
    target = args.target.resolve()
    if target.exists():
        raise FileExistsError('Use a fresh target; existing directory preserved')
    args.cache.mkdir(parents=True, exist_ok=True)
    shutil.copytree(overlay, target)
    def apply(entry):
        archive = download(entry, args.cache, args.offline)
        if entry['kind'] == 'file':
            dst = target / relative(entry['target']); dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(archive, dst)
        else:
            extract_members(archive, entry, target, expected)
        if not args.offline:
            # Fixed downloads are no longer needed after extraction on a 14GB runner.
            archive.unlink()
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(apply, plan))
    for name, row in expected.items():
        if not verified(target / relative(name), row):
            raise ValueError('Reconstructed candidate mismatch: ' + name)
    actual = {p.relative_to(target).as_posix() for p in target.rglob('*') if p.is_file()}
    if actual != set(expected) | {'payload-sha256.json'}:
        raise ValueError('Unexpected reconstructed file set')
    report = dict(status='passed',payload_files_verified=len(expected),
                  original_zip_extraction_tested=False,native_windows_execution_tested=False,
                  scope='Every frozen payload byte reconstructed; no install/model execution in this step')
    print(json.dumps(report))


if __name__ == '__main__':
    main()

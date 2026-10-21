# SPDX-License-Identifier: GPL-3.0-only
"""Collect only bounded, named installer logs from this fresh synthetic CI run."""
import argparse
import hashlib
import json
from pathlib import Path

NAMES = ('package-install.log', 'pip-check.log', 'native-imports.log', 'doctor.log',
         'native-startup.log', 'doctor-startup.log', 'install-state.json')
LIMIT = 2 * 1024 * 1024


def collect(bundle, result):
    result.mkdir(parents=True, exist_ok=True)
    target = result / 'installer-logs'
    target.mkdir(exist_ok=True)
    rows = []
    for name in NAMES:
        source = bundle / 'CoverLab' / 'runtime' / name
        if not source.is_file() or source.is_symlink():
            continue
        with source.open('rb') as stream:
            content = stream.read(LIMIT)
        saved = target / name
        saved.write_bytes(content)
        rows.append(dict(name=name, original_bytes=source.stat().st_size,
                         saved_bytes=len(content), truncated=source.stat().st_size > LIMIT,
                         saved_sha256=hashlib.sha256(content).hexdigest()))
    summary_path = result / 'acceptance' / 'summary.json'
    if summary_path.is_file():
        summary = json.loads(summary_path.read_text(encoding='utf-8'))
        if summary.get('status') == 'running':
            summary.update(status='incomplete', termination_confirmed=False,
                           reason='Workflow did not reach a terminal acceptance result; no pass inferred')
            for step in summary.get('steps', []):
                if step.get('status') == 'running':
                    step.update(status='interrupted', return_code=None)
            summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    report = dict(status='collected', installer_logs=rows,
                  native_acceptance_summary_present=summary_path.is_file())
    (result/'evidence-collection.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--result', type=Path, required=True)
    args = parser.parse_args()
    collect(args.bundle.resolve(), args.result.resolve())

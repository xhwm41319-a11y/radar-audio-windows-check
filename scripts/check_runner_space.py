# SPDX-License-Identifier: GPL-3.0-only
"""Fail explicitly when the fresh runner cannot retain the product's disk floor."""
import argparse
import json
from pathlib import Path
import shutil
import sys

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--path', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    rows = json.loads((repo/'candidate-files/payload-sha256.json').read_text(encoding='utf-8'))
    payload = sum(row['bytes'] for row in rows)
    required = payload + 10 * 1024**3 + 512 * 1024**2
    available = shutil.disk_usage(args.path).free
    report = dict(status='sufficient' if available >= required else 'insufficient',
                  available_bytes=available, required_before_reconstruction_bytes=required,
                  frozen_payload_bytes=payload, required_after_reconstruction_bytes=10 * 1024**3,
                  additional_margin_bytes=512 * 1024**2,
                  native_acceptance_executed=False)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report))
    sys.exit(0 if available >= required else 4)

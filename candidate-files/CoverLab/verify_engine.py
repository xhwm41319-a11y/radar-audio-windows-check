# SPDX-License-Identifier: GPL-3.0-only
"""Verify selected engine source bytes before importing or running that engine."""
from pathlib import Path, PurePosixPath
import hashlib, json, re

def verify_engine(package, engine, expected_revision):
    manifest=json.loads((Path(package)/'engine-code-sha256.json').read_text())
    if manifest['revision'] != expected_revision:
        raise ValueError('Engine revision declaration does not match the accepted manifest')
    rows=manifest['files']
    if not isinstance(rows,list) or not rows or len(rows)>1024:
        raise ValueError('Invalid engine source manifest')
    failures=[]
    for row in rows:
        name=PurePosixPath(row['path'])
        if name.is_absolute() or '..' in name.parts or not re.fullmatch('[0-9a-f]{64}',row['sha256']):
            raise ValueError('Invalid engine source entry')
        try:
            digest=hashlib.sha256((Path(engine)/str(name)).read_bytes()).hexdigest()
            if digest != row['sha256']: failures.append(str(name))
        except OSError: failures.append(str(name))
    if failures: raise ValueError('Engine source mismatch: '+', '.join(failures))
    return {'revision':manifest['revision'],'verified_files':len(rows),'scope':manifest['scope']}

# SPDX-License-Identifier: GPL-3.0-only
"""Compare local model bytes with the previously accepted local reference manifest."""
from pathlib import Path,PurePosixPath
import hashlib,json,re,sys
from runtime_config import load_installation

def verify(root):
    root=Path(root);config=load_installation(root);rows=json.loads((root/'model-file-sha256.json').read_text());results=[]
    if not isinstance(rows,list) or not rows or len(rows)>256:raise ValueError('Invalid model inventory')
    for row in rows:
        section=row['section'];name=PurePosixPath(row['path']);expected=row['sha256']
        if section not in ('voice','separation') or name.is_absolute() or '..' in name.parts or not re.fullmatch('[0-9a-f]{64}',expected):raise ValueError('Invalid model inventory entry')
        folder=Path(config['models' if section=='voice' else 'separation_models']);path=folder/str(name);h=hashlib.sha256();error=None
        try:
            if path.stat().st_size!=row['bytes']:raise ValueError('size mismatch')
            with path.open('rb') as f:
                for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
            if h.hexdigest()!=expected:raise ValueError('SHA256 mismatch')
        except (OSError,ValueError) as exc:error=str(exc)
        results.append({'section':section,'path':str(name),'matches':error is None,'error':error})
    return {'scope':'Previously accepted local bytes; not publisher signatures or licensing validation','all_match':all(x['matches'] for x in results),'files':results}
if __name__=='__main__':
    result=verify(Path(__file__).resolve().parent);print(json.dumps(result,ensure_ascii=False,indent=2));sys.exit(0 if result['all_match'] else 2)

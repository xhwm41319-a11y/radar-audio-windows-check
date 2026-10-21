# SPDX-License-Identifier: GPL-3.0-only
"""Select an already installed runtime; never install or execute dependencies."""
from pathlib import Path
import argparse,json,os,uuid
from runtime_config import PATH_KEYS

def configure(package,runtime,replace=False):
    package=Path(package).resolve();runtime=Path(runtime).expanduser().resolve()
    layout={'engine_root':runtime/'seed-vc','voice_python':runtime/('python/python.exe' if os.name=='nt' else 'voice-env/bin/python'),'models':runtime/'voice-models','separation_python':runtime/('python/python.exe' if os.name=='nt' else 'separation-env/bin/python'),'separation_models':Path(__file__).resolve().parent/'models/separation'}
    for key,p in layout.items():
        if key.endswith('_python'):
            if not p.is_file() or not os.access(p,os.X_OK):raise ValueError('Missing executable: '+str(p))
        elif not p.is_dir():raise ValueError('Missing directory: '+str(p))
    if not (layout['engine_root']/'inference.py').is_file():raise ValueError('Missing inference.py')
    target=package/'installation.local.json'
    if target.exists() and not replace:raise FileExistsError('Existing local configuration retained; use --replace intentionally')
    payload=json.dumps({k:str(layout[k]) for k in PATH_KEYS},ensure_ascii=False,indent=2)+'\n'
    temporary=package/('installation-'+uuid.uuid4().hex+'.pending')
    try:
        with temporary.open('x') as f:f.write(payload)
        if replace:temporary.replace(target)
        else:os.link(temporary,target) # Never overwrite a concurrently created configuration.
    finally:temporary.unlink(missing_ok=True)
    return target

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--runtime-root',type=Path,required=True);p.add_argument('--replace',action='store_true');args=p.parse_args()
    print(configure(Path(__file__).resolve().parent,args.runtime_root,args.replace))
if __name__=='__main__':main()

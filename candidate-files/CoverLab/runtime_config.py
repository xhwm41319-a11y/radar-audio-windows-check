# SPDX-License-Identifier: GPL-3.0-only
"""Resolve one trusted local runtime layout, without downloading or importing models."""
from pathlib import Path
import json,os

PATH_KEYS = ('engine_root','voice_python','models','separation_python','separation_models')

def load_installation(root):
    root=Path(root).resolve()
    local=root/'installation.local.json'
    path=local if local.exists() else root/'installation.json'
    if path.stat().st_size>65536:raise ValueError('Runtime configuration is too large')
    value=json.loads(path.read_text())
    if not isinstance(value,dict) or set(value)!=set(PATH_KEYS):raise ValueError('Runtime configuration must contain exactly the five documented paths')
    result={}
    for key in PATH_KEYS:
        item=value[key]
        if not isinstance(item,str) or not item or len(item)>4096 or any(ord(c)<32 for c in item):raise ValueError('Invalid runtime path: '+key)
        p=Path(item).expanduser()
        result[key]=os.path.abspath(root/p if not p.is_absolute() else p)
    result['separation_worker']=str(root/'workers/separate.py')
    result['mix_worker']=str(root/'workers/mix.py')
    return result

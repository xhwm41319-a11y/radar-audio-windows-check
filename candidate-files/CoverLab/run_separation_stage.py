# SPDX-License-Identifier: GPL-3.0-only
"""Run the package-local separation integration with network access disabled."""
import json,os,runpy,socket,sys
from pathlib import Path
req=Path(sys.argv[1]);config=json.loads(req.read_text());cache=req.parent/'cache'
os.environ.update(PYTHONDONTWRITEBYTECODE='1',MPLCONFIGDIR=str(cache/'mpl'),XDG_CACHE_HOME=str(cache),TORCH_HOME=str(cache/'torch'),HF_HOME=str(cache/'hf'),NUMBA_CACHE_DIR=str(cache/'numba'),HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',HF_HUB_DISABLE_TELEMETRY='1',OMP_NUM_THREADS='4',MKL_NUM_THREADS='4')
def deny(*args,**kwargs):raise RuntimeError('Network disabled during separation')
socket.socket.connect=deny;socket.socket.connect_ex=deny
import torch
import numpy as np
np.random.seed(42);torch.manual_seed(42);torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)));torch.set_num_interop_threads(max(1, min(2, os.cpu_count() or 1)))
torch.backends.mps.is_available=lambda:False;torch.cuda.is_available=lambda:False
worker=Path(config['worker']);sys.argv=[str(worker),str(req)];runpy.run_path(str(worker),run_name='__main__')

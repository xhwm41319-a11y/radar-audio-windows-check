# SPDX-License-Identifier: GPL-3.0-only
"""FCPE 0.0.4 adapter: local bundled MIT model, native V/UV, 10ms Hz."""
from pathlib import Path
import os
import sys
import time
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parent/'vendor'))
_MODELS={}
LAST_RUN={}

def extract_fcpe(audio, thred=None):
    # RMVPE's `thred` is a different quantity and is intentionally not mapped.
    import torch
    from torchfcpe import spawn_bundled_infer_model
    if hasattr(audio,'detach'):
        audio=audio.detach().float().cpu().numpy()
    audio=np.asarray(audio,dtype=np.float32)
    if audio.ndim!=1 or not len(audio) or not np.isfinite(audio).all():
        raise ValueError('Expected finite mono 16kHz audio')
    device=os.environ.get('RADAR_FCPE_DEVICE','cpu')
    if device not in ('cpu','mps') or (device=='mps' and not torch.backends.mps.is_available()):
        raise ValueError('Requested FCPE device is unavailable')
    started=time.perf_counter()
    if device not in _MODELS:
        _MODELS[device]=spawn_bundled_infer_model(device=device)
    model=_MODELS[device]
    assert str(model.tensor_device_marker.device)==device
    wave=torch.from_numpy(np.array(audio,copy=True)).reshape(1,-1,1).to(device)
    with torch.inference_mode():
        f0=model.infer(wave,sr=16000,decoder_mode='local_argmax',threshold=0.006,
                      interp_uv=False,output_interp_target_length=len(audio)//160+1)
    result=f0[0,:,0].detach().float().cpu().numpy()
    if result.ndim!=1 or not np.isfinite(result).all() or (result<0).any():
        raise ValueError('FCPE returned invalid F0')
    LAST_RUN.update({'model':'fcpe_c_v001.pt','package':'torchfcpe0.0.4','device':device,
                     'decoder':'local_argmax','threshold':0.006,'interp_uv':False,
                     'frequency_bounds':'native model range; no post-clipping',
                     'target_hop_samples':160,'target_sample_rate':16000,
                     'input_samples':len(audio),'frames':len(result),
                     'elapsed_seconds':time.perf_counter()-started,
                     'model_sample_rate':model.get_model_sr(),'model_hop_size':model.get_hop_size()})
    return result

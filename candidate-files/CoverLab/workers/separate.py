# SPDX-License-Identifier: MIT
"""Offline wideband candidate. Independent NumPy DSP following Deezer's
2stems STFT/mask contract; uses unchanged sherpa-onnx converted model graphs.
Not a six-stem replacement or a certified TensorFlow-equivalent implementation.
"""
from pathlib import Path
import hashlib,json,math,os,socket,sys,time
os.environ['ORT_DISABLE_TELEMETRY']='1'
os.environ['PYTHONDONTWRITEBYTECODE']='1'
def deny(*a,**kw):raise RuntimeError('Network disabled during separation')
socket.socket.connect=deny;socket.socket.connect_ex=deny
import numpy as np
import soundfile as sf
from scipy.fft import rfft,irfft
from scipy.signal import resample_poly
import onnxruntime as ort
ort.disable_telemetry_events()
RATE,NFFT,HOP,BINS,BLOCK=44100,4096,1024,2048,512
WINDOW=(.5-.5*np.cos(2*np.pi*np.arange(NFFT)/NFFT)).astype(np.float32)
MODEL_HASHES={'vocals':'bdc16ab6bf6117ddd4842c19e80e40e2be188fc555295064d424616b0224ac97','accompaniment':'671ace17acd3720674a2bc14de32ac6292453dec20d9eb0ba4255d4ad8e3d8c0'}

def stft(x):
    # Official model prepends NFFT zeros then TF stft(pad_end=True).
    y=np.pad(x,((NFFT,0),(0,0)))
    frames=(len(y)+HOP-1)//HOP
    y=np.pad(y,((0,(frames-1)*HOP+NFFT-len(y)),(0,0)))
    f=np.lib.stride_tricks.sliding_window_view(y,NFFT,axis=0)[::HOP]
    return rfft(f*WINDOW,axis=-1).transpose(1,0,2).astype(np.complex64)

def inverse(spec,length):
    # TF inverse_stft with Hann and fixed 2/3 OLA compensation, then official crop.
    f=irfft(spec,n=NFFT,axis=-1)*WINDOW
    y=np.zeros(((spec.shape[1]-1)*HOP+NFFT,2),dtype=np.float32)
    for i in range(spec.shape[1]):y[i*HOP:i*HOP+NFFT]+=f[:,i,:].T*(2/3)
    return y[NFFT:NFFT+length]

def load_torch_models(model_dir):
    import torch,onnx
    from onnx import numpy_helper
    from unet import UNet
    torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
    models={};audits={}
    for name,digest in MODEL_HASHES.items():
        path=Path(model_dir)/(name+'.onnx')
        if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise ValueError('Wrong frozen model bytes')
        graph=onnx.load(str(path),load_external_data=False)
        if graph.functions or any(t.external_data for t in graph.graph.initializer):raise ValueError('External model data/functions not accepted')
        tensors={t.name:numpy_helper.to_array(t).copy() for t in graph.graph.initializer}
        aliases={n.output[0]:n.input[0] for n in graph.graph.node if n.op_type=='Identity'}
        def resolve(key):
            seen=set()
            while key in aliases:
                if key in seen:raise ValueError('Cyclic tensor alias')
                seen.add(key);key=aliases[key]
            return key
        network=UNet().eval();state=network.state_dict();mapped={};used=[];generated=[]
        for key,expected in state.items():
            if key.endswith('.num_batches_tracked'):
                # Eval-only bookkeeping is not an inference parameter.
                mapped[key]=torch.zeros_like(expected);generated.append(key);continue
            source=resolve(key)
            if source not in tensors:raise ValueError('Missing inference tensor '+key)
            value=torch.from_numpy(tensors[source])
            if value.shape!=expected.shape or value.dtype!=expected.dtype:raise ValueError('Tensor contract mismatch '+key)
            mapped[key]=value;used.append({'target':key,'source':source})
        network.load_state_dict(mapped,strict=True)
        models[name]=network
        audits[name]={'tensors':used,'non_inference_bookkeeping_generated_zero':generated,'strict_loaded':True,'weights_retrained':False}
    return models,audits

class TorchSession:
    def __init__(self,model):self.model=model
    def run(self,outputs,feed):
        import torch
        with torch.inference_mode():return [self.model(torch.from_numpy(feed['x'])).numpy()]

def sessions(model_dir):
    models,audits=load_torch_models(model_dir)
    return {n:TorchSession(m) for n,m in models.items()},audits

def separate(req):
    global BINS
    BINS=req.get('frequency_bins',2048)
    if BINS not in (1024,1536,2048):raise ValueError('Unsupported frequency-bin configuration')
    started=time.perf_counter();src=Path(req['input_path']).resolve(strict=True);out=Path(req['output_dir']).absolute()
    if out.exists() and any(out.iterdir()):raise FileExistsError('Use empty output directory')
    out.mkdir(parents=True,exist_ok=True)
    x,rate=sf.read(src,dtype='float32',always_2d=True)
    if x.shape[1] not in (1,2) or not len(x) or not np.isfinite(x).all():raise ValueError('Finite mono/stereo input required')
    if len(x)/rate>600:raise ValueError('Candidate limited to 10 minutes')
    if x.shape[1]==1:x=np.repeat(x,2,axis=1)
    original_samples=len(x)
    if rate!=RATE:
        gcd=math.gcd(rate,RATE);x=resample_poly(x,RATE//gcd,rate//gcd).astype(np.float32)
    sess,metadata=sessions(req['models']);spec=stft(x);frames=spec.shape[1];chunks=(frames+BLOCK-1)//BLOCK
    mags=np.pad(abs(spec[:,:,:BINS]),((0,0),(0,chunks*BLOCK-frames),(0,0)))
    # One network block at a time to bound activation memory for long tracks.
    masks={n:np.empty((2,frames,BINS),np.float32) for n in sess}
    for k in range(chunks):
        a=k*BLOCK;b=min(a+BLOCK,frames);network_input=mags[:,a:a+BLOCK,:][:,None,:,:].astype(np.float32)
        estimates={n:s.run(['y'],{'x':network_input})[0][:,0,:b-a,:] for n,s in sess.items()}
        if any(v.shape!=(2,b-a,BINS) or not np.isfinite(v).all() or np.min(v)<0 for v in estimates.values()):raise ValueError('Invalid spectral estimate')
        denom=sum(v*v for v in estimates.values())+1e-10
        for n,v in estimates.items():masks[n][:,a:b,:]=(v*v+5e-11)/denom
        print('STUDIO_EVENT '+json.dumps({'stage':'Spleeter两轨分离','progress':(k+1)/chunks}),flush=True)
    tracks={};files=[];waves={}
    for name,mask in masks.items():
        masked=np.zeros_like(spec);masked[:,:,:BINS]=mask*spec[:,:,:BINS]
        wave=inverse(masked,len(x));waves[name]=wave
        path=out/('instrumental.wav' if name=='accompaniment' else 'vocals.wav');sf.write(path,wave,RATE,subtype='FLOAT');files.append(str(path))
        tracks[name]={'file':str(path),'duration':len(wave)/RATE,'rms':float(np.sqrt(np.mean(wave.astype(np.float64)**2))),'peak':float(abs(wave).max())}
    low=np.zeros_like(spec);low[:,:,:BINS]=spec[:,:,:BINS]
    expected=inverse(low,len(x));err=waves['vocals']+waves['accompaniment']-expected
    report={'files':files,'stems':tracks,'instrumental':tracks['accompaniment']['file'],'engine':'Spleeter 2stems / frozen ONNX tensors -> PyTorch wideband candidate','device':'cpu','elapsed_seconds':time.perf_counter()-started,'input_samples':original_samples,'input_rate':rate,'output_samples':len(x),'output_rate':RATE,'frequency_bins':BINS,'weight_mapping':metadata,'sample_rate_metadata_overridden_reason':'Export source says 41000; official 2stems config and inference say 44100','python_socket_outbound_blocked':True,'six_stem_equivalent':False,'TF_numeric_parity_verified':False,'lowpass_cutoff_hz':BINS*RATE/NFFT,'highest_included_bin_hz':(BINS-1)*RATE/NFFT,'nyquist_bin_included':False,'sum_vs_bandlimited_input_max_abs':float(abs(err).max()),'sum_vs_bandlimited_input_rms':float(np.sqrt(np.mean(err.astype(np.float64)**2))),'quality_note':f'F{BINS} retains bins 0..{BINS-1}; band boundary {BINS*RATE/NFFT:g} Hz, excludes Nyquist. Same model trained only to 11kHz; high-band attribution may be wrong. No human quality approval.'}
    (out/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n');return report

if __name__=='__main__':
    req=json.loads(Path(sys.argv[1]).read_text());print(json.dumps(separate(req),ensure_ascii=False),flush=True)

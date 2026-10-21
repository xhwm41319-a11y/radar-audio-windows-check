# SPDX-License-Identifier: GPL-3.0-only
"""One job: estimate stems, convert vocal, and mix against estimated accompaniment.
Child processes intentionally inherit the job process group for cancellation.
"""
from pathlib import Path
import json,os,shutil,subprocess,sys,time
ROOT=Path(__file__).resolve().parent

def main():
 request_path=Path(sys.argv[1]);req=json.loads(request_path.read_text());out=Path(req['output_dir']);out.mkdir(parents=True,exist_ok=True)
 if (out/'result.json').exists() or list(out.glob('vc_*.wav')):raise FileExistsError('Use a new mixed job output directory')
 install=req['installation'];timings={};start=time.monotonic()
 def stage(name,python,runner,config):
  status=request_path.parent/'stage.json';temp=status.with_suffix('.tmp');temp.write_text(json.dumps({'stage':name}));temp.replace(status)
  fp=out/(name+'-request.json');fp.write_text(json.dumps(config,ensure_ascii=False,indent=2));t=time.monotonic()
  with (out/(name+'.log')).open('wb') as log:
   z=subprocess.run([str(python),'-u',str(runner),str(fp)],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,env=os.environ.copy())
  timings[name]=round(time.monotonic()-t,3)
  if z.returncode:raise RuntimeError(name+' failed; see job stage log')
 stage('separating',install['separation_python'],ROOT/'run_separation_stage.py',{'input_path':req['input_path'],'models':install['separation_models'],'worker':install['separation_worker'],'output_dir':str(out/'stems')})
 sep=json.loads((out/'stems/result.json').read_text());v=dict(req);v.update(input_path=sep['stems']['vocals']['file'],output_dir=str(out/'converted'));v['params']=dict(req['params'],test_type='mixed_audio_web_job')
 stage('converting',sys.executable,ROOT/'run_voice.py',v)
 converted=json.loads((out/'converted/result.json').read_text());paths=list((out/'converted').glob('vc_*.wav'))
 if len(paths)!=1:raise RuntimeError('Expected one converted vocal')
 stage('mixing',sys.executable,install['mix_worker'],{'vocal_path':str(paths[0]),'instrumental_path':sep['instrumental'],'output_dir':str(out/'mixed'),'ffmpeg':shutil.which('ffmpeg'),'params':{'vocal_db':0,'instrumental_db':0,'highpass_hz':0,'presence_db':0,'compressor':False,'reverb':0,'target_lufs':-19}})
 target=out/'vc_mixed.wav';shutil.copyfile(out/'mixed/mix-master.wav',target)
 report={'files':[str(target)],'elapsed_seconds':round(time.monotonic()-start,3),'device':converted.get('device','unknown'),'separation_device':'cpu','device_selection':converted.get('device_selection',{}),'stage_wall_seconds':timings,'input_mode':'mixed','engine':str(sep.get('engine','Unknown separation'))+' -> Seed-VC -> FFmpeg mix','separation_frequency_bins':sep.get('frequency_bins'),'six_stem_equivalent':False,'human_quality_approved':False,'reference_identity_similarity_verified':False,'vendor_fidelity_percent':None,'separation_estimates_not_original_stems':True,'underlying_vocal_conversion':converted.get('elapsed_seconds')}
 (out/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));status=request_path.parent/'stage.json';temp=status.with_suffix('.tmp');temp.write_text(json.dumps({'stage':'finalizing'}));temp.replace(status);print(json.dumps(report),flush=True)
if __name__=='__main__':main()

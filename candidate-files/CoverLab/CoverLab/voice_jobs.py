# SPDX-License-Identifier: GPL-3.0-only
"""Single local conversion with a duration budget and cooperative user cancellation."""
from pathlib import Path
import json, math, os, shutil, signal, subprocess, threading, time, uuid, sys
from core import AudioError, probe_audio
from job_history import load_history
from process_tree import launch, TerminationUnknown
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from runtime_config import load_installation
INSTALLATION = load_installation(ROOT)
CONFIG = dict(json.loads((ROOT / 'request.json').read_text()),engine_root=INSTALLATION['engine_root'],models=INSTALLATION['models'])
PYTHON = Path(INSTALLATION['voice_python'])
RUNNER = ROOT / 'run_voice.py'
MIXED_RUNNER = ROOT / 'run_mixed.py'
MIXED_CONFIG = ROOT / 'installation.json'
MAX_SOURCE_SECONDS = 600

def time_budget(seconds):
    # Conservative stop budget, NOT a promised completion estimate.
    return min(7200, max(300, math.ceil(120 + 9 * seconds)))

def write_job(path, data):
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2))
    temporary.replace(path)

class Cancelled(Exception): pass

class VoiceJobs:
    def __init__(self, workspace):
        self.workspace=workspace; self.controls={}; self.lock=threading.RLock()
        self.jobs,self.reuse_blocked=load_history(workspace)
        if self.reuse_blocked:self.workspace.process_lock.acquire(blocking=False)
    def available(self):
        return PYTHON.is_file() and RUNNER.is_file() and Path(CONFIG['models']).is_dir()
    def mixed_available(self):
        if os.name not in ('posix','nt') or not self.available() or not MIXED_RUNNER.is_file() or not MIXED_CONFIG.is_file():return False
        try:
            config=INSTALLATION
            return all(Path(config[k]).exists() for k in ('separation_python','separation_worker','separation_models','mix_worker'))
        except (OSError,KeyError,ValueError):return False
    def get(self, job_id):
        with self.lock:
            if job_id not in self.jobs: raise AudioError('Voice job not found in this server session',404)
            result=json.loads(json.dumps(self.jobs[job_id]))
            if result['status'] in ('running','cancelling'):
                result['wall_elapsed_seconds']=round(time.monotonic()-self.controls[job_id]['started'],1)
                try:result['stage']=json.loads((self.controls[job_id]['folder']/'stage.json').read_text())['stage']
                except (OSError,ValueError,KeyError):pass
            return result
    def recent(self):
        with self.lock: return {'jobs':[self.get(k) for k in list(self.jobs)[-20:]][::-1],'engine_reuse_blocked':self.reuse_blocked}
    def cancel(self, job_id):
        with self.lock:
            self.get(job_id)
            if self.jobs[job_id]['status'] in ('running','cancelling'):
                self.controls[job_id]['cancel'].set(); self.jobs[job_id]['status']='cancelling'
            return self.get(job_id)
    def start(self, request):
        if not isinstance(request,dict): raise AudioError('Expected a JSON object')
        if self.reuse_blocked:raise AudioError('Previous engine state is unknown; manual recovery required before processing',409)
        if not self.available(): raise AudioError('Local Seed-VC installation unavailable',503)
        input_mode=request.get('input_mode','dry')
        if input_mode not in ('dry','mixed'):raise AudioError('Select dry or mixed input')
        processing_mode=request.get('processing_mode','auto')
        if processing_mode not in ('auto','cpu','gpu'):raise AudioError('Select automatic, CPU, or GPU processing')
        if input_mode=='mixed' and not self.mixed_available():raise AudioError('Mixed audio pipeline unavailable',503)
        source,sr=self.workspace.file(request.get('source_id')); reference,rr=self.workspace.file(request.get('reference_id'))
        steps=request.get('steps',30)
        if type(steps) is not int or steps not in (8,30): raise AudioError('Select 8 or 30 steps')
        duration=sr['duration'];ref_duration=rr['duration']
        if not math.isfinite(duration) or not 1<=duration<=MAX_SOURCE_SECONDS: raise AudioError(f'Input audio must be 1–{MAX_SOURCE_SECONDS} seconds')
        if not math.isfinite(ref_duration) or not 3<=ref_duration<=15: raise AudioError('Reference must be 3–15 seconds')
        if not self.workspace.process_lock.acquire(blocking=False): raise AudioError('Another audio operation is running',409)
        job_id=uuid.uuid4().hex;folder=self.workspace.data/'voice-runs'/job_id
        try:
            folder.mkdir(parents=True,exist_ok=False)
            config=dict(CONFIG);config.update(installation=dict(INSTALLATION),input_path=str(source),reference_path=str(reference),output_dir=str(folder/'engine'),processing_mode=processing_mode,params={'steps':steps,'semitones':0,'test_type':'user_local_audio'},reference_provenance={'kind':'User-selected local audio; rights not independently verified'},seed=42)
            (folder/'request.json').write_text(json.dumps(config,ensure_ascii=False,indent=2))
            with self.lock:
                self.controls[job_id]={'cancel':threading.Event(),'started':time.monotonic(),'folder':folder}
                self.jobs[job_id]={'id':job_id,'status':'running','steps':steps,'source_seconds':duration,'timeout_seconds':min(7200,max(360,math.ceil(180+12*duration))) if input_mode=='mixed' else time_budget(duration),'source_name':sr['name'],'source_id':sr['id'],'reference_id':rr['id'],'input_mode':input_mode,'processing_mode':processing_mode,'stage':'queued'}
                write_job(folder/'job.json',self.jobs[job_id])
                worker=threading.Thread(target=self._run,args=(job_id,folder,sr['name']),daemon=True)
                self.controls[job_id]['thread']=worker;worker.start()
            return self.get(job_id)
        except Exception:
            with self.lock:
                if job_id in self.jobs:
                    self.jobs[job_id].update(status='failed',error='Worker could not start')
                    try:write_job(folder/'job.json',self.jobs[job_id])
                    except OSError:pass
            self.workspace.process_lock.release();raise
    @staticmethod
    def _stop(proc):
        if hasattr(proc,'stop'):
            proc.stop()
            return
        if os.name!='posix':
            if proc.poll() is None:
                proc.terminate()
                try:proc.wait(timeout=5)
                except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
            return
        def group_alive():
            try:os.killpg(proc.pid,0);return True
            except ProcessLookupError:return False
        if group_alive():
            try:os.killpg(proc.pid,signal.SIGTERM)
            except ProcessLookupError:pass
        try:proc.wait(timeout=5)
        except subprocess.TimeoutExpired:pass
        if group_alive():
            try:os.killpg(proc.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            proc.wait(timeout=5)
            deadline=time.monotonic()+2
            while group_alive() and time.monotonic()<deadline:time.sleep(.05)
            if group_alive():raise RuntimeError('Engine process group remains; restarting conversions is gated')

    def _run(self,job_id,folder,source_name):
        proc=None;target=None;update={'status':'failed','error':'Worker interrupted'}
        control=self.controls[job_id];budget=self.jobs[job_id]['timeout_seconds']
        try:
            if control['cancel'].is_set():raise Cancelled()
            env=os.environ.copy();cache=str(folder/'cache')
            env.update(PYTHONUTF8='1',PYTHONDONTWRITEBYTECODE='1',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',HF_HUB_DISABLE_TELEMETRY='1',NUMBA_CACHE_DIR=cache+'/numba',MPLCONFIGDIR=cache+'/mpl',XDG_CACHE_HOME=cache,OMP_NUM_THREADS='4',OPENBLAS_NUM_THREADS='4')
            with (folder/'inference.log').open('wb') as log:
                runner=MIXED_RUNNER if self.jobs[job_id]['input_mode']=='mixed' else RUNNER
                proc=launch([str(PYTHON),'-u',str(runner),str(folder/'request.json')],gate_python=PYTHON,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,env=env,start_new_session=os.name=='posix')
                while proc.poll() is None:
                    if control['cancel'].wait(.2):raise Cancelled()
                    if time.monotonic()-control['started']>budget:raise TimeoutError()
                if control['cancel'].is_set():raise Cancelled()
                if time.monotonic()-control['started']>budget:raise TimeoutError()
                if proc.returncode:raise AudioError('Engine failed; see this local job’s inference.log',422)
            self._stop(proc)
            proc=None
            report=json.loads((folder/'engine/result.json').read_text());outputs=list((folder/'engine').glob('vc_*.wav'))
            if len(outputs)!=1:raise AudioError('Expected one new engine output',422)
            target=self.workspace.outputs/f'{uuid.uuid4().hex}.wav';shutil.copyfile(outputs[0],target);metadata=probe_audio(target)
            expected=self.jobs[job_id]['source_seconds']
            if abs(metadata['duration']-expected)>max(.1,expected*.03):raise AudioError('Output length does not match input',422)
            with self.lock:
                if control['cancel'].is_set():raise Cancelled()
                if time.monotonic()-control['started']>budget:raise TimeoutError()
                with self.workspace.lock:public=self.workspace._add(target,Path(source_name).stem+('-MixedCover.wav' if self.jobs[job_id]['input_mode']=='mixed' else '-SeedVC.wav'),'outputs',metadata)
                update={'status':'completed','file':public,'elapsed_seconds':report['elapsed_seconds'],'device':report['device'],'stage':'completed','stage_wall_seconds':report.get('stage_wall_seconds',{}),'input_mode':self.jobs[job_id]['input_mode']}
                self.jobs[job_id].update(update) # Publication commit: later cancellation cannot retract a completed file.
                target=None
        except Cancelled:update={'status':'cancelled','error':'User cancelled; no output published'}
        except TimeoutError:update={'status':'failed','error':f'Conversion exceeded {budget} seconds and was stopped'}
        except TerminationUnknown as exc:
            self.reuse_blocked=True
            update={'status':'failed','error':str(exc),'engine_reuse_blocked':True}
        except Exception as exc:update={'status':'failed','error':str(exc)}
        finally:
            termination_verified=not self.reuse_blocked
            try:
                if proc is not None:self._stop(proc)
            except Exception as exc:
                termination_verified=False
                self.reuse_blocked=True
                update={'status':'failed','error':'Could not verify engine termination: '+str(exc),'engine_reuse_blocked':True}
            try:
                if target is not None:
                    try:target.unlink(missing_ok=True)
                    except OSError as exc:update['cleanup_error']=str(exc)
                with self.lock:
                    self.jobs[job_id].update(update)
                    self.jobs[job_id]['wall_elapsed_seconds']=round(time.monotonic()-control['started'],1)
                    try:write_job(folder/'job.json',self.jobs[job_id])
                    except OSError as exc:self.jobs[job_id]['persistence_error']=str(exc)
            finally:
                if termination_verified:self.workspace.process_lock.release()
    def close(self):
        with self.lock:
            controls=list(self.controls.values())
            for c in controls:c['cancel'].set()
        for c in controls:
            if c.get('thread') and c['thread'].ident is not None:c['thread'].join(timeout=12)
        return not any(c.get('thread') and c['thread'].is_alive() for c in controls)

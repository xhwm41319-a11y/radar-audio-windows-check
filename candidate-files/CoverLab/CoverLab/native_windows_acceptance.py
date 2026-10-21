"""Run on Windows with Python: native single-instance and Job Object acceptance."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def require(condition, description):
    if not condition:
        raise AssertionError(description)


def wait_for(predicate, description, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.05)
    raise AssertionError(description)


def run():
    from instance_lock import WorkspaceLease
    from process_tree import launch
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
    kernel.IsProcessInJob.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL

    def in_job(pid, job):
        handle = kernel.OpenProcess(0x1000, False, pid)
        require(handle, f'OpenProcess failed for own PID {pid}: {ctypes.get_last_error()}')
        try:
            answer = wintypes.BOOL()
            require(kernel.IsProcessInJob(handle, job, ctypes.byref(answer)),
                    f'IsProcessInJob failed: {ctypes.get_last_error()}')
            return bool(answer.value)
        finally:
            kernel.CloseHandle(handle)

    with tempfile.TemporaryDirectory(prefix='coverlab-native-acceptance-') as directory:
        root = Path(directory)
        env = dict(os.environ, PYTHONPATH=str(HERE))
        contender = ("import json,sys; from pathlib import Path; "
                     "from instance_lock import WorkspaceLease,WorkspaceInUse; "
                     "\ntry:\n with WorkspaceLease(Path(sys.argv[1])): print('ACQUIRED')"
                     "\nexcept WorkspaceInUse: print('BUSY')")
        def try_lock():
            return subprocess.check_output([sys.executable, '-c', contender, str(root)],
                                           env=env, text=True, timeout=8).strip()
        with WorkspaceLease(root):
            require(try_lock() == 'BUSY', 'second process acquired live workspace lock')
        require(try_lock() == 'ACQUIRED', 'workspace lock did not release')

        marker = root / 'grandchild-heartbeat'
        target_pid_file = root / 'target.pid'
        child_pid_file = root / 'child.pid'
        grandchild_pid_file = root / 'grandchild.pid'
        grandchild_code = ("import os,pathlib,sys,time; "
                           "pathlib.Path(sys.argv[2]).write_text(str(os.getpid())); "
                           "p=pathlib.Path(sys.argv[1]); "
                           "\nwhile True:\n p.open('a').write('x'); time.sleep(.03)")
        child_code = ("import os,pathlib,subprocess,sys; "
                      "pathlib.Path(sys.argv[3]).write_text(str(os.getpid())); "
                      "subprocess.call([sys.executable,'-u','-c',sys.argv[1],sys.argv[2],sys.argv[4]])")
        target_code = ("import os,pathlib,subprocess,sys; "
                       "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
                       "subprocess.call([sys.executable,'-u','-c',sys.argv[2],*sys.argv[3:]])")
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        job_process = None
        try:
            job_process = launch([sys.executable, '-u', '-c', target_code, str(target_pid_file),
                                  child_code, grandchild_code, str(marker), str(child_pid_file),
                                  str(grandchild_pid_file)], gate_python=sys.executable,
                                 cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=False)
            wait_for(lambda: all(p.exists() for p in (target_pid_file, child_pid_file,
                                                       grandchild_pid_file, marker)),
                     'gated target/child/grandchild did not start')
            wait_for(lambda: marker.stat().st_size >= 3, 'grandchild did not write heartbeat')
            pids = [job_process.pid, int(target_pid_file.read_text()),
                    int(child_pid_file.read_text()), int(grandchild_pid_file.read_text())]
            require(all(in_job(pid, job_process.job) for pid in pids),
                    'gate, target, child, or grandchild was outside Job Object')
            job_process.stop()  # Internally verifies ActiveProcesses == 0 before closing.
            require(job_process.job is None, 'Job Object did not confirm zero active processes')
            size = marker.stat().st_size
            time.sleep(.35)
            require(marker.stat().st_size == size, 'grandchild continued writing after stop')
            require(unrelated.poll() is None, 'unrelated control process was terminated')
            return {'status': 'passed', 'native_windows': True,
                    'checks': ['cross-process workspace lock', 'gate and three descendants bound to Job Object',
                               'ActiveProcesses=0', 'heartbeat stopped', 'unrelated process alive']}
        finally:
            if job_process is not None and job_process.job is not None:
                try:
                    job_process.stop()
                except Exception:
                    kernel.CloseHandle(job_process.job)  # Kill-on-close applies only to this script's job.
                    job_process.job = None
            if unrelated.poll() is None:
                unrelated.terminate()
            unrelated.wait(timeout=5)


if __name__ == '__main__':
    if os.name != 'nt':
        print(json.dumps({'status': 'skipped', 'native_windows': False,
                          'reason': 'Run this script on Windows; no native Windows checks ran.'}))
    else:
        try:
            print(json.dumps(run(), ensure_ascii=False))
        except Exception as exc:
            print(json.dumps({'status': 'failed', 'native_windows': True,
                              'error': f'{type(exc).__name__}: {exc}'}, ensure_ascii=False))
            sys.exit(1)

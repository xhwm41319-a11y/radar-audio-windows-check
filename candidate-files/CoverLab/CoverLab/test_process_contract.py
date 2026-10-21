"""Local process and lock contracts; run with python -m unittest test_process_contract.py."""
import ast
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from instance_lock import WorkspaceInUse, WorkspaceLease
from process_tree import launch


def _stop_from_voice_jobs():
    # Extract the real method without importing model configuration or audio dependencies.
    source = ast.parse((HERE / 'voice_jobs.py').read_text())
    owner = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == 'VoiceJobs')
    method = next(n for n in owner.body if isinstance(n, ast.FunctionDef) and n.name == '_stop')
    module = ast.fix_missing_locations(ast.Module(
        body=[ast.ClassDef(name='VoiceJobs', bases=[], keywords=[], body=[method], decorator_list=[])],
        type_ignores=[]))
    namespace = {'os': os, 'signal': signal, 'subprocess': subprocess, 'time': time}
    exec(compile(module, '<voice_jobs._stop>', 'exec'), namespace)
    return namespace['VoiceJobs']._stop


class ProcessContract(unittest.TestCase):
    def test_workspace_lease_blocks_second_process_and_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            code = ("from pathlib import Path; from instance_lock import WorkspaceLease, WorkspaceInUse; "
                    "import sys; p=Path(sys.argv[1]); "
                    "\ntry:\n with WorkspaceLease(p): print('ACQUIRED')"
                    "\nexcept WorkspaceInUse: print('BUSY')")
            env = dict(os.environ, PYTHONPATH=str(HERE))
            def attempt():
                return subprocess.check_output([sys.executable, '-c', code, directory], env=env, text=True).strip()
            with WorkspaceLease(Path(directory)):
                self.assertEqual(attempt(), 'BUSY')
            self.assertEqual(attempt(), 'ACQUIRED')

    @unittest.skipUnless(os.name == 'posix', 'POSIX symlink behavior')
    def test_workspace_lease_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / '.coverlab-instance.lock').symlink_to(Path(directory) / 'other')
            with self.assertRaises(OSError):
                with WorkspaceLease(Path(directory)):
                    pass

    def test_managed_stop_interface(self):
        class FakeWindowsJob:
            stopped = False
            def stop(self):
                self.stopped = True
        job = FakeWindowsJob()
        _stop_from_voice_jobs()(job)
        self.assertTrue(job.stopped)

    @unittest.skipUnless(os.name == 'posix', 'POSIX real process-group test')
    def test_stop_terminates_real_child_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'heartbeat'
            child_code = ("import pathlib,sys,time; p=pathlib.Path(sys.argv[1]); "
                          "\nwhile True:\n p.open('a').write('x'); time.sleep(.03)")
            parent_code = ("import subprocess,sys,time; "
                           "c=subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]); "
                           "print(c.pid,flush=True); time.sleep(60)")
            proc = launch([sys.executable, '-c', parent_code, child_code, str(marker)],
                          stdout=subprocess.PIPE, start_new_session=True)
            try:
                child_pid = int(proc.stdout.readline().decode())
                deadline = time.monotonic() + 3
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(marker.exists())
                _stop_from_voice_jobs()(proc)
                before = marker.stat().st_size
                time.sleep(.2)
                self.assertEqual(marker.stat().st_size, before)
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    pass
                else:
                    stat = subprocess.check_output(['ps', '-o', 'stat=', '-p', str(child_pid)], text=True).strip()
                    self.assertTrue(stat.startswith('Z'), stat)
            finally:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
                proc.stdout.close()


if __name__ == '__main__':
    unittest.main()

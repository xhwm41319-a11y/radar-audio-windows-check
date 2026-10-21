"""Keep every Windows conversion descendant inside one kill-on-close Job Object."""
import os
import subprocess
import time
from pathlib import Path


class TerminationUnknown(RuntimeError):
    """A worker may still be alive; do not start another conversion."""


if os.name == 'nt':
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _BasicLimit(ctypes.Structure):
        _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64),
                    ('PerJobUserTimeLimit', ctypes.c_int64),
                    ('LimitFlags', wintypes.DWORD),
                    ('MinimumWorkingSetSize', ctypes.c_size_t),
                    ('MaximumWorkingSetSize', ctypes.c_size_t),
                    ('ActiveProcessLimit', wintypes.DWORD),
                    ('Affinity', ctypes.c_size_t),
                    ('PriorityClass', wintypes.DWORD),
                    ('SchedulingClass', wintypes.DWORD)]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in
                    ('ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                     'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [('BasicLimitInformation', _BasicLimit), ('IoInfo', _IoCounters),
                    ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                    ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]

    class _Accounting(ctypes.Structure):
        _fields_ = [('TotalUserTime', ctypes.c_int64), ('TotalKernelTime', ctypes.c_int64),
                    ('ThisPeriodTotalUserTime', ctypes.c_int64), ('ThisPeriodTotalKernelTime', ctypes.c_int64),
                    ('TotalPageFaultCount', wintypes.DWORD), ('TotalProcesses', wintypes.DWORD),
                    ('ActiveProcesses', wintypes.DWORD), ('TotalTerminatedProcesses', wintypes.DWORD)]

    _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.QueryInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
                                                    wintypes.DWORD, wintypes.LPVOID)
    _kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL

    def _check(result, action):
        if not result:
            raise OSError(f'{action}: {ctypes.WinError(ctypes.get_last_error())}')

    class WindowsJobProcess:
        def __init__(self, proc, job):
            self.proc = proc
            self.job = job

        @property
        def pid(self):
            return self.proc.pid

        @property
        def returncode(self):
            return self.proc.returncode

        def poll(self):
            return self.proc.poll()

        def wait(self, timeout=None):
            return self.proc.wait(timeout=timeout)

        def stop(self):
            if self.job is None:
                return
            _check(_kernel32.TerminateJobObject(self.job, 1), 'TerminateJobObject')
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError('Job gate did not exit after termination') from exc
            deadline = time.monotonic() + 5
            while True:
                account = _Accounting()
                _check(_kernel32.QueryInformationJobObject(self.job, 1, ctypes.byref(account),
                                                           ctypes.sizeof(account), None), 'QueryInformationJobObject')
                if account.ActiveProcesses == 0:
                    _check(_kernel32.CloseHandle(self.job), 'CloseHandle(job)')
                    self.job = None
                    return
                if time.monotonic() >= deadline:
                    raise RuntimeError('Engine Job Object still has active processes; restarting conversions is gated')
                time.sleep(.05)

    def _launch_windows(args, **kwargs):
        gate_python = kwargs.pop('gate_python', args[0])
        job = _kernel32.CreateJobObjectW(None, None)
        _check(job, 'CreateJobObjectW')
        proc = None
        assigned = False
        job_closed = False
        read_fd = write_fd = None
        try:
            limits = _ExtendedLimit()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            _check(_kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits),
                                                     ctypes.sizeof(limits)), 'SetInformationJobObject')
            read_fd, write_fd = os.pipe()
            os.set_inheritable(read_fd, True)
            startup = subprocess.STARTUPINFO()
            startup.lpAttributeList = {'handle_list': [msvcrt.get_osfhandle(read_fd)]}
            proc = subprocess.Popen([str(gate_python), '-u', str(Path(__file__).with_name('process_gate.py')),
                                     str(msvcrt.get_osfhandle(read_fd)), *map(str, args)],
                                    startupinfo=startup, close_fds=True, **kwargs)
            os.close(read_fd)
            read_fd = None
            _check(_kernel32.AssignProcessToJobObject(job, int(proc._handle)), 'AssignProcessToJobObject')
            assigned = True
            os.write(write_fd, b'\x01')
            os.close(write_fd)
            write_fd = None
            return WindowsJobProcess(proc, job)
        except BaseException:
            if write_fd is not None:
                try:
                    os.close(write_fd)  # The gate must never launch the engine on failure.
                except OSError:
                    pass
            if read_fd is not None:
                try:
                    os.close(read_fd)
                except OSError:
                    pass
            if proc is not None:
                try:
                    if assigned:
                        WindowsJobProcess(proc, job).stop()
                        job_closed = True
                    else:
                        if proc.poll() is None:
                            proc.terminate()
                        proc.wait(timeout=5)
                except BaseException as exc:
                    # Keep the job handle open; the server's lock must also remain held.
                    _uncertain_jobs.append(job)
                    raise TerminationUnknown('Could not verify gated worker termination') from exc
            if not job_closed:
                _kernel32.CloseHandle(job)
            raise

    _uncertain_jobs = []


def launch(args, **kwargs):
    if os.name == 'nt':
        return _launch_windows(args, **kwargs)
    kwargs.pop('gate_python', None)
    return subprocess.Popen(args, **kwargs)

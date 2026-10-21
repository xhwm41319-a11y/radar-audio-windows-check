"""One cooperative server process per canonical workspace."""
from __future__ import annotations

import errno
import os
from pathlib import Path
import stat

if os.name == 'nt':
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _kernel32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.GetFileInformationByHandleEx.argtypes = (wintypes.HANDLE, ctypes.c_int,
                                                       wintypes.LPVOID, wintypes.DWORD)
    _kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL

    class _FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [('FileAttributes', wintypes.DWORD), ('ReparseTag', wintypes.DWORD)]

    def _open_exclusive_lock(path):
        # No sharing, including delete sharing: another instance cannot open or replace this inode.
        handle = _kernel32.CreateFileW(str(path), 0xC0000000, 0, None, 4, 0x00200080, None)
        if handle == ctypes.c_void_p(-1).value:
            code = ctypes.get_last_error()
            if code == 32:
                raise WorkspaceInUse('工作台已在此目录运行，请使用已打开的窗口；不会启动第二实例或终止已有任务。') from None
            raise ctypes.WinError(code)
        try:
            info = _FileAttributeTagInfo()
            if not _kernel32.GetFileInformationByHandleEx(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            if info.FileAttributes & (0x400 | 0x10):
                raise OSError('Workspace lock must be a regular file, not a reparse point or directory')
            return handle
        except BaseException:
            _kernel32.CloseHandle(handle)
            raise
else:
    import fcntl


class WorkspaceInUse(RuntimeError):
    pass


class WorkspaceLease:
    def __init__(self, root: Path):
        self.root = Path(root).resolve(strict=True)
        self.path = self.root / ".coverlab-instance.lock"
        self.fd = None
        self.handle = None
        self._retain = False

    def __enter__(self):
        if not self.root.is_dir():
            raise NotADirectoryError(self.root)
        if os.name == 'nt':
            self.handle = _open_exclusive_lock(self.path)
            return self
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("Workspace lock must be a regular file")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise WorkspaceInUse("工作台已在此目录运行，请使用已打开的窗口；不会启动第二实例或终止已有任务。") from None
                raise
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        return self

    def retain_until_process_exit(self):
        # If a worker's termination is unknown, never unlock while this process lives.
        self._retain = True

    def allow_release(self):
        self._retain = False

    def __exit__(self, *exc):
        if self.fd is not None and not self._retain:
            os.close(self.fd)
            self.fd = None
        if self.handle is not None and not self._retain:
            _kernel32.CloseHandle(self.handle)
            self.handle = None
        # Never unlink: removing a live lock permits a second inode/second owner.

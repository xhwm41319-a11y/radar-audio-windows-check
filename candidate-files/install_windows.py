# SPDX-License-Identifier: GPL-3.0-only
"""Install and start the fully offline Windows x64 CoverLab candidate."""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import hashlib
import json
import msvcrt
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid


BUNDLE = Path(__file__).resolve().parent
APP = BUNDLE / "CoverLab"
PAYLOAD = BUNDLE / "payload"
BOOTSTRAP = BUNDLE / ".bootstrap" / "python"
RUNTIME = APP / "runtime"
LOCK = APP / ".runtime-install.lock"
REQUIRED_ROOTS = (".bootstrap/python/", "payload/", "native-tools/bin/")
MAX_MANIFEST_ENTRIES = 20000


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def relative_path(value: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("Invalid manifest path")
    if value.startswith("/") or any(x in ("", ".", "..") for x in value.split("/")):
        raise ValueError("Invalid manifest path")
    part = PurePosixPath(value)
    if part.is_absolute() or any(x in ("", ".", "..") or x.rstrip(" .") != x for x in part.parts):
        raise ValueError("Invalid manifest path")
    if any(x.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", "COM1", "LPT1"} for x in part.parts):
        raise ValueError("Invalid Windows manifest path")
    return Path(*part.parts)


def regular_file(path: Path) -> bool:
    if linked(path):
        return False
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except OSError:
        return False


def linked(path: Path) -> bool:
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def verify_payload() -> int:
    manifest_path = BUNDLE / "payload-sha256.json"
    if not regular_file(manifest_path) or manifest_path.stat().st_size > 4_000_000:
        raise ValueError("Missing or invalid payload-sha256.json")
    rows = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not 0 < len(rows) <= MAX_MANIFEST_ENTRIES:
        raise ValueError("Invalid payload manifest")
    listed: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "bytes"}:
            raise ValueError("Invalid payload manifest entry")
        rel = relative_path(row["path"])
        key = str(rel).replace("\\", "/").casefold()
        if key in listed:
            raise ValueError("Duplicate payload path: " + row["path"])
        listed.add(key)
        if (type(row["bytes"]) is not int or row["bytes"] < 0
                or not isinstance(row["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"])):
            raise ValueError("Invalid payload size or SHA256: " + row["path"])
        path = BUNDLE / rel
        for parent in path.parents:
            if parent == BUNDLE:
                break
            if linked(parent):
                raise ValueError("Payload path crosses a link: " + row["path"])
        if not regular_file(path) or path.stat().st_size != row["bytes"] or digest(path) != row["sha256"]:
            raise ValueError("Payload mismatch: " + row["path"])
    for name in (".bootstrap/python/python.exe", "native-tools/bin/ffmpeg.exe",
                 "native-tools/bin/ffprobe.exe", "windows-requirements.lock"):
        if name.casefold() not in listed:
            raise ValueError("Required file absent from payload manifest: " + name)
    for root in REQUIRED_ROOTS:
        folder = BUNDLE / relative_path(root.rstrip("/"))
        if not folder.is_dir() or linked(folder):
            raise ValueError("Missing or linked payload directory: " + root)
        for path in folder.rglob("*"):
            if linked(path):
                raise ValueError("Linked payload member: " + str(path))
            if path.is_file() and path.relative_to(BUNDLE).as_posix().casefold() not in listed:
                raise ValueError("Unlisted payload file: " + str(path))
    return len(rows)


def native_machine() -> int:
    process_machine = ctypes.c_ushort()
    native = ctypes.c_ushort()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    func = kernel.IsWow64Process2
    func.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ushort), ctypes.POINTER(ctypes.c_ushort)]
    func.restype = ctypes.c_int
    if not func(kernel.GetCurrentProcess(), ctypes.byref(process_machine), ctypes.byref(native)):
        raise OSError(ctypes.get_last_error(), "IsWow64Process2 failed")
    return native.value


def check_host(require_space: bool = True) -> None:
    if sys.platform != "win32" or sys.version_info[:2] != (3, 12):
        raise RuntimeError("Requires bundled CPython 3.12 on Windows")
    version = sys.getwindowsversion()
    if version.major != 10 or version.build < 19041:
        raise RuntimeError("Windows 10 version 2004 or Windows 11 is required")
    if native_machine() != 0x8664:
        raise RuntimeError("Native x64 Windows is required; ARM64 emulation is unsupported")
    if BUNDLE.drive == "" or str(BUNDLE).startswith("\\\\"):
        raise RuntimeError("Extract to a short local drive path, such as C:\\RADAR\\CoverLab")
    if linked(APP) or not APP.is_dir():
        raise RuntimeError("CoverLab package directory is missing or linked")
    drive_type_api = ctypes.windll.kernel32.GetDriveTypeW
    drive_type_api.argtypes = [ctypes.c_wchar_p]
    drive_type_api.restype = ctypes.c_uint
    drive_type = drive_type_api(BUNDLE.drive + "\\")
    if drive_type != 3:
        raise RuntimeError("Extract to a fixed local drive, such as C:\\RADAR\\CoverLab")
    if len(str(BUNDLE)) > 50:
        raise RuntimeError("Extract to a shorter local path, such as C:\\RADAR\\CoverLab; Windows long paths are not changed")
    if require_space and shutil.disk_usage(APP).free < 10 * 1024**3:
        raise RuntimeError("At least 10 GiB free on the installation drive is required")
    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                    ("total_phys", ctypes.c_ulonglong), ("avail_phys", ctypes.c_ulonglong),
                    ("total_page", ctypes.c_ulonglong), ("avail_page", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong), ("avail_virtual", ctypes.c_ulonglong),
                    ("avail_extended", ctypes.c_ulonglong)]
    memory = MemoryStatus()
    memory.length = ctypes.sizeof(memory)
    if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)) and memory.total_phys < 16 * 1024**3:
        print("Warning: less than 16 GiB RAM; model execution may fail or be slow.", flush=True)
    try:
        ctypes.WinDLL("msvcp140.dll")
    except OSError as exc:
        raise RuntimeError("需要 Microsoft Visual C++ x64 运行库。请打开包内 START-HERE.html 的微软官方链接安装后重试；程序不会自动更改系统。") from exc
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        if not regular_file(BUNDLE / "native-tools" / "bin" / name):
            raise RuntimeError("Missing bundled native tool: " + name)


@contextlib.contextmanager
def installation_lock():
    if linked(LOCK) or (LOCK.exists() and not regular_file(LOCK)):
        raise RuntimeError("Installer lock is not a regular file; preserved unchanged")
    with LOCK.open("a+b") as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError("Another installer is active; wait for it to finish") from None
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def failed_state(target: Path) -> dict:
    state_file = target / "install-state.json"
    if linked(target) or not target.is_dir() or not regular_file(state_file) or state_file.stat().st_size > 65536:
        raise RuntimeError("Retry requires this installer's regular failed runtime; preserved unchanged")
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RuntimeError("Unreadable failed state; runtime preserved unchanged") from None
    if (not isinstance(state, dict) or state.get("status") != "failed"
            or state.get("installer") != "coverlab-windows-v1"
            or not isinstance(state.get("error"), str)
            or type(state.get("payload_files_verified")) is not int
            or not 0 < state["payload_files_verified"] <= MAX_MANIFEST_ENTRIES):
        raise RuntimeError("Only a recorded failed Windows installation is retryable; runtime preserved")
    return state


def environment() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(("PIP_", "PYTHON", "HF_", "TRANSFORMERS_"))}
    env.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", PIP_NO_INDEX="1",
               PIP_CONFIG_FILE="NUL", PIP_DISABLE_PIP_VERSION_CHECK="1",
               HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1",
               PATH=str(BUNDLE / "native-tools" / "bin") + os.pathsep + env.get("PATH", ""),
               HF_HOME=str(RUNTIME / "cache" / "hf"),
               MPLCONFIGDIR=str(RUNTIME / "cache" / "mpl"),
               NUMBA_CACHE_DIR=str(RUNTIME / "cache" / "numba"))
    return env


def run_logged(command: list[str | Path], log: Path, env: dict[str, str], timeout: int = 1200) -> None:
    with log.open("w", encoding="utf-8") as output:
        result = subprocess.run([str(x) for x in command], cwd=BUNDLE, env=env,
                                stdin=subprocess.DEVNULL, stdout=output,
                                stderr=subprocess.STDOUT, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError("Step failed (exit " + str(result.returncode) + "); see " + str(log))


def pip_command(python: Path, *args: str | Path) -> list[str | Path]:
    return [python, "-X", "utf8", "-I", "-B", "-m", "pip", *args]


def native_cpu_probe(python: Path, log: Path, env: dict[str, str], engine_root: Path) -> None:
    """Validate this host, even when a ready folder was copied from another PC."""
    probe = ("import json,socket; socket.socket.connect=lambda *a,**k: "
             "(_ for _ in ()).throw(RuntimeError('Offline import probe')); "
             "import torch,torchaudio,numpy,soundfile,transformers,librosa,yaml,onnx,onnxruntime,scipy; "
             "assert 'CPUExecutionProvider' in onnxruntime.get_available_providers(); "
             "torch.set_num_threads(1); "
             "print(json.dumps({'torch':torch.__version__,'numpy':numpy.__version__,"
             "'cpu_tensor_sum':float(torch.ones(3,device='cpu').sum())}))")
    probe += ("; import sys; sys.path.insert(0," + repr(str(engine_root)) + "); "
              "import inference; from modules.length_regulator import InterpolateRegulator; "
              "from modules.bigvgan.bigvgan import BigVGAN; import fcpe_pitch; "
              "print('Selected model import chain passed; no model loaded')")
    run_logged([python, "-X", "utf8", "-I", "-B", "-c", probe],
               log, env, timeout=240)


def install_new(verified_count: int) -> dict:
    if RUNTIME.exists() or linked(RUNTIME):
        raise FileExistsError("Existing runtime preserved; use --retry-failed only for a recorded failed install")
    if (APP / "installation.local.json").exists():
        raise FileExistsError("Existing local configuration preserved; extract a fresh package")
    RUNTIME.mkdir()
    state = {"installer": "coverlab-windows-v1", "status": "installing",
             "started_at": time.time(), "payload_files_verified": verified_count,
             "target": str(RUNTIME), "steps": []}
    def save() -> None:
        temporary = RUNTIME / "install-state.pending"
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(RUNTIME / "install-state.json")
    save()
    started = time.monotonic()
    env = environment()
    try:
        shutil.copytree(BOOTSTRAP, RUNTIME / "python")
        python = RUNTIME / "python" / "python.exe"
        (RUNTIME / "python" / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True)
        run_logged(pip_command(python, "--isolated", "install", "--no-index", "--no-cache-dir",
                               "--no-compile", "--no-deps", "--require-hashes", "--find-links",
                               PAYLOAD / "wheelhouse", "-r", BUNDLE / "windows-requirements.lock"),
                   RUNTIME / "package-install.log", env, timeout=3600)
        state["steps"].append("locked_offline_wheels_installed")
        save()
        run_logged(pip_command(python, "--isolated", "check"),
                   RUNTIME / "pip-check.log", env, timeout=180)
        native_cpu_probe(python, RUNTIME / "native-imports.log", env, PAYLOAD / "seed-vc")
        state["steps"].append("package_check_and_native_cpu_imports")
        save()
        shutil.copytree(PAYLOAD / "seed-vc", RUNTIME / "seed-vc")
        shutil.copytree(PAYLOAD / "voice-models", RUNTIME / "voice-models")
        sys.path.insert(0, str(APP))
        from runtime_config import load_installation
        from verify_engine import verify_engine
        from verify_models import verify
        config = load_installation(APP)
        expected = {"voice_python": python, "separation_python": python,
                    "engine_root": RUNTIME / "seed-vc", "models": RUNTIME / "voice-models",
                    "separation_models": APP / "models" / "separation"}
        for key, path in expected.items():
            if Path(config[key]) != path:
                raise RuntimeError("Unexpected packaged runtime configuration: " + key)
        revision = json.loads((APP / "request.json").read_text(encoding="utf-8"))["code_revision"]
        state["engine"] = verify_engine(APP, config["engine_root"], revision)
        models = verify(APP)
        if not models["all_match"]:
            raise RuntimeError("Installed model verification failed")
        state["models_verified"] = len(models["files"])
        run_logged([python, "-X", "utf8", "-B", APP / "doctor.py", "--port", "0", "--mode", "mixed"],
                   RUNTIME / "doctor.log", env, timeout=180)
        state["steps"].append("engine_models_and_doctor_verified")
        state.update(status="ready", elapsed_seconds=round(time.monotonic() - started, 3),
                     model_inference_tested=False,
                     scope="Native Windows x64 CPU imports verified on this host; model inference not tested")
        save()
        return state
    except BaseException as exc:
        state.update(status="failed", error=type(exc).__name__ + ": " + str(exc),
                     elapsed_seconds=round(time.monotonic() - started, 3))
        save()
        raise


def ready_state() -> dict:
    path = RUNTIME / "install-state.json"
    if linked(RUNTIME) or not RUNTIME.is_dir() or not regular_file(path) or path.stat().st_size > 65536:
        raise RuntimeError("Runtime has no valid ready state; existing files preserved")
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or state.get("installer") != "coverlab-windows-v1" or state.get("status") != "ready":
        raise RuntimeError("Runtime is not ready; failed installs may use --retry-failed")
    return state


def install(retry_failed: bool) -> dict:
    if (APP / "installation.local.json").exists():
        raise FileExistsError("Existing local configuration preserved; extract a fresh package")
    with installation_lock():
        if RUNTIME.exists() or linked(RUNTIME):
            if not retry_failed:
                state = ready_state()
                check_host(require_space=False)
                # A recorded installation is not proof of compatibility on this host.
                native_cpu_probe(RUNTIME / "python" / "python.exe",
                                 RUNTIME / "native-startup.log", environment(),
                                 RUNTIME / "seed-vc")
                return state
            failed_state(RUNTIME)
            count = verify_payload()
            check_host()
            history = APP / ".install-history"
            if linked(history) or (history.exists() and not history.is_dir()):
                raise RuntimeError("Invalid failure history; runtime preserved")
            history.mkdir(exist_ok=True)
            saved = history / uuid.uuid4().hex[:8]
            saved.mkdir()
            RUNTIME.rename(saved / "runtime")
            print("Preserved failed installation: " + str(saved / "runtime"), flush=True)
            return install_new(count)
        if retry_failed:
            raise RuntimeError("No failed runtime exists; remove --retry-failed for first install")
        count = verify_payload()
        check_host()
        return install_new(count)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--check", action="store_true", help="Install if needed and verify startup without opening the UI")
    args = parser.parse_args()
    try:
        state = install(args.retry_failed)
        env = environment()
        python = RUNTIME / "python" / "python.exe"
        run_logged([python, "-X", "utf8", "-B", APP / "doctor.py", "--port", "0", "--mode", "mixed"],
                   RUNTIME / "doctor-startup.log", env, timeout=180)
        print(json.dumps({"status": state["status"], "runtime": str(RUNTIME),
                          "startup_check": "passed", "model_inference_tested": False},
                         ensure_ascii=False), flush=True)
        if args.check:
            return 0
        return subprocess.call([str(python), "-X", "utf8", "-B", str(APP / "CoverLab" / "server.py"),
                                "--port", "0", "--open-browser"], cwd=str(BUNDLE), env=env)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print("Installation/startup stopped: " + str(exc), file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    sys.exit(main())

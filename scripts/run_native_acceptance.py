# SPDX-License-Identifier: GPL-3.0-only
#!/usr/bin/env python3
"""Offline, bounded native Windows x64 technical acceptance for CoverLab."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import struct
import subprocess
import sys
import time
import wave


SOURCE_SUFFIXES = {".py", ".ps1", ".cmd", ".bat", ".js", ".ts", ".tsx", ".css", ".html"}
EXIT_SKIPPED = 3


class AcceptanceFailure(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def machine_info() -> dict:
    info = {"sys_platform": sys.platform, "os_name": os.name,
            "machine": platform.machine(), "python": sys.version.split()[0],
            "python_bits": struct.calcsize("P") * 8,
            "logical_cpu_count": os.cpu_count()}
    if sys.platform != "win32":
        return info
    version = sys.getwindowsversion()
    info.update(os_version={"major": version.major, "minor": version.minor,
                            "build": version.build})

    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                    ("total_phys", ctypes.c_ulonglong), ("available_phys", ctypes.c_ulonglong),
                    ("total_page", ctypes.c_ulonglong), ("available_page", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong), ("available_virtual", ctypes.c_ulonglong),
                    ("available_extended", ctypes.c_ulonglong)]

    memory = MemoryStatus()
    memory.length = ctypes.sizeof(memory)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
        raise OSError(ctypes.get_last_error(), "GlobalMemoryStatusEx failed")
    info["total_memory_bytes"] = memory.total_phys
    process_machine = ctypes.c_ushort()
    native_machine = ctypes.c_ushort()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.IsWow64Process2.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ushort),
                                       ctypes.POINTER(ctypes.c_ushort)]
    kernel.IsWow64Process2.restype = ctypes.c_int
    if not kernel.IsWow64Process2(kernel.GetCurrentProcess(), ctypes.byref(process_machine),
                                 ctypes.byref(native_machine)):
        raise OSError(ctypes.get_last_error(), "IsWow64Process2 failed")
    info["native_machine_code"] = native_machine.value
    info["process_machine_code"] = process_machine.value
    return info


def source_hashes(bundle: Path) -> dict[str, str]:
    files = [Path(__file__).resolve()]
    if bundle.is_dir():
        files.extend(path for path in bundle.rglob("*") if path.is_file()
                     and (path.suffix.lower() in SOURCE_SUFFIXES or
                          path.name in {"request.json", "payload-sha256.json", "windows-requirements.lock"})
                     and "runtime" not in path.relative_to(bundle).parts)
    hashes = {}
    for path in sorted(files):
        if path.is_symlink():
            raise AcceptanceFailure(f"Linked source file is not accepted: {path}")
        key = "acceptance/" + path.name if path == files[0] else path.relative_to(bundle).as_posix()
        hashes[key] = sha256(path)
    return hashes


def write_json(path: Path, value: dict) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def generate_signal(path: Path, frequency: float) -> None:
    rate = 22050
    samples = bytearray()
    for index in range(3 * rate):
        seconds = index / rate
        envelope = min(1.0, seconds / .08, (3 - seconds) / .08)
        value = .18 * envelope * (math.sin(2 * math.pi * frequency * seconds) +
                                   .18 * math.sin(2 * math.pi * frequency * 2 * seconds))
        samples.extend(struct.pack("<h", round(value * 32767)))
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(samples)


def validate_pcm(path: Path, expected_seconds: float = 3.0) -> dict:
    with wave.open(str(path), "rb") as stream:
        channels, width, rate, frames = (stream.getnchannels(), stream.getsampwidth(),
                                          stream.getframerate(), stream.getnframes())
        if channels not in (1, 2) or width not in (1, 2, 3, 4) or rate < 8000 or frames <= 0:
            raise AcceptanceFailure("Output has invalid PCM layout")
        duration = frames / rate
        if not math.isfinite(duration) or abs(duration - expected_seconds) > max(.1, expected_seconds * .03):
            raise AcceptanceFailure(f"Output duration {duration:.3f}s differs from 3s input")
        total = count = 0
        while True:
            data = stream.readframes(16384)
            if not data:
                break
            if len(data) % width:
                raise AcceptanceFailure("Output PCM has a partial sample")
            for offset in range(0, len(data), width):
                raw = int.from_bytes(data[offset:offset + width], "little", signed=width != 1)
                sample = ((raw - 128) / 128) if width == 1 else raw / (1 << (8 * width - 1))
                total += sample * sample
                count += 1
        if count != frames * channels:
            raise AcceptanceFailure("Output PCM frame count is inconsistent")
        rms = math.sqrt(total / count)
        if not math.isfinite(rms) or rms < .001:
            raise AcceptanceFailure(f"Output is silent or non-finite (RMS {rms})")
        return {"file": str(path), "sha256": sha256(path), "sample_rate": rate,
                "channels": channels, "sample_width_bytes": width,
                "duration_seconds": duration, "rms": rms}


def run_step(name: str, command: list[str], timeout: int, cwd: Path, env: dict,
             gate_python: Path, launch, result: Path, summary: dict) -> None:
    stdout = result / f"{name}.stdout.log"
    stderr = result / f"{name}.stderr.log"
    step = {"name": name, "command": command, "timeout_seconds": timeout,
            "stdout": str(stdout), "stderr": str(stderr), "status": "running",
            "return_code": None}
    summary["steps"].append(step)
    started = time.monotonic()
    proc = None
    try:
        with stdout.open("wb") as out, stderr.open("wb") as err:
            proc = launch(command, gate_python=gate_python, cwd=cwd, env=env,
                          stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                          start_new_session=False)
            try:
                code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                step["status"] = "timeout"
                raise AcceptanceFailure(f"{name} timed out after {timeout}s")
            step["return_code"] = code
            if code:
                step["status"] = "failed"
                raise AcceptanceFailure(f"{name} exited {code}; see step logs")
            step["status"] = "passed"
    except Exception:
        if step["status"] == "running":
            step["status"] = "failed"
        raise
    finally:
        step["wall_seconds"] = time.monotonic() - started
        if proc is not None:
            try:
                proc.stop()
            except Exception as exc:
                step["cleanup_error"] = f"{type(exc).__name__}: {exc}"
                step["status"] = "failed"
                raise AcceptanceFailure(f"{name}: process tree termination unverified") from exc
        write_json(result / "summary.json", summary)


def require_install_state(bundle: Path) -> None:
    app = bundle / "CoverLab"
    if not app.is_dir() or not (bundle / "install_windows.py").is_file():
        raise AcceptanceFailure("Bundle is missing the complete CoverLab app or installer")
    if (app / "runtime").exists() or (app / "installation.local.json").exists():
        raise AcceptanceFailure("Bundle is not a fresh, uninstalled candidate directory")
    if not (bundle / ".bootstrap/python/python.exe").is_file():
        raise AcceptanceFailure("Bundled bootstrap Python is missing")


def accept(bundle: Path, result: Path, summary: dict) -> None:
    require_install_state(bundle)
    app = bundle / "CoverLab"
    bootstrap = bundle / ".bootstrap/python/python.exe"
    sys.path.insert(0, str(app / "CoverLab"))
    from process_tree import launch

    env = os.environ.copy()
    env.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", HF_HUB_OFFLINE="1",
               TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1",
               PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1")
    run_step("install", [str(bootstrap), "-u", str(bundle / "install_windows.py"), "--check"],
             1200, bundle, env, bootstrap, launch, result, summary)
    runtime = app / "runtime"
    runtime_python = runtime / "python/python.exe"
    state_path = runtime / "install-state.json"
    if not runtime_python.is_file() or not state_path.is_file():
        raise AcceptanceFailure("Installer returned zero without a runtime Python and state")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("status") != "ready" or state.get("installer") != "coverlab-windows-v1":
        raise AcceptanceFailure("Installer returned zero without ready runtime state")
    summary["installation"] = {"state": state, "runtime_python": str(runtime_python)}
    write_json(result / "summary.json", summary)

    native = app / "CoverLab/native_windows_acceptance.py"
    run_step("native_contract", [str(runtime_python), "-u", str(native)],
             120, app, env, runtime_python, launch, result, summary)
    native_lines = (result / "native_contract.stdout.log").read_text(encoding="utf-8").splitlines()
    native_report = json.loads(native_lines[-1]) if native_lines else {}
    if native_report.get("status") != "passed" or native_report.get("native_windows") is not True:
        raise AcceptanceFailure("Native contract did not report passed")
    summary["native_contract"] = native_report
    write_json(result / "summary.json", summary)

    source, reference = result / "synthetic-source.wav", result / "synthetic-reference.wav"
    generate_signal(source, 220)
    generate_signal(reference, 293.66)
    template = json.loads((app / "request.json").read_text(encoding="utf-8"))
    if not isinstance(template.get("code_revision"), str):
        raise AcceptanceFailure("Packaged request schema has no code_revision")
    output = result / "converted"
    output.mkdir()
    request = dict(template, engine_root=str(runtime / "seed-vc"),
                   models=str(runtime / "voice-models"), input_path=str(source),
                   reference_path=str(reference), output_dir=str(output),
                   processing_mode="cpu", seed=42,
                   reference_provenance={"kind": "synthetic test signal, no human identity"},
                   params={"steps": 8, "semitones": 0, "test_type": "technical_smoke"})
    request_path = result / "voice-request.json"
    write_json(request_path, request)
    cache = result / "cache"
    env.update(NUMBA_CACHE_DIR=str(cache / "numba"), MPLCONFIGDIR=str(cache / "mpl"),
               XDG_CACHE_HOME=str(cache), OMP_NUM_THREADS="4", OPENBLAS_NUM_THREADS="4",
               RADAR_FCPE_DEVICE="cpu")
    run_step("voice_8step_cpu", [str(runtime_python), "-u", str(app / "run_voice.py"),
                                 str(request_path)],
             900, app, env, runtime_python, launch, result, summary)
    reports = list(output.glob("vc_*.wav"))
    if len(reports) != 1 or not (output / "result.json").is_file():
        raise AcceptanceFailure("Voice runner did not produce one fresh WAV and result.json")
    report = json.loads((output / "result.json").read_text(encoding="utf-8"))
    if report.get("device") != "cpu" or report.get("params", {}).get("diffusion_steps") != 8:
        raise AcceptanceFailure("Voice runner did not report CPU and eight diffusion steps")
    if len(report.get("files", [])) != 1 or Path(report["files"][0]).resolve() != reports[0].resolve():
        raise AcceptanceFailure("Voice runner result does not identify the generated WAV")
    elapsed = report.get("elapsed_seconds")
    if not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed <= 0:
        raise AcceptanceFailure("Voice runner lacks a valid measured inference time")
    summary["voice"] = {"technical_smoke_only": True, "human_voice_quality_tested": False,
                        "full_song_tested": False, "model_elapsed_seconds": elapsed,
                        "wall_seconds": summary["steps"][-1]["wall_seconds"],
                        "output": validate_pcm(reports[0])}
    write_json(result / "summary.json", summary)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    bundle, result = args.bundle.resolve(), args.result.resolve()
    if result == bundle or bundle in result.parents:
        parser.error("--result must be outside --bundle")
    if result.exists() and any(result.iterdir()):
        parser.error("--result must be a new or empty directory")
    result.mkdir(parents=True, exist_ok=True)
    summary = {"status": "running", "scope": "offline native Windows x64 CPU technical smoke",
               "bundle": str(bundle), "result": str(result), "steps": []}
    write_json(result / "summary.json", summary)
    try:
        summary["platform"] = machine_info()
        summary["source_sha256"] = source_hashes(bundle)
        write_json(result / "summary.json", summary)
        platform_info = summary["platform"]
        if (sys.platform != "win32" or platform_info["python"][:4] != "3.12" or
                platform_info["python_bits"] != 64 or
                platform_info.get("native_machine_code") != 0x8664):
            summary.update(status="skipped", reason="Requires native Windows x64 with 64-bit Python 3.12; no acceptance steps ran")
            write_json(result / "summary.json", summary)
            print(json.dumps({"status": "skipped", "summary": str(result / "summary.json")}, ensure_ascii=False))
            return EXIT_SKIPPED
        accept(bundle, result, summary)
    except Exception as exc:
        summary.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        write_json(result / "summary.json", summary)
        print(json.dumps({"status": "failed", "error": summary["error"],
                          "summary": str(result / "summary.json")}, ensure_ascii=False), file=sys.stderr)
        return 1
    summary["status"] = "passed"
    write_json(result / "summary.json", summary)
    print(json.dumps({"status": "passed", "summary": str(result / "summary.json")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

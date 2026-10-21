# SPDX-License-Identifier: GPL-3.0-only
"""Bounded local startup checks. Never loads models, installs, or starts a server."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
from runtime_config import load_installation
from verify_engine import verify_engine


def inspect(root, port=8879, mode="mixed"):
    rows = []

    def add(name, ok, detail):
        rows.append({"check": name, "ok": bool(ok), "detail": detail})

    def file_check(name, path, executable=False, expected_bytes=None, expected_sha256=None):
        try:
            p = Path(path)
            ok = p.is_file() and p.stat().st_size > 0 and os.access(p, os.R_OK)
            if executable:
                ok = ok and os.access(p, os.X_OK)
            if expected_bytes is not None:
                ok = ok and p.stat().st_size == expected_bytes
            if ok and expected_sha256 is not None:
                digest = hashlib.sha256()
                with p.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
                ok = digest.hexdigest() == expected_sha256
            add(name, ok, str(p) if ok else str(p) + " (missing, unreadable, size or SHA256 mismatch)")
            return ok
        except (OSError, TypeError, ValueError) as exc:
            add(name, False, str(exc))
            return False

    def load(name):
        try:
            data = json.loads((root / name).read_text())
            if not isinstance(data, dict):
                raise ValueError("Expected JSON object")
            add(name, True, "JSON object parsed")
            return data
        except (OSError, ValueError) as exc:
            add(name, False, str(exc))
            return {}

    def packages(name, python, names):
        if not file_check(name + ".python", python, executable=True):
            return
        # Only discover top-level module specs; do not import ML packages/models.
        code = ("import importlib.util,json; print(json.dumps({n:"
                "importlib.util.find_spec(n) is not None for n in " + repr(names) + "}))")
        try:
            result = subprocess.run([str(python), "-I", "-B", "-c", code],
                                    capture_output=True, text=True, timeout=15)
            found = json.loads(result.stdout) if result.returncode == 0 else {}
            ok = result.returncode == 0 and all(found.get(n) is True for n in names)
            add(name + ".module_discovery", ok, found if found else "Probe failed; no package imports attempted")
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            add(name + ".module_discovery", False, type(exc).__name__)

    add("platform", os.name in ("posix", "nt"), "POSIX groups / Windows Job Object; native inference checked separately")
    add("python_version", sys.version_info >= (3, 10), sys.version.split()[0])
    for name in ("ffmpeg", "ffprobe"):
        binary = shutil.which(name)
        if not binary:
            add(name, False, "Not found in PATH")
            continue
        try:
            result = subprocess.run([binary, "-version"], capture_output=True, timeout=5)
            add(name, result.returncode == 0, binary)
        except (OSError, subprocess.TimeoutExpired) as exc:
            add(name, False, type(exc).__name__)
    for name in ("CoverLab/server.py", "CoverLab/core.py", "CoverLab/voice_jobs.py",
                 "run_voice.py", "voice_compat.py"):
        file_check(name, root / name)
    config = load("request.json")
    try:config.update(load_installation(root));add("runtime_installation",True,"Local configuration resolved")
    except (OSError,ValueError,TypeError) as exc:add("runtime_installation",False,str(exc))
    inventory = load("model-file-sizes.json")
    try:
        engine = Path(config["engine_root"])
        models = Path(config["models"])
        file_check("engine.inference", engine / "inference.py")
        try: add("engine.source_hashes", True, verify_engine(root, engine, config["code_revision"]))
        except (OSError, ValueError, KeyError) as exc: add("engine.source_hashes", False, str(exc))
        packages("voice", config["voice_python"],
                 ["torch", "numpy", "soundfile", "yaml", "torchaudio", "transformers", "librosa"])
        entries = inventory["voice"]
        if (not isinstance(entries, list) or len(entries) != 16
                or any(entry.get("relative_path") == "lj1995--VoiceConversionWebUI/rmvpe.pt" for entry in entries)):
            raise ValueError("Expected the selected 16-file FCPE voice inventory without RMVPE")
        for entry in entries:
            file_check("voice_model:" + entry["relative_path"], models / entry["relative_path"],
                       expected_bytes=entry["expected_bytes"])
    except (KeyError, TypeError, ValueError) as exc:
        add("voice_configuration", False, str(exc))
    if mode == "mixed":
        install = config
        try:
            for name in ("run_mixed.py", "run_separation_stage.py", "unet.py"):
                file_check(name, root / name)
            for name in ("separation_worker", "mix_worker"):
                file_check(name, install[name])
            packages("separation", install["separation_python"],
                     ["torch", "onnx", "onnxruntime", "scipy", "numpy", "soundfile"])
            entries = inventory["separation"]
            expected = {"vocals.onnx", "accompaniment.onnx"}
            if (not isinstance(entries, list) or len(entries) != 2
                    or {entry["relative_path"] for entry in entries} != expected):
                raise ValueError("Expected the two frozen wideband ONNX graphs")
            hashes = json.loads((root / "model-file-sha256.json").read_text())
            if not isinstance(hashes, list):
                raise ValueError("Expected a model digest inventory")
            separation_hashes = [row for row in hashes if row["section"] == "separation"]
            if (len(separation_hashes) != 2
                    or {row["path"] for row in separation_hashes} != expected):
                raise ValueError("Expected two wideband ONNX graph digests")
            digest_by_path = {row["path"]: row for row in separation_hashes}
            for entry in entries:
                relative_path = entry["relative_path"]
                digest = digest_by_path[relative_path]
                size = entry["expected_bytes"]
                if (not isinstance(size, int) or size <= 0 or digest["bytes"] != size
                        or not isinstance(digest["sha256"], str)
                        or not re.fullmatch(r"[0-9a-f]{64}", digest["sha256"])):
                    raise ValueError("Invalid wideband graph size or digest: " + relative_path)
                file_check("separation_model:" + entry["relative_path"],
                           Path(install["separation_models"]) / relative_path,
                           expected_bytes=size, expected_sha256=digest["sha256"])
        except (OSError, KeyError, TypeError, ValueError) as exc:
            add("mixed_configuration", False, str(exc))
    try:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
        add("port_bind", True, {"port": port, "scope": "Point-in-time bind only; not a reservation"})
    except OSError as exc:
        add("port_bind", False, {"port": port, "reason": str(exc), "action": "Do not kill or restart another process"})
    return {"scope": "Local startup prerequisites only; no inference or quality validation",
            "mode": mode, "startup_checks_passed": all(r["ok"] for r in rows),
            "inference_verified": False, "checks": rows,
            "limits": ["Module discovery does not prove native libraries import or inference works",
                       "Model digests match the packaged manifest but are not publisher signatures or complete dependency validation",
                       "No model loading, downloads, external requests, port takeover or data-directory writes"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--port", type=int, default=8879)
    parser.add_argument("--mode", choices=("dry", "mixed"), default="mixed")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("Port must be between 0 and 65535")
    report = inspect(args.root, args.port, args.mode)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["startup_checks_passed"] else 2


if __name__ == "__main__":
    sys.exit(main())

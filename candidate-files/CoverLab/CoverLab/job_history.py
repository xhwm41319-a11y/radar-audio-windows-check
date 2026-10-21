# SPDX-License-Identifier: GPL-3.0-only
"""Read persisted CoverLab voice-job history without resuming work."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat


_JOB_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_JOB_BYTES = 64 * 1024
_MAX_OUTPUT_BYTES = 1024 * 1024 * 1024
_MAX_JOBS = 4096


def _recovery(job_id: str, reason: str) -> dict:
    return {
        "id": job_id,
        "status": "recovery_required",
        "error": reason,
        "engine_reuse_blocked": True,
    }


def _read_job(folder: Path) -> dict:
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError("unsafe job directory")
    path = folder / "job.json"
    if path.is_symlink():
        raise ValueError("unsafe job record")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_JOB_BYTES:
            raise ValueError("invalid job record size or type")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            raw = source.read(_MAX_JOB_BYTES + 1)
        if len(raw) > _MAX_JOB_BYTES:
            raise ValueError("job record is too large")
    finally:
        os.close(descriptor)
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("job record is not an object")
    return value


def _public_job(record: dict, job_id: str, status: str) -> dict:
    if record.get("id") != job_id:
        raise ValueError("job identity does not match its directory")
    steps = record.get("steps")
    seconds = record.get("source_seconds")
    timeout = record.get("timeout_seconds")
    name = record.get("source_name")
    mode = record.get("input_mode")
    if type(steps) is not int or steps not in (8, 30):
        raise ValueError("invalid job steps")
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 1 <= seconds <= 600:
        raise ValueError("invalid job duration")
    if type(timeout) is not int or not 1 <= timeout <= 7200:
        raise ValueError("invalid job timeout")
    if not isinstance(name, str) or not name or len(name) > 300:
        raise ValueError("invalid source name")
    if mode not in ("dry", "mixed"):
        raise ValueError("invalid input mode")
    processing_mode = record.get("processing_mode", "auto")
    if processing_mode not in ("auto", "cpu", "gpu"):
        raise ValueError("invalid processing mode")
    result = {
        "id": job_id,
        "status": status,
        "steps": steps,
        "source_seconds": float(seconds),
        "timeout_seconds": timeout,
        "source_name": name,
        "input_mode": mode,
        "processing_mode": processing_mode,
    }
    for key in ("source_id", "reference_id"):
        if key in record:
            value = record[key]
            if not isinstance(value, str) or not _JOB_ID.fullmatch(value):
                raise ValueError(f"invalid {key}")
            result[key] = value
    if status == "completed":
        elapsed = record.get("elapsed_seconds")
        device = record.get("device")
        if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or not 0 <= elapsed <= 7200:
            raise ValueError("invalid elapsed time")
        if not isinstance(device, str) or not device or len(device) > 80 or any(ord(char) < 32 for char in device):
            raise ValueError("invalid device")
        result["elapsed_seconds"] = float(elapsed)
        result["device"] = device
    stage = record.get("stage")
    if isinstance(stage, str) and len(stage) <= 80:
        result["stage"] = stage
    if status == "cancelled":
        result["error"] = "User cancelled; no output was published"
    elif status == "failed":
        result["error"] = "Saved job failed"
    return result


def _verified_output(workspace, file_value: object) -> dict | None:
    if not isinstance(file_value, dict):
        return None
    file_id = file_value.get("id")
    stored_hash = file_value.get("sha256")
    if not isinstance(file_id, str) or not _JOB_ID.fullmatch(file_id):
        return None
    if not isinstance(stored_hash, str) or not _SHA256.fullmatch(stored_hash):
        return None
    try:
        path, catalog_record = workspace.file(file_id)
        if catalog_record.get("id") != file_id or catalog_record.get("section") != "outputs":
            return None
        catalog_hash = catalog_record.get("sha256")
        if not isinstance(catalog_hash, str) or not _SHA256.fullmatch(catalog_hash) or stored_hash != catalog_hash:
            return None
        data_root = Path(workspace.data).resolve()
        if path.is_symlink() or path.parent.is_symlink() or not path.resolve(strict=True).is_relative_to(data_root):
            return None
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_OUTPUT_BYTES:
                return None
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_OUTPUT_BYTES:
                    return None
                digest.update(chunk)
        finally:
            os.close(descriptor)
        if digest.hexdigest() != stored_hash:
            return None
        return workspace._public(catalog_record)
    except Exception:
        return None


def load_history(workspace) -> tuple[dict, bool]:
    """Load terminal jobs; uncertain persisted jobs require manual recovery."""
    history = Path(workspace.data) / "voice-runs"
    jobs: dict[str, dict] = {}
    reuse_blocked = False
    try:
        if history.is_symlink():
            return jobs, True
        if not history.exists():
            return jobs, False
        if not history.is_dir():
            return jobs, True
        with os.scandir(history) as entries:
            for index, entry in enumerate(entries):
                if index >= _MAX_JOBS:
                    reuse_blocked = True
                    break
                if not _JOB_ID.fullmatch(entry.name):
                    continue
                job_id = entry.name
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    reuse_blocked = True
                    continue
                try:
                    record = _read_job(history / job_id)
                    if record.get("id") != job_id:
                        raise ValueError("job identity does not match its directory")
                    status = record.get("status")
                    if "engine_reuse_blocked" in record and record["engine_reuse_blocked"] is not False:
                        jobs[job_id] = _recovery(job_id, "Engine reuse is blocked pending manual recovery")
                        reuse_blocked = True
                        continue
                    if status not in ("completed", "failed", "cancelled"):
                        jobs[job_id] = _recovery(job_id, "Persisted job state is unknown; manual recovery is required")
                        reuse_blocked = True
                        continue
                    public = _public_job(record, job_id, status)
                    if status == "completed":
                        output = _verified_output(workspace, record.get("file"))
                        if output is None:
                            public["status"] = "failed"
                            public["error"] = "Saved output is unavailable; download disabled"
                        else:
                            public["file"] = output
                    jobs[job_id] = public
                except Exception:
                    jobs[job_id] = _recovery(job_id, "Persisted job record is corrupt; manual recovery is required")
                    reuse_blocked = True
    except OSError:
        reuse_blocked = True
    return jobs, reuse_blocked

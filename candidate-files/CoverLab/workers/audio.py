# SPDX-License-Identifier: MIT
"""Standalone FFmpeg audio conversion and mastering worker."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit


LOUDNESS_FIELDS = (
    "input_i",
    "input_tp",
    "input_lra",
    "input_thresh",
    "target_offset",
)
_LOUDNESS_OBJECT = re.compile(r'\{\s*"input_i"\s*:[\s\S]*?\}')


def _is_url(value: str) -> bool:
    return bool(urlsplit(value).scheme)


def local_path(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{label} must be a non-empty local path")
    if _is_url(value):
        raise ValueError(f"{label} must be a local path, not a URL")
    return Path(value).expanduser()


def input_file(value: Any, label: str) -> Path:
    path = local_path(value, label)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{label} does not exist: {path}") from exc
    if not resolved.is_file():
        raise ValueError(f"{label} must name a regular file: {path}")
    return resolved


def output_directory(value: Any) -> Path:
    path = local_path(value, "output_dir")
    try:
        path.mkdir(parents=True, exist_ok=True)
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"cannot create output_dir: {path}") from exc
    if not resolved.is_dir():
        raise ValueError(f"output_dir must be a directory: {path}")
    return resolved


def reject_existing(paths: Sequence[Path]) -> None:
    for path in paths:
        if os.path.lexists(os.fspath(path)):
            raise FileExistsError(f"Refusing to overwrite existing path: {path}")


def resolve_ffmpeg(value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or _is_url(value):
        raise ValueError("ffmpeg must be an executable path or command name")
    found = shutil.which(value)
    candidate = Path(found) if found else Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = candidate.resolve()
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise ValueError(f"FFmpeg executable is unavailable: {value}")
    return str(candidate)


def execute(command: Sequence[str]) -> str:
    """Run an explicit FFmpeg argv without a shell or interactive stdin."""
    if isinstance(command, (str, bytes)) or not command:
        raise ValueError("command must be a non-empty argument sequence")
    argv = [os.fspath(item) for item in command]
    if "-nostdin" not in argv:
        raise ValueError("FFmpeg commands must include -nostdin")
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=900,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("FFmpeg exceeded the 900-second processing limit") from exc
    except OSError as exc:
        raise RuntimeError(f"could not start FFmpeg: {exc}") from exc
    if completed.returncode:
        detail = completed.stderr[-4000:].strip()
        raise RuntimeError(
            f"FFmpeg exited with status {completed.returncode}"
            + (f":\n{detail}" if detail else "")
        )
    return completed.stderr


def ffmpeg_prefix(ffmpeg: str, loglevel: str = "error") -> list[str]:
    return [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-nostats",
        "-loglevel",
        loglevel,
        "-protocol_whitelist",
        "file,pipe",
    ]


def finite_number(value: Any, label: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValueError(f"{label} must be finite and between {minimum} and {maximum}")
    return number


def boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value


def loudness_analysis_filter(target_lufs: float) -> str:
    return f"loudnorm=I={target_lufs:.2f}:TP=-1.5:LRA=11:print_format=json"


def parse_loudness(stderr: str) -> dict[str, float]:
    data = None
    for match in _LOUDNESS_OBJECT.finditer(stderr):
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
    if not isinstance(data, dict):
        raise ValueError("FFmpeg did not return a loudness measurement")

    result: dict[str, float] = {}
    for field in LOUDNESS_FIELDS:
        value = data.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError(f"loudness measurement is missing {field}")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"loudness measurement has invalid {field}") from exc
        if not math.isfinite(number):
            raise ValueError("silent or non-finite loudness measurement")
        result[field] = number
    return result


def loudnorm_filter(target_lufs: float, measurement: dict[str, float]) -> str:
    measured = ":".join(
        f"{name}={measurement[field]:.6f}"
        for name, field in (
            ("measured_I", "input_i"),
            ("measured_TP", "input_tp"),
            ("measured_LRA", "input_lra"),
            ("measured_thresh", "input_thresh"),
            ("offset", "target_offset"),
        )
    )
    return (
        f"loudnorm=I={target_lufs:.2f}:TP=-1.5:LRA=11:{measured}:"
        "linear=true:print_format=json"
    )


def write_result_atomic(path: Path, report: dict[str, Any]) -> None:
    temp_path: str | None = None
    try:
        descriptor, temp_path = tempfile.mkstemp(
            prefix=".result-", suffix=".tmp", dir=path.parent
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        # Linking is atomic and fails if result.json appeared after preflight.
        os.link(temp_path, path)
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass


def read_request(path_value: str) -> dict[str, Any]:
    path = input_file(path_value, "request_json")
    if path.stat().st_size > 65536:
        raise ValueError("request JSON exceeds 64 KiB")
    try:
        request = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read request JSON: {exc}") from exc
    if not isinstance(request, dict):
        raise ValueError("request JSON must contain an object")
    return request


def _audio_request(request: dict[str, Any]) -> dict[str, Any]:
    for key in ("ffmpeg", "input_path", "output_dir", "params"):
        if key not in request:
            raise ValueError(f"request is missing {key}")
    params = request["params"]
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    allowed = {"operation", "format", "target_lufs", "denoise", "trim_silence"}
    unknown = set(params) - allowed
    if unknown:
        raise ValueError(f"unknown audio parameter(s): {', '.join(sorted(unknown))}")

    operation = params.get("operation")
    if not isinstance(operation, str) or operation not in {"convert", "clean", "master"}:
        raise ValueError("operation must be convert, clean, or master")
    output_format = params.get("format")
    if not isinstance(output_format, str) or output_format not in {"wav", "mp3", "flac"}:
        raise ValueError("format must be wav, mp3, or flac")
    target_lufs = finite_number(
        params.get("target_lufs", -19), "target_lufs", -36, -9
    )
    denoise = boolean(params.get("denoise", False), "denoise")
    trim_silence = boolean(params.get("trim_silence", False), "trim_silence")
    if operation != "clean" and (denoise or trim_silence):
        raise ValueError("denoise and trim_silence are available only for clean")

    normalized = {
        "operation": operation,
        "format": output_format,
        "target_lufs": target_lufs,
        "denoise": denoise,
        "trim_silence": trim_silence,
    }
    return {
        "ffmpeg": resolve_ffmpeg(request["ffmpeg"]),
        "input": input_file(request["input_path"], "input_path"),
        "output_dir": output_directory(request["output_dir"]),
        "params": normalized,
    }


def run_audio_request(request: dict[str, Any]) -> dict[str, Any]:
    config = _audio_request(request)
    params = config["params"]
    suffix = {"wav": ".wav", "mp3": ".mp3", "flac": ".flac"}[params["format"]]
    output_path = config["output_dir"] / f"{params['operation']}{suffix}"
    result_path = config["output_dir"] / "result.json"
    reject_existing([output_path, result_path])

    base_filters: list[str] = []
    if params["operation"] == "clean":
        if params["denoise"]:
            base_filters.append("afftdn")
        if params["trim_silence"]:
            base_filters.append(
                "silenceremove=start_periods=1:start_duration=0.2:"
                "start_threshold=-50dB:stop_periods=1:stop_duration=0.2:"
                "stop_threshold=-50dB"
            )

    started = time.monotonic()
    measurement: dict[str, float] | None = None
    filters = list(base_filters)
    if params["operation"] in {"clean", "master"}:
        analysis = loudness_analysis_filter(params["target_lufs"])
        analysis_chain = ",".join(base_filters + [analysis])
        analysis_command = (
            ffmpeg_prefix(config["ffmpeg"], "info")
            + [
                "-i",
                str(config["input"]),
                "-map",
                "0:a:0",
                "-vn",
                "-af",
                analysis_chain,
                "-f",
                "null",
                "-",
            ]
        )
        measurement = parse_loudness(execute(analysis_command))
        master = loudnorm_filter(params["target_lufs"], measurement)
        filters.append(master)
        output_chain = ",".join(base_filters + [master])
    else:
        output_chain = ",".join(base_filters) if base_filters else None

    command = ffmpeg_prefix(config["ffmpeg"])
    command += ["-i", str(config["input"]), "-map", "0:a:0", "-vn"]
    # Loudnorm may emit large frames; bound them after resampling so FLAC
    # receives legal block sizes without padding the final audio duration.
    final_filters = ([output_chain] if output_chain else []) + [
        "aresample=48000", "asetnsamples=n=4096:p=0"
    ]
    command += ["-af", ",".join(final_filters)]
    command += ["-ar", "48000", "-map_metadata", "-1"]
    if params["format"] == "wav":
        command += ["-c:a", "pcm_s24le"]
    elif params["format"] == "mp3":
        command += ["-c:a", "libmp3lame", "-b:a", "320k"]
    else:
        command += ["-c:a", "flac"]
    command += ["-n", str(output_path)]
    execute(command)

    report = {
        "files": [str(output_path)],
        "engine": "ffmpeg",
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "filters": filters,
        "loudness_measurement": measurement,
        "params": params,
    }
    write_result_atomic(result_path, report)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: audio.py REQUEST.json", file=sys.stderr)
        return 2
    try:
        run_audio_request(read_request(args[0]))
    except Exception as exc:
        print(f"audio worker failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

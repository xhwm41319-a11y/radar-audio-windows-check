# SPDX-License-Identifier: MIT
"""Standalone FFmpeg vocal and instrumental mixing worker."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Sequence

from audio import (
    boolean,
    execute,
    ffmpeg_prefix,
    finite_number,
    input_file,
    loudness_analysis_filter,
    loudnorm_filter,
    output_directory,
    parse_loudness,
    read_request,
    reject_existing,
    resolve_ffmpeg,
    write_result_atomic,
)


def _mix_request(request: dict[str, Any]) -> dict[str, Any]:
    for key in ("ffmpeg", "vocal_path", "instrumental_path", "output_dir", "params"):
        if key not in request:
            raise ValueError(f"request is missing {key}")
    params = request["params"]
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    allowed = {
        "vocal_db",
        "instrumental_db",
        "highpass_hz",
        "presence_db",
        "compressor",
        "reverb",
        "target_lufs",
    }
    unknown = set(params) - allowed
    if unknown:
        raise ValueError(f"unknown mix parameter(s): {', '.join(sorted(unknown))}")

    vocal_db = finite_number(params.get("vocal_db", 0), "vocal_db", -60, 24)
    instrumental_db = finite_number(
        params.get("instrumental_db", 0), "instrumental_db", -60, 24
    )
    highpass_hz = finite_number(
        params.get("highpass_hz", 0), "highpass_hz", 0, 20000
    )
    if highpass_hz != 0 and highpass_hz < 20:
        raise ValueError("highpass_hz must be 0 or between 20 and 20000")
    presence_db = finite_number(
        params.get("presence_db", 0), "presence_db", -24, 24
    )
    compressor = boolean(params.get("compressor", False), "compressor")
    reverb = finite_number(params.get("reverb", 0), "reverb", 0, 0.5)
    target_lufs = finite_number(
        params.get("target_lufs", -19), "target_lufs", -36, -9
    )
    return {
        "ffmpeg": resolve_ffmpeg(request["ffmpeg"]),
        "vocal": input_file(request["vocal_path"], "vocal_path"),
        "instrumental": input_file(
            request["instrumental_path"], "instrumental_path"
        ),
        "output_dir": output_directory(request["output_dir"]),
        "params": {
            "vocal_db": vocal_db,
            "instrumental_db": instrumental_db,
            "highpass_hz": highpass_hz,
            "presence_db": presence_db,
            "compressor": compressor,
            "reverb": reverb,
            "target_lufs": target_lufs,
        },
    }


def _number(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


def _mix_graph(params: dict[str, Any]) -> tuple[str, list[str]]:
    vocal_gain = f"volume={_number(params['vocal_db'])}dB"
    instrumental_gain = f"volume={_number(params['instrumental_db'])}dB"
    vocal_filters = [
        "aresample=48000",
        "aformat=sample_rates=48000:channel_layouts=stereo",
        vocal_gain,
    ]
    if params["highpass_hz"] > 0:
        vocal_filters.append(f"highpass=f={_number(params['highpass_hz'])}")
    if params["presence_db"] != 0:
        vocal_filters.append(
            f"equalizer=f=3000:t=q:w=1:g={_number(params['presence_db'])}"
        )
    if params["compressor"]:
        vocal_filters.append(
            "acompressor=threshold=0.125:ratio=3:attack=15:release=150:makeup=1"
        )
    if params["reverb"] > 0:
        depth = params["reverb"]
        decays = "|".join(
            _number(depth * factor) for factor in (0.70, 0.50, 0.35, 0.22)
        )
        vocal_filters.append(
            f"aecho=in_gain=1:out_gain=0.8:delays=70|140|210|280:decays={decays}"
        )
    # Finish voice effects before padding. Keeping the band first in amix
    # fixes the output length even when voice/reverb extends beyond the band.
    vocal_filters.append("apad")
    filters = [
        "[0:a:0]" + ",".join(vocal_filters) + "[vocal]",
        f"[1:a:0]aresample=48000,aformat=sample_rates=48000:"
        f"channel_layouts=stereo,{instrumental_gain}[band]",
        "[band][vocal]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[mix]",
    ]
    return ";".join(filters), vocal_filters


def run_mix_request(request: dict[str, Any]) -> dict[str, Any]:
    config = _mix_request(request)
    params = config["params"]
    output_dir: Path = config["output_dir"]
    premaster_path = output_dir / "mix-premaster.wav"
    master_wav_path = output_dir / "mix-master.wav"
    master_mp3_path = output_dir / "mix-master.mp3"
    result_path = output_dir / "result.json"
    reject_existing(
        [premaster_path, master_wav_path, master_mp3_path, result_path]
    )

    started = time.monotonic()
    graph, _vocal_filters = _mix_graph(params)
    premaster_command = (
        ffmpeg_prefix(config["ffmpeg"])
        + [
            "-i",
            str(config["vocal"]),
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            str(config["instrumental"]),
            "-filter_complex",
            graph,
            "-map",
            "[mix]",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-c:a",
            "pcm_f32le",
            "-map_metadata",
            "-1",
            "-n",
            str(premaster_path),
        ]
    )
    execute(premaster_command)

    analysis = loudness_analysis_filter(params["target_lufs"])
    analysis_command = (
        ffmpeg_prefix(config["ffmpeg"], "info")
        + [
            "-i",
            str(premaster_path),
            "-map",
            "0:a:0",
            "-af",
            analysis,
            "-f",
            "null",
            "-",
        ]
    )
    measurement = parse_loudness(execute(analysis_command))
    master_filter = loudnorm_filter(params["target_lufs"], measurement)
    split_master = f"{master_filter},asplit=2[wavout][mp3out]"
    master_command = (
        ffmpeg_prefix(config["ffmpeg"])
        + [
            "-i",
            str(premaster_path),
            "-filter_complex",
            f"[0:a:0]{split_master}",
            "-map",
            "[wavout]",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-c:a",
            "pcm_s24le",
            "-map_metadata",
            "-1",
            "-n",
            str(master_wav_path),
            "-map",
            "[mp3out]",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "320k",
            "-map_metadata",
            "-1",
            "-n",
            str(master_mp3_path),
        ]
    )
    execute(master_command)

    report = {
        "files": [
            str(premaster_path),
            str(master_wav_path),
            str(master_mp3_path),
        ],
        "engine": "ffmpeg",
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "filters": [graph],
        "loudness_measurement": measurement,
        "params": params,
        "master_filter": master_filter,
        "duration_policy": (
            "instrumental_first; vocal_apad; amix_duration_first; "
            "48 kHz stereo"
        ),
    }
    write_result_atomic(result_path, report)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: mix.py REQUEST.json", file=sys.stderr)
        return 2
    try:
        run_mix_request(read_request(args[0]))
    except Exception as exc:
        print(f"mix worker failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Local audio catalog and fixed FFmpeg operations for the cover workbench."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import uuid


def native_tool(name):
    bundled = Path(__file__).resolve().parents[2] / "native-tools" / "bin" / (name + (".exe" if os.name == "nt" else ""))
    return bundled if bundled.is_file() else Path(shutil.which(name) or name)


FFMPEG = native_tool("ffmpeg")
FFPROBE = native_tool("ffprobe")
MAX_UPLOAD_BYTES = 128 * 1024 * 1024
MAX_DURATION = 30 * 60
CAPABILITIES = {
    "convert": True,
    "trim": True,
    "pitch": True,
    "tempo": True,
    "normalize": True,
    "mix": True,
    "timbre_conversion": False,
    "voice_training": False,
    "stem_separation": False,
}


class AudioError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _number(value: object, label: str, low: float, high: float) -> float:
    if isinstance(value, bool):
        raise AudioError(f"{label} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise AudioError(f"{label} must be a finite number") from None
    if not math.isfinite(result) or not low <= result <= high:
        raise AudioError(f"{label} must be between {low:g} and {high:g}")
    return result


def _run(command: list[str], timeout: int) -> bytes:
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise AudioError("Audio processing timed out", 504) from None
    except OSError:
        raise AudioError("FFmpeg or FFprobe is unavailable", 503) from None
    if result.returncode:
        message = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise AudioError(message[-1][:300] if message else "Audio processing failed", 422)
    return result.stdout


def probe_audio(path: Path) -> dict:
    raw = _run([
        str(FFPROBE), "-v", "error", "-protocol_whitelist", "file,pipe",
        "-f", path.suffix[1:], "-show_entries",
        "format=duration:stream=codec_type,sample_rate,channels,duration",
        "-of", "json", str(path),
    ], 20)
    try:
        result = json.loads(raw)
        streams = result["streams"]
        if any(item.get("codec_type") == "video" for item in streams):
            raise AudioError("Video files are not supported")
        stream = next(item for item in streams if item.get("codec_type") == "audio")
        duration = _number(
            stream.get("duration") or result.get("format", {}).get("duration"),
            "duration", 0.001, MAX_DURATION,
        )
        sample_rate = int(stream["sample_rate"])
        channels = int(stream["channels"])
        if not 8000 <= sample_rate <= 192000 or not 1 <= channels <= 8:
            raise ValueError("unsupported audio layout")
    except (KeyError, StopIteration, TypeError, ValueError, json.JSONDecodeError):
        raise AudioError("File must contain supported audio", 422) from None
    return {"duration": duration, "channels": channels, "sample_rate": sample_rate}


class AudioWorkspace:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.data = self.root / "data"
        self.uploads = self.data / "uploads"
        self.outputs = self.data / "outputs"
        self.demo = self.data / "demo"
        for directory in (self.uploads, self.outputs, self.demo):
            directory.mkdir(parents=True, exist_ok=True)
        self.catalog_path = self.data / "catalog.json"
        self.lock = threading.RLock()
        self.process_lock = threading.Lock()
        self.catalog = self._load_catalog()

    def _load_catalog(self) -> dict[str, dict]:
        if not self.catalog_path.exists():
            return {}
        try:
            records = json.loads(self.catalog_path.read_text(encoding="utf-8"))
            if not isinstance(records, list):
                raise ValueError()
            return {record["id"]: record for record in records
                    if self._record_path(record).is_file()}
        except (OSError, ValueError, KeyError, TypeError):
            raise AudioError("Audio catalog is damaged", 500) from None

    def _record_path(self, record: dict) -> Path:
        section = record["section"]
        file_id = record["id"]
        extension = record["extension"]
        if section not in ("uploads", "outputs", "demo") or not re.fullmatch(r"[0-9a-f]{32}", file_id) or extension not in ("wav", "mp3"):
            raise AudioError("Invalid audio catalog entry", 500)
        path = self.data / section / f"{file_id}.{extension}"
        if not path.resolve().is_relative_to(self.data):
            raise AudioError("Invalid audio catalog path", 500)
        return path

    def _save_catalog(self) -> None:
        temporary = self.catalog_path.with_name(f"catalog-{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(list(self.catalog.values()), indent=2), encoding="utf-8")
            os.replace(temporary, self.catalog_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _add(self, path: Path, name: str, section: str, metadata: dict) -> dict:
        record = {
            "id": path.stem,
            "name": name,
            "section": section,
            "extension": path.suffix[1:],
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            **metadata,
        }
        previous = self.catalog.get(record["id"])
        self.catalog[record["id"]] = record
        try:
            self._save_catalog()
        except Exception:
            if previous is None:
                self.catalog.pop(record["id"], None)
            else:
                self.catalog[record["id"]] = previous
            raise
        return self._public(record)

    @staticmethod
    def _public(record: dict) -> dict:
        return {key: record[key] for key in ("id", "name", "duration", "channels", "sample_rate", "sha256")} | {
            "url": f"/api/audio/{record['id']}"
        }

    def state(self) -> dict:
        with self.lock:
            return {"files": [self._public(item) for item in self.catalog.values()],
                    "capabilities": CAPABILITIES.copy()}

    def file(self, file_id: str) -> tuple[Path, dict]:
        with self.lock:
            if not re.fullmatch(r"[0-9a-f]{32}", file_id or "") or file_id not in self.catalog:
                raise AudioError("Audio file not found", 404)
            record = self.catalog[file_id]
            path = self._record_path(record)
            if not path.is_file():
                raise AudioError("Audio file not found", 404)
            return path, record

    def upload(self, body: bytes, filename: str) -> dict:
        if not body or len(body) > MAX_UPLOAD_BYTES:
            raise AudioError("Audio upload must be between 1 byte and 128 MiB", 413)
        name = Path(filename.replace("\\", "/")).name.strip()
        extension = Path(name).suffix.lower().lstrip(".")
        if not name or len(name) > 150 or extension not in ("wav", "mp3"):
            raise AudioError("Upload a WAV or MP3 file")
        path = self.uploads / f"{uuid.uuid4().hex}.{extension}"
        try:
            with path.open("xb") as target:
                target.write(body)
            metadata = probe_audio(path)
            with self.lock:
                return self._add(path, name, "uploads", metadata)
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def process(self, request: dict) -> dict:
        if not self.process_lock.acquire(blocking=False):
            raise AudioError("Another audio operation is running", 409)
        try:
            return self._process(request)
        finally:
            self.process_lock.release()

    def _process(self, request: dict) -> dict:
        if not isinstance(request, dict):
            raise AudioError("Expected a JSON object")
        operation = request.get("operation")
        if operation not in ("convert", "trim", "pitch", "tempo", "normalize", "mix"):
            raise AudioError("Operation is unavailable", 422)
        output_format = request.get("format", "wav")
        if output_format not in ("wav", "mp3"):
            raise AudioError("Output format must be wav or mp3")
        source, source_record = self.file(request.get("file_id"))
        filters: list[str] = []
        inputs = ["-protocol_whitelist", "file,pipe", "-f", source.suffix[1:], "-i", str(source)]
        if operation == "trim":
            start = _number(request.get("start"), "start", 0, source_record["duration"])
            end = _number(request.get("end"), "end", 0, source_record["duration"])
            if end <= start:
                raise AudioError("end must be greater than start")
            filters = [f"atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS"]
        elif operation == "pitch":
            semitones = _number(request.get("semitones"), "semitones", -12, 12)
            factor = 2 ** (semitones / 12)
            rate = source_record["sample_rate"]
            filters = [f"asetrate={rate * factor:.6f},aresample={rate},atempo={1 / factor:.9f}"]
        elif operation == "tempo":
            speed = _number(request.get("tempo"), "tempo", 0.5, 2)
            filters = [f"atempo={speed:.9f}"]
        elif operation == "normalize":
            # Match the supplied client's default -19 LUFS / -1.5 dBTP / LRA11.
            analysis = subprocess.run([
                str(FFMPEG), "-hide_banner", "-nostdin", *inputs, "-af",
                "loudnorm=I=-19:TP=-1.5:LRA=11:print_format=json",
                "-f", "null", "-"], capture_output=True, timeout=180)
            if analysis.returncode:
                raise AudioError("Loudness analysis failed", 422)
            match = re.search(r'\{\s*"input_i"[\s\S]*?\}', analysis.stderr.decode("utf8", "replace"))
            if not match:
                raise AudioError("Loudness analysis produced no measurements", 422)
            measured = json.loads(match.group())
            keys = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
            if all(math.isfinite(float(measured[k])) for k in keys):
                filters = ["loudnorm=I=-19:TP=-1.5:LRA=11:linear=true:"
                           f"measured_I={measured['input_i']}:measured_TP={measured['input_tp']}:"
                           f"measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}:"
                           f"offset={measured['target_offset']}"]
            else:
                # All-silent recordings have undefined loudness; retain silence.
                filters = ["anull"]
        elif operation == "mix":
            other, _ = self.file(request.get("other_id"))
            vocal_gain = _number(request.get("vocal_gain", 1), "vocal_gain", 0, 4)
            other_gain = _number(request.get("other_gain", 1), "other_gain", 0, 4)
            inputs.extend(["-protocol_whitelist", "file,pipe", "-f", other.suffix[1:], "-i", str(other)])
            filters = [f"[0:a]volume={vocal_gain:.6f}[a];[1:a]volume={other_gain:.6f}[b];[a][b]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0[out]"]

        path = self.outputs / f"{uuid.uuid4().hex}.{output_format}"
        command = [str(FFMPEG), "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-threads", "2", *inputs]
        if operation == "mix":
            command.extend(["-filter_complex", filters[0], "-map", "[out]"])
        else:
            command.extend(["-map", "0:a:0"])
            if filters:
                command.extend(["-af", filters[0]])
        if output_format == "wav":
            command.extend(["-c:a", "pcm_s16le"])
        else:
            command.extend(["-c:a", "libmp3lame", "-b:a", "320k"])
        command.extend(["-ar", "44100", "-ac", "2"])
        command.append(str(path))
        try:
            _run(command, 180)
            metadata = probe_audio(path)
            with self.lock:
                public = self._add(path, f"{Path(source_record['name']).stem}-{operation}.{output_format}", "outputs", metadata)
            return {"file": public}
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def install_demo(self) -> dict:
        from make_demo import create_demo_audio

        with self.lock:
            if any(record["section"] == "demo" for record in self.catalog.values()):
                return self.state()
            files = create_demo_audio(self.demo)
            for path, name in files:
                self._add(path, name, "demo", probe_audio(path))
            return self.state()

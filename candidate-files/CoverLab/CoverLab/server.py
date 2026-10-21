"""Loopback-only HTTP UI and API for local audio editing."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import mimetypes
from pathlib import Path
import re
import sys
from instance_lock import WorkspaceLease, WorkspaceInUse
from urllib.parse import unquote, urlparse

from voice_jobs import VoiceJobs
from core import AudioError, AudioWorkspace, CAPABILITIES, MAX_UPLOAD_BYTES


ROOT = Path(__file__).resolve().parent
STATIC_FILES = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/style.css": "style.css", "/voice.js": "voice.js"}


class WorkspaceHTTPServer(ThreadingHTTPServer):
    # server_close must drain requests (and their synchronous FFmpeg children).
    daemon_threads = False
    block_on_close = True

    def get_request(self):
        connection, address = super().get_request()
        # Bound idle/incomplete requests, without timing out audio computation.
        connection.settimeout(5)
        return connection, address


class LocalHandler(BaseHTTPRequestHandler):
    workspace: AudioWorkspace
    voice: VoiceJobs
    protocol_version = "HTTP/1.1"

    def _local_request(self) -> bool:
        host = self.headers.get("Host", "")
        if not re.fullmatch(r"(?:127\.0\.0\.1|localhost)(?::[0-9]{1,5})?", host):
            return False
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlparse(origin)
            if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost") or parsed.port != self.server.server_port:
                return False
        return True

    def _send(self, status: int, body: bytes, content_type: str = "application/json; charset=utf-8", extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: dict) -> None:
        self._send(status, json.dumps(data).encode("utf-8"))

    def _body(self, limit: int) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise AudioError("Content-Length is required", 411) from None
        if length < 1 or length > limit:
            raise AudioError("Request body is empty or too large", 413)
        body = self.rfile.read(length)
        if len(body) != length:
            raise AudioError("Incomplete request body")
        return body

    def _handle(self, action) -> None:
        try:
            if not self._local_request():
                raise AudioError("Local requests only", 403)
            action()
        except AudioError as error:
            self._json(error.status, {"error": str(error)})
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"error": "Invalid request"})
        except Exception:
            self._json(500, {"error": "Internal server error"})

    def do_GET(self) -> None:
        self._handle(self._get)

    def _get(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/state":
            self._json(200, self.workspace.state())
        elif path == "/api/voice":
            self._json(200, self.voice.recent())
        elif re.fullmatch(r"/api/voice/[0-9a-f]{32}", path):
            self._json(200, self.voice.get(path.rsplit("/", 1)[-1]))
        elif path == "/api/capabilities":
            self._json(200, {"capabilities": CAPABILITIES})
        elif re.fullmatch(r"/api/audio/[0-9a-f]{32}", path):
            self._audio(path.rsplit("/", 1)[-1])
        elif path in STATIC_FILES:
            file_path = ROOT / STATIC_FILES[path]
            if not file_path.is_file():
                raise AudioError("Page not found", 404)
            mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
            self._send(200, file_path.read_bytes(), f"{mime}; charset=utf-8")
        else:
            raise AudioError("Not found", 404)

    def _audio(self, file_id: str) -> None:
        path, record = self.workspace.file(file_id)
        size = path.stat().st_size
        start, end = 0, size - 1
        range_header = self.headers.get("Range")
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match or (not match[1] and not match[2]):
                raise AudioError("Invalid byte range", 416)
            if match[1]:
                start = int(match[1])
                end = int(match[2]) if match[2] else size - 1
            else:
                suffix = int(match[2])
                start = max(0, size - suffix)
            if start >= size or end < start or end >= size:
                raise AudioError("Invalid byte range", 416)
        length = end - start + 1
        mime = "audio/wav" if record["extension"] == "wav" else "audio/mpeg"
        self.send_response(206 if range_header else 200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if range_header:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as source:
            source.seek(start)
            remaining = length
            while remaining:
                chunk = source.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_POST(self) -> None:
        self._handle(self._post)

    def _post(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/upload":
            filename = unquote(self.headers.get("X-Filename", ""))
            public = self.workspace.upload(self._body(MAX_UPLOAD_BYTES), filename)
            self._json(201, {"file": public})
        elif re.fullmatch(r"/api/voice/[0-9a-f]{32}/cancel", path):
            self._json(202, self.voice.cancel(path.split("/")[-2]))
        elif path == "/api/voice":
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise AudioError("Expected application/json", 415)
            self._json(202, self.voice.start(json.loads(self._body(16384))))
        elif path == "/api/process":
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise AudioError("Expected application/json", 415)
            request = json.loads(self._body(16384))
            self._json(200, self.workspace.process(request))
        elif path == "/api/demo":
            self._json(200, self.workspace.install_demo())
        else:
            raise AudioError("Not found", 404)


def serve(port: int = 8877, root: Path = ROOT, open_browser: bool = False) -> None:
    with WorkspaceLease(root) as lease:
        workspace = AudioWorkspace(root)
        voice = VoiceJobs(workspace)
        server = None
        server_closed = False
        lease.retain_until_process_exit()
        try:
            CAPABILITIES["timbre_conversion"] = voice.available()
            CAPABILITIES["mixed_audio_conversion"] = voice.mixed_available()
            handler = type("WorkspaceHandler", (LocalHandler,), {"workspace": workspace, "voice": voice})
            server = WorkspaceHTTPServer(("127.0.0.1", port), handler)
            try:
                print(f"Local audio workbench: http://127.0.0.1:{server.server_port}", flush=True)
                if open_browser:
                    import webbrowser
                    try:
                        if not webbrowser.open(f"http://127.0.0.1:{server.server_port}", new=2):
                            print("浏览器未自动打开，请使用上面的本机地址。", flush=True)
                    except (OSError, webbrowser.Error):
                        print("浏览器未自动打开，请使用上面的本机地址。", flush=True)
                server.serve_forever()
            finally:
                server.server_close()
                server_closed = True
        finally:
            if voice.close() is True and (server is None or server_closed):
                lease.allow_release()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the local audio workbench")
    parser.add_argument("--port", type=int, default=8877)
    parser.add_argument("--open-browser", action="store_true", help="Open the bound loopback page in the default browser")
    arguments = parser.parse_args()
    try:
        serve(arguments.port, open_browser=arguments.open_browser)
    except WorkspaceInUse as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
    except KeyboardInterrupt:
        pass

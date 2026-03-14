from __future__ import annotations

import json
import mimetypes
import os
import re
import socket
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse


BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = BASE_DIR / "templates" / "index.html"
ALLOWED_EXTENSIONS = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi"}
RANGE_PATTERN = re.compile(r"bytes=(\d*)-(\d*)")
QUALITY_LABEL_PATTERN = re.compile(r"(?<!\d)(2160p|1440p|1080p|720p|480p|360p|240p|4k)(?!\d)", re.IGNORECASE)
VARIANT_TOKEN_PATTERN = re.compile(
    r"\b(?:2160p|1440p|1080p|720p|480p|360p|240p|4k|web[\s._-]?dl|web[\s._-]?rip|bluray|brrip|hdrip|hdtc|hdts|hdcam|hevc|x264|x265|h264|h265|aac|dd5[\s._-]?1|esub|multi[\s._-]?audio|dual[\s._-]?audio)\b",
    re.IGNORECASE,
)
CHUNK_SIZE = 1024 * 1024
UPLOAD_CHUNK_SIZE = 1024 * 1024


def get_video_dir() -> Path:
    raw_video_dir = os.environ.get("VIDEO_DIR")
    if raw_video_dir:
        return Path(raw_video_dir).expanduser().resolve()
    return (BASE_DIR / "videos").resolve()


VIDEO_DIR = get_video_dir()


def discover_ipv4_addresses() -> list[str]:
    addresses: set[str] = set()

    try:
        hostname = socket.gethostname()
        for _, _, _, _, sockaddr in socket.getaddrinfo(
            hostname,
            None,
            family=socket.AF_INET,
            type=socket.SOCK_STREAM,
        ):
            ip_address = sockaddr[0]
            if not ip_address.startswith(("127.", "169.254.")):
                addresses.add(ip_address)
    except socket.gaierror:
        pass

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe_socket:
            probe_socket.connect(("8.8.8.8", 80))
            ip_address = probe_socket.getsockname()[0]
            if not ip_address.startswith(("127.", "169.254.")):
                addresses.add(ip_address)
    except OSError:
        pass

    return sorted(addresses)


def normalize_filename(raw_name: str) -> str | None:
    filename = Path(unquote(raw_name)).name.strip()
    if not filename or filename in {".", ".."}:
        return None
    return filename


def detect_quality_label(file_name: str) -> str:
    match = QUALITY_LABEL_PATTERN.search(Path(file_name).stem)
    if match:
        return match.group(1).lower().replace("k", "K")
    return "Original"


def build_variant_key(file_name: str) -> str:
    normalized_stem = re.sub(r"[._-]+", " ", Path(file_name).stem.lower())
    normalized_stem = VARIANT_TOKEN_PATTERN.sub(" ", normalized_stem)
    normalized_stem = re.sub(r"\s+", " ", normalized_stem).strip()
    return normalized_stem or Path(file_name).stem.lower()


def list_videos() -> list[dict[str, str]]:
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    videos = []

    for file_path in sorted(VIDEO_DIR.iterdir(), key=lambda path: path.name.lower()):
        if not file_path.is_file() or file_path.suffix.lower() not in ALLOWED_EXTENSIONS:
            continue

        mime_type, _ = mimetypes.guess_type(file_path.name)
        videos.append(
            {
                "name": file_path.name,
                "size_mb": f"{file_path.stat().st_size / (1024 * 1024):.1f}",
                "mime_type": mime_type or "application/octet-stream",
                "url": f"/videos/{quote(file_path.name)}",
                "download_url": f"/videos/{quote(file_path.name)}?download=1",
                "quality_label": detect_quality_label(file_path.name),
                "variant_key": build_variant_key(file_path.name),
            }
        )

    return videos


def resolve_video_path(raw_name: str) -> Path | None:
    filename = normalize_filename(raw_name)
    if filename is None:
        return None

    candidate = (VIDEO_DIR / filename).resolve()

    try:
        candidate.relative_to(VIDEO_DIR.resolve())
    except ValueError:
        return None

    if not candidate.is_file() or candidate.suffix.lower() not in ALLOWED_EXTENSIONS:
        return None

    return candidate


class VideoRequestHandler(BaseHTTPRequestHandler):
    server_version = "VideoServer/1.0"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/":
            self.serve_index()
            return

        if parsed.path == "/api/videos":
            self.serve_video_library()
            return

        if parsed.path.startswith("/videos/"):
            self.serve_video(parsed.path.removeprefix("/videos/"), parsed.query)
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/api/upload":
            self.handle_upload()
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def serve_index(self) -> None:
        if not TEMPLATE_PATH.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Template not found")
            return

        content = TEMPLATE_PATH.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def serve_video_library(self) -> None:
        payload = json.dumps(
            {
                "videos": list_videos(),
                "video_dir": str(VIDEO_DIR),
            }
        ).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_upload(self) -> None:
        filename = normalize_filename(self.headers.get("X-Filename", ""))
        if filename is None:
            self.send_error(HTTPStatus.BAD_REQUEST, "Missing or invalid filename")
            return

        if Path(filename).suffix.lower() not in ALLOWED_EXTENSIONS:
            self.send_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Unsupported video format")
            return

        content_length_header = self.headers.get("Content-Length")
        if content_length_header is None:
            self.send_error(HTTPStatus.LENGTH_REQUIRED, "Missing Content-Length")
            return

        try:
            content_length = int(content_length_header)
        except ValueError:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
            return

        if content_length < 0:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
            return

        VIDEO_DIR.mkdir(parents=True, exist_ok=True)
        final_path = VIDEO_DIR / filename
        temp_path = VIDEO_DIR / f".{filename}.uploading"
        existed_before = final_path.exists()
        remaining = content_length

        try:
            with temp_path.open("wb") as output_file:
                while remaining > 0:
                    chunk = self.rfile.read(min(UPLOAD_CHUNK_SIZE, remaining))
                    if not chunk:
                        raise ConnectionError("Upload ended before the request body was fully received")
                    output_file.write(chunk)
                    remaining -= len(chunk)

            temp_path.replace(final_path)
        except Exception as exc:
            temp_path.unlink(missing_ok=True)
            self.send_error(HTTPStatus.BAD_REQUEST, f"Upload failed: {exc}")
            return

        self.send_json(
            HTTPStatus.OK if existed_before else HTTPStatus.CREATED,
            {
                "uploaded": True,
                "filename": filename,
                "size_bytes": final_path.stat().st_size,
                "video_dir": str(VIDEO_DIR),
            },
        )

    def serve_video(self, raw_name: str, query_string: str = "") -> None:
        file_path = resolve_video_path(raw_name)
        if file_path is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Video not found")
            return

        query_params = parse_qs(query_string)
        should_download = query_params.get("download", ["0"])[0].lower() in {"1", "true", "yes"}
        file_size = file_path.stat().st_size
        content_type, _ = mimetypes.guess_type(file_path.name)
        content_type = content_type or "application/octet-stream"
        range_header = self.headers.get("Range")

        if range_header:
            match = RANGE_PATTERN.fullmatch(range_header.strip())
            if not match:
                self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, "Invalid Range")
                return

            start_group, end_group = match.groups()
            if not start_group and not end_group:
                self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, "Invalid Range")
                return

            if start_group:
                start = int(start_group)
                end = int(end_group) if end_group else file_size - 1
            else:
                suffix_length = int(end_group)
                if suffix_length <= 0:
                    self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, "Invalid Range")
                    return
                start = max(file_size - suffix_length, 0)
                end = file_size - 1

            if start >= file_size or end < start:
                self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, "Invalid Range")
                return

            end = min(end, file_size - 1)
            length = end - start + 1

            self.send_response(HTTPStatus.PARTIAL_CONTENT)
            self.send_header("Content-Type", content_type)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
            self.send_header("Content-Length", str(length))
            if should_download:
                self.send_header("Content-Disposition", f'attachment; filename="{file_path.name}"')
            self.end_headers()
            self.stream_file(file_path, start, length)
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(file_size))
        if should_download:
            self.send_header("Content-Disposition", f'attachment; filename="{file_path.name}"')
        self.end_headers()
        self.stream_file(file_path, 0, file_size)

    def stream_file(self, file_path: Path, start: int, length: int) -> None:
        remaining = length
        with file_path.open("rb") as video_file:
            # Use the socket fast path when available to reduce copy overhead for large media.
            if hasattr(self.connection, "sendfile"):
                try:
                    self.wfile.flush()
                    self.connection.sendfile(video_file, offset=start, count=length)
                    return
                except OSError:
                    video_file.seek(start)
            else:
                video_file.seek(start)

            while remaining > 0:
                chunk = video_file.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def log_message(self, format: str, *args) -> None:
        return


def run_server() -> None:
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    httpd = ThreadingHTTPServer((host, port), VideoRequestHandler)
    print(f"Serving videos from {VIDEO_DIR}")

    if host in {"0.0.0.0", "::"}:
        print(f"Local:      http://127.0.0.1:{port}")
        lan_addresses = discover_ipv4_addresses()
        if lan_addresses:
            for lan_address in lan_addresses:
                print(f"Mobile/LAN: http://{lan_address}:{port}")
        else:
            print("Mobile/LAN: No IPv4 LAN address detected")
    else:
        print(f"Open:       http://{host}:{port}")

    httpd.serve_forever()


if __name__ == "__main__":
    run_server()

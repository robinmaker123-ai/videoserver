from __future__ import annotations

import json
import mimetypes
import os
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse


BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = BASE_DIR / "templates" / "index.html"
ALLOWED_EXTENSIONS = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi"}
RANGE_PATTERN = re.compile(r"bytes=(\d*)-(\d*)")
CHUNK_SIZE = 64 * 1024


def get_video_dir() -> Path:
    raw_video_dir = os.environ.get("VIDEO_DIR")
    if raw_video_dir:
        return Path(raw_video_dir).expanduser().resolve()
    return (BASE_DIR / "videos").resolve()


VIDEO_DIR = get_video_dir()


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
            }
        )

    return videos


def resolve_video_path(raw_name: str) -> Path | None:
    filename = unquote(raw_name)
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

    def do_GET(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/":
            self.serve_index()
            return

        if parsed.path == "/api/videos":
            self.serve_video_library()
            return

        if parsed.path.startswith("/videos/"):
            self.serve_video(parsed.path.removeprefix("/videos/"))
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

    def serve_video(self, raw_name: str) -> None:
        file_path = resolve_video_path(raw_name)
        if file_path is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Video not found")
            return

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
            self.end_headers()
            self.stream_file(file_path, start, length)
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(file_size))
        self.end_headers()
        self.stream_file(file_path, 0, file_size)

    def stream_file(self, file_path: Path, start: int, length: int) -> None:
        remaining = length
        with file_path.open("rb") as video_file:
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
    print(f"Serving videos from {VIDEO_DIR} at http://127.0.0.1:{port}")
    httpd.serve_forever()


if __name__ == "__main__":
    run_server()

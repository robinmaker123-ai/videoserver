from __future__ import annotations

import json
import mimetypes
import os
import re
import socket
import subprocess
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse


BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = BASE_DIR / "templates" / "index.html"
CONFIG_PATH = BASE_DIR / "videoserver.json"
ALLOWED_EXTENSIONS = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi"}
RANGE_PATTERN = re.compile(r"bytes=(\d*)-(\d*)")
QUALITY_LABEL_PATTERN = re.compile(r"(?<!\d)(2160p|1440p|1080p|720p|480p|360p|240p|4k)(?!\d)", re.IGNORECASE)
VARIANT_TOKEN_PATTERN = re.compile(
    r"\b(?:2160p|1440p|1080p|720p|480p|360p|240p|4k|web[\s._-]?dl|web[\s._-]?rip|bluray|brrip|hdrip|hdtc|hdts|hdcam|hevc|x264|x265|h264|h265|aac|dd5[\s._-]?1|esub|multi[\s._-]?audio|dual[\s._-]?audio)\b",
    re.IGNORECASE,
)
SEARCH_TEXT_PATTERN = re.compile(r"[^\w]+", re.UNICODE)
CHUNK_SIZE = 1024 * 1024
UPLOAD_CHUNK_SIZE = 1024 * 1024
VIDEO_DIR_ENV_OVERRIDE = os.environ.get("VIDEO_DIR")


def load_saved_video_dir() -> Path | None:
    if not CONFIG_PATH.is_file():
        return None

    try:
        payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    raw_video_dir = payload.get("video_dir")
    if not isinstance(raw_video_dir, str) or not raw_video_dir.strip():
        return None

    return Path(raw_video_dir).expanduser().resolve()


def get_video_dir() -> Path:
    if VIDEO_DIR_ENV_OVERRIDE:
        return Path(VIDEO_DIR_ENV_OVERRIDE).expanduser().resolve()

    saved_video_dir = load_saved_video_dir()
    if saved_video_dir is not None:
        return saved_video_dir

    return (BASE_DIR / "videos").resolve()


VIDEO_DIR = get_video_dir()


def normalize_video_dir(raw_path: str) -> Path:
    path_text = raw_path.strip()
    if not path_text:
        raise ValueError("Enter a folder path like D:\\Movies")

    video_dir = Path(path_text).expanduser().resolve()
    video_dir.mkdir(parents=True, exist_ok=True)

    if not video_dir.is_dir():
        raise NotADirectoryError(f"Not a folder: {video_dir}")

    return video_dir


def save_video_dir(video_dir: Path) -> None:
    CONFIG_PATH.write_text(json.dumps({"video_dir": str(video_dir)}, indent=2), encoding="utf-8")


def set_video_dir(raw_path: str, persist: bool = True) -> Path:
    global VIDEO_DIR

    video_dir = normalize_video_dir(raw_path)
    VIDEO_DIR = video_dir

    if persist:
        save_video_dir(video_dir)

    return video_dir


def open_directory(path: Path) -> None:
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
        return

    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
        return

    subprocess.Popen(["xdg-open", str(path)])


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


def normalize_search_text(raw_text: str) -> str:
    normalized_text = SEARCH_TEXT_PATTERN.sub(" ", raw_text.casefold())
    return re.sub(r"\s+", " ", normalized_text).strip()


def tokenize_search_query(raw_query: str) -> list[str]:
    normalized_query = normalize_search_text(raw_query)
    return normalized_query.split() if normalized_query else []


def build_search_blob(file_name: str) -> str:
    return " ".join(
        part
        for part in (
            file_name.casefold(),
            Path(file_name).stem.casefold(),
            normalize_search_text(file_name),
            build_variant_key(file_name),
            detect_quality_label(file_name).casefold(),
        )
        if part
    )


def video_matches_query(file_name: str, search_tokens: list[str]) -> bool:
    if not search_tokens:
        return True

    search_blob = build_search_blob(file_name)
    return all(search_token in search_blob for search_token in search_tokens)


def list_videos(search_query: str = "") -> tuple[list[dict[str, str]], int]:
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    videos = []
    total_videos = 0
    search_tokens = tokenize_search_query(search_query)

    for file_path in sorted(VIDEO_DIR.iterdir(), key=lambda path: path.name.lower()):
        if not file_path.is_file() or file_path.suffix.lower() not in ALLOWED_EXTENSIONS:
            continue

        total_videos += 1
        if not video_matches_query(file_path.name, search_tokens):
            continue

        mime_type, _ = mimetypes.guess_type(file_path.name)
        videos.append(
            {
                "name": file_path.name,
                "size_mb": f"{file_path.stat().st_size / (1024 * 1024):.1f}",
                "mime_type": mime_type or "application/octet-stream",
                "url": f"/videos/{quote(file_path.name)}",
                "download_url": f"/videos/{quote(file_path.name)}?download=1",
                "delete_url": f"/api/videos/{quote(file_path.name)}",
                "quality_label": detect_quality_label(file_path.name),
                "variant_key": build_variant_key(file_path.name),
            }
        )

    return videos, total_videos


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
            self.serve_video_library(parsed.query)
            return

        if parsed.path.startswith("/videos/"):
            self.serve_video(parsed.path.removeprefix("/videos/"), parsed.query)
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/api/video-dir":
            self.handle_set_video_dir()
            return

        if parsed.path == "/api/open-video-dir":
            self.handle_open_video_dir()
            return

        if parsed.path == "/api/upload":
            self.handle_upload()
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path.startswith("/api/videos/"):
            self.handle_delete_video(parsed.path.removeprefix("/api/videos/"))
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

    def serve_video_library(self, query_string: str = "") -> None:
        query_params = parse_qs(query_string)
        raw_query = query_params.get("q", [""])[0].strip()
        videos, total_videos = list_videos(raw_query)
        payload = json.dumps(
            {
                "videos": videos,
                "video_dir": str(VIDEO_DIR),
                "query": raw_query,
                "total_videos": total_videos,
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

    def read_json_body(self) -> dict[str, object] | None:
        content_length_header = self.headers.get("Content-Length")
        if content_length_header is None:
            self.send_json(HTTPStatus.LENGTH_REQUIRED, {"error": "Missing Content-Length"})
            return None

        try:
            content_length = int(content_length_header)
        except ValueError:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid Content-Length"})
            return None

        if content_length < 0:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid Content-Length"})
            return None

        try:
            body = self.rfile.read(content_length)
            payload = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON body"})
            return None

        if not isinstance(payload, dict):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "JSON body must be an object"})
            return None

        return payload

    def handle_set_video_dir(self) -> None:
        payload = self.read_json_body()
        if payload is None:
            return

        raw_video_dir = payload.get("video_dir")
        if not isinstance(raw_video_dir, str) or not raw_video_dir.strip():
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Enter a folder path like D:\\Movies"})
            return

        persist = VIDEO_DIR_ENV_OVERRIDE is None

        try:
            video_dir = set_video_dir(raw_video_dir, persist=persist)
        except (OSError, ValueError) as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        self.send_json(
            HTTPStatus.OK,
            {
                "updated": True,
                "video_dir": str(video_dir),
                "persisted": persist,
                "env_override": bool(VIDEO_DIR_ENV_OVERRIDE),
            },
        )

    def handle_open_video_dir(self) -> None:
        VIDEO_DIR.mkdir(parents=True, exist_ok=True)

        try:
            open_directory(VIDEO_DIR)
        except OSError as exc:
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"Could not open folder: {exc}"})
            return

        self.send_json(
            HTTPStatus.OK,
            {
                "opened": True,
                "video_dir": str(VIDEO_DIR),
            },
        )

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

    def handle_delete_video(self, raw_name: str) -> None:
        file_path = resolve_video_path(raw_name)
        if file_path is None:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Video not found"})
            return

        try:
            file_path.unlink()
        except PermissionError:
            self.send_json(
                HTTPStatus.CONFLICT,
                {"error": "Video is currently in use. Pause playback and try again."},
            )
            return
        except OSError as exc:
            self.send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"Could not delete video: {exc}"},
            )
            return

        self.send_json(
            HTTPStatus.OK,
            {
                "deleted": True,
                "filename": file_path.name,
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

#!/usr/bin/env python3
"""Local web UI for previewing slide projects and starting render jobs."""

from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import html
import io
import json
import os
import re
import shutil
import shlex
import signal
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
import webbrowser
import zipfile
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import Request, urlopen

from social_upload import (
    build_upload_metadata,
    facebook_comment_source,
    facebook_upload_video,
    finish_youtube_oauth,
    set_facebook_active_page,
    set_youtube_active_channel,
    social_status,
    start_youtube_oauth,
    update_facebook_page_config,
    youtube_upload_video,
)
import social_upload.metadata as social_metadata
from tts.elevenlabs import (
    elevenlabs_api_key,
    elevenlabs_auth_configured,
    elevenlabs_config,
    elevenlabs_public_config,
    elevenlabs_proxy_base_url,
    elevenlabs_proxy_key,
    elevenlabs_voice_id,
    update_elevenlabs_api_key,
    update_elevenlabs_proxy_config,
    update_elevenlabs_voice_id,
)


if sys.platform.startswith("win"):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


REPO_ROOT = Path(__file__).resolve().parent
CONFIG_ROOT = REPO_ROOT / "config"
CONNECTIONS_CONFIG = CONFIG_ROOT / "connections.json"
DEFAULT_SLIDE_ROOT = REPO_ROOT / "slide"
TEMPLATE_ROOT = REPO_ROOT / "template"
PROJECT_METADATA_FILENAME = "project.json"
TEMPLATE_METADATA_FILENAME = "template.json"
STUDIO_STATE_FILENAME = "studio-state.json"
SLIDE_ROOT = DEFAULT_SLIDE_ROOT
SOURCE_ROOT_IS_PROJECT = False
VENV_PYTHON = REPO_ROOT / ".venv" / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")
RENDER_PYTHON_OVERRIDE = os.environ.get("VIRO_RENDER_PYTHON")
RENDER_PYTHON = Path(RENDER_PYTHON_OVERRIDE) if RENDER_PYTHON_OVERRIDE else (VENV_PYTHON if VENV_PYTHON.exists() else Path(sys.executable))
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8765
BUILTIN_ELEVENLABS_ROTATOR_PATH = "/api/rotator/elevenlabs"
BUILTIN_ELEVENLABS_ROTATOR_KEY = "viro-local-rotator"
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
MAX_LOG_CHARS = 240_000
SENSITIVE_CLI_FLAGS = {"--api-key", "--proxy-key", "--token", "--password", "--secret"}
MAX_TEMPLATE_ARCHIVE_BYTES = 80 * 1024 * 1024
MAX_TEMPLATE_ARCHIVE_UNCOMPRESSED_BYTES = 260 * 1024 * 1024
MAX_TEMPLATE_ARCHIVE_FILES = 2500
TEMPLATE_ARCHIVE_MANIFEST = "viro-template-export.json"
TEMPLATE_REQUIRED_FILES = {"index.html", "style.css", "app.js"}
TEMPLATE_ARCHIVE_EXCLUDED_DIRS = {".git", "__pycache__", "node_modules", "output", "test-results", ".pytest_cache"}
TEMPLATE_ARCHIVE_EXCLUDED_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}
RENDER_HISTORY_DIRNAME = "renders"
AUDIO_UPLOAD_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".webm", ".flac"}
SLIDE_MEDIA_UPLOAD_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".webm", ".mov"}
SLIDE_MEDIA_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
SLIDE_MEDIA_VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov"}
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
ROTATOR_LOCK = threading.Lock()
ROTATOR_INDEX = {"elevenlabs": 0}
ACTIVE_JOB_STATUSES = {"queued", "running", "cancelling"}
PRIVATE_JOB_FIELDS = {"process"}
SLIDE_AUDIO_SETTING_KEYS = {
    "transitionSounds": "slideTransitions",
    "revealSounds": "slideReveals",
}
CONNECTION_PROVIDERS = {
    "elevenlabs": "ElevenLabs",
    "apikeyrotator": "APIKeyRotator",
    "youtube": "YouTube",
    "facebook": "Facebook",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "custom": "Custom",
}
CONNECTION_KINDS = {
    "api_key": "API key",
    "oauth": "OAuth",
    "page_token": "Page token",
    "proxy_key": "Proxy key",
    "cookie": "Cookie",
    "custom": "Custom",
}
CONNECTION_PROVIDER_HEALTH_ENDPOINTS = {
    "elevenlabs": "https://api.elevenlabs.io/v1/user",
    "openai": "https://api.openai.com/v1/models",
    "anthropic": "https://api.anthropic.com/v1/models",
}
SUPPORTED_LANGUAGES = {"vi", "en"}
REQUEST_CONTEXT = threading.local()


ANSI_ENABLED = os.environ.get("NO_COLOR") is None and (sys.stdout.isatty() or sys.stderr.isatty())
ANSI = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "green": "\033[32m",
    "cyan": "\033[36m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "magenta": "\033[35m",
    "blue": "\033[34m",
    "underline": "\033[4m",
} if ANSI_ENABLED else {key: "" for key in ["reset", "bold", "dim", "green", "cyan", "yellow", "red", "magenta", "blue", "underline"]}


def color_text(text: object, *styles: str) -> str:
    return "".join(ANSI.get(style, "") for style in styles) + str(text) + ANSI["reset"]


def json_dumps(data: object) -> bytes:
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


def normalize_language(value: object) -> str:
    lang = str(value or "").strip().lower()
    if lang in {"en-us", "en_gb", "english"}:
        return "en"
    if lang in {"vi-vn", "vn", "vietnamese", "tieng-viet"}:
        return "vi"
    return lang if lang in SUPPORTED_LANGUAGES else "vi"


def active_language() -> str:
    return normalize_language(getattr(REQUEST_CONTEXT, "language", "vi"))


def tx(vi: str, en: str) -> str:
    return en if active_language() == "en" else vi


def request_language(headers, parsed) -> str:
    query_lang = (parse_qs(parsed.query).get("lang") or [""])[0]
    if query_lang:
        return normalize_language(query_lang)
    cookie_header = headers.get("Cookie") or ""
    if cookie_header:
        try:
            cookies = SimpleCookie(cookie_header)
            if "viro_lang" in cookies:
                return normalize_language(cookies["viro_lang"].value)
        except Exception:
            pass
    return "vi"


def configure_source_root(path: str | Path | None) -> None:
    global SLIDE_ROOT, SOURCE_ROOT_IS_PROJECT
    raw_path = Path(path).expanduser() if path else DEFAULT_SLIDE_ROOT
    source_root = raw_path.resolve()
    if not source_root.is_dir():
        raise FileNotFoundError(f"Source root not found: {source_root}")

    SLIDE_ROOT = source_root
    SOURCE_ROOT_IS_PROJECT = (SLIDE_ROOT / "index.html").is_file()
    social_metadata.SLIDE_ROOT = project_lookup_root()


def project_lookup_root() -> Path:
    return SLIDE_ROOT.parent if SOURCE_ROOT_IS_PROJECT else SLIDE_ROOT


def source_root_mode() -> str:
    return "single-project" if SOURCE_ROOT_IS_PROJECT else "collection"


def source_root_mode_for(path: Path) -> str:
    return "single-project" if (path / "index.html").is_file() else "collection"


def source_root_project_count(path: Path) -> int:
    if not path.is_dir():
        return 0
    if (path / "index.html").is_file():
        return 1
    return sum(1 for child in path.iterdir() if child.is_dir() and (child / "index.html").is_file())


def require_project_collection_source(path: Path) -> None:
    if source_root_mode_for(path) == "single-project":
        raise ValueError("Hãy chọn folder chứa các project, không chọn trực tiếp một project đơn.")


def source_root_candidates() -> list[tuple[str, Path]]:
    raw_candidates = [
        ("Slide", DEFAULT_SLIDE_ROOT),
        ("Template", REPO_ROOT / "template"),
        ("Current", SLIDE_ROOT),
    ]
    cwd = Path.cwd().resolve()
    if cwd != REPO_ROOT and (cwd / "index.html").is_file():
        raw_candidates.append(("Current working folder", cwd))

    seen: set[Path] = set()
    candidates: list[tuple[str, Path]] = []
    for label, path in raw_candidates:
        resolved = path.expanduser().resolve()
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        candidates.append((label, resolved))
    return candidates


def source_root_option_payload() -> list[dict]:
    return [
        {
            "label": label,
            "path": str(path),
            "mode": source_root_mode_for(path),
            "projects": source_root_project_count(path),
            "active": path == SLIDE_ROOT,
        }
        for label, path in source_root_candidates()
    ]


def iter_project_dirs() -> list[Path]:
    if not SLIDE_ROOT.exists():
        return []
    if SOURCE_ROOT_IS_PROJECT:
        return [SLIDE_ROOT]
    return [path for path in SLIDE_ROOT.iterdir() if path.is_dir()]


def validate_project_name(project: str) -> str:
    project = unquote(str(project or "")).strip()
    if not project or project in {".", ".."} or "/" in project or "\\" in project or "\x00" in project:
        raise ValueError("Invalid project name.")
    return project


def slugify_project_name(value: object) -> str:
    text = str(value or "").strip()
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return text[:72].strip("-") or f"project-{time.strftime('%Y%m%d-%H%M%S')}"


def unique_project_slug(base_slug: str) -> str:
    base_slug = slugify_project_name(base_slug)
    if SOURCE_ROOT_IS_PROJECT:
        raise RuntimeError("Source root đang là một project đơn. Hãy đổi sang folder chứa nhiều project trước khi tạo project mới.")
    for index in range(1, 1000):
        slug = base_slug if index == 1 else f"{base_slug}-{index}"
        if not (SLIDE_ROOT / slug).exists():
            return slug
    raise RuntimeError("Không tìm được tên folder project còn trống.")


def render_history_url(project: str, filename: str) -> str:
    return project_url(project) + quote_relative_url(f"output/{RENDER_HISTORY_DIRNAME}/{filename}")


def list_render_versions(project_dir: Path, limit: int = 20) -> list[dict]:
    history_dir = project_dir / "output" / RENDER_HISTORY_DIRNAME
    if not history_dir.is_dir():
        return []
    versions = []
    for path in sorted(history_dir.glob("*.mp4"), key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True):
        if not path.is_file():
            continue
        stat = path.stat()
        versions.append(
            {
                "name": path.name,
                "path": str(path),
                "url": render_history_url(project_dir.name, path.name),
                "size": stat.st_size,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime)),
            }
        )
        if len(versions) >= limit:
            break
    return versions


def slugify_template_name(value: object) -> str:
    text = str(value or "").strip()
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return text[:72].strip("-") or f"template-{time.strftime('%Y%m%d-%H%M%S')}"


def unique_template_slug(base_slug: str) -> str:
    base_slug = slugify_template_name(base_slug)
    for index in range(1, 1000):
        slug = base_slug if index == 1 else f"{base_slug}-{index}"
        if not (TEMPLATE_ROOT / slug).exists():
            return slug
    raise RuntimeError("Không tìm được tên folder template còn trống.")


def project_url(project: str) -> str:
    return f"/slide/{quote(project)}/"


def project_metadata_path(project_dir: Path) -> Path:
    return project_dir / PROJECT_METADATA_FILENAME


def read_project_metadata(project_dir: Path) -> dict:
    path = project_metadata_path(project_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def write_project_metadata(project_dir: Path, metadata: dict) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    project_metadata_path(project_dir).write_bytes(json_dumps(metadata) + b"\n")


def source_root_response() -> dict:
    return {
        "source_root": str(SLIDE_ROOT),
        "source_mode": source_root_mode(),
        "options": source_root_option_payload(),
        "projects": list_projects(),
    }


def has_active_jobs() -> bool:
    with JOBS_LOCK:
        return any(
            job.get("status") in ACTIVE_JOB_STATUSES
            for job in JOBS.values()
        )


def choose_source_root_dialog() -> Path:
    if sys.platform == "darwin":
        script = 'POSIX path of (choose folder with prompt "Chọn source folder cho Viro Web UI")'
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout or "").strip()
            if "User canceled" in message or proc.returncode == 1:
                raise RuntimeError("Đã huỷ chọn folder.")
            raise RuntimeError(message or "Không mở được Finder để chọn folder.")
        selected = proc.stdout.strip()
        if not selected:
            raise RuntimeError("Chưa chọn folder.")
        return Path(selected)

    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:
        raise RuntimeError("Máy này không hỗ trợ hộp chọn folder native.") from exc

    root = tk.Tk()
    root.withdraw()
    try:
        selected = filedialog.askdirectory(title="Chọn source folder cho Viro Web UI")
    finally:
        root.destroy()
    if not selected:
        raise RuntimeError("Đã huỷ chọn folder.")
    return Path(selected)


def list_projects() -> list[dict]:
    if not SLIDE_ROOT.exists():
        return []

    def project_sort_key(path: Path) -> tuple[float, str]:
        try:
            updated_at = path.stat().st_mtime
        except OSError:
            updated_at = 0
        return (-updated_at, path.name.lower())

    projects = []
    project_dirs = sorted(iter_project_dirs(), key=project_sort_key)
    for project_dir in project_dirs:
        if not project_dir.is_dir():
            continue
        if not (project_dir / "index.html").exists():
            continue
        script_path = project_dir / "script-90s.txt"
        output_dir = project_dir / "output"
        final_video = project_dir / "output" / "final_video.mp4"
        has_output = output_dir.is_dir() and any(output_dir.iterdir())
        output_url = final_video_url(project_dir.name)
        render_versions = list_render_versions(project_dir)
        metadata = read_project_metadata(project_dir)
        voice = metadata.get("voice") if isinstance(metadata.get("voice"), dict) else {}
        project_connections = metadata.get("connections") if isinstance(metadata.get("connections"), list) else []
        projects.append(
            {
                "name": project_dir.name,
                "title": str(metadata.get("title") or metadata.get("name") or project_dir.name),
                "url": project_url(project_dir.name),
                "source_path": str(project_dir),
                "metadata_path": str(project_metadata_path(project_dir)),
                "template": str(metadata.get("template") or ""),
                "language": normalize_language(metadata.get("language")),
                "created_at": str(metadata.get("created_at") or ""),
                "updated_at": str(metadata.get("updated_at") or ""),
                "connections": [str(item) for item in project_connections if str(item).strip()],
                "voice_connection_id": str(voice.get("connection_id") or ""),
                "has_script": script_path.exists(),
                "script_count": len(
                    [line for line in script_path.read_text(encoding="utf-8").splitlines() if line.strip()]
                )
                if script_path.exists()
                else 0,
                "has_output": has_output,
                "output_url": output_url,
                "video_url": output_url if final_video.exists() else None,
                "render_versions": render_versions,
                "render_count": len(render_versions),
                "latest_render_url": render_versions[0]["url"] if render_versions else None,
                "latest_render_name": render_versions[0]["name"] if render_versions else "",
            }
        )
    return projects


def list_templates() -> list[dict]:
    if not TEMPLATE_ROOT.exists():
        return []

    def template_sort_key(path: Path) -> tuple[float, str]:
        try:
            updated_at = path.stat().st_mtime
        except OSError:
            updated_at = 0
        starter_bias = 0 if path.name == "viro-slide-starter" else 1
        return (starter_bias, -updated_at, path.name.lower())

    templates = []
    for template_dir in sorted([path for path in TEMPLATE_ROOT.iterdir() if path.is_dir()], key=template_sort_key):
        metadata = read_template_metadata(template_dir)
        voice = metadata.get("voice") if isinstance(metadata.get("voice"), dict) else {}
        domain = template_domain_metadata({}, metadata)
        script_path = template_dir / "script-90s.txt"
        demo_path = template_dir / "demo.mp4"
        preview_path = next(
            (
                candidate
                for candidate in [
                    template_dir / "viro-icon.svg",
                    template_dir / "image.png",
                    template_dir / "webui_en.png",
                    template_dir / "flow_en.png",
                    template_dir / "source" / "tweet-screenshot.png",
                    template_dir / "source" / "x-2056415413077233983-photo1.png",
                ]
                if candidate.exists()
            ),
            None,
        )
        script_count = (
            len([line for line in script_path.read_text(encoding="utf-8").splitlines() if line.strip()])
            if script_path.exists()
            else 0
        )
        templates.append(
            {
                "name": template_dir.name,
                "title": str(metadata.get("title") or template_dir.name),
                "description": str(metadata.get("description") or ""),
                "tags": metadata.get("tags") if isinstance(metadata.get("tags"), list) else [],
                "pack": domain["pack"],
                "variant": domain["variant"],
                "aspect_ratio": domain["aspect_ratio"],
                "platforms": domain["platforms"],
                "voice": voice,
                "voice_connection_id": str(voice.get("connection_id") or ""),
                "metadata_path": str(template_metadata_path(template_dir)),
                "url": f"/template/{quote(template_dir.name)}/",
                "edit_url": f"/template/{quote(template_dir.name)}/edit",
                "source_path": str(template_dir),
                "script_count": script_count,
                "has_index": (template_dir / "index.html").exists(),
                "has_style": (template_dir / "style.css").exists(),
                "has_app": (template_dir / "app.js").exists(),
                "has_preview_settings": (template_dir / "preview-settings.json").exists(),
                "demo_url": f"/template/{quote(template_dir.name)}/demo.mp4" if demo_path.exists() else None,
                "preview_url": f"/template/{quote(template_dir.name)}/{quote_relative_url(preview_path.relative_to(template_dir).as_posix())}"
                if preview_path
                else None,
            }
        )
    return templates


def template_metadata_path(template_dir: Path) -> Path:
    return template_dir / TEMPLATE_METADATA_FILENAME


def read_template_metadata(template_dir: Path) -> dict:
    path = template_metadata_path(template_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def write_template_metadata(template_dir: Path, metadata: dict) -> None:
    template_dir.mkdir(parents=True, exist_ok=True)
    template_metadata_path(template_dir).write_bytes(json_dumps(metadata) + b"\n")


def require_template_name(template_name: str) -> str:
    template_name = unquote(str(template_name or "")).strip()
    if not template_name or template_name in {".", ".."} or "/" in template_name or "\\" in template_name or "\x00" in template_name:
        raise ValueError("Invalid template name.")
    return template_name


def require_existing_template_dir(template_name: str) -> Path:
    template_name = require_template_name(template_name)
    template_dir = (TEMPLATE_ROOT / template_name).resolve()
    try:
        template_dir.relative_to(TEMPLATE_ROOT.resolve())
    except ValueError as exc:
        raise ValueError("Invalid template path.") from exc
    if not template_dir.is_dir():
        raise FileNotFoundError(f"Template not found: {template_name}")
    return template_dir


def read_template_text_file(template_dir: Path, filename: str, max_chars: int = 240_000) -> str:
    path = template_dir / filename
    if not path.exists() or not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[:max_chars]


def split_list_field(value: object, limit: int = 20) -> list[str]:
    if isinstance(value, list):
        items = value
    else:
        items = re.split(r"[,;\n]+", str(value or ""))
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text[:80])
        if len(cleaned) >= limit:
            break
    return cleaned


def template_domain_metadata(payload: dict, existing: dict | None = None) -> dict:
    existing = existing if isinstance(existing, dict) else {}
    pack = str((payload.get("pack") if "pack" in payload else "") or existing.get("pack") or "").strip()[:80]
    variant = str((payload.get("variant") if "variant" in payload else "") or existing.get("variant") or "").strip()[:80]
    aspect_value = (
        payload.get("aspect_ratio")
        if "aspect_ratio" in payload
        else payload.get("aspectRatio")
        if "aspectRatio" in payload
        else ""
    )
    aspect_ratio = str(aspect_value or existing.get("aspect_ratio") or "").strip()[:24]
    platforms_source = (
        payload.get("platforms")
        if "platforms" in payload and payload.get("platforms")
        else existing.get("platforms")
        if isinstance(existing.get("platforms"), list)
        else ""
    )
    platforms = split_list_field(platforms_source, limit=12)
    return {
        "pack": pack or "General",
        "variant": variant or "Default",
        "aspect_ratio": aspect_ratio or "9:16",
        "platforms": platforms,
    }


def template_detail_response(template_name: str) -> dict:
    template_dir = require_existing_template_dir(template_name)
    metadata = read_template_metadata(template_dir)
    template = next((item for item in list_templates() if item["name"] == template_dir.name), None)
    preview_settings = read_template_text_file(template_dir, "preview-settings.json")
    domain = template_domain_metadata({}, metadata)
    return {
        "template": template,
        "metadata": metadata,
        "editable": {
            "title": str(metadata.get("title") or template_dir.name),
            "description": str(metadata.get("description") or ""),
            "tags": ", ".join(split_list_field(metadata.get("tags", []))),
            "pack": domain["pack"],
            "variant": domain["variant"],
            "aspect_ratio": domain["aspect_ratio"],
            "platforms": ", ".join(domain["platforms"]),
            "voice_connection_id": str((metadata.get("voice") if isinstance(metadata.get("voice"), dict) else {}).get("connection_id") or ""),
            "script": read_template_text_file(template_dir, "script-90s.txt"),
            "rules": read_template_text_file(template_dir, "TEMPLATE_RULES.md"),
            "preview_settings": preview_settings,
        },
    }


def template_response() -> dict:
    return {
        "templates": list_templates(),
        "template_root": str(TEMPLATE_ROOT),
    }


def template_archive_skip(relative_path: Path) -> bool:
    parts = set(relative_path.parts)
    if parts & TEMPLATE_ARCHIVE_EXCLUDED_DIRS:
        return True
    return relative_path.name in TEMPLATE_ARCHIVE_EXCLUDED_FILES


def template_archive_manifest(template_dir: Path, template_name: str) -> dict:
    metadata = read_template_metadata(template_dir)
    domain = template_domain_metadata({}, metadata)
    return {
        "schema_version": 1,
        "type": "viro_template",
        "exported_at": utc_now_iso(),
        "root": template_name,
        "template": {
            "name": template_name,
            "title": str(metadata.get("title") or template_name),
            "description": str(metadata.get("description") or ""),
            "tags": metadata.get("tags") if isinstance(metadata.get("tags"), list) else [],
            "pack": domain["pack"],
            "variant": domain["variant"],
            "aspect_ratio": domain["aspect_ratio"],
            "platforms": domain["platforms"],
        },
        "required_files": sorted(TEMPLATE_REQUIRED_FILES),
        "excluded": {
            "dirs": sorted(TEMPLATE_ARCHIVE_EXCLUDED_DIRS),
            "files": sorted(TEMPLATE_ARCHIVE_EXCLUDED_FILES),
        },
    }


def export_template_archive(template_name: str) -> dict:
    template_dir = require_existing_template_dir(template_name)
    if not (template_dir / "index.html").exists():
        raise ValueError("Template thiếu index.html, chưa thể export.")
    template_name = template_dir.name
    buffer = io.BytesIO()
    manifest = template_archive_manifest(template_dir, template_name)
    file_count = 0
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(TEMPLATE_ARCHIVE_MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        for path in sorted(template_dir.rglob("*")):
            if not path.is_file():
                continue
            relative_path = path.relative_to(template_dir)
            if template_archive_skip(relative_path):
                continue
            archive.write(path, f"{template_name}/{relative_path.as_posix()}")
            file_count += 1
    data = buffer.getvalue()
    if len(data) > MAX_TEMPLATE_ARCHIVE_BYTES:
        raise ValueError("Template archive quá lớn để export từ UI.")
    return {
        "filename": f"{template_name}.viro-template.zip",
        "content_type": "application/zip",
        "data": data,
        "size": len(data),
        "file_count": file_count,
    }


def decode_template_archive(payload: dict) -> bytes:
    encoded = str(payload.get("archive") or payload.get("data") or "").strip()
    if "," in encoded[:120]:
        encoded = encoded.split(",", 1)[1]
    if not encoded:
        raise ValueError("Missing template archive.")
    try:
        data = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise ValueError("Template archive must be base64 ZIP data.") from exc
    if not data:
        raise ValueError("Template archive is empty.")
    if len(data) > MAX_TEMPLATE_ARCHIVE_BYTES:
        raise ValueError("Template archive quá lớn.")
    return data


def normalized_zip_name(name: str) -> str:
    normalized = str(name or "").replace("\\", "/").strip()
    if not normalized or normalized.startswith("/") or normalized.startswith("../") or "/../" in normalized or normalized.endswith("/.."):
        raise ValueError("Template archive chứa path không an toàn.")
    if re.match(r"^[A-Za-z]:", normalized):
        raise ValueError("Template archive chứa Windows drive path không an toàn.")
    return normalized


def template_import_source(archive: zipfile.ZipFile, filename: str) -> tuple[dict, str, list[zipfile.ZipInfo]]:
    manifest: dict = {}
    try:
        if TEMPLATE_ARCHIVE_MANIFEST in archive.namelist():
            manifest_data = archive.read(TEMPLATE_ARCHIVE_MANIFEST)
            manifest = json.loads(manifest_data.decode("utf-8")) if manifest_data else {}
            if not isinstance(manifest, dict):
                manifest = {}
    except Exception:
        manifest = {}

    infos: list[zipfile.ZipInfo] = []
    total_uncompressed = 0
    for info in archive.infolist():
        name = normalized_zip_name(info.filename)
        if info.is_dir() or name == TEMPLATE_ARCHIVE_MANIFEST:
            continue
        if name.startswith("__MACOSX/") or name.endswith("/.DS_Store"):
            continue
        if len(infos) >= MAX_TEMPLATE_ARCHIVE_FILES:
            raise ValueError("Template archive có quá nhiều file.")
        total_uncompressed += int(info.file_size or 0)
        if total_uncompressed > MAX_TEMPLATE_ARCHIVE_UNCOMPRESSED_BYTES:
            raise ValueError("Template archive giải nén quá lớn.")
        infos.append(info)

    if not infos:
        raise ValueError("Template archive không có file template.")

    safe_names = [normalized_zip_name(info.filename) for info in infos]
    first_parts = {name.split("/", 1)[0] for name in safe_names if "/" in name}
    root = str(manifest.get("root") or "").strip().strip("/")
    if root and all(name == root or name.startswith(f"{root}/") for name in safe_names):
        prefix = f"{root}/"
    elif len(first_parts) == 1 and all("/" in name for name in safe_names):
        prefix = f"{next(iter(first_parts))}/"
        root = prefix.strip("/")
    else:
        prefix = ""
        root = Path(filename or "").stem.replace(".viro-template", "") or str(manifest.get("template", {}).get("name") or "")
    return manifest, prefix, infos


def imported_template_relative_name(info: zipfile.ZipInfo, prefix: str) -> str:
    name = normalized_zip_name(info.filename)
    relative = name[len(prefix):] if prefix and name.startswith(prefix) else name
    relative = relative.strip("/")
    if not relative:
        raise ValueError("Template archive chứa file path rỗng.")
    path = Path(relative)
    if template_archive_skip(path):
        return ""
    return relative


def import_template_archive(payload: dict) -> dict:
    TEMPLATE_ROOT.mkdir(parents=True, exist_ok=True)
    archive_bytes = decode_template_archive(payload)
    filename = Path(str(payload.get("filename") or "template.zip")).name
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        manifest, prefix, infos = template_import_source(archive, filename)
        relative_names = [imported_template_relative_name(info, prefix) for info in infos]
        relative_names = [name for name in relative_names if name]
        present_required = {Path(name).as_posix() for name in relative_names}
        missing = sorted(TEMPLATE_REQUIRED_FILES - present_required)
        if missing:
            raise ValueError(f"Template archive thiếu file bắt buộc: {', '.join(missing)}.")

        manifest_template = manifest.get("template") if isinstance(manifest.get("template"), dict) else {}
        title = str(payload.get("title") or manifest_template.get("title") or manifest.get("root") or filename).strip()[:120]
        slug_source = str(payload.get("slug") or manifest_template.get("name") or manifest.get("root") or title or filename).strip()
        slug = unique_template_slug(slug_source)
        template_dir = (TEMPLATE_ROOT / slug).resolve()
        try:
            template_dir.relative_to(TEMPLATE_ROOT.resolve())
        except ValueError as exc:
            raise ValueError("Invalid template import path.") from exc
        if template_dir.exists():
            raise FileExistsError(f"Template already exists: {slug}")
        template_dir.mkdir(parents=True)
        try:
            for info in infos:
                relative = imported_template_relative_name(info, prefix)
                if not relative:
                    continue
                target = (template_dir / relative).resolve()
                try:
                    target.relative_to(template_dir)
                except ValueError as exc:
                    raise ValueError("Template archive chứa path không an toàn.") from exc
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(info))
        except Exception:
            shutil.rmtree(template_dir, ignore_errors=True)
            raise

    now = utc_now_iso()
    metadata = read_template_metadata(template_dir)
    if not isinstance(metadata, dict):
        metadata = {}
    tags = metadata.get("tags") if isinstance(metadata.get("tags"), list) else manifest_template.get("tags", [])
    domain = template_domain_metadata(payload, {**manifest_template, **metadata})
    metadata.update(
        {
            "schema_version": 1,
            "id": f"template_{uuid.uuid4().hex[:12]}",
            "name": slug,
            "title": title or slug,
            "description": str(payload.get("description") or metadata.get("description") or manifest_template.get("description") or "").strip()[:1000],
            "tags": split_list_field(tags),
            "pack": domain["pack"],
            "variant": domain["variant"],
            "aspect_ratio": domain["aspect_ratio"],
            "platforms": domain["platforms"],
            "created_at": now,
            "updated_at": now,
            "source": {
                "imported_from": filename,
                "imported_at": now,
                "original_template": str(manifest_template.get("name") or manifest.get("root") or ""),
            },
        }
    )
    voice = metadata.get("voice") if isinstance(metadata.get("voice"), dict) else {}
    voice_connection_id = normalize_studio_voice_connection_id(voice.get("connection_id") if isinstance(voice, dict) else "")
    if voice_connection_id:
        metadata["voice"] = {"engine": "elevenlabs", "mode": "api", "connection_id": voice_connection_id}
    else:
        metadata.pop("voice", None)
    write_template_metadata(template_dir, metadata)
    return {
        **template_response(),
        "template": next((item for item in list_templates() if item["name"] == slug), None),
        "template_url": f"/template/{quote(slug)}/",
        "import": {
            "filename": filename,
            "file_count": len(relative_names),
            "template": slug,
        },
    }


def create_template_from_template(payload: dict) -> dict:
    TEMPLATE_ROOT.mkdir(parents=True, exist_ok=True)
    title = str(payload.get("title") or payload.get("name") or "").strip()[:120]
    if not title:
        raise ValueError("Missing template name.")
    base_name = str(payload.get("base_template") or payload.get("template") or "viro-slide-starter").strip()
    base_template_name, base_template_dir = require_template_dir(base_name)
    slug = unique_template_slug(str(payload.get("slug") or title))
    template_dir = (TEMPLATE_ROOT / slug).resolve()
    try:
        template_dir.relative_to(TEMPLATE_ROOT.resolve())
    except ValueError as exc:
        raise ValueError("Invalid template path.") from exc
    if template_dir.exists():
        raise FileExistsError(f"Template already exists: {slug}")
    shutil.copytree(
        base_template_dir,
        template_dir,
        ignore=shutil.ignore_patterns(".git", "__pycache__", "node_modules", "output", "test-results"),
    )
    now = utc_now_iso()
    tags = split_list_field(payload.get("tags"))
    domain = template_domain_metadata(payload, read_template_metadata(base_template_dir))
    voice_connection_id = normalize_studio_voice_connection_id(payload.get("voice_connection_id") or payload.get("voiceConnectionId"))
    metadata = {
        "schema_version": 1,
        "id": f"template_{uuid.uuid4().hex[:12]}",
        "name": slug,
        "title": title,
        "description": str(payload.get("description") or "").strip()[:1000],
        "tags": tags,
        "pack": domain["pack"],
        "variant": domain["variant"],
        "aspect_ratio": domain["aspect_ratio"],
        "platforms": domain["platforms"],
        "created_at": now,
        "updated_at": now,
        "source": {
            "base_template": base_template_name,
            "base_template_path": str(base_template_dir),
        },
    }
    if voice_connection_id:
        metadata["voice"] = {
            "engine": "elevenlabs",
            "mode": "api",
            "connection_id": voice_connection_id,
        }
    write_template_metadata(template_dir, metadata)
    script = str(payload.get("script") or "").strip()
    if script:
        (template_dir / "script-90s.txt").write_text(script + "\n", encoding="utf-8")
    return {
        **template_response(),
        "template": next((item for item in list_templates() if item["name"] == slug), None),
        "template_url": f"/template/{quote(slug)}/",
    }


def update_template(payload: dict) -> dict:
    template_name = require_template_name(str(payload.get("template") or payload.get("name") or ""))
    template_dir = require_existing_template_dir(template_name)
    metadata = read_template_metadata(template_dir)
    now = utc_now_iso()
    tags = split_list_field(payload.get("tags"))
    domain = template_domain_metadata(payload, metadata)
    metadata.update(
        {
            "schema_version": 1,
            "name": template_name,
            "title": str(payload.get("title") or template_name).strip()[:120],
            "description": str(payload.get("description") or "").strip()[:1000],
            "tags": tags,
            "pack": domain["pack"],
            "variant": domain["variant"],
            "aspect_ratio": domain["aspect_ratio"],
            "platforms": domain["platforms"],
            "updated_at": now,
        }
    )
    if "voice_connection_id" in payload or "voiceConnectionId" in payload:
        voice_connection_id = normalize_studio_voice_connection_id(payload.get("voice_connection_id") or payload.get("voiceConnectionId"))
        if voice_connection_id:
            metadata["voice"] = {
                "engine": "elevenlabs",
                "mode": "api",
                "connection_id": voice_connection_id,
            }
        else:
            metadata.pop("voice", None)
    if not metadata.get("id"):
        metadata["id"] = f"template_{uuid.uuid4().hex[:12]}"
    if not metadata.get("created_at"):
        metadata["created_at"] = now
    write_template_metadata(template_dir, metadata)
    if "script" in payload:
        (template_dir / "script-90s.txt").write_text(str(payload.get("script") or "").strip() + "\n", encoding="utf-8")
    if "rules" in payload:
        (template_dir / "TEMPLATE_RULES.md").write_text(str(payload.get("rules") or "").rstrip() + "\n", encoding="utf-8")
    if "preview_settings" in payload:
        preview_settings = str(payload.get("preview_settings") or "").strip()
        if preview_settings:
            try:
                parsed = json.loads(preview_settings)
            except json.JSONDecodeError as exc:
                raise ValueError(f"preview-settings.json không hợp lệ: {exc}") from exc
            (template_dir / "preview-settings.json").write_bytes(json_dumps(parsed) + b"\n")
    return {
        **template_response(),
        "template": next((item for item in list_templates() if item["name"] == template_name), None),
        "template_url": f"/template/{quote(template_name)}/",
    }


def create_template_from_project(payload: dict) -> dict:
    TEMPLATE_ROOT.mkdir(parents=True, exist_ok=True)
    project_name = validate_project_name(str(payload.get("project") or payload.get("project_name") or ""))
    project_dir = require_slide_project(project_name)
    project_metadata = read_project_metadata(project_dir)
    title = str(payload.get("title") or payload.get("name") or "").strip()[:120]
    if not title:
        project_title = str(project_metadata.get("title") or project_name).strip()
        title = f"{project_title} template"[:120]
    slug = unique_template_slug(str(payload.get("slug") or title))
    template_dir = (TEMPLATE_ROOT / slug).resolve()
    try:
        template_dir.relative_to(TEMPLATE_ROOT.resolve())
    except ValueError as exc:
        raise ValueError("Invalid template path.") from exc
    if template_dir.exists():
        raise FileExistsError(f"Template already exists: {slug}")

    shutil.copytree(
        project_dir,
        template_dir,
        ignore=shutil.ignore_patterns(
            ".git",
            "__pycache__",
            "node_modules",
            "output",
            "test-results",
            ".pytest_cache",
            "input_audio",
            PROJECT_METADATA_FILENAME,
            "upload-metadata.json",
        ),
    )
    source_template_name = str(project_metadata.get("template") or "").strip()
    try:
        source_template_metadata = read_template_metadata(require_existing_template_dir(source_template_name)) if source_template_name else {}
    except Exception:
        source_template_metadata = {}
    now = utc_now_iso()
    tags = split_list_field(payload.get("tags") or source_template_metadata.get("tags"))
    domain = template_domain_metadata(payload, source_template_metadata)
    description = str(
        payload.get("description")
        or source_template_metadata.get("description")
        or f"Saved from project {project_name}."
    ).strip()[:1000]
    metadata = {
        "schema_version": 1,
        "id": f"template_{uuid.uuid4().hex[:12]}",
        "name": slug,
        "title": title,
        "description": description,
        "tags": tags,
        "pack": domain["pack"],
        "variant": domain["variant"],
        "aspect_ratio": domain["aspect_ratio"],
        "platforms": domain["platforms"],
        "created_at": now,
        "updated_at": now,
        "source": {
            "saved_from_project": project_name,
            "project_path": str(project_dir),
            "base_template": source_template_name,
            "saved_at": now,
        },
    }
    voice = project_metadata.get("voice") if isinstance(project_metadata.get("voice"), dict) else {}
    voice_connection_id = normalize_studio_voice_connection_id(voice.get("connection_id") if isinstance(voice, dict) else "")
    if voice_connection_id:
        metadata["voice"] = {"engine": "elevenlabs", "mode": "api", "connection_id": voice_connection_id}
    write_template_metadata(template_dir, metadata)
    if not (template_dir / "script-90s.txt").exists():
        (template_dir / "script-90s.txt").write_text("", encoding="utf-8")
    return {
        **template_response(),
        "template": next((item for item in list_templates() if item["name"] == slug), None),
        "template_url": f"/template/{quote(slug)}/",
    }


def utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def require_template_dir(template_name: str | None) -> tuple[str, Path]:
    templates = list_templates()
    selected = selected_template_name(templates, template_name)
    if not selected:
        raise FileNotFoundError("Template not found.")
    template_dir = (TEMPLATE_ROOT / selected).resolve()
    try:
        template_dir.relative_to(TEMPLATE_ROOT.resolve())
    except ValueError as exc:
        raise ValueError("Invalid template path.") from exc
    if not template_dir.is_dir() or not (template_dir / "index.html").is_file():
        raise FileNotFoundError(f"Template not ready: {selected}")
    return selected, template_dir


def create_project_from_template(payload: dict) -> dict:
    if SOURCE_ROOT_IS_PROJECT:
        raise RuntimeError("Source root đang là một project đơn. Hãy đổi source sang folder chứa nhiều project trước.")
    title = str(payload.get("title") or payload.get("name") or "").strip()[:120]
    template_name, template_dir = require_template_dir(str(payload.get("template") or payload.get("template_name") or ""))
    if not title:
        title = f"{template_name.replace('-', ' ').title()} {time.strftime('%Y%m%d-%H%M')}"
    slug = unique_project_slug(str(payload.get("slug") or title))
    voice_connection_id = normalize_studio_voice_connection_id(payload.get("voice_connection_id") or payload.get("voiceConnectionId"))
    project_dir = (SLIDE_ROOT / slug).resolve()
    try:
        project_dir.relative_to(SLIDE_ROOT.resolve())
    except ValueError as exc:
        raise ValueError("Invalid project path.") from exc
    if project_dir.exists():
        raise FileExistsError(f"Project already exists: {slug}")

    shutil.copytree(
        template_dir,
        project_dir,
        ignore=shutil.ignore_patterns(".git", "__pycache__", "node_modules", "output"),
    )
    now = utc_now_iso()
    metadata = {
        "schema_version": 1,
        "id": f"project_{uuid.uuid4().hex[:12]}",
        "name": slug,
        "title": title,
        "template": template_name,
        "language": normalize_language(payload.get("language") or active_language()),
        "created_at": now,
        "updated_at": now,
        "source": {
            "template_path": str(template_dir),
            "template_name": template_name,
        },
        "connections": [],
        "notes": str(payload.get("notes") or "").strip()[:500],
    }
    if voice_connection_id:
        metadata["connections"] = [voice_connection_id]
        metadata["voice"] = {
            "engine": "elevenlabs",
            "mode": "api",
            "connection_id": voice_connection_id,
        }
    write_project_metadata(project_dir, metadata)
    if voice_connection_id:
        assign_connection_to_project(voice_connection_id, slug)
    if not (project_dir / "script-90s.txt").exists():
        (project_dir / "script-90s.txt").write_text("", encoding="utf-8")
    projects = list_projects()
    project = next((item for item in projects if item["name"] == slug), None)
    input_mode = str(payload.get("input_mode") or payload.get("inputMode") or "ai").strip()
    if input_mode not in {"ai", "import", "voiceScript"}:
        input_mode = "ai"
    return {
        "project": project or {
            "name": slug,
            "title": title,
            "template": template_name,
            "url": project_url(slug),
            "source_path": str(project_dir),
        },
        "projects": projects,
        "studio_url": f"/studio?project={quote(slug)}&template={quote(template_name)}&inputMode={quote(input_mode)}",
    }


def read_connections_config() -> dict:
    if not CONNECTIONS_CONFIG.exists():
        return {"connections": [], "assignments": {}}
    try:
        data = json.loads(CONNECTIONS_CONFIG.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Connections config is invalid JSON: {CONNECTIONS_CONFIG}") from exc
    if not isinstance(data, dict):
        return {"connections": [], "assignments": {}}
    connections = data.get("connections") if isinstance(data.get("connections"), list) else []
    assignments = data.get("assignments") if isinstance(data.get("assignments"), dict) else {}
    return {"connections": connections, "assignments": assignments}


def write_connections_config(data: dict) -> None:
    CONFIG_ROOT.mkdir(parents=True, exist_ok=True)
    payload = {
        "connections": data.get("connections") if isinstance(data.get("connections"), list) else [],
        "assignments": data.get("assignments") if isinstance(data.get("assignments"), dict) else {},
    }
    CONNECTIONS_CONFIG.write_bytes(json_dumps(payload) + b"\n")


def request_origin(headers: object | None = None) -> str:
    host = ""
    if headers is not None:
        try:
            host = str(headers.get("Host") or "").strip()
        except Exception:
            host = ""
    if not host:
        host = f"127.0.0.1:{DEFAULT_PORT}"
    scheme = "http"
    if headers is not None:
        try:
            forwarded_proto = str(headers.get("X-Forwarded-Proto") or "").strip().lower()
            if forwarded_proto in {"http", "https"}:
                scheme = forwarded_proto
        except Exception:
            pass
    return f"{scheme}://{host}"


def builtin_elevenlabs_rotator_base_url(origin: str | None = None) -> str:
    origin = str(origin or getattr(REQUEST_CONTEXT, "origin", "") or request_origin()).strip().rstrip("/")
    return f"{origin}{BUILTIN_ELEVENLABS_ROTATOR_PATH}"


def normalize_connection_provider(value: object) -> str:
    provider = re.sub(r"[^a-z0-9_-]+", "", str(value or "").strip().lower())
    return provider if provider in CONNECTION_PROVIDERS else "custom"


def normalize_connection_kind(value: object) -> str:
    kind = re.sub(r"[^a-z0-9_-]+", "", str(value or "").strip().lower())
    return kind if kind in CONNECTION_KINDS else "custom"


def normalize_connection_endpoint_url(value: object) -> str:
    endpoint_url = str(value or "").strip().rstrip("/")
    if not endpoint_url:
        return ""
    if len(endpoint_url) > 300:
        endpoint_url = endpoint_url[:300]
    parsed = urlparse(endpoint_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Endpoint URL phải bắt đầu bằng http:// hoặc https://.")
    return endpoint_url


def valid_project_names() -> set[str]:
    return {project["name"] for project in list_projects()}


def normalize_connection_projects(project_ids: object) -> list[str]:
    if isinstance(project_ids, str):
        raw_ids = [project_ids]
    elif isinstance(project_ids, list):
        raw_ids = project_ids
    else:
        raw_ids = []
    allowed = valid_project_names()
    result = []
    for raw_id in raw_ids:
        project_id = str(raw_id or "").strip()
        if project_id and project_id in allowed and project_id not in result:
            result.append(project_id)
    return result


def mask_secret(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 6:
        return "••••"
    return f"•••• {text[-4:]}"


def safe_connection(connection: dict, assignments: dict | None = None) -> dict:
    assignments = assignments or {}
    connection_id = str(connection.get("id") or "").strip()
    secret_value = str(connection.get("secret_value") or "")
    project_ids = assignments.get(connection_id, connection.get("project_ids", []))
    safe = {
        "id": connection_id,
        "name": str(connection.get("name") or "Untitled").strip()[:80],
        "provider": normalize_connection_provider(connection.get("provider")),
        "kind": normalize_connection_kind(connection.get("kind")),
        "account_id": str(connection.get("account_id") or "").strip()[:160],
        "endpoint_url": str(connection.get("endpoint_url") or connection.get("base_url") or "").strip()[:300],
        "secret_label": str(connection.get("secret_label") or "").strip()[:120],
        "secret_mask": str(connection.get("secret_mask") or mask_secret(secret_value)),
        "secret_configured": bool(connection.get("secret_configured") or secret_value),
        "project_ids": normalize_connection_projects(project_ids),
        "notes": str(connection.get("notes") or "").strip()[:500],
        "status": str(connection.get("status") or ("ready" if secret_value else "needs_setup")),
        "source": str(connection.get("source") or "registry"),
        "managed": bool(connection.get("managed")),
        "updated_at": str(connection.get("updated_at") or ""),
    }
    safe["provider_label"] = CONNECTION_PROVIDERS.get(safe["provider"], safe["provider"])
    safe["kind_label"] = CONNECTION_KINDS.get(safe["kind"], safe["kind"])
    return safe


def discovered_connections(assignments: dict) -> list[dict]:
    result: list[dict] = []
    try:
        tts_config = elevenlabs_public_config()
        if tts_config.get("api_key_configured"):
            result.append(
                safe_connection(
                    {
                        "id": "system:elevenlabs:direct",
                        "name": "ElevenLabs API",
                        "provider": "elevenlabs",
                        "kind": "api_key",
                        "secret_configured": True,
                        "secret_mask": "đã lưu",
                        "account_id": tts_config.get("voice_id") or "",
                        "status": "ready",
                        "source": "config/tts.json",
                        "managed": True,
                    },
                    assignments,
                )
            )
        if tts_config.get("proxy_key_configured") or tts_config.get("proxy_base_url"):
            result.append(
                safe_connection(
                    {
                        "id": "system:elevenlabs:proxy",
                        "name": "APIKeyRotator proxy",
                        "provider": "apikeyrotator",
                        "kind": "proxy_key",
                        "secret_configured": bool(tts_config.get("proxy_key_configured")),
                        "secret_mask": "đã lưu" if tts_config.get("proxy_key_configured") else "",
                        "account_id": tts_config.get("proxy_base_url") or "",
                        "endpoint_url": tts_config.get("proxy_base_url") or "",
                        "status": "ready" if tts_config.get("proxy_key_configured") and tts_config.get("proxy_base_url") else "needs_setup",
                        "source": "config/tts.json",
                        "managed": True,
                    },
                    assignments,
                )
            )
    except Exception:
        pass

    try:
        status = social_status()
        youtube = status.get("platforms", {}).get("youtube", {})
        for channel in youtube.get("channels") or []:
            channel_id = str(channel.get("id") or "").strip()
            if not channel_id:
                continue
            result.append(
                safe_connection(
                    {
                        "id": f"system:youtube:{channel_id}",
                        "name": channel.get("title") or "YouTube channel",
                        "provider": "youtube",
                        "kind": "oauth",
                        "account_id": channel_id,
                        "secret_configured": True,
                        "secret_mask": "OAuth",
                        "status": "ready" if channel.get("active") else "available",
                        "source": "config/social-upload.json",
                        "managed": True,
                    },
                    assignments,
                )
            )
        facebook = status.get("platforms", {}).get("facebook", {})
        for page in facebook.get("pages") or []:
            page_id = str(page.get("id") or "").strip()
            if not page_id:
                continue
            result.append(
                safe_connection(
                    {
                        "id": f"system:facebook:{page_id}",
                        "name": page.get("name") or "Facebook Page",
                        "provider": "facebook",
                        "kind": "page_token",
                        "account_id": page_id,
                        "secret_configured": True,
                        "secret_mask": "Page token",
                        "status": "ready" if page.get("active") else "available",
                        "source": "config/social-upload.json",
                        "managed": True,
                    },
                    assignments,
                )
            )
    except Exception:
        pass
    return result


def list_connections() -> list[dict]:
    config = read_connections_config()
    assignments = config.get("assignments", {})
    registry_connections = [
        safe_connection(connection, assignments)
        for connection in config.get("connections", [])
        if isinstance(connection, dict) and str(connection.get("id") or "").strip()
    ]
    discovered = discovered_connections(assignments)
    seen = set()
    result = []
    for connection in discovered + registry_connections:
        connection_id = connection.get("id")
        if not connection_id or connection_id in seen:
            continue
        seen.add(connection_id)
        result.append(connection)
    return result


def studio_voice_connection_label(connection: dict) -> str:
    name = str(connection.get("name") or "Untitled").strip()
    provider = str(connection.get("provider") or "")
    kind = str(connection.get("kind") or "")
    if provider == "apikeyrotator" and kind == "proxy_key":
        endpoint = str(connection.get("endpoint_url") or connection.get("account_id") or "").strip()
        suffix = "Viro Key Rotate" if str(connection.get("id") or "") == "system:elevenlabs:proxy" else "Proxy"
        return f"{name} - {suffix}" + (f" ({urlparse(endpoint).netloc})" if endpoint else "")
    label = str(connection.get("secret_label") or connection.get("account_id") or "").strip()
    return f"{name} - ElevenLabs key" + (f" ({label})" if label else "")


def studio_voice_connection_rank(connection: dict) -> tuple[int, int, str]:
    provider = str(connection.get("provider") or "")
    kind = str(connection.get("kind") or "")
    connection_id = str(connection.get("id") or "")
    provider_rank = 0 if provider == "apikeyrotator" and kind == "proxy_key" else 1
    system_rank = 0 if connection_id == "system:elevenlabs:proxy" else 1
    return (provider_rank, system_rank, str(connection.get("name") or "").lower())


def studio_voice_connections() -> list[dict]:
    result: list[dict] = []
    for connection in list_connections():
        provider = str(connection.get("provider") or "")
        kind = str(connection.get("kind") or "")
        secret_ready = bool(connection.get("secret_configured"))
        endpoint_ready = bool(str(connection.get("endpoint_url") or "").strip())
        if provider == "apikeyrotator" and kind == "proxy_key" and secret_ready and endpoint_ready:
            item = dict(connection)
        elif provider == "elevenlabs" and kind == "api_key" and secret_ready:
            item = dict(connection)
        else:
            continue
        item["label"] = studio_voice_connection_label(item)
        result.append(item)
    return sorted(result, key=studio_voice_connection_rank)


def selected_studio_voice_connection_id(connections: list[dict] | None = None) -> str:
    connections = connections if connections is not None else studio_voice_connections()
    if not connections:
        return ""
    try:
        public_config = elevenlabs_studio_public_config(ensure=False)
    except Exception:
        public_config = {}
    proxy_url = str(public_config.get("proxy_base_url") or "").strip().rstrip("/")
    if proxy_url:
        for connection in connections:
            if (
                connection.get("provider") == "apikeyrotator"
                and connection.get("kind") == "proxy_key"
                and str(connection.get("endpoint_url") or "").strip().rstrip("/") == proxy_url
            ):
                return str(connection.get("id") or "")
    if public_config.get("api_key_configured"):
        direct = next((connection for connection in connections if connection.get("id") == "system:elevenlabs:direct"), None)
        if direct:
            return str(direct.get("id") or "")
    return str(connections[0].get("id") or "")


def normalize_studio_voice_connection_id(connection_id: object) -> str:
    value = str(connection_id or "").strip()
    if not value:
        return ""
    valid_ids = {str(connection.get("id") or "") for connection in studio_voice_connections()}
    return value if value in valid_ids else ""


def studio_voice_connection_options_html(selected_connection_id: str = "", *, include_empty: bool = False) -> str:
    connections = studio_voice_connections()
    selected_connection_id = selected_connection_id if selected_connection_id else selected_studio_voice_connection_id(connections)
    if not connections:
        return '<option value="">Chưa có API key trong Secret Hub</option>'
    options = []
    if include_empty:
        options.append('<option value="">Chọn khi render</option>')
    for connection in connections:
        connection_id = str(connection.get("id") or "")
        selected = "selected" if connection_id == selected_connection_id else ""
        provider_label = "APIKeyRotator" if connection.get("provider") == "apikeyrotator" else "ElevenLabs"
        label = f"{provider_label} · {connection.get('label') or connection.get('name')}"
        options.append(f'<option value="{html.escape(connection_id)}" {selected}>{html.escape(label)}</option>')
    return "\n".join(options)


def studio_ai_connections() -> list[dict]:
    result: list[dict] = []
    config = read_connections_config()
    assignments = config.get("assignments") if isinstance(config.get("assignments"), dict) else {}
    for connection in config.get("connections", []):
        if not isinstance(connection, dict):
            continue
        safe = safe_connection(connection, assignments)
        if safe.get("provider") != "openai" or safe.get("kind") != "api_key":
            continue
        if not safe.get("secret_configured"):
            continue
        item = dict(safe)
        item["label"] = f"OpenAI · {safe.get('name') or safe.get('secret_label') or safe.get('id')}"
        result.append(item)
    if str(os.environ.get("OPENAI_API_KEY") or "").strip():
        result.append(
            {
                "id": "system:openai:env",
                "name": "OPENAI_API_KEY",
                "provider": "openai",
                "kind": "api_key",
                "secret_configured": True,
                "secret_mask": "env",
                "status": "ready",
                "source": "environment",
                "managed": True,
                "label": "OpenAI · OPENAI_API_KEY",
            }
        )
    return sorted(result, key=lambda item: (0 if item.get("id") == "system:openai:env" else 1, str(item.get("name") or "").lower()))


def selected_studio_ai_connection_id(connections: list[dict] | None = None) -> str:
    connections = connections if connections is not None else studio_ai_connections()
    return str(connections[0].get("id") or "") if connections else ""


def studio_ai_connection_options_html(selected_connection_id: str = "") -> str:
    connections = studio_ai_connections()
    selected_connection_id = selected_connection_id or selected_studio_ai_connection_id(connections)
    placeholder = "Chọn OpenAI key" if connections else "Chưa có OpenAI key trong Secret Hub"
    options = [f'<option value="">{html.escape(placeholder)}</option>']
    for connection in connections:
        connection_id = str(connection.get("id") or "")
        selected = "selected" if connection_id == selected_connection_id else ""
        label = str(connection.get("label") or connection.get("name") or connection_id)
        options.append(f'<option value="{html.escape(connection_id)}" {selected}>{html.escape(label)}</option>')
    return "\n".join(options)


def openai_responses_url(endpoint_url: str = "") -> str:
    raw = str(endpoint_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").strip().rstrip("/")
    if not raw:
        raw = "https://api.openai.com/v1"
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("OpenAI endpoint URL phai bat dau bang http:// hoac https://.")
    if raw.endswith("/responses"):
        return raw
    if raw.endswith("/v1"):
        return f"{raw}/responses"
    if parsed.path in {"", "/"}:
        return f"{raw}/v1/responses"
    return f"{raw}/responses"


def openai_storyboard_auth_for_connection(connection_id: str = "") -> dict:
    connection_id = str(connection_id or "").strip()
    if not connection_id:
        connection_id = selected_studio_ai_connection_id()
    if not connection_id:
        return {}
    if connection_id == "system:openai:env":
        api_key = str(os.environ.get("OPENAI_API_KEY") or "").strip()
        if not api_key:
            return {}
        return {
            "connection_id": connection_id,
            "api_key": api_key,
            "endpoint_url": openai_responses_url(),
            "label": "OPENAI_API_KEY",
            "source": "environment",
        }
    config = read_connections_config()
    assignments = config.get("assignments") if isinstance(config.get("assignments"), dict) else {}
    for connection in config.get("connections", []):
        if not isinstance(connection, dict) or str(connection.get("id") or "") != connection_id:
            continue
        safe = safe_connection(connection, assignments)
        if safe.get("provider") != "openai" or safe.get("kind") != "api_key":
            raise ValueError("Connection AI phai la OpenAI / API key.")
        api_key = str(connection.get("secret_value") or "").strip()
        if not api_key:
            raise ValueError("OpenAI connection chua co API key.")
        return {
            "connection_id": connection_id,
            "api_key": api_key,
            "endpoint_url": openai_responses_url(str(connection.get("endpoint_url") or "")),
            "label": str(safe.get("name") or safe.get("secret_label") or connection_id),
            "source": str(safe.get("source") or "config/connections.json"),
        }
    raise ValueError("Khong tim thay OpenAI connection trong Secret Hub.")


def assign_connection_to_project(connection_id: str, project_name: str) -> None:
    connection_id = normalize_studio_voice_connection_id(connection_id)
    project_name = str(project_name or "").strip()
    if not connection_id or project_name not in valid_project_names():
        return
    config = read_connections_config()
    assignments = config.get("assignments") if isinstance(config.get("assignments"), dict) else {}
    current = assignments.get(connection_id) if isinstance(assignments.get(connection_id), list) else []
    if project_name not in current:
        current = [*current, project_name]
    assignments[connection_id] = normalize_connection_projects(current)
    for connection in config.get("connections", []):
        if isinstance(connection, dict) and connection.get("id") == connection_id:
            connection["project_ids"] = assignments[connection_id]
            connection["updated_at"] = utc_now_iso()
    config["assignments"] = assignments
    write_connections_config(config)


def elevenlabs_render_auth_for_connection(connection_id: str) -> dict:
    connection_id = normalize_studio_voice_connection_id(connection_id)
    if not connection_id:
        return {}
    raw, assignments = find_connection_for_test(connection_id)
    safe = safe_connection(raw, assignments)
    provider = safe.get("provider")
    kind = safe.get("kind")
    secret_value = str(raw.get("secret_value") or "").strip()
    if provider == "apikeyrotator" and kind == "proxy_key":
        endpoint_url = str(safe.get("endpoint_url") or "").strip()
        if not endpoint_url:
            raise ValueError("APIKeyRotator proxy cần Endpoint URL.")
        if not secret_value:
            raise ValueError("APIKeyRotator proxy cần X-Proxy-Key.")
        return {"mode": "proxy", "proxy_base_url": endpoint_url, "proxy_key": secret_value, "connection_id": connection_id}
    if provider == "elevenlabs" and kind == "api_key":
        if not secret_value:
            raise ValueError("Connection chưa có ElevenLabs API key.")
        return {"mode": "direct", "api_key": secret_value, "connection_id": connection_id}
    raise ValueError("Voice credential này chưa hỗ trợ render ElevenLabs API.")


def raw_registry_connection(connection_id: str) -> dict | None:
    connection_id = str(connection_id or "").strip()
    if not connection_id:
        return None
    config = read_connections_config()
    for connection in config.get("connections", []):
        if isinstance(connection, dict) and connection.get("id") == connection_id:
            return connection
    return None


def usable_elevenlabs_key_connections() -> list[dict]:
    config = read_connections_config()
    assignments = config.get("assignments") if isinstance(config.get("assignments"), dict) else {}
    keys: list[dict] = []
    for connection in config.get("connections", []):
        if not isinstance(connection, dict):
            continue
        safe = safe_connection(connection, assignments)
        if safe.get("provider") != "elevenlabs" or safe.get("kind") != "api_key":
            continue
        secret_value = str(connection.get("secret_value") or "").strip()
        if not secret_value:
            continue
        item = dict(safe)
        item["secret_value"] = secret_value
        keys.append(item)
    return keys


def select_elevenlabs_rotator_key() -> dict:
    keys = usable_elevenlabs_key_connections()
    if not keys:
        raise ValueError("No ElevenLabs API key in Secret Hub. Add an ElevenLabs/api_key connection first.")
    with ROTATOR_LOCK:
        index = ROTATOR_INDEX["elevenlabs"] % len(keys)
        ROTATOR_INDEX["elevenlabs"] = (ROTATOR_INDEX["elevenlabs"] + 1) % len(keys)
    return keys[index]


def ensure_elevenlabs_studio_auth(origin: str | None = None) -> dict:
    config = elevenlabs_config()
    if elevenlabs_api_configured(config):
        return elevenlabs_studio_public_config(origin, ensure=False)
    if usable_elevenlabs_key_connections():
        update_elevenlabs_proxy_config(
            builtin_elevenlabs_rotator_base_url(origin),
            BUILTIN_ELEVENLABS_ROTATOR_KEY,
        )
    return elevenlabs_studio_public_config(origin, ensure=False)


def elevenlabs_api_configured(config: dict | None = None) -> bool:
    config = config or elevenlabs_config()
    return bool(str(config.get("api_key") or os.environ.get("ELEVENLABS_API_KEY") or "").strip()) or bool(
        elevenlabs_proxy_base_url(config) and elevenlabs_proxy_key(config)
    )


def elevenlabs_studio_public_config(origin: str | None = None, *, ensure: bool = True) -> dict:
    if ensure:
        return ensure_elevenlabs_studio_auth(origin)
    data = elevenlabs_public_config()
    rotator_url = builtin_elevenlabs_rotator_base_url(origin)
    data["builtin_rotator_base_url"] = rotator_url
    data["builtin_rotator_configured"] = data.get("proxy_base_url") == rotator_url
    data["registry_key_count"] = len(usable_elevenlabs_key_connections())
    data["studio_auth_ready"] = bool(data.get("api_key_configured") or (data.get("proxy_key_configured") and data.get("proxy_base_url")))
    return data


def connections_response() -> dict:
    projects = list_projects()
    connections = list_connections()
    used_connection_count = sum(1 for connection in connections if connection.get("project_ids"))
    ready_connection_count = sum(1 for connection in connections if connection.get("status") in {"ready", "available"})
    studio_auth = elevenlabs_studio_public_config(ensure=False)
    return {
        "config_path": str(CONNECTIONS_CONFIG),
        "providers": CONNECTION_PROVIDERS,
        "kinds": CONNECTION_KINDS,
        "projects": projects,
        "connections": connections,
        "studio_auth": studio_auth,
        "stats": {
            "connections": len(connections),
            "used_connections": used_connection_count,
            "ready_connections": ready_connection_count,
            "elevenlabs_keys": studio_auth.get("registry_key_count", 0),
        },
    }


def upsert_connection(payload: dict) -> dict:
    config = read_connections_config()
    connections = [connection for connection in config.get("connections", []) if isinstance(connection, dict)]
    connection_id = str(payload.get("id") or "").strip()
    existing = next((connection for connection in connections if connection.get("id") == connection_id), None)
    if connection_id == "system:elevenlabs:proxy":
        endpoint_url = normalize_connection_endpoint_url(payload.get("endpoint_url") or payload.get("base_url"))
        if not endpoint_url:
            endpoint_url = elevenlabs_proxy_base_url(elevenlabs_config())
        if not endpoint_url:
            raise ValueError("Missing APIKeyRotator proxy URL.")
        secret_value = str(payload.get("secret_value") or payload.get("secret") or "").strip()
        update_elevenlabs_proxy_config(endpoint_url, secret_value or None)
        assignments = config.get("assignments") if isinstance(config.get("assignments"), dict) else {}
        assignments[connection_id] = normalize_connection_projects(payload.get("project_ids"))
        config["assignments"] = assignments
        write_connections_config(config)
        return connections_response()
    if connection_id.startswith("system:"):
        raise ValueError("System connection cannot be edited here.")
    if not connection_id:
        connection_id = f"conn_{uuid.uuid4().hex[:12]}"
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("Missing connection name.")
    provider = normalize_connection_provider(payload.get("provider"))
    kind = normalize_connection_kind(payload.get("kind"))
    endpoint_url = normalize_connection_endpoint_url(payload.get("endpoint_url") or payload.get("base_url"))
    secret_value = str(payload.get("secret_value") or payload.get("secret") or "").strip()
    now = utc_now_iso()
    next_connection = dict(existing or {})
    next_connection.update(
        {
            "id": connection_id,
            "name": name[:80],
            "provider": provider,
            "kind": kind,
            "account_id": str(payload.get("account_id") or "").strip()[:160],
            "endpoint_url": endpoint_url,
            "secret_label": str(payload.get("secret_label") or "").strip()[:120],
            "notes": str(payload.get("notes") or "").strip()[:500],
            "project_ids": normalize_connection_projects(payload.get("project_ids")),
            "status": "ready" if (secret_value or next_connection.get("secret_value")) else "needs_setup",
            "source": "config/connections.json",
            "managed": False,
            "updated_at": now,
        }
    )
    if secret_value:
        next_connection["secret_value"] = secret_value
        next_connection["secret_mask"] = mask_secret(secret_value)
        next_connection["secret_configured"] = True
    elif not next_connection.get("secret_value"):
        next_connection["secret_mask"] = ""
        next_connection["secret_configured"] = False
    if not existing:
        next_connection["created_at"] = now
        connections.append(next_connection)
    else:
        for index, connection in enumerate(connections):
            if connection.get("id") == connection_id:
                connections[index] = next_connection
                break
    config["connections"] = connections
    assignments = config.get("assignments") if isinstance(config.get("assignments"), dict) else {}
    assignments[connection_id] = next_connection["project_ids"]
    config["assignments"] = assignments
    write_connections_config(config)
    if provider == "elevenlabs" and kind == "api_key" and (secret_value or next_connection.get("secret_value")):
        update_elevenlabs_proxy_config(
            builtin_elevenlabs_rotator_base_url(),
            BUILTIN_ELEVENLABS_ROTATOR_KEY,
        )
    elif provider == "apikeyrotator" and kind == "proxy_key" and endpoint_url and (secret_value or next_connection.get("secret_value")):
        update_elevenlabs_proxy_config(endpoint_url, str(next_connection.get("secret_value") or ""))
    return connections_response()


def assign_connection_projects(payload: dict) -> dict:
    connection_id = str(payload.get("id") or payload.get("connection_id") or "").strip()
    if not connection_id:
        raise ValueError("Missing connection id.")
    known_ids = {connection["id"] for connection in list_connections()}
    if connection_id not in known_ids:
        raise ValueError("Connection not found.")
    project_ids = normalize_connection_projects(payload.get("project_ids"))
    config = read_connections_config()
    assignments = config.get("assignments") if isinstance(config.get("assignments"), dict) else {}
    assignments[connection_id] = project_ids
    for connection in config.get("connections", []):
        if isinstance(connection, dict) and connection.get("id") == connection_id:
            connection["project_ids"] = project_ids
            connection["updated_at"] = utc_now_iso()
    config["assignments"] = assignments
    write_connections_config(config)
    return connections_response()


def delete_connection(payload: dict) -> dict:
    connection_id = str(payload.get("id") or payload.get("connection_id") or "").strip()
    if not connection_id:
        raise ValueError("Missing connection id.")
    if connection_id.startswith("system:"):
        raise ValueError("System connection cannot be deleted here.")
    config = read_connections_config()
    connections = config.get("connections", []) if isinstance(config.get("connections"), list) else []
    next_connections = [connection for connection in connections if not (isinstance(connection, dict) and connection.get("id") == connection_id)]
    if len(next_connections) == len(connections):
        raise ValueError("Connection not found.")
    assignments = config.get("assignments") if isinstance(config.get("assignments"), dict) else {}
    assignments.pop(connection_id, None)
    config["connections"] = next_connections
    config["assignments"] = assignments
    write_connections_config(config)
    return connections_response()


def use_connection_for_studio(payload: dict, origin: str | None = None) -> dict:
    connection_id = str(payload.get("id") or payload.get("connection_id") or "").strip()
    if not connection_id:
        raise ValueError("Missing connection id.")
    raw, assignments = find_connection_for_test(connection_id)
    safe = safe_connection(raw, assignments)
    provider = safe.get("provider")
    kind = safe.get("kind")
    secret_value = str(raw.get("secret_value") or "").strip()
    endpoint_url = str(safe.get("endpoint_url") or "").strip()

    if provider == "elevenlabs" and kind == "api_key":
        if not secret_value:
            raise ValueError("Connection chưa có ElevenLabs API key.")
        update_elevenlabs_proxy_config(
            builtin_elevenlabs_rotator_base_url(origin),
            BUILTIN_ELEVENLABS_ROTATOR_KEY,
        )
        return {
            "ok": True,
            "mode": "builtin_rotator",
            "message": "Đã dùng Secret Hub làm Viro Key Rotate cho Studio.",
            "tts": elevenlabs_studio_public_config(origin, ensure=False),
            "connections": connections_response(),
        }

    if provider == "apikeyrotator" and kind == "proxy_key":
        if not endpoint_url:
            raise ValueError("APIKeyRotator proxy cần Endpoint URL.")
        if not secret_value:
            raise ValueError("APIKeyRotator proxy cần X-Proxy-Key.")
        update_elevenlabs_proxy_config(endpoint_url, secret_value)
        return {
            "ok": True,
            "mode": "external_proxy",
            "message": "Đã dùng APIKeyRotator proxy cho Studio.",
            "tts": elevenlabs_studio_public_config(origin, ensure=False),
            "connections": connections_response(),
        }

    raise ValueError("Connection này chưa hỗ trợ dùng trực tiếp cho Studio. Chọn ElevenLabs/API key hoặc APIKeyRotator/Proxy key.")


def find_connection_for_test(connection_id: str) -> tuple[dict, dict]:
    connection_id = str(connection_id or "").strip()
    if not connection_id:
        raise ValueError("Missing connection id.")
    config = read_connections_config()
    assignments = config.get("assignments") if isinstance(config.get("assignments"), dict) else {}
    for connection in config.get("connections", []):
        if isinstance(connection, dict) and connection.get("id") == connection_id:
            return dict(connection), assignments
    for connection in discovered_connections(assignments):
        if connection.get("id") != connection_id:
            continue
        raw = dict(connection)
        try:
            tts_config = elevenlabs_config()
            if connection_id == "system:elevenlabs:direct":
                raw["secret_value"] = elevenlabs_api_key(tts_config)
            elif connection_id == "system:elevenlabs:proxy":
                raw["secret_value"] = elevenlabs_proxy_key(tts_config)
                raw["endpoint_url"] = elevenlabs_proxy_base_url(tts_config)
        except Exception:
            pass
        return raw, assignments
    raise ValueError("Connection not found.")


def connection_check(status: str, label: str, detail: str) -> dict:
    return {"status": status, "label": label, "detail": detail}


def is_local_endpoint(endpoint_url: str) -> bool:
    host = (urlparse(endpoint_url).hostname or "").strip().lower()
    return host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def http_status_message(exc: Exception) -> str:
    if isinstance(exc, HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, URLError):
        return str(exc.reason)
    return str(exc)


def ping_http_endpoint(endpoint_url: str, headers: dict[str, str] | None = None, timeout: int = 5) -> dict:
    request = Request(
        endpoint_url,
        headers={"User-Agent": "ViroConnectionHealth/1.0", **(headers or {})},
        method="GET",
    )
    with urlopen(request, timeout=timeout) as response:
        return {"status": int(response.status), "reason": str(response.reason or "OK")}


def provider_auth_headers(provider: str, secret_value: str) -> dict[str, str]:
    if provider == "elevenlabs":
        return {
            "Accept": "application/json",
            "xi-api-key": secret_value,
        }
    if provider == "openai":
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {secret_value}",
        }
    if provider == "anthropic":
        return {
            "Accept": "application/json",
            "x-api-key": secret_value,
            "anthropic-version": "2023-06-01",
        }
    return {}


def test_provider_key(provider: str, api_key: str, endpoint_url: str) -> dict:
    request = Request(
        endpoint_url,
        headers={"User-Agent": "ViroConnectionHealth/1.0", **provider_auth_headers(provider, api_key)},
        method="GET",
    )
    with urlopen(request, timeout=10) as response:
        return {"status": int(response.status), "reason": str(response.reason or "OK")}


def raw_connection_for_health(payload: dict) -> tuple[dict, dict]:
    connection_id = str(payload.get("id") or payload.get("connection_id") or "").strip()
    assignments: dict = {}
    if connection_id:
        raw, assignments = find_connection_for_test(connection_id)
    else:
        raw = {
            "id": "__draft__",
            "name": str(payload.get("name") or "Draft connection").strip() or "Draft connection",
            "source": "draft",
            "managed": False,
        }
    for field in ("name", "provider", "kind", "account_id", "endpoint_url", "base_url", "secret_label", "notes", "project_ids"):
        if field in payload:
            raw[field] = payload.get(field)
    secret_value = str(payload.get("secret_value") or payload.get("secret") or "").strip()
    if secret_value:
        raw["secret_value"] = secret_value
        raw["secret_configured"] = True
        raw["secret_mask"] = mask_secret(secret_value)
    elif not raw.get("secret_value"):
        raw["secret_configured"] = False
    return raw, assignments


def test_connection_health(payload: dict) -> dict:
    raw, assignments = raw_connection_for_health(payload)
    safe = safe_connection(raw, assignments)
    provider = safe["provider"]
    kind = safe["kind"]
    secret_value = str(raw.get("secret_value") or "").strip()
    endpoint_url = str(safe.get("endpoint_url") or "").strip()
    account_id = str(safe.get("account_id") or "").strip()
    if not endpoint_url and account_id.startswith(("http://", "https://")):
        endpoint_url = account_id
    checks: list[dict] = []

    if safe.get("secret_configured"):
        checks.append(connection_check("ok", "Secret", "Đã lưu secret ở local registry/config."))
    elif provider in {"elevenlabs", "apikeyrotator", "openai", "anthropic"}:
        checks.append(connection_check("bad", "Secret", "Thiếu API key hoặc proxy key."))
    else:
        checks.append(connection_check("warn", "Secret", "Chưa có secret; chỉ kiểm tra metadata."))

    default_health_endpoint = CONNECTION_PROVIDER_HEALTH_ENDPOINTS.get(provider, "")
    if provider == "apikeyrotator" and not endpoint_url:
        if kind == "proxy_key":
            checks.append(connection_check("bad", "Endpoint", "APIKeyRotator proxy cần Endpoint URL hoặc proxy base URL."))
        else:
            checks.append(connection_check("warn", "Provider", "APIKeyRotator không biết provider gốc để live-test key. Chọn ElevenLabs/OpenAI/Anthropic để test key trực tiếp, hoặc dùng Proxy key kèm Endpoint URL."))
    elif provider == "custom" and not endpoint_url:
        checks.append(connection_check("warn", "Endpoint", "Custom connection nên có Endpoint URL nếu muốn health check thật."))

    if endpoint_url:
        try:
            endpoint_url = normalize_connection_endpoint_url(endpoint_url)
            checks.append(connection_check("ok", "Endpoint", endpoint_url))
        except Exception as exc:
            checks.append(connection_check("bad", "Endpoint", str(exc)))
            endpoint_url = ""
    elif default_health_endpoint:
        endpoint_url = default_health_endpoint
        checks.append(connection_check("ok", "Endpoint", f"Dùng endpoint mặc định: {endpoint_url}"))

    live_tested = False
    if provider in CONNECTION_PROVIDER_HEALTH_ENDPOINTS and secret_value:
        live_tested = True
        try:
            result = test_provider_key(provider, secret_value, endpoint_url)
            checks.append(connection_check("ok", CONNECTION_PROVIDERS.get(provider, provider), f"API key hợp lệ ({result['status']})."))
        except Exception as exc:
            checks.append(connection_check("bad", CONNECTION_PROVIDERS.get(provider, provider), f"API key không pass live check: {http_status_message(exc)}."))

    if endpoint_url and is_local_endpoint(endpoint_url):
        live_tested = True
        headers = {}
        if provider == "apikeyrotator" and secret_value:
            headers["X-Proxy-Key"] = secret_value
        try:
            result = ping_http_endpoint(endpoint_url, headers=headers)
            status = int(result["status"])
            if 200 <= status < 400:
                checks.append(connection_check("ok", "HTTP", f"Kết nối OK ({status})."))
            else:
                checks.append(connection_check("bad", "HTTP", f"Endpoint trả HTTP {status}."))
        except Exception as exc:
            checks.append(connection_check("bad", "HTTP", f"Không gọi được endpoint local: {http_status_message(exc)}."))
    elif endpoint_url and provider not in CONNECTION_PROVIDER_HEALTH_ENDPOINTS:
        checks.append(connection_check("warn", "HTTP", "Không ping URL external mặc định; bật provider-specific test khi cần."))

    if provider in {"youtube", "facebook"}:
        if safe.get("managed") and safe.get("status") in {"ready", "available"}:
            checks.append(connection_check("ok", "OAuth", "Đã thấy account từ social upload config."))
        else:
            checks.append(connection_check("warn", "OAuth", "Dùng flow Connect/Upload để refresh token trước khi publish."))

    if not live_tested and not any(check["status"] == "bad" for check in checks):
        checks.append(connection_check("ok", "Config", "Cấu hình local đủ để gán vào project."))

    has_bad = any(check["status"] == "bad" for check in checks)
    has_warn = any(check["status"] == "warn" for check in checks)
    status = "failed" if has_bad else ("warning" if has_warn else "ready")
    if status == "ready":
        message = "Kết nối OK."
    elif status == "warning":
        message = "Cấu hình OK, còn cảnh báo cần kiểm tra thủ công."
    else:
        first_bad = next((check for check in checks if check["status"] == "bad"), checks[0])
        message = f"Health check lỗi: {first_bad['detail']}"
    return {
        "ok": not has_bad,
        "status": status,
        "connection": safe,
        "message": message,
        "checks": checks,
    }


def require_slide_project(project: str) -> Path:
    project = validate_project_name(project)

    if SOURCE_ROOT_IS_PROJECT:
        if project != SLIDE_ROOT.name:
            raise FileNotFoundError(f"Slide project not found: {project}")
        project_dir = SLIDE_ROOT.resolve()
    else:
        project_dir = (SLIDE_ROOT / project).resolve()
    try:
        project_dir.relative_to(project_lookup_root().resolve())
    except ValueError as exc:
        raise ValueError("Invalid project path.") from exc

    if not project_dir.is_dir() or not (project_dir / "index.html").exists():
        raise FileNotFoundError(f"Slide project not found: {project}")
    return project_dir


def require_payload_project(payload: dict) -> str:
    project = str(payload.get("project") or "").strip()
    require_slide_project(project)
    return project


def studio_state_path(project_dir: Path) -> Path:
    return project_dir / STUDIO_STATE_FILENAME


def read_studio_state(project: str) -> dict:
    project_dir = require_slide_project(project)
    path = studio_state_path(project_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def write_studio_state(project: str, state: dict) -> dict:
    project_dir = require_slide_project(project)
    payload = state if isinstance(state, dict) else {}
    payload["schema_version"] = 1
    payload["updated_at"] = utc_now_iso()
    studio_state_path(project_dir).write_bytes(json_dumps(payload) + b"\n")
    return payload


def update_studio_state(project: str, patch: dict) -> dict:
    state = read_studio_state(project)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(state.get(key), dict):
            state[key] = {**state[key], **value}
        else:
            state[key] = value
    return write_studio_state(project, state)


def project_script_text(project_dir: Path) -> str:
    path = project_dir / "script-90s.txt"
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def voice_preview_signature(project: str, payload: dict | None = None) -> str:
    payload = payload or {}
    project_dir = require_slide_project(project)
    script_text = project_script_text(project_dir)
    signature_payload = {
        "script": script_text,
        "engine": str(payload.get("engine") or "elevenlabs").strip().lower(),
        "mode": str(payload.get("mode") or "tts").strip().lower(),
        "voice": str(payload.get("voice") or "").strip(),
        "connection_id": str(payload.get("connection_id") or payload.get("voice_connection_id") or payload.get("voiceConnectionId") or "").strip(),
        "speed": f"{coerce_speed(payload.get('speed', 1.1)):g}",
    }
    encoded = json.dumps(signature_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def voice_preview_state(project: str, payload: dict | None = None) -> dict:
    project_dir = require_slide_project(project)
    state = read_studio_state(project)
    preview = state.get("voice_preview") if isinstance(state.get("voice_preview"), dict) else {}
    expected_signature = (
        voice_preview_signature(project, payload)
        if payload is not None
        else str(preview.get("signature") or "")
    )
    output_dir = project_dir / "output"
    audio_path = output_dir / "voiceover_concat.mp3"
    timing_path = output_dir / "timing.json"
    current = (
        bool(expected_signature)
        and preview.get("signature") == expected_signature
        and audio_path.is_file()
        and timing_path.is_file()
        and not bool(preview.get("stale"))
    )
    return {
        "project": project,
        "current": bool(current),
        "stale": not bool(current),
        "signature": expected_signature,
        "preview": preview,
        "audio_url": f"{project_url(project)}output/voiceover_concat.mp3" if audio_path.is_file() else "",
        "timing_url": f"{project_url(project)}output/timing.json" if timing_path.is_file() else "",
        "audio_path": str(audio_path) if audio_path.is_file() else "",
        "timing_path": str(timing_path) if timing_path.is_file() else "",
    }


def mark_voice_preview_stale(project: str, reason: str) -> None:
    try:
        state = read_studio_state(project)
        preview = state.get("voice_preview") if isinstance(state.get("voice_preview"), dict) else {}
        if preview:
            preview["current"] = False
            preview["stale"] = True
            preview["stale_reason"] = reason
            preview["stale_at"] = utc_now_iso()
            state["voice_preview"] = preview
            write_studio_state(project, state)
    except Exception:
        pass


def coerce_speed(value: object) -> float:
    try:
        speed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Speed must be a number.") from exc
    if not 0.5 <= speed <= 2.0:
        raise ValueError("Speed must be between 0.5 and 2.0.")
    return speed


def coerce_render_size(value: object) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"", "1080", "1080x1920"}:
        return "1080x1920"
    if raw in {"720", "720x1280"}:
        return "720x1280"
    raise ValueError("Render size must be 1080x1920 or 720x1280.")


def clean_filename(name: str, fallback: str = "voiceover.mp3") -> str:
    cleaned = Path(name or fallback).name
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", cleaned).strip("._")
    return (cleaned or fallback)[:96]


def decode_audio_payload(audio: dict, project_dir: Path) -> Path:
    if not isinstance(audio, dict):
        raise ValueError("Missing ElevenLabs audio payload.")

    encoded = str(audio.get("data") or "")
    if "," in encoded:
        encoded = encoded.split(",", 1)[1]
    if not encoded:
        raise ValueError("Missing ElevenLabs audio data.")

    try:
        audio_bytes = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise ValueError("Audio payload is not valid base64.") from exc

    if not audio_bytes:
        raise ValueError("Uploaded audio is empty.")
    if len(audio_bytes) > MAX_UPLOAD_BYTES:
        raise ValueError("Uploaded audio is too large.")

    upload_dir = project_dir / "input_audio"
    upload_dir.mkdir(parents=True, exist_ok=True)
    stamped_name = f"{time.strftime('%Y%m%d-%H%M%S')}_{clean_filename(str(audio.get('name') or 'voiceover.mp3'))}"
    audio_path = upload_dir / stamped_name
    audio_path.write_bytes(audio_bytes)
    return audio_path


def quote_relative_url(path: str) -> str:
    return "/".join(quote(part) for part in path.split("/"))


def upload_preview_bgm(payload: dict) -> dict:
    project = str(payload.get("project") or "").strip()
    project_dir = require_slide_project(project)
    audio = payload.get("audio")
    if not isinstance(audio, dict):
        raise ValueError("Missing BGM audio payload.")

    encoded = str(audio.get("data") or "")
    if "," in encoded:
        encoded = encoded.split(",", 1)[1]
    if not encoded:
        raise ValueError("Missing BGM audio data.")

    try:
        audio_bytes = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise ValueError("BGM payload is not valid base64.") from exc

    if not audio_bytes:
        raise ValueError("Uploaded BGM is empty.")
    if len(audio_bytes) > MAX_UPLOAD_BYTES:
        raise ValueError("Uploaded BGM is too large.")

    filename = clean_filename(str(audio.get("name") or "background.mp3"), "background.mp3")
    suffix = Path(filename).suffix.lower()
    if suffix not in AUDIO_UPLOAD_EXTENSIONS:
        raise ValueError("BGM file must be an audio file.")

    upload_dir = project_dir / "preview-assets" / "bgm"
    upload_dir.mkdir(parents=True, exist_ok=True)
    saved_name = f"{time.strftime('%Y%m%d-%H%M%S')}_{filename}"
    audio_path = upload_dir / saved_name
    audio_path.write_bytes(audio_bytes)
    rel_path = audio_path.relative_to(project_dir).as_posix()
    return {
        "project": project_dir.name,
        "audio": {
            "name": filename,
            "path": rel_path,
            "url": project_url(project_dir.name) + quote_relative_url(rel_path),
            "size": len(audio_bytes),
        },
    }


def slide_media_type_from_suffix(suffix: str) -> str:
    suffix = suffix.lower()
    if suffix in SLIDE_MEDIA_IMAGE_EXTENSIONS:
        return "image"
    if suffix in SLIDE_MEDIA_VIDEO_EXTENSIONS:
        return "video"
    raise ValueError("Media file must be an image or video.")


def clean_slide_media_fit(value: object) -> str:
    fit = str(value or "").strip().lower()
    return fit if fit in {"cover", "contain", "fill"} else "cover"


def clean_slide_media_position(value: object) -> str:
    position = str(value or "").strip().lower()
    return position if position in {"center", "top", "bottom", "left", "right"} else "center"


def project_relative_file(project_dir: Path, relative_path: str) -> Path:
    relative = str(relative_path or "").replace("\\", "/").strip().lstrip("/")
    if not relative or "\x00" in relative or relative.startswith("../") or "/../" in relative:
        raise ValueError("Invalid project-relative path.")
    target = (project_dir / relative).resolve()
    try:
        target.relative_to(project_dir.resolve())
    except ValueError as exc:
        raise ValueError("Invalid project-relative path.") from exc
    return target


def slide_media_url(project: str, item: dict) -> str:
    path = str(item.get("path") or "").strip()
    if path:
        return project_url(project) + quote_relative_url(path)
    return str(item.get("url") or "").strip()


def clean_slide_media_items(project_dir: Path, project: str, raw_items: object, active_count: int) -> list[dict]:
    if not isinstance(raw_items, list):
        return []
    cleaned: list[dict] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        try:
            slide_index = int(raw.get("slide", raw.get("slideIndex", raw.get("index", -1))))
        except (TypeError, ValueError):
            continue
        if slide_index < 0 or slide_index >= active_count:
            continue
        path = str(raw.get("path") or raw.get("src") or "").strip().replace("\\", "/")
        url = str(raw.get("url") or "").strip()
        if not path and not url:
            continue
        media_type = str(raw.get("type") or "").strip().lower()
        stored_path = ""
        if path:
            target = project_relative_file(project_dir, path)
            if not target.exists() or not target.is_file():
                continue
            suffix_type = slide_media_type_from_suffix(target.suffix)
            media_type = media_type if media_type in {"image", "video"} else suffix_type
            stored_path = target.relative_to(project_dir.resolve()).as_posix()
        else:
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"}:
                continue
            media_type = media_type if media_type in {"image", "video"} else "image"
        item = {
            "slide": slide_index,
            "type": media_type,
            "fit": clean_slide_media_fit(raw.get("fit")),
            "position": clean_slide_media_position(raw.get("position")),
        }
        if stored_path:
            item["path"] = stored_path
        else:
            item["url"] = url
        if raw.get("name"):
            item["name"] = str(raw.get("name")).strip()[:120]
        item["resolved_url"] = slide_media_url(project, item)
        cleaned.append(item)
    return cleaned[:active_count]


def list_project_media_files(project: str, project_dir: Path) -> list[dict]:
    media_dir = project_dir / "media"
    if not media_dir.exists():
        return []
    files: list[dict] = []
    for path in sorted(media_dir.rglob("*"), key=lambda item: item.stat().st_mtime if item.is_file() else 0, reverse=True):
        if not path.is_file() or path.suffix.lower() not in SLIDE_MEDIA_UPLOAD_EXTENSIONS:
            continue
        relative = path.relative_to(project_dir).as_posix()
        try:
            media_type = slide_media_type_from_suffix(path.suffix)
        except ValueError:
            continue
        files.append(
            {
                "name": path.name,
                "path": relative,
                "url": project_url(project) + quote_relative_url(relative),
                "type": media_type,
                "size": path.stat().st_size,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(path.stat().st_mtime)),
            }
        )
    return files


def upload_slide_media(payload: dict) -> dict:
    project = str(payload.get("project") or "").strip()
    project_dir = require_slide_project(project)
    media = payload.get("media")
    if not isinstance(media, dict):
        raise ValueError("Missing media payload.")
    encoded = str(media.get("data") or "")
    if "," in encoded:
        encoded = encoded.split(",", 1)[1]
    if not encoded:
        raise ValueError("Missing media data.")
    try:
        media_bytes = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise ValueError("Media payload is not valid base64.") from exc
    if not media_bytes:
        raise ValueError("Uploaded media is empty.")
    if len(media_bytes) > MAX_UPLOAD_BYTES:
        raise ValueError("Uploaded media is too large.")
    filename = clean_filename(str(media.get("name") or "slide-media.png"), "slide-media.png")
    suffix = Path(filename).suffix.lower()
    if suffix not in SLIDE_MEDIA_UPLOAD_EXTENSIONS:
        raise ValueError("Media file must be PNG, JPG, WEBP, GIF, MP4, WEBM, or MOV.")
    media_type = slide_media_type_from_suffix(suffix)
    upload_dir = project_dir / "media"
    upload_dir.mkdir(parents=True, exist_ok=True)
    saved_name = f"{time.strftime('%Y%m%d-%H%M%S')}_{filename}"
    media_path = upload_dir / saved_name
    media_path.write_bytes(media_bytes)
    rel_path = media_path.relative_to(project_dir).as_posix()
    item = {
        "name": filename,
        "path": rel_path,
        "url": project_url(project_dir.name) + quote_relative_url(rel_path),
        "type": media_type,
        "size": len(media_bytes),
    }
    return {"project": project_dir.name, "media": item, "files": list_project_media_files(project_dir.name, project_dir)}


def preview_settings_path(project: str) -> Path:
    return require_slide_project(project) / "preview-settings.json"


def read_preview_settings(project: str) -> dict:
    path = preview_settings_path(project)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"preview-settings.json is invalid: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("preview-settings.json must contain a JSON object.")
    return data


def write_preview_settings(payload: dict) -> dict:
    project = str(payload.get("project") or "").strip()
    settings = payload.get("settings")
    if not isinstance(settings, dict):
        raise ValueError("Missing preview settings.")
    path = preview_settings_path(project)
    settings = sync_settings_script_lines(path.parent, settings)
    encoded = json.dumps(settings, ensure_ascii=False, indent=2).encode("utf-8")
    if len(encoded) > 200_000:
        raise ValueError("Preview settings are too large.")
    path.write_bytes(encoded + b"\n")
    app_js_synced = sync_app_js_preview_settings(path.parent, settings)
    return {"project": project, "settings": settings, "app_js_synced": app_js_synced}


def clean_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned = [str(item).strip() for item in value]
    return [item for item in cleaned if item]


def js_literal(value: object, base_indent: str = "") -> str:
    literal = json.dumps(value, ensure_ascii=False, indent=2)
    if "\n" not in literal or not base_indent:
        return literal
    lines = literal.splitlines()
    return lines[0] + "\n" + "\n".join(f"{base_indent}{line}" for line in lines[1:])


def js_literal_span(source: str, start: int) -> tuple[int, int]:
    idx = start
    while idx < len(source) and source[idx].isspace():
        idx += 1
    if idx >= len(source) or source[idx] not in "[{(":
        raise ValueError("Expected JS literal.")
    open_to_close = {"[": "]", "{": "}", "(": ")"}
    literal_start = idx
    stack: list[str] = []
    quote = ""
    escaped = False
    line_comment = False
    block_comment = False
    while idx < len(source):
        char = source[idx]
        next_char = source[idx + 1] if idx + 1 < len(source) else ""
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            idx += 1
            continue
        if line_comment:
            if char == "\n":
                line_comment = False
            idx += 1
            continue
        if block_comment:
            if char == "*" and next_char == "/":
                block_comment = False
                idx += 2
                continue
            idx += 1
            continue
        if char == "/" and next_char == "/":
            line_comment = True
            idx += 2
            continue
        if char == "/" and next_char == "*":
            block_comment = True
            idx += 2
            continue
        if char in {"'", '"', "`"}:
            quote = char
            idx += 1
            continue
        if char in open_to_close:
            stack.append(open_to_close[char])
        elif char in {"]", "}", ")"}:
            if not stack or char != stack[-1]:
                raise ValueError("Malformed JS literal.")
            stack.pop()
            if not stack:
                return literal_start, idx + 1
        idx += 1
    raise ValueError("Unterminated JS literal.")


def js_const_literal_span(source: str, const_name: str) -> tuple[int, int]:
    match = re.search(rf"\bconst\s+{re.escape(const_name)}\s*=\s*", source)
    if not match:
        raise ValueError(f"Missing const {const_name}.")
    return js_literal_span(source, match.end())


def read_js_const_literal(project_dir: Path, const_name: str) -> object | None:
    path = project_dir / "app.js"
    if not path.exists():
        return None
    source = path.read_text(encoding="utf-8")
    try:
        start, end = js_const_literal_span(source, const_name)
        return ast.literal_eval(source[start:end])
    except Exception:
        return None


def replace_js_const_literal(source: str, const_name: str, value: object) -> str:
    start, end = js_const_literal_span(source, const_name)
    line_start = source.rfind("\n", 0, start) + 1
    base_indent = re.match(r"\s*", source[line_start:start]).group(0)
    return source[:start] + js_literal(value, base_indent) + source[end:]


def replace_default_preview_script_lines(source: str, lines: list[str]) -> str:
    object_start, object_end = js_const_literal_span(source, "defaultPreviewSettings")
    object_source = source[object_start:object_end]
    match = re.search(r'(?m)^(\s*)(["\']?scriptLines["\']?\s*:\s*)', object_source)
    if not match:
        raise ValueError("Missing defaultPreviewSettings.slides.scriptLines.")
    value_start = object_start + match.end()
    value_end = js_literal_span(source, value_start)[1]
    return source[:value_start] + js_literal(lines, match.group(1)) + source[value_end:]


def sync_app_js_script_lines(project_dir: Path, lines: list[str]) -> bool:
    path = project_dir / "app.js"
    if not path.exists():
        return False
    source = path.read_text(encoding="utf-8")
    updated = replace_js_const_literal(source, "slideScripts", lines)
    updated = replace_default_preview_script_lines(updated, lines)
    if updated != source:
        path.write_text(updated, encoding="utf-8")
    return updated != source


def sync_app_js_preview_settings(project_dir: Path, settings: dict) -> bool:
    path = project_dir / "app.js"
    if not path.exists():
        return False
    source = path.read_text(encoding="utf-8")
    updated = replace_js_const_literal(source, "defaultPreviewSettings", settings)
    script_lines = settings.get("slides", {}).get("scriptLines", [])
    if isinstance(script_lines, list) and script_lines and all(isinstance(line, str) for line in script_lines):
        updated = replace_js_const_literal(updated, "slideScripts", script_lines)
    slides = settings.get("slides", {})
    if isinstance(slides, dict):
        for setting_key, const_name in SLIDE_AUDIO_SETTING_KEYS.items():
            values = clean_string_list(slides.get(setting_key))
            if values:
                updated = replace_js_const_literal(updated, const_name, values)
    if updated != source:
        path.write_text(updated, encoding="utf-8")
    return updated != source


def read_script_lines_from_path(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def sync_settings_script_lines(project_dir: Path, settings: dict) -> dict:
    script_lines = read_script_lines_from_path(project_dir / "script-90s.txt")
    synced = json.loads(json.dumps(settings, ensure_ascii=False))
    slides = synced.get("slides")
    if not isinstance(slides, dict):
        slides = {}
    if script_lines:
        slides["scriptLines"] = script_lines
    existing_slides: dict = {}
    settings_path = project_dir / "preview-settings.json"
    if settings_path.exists():
        try:
            existing_settings = json.loads(settings_path.read_text(encoding="utf-8"))
            if isinstance(existing_settings, dict) and isinstance(existing_settings.get("slides"), dict):
                existing_slides = existing_settings["slides"]
        except json.JSONDecodeError:
            existing_slides = {}
    for setting_key, const_name in SLIDE_AUDIO_SETTING_KEYS.items():
        values = clean_string_list(slides.get(setting_key))
        if not values:
            values = clean_string_list(existing_slides.get(setting_key))
        if not values:
            values = clean_string_list(read_js_const_literal(project_dir, const_name))
        if values:
            slides[setting_key] = values
    synced["slides"] = slides
    return synced


def sync_preview_settings_script_lines(project_dir: Path, lines: list[str]) -> dict:
    path = project_dir / "preview-settings.json"
    settings: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"preview-settings.json is invalid: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("preview-settings.json must contain a JSON object.")
        settings = data
    slides = settings.get("slides")
    if not isinstance(slides, dict):
        slides = {}
    slides["scriptLines"] = lines
    for setting_key, const_name in SLIDE_AUDIO_SETTING_KEYS.items():
        values = clean_string_list(slides.get(setting_key))
        if not values:
            values = clean_string_list(read_js_const_literal(project_dir, const_name))
        if values:
            slides[setting_key] = values
    settings["slides"] = slides
    encoded = json.dumps(settings, ensure_ascii=False, indent=2).encode("utf-8")
    if len(encoded) > 200_000:
        raise ValueError("Preview settings are too large.")
    path.write_bytes(encoded + b"\n")
    return settings


class ProjectSlideIdParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.slide_ids: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "div":
            return
        attr_map = {key.lower(): value for key, value in attrs}
        classes = str(attr_map.get("class") or "").split()
        if "slide" not in classes:
            return
        slide_id = str(attr_map.get("data-slide-id") or attr_map.get("data-slide") or len(self.slide_ids)).strip()
        if slide_id and slide_id not in self.slide_ids:
            self.slide_ids.append(slide_id)


def project_slide_ids(project_dir: Path) -> list[str]:
    index_path = project_dir / "index.html"
    if not index_path.exists():
        return []
    parser = ProjectSlideIdParser()
    parser.feed(index_path.read_text(encoding="utf-8", errors="ignore"))
    return parser.slide_ids


def clean_deleted_slide_ids(raw: object, slide_ids: list[str], active_count: int) -> list[str] | None:
    if not isinstance(raw, list):
        return None
    allowed = set(slide_ids)
    deleted: list[str] = []
    for item in raw:
        value = str(item).strip()
        if value in allowed and value not in deleted:
            deleted.append(value)
    if len(slide_ids) - len(deleted) != active_count:
        return None
    return deleted


def resize_deleted_slide_ids(
    project_dir: Path,
    line_count: int,
    payload: dict,
    existing_settings: dict,
) -> list[str] | None:
    slide_ids = project_slide_ids(project_dir)
    if not slide_ids:
        return None
    if line_count > len(slide_ids):
        raise ValueError(f"Script has {line_count} lines, but template only has {len(slide_ids)} slides.")

    requested = clean_deleted_slide_ids(
        payload.get("deletedIds", payload.get("deleted_ids")),
        slide_ids,
        line_count,
    )
    if requested is not None:
        return requested

    existing_deleted = clean_deleted_slide_ids(
        (existing_settings.get("slides") or {}).get("deletedIds") if isinstance(existing_settings.get("slides"), dict) else None,
        slide_ids,
        line_count,
    )
    if existing_deleted is not None:
        return existing_deleted

    return slide_ids[line_count:]


def slide_script_path(project: str) -> Path:
    return require_slide_project(project) / "script-90s.txt"


def read_slide_script(project: str) -> dict:
    path = slide_script_path(project)
    return {"project": project, "lines": read_script_lines_from_path(path)}


def write_slide_script(payload: dict) -> dict:
    project = str(payload.get("project") or "").strip()
    path = slide_script_path(project)
    lines = payload.get("lines")
    allow_count_change = bool(payload.get("allowCountChange") or payload.get("allow_count_change"))
    if not isinstance(lines, list):
        raise ValueError("Missing script lines.")
    cleaned = [str(line).strip() for line in lines]
    if not cleaned or any(not line for line in cleaned):
        raise ValueError("Script lines cannot be empty.")
    existing_count = len(read_slide_script(project)["lines"])
    if existing_count and len(cleaned) != existing_count and not allow_count_change:
        raise ValueError(f"Expected {existing_count} script lines.")
    encoded = ("\n".join(cleaned) + "\n").encode("utf-8")
    if len(encoded) > 100_000:
        raise ValueError("Script is too large.")
    project_dir = path.parent
    existing_metadata = social_metadata.read_project_upload_metadata(project_dir)
    upload_metadata = social_metadata.generated_upload_metadata(project_dir, cleaned, existing_metadata)
    path.write_bytes(encoded)
    social_metadata.write_project_upload_metadata(project_dir, upload_metadata)
    existing_settings = read_preview_settings(project)
    deleted_ids = resize_deleted_slide_ids(project_dir, len(cleaned), payload, existing_settings) if allow_count_change else None
    settings = sync_preview_settings_script_lines(project_dir, cleaned)
    if deleted_ids is not None:
        settings.setdefault("slides", {})["deletedIds"] = deleted_ids
        settings_path = project_dir / "preview-settings.json"
        settings_path.write_bytes(json.dumps(settings, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
        app_js_synced = sync_app_js_preview_settings(project_dir, settings)
    else:
        app_js_synced = sync_app_js_script_lines(project_dir, cleaned)
    mark_voice_preview_stale(project, "script_changed")
    return {"project": project, "lines": cleaned, "upload_metadata": upload_metadata, "app_js_synced": app_js_synced}


def active_slide_ids_for_settings(project_dir: Path, settings: dict) -> list[str]:
    slide_ids = project_slide_ids(project_dir)
    if not slide_ids:
        return []
    raw_deleted = (settings.get("slides") or {}).get("deletedIds") if isinstance(settings.get("slides"), dict) else []
    deleted = {str(item) for item in raw_deleted} if isinstance(raw_deleted, list) else set()
    return [slide_id for slide_id in slide_ids if slide_id not in deleted]


def clean_storyboard_items(value: object, slide_count: int) -> list[dict]:
    if not isinstance(value, list):
        return []
    items = []
    for index, raw in enumerate(value[:slide_count]):
        raw = raw if isinstance(raw, dict) else {}
        slide_index = int(raw.get("slide", raw.get("index", index)) or index)
        if slide_index < 0 or slide_index >= slide_count:
            slide_index = index
        duration = raw.get("duration")
        try:
            duration_value = max(1.0, min(60.0, float(duration))) if duration is not None else 0
        except (TypeError, ValueError):
            duration_value = 0
        item = {
            "slide": slide_index,
            "screen_text": str(raw.get("screen_text") or raw.get("screenText") or "").strip()[:220],
            "visual_direction": str(raw.get("visual_direction") or raw.get("visualDirection") or "").strip()[:600],
            "media_direction": str(raw.get("media_direction") or raw.get("mediaDirection") or "").strip()[:400],
        }
        transition = re.sub(r"[^a-z0-9_-]+", "", str(raw.get("transition") or "").strip().lower())
        reveal = re.sub(r"[^a-z0-9_-]+", "", str(raw.get("reveal") or "").strip().lower())
        if transition:
            item["transition"] = transition[:40]
        if reveal:
            item["reveal"] = reveal[:40]
        if duration_value:
            item["duration"] = round(duration_value, 2)
        items.append(item)
    items.sort(key=lambda item: int(item.get("slide", 0)))
    return items


def slide_composer_response(project: str) -> dict:
    project_dir = require_slide_project(project)
    settings = read_preview_settings(project)
    script_lines = read_slide_script(project)["lines"]
    active_ids = active_slide_ids_for_settings(project_dir, settings)
    active_count = len(active_ids) or len(script_lines)
    slides_settings = settings.get("slides") if isinstance(settings.get("slides"), dict) else {}
    transitions = clean_string_list(slides_settings.get("transitionSounds"))
    reveals = clean_string_list(slides_settings.get("revealSounds"))
    media_items = clean_slide_media_items(project_dir, project, slides_settings.get("media"), active_count)
    media_by_slide = {int(item["slide"]): item for item in media_items}
    storyboard_items = clean_storyboard_items(slides_settings.get("storyboard"), active_count)
    storyboard_by_slide = {int(item["slide"]): item for item in storyboard_items}
    slides = []
    for index in range(active_count):
        media_item = media_by_slide.get(index)
        storyboard_item = storyboard_by_slide.get(index, {})
        slides.append(
            {
                "index": index,
                "id": active_ids[index] if index < len(active_ids) else str(index),
                "text": script_lines[index] if index < len(script_lines) else "",
                "transition": transitions[index] if index < len(transitions) else "",
                "reveal": reveals[index] if index < len(reveals) else "",
                "media": media_item,
                "screen_text": storyboard_item.get("screen_text", ""),
                "visual_direction": storyboard_item.get("visual_direction", ""),
                "media_direction": storyboard_item.get("media_direction", ""),
                "duration": storyboard_item.get("duration", ""),
            }
        )
    return {
        "project": project,
        "project_url": project_url(project),
        "settings": settings,
        "slides": slides,
        "media_files": list_project_media_files(project, project_dir),
        "studio_state": read_studio_state(project),
        "voice_preview": voice_preview_state(project),
    }


def save_slide_composer(payload: dict) -> dict:
    project = str(payload.get("project") or "").strip()
    project_dir = require_slide_project(project)
    raw_slides = payload.get("slides")
    if not isinstance(raw_slides, list):
        raise ValueError("Missing slides.")
    existing_settings = read_preview_settings(project)
    existing_slides = existing_settings.get("slides") if isinstance(existing_settings.get("slides"), dict) else {}
    cleaned_slides = [item for item in raw_slides if isinstance(item, dict)]
    if not cleaned_slides:
        raise ValueError("No slide data to save.")
    cleaned_slides.sort(key=lambda item: int(item.get("index", item.get("slide", 0)) or 0))
    lines = [str(item.get("text") or "").strip() for item in cleaned_slides]
    if any(not line for line in lines):
        raise ValueError("Slide text cannot be empty.")
    deleted_ids = existing_slides.get("deletedIds") if isinstance(existing_slides.get("deletedIds"), list) else []
    write_slide_script({"project": project, "lines": lines, "allowCountChange": True, "deletedIds": deleted_ids})

    settings = read_preview_settings(project)
    slides_settings = settings.get("slides") if isinstance(settings.get("slides"), dict) else {}
    slides_settings["scriptLines"] = lines
    if deleted_ids:
        slides_settings["deletedIds"] = deleted_ids
    transitions = [str(item.get("transition") or "").strip() for item in cleaned_slides]
    reveals = [str(item.get("reveal") or "").strip() for item in cleaned_slides]
    if any(transitions):
        slides_settings["transitionSounds"] = [value or "minimal" for value in transitions]
    if any(reveals):
        slides_settings["revealSounds"] = [value or "ping" for value in reveals]
    raw_media = []
    for index, slide in enumerate(cleaned_slides):
        media = slide.get("media")
        if not isinstance(media, dict):
            continue
        media = {**media, "slide": index}
        raw_media.append(media)
    media_items = clean_slide_media_items(project_dir, project, raw_media, len(cleaned_slides))
    for item in media_items:
        item.pop("resolved_url", None)
    if media_items:
        slides_settings["media"] = media_items
    else:
        slides_settings.pop("media", None)
    storyboard_items = []
    for index, slide in enumerate(cleaned_slides):
        storyboard_items.append(
            {
                "slide": index,
                "screen_text": str(slide.get("screen_text") or slide.get("screenText") or "").strip(),
                "visual_direction": str(slide.get("visual_direction") or slide.get("visualDirection") or "").strip(),
                "media_direction": str(slide.get("media_direction") or slide.get("mediaDirection") or "").strip(),
                "duration": slide.get("duration") or "",
            }
        )
    slides_settings["storyboard"] = clean_storyboard_items(storyboard_items, len(cleaned_slides))
    settings["slides"] = slides_settings
    write_preview_settings({"project": project, "settings": settings})
    return slide_composer_response(project)


def storyboard_duration_seconds(value: object, slide_count: int) -> int:
    mode = str(value or "short").strip().lower()
    if mode in {"long", "120", "120s"}:
        return 120
    if mode in {"medium", "90", "90s"}:
        return 90
    if mode in {"auto", "ai"}:
        return max(45, min(120, slide_count * 12))
    return 60


def split_prompt_units(prompt: str) -> list[str]:
    lines = [line.strip() for line in prompt.replace("\r", "\n").split("\n") if line.strip()]
    if len(lines) > 1:
        return lines
    parts = [part.strip() for part in re.split(r"(?<=[.!?。！？])\s+", prompt.strip()) if part.strip()]
    return parts or ([prompt.strip()] if prompt.strip() else [])


def compact_screen_text(text: str, language: str) -> str:
    words = re.findall(r"\S+", text)
    limit = 8 if language == "vi" else 7
    compact = " ".join(words[:limit]).strip(" ,.;:!?")
    return compact[:70] or ("Ý chính" if language == "vi" else "Key idea")


def local_storyboard_lines(prompt: str, slide_count: int, language: str) -> list[str]:
    units = split_prompt_units(prompt)
    if len(units) >= slide_count:
        return units[:slide_count]
    seed = units[0] if units else prompt.strip()
    if language == "en":
        templates = [
            "Open with the core problem: {seed}.",
            "Show the context and why this matters for the target viewer.",
            "Explain the workflow in one concrete, easy-to-follow step.",
            "Highlight the strongest proof point or contrast.",
            "Turn the takeaway into a practical action the viewer can remember.",
            "Close with a short verdict and the next step.",
        ]
    else:
        templates = [
            "Mở đầu bằng vấn đề chính: {seed}.",
            "Cho người xem thấy bối cảnh và lý do nội dung này đáng chú ý.",
            "Giải thích workflow bằng một bước cụ thể, dễ hình dung.",
            "Nhấn mạnh điểm chứng minh mạnh nhất hoặc sự khác biệt chính.",
            "Biến thông điệp thành một hành động thực tế người xem có thể nhớ.",
            "Chốt lại bằng verdict ngắn và bước tiếp theo.",
        ]
    lines = units[:]
    while len(lines) < slide_count:
        template = templates[len(lines) % len(templates)]
        lines.append(template.format(seed=seed or ("ý tưởng này" if language == "vi" else "this idea")))
    return lines[:slide_count]


def storyboard_json_schema(slide_count: int) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "slides": {
                "type": "array",
                "minItems": slide_count,
                "maxItems": slide_count,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "voiceover": {"type": "string"},
                        "screen_text": {"type": "string"},
                        "visual_direction": {"type": "string"},
                        "media_direction": {"type": "string"},
                        "transition": {"type": "string"},
                        "reveal": {"type": "string"},
                        "duration": {"type": "number"},
                    },
                    "required": [
                        "voiceover",
                        "screen_text",
                        "visual_direction",
                        "media_direction",
                        "transition",
                        "reveal",
                        "duration",
                    ],
                },
            }
        },
        "required": ["slides"],
    }


def openai_response_text(response: dict) -> str:
    direct = response.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    chunks: list[str] = []
    for item in response.get("output") or []:
        if not isinstance(item, dict):
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str) and text:
                chunks.append(text)
    return "\n".join(chunks).strip()


def call_openai_storyboard(
    *,
    auth: dict,
    prompt: str,
    slide_count: int,
    language: str,
    platform: str,
    template_name: str,
    target_duration: int,
    model: str,
) -> dict:
    model = str(model or os.environ.get("VIRO_OPENAI_STORYBOARD_MODEL") or "gpt-4.1-mini").strip()
    instructions = (
        "You are the storyboard engine inside Viro, an AI video studio. "
        "Return only the JSON object requested by the schema. "
        "Each slide must be production-ready for a vertical short video: concise voiceover, short on-screen text, "
        "clear visual/media direction, transition/reveal names, and realistic duration seconds. "
        "Do not mention that you are an AI. Do not add markdown."
    )
    user_input = {
        "task": "Generate a structured storyboard from the user prompt.",
        "user_prompt": prompt,
        "language": language,
        "platform": platform,
        "template": template_name or "selected Viro template",
        "slide_count": slide_count,
        "target_duration_seconds": target_duration,
        "transition_options": ["dramatic", "rise", "sweep", "chime", "bass", "minimal", "gong", "retro"],
        "reveal_options": ["sparkle", "pop", "chime", "click", "blip", "bell", "ping"],
        "rules": [
            "slides.length must exactly match slide_count",
            "voiceover should be natural when read aloud",
            "screen_text should be short enough for mobile",
            "visual_direction should be specific enough for a slide editor",
            "media_direction should say what media to attach or when to keep template motion",
            "duration values should sum close to target_duration_seconds",
        ],
    }
    request_payload = {
        "model": model,
        "instructions": instructions,
        "input": json.dumps(user_input, ensure_ascii=False),
        "text": {
            "format": {
                "type": "json_schema",
                "name": "viro_storyboard",
                "strict": True,
                "schema": storyboard_json_schema(slide_count),
            }
        },
    }
    request = Request(
        str(auth["endpoint_url"]),
        data=json.dumps(request_payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {auth['api_key']}",
            "User-Agent": "ViroStoryboard/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=75) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI storyboard failed ({exc.code}): {detail[:500]}") from exc
    except URLError as exc:
        raise RuntimeError(f"OpenAI storyboard network error: {exc.reason}") from exc
    data = json.loads(raw)
    if str(data.get("status") or "completed") not in {"completed", ""}:
        raise RuntimeError(f"OpenAI storyboard status is {data.get('status')}.")
    text = openai_response_text(data)
    if not text:
        raise RuntimeError("OpenAI storyboard response did not include output text.")
    try:
        storyboard = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("OpenAI storyboard response was not valid JSON.") from exc
    if not isinstance(storyboard, dict) or not isinstance(storyboard.get("slides"), list):
        raise RuntimeError("OpenAI storyboard response missing slides array.")
    storyboard["_model"] = model
    storyboard["_response_id"] = str(data.get("id") or "")
    return storyboard


def normalize_storyboard_result(
    raw_storyboard: dict,
    *,
    slide_count: int,
    language: str,
    platform: str,
    template_name: str,
    target_duration: int,
) -> tuple[list[str], list[dict]]:
    raw_slides = raw_storyboard.get("slides") if isinstance(raw_storyboard.get("slides"), list) else []
    fallback_lines = local_storyboard_lines("", slide_count, language)
    transition_cycle = ["dramatic", "rise", "sweep", "chime", "bass", "minimal"]
    reveal_cycle = ["sparkle", "pop", "chime", "click", "blip", "bell"]
    per_slide_duration = round(target_duration / max(slide_count, 1), 2)
    lines: list[str] = []
    items: list[dict] = []
    for index in range(slide_count):
        item = raw_slides[index] if index < len(raw_slides) and isinstance(raw_slides[index], dict) else {}
        voiceover = str(item.get("voiceover") or item.get("text") or item.get("slide_text") or "").strip()
        if not voiceover:
            voiceover = fallback_lines[index] if index < len(fallback_lines) else ("Slide" if language == "en" else "Noi dung slide")
        screen_text = str(item.get("screen_text") or item.get("screenText") or "").strip() or compact_screen_text(voiceover, language)
        visual_direction = str(item.get("visual_direction") or item.get("visualDirection") or "").strip()
        media_direction = str(item.get("media_direction") or item.get("mediaDirection") or "").strip()
        if not visual_direction:
            visual_direction = f"Use {template_name or 'selected template'} for {platform}; focus on {compact_screen_text(voiceover, language)}."
        if not media_direction:
            media_direction = "Use template motion unless project media is attached."
        try:
            duration = float(item.get("duration"))
        except (TypeError, ValueError):
            duration = per_slide_duration
        transition = re.sub(r"[^a-z0-9_-]+", "", str(item.get("transition") or "").strip().lower()) or transition_cycle[index % len(transition_cycle)]
        reveal = re.sub(r"[^a-z0-9_-]+", "", str(item.get("reveal") or "").strip().lower()) or reveal_cycle[index % len(reveal_cycle)]
        lines.append(voiceover[:600])
        items.append(
            {
                "slide": index,
                "screen_text": screen_text[:120],
                "visual_direction": visual_direction[:500],
                "media_direction": media_direction[:500],
                "duration": max(1, min(60, round(duration, 2))),
                "transition": transition,
                "reveal": reveal,
            }
        )
    return lines, items


def local_storyboard_result(
    *,
    prompt: str,
    slide_count: int,
    language: str,
    platform: str,
    template_name: str,
    target_duration: int,
) -> tuple[list[str], list[dict]]:
    lines = local_storyboard_lines(prompt, slide_count, language)
    transition_cycle = ["dramatic", "rise", "sweep", "chime", "bass", "minimal"]
    reveal_cycle = ["sparkle", "pop", "chime", "click", "blip", "bell"]
    per_slide_duration = round(target_duration / max(slide_count, 1), 2)
    storyboard_items = []
    for index, line in enumerate(lines):
        if language == "en":
            visual_direction = f"Use the {template_name or 'selected'} template layout for {platform}; keep the visual focused on: {compact_screen_text(line, language)}."
            media_direction = "Use existing template motion unless the user attaches media to this slide."
        else:
            visual_direction = f"Dung layout template {template_name or 'dang chon'} cho {platform}; giu visual tap trung vao: {compact_screen_text(line, language)}."
            media_direction = "Dung motion san co cua template, tru khi nguoi dung gan media rieng cho slide nay."
        storyboard_items.append(
            {
                "slide": index,
                "screen_text": compact_screen_text(line, language),
                "visual_direction": visual_direction,
                "media_direction": media_direction,
                "duration": per_slide_duration,
                "transition": transition_cycle[index % len(transition_cycle)],
                "reveal": reveal_cycle[index % len(reveal_cycle)],
            }
        )
    return lines, storyboard_items


def generate_storyboard(payload: dict) -> dict:
    project = str(payload.get("project") or "").strip()
    project_dir = require_slide_project(project)
    prompt = str(payload.get("prompt") or payload.get("input") or "").strip()
    if not prompt:
        raise ValueError("Missing AI prompt.")
    language = normalize_language(payload.get("language") or active_language())
    platform = str(payload.get("platform") or "shorts").strip()[:80] or "shorts"
    duration_mode = str(payload.get("videoDuration") or payload.get("duration") or "short").strip()
    template_name = str(payload.get("template") or read_project_metadata(project_dir).get("template") or "").strip()
    settings = read_preview_settings(project)
    active_ids = active_slide_ids_for_settings(project_dir, settings)
    current_lines = read_slide_script(project)["lines"]
    slide_count = len(active_ids) or len(current_lines) or 6
    target_duration = storyboard_duration_seconds(duration_mode, slide_count)
    per_slide_duration = round(target_duration / max(slide_count, 1), 2)
    lines = local_storyboard_lines(prompt, slide_count, language)
    transition_cycle = ["dramatic", "rise", "sweep", "chime", "bass", "minimal"]
    reveal_cycle = ["sparkle", "pop", "chime", "click", "blip", "bell"]
    storyboard_items = []
    for index, line in enumerate(lines):
        if language == "en":
            visual_direction = f"Use the {template_name or 'selected'} template layout for {platform}; keep the visual focused on: {compact_screen_text(line, language)}."
            media_direction = "Use existing template motion unless the user attaches media to this slide."
        else:
            visual_direction = f"Dùng layout template {template_name or 'đang chọn'} cho {platform}; giữ visual tập trung vào: {compact_screen_text(line, language)}."
            media_direction = "Dùng motion sẵn có của template, trừ khi người dùng gắn media riêng cho slide này."
        storyboard_items.append(
            {
                "slide": index,
                "screen_text": compact_screen_text(line, language),
                "visual_direction": visual_direction,
                "media_direction": media_direction,
                "duration": per_slide_duration,
            }
        )

    ai_connection_id = str(payload.get("ai_connection_id") or payload.get("aiConnectionId") or payload.get("storyboard_connection_id") or "").strip()
    ai_model = str(payload.get("ai_model") or payload.get("model") or os.environ.get("VIRO_OPENAI_STORYBOARD_MODEL") or "gpt-4.1-mini").strip()
    ai_source = "viro_local_storyboard"
    ai_provider = "local"
    ai_label = "Local fallback"
    ai_error = ""
    ai_response_id = ""
    auth = openai_storyboard_auth_for_connection(ai_connection_id) if ai_connection_id else {}
    if auth:
        raw_storyboard = call_openai_storyboard(
            auth=auth,
            prompt=prompt,
            slide_count=slide_count,
            language=language,
            platform=platform,
            template_name=template_name,
            target_duration=target_duration,
            model=ai_model,
        )
        lines, storyboard_items = normalize_storyboard_result(
            raw_storyboard,
            slide_count=slide_count,
            language=language,
            platform=platform,
            template_name=template_name,
            target_duration=target_duration,
        )
        ai_source = "openai_responses"
        ai_provider = "openai"
        ai_label = str(auth.get("label") or "OpenAI")
        ai_model = str(raw_storyboard.get("_model") or ai_model)
        ai_response_id = str(raw_storyboard.get("_response_id") or "")
    else:
        lines, storyboard_items = local_storyboard_result(
            prompt=prompt,
            slide_count=slide_count,
            language=language,
            platform=platform,
            template_name=template_name,
            target_duration=target_duration,
        )
        ai_error = "No OpenAI API key selected/configured; used local fallback."

    write_slide_script({"project": project, "lines": lines, "allowCountChange": True})
    settings = read_preview_settings(project)
    slides_settings = settings.get("slides") if isinstance(settings.get("slides"), dict) else {}
    slides_settings["scriptLines"] = lines
    slides_settings["transitionSounds"] = [str(item.get("transition") or "minimal") for item in storyboard_items]
    slides_settings["revealSounds"] = [str(item.get("reveal") or "ping") for item in storyboard_items]
    slides_settings["storyboard"] = clean_storyboard_items(storyboard_items, slide_count)
    settings["slides"] = slides_settings
    write_preview_settings({"project": project, "settings": settings})

    now = utc_now_iso()
    studio_state = update_studio_state(
        project,
        {
            "storyboard": {
                "source": ai_source,
                "provider": ai_provider,
                "connection_id": ai_connection_id or str(auth.get("connection_id") or ""),
                "connection_label": ai_label,
                "model": ai_model if ai_provider == "openai" else "",
                "response_id": ai_response_id,
                "fallback_reason": ai_error,
                "prompt": prompt,
                "language": language,
                "platform": platform,
                "template": template_name,
                "duration_mode": duration_mode,
                "target_duration": target_duration,
                "generated_at": now,
                "slides": slides_settings["storyboard"],
            },
            "last_ai_prompt": prompt,
        },
    )
    response = slide_composer_response(project)
    response["storyboard"] = studio_state.get("storyboard", {})
    return response


def append_log(job_id: str, text: str) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job["logs"] = (job.get("logs", "") + text)[-MAX_LOG_CHARS:]
        job["updated_at"] = time.time()


def set_job_state(job_id: str, **updates: object) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job.update(updates)
        job["updated_at"] = time.time()


def public_job(job: dict) -> dict:
    return {key: value for key, value in job.items() if key not in PRIVATE_JOB_FIELDS}


def redact_cli_command(cmd: list[str]) -> str:
    redacted: list[str] = []
    redact_next = False
    for part in cmd:
        text = str(part)
        if redact_next:
            redacted.append("[redacted]")
            redact_next = False
            continue
        if text in SENSITIVE_CLI_FLAGS:
            redacted.append(text)
            redact_next = True
            continue
        if any(text.startswith(f"{flag}=") for flag in SENSITIVE_CLI_FLAGS):
            flag, _, _value = text.partition("=")
            redacted.append(f"{flag}=[redacted]")
            continue
        redacted.append(text)
    return " ".join(shlex.quote(part) for part in redacted)


def job_cancel_requested(job_id: str) -> bool:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        return bool(job and job.get("cancel_requested"))


def failure_summary(logs: str, returncode: int) -> str:
    lines = [line.strip() for line in str(logs or "").splitlines() if line.strip()]
    skip_patterns = (
        r"^\$ ",
        r"^Traceback ",
        r"^File ",
        r"^\^+$",
        r"^Render failed with exit code ",
        r"returned non-zero exit status",
    )
    candidates = []
    for line in lines:
        if any(re.search(pattern, line) for pattern in skip_patterns):
            continue
        if re.search(r"(Missing ElevenLabs|ElevenLabs API failed|ElevenLabs TTS failed|Missing ElevenLabs SDK|Invalid ElevenLabs|ValueError:|RuntimeError:|ModuleNotFoundError:|FileNotFoundError:|❌)", line):
            candidates.append(line)
    if candidates:
        summary = candidates[-1]
        summary = re.sub(r"^❌\s*", "", summary)
        return re.sub(r"^(ValueError|RuntimeError|ModuleNotFoundError|FileNotFoundError):\s*", "", summary)
    for line in reversed(lines):
        if not any(re.search(pattern, line) for pattern in skip_patterns):
            return line
    return f"Render failed with exit code {returncode}."


def final_video_url(project: str) -> str:
    return f"/slide/{quote(project)}/output/final_video.mp4"


def normalize_render_studio_mode(value: object) -> str:
    mode = str(value or "").strip()
    if mode in {"voiceScript", "manual", "studio-manual"}:
        return "manual"
    if mode in {"import", "article", "studio-import"}:
        return "article"
    if mode in {"ai", "studio-ai"}:
        return "ai"
    return "studio"


def normalize_render_engine_label(value: object) -> str:
    engine = str(value or "").strip().lower()
    if engine in {"elevenlabs", "elevenlab"}:
        return "elevenlabs"
    if engine in {"elevenlabs-upload", "elevenlab-upload"}:
        return "elevenlabs-upload"
    if engine in {"edgetts", "edtts", "edge_tts", "edge-tts"}:
        return "edgetts"
    return slugify_project_name(engine or "render")


def archive_rendered_video(project_dir: Path, job_id: str, video_path: Path, *, engine: str = "", studio_mode: str = "") -> dict:
    if not video_path.is_file():
        raise FileNotFoundError(f"final_video.mp4 not found for project '{project_dir.name}'.")
    history_dir = (project_dir / "output" / RENDER_HISTORY_DIRNAME).resolve()
    try:
        history_dir.relative_to(project_dir.resolve())
    except ValueError as exc:
        raise ValueError("Invalid render history path.") from exc
    history_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    project_slug = slugify_project_name(project_dir.name)
    mode_label = normalize_render_studio_mode(studio_mode)
    engine_label = normalize_render_engine_label(engine)
    base_name = f"{project_slug}__{timestamp}__{mode_label}__{engine_label}"
    target = history_dir / f"{base_name}.mp4"
    for index in range(2, 1000):
        if not target.exists():
            break
        target = history_dir / f"{base_name}__v{index}.mp4"
    shutil.copy2(video_path, target)
    return {
        "name": target.name,
        "path": str(target),
        "url": render_history_url(project_dir.name, target.name),
        "size": target.stat().st_size,
        "job_id": job_id,
        "studio_mode": mode_label,
        "engine": engine_label,
    }


def run_job(job_id: str, cmd: list[str], project: str, engine: str = "", studio_mode: str = "") -> None:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    pretty_cmd = redact_cli_command(cmd)

    set_job_state(job_id, status="running", command=pretty_cmd, started_at=time.time())
    append_log(job_id, f"$ {pretty_cmd}\n\n")
    if job_cancel_requested(job_id):
        append_log(job_id, "Render stopped before process start.\n")
        set_job_state(job_id, status="cancelled", returncode=None, finished_at=time.time())
        return

    try:
        popen_kwargs = {}
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            if creationflags:
                popen_kwargs["creationflags"] = creationflags
        else:
            popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **popen_kwargs,
        )
        set_job_state(job_id, process=proc, pid=proc.pid)
    except Exception as exc:
        append_log(job_id, f"Failed to start render: {exc}\n")
        set_job_state(job_id, status="failed", returncode=-1, finished_at=time.time())
        return

    assert proc.stdout is not None
    for line in proc.stdout:
        append_log(job_id, line)

    returncode = proc.wait()
    set_job_state(job_id, process=None, pid=None)
    if job_cancel_requested(job_id):
        append_log(job_id, "\nRender stopped by user.\n")
        set_job_state(job_id, status="cancelled", returncode=returncode, finished_at=time.time())
        return
    try:
        project_dir = require_slide_project(project)
    except Exception:
        project_dir = project_lookup_root() / project
    video_path = project_dir / "output" / "final_video.mp4"

    if returncode == 0 and video_path.exists():
        try:
            render_copy = archive_rendered_video(project_dir, job_id, video_path, engine=engine, studio_mode=studio_mode)
            append_log(job_id, f"\nVersioned render: {render_copy['name']} -> {render_copy['path']}\n")
        except Exception as exc:
            render_copy = {}
            append_log(job_id, f"\nCould not create versioned render copy: {exc}\n")
        append_log(job_id, f"\nDone: {video_path}\n")
        set_job_state(
            job_id,
            status="done",
            returncode=returncode,
            finished_at=time.time(),
            video_url=final_video_url(project),
            render_url=render_copy.get("url"),
            render_name=render_copy.get("name", ""),
            render_path=render_copy.get("path", ""),
            render_studio_mode=render_copy.get("studio_mode", ""),
            render_engine=render_copy.get("engine", ""),
        )
    elif returncode == 0:
        append_log(job_id, "\nRender finished, but final_video.mp4 was not found.\n")
        set_job_state(job_id, status="failed", returncode=returncode, finished_at=time.time())
    else:
        with JOBS_LOCK:
            logs = str((JOBS.get(job_id) or {}).get("logs") or "")
        summary = failure_summary(logs, returncode)
        append_log(job_id, f"\nRender failed: {summary}\n")
        set_job_state(job_id, status="failed", returncode=returncode, finished_at=time.time(), error=summary)


def build_render_command(payload: dict) -> tuple[list[str], str]:
    project = str(payload.get("project") or "").strip()
    project_dir = require_slide_project(project)
    speed = coerce_speed(payload.get("speed", 1.1))
    render_size = coerce_render_size(payload.get("size", "1080x1920"))
    engine = str(payload.get("engine") or "").strip().lower()

    if engine in {"elevenlabs", "elevenlab"}:
        mode = str(payload.get("mode") or "tts").strip().lower()
        if mode == "upload":
            audio_path = decode_audio_payload(payload.get("audio", {}), project_dir)
            cmd = [
                str(RENDER_PYTHON),
                "-u",
                str(REPO_ROOT / "render_elevenlabs.py"),
                str(project_dir),
                str(audio_path),
                "--speed",
                f"{speed:g}",
                "--size",
                render_size,
            ]
            return cmd, "elevenlabs-upload"
        if mode not in {"tts", "api", "elevenlabs-tts"}:
            raise ValueError("ElevenLabs mode must be tts or upload.")
        voice = str(payload.get("voice") or "").strip()
        config = elevenlabs_config()
        connection_id = str(payload.get("connection_id") or payload.get("voice_connection_id") or payload.get("voiceConnectionId") or "").strip()
        render_auth = elevenlabs_render_auth_for_connection(connection_id) if connection_id else {}
        auth_config = dict(config)
        if render_auth.get("mode") == "proxy":
            auth_config["proxy_base_url"] = render_auth["proxy_base_url"]
            auth_config["proxy_key"] = render_auth["proxy_key"]
            elevenlabs_auth_configured(None, auth_config)
        elif render_auth.get("mode") == "direct":
            auth_config.pop("proxy_base_url", None)
            auth_config.pop("proxy_key", None)
            elevenlabs_auth_configured(str(render_auth.get("api_key") or ""), auth_config)
        else:
            elevenlabs_auth_configured(None, config)
        elevenlabs_voice_id(voice or None, config)
        if render_auth.get("mode") == "direct":
            elevenlabs_api_key(str(render_auth.get("api_key") or ""), auth_config)
            try:
                import elevenlabs.client
            except ImportError as exc:
                raise ValueError("Missing ElevenLabs SDK. Run ./setup_and_run.sh or pip install -r requirements.txt.") from exc
        elif not render_auth and not elevenlabs_proxy_base_url(config):
            elevenlabs_api_key(None, config)
            try:
                import elevenlabs.client
            except ImportError as exc:
                raise ValueError("Missing ElevenLabs SDK. Run ./setup_and_run.sh or pip install -r requirements.txt.") from exc
        cmd = [
            str(RENDER_PYTHON),
            "-u",
            str(REPO_ROOT / "render_elevenlabs_tts.py"),
            str(project_dir),
            "--speed",
            f"{speed:g}",
            "--size",
            render_size,
        ]
        if voice:
            if not re.fullmatch(r"[A-Za-z0-9._-]+", voice):
                raise ValueError("Invalid ElevenLabs voice id.")
            cmd.extend(["--voice", voice])
        if render_auth.get("mode") == "proxy":
            cmd.extend(["--proxy-base-url", str(render_auth.get("proxy_base_url") or "")])
            cmd.extend(["--proxy-key", str(render_auth.get("proxy_key") or "")])
        elif render_auth.get("mode") == "direct":
            cmd.extend(["--api-key", str(render_auth.get("api_key") or ""), "--no-proxy"])
        model_id = str(payload.get("modelId") or "").strip()
        if model_id:
            if not re.fullmatch(r"[A-Za-z0-9._-]+", model_id):
                raise ValueError("Invalid ElevenLabs model id.")
            cmd.extend(["--model-id", model_id])
        output_format = str(payload.get("outputFormat") or "").strip()
        if output_format:
            if not re.fullmatch(r"[A-Za-z0-9._-]+", output_format):
                raise ValueError("Invalid ElevenLabs output format.")
            cmd.extend(["--output-format", output_format])
        if bool(payload.get("force")):
            cmd.append("--force")
        return cmd, "elevenlabs"

    if engine in {"edgetts", "edtts", "edge_tts", "edge-tts"}:
        voice = str(payload.get("voice") or "vi-VN-HoaiMyNeural").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]+", voice):
            raise ValueError("Invalid Edge TTS voice name.")
        per_slide = bool(payload.get("edgePerSlide") or payload.get("edge_per_slide") or payload.get("perSlide"))
        cmd = [
            str(RENDER_PYTHON),
            "-u",
            str(REPO_ROOT / "render_edgetts.py"),
            str(project_dir),
            "--speed",
            f"{speed:g}",
            "--voice",
            voice,
            "--size",
            render_size,
            "--edge-mode",
            "per-slide" if per_slide else "full",
        ]
        if bool(payload.get("force")):
            cmd.append("--force")
        return cmd, "edgetts"

    raise ValueError("Engine must be elevenlabs or edgetts.")


def build_voice_preview_command(payload: dict) -> tuple[list[str], str, str]:
    project = str(payload.get("project") or "").strip()
    project_dir = require_slide_project(project)
    if not project_script_text(project_dir):
        raise ValueError("Project script is empty. Generate storyboard first.")
    speed = coerce_speed(payload.get("speed", 1.1))
    engine = str(payload.get("engine") or "elevenlabs").strip().lower()
    if engine in {"elevenlabs", "elevenlab"}:
        mode = str(payload.get("mode") or "tts").strip().lower()
        if mode in {"upload", "file"}:
            raise ValueError("Voice preview API chỉ hỗ trợ TTS. Với upload audio, hãy nghe file upload trực tiếp.")
        voice = str(payload.get("voice") or "").strip()
        config = elevenlabs_config()
        connection_id = str(payload.get("connection_id") or payload.get("voice_connection_id") or payload.get("voiceConnectionId") or "").strip()
        render_auth = elevenlabs_render_auth_for_connection(connection_id) if connection_id else {}
        auth_config = dict(config)
        if render_auth.get("mode") == "proxy":
            auth_config["proxy_base_url"] = render_auth["proxy_base_url"]
            auth_config["proxy_key"] = render_auth["proxy_key"]
            elevenlabs_auth_configured(None, auth_config)
        elif render_auth.get("mode") == "direct":
            auth_config.pop("proxy_base_url", None)
            auth_config.pop("proxy_key", None)
            elevenlabs_auth_configured(str(render_auth.get("api_key") or ""), auth_config)
        else:
            elevenlabs_auth_configured(None, config)
        elevenlabs_voice_id(voice or None, config)
        cmd = [
            str(RENDER_PYTHON),
            "-u",
            str(REPO_ROOT / "generate_tts.py"),
            str(project_dir),
            "--engine",
            "elevenlabs",
            "--voice-speed",
            f"{speed:g}",
        ]
        if voice:
            if not re.fullmatch(r"[A-Za-z0-9._-]+", voice):
                raise ValueError("Invalid ElevenLabs voice id.")
            cmd.extend(["--voice", voice])
        if render_auth.get("mode") == "proxy":
            cmd.extend(["--proxy-base-url", str(render_auth.get("proxy_base_url") or "")])
            cmd.extend(["--proxy-key", str(render_auth.get("proxy_key") or "")])
        elif render_auth.get("mode") == "direct":
            cmd.extend(["--api-key", str(render_auth.get("api_key") or "")])
        model_id = str(payload.get("modelId") or "").strip()
        if model_id:
            if not re.fullmatch(r"[A-Za-z0-9._-]+", model_id):
                raise ValueError("Invalid ElevenLabs model id.")
            cmd.extend(["--model-id", model_id])
        output_format = str(payload.get("outputFormat") or "").strip()
        if output_format:
            if not re.fullmatch(r"[A-Za-z0-9._-]+", output_format):
                raise ValueError("Invalid ElevenLabs output format.")
            cmd.extend(["--output-format", output_format])
        if bool(payload.get("force")):
            cmd.append("--force")
        return cmd, "elevenlabs", voice_preview_signature(project, payload)

    if engine in {"edgetts", "edtts", "edge_tts", "edge-tts"}:
        voice = str(payload.get("voice") or "vi-VN-HoaiMyNeural").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]+", voice):
            raise ValueError("Invalid Edge TTS voice name.")
        cmd = [
            str(RENDER_PYTHON),
            "-u",
            str(REPO_ROOT / "generate_tts.py"),
            str(project_dir),
            "--engine",
            "edgetts",
            "--voice",
            voice,
            "--edge-mode",
            "full",
        ]
        if bool(payload.get("force")):
            cmd.append("--force")
        return cmd, "edgetts", voice_preview_signature(project, payload)

    raise ValueError("Engine must be elevenlabs or edgetts.")


def run_voice_preview_job(job_id: str, cmd: list[str], project: str, engine: str, signature: str, payload: dict) -> None:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    pretty_cmd = redact_cli_command(cmd)
    set_job_state(job_id, status="running", command=pretty_cmd, started_at=time.time())
    append_log(job_id, f"$ {pretty_cmd}\n\n")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        set_job_state(job_id, process=proc, pid=proc.pid)
    except Exception as exc:
        append_log(job_id, f"Failed to start voice preview: {exc}\n")
        set_job_state(job_id, status="failed", returncode=-1, finished_at=time.time(), error=str(exc))
        return

    assert proc.stdout is not None
    for line in proc.stdout:
        append_log(job_id, line)

    returncode = proc.wait()
    set_job_state(job_id, process=None, pid=None)
    project_dir = require_slide_project(project)
    audio_path = project_dir / "output" / "voiceover_concat.mp3"
    timing_path = project_dir / "output" / "timing.json"
    if returncode == 0 and audio_path.is_file() and timing_path.is_file():
        duration = 0.0
        try:
            timing = json.loads(timing_path.read_text(encoding="utf-8"))
            if isinstance(timing, list):
                duration = sum(float(item.get("duration", 0)) for item in timing if isinstance(item, dict))
        except Exception:
            duration = 0.0
        preview = {
            "current": True,
            "stale": False,
            "signature": signature,
            "engine": engine,
            "mode": str(payload.get("mode") or "tts"),
            "voice": str(payload.get("voice") or ""),
            "connection_id": str(payload.get("connection_id") or payload.get("voice_connection_id") or payload.get("voiceConnectionId") or ""),
            "speed": f"{coerce_speed(payload.get('speed', 1.1)):g}",
            "audio_url": f"{project_url(project)}output/voiceover_concat.mp3",
            "timing_url": f"{project_url(project)}output/timing.json",
            "duration": round(duration, 3),
            "updated_at": utc_now_iso(),
            "job_id": job_id,
        }
        update_studio_state(project, {"voice_preview": preview})
        append_log(job_id, f"\nVoice preview ready: {audio_path}\n")
        set_job_state(
            job_id,
            status="done",
            returncode=returncode,
            finished_at=time.time(),
            audio_url=preview["audio_url"],
            timing_url=preview["timing_url"],
            duration=preview["duration"],
            voice_preview=preview,
        )
    else:
        with JOBS_LOCK:
            logs = str((JOBS.get(job_id) or {}).get("logs") or "")
        summary = failure_summary(logs, returncode)
        append_log(job_id, f"\nVoice preview failed: {summary}\n")
        set_job_state(job_id, status="failed", returncode=returncode, finished_at=time.time(), error=summary)


def create_voice_preview_job(payload: dict) -> dict:
    project = str(payload.get("project") or "").strip()
    require_slide_project(project)
    if has_running_job(project):
        raise RuntimeError(f"Project '{project}' already has a running job.")
    cmd, engine, signature = build_voice_preview_command(payload)
    job_id = uuid.uuid4().hex[:12]
    now = time.time()
    job = {
        "id": job_id,
        "type": "voice_preview",
        "project": project,
        "engine": engine,
        "status": "queued",
        "logs": "",
        "created_at": now,
        "updated_at": now,
    }
    with JOBS_LOCK:
        JOBS[job_id] = job
    thread = threading.Thread(target=run_voice_preview_job, args=(job_id, cmd, project, engine, signature, dict(payload)), daemon=True)
    thread.start()
    return public_job(job)


def render_requires_voice_preview(payload: dict) -> bool:
    engine = str(payload.get("engine") or "").strip().lower()
    mode = str(payload.get("mode") or "tts").strip().lower()
    return engine in {"elevenlabs", "elevenlab"} and mode not in {"upload", "file"}


def has_running_job(project: str) -> bool:
    with JOBS_LOCK:
        return any(
            job.get("project") == project and job.get("status") in ACTIVE_JOB_STATUSES
            for job in JOBS.values()
        )


def create_job(payload: dict) -> dict:
    project = str(payload.get("project") or "").strip()
    require_slide_project(project)

    if has_running_job(project):
        raise RuntimeError(f"Project '{project}' already has a running render job.")
    if render_requires_voice_preview(payload):
        preview = voice_preview_state(project, payload)
        if not preview.get("current"):
            raise RuntimeError("Voice preview đã cũ hoặc chưa có. Hãy bấm Preview voice trước khi Generate video.")

    cmd, engine = build_render_command(payload)
    studio_mode = normalize_render_studio_mode(payload.get("input_mode") or payload.get("inputMode") or payload.get("studio_mode"))
    job_id = uuid.uuid4().hex[:12]
    now = time.time()
    job = {
        "id": job_id,
        "project": project,
        "engine": engine,
        "status": "queued",
        "logs": "",
        "created_at": now,
        "updated_at": now,
        "video_url": None,
        "render_url": None,
        "render_name": "",
        "studio_mode": studio_mode,
    }

    with JOBS_LOCK:
        JOBS[job_id] = job

    thread = threading.Thread(target=run_job, args=(job_id, cmd, project, engine, studio_mode), daemon=True)
    thread.start()
    return public_job(job)


def get_job(job_id: str) -> dict | None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        return public_job(job) if job else None


def terminate_process(proc: subprocess.Popen) -> bool:
    if proc.poll() is not None:
        return True
    try:
        if os.name == "nt":
            proc.terminate()
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        proc.terminate()
    try:
        proc.wait(timeout=5)
        return True
    except subprocess.TimeoutExpired:
        pass
    try:
        if os.name == "nt":
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        proc.kill()
    try:
        proc.wait(timeout=5)
        return True
    except subprocess.TimeoutExpired:
        return False


def cancel_job(job_id: str) -> dict | None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return None
        status = str(job.get("status") or "")
        if status not in ACTIVE_JOB_STATUSES:
            return public_job(job)
        job["cancel_requested"] = True
        job["status"] = "cancelling"
        job["updated_at"] = time.time()
        proc = job.get("process")

    append_log(job_id, "\nStop requested by user.\n")
    if isinstance(proc, subprocess.Popen):
        if terminate_process(proc):
            set_job_state(job_id, status="cancelled", returncode=proc.returncode, process=None, pid=None, finished_at=time.time())
    else:
        set_job_state(job_id, status="cancelled", returncode=None, finished_at=time.time())
    return get_job(job_id)


def delete_project_output(payload: dict) -> dict:
    project = str(payload.get("project") or "").strip()
    confirm = bool(payload.get("confirm"))
    if not confirm:
        raise ValueError("Missing delete confirmation.")
    if has_running_job(project):
        raise RuntimeError(f"Project '{project}' has a running render job.")

    project_dir = require_slide_project(project)
    output_dir = (project_dir / "output").resolve()
    try:
        output_dir.relative_to(project_dir.resolve())
    except ValueError as exc:
        raise ValueError("Invalid output path.") from exc
    if output_dir.name != "output":
        raise ValueError("Refusing to delete a non-output directory.")

    existed = output_dir.is_dir() and any(output_dir.iterdir())
    if output_dir.exists() and not output_dir.is_dir():
        raise ValueError("Output path exists but is not a directory.")
    if existed:
        shutil.rmtree(output_dir)

    return {
        "ok": True,
        "project": project_dir.name,
        "deleted": existed,
        "output_url": final_video_url(project_dir.name),
    }


def reveal_project_output(payload: dict) -> dict:
    project = str(payload.get("project") or "").strip()
    project_dir = require_slide_project(project)
    output_dir = (project_dir / "output").resolve()
    video_path = (output_dir / "final_video.mp4").resolve()
    try:
        output_dir.relative_to(project_dir.resolve())
        video_path.relative_to(project_dir.resolve())
    except ValueError as exc:
        raise ValueError("Invalid output path.") from exc
    if not video_path.is_file():
        raise FileNotFoundError(f"final_video.mp4 not found for project '{project_dir.name}'.")

    if sys.platform == "darwin":
        cmd = ["open", "-R", str(video_path)]
    elif sys.platform.startswith("win"):
        cmd = ["explorer", f"/select,{video_path}"]
    else:
        opener = shutil.which("xdg-open")
        if not opener:
            raise RuntimeError("No supported file manager opener found.")
        cmd = [opener, str(output_dir)]

    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {
        "ok": True,
        "project": project_dir.name,
        "path": str(video_path),
        "output_dir": str(output_dir),
    }




def social_callback_html(title: str, message: str, ok: bool = True) -> bytes:
    color = "#f2b261" if ok else "#ff8585"
    return f"""<!doctype html>
<html lang="vi">
<head><meta charset="utf-8"><title>{html.escape(title)}</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#070b0a;color:#eefcf4;padding:32px;">
  <main style="max-width:680px;margin:0 auto;border:1px solid rgba(255,255,255,.14);border-radius:20px;padding:24px;background:rgba(255,255,255,.06);">
    <h1 style="margin-top:0;color:{color};">{html.escape(title)}</h1>
    <p style="line-height:1.6;">{html.escape(message)}</p>
    <p style="opacity:.72;">Bạn có thể đóng tab này và quay lại Publish Studio.</p>
  </main>
</body>
</html>""".encode("utf-8")


def render_elevenlabs_guide_html() -> bytes:
    return render_page_shell(
        title="Hướng dẫn ElevenLabs",
        body="""
  <main class="guide-page">
    <header class="guide-hero">
      <a class="small-link back-link" href="/">← Quay lại Web UI</a>
      <h1>Hướng dẫn ElevenLabs</h1>
      <p>Dùng cách thủ công khi bạn muốn tự nghe và tải file audio. Dùng API khi muốn Web UI tự gọi ElevenLabs rồi render luôn.</p>
      <div class="guide-links">
        <a href="https://elevenlabs.io/app/speech-synthesis/text-to-speech" target="_blank" rel="noreferrer">Mở ElevenLabs Dashboard</a>
        <a class="small-link" href="https://elevenlabs.io/app/voice-library?search=nh%E1%BA%ADt+" target="_blank" rel="noreferrer">Mở Voice Library</a>
        <a class="small-link" href="https://elevenlabs.io/app/subscription/api" target="_blank" rel="noreferrer">Nạp API credit</a>
        <a class="small-link" href="https://elevenlabs.io/app/developers/api-keys" target="_blank" rel="noreferrer">Tạo API key</a>
      </div>
    </header>

    <section class="guide-card" id="manual">
      <div class="guide-copy">
        <p class="kicker">Tải file audio</p>
        <h2>Cách làm thủ công</h2>
        <ol>
          <li>Bấm mở ElevenLabs Dashboard.</li>
          <li>Dán toàn bộ `script-90s.txt` vào vùng nhập chữ.</li>
          <li>Chọn voice “Nhật - Narrative & Compelling”.</li>
          <li>Chọn model Eleven v3, generate rồi tải audio về.</li>
          <li>Quay lại Web UI, chọn file audio và render.</li>
        </ol>
      </div>
      <div class="guide-shot tts-shot" aria-label="Hướng dẫn ElevenLabs Text to Speech">
        <div class="shot-sidebar"><strong>ElevenLabs</strong><span>Text to Speech</span><span>Voices</span><span>Studio</span></div>
        <div class="shot-main">
          <div class="shot-top">Text to Speech</div>
          <div class="script-zone callout script-callout">
            <span class="voice-pill callout voice-callout">Nhật - Narrative & Compelling</span>
            <p>Dán script-90s.txt vào đây</p>
          </div>
        </div>
        <div class="shot-settings">
          <strong>Settings</strong>
          <div class="setting-row callout voice-callout">Voice: Nhật...</div>
          <div class="setting-row callout model-callout">Model: Eleven v3</div>
          <div class="setting-row">Output: MP3 44.1kHz</div>
        </div>
        <div class="note-pin note-script">Dán script</div>
        <div class="note-pin note-voice">Voice Nhật</div>
        <div class="note-pin note-model">Model v3</div>
      </div>
      <a href="https://elevenlabs.io/app/speech-synthesis/text-to-speech" target="_blank" rel="noreferrer">Mở ElevenLabs Dashboard</a>
    </section>

    <section class="guide-card" id="api-credit">
      <div class="guide-copy">
        <p class="kicker">ElevenLabs API</p>
        <h2>Nạp credit trước khi test</h2>
        <ol>
          <li>Vào trang ElevenAPI subscription.</li>
          <li>Bấm Add credits.</li>
          <li>Nạp khoảng 5 đô để test trước, đừng nạp nhiều khi chưa rõ workflow.</li>
        </ol>
      </div>
      <div class="guide-shot billing-shot" aria-label="Hướng dẫn nạp credit ElevenLabs API">
        <div class="shot-sidebar"><strong>ElevenLabs</strong><span>Home</span><span>Text to Speech</span><span>Developers</span></div>
        <div class="billing-main">
          <h3>Subscription</h3>
          <div class="billing-tabs"><span>ElevenCreative</span><span class="active">ElevenAPI</span></div>
          <div class="balance-box">
            <span>Top up balance</span>
            <strong>$2.43</strong>
            <button class="callout credit-callout">+ Add credits</button>
          </div>
          <div class="pricing-row"><span>Multilingual v2 / v3</span><b>$0.10</b><small>per 1K characters</small></div>
        </div>
        <div class="note-pin note-credit">Nạp $5 để test</div>
      </div>
      <a href="https://elevenlabs.io/app/subscription/api" target="_blank" rel="noreferrer">Mở trang nạp API credit</a>
    </section>

    <section class="guide-card" id="api-key">
      <div class="guide-copy">
        <p class="kicker">API key</p>
        <h2>Tạo key rồi lưu vào Web UI</h2>
        <ol>
          <li>Vào Developers → API Keys.</li>
          <li>Bấm Create Key.</li>
          <li>Copy key, quay lại Web UI và dán vào ô ElevenLabs API key.</li>
        </ol>
      </div>
      <div class="guide-shot api-shot" aria-label="Hướng dẫn tạo ElevenLabs API key">
        <div class="shot-sidebar"><strong>ElevenLabs</strong><span>Home</span><span>Text to Speech</span><span>Developers</span></div>
        <div class="api-main">
          <h3>Developers</h3>
          <div class="api-tabs"><span>Overview</span><span class="active">API Keys</span><span>Webhooks</span><span>Analytics</span></div>
          <p>An API key lets you connect to the API.</p>
          <button class="callout key-callout">+ Create Key</button>
          <div class="key-row"><span>main</span><span>••••••••••••a283</span><span>Enabled</span></div>
        </div>
        <div class="note-pin note-key">Tạo API key</div>
      </div>
      <a href="https://elevenlabs.io/app/developers/api-keys" target="_blank" rel="noreferrer">Mở trang tạo API key</a>
    </section>

    <section class="guide-card" id="voice-id">
      <div class="guide-copy">
        <p class="kicker">Voice Library</p>
        <h2>Lấy Voice ID của giọng Nhật</h2>
        <ol>
          <li>Mở Voice Library với từ khóa “nhật” đã điền sẵn.</li>
          <li>Tìm dòng “Nhật - Narrative & Compelling”.</li>
          <li>Bấm menu ba chấm rồi chọn Copy voice ID.</li>
          <li>Quay lại Web UI, dán vào ô Voice ID trong phần nâng cao.</li>
        </ol>
      </div>
      <div class="guide-shot voice-shot" aria-label="Hướng dẫn lấy ElevenLabs Voice ID">
        <div class="shot-sidebar"><strong>ElevenLabs</strong><span>Home</span><span class="active-side">Voices</span><span>Studio</span><span>Flows</span></div>
        <div class="voice-main">
          <div class="voice-breadcrumb">Voices › Explore</div>
          <h3>Voices</h3>
          <div class="voice-tabs"><span class="active">Explore</span><span>My Voices</span></div>
          <div class="voice-search">⌕ <span>nhật</span></div>
          <div class="voice-filters"><span>Language</span><span>Narration</span><span>Characters</span><span>Social Media</span><span>Educational</span></div>
          <p class="voice-count">2,721 voices</p>
          <div class="voice-list">
            <div class="voice-row featured">
              <div class="voice-avatar"></div>
              <div class="voice-title"><strong>Nhật - Narrative & Compelling</strong><span>Articulate Vietnamese voice suited...</span></div>
              <span>Vietnamese</span><span>Northern</span><span>45.8K</span><span>Narration</span>
              <button class="voice-dots callout voice-dots-callout" type="button">⋮</button>
            </div>
            <div class="voice-row muted-row"><div class="voice-avatar dark"></div><div class="voice-title"><strong>Finn - The British voice that makes ...</strong><span>I'm Finn, a British voice artist with...</span></div><span>English</span><span>British</span><span>15.4K</span><span>Conversational</span><button class="voice-dots" type="button">⋮</button></div>
            <div class="voice-row muted-row"><div class="voice-avatar blue"></div><div class="voice-title"><strong>Suhaan - Calm, Clear and Neat</strong><span>Suhaan - Delhi Guy - Suhan is a...</span></div><span>Hindi</span><span>Standard</span><span>44.2K</span><span>Conversational</span><button class="voice-dots" type="button">⋮</button></div>
          </div>
          <div class="voice-menu callout voice-id-callout">
            <div class="voice-copy-row">⧉ <strong>Copy voice ID</strong></div>
            <div>▱ Add to collection</div>
            <div>≋ View similar</div>
          </div>
        </div>
        <div class="note-pin note-voice-id">Khoanh chỗ lấy Voice ID</div>
      </div>
      <a href="https://elevenlabs.io/app/voice-library?search=nh%E1%BA%ADt+" target="_blank" rel="noreferrer">Mở Voice Library đã search “nhật”</a>
    </section>
  </main>
""",
        extra_style="""
    body { overflow: auto; }
    .guide-page { max-width: 1180px; margin: 0 auto; }
    .guide-hero { max-width: none; margin-bottom: 22px; }
    .guide-hero h1 {
      max-width: none;
      font-size: clamp(34px, 4vw, 54px);
      white-space: nowrap;
    }
    .back-link { margin-bottom: 18px; }
    .guide-links { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 18px; }
    .guide-card {
      display: grid;
      grid-template-columns: minmax(240px, 0.72fr) minmax(420px, 1.28fr);
      gap: 20px;
      align-items: center;
      border: 1px solid var(--line);
      border-radius: 28px;
      padding: 22px;
      background: var(--panel);
      box-shadow: var(--shadow);
      margin: 18px 0;
    }
    .guide-copy h2 { margin: 0 0 12px; font-size: clamp(26px, 3vw, 42px); letter-spacing: 0; }
    .guide-copy ol { margin: 0; padding-left: 20px; color: var(--text-soft); line-height: 1.65; font-weight: 700; }
    .kicker { margin: 0 0 8px; color: var(--accent); font-size: 12px; font-weight: 950; letter-spacing: 0.14em; text-transform: uppercase; }
    .guide-shot {
      position: relative;
      min-height: 360px;
      border: 1px solid rgba(0,0,0,.12);
      border-radius: 22px;
      overflow: hidden;
      background: #f7f7f7;
      color: #171717;
      box-shadow: 0 22px 60px rgba(0,0,0,.22);
      font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    .shot-sidebar {
      position: absolute;
      left: 0;
      top: 0;
      bottom: 0;
      width: 170px;
      padding: 18px 16px;
      border-right: 1px solid #ddd;
      background: #fbfbfb;
      display: grid;
      align-content: start;
      gap: 18px;
      color: #6b6b78;
      font-weight: 700;
    }
    .shot-sidebar strong { color: #080808; font-size: 20px; }
    .shot-sidebar .active-side {
      margin-left: -8px;
      margin-right: -8px;
      padding: 8px;
      border-radius: 12px;
      background: #ededed;
      color: #151515;
    }
    .shot-main { margin-left: 170px; margin-right: 280px; min-height: 360px; padding: 18px 22px; background: #fff; }
    .shot-top { font-weight: 800; font-size: 18px; margin-bottom: 56px; }
    .script-zone {
      position: relative;
      height: 174px;
      border-radius: 18px;
      background: linear-gradient(#fff,#fff) padding-box, linear-gradient(135deg,#38bdf8,#f472b6,#fb923c) border-box;
      border: 3px solid transparent;
      padding: 24px;
    }
    .script-zone p { color: #8a8a96; font-size: 18px; margin: 24px 0 0; }
    .voice-pill { display: inline-flex; padding: 9px 13px; border: 2px solid #0f172a; border-radius: 999px; background: #fff; font-weight: 800; }
    .shot-settings {
      position: absolute;
      right: 0;
      top: 0;
      bottom: 0;
      width: 280px;
      padding: 70px 16px 18px;
      border-left: 1px solid #ddd;
      background: #fff;
      display: grid;
      align-content: start;
      gap: 14px;
    }
    .shot-settings strong { font-size: 18px; margin-bottom: 10px; }
    .setting-row { padding: 14px 12px; border: 1px solid #dedee5; border-radius: 14px; background: #fff; font-weight: 800; }
    .billing-main, .api-main { margin-left: 170px; min-height: 360px; padding: 30px; background: #fff; }
    .billing-main h3, .api-main h3 { margin: 0 0 18px; font-size: 34px; font-weight: 500; }
    .billing-tabs, .api-tabs { display: flex; gap: 18px; padding-bottom: 12px; border-bottom: 1px solid #e5e5e5; color: #777985; font-weight: 700; }
    .billing-tabs .active, .api-tabs .active { color: #111; border: 2px solid #111; border-radius: 10px; padding: 7px 12px; margin-top: -9px; }
    .balance-box { width: min(520px, 92%); margin-top: 36px; border: 1px solid #ddd; border-radius: 20px; padding: 22px; box-shadow: 0 8px 20px rgba(0,0,0,.05); }
    .balance-box span { color: #7a7d89; font-weight: 700; }
    .balance-box strong { display: block; font-size: 32px; margin: 8px 0; }
    .balance-box button, .api-main button { border: 0; border-radius: 12px; padding: 13px 18px; background: #050505; color: #fff; font-weight: 800; font-size: 17px; }
    .pricing-row { margin-top: 40px; width: 290px; border: 1px solid #e1e1e1; border-radius: 18px; padding: 22px; display: grid; gap: 14px; }
    .pricing-row b { font-size: 28px; }
    .pricing-row small { color: #7a7d89; font-size: 16px; }
    .api-main p { color: #777985; font-weight: 700; max-width: 520px; line-height: 1.5; }
    .api-main button { position: absolute; right: 26px; top: 142px; }
    .key-row { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; margin-top: 86px; border-top: 1px solid #e5e5e5; padding-top: 18px; font-weight: 700; }
    .callout { box-shadow: 0 0 0 4px rgba(244,114,182,.35), 0 0 0 8px rgba(56,189,248,.22) !important; }
    .note-pin {
      position: absolute;
      z-index: 5;
      padding: 9px 12px;
      border-radius: 999px;
      color: #fff;
      background: #e11d48;
      font-weight: 950;
      font-size: 13px;
      box-shadow: 0 10px 24px rgba(225,29,72,.24);
    }
    .note-script { left: 250px; top: 94px; }
    .note-voice { right: 126px; top: 118px; }
    .note-model { right: 56px; top: 220px; }
    .voice-shot { min-height: 420px; background: #fff; }
    .voice-main {
      position: relative;
      margin-left: 170px;
      min-height: 420px;
      padding: 18px 22px 20px;
      background: #fff;
    }
    .voice-breadcrumb { color: #5f6068; font-size: 16px; font-weight: 700; margin-bottom: 20px; }
    .voice-main h3 { margin: 0 0 14px; font-size: 32px; font-weight: 500; }
    .voice-tabs {
      display: flex;
      gap: 18px;
      align-items: center;
      margin-bottom: 14px;
      font-weight: 750;
      color: #777985;
    }
    .voice-tabs span { padding: 9px 11px; border-radius: 11px; }
    .voice-tabs .active { color: #121212; border: 1px solid #d7d7d7; border-bottom: 3px solid #121212; }
    .voice-search {
      height: 42px;
      border: 1px solid #e3e3e8;
      border-radius: 14px;
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 0 14px;
      color: #9798a3;
      font-size: 20px;
      font-weight: 700;
    }
    .voice-search span { color: #222; font-size: 18px; font-weight: 500; }
    .voice-filters { display: flex; gap: 8px; overflow: hidden; margin: 12px 0 18px; white-space: nowrap; }
    .voice-filters span {
      border: 1px solid #e2e2e7;
      border-radius: 12px;
      padding: 9px 12px;
      color: #5f6068;
      font-weight: 750;
      background: #fff;
    }
    .voice-count { margin: 0 0 10px; color: #6a6b73; font-weight: 800; }
    .voice-list { display: grid; gap: 0; border-radius: 16px; overflow: hidden; }
    .voice-row {
      position: relative;
      display: grid;
      grid-template-columns: 40px minmax(180px, 1fr) 92px 82px 72px 118px 34px;
      align-items: center;
      gap: 10px;
      min-height: 58px;
      padding: 6px 10px;
      font-size: 14px;
      color: #242424;
    }
    .voice-row.featured { background: #f0f0f2; }
    .muted-row { color: #4b4c54; }
    .voice-avatar {
      width: 34px;
      height: 34px;
      border-radius: 50%;
      background: radial-gradient(circle at 30% 30%, #90e0ef, #a78bfa 45%, #475569);
    }
    .voice-avatar.dark { background: radial-gradient(circle at 35% 35%, #fde68a, #7c2d12 50%, #111827); }
    .voice-avatar.blue { background: radial-gradient(circle at 35% 35%, #bae6fd, #64748b 55%, #111827); }
    .voice-title { display: grid; gap: 2px; min-width: 0; }
    .voice-title strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 14px; }
    .voice-title span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #7a7b84; font-weight: 650; }
    .voice-dots {
      width: 30px;
      height: 30px;
      border: 0;
      border-radius: 10px;
      background: #d7d7dd;
      color: #252525;
      font-size: 20px;
      font-weight: 900;
    }
    .voice-menu {
      position: absolute;
      right: 18px;
      top: 232px;
      width: 176px;
      border: 1px solid #dedee4;
      border-radius: 14px;
      background: #fff;
      box-shadow: 0 16px 34px rgba(0,0,0,.18);
      padding: 8px;
      display: grid;
      gap: 2px;
      z-index: 4;
    }
    .voice-menu div {
      padding: 9px 10px;
      border-radius: 10px;
      font-size: 14px;
      font-weight: 750;
      color: #1f1f1f;
    }
    .voice-copy-row {
      background: rgba(244,114,182,.08);
      outline: 3px solid rgba(244,114,182,.45);
    }
    .note-voice-id { right: 38px; top: 184px; }
    .note-credit { left: 300px; top: 195px; }
    .note-key { right: 44px; top: 94px; }
    @media (max-width: 920px) {
      .guide-card { grid-template-columns: 1fr; }
      .guide-hero h1 { white-space: normal; }
      .guide-shot { overflow-x: auto; }
    }
""",
    )


UPLOAD_GUIDE_DOCS = {
    "youtube": {
        "path": REPO_ROOT / "docs" / "upload" / "youtube-api-upload.md",
        "kicker": "YouTube API",
        "actions": [
            ("Mở Google Cloud Console", "https://console.cloud.google.com/"),
            ("Docs upload video", "https://developers.google.com/youtube/v3/guides/uploading_a_video"),
            ("Docs OAuth", "https://developers.google.com/youtube/v3/guides/authentication"),
        ],
    },
    "facebook": {
        "path": REPO_ROOT / "docs" / "upload" / "facebook-api-upload.md",
        "kicker": "Facebook Reels API",
        "actions": [
            ("Mở Meta for Developers", "https://developers.facebook.com/"),
            ("Graph API Explorer", "https://developers.facebook.com/tools/explorer/"),
            ("Token Debugger", "https://developers.facebook.com/tools/debug/accesstoken/"),
        ],
    },
}


def markdown_doc_url(target: str, markdown_path: Path) -> str:
    target = str(target or "").strip()
    if re.match(r"^https?://", target):
        return target
    asset_path = (markdown_path.parent / target).resolve()
    try:
        relative = asset_path.relative_to(REPO_ROOT)
    except ValueError:
        return "#"
    return "/" + quote(relative.as_posix(), safe="/._-")


def render_inline_markdown(text: str) -> str:
    rendered = html.escape(text)
    rendered = re.sub(r"`([^`]+)`", r"<code>\1</code>", rendered)
    rendered = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", rendered)

    def link_repl(match: re.Match) -> str:
        url = match.group(0)
        trailing = ""
        while url and url[-1] in ".,)":
            trailing = url[-1] + trailing
            url = url[:-1]
        escaped_url = html.escape(url, quote=True)
        return f'<a class="text-link" href="{escaped_url}" target="_blank" rel="noreferrer">{html.escape(url)}</a>{trailing}'

    return re.sub(r"https?://[^\s<]+", link_repl, rendered)


def render_markdown_blocks(lines: list[str], markdown_path: Path) -> str:
    blocks: list[str] = []
    paragraph: list[str] = []
    image_pattern = re.compile(r"^!\[([^\]]*)\]\(([^)]+)\)\s*$")

    def flush_paragraph() -> None:
        if paragraph:
            text = " ".join(part.strip() for part in paragraph if part.strip())
            if text:
                blocks.append(f"<p>{render_inline_markdown(text)}</p>")
            paragraph.clear()

    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            index += 1
            continue

        if stripped.startswith("```"):
            flush_paragraph()
            code_lines: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code_lines.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            code = html.escape("\n".join(code_lines))
            blocks.append(f"<pre><code>{code}</code></pre>")
            continue

        heading_match = re.match(r"^(#{3,6})\s+(.+)$", stripped)
        if heading_match:
            flush_paragraph()
            level = min(len(heading_match.group(1)), 4)
            blocks.append(f"<h{level}>{render_inline_markdown(heading_match.group(2))}</h{level}>")
            index += 1
            continue

        image_match = image_pattern.match(stripped)
        if image_match:
            flush_paragraph()
            images: list[str] = []
            while index < len(lines):
                next_match = image_pattern.match(lines[index].strip())
                if not next_match:
                    break
                alt = next_match.group(1).strip()
                src = markdown_doc_url(next_match.group(2), markdown_path)
                images.append(
                    f'<figure><img src="{html.escape(src, quote=True)}" alt="{html.escape(alt, quote=True)}" /></figure>'
                )
                index += 1
            grid_class = "single" if len(images) == 1 else "multi"
            blocks.append(f'<div class="guide-image-grid {grid_class}">' + "".join(images) + "</div>")
            continue

        if re.match(r"^\d+\.\s+", stripped):
            flush_paragraph()
            items: list[str] = []
            while index < len(lines):
                item_match = re.match(r"^\d+\.\s+(.+)$", lines[index].strip())
                if not item_match:
                    break
                items.append(f"<li>{render_inline_markdown(item_match.group(1))}</li>")
                index += 1
            blocks.append("<ol>" + "".join(items) + "</ol>")
            continue

        if re.match(r"^[-*]\s+", stripped):
            flush_paragraph()
            items = []
            while index < len(lines):
                item_match = re.match(r"^[-*]\s+(.+)$", lines[index].strip())
                if not item_match:
                    break
                items.append(f"<li>{render_inline_markdown(item_match.group(1))}</li>")
                index += 1
            blocks.append("<ul>" + "".join(items) + "</ul>")
            continue

        paragraph.append(line)
        index += 1

    flush_paragraph()
    return "\n".join(blocks)


def split_markdown_guide(markdown_text: str) -> tuple[str, list[str], list[tuple[str, list[str]]]]:
    lines = markdown_text.splitlines()
    title = "Hướng dẫn upload"
    if lines and lines[0].startswith("# "):
        title = lines[0][2:].strip()
        lines = lines[1:]

    lead: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    section_title = ""
    section_lines: list[str] = []

    for line in lines:
        if line.startswith("## "):
            if section_title or section_lines:
                sections.append((section_title, section_lines))
            section_title = line[3:].strip()
            section_lines = []
            continue
        if section_title:
            section_lines.append(line)
        else:
            lead.append(line)

    if section_title or section_lines:
        sections.append((section_title, section_lines))
    return title, lead, sections


def render_social_upload_guide_html(platform: str) -> bytes:
    platform = platform.strip().lower()
    guide = UPLOAD_GUIDE_DOCS.get(platform)
    if not guide:
        return render_page_shell(
            title="Không tìm thấy hướng dẫn",
            body='<main class="social-guide-page"><a class="small-link back-link" href="/upload">← Quay lại Publish Studio</a><h1>Không tìm thấy hướng dẫn upload.</h1></main>',
        )

    markdown_path = guide["path"]
    markdown_text = markdown_path.read_text(encoding="utf-8")
    title, lead_lines, sections = split_markdown_guide(markdown_text)
    lead_html = render_markdown_blocks(lead_lines, markdown_path)
    actions_html = "".join(
        f'<a class="guide-action-link {"small-link" if index else ""}" href="{html.escape(url, quote=True)}" target="_blank" rel="noreferrer">{html.escape(label)}</a>'
        for index, (label, url) in enumerate(guide["actions"])
    )
    section_html = "\n".join(
        f"""
    <section class="md-guide-section">
      <div class="md-section-head">
        <p class="kicker">Guide</p>
        <h2>{render_inline_markdown(section_title)}</h2>
      </div>
      <div class="md-section-body">
        {render_markdown_blocks(section_lines, markdown_path)}
      </div>
    </section>
"""
        for section_title, section_lines in sections
    )

    body = f"""
  <main class="social-guide-page md-guide-page">
    <header class="guide-hero">
      <a class="small-link back-link" href="/upload">← Quay lại Publish Studio</a>
      <p class="kicker">{html.escape(str(guide["kicker"]))}</p>
      <h1>{html.escape(title)}</h1>
      <div class="guide-lead">{lead_html}</div>
      <div class="guide-links">{actions_html}</div>
    </header>
    <article class="md-guide">
      {section_html}
    </article>
  </main>
"""

    return render_page_shell(
        title=title,
        body=body,
        extra_style="""
    body { overflow: auto; }
    .social-guide-page {
      width: min(100%, 1280px);
      margin: 0 auto;
    }
    .guide-hero {
      max-width: none;
      margin-bottom: 24px;
      border: 1px solid var(--line);
      border-radius: 30px;
      background: var(--panel);
      box-shadow: var(--shadow);
      padding: clamp(22px, 3vw, 34px);
    }
    .guide-hero h1 {
      max-width: none;
      font-size: clamp(28px, 3vw, 46px);
      line-height: 1;
      white-space: nowrap;
      letter-spacing: 0;
    }
    .guide-lead {
      max-width: 980px;
      color: var(--text-soft);
      font-size: 16px;
      line-height: 1.7;
      font-weight: 720;
    }
    .guide-lead p { margin: 10px 0 0; }
    .back-link { margin-bottom: 18px; }
    .guide-links {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 20px;
    }
    .guide-action-link { min-height: 42px; }
    .md-guide {
      display: grid;
      gap: 18px;
    }
    .md-guide-section {
      border: 1px solid var(--line);
      border-radius: 28px;
      padding: clamp(18px, 2.4vw, 28px);
      background: var(--panel);
      box-shadow: var(--shadow);
    }
    .md-section-head {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 18px;
      margin-bottom: 16px;
      border-bottom: 1px solid var(--line);
      padding-bottom: 14px;
    }
    .md-section-head .kicker {
      flex: 0 0 auto;
      margin: 0;
    }
    .md-section-head h2 {
      flex: 1 1 auto;
      margin: 0;
      color: var(--text);
      font-size: clamp(24px, 2.35vw, 38px);
      line-height: 1.05;
      letter-spacing: 0;
      text-align: right;
    }
    .md-section-body {
      display: grid;
      gap: 14px;
      color: var(--text-soft);
      font-size: 16px;
      line-height: 1.7;
      font-weight: 720;
    }
    .md-section-body p,
    .md-section-body ol,
    .md-section-body ul { margin: 0; }
    .md-section-body ol,
    .md-section-body ul {
      padding-left: 24px;
    }
    .md-section-body li + li { margin-top: 8px; }
    .md-section-body h3,
    .md-section-body h4 {
      margin: 14px 0 0;
      color: var(--text);
      font-size: clamp(19px, 1.7vw, 26px);
      letter-spacing: 0;
    }
    .md-section-body strong { color: var(--text); font-weight: 950; }
    .md-guide code {
      border-radius: 8px;
      background: rgba(164, 98, 42, 0.12);
      color: #7a3f0c;
      padding: 0.05em 0.35em;
      font-weight: 850;
    }
    .md-guide pre {
      margin: 0;
      overflow-x: auto;
      border: 1px solid var(--control-line-soft);
      border-radius: 18px;
      padding: 16px;
      background: rgba(255, 251, 244, 0.92);
      color: var(--text);
      box-shadow: inset 0 1px 0 rgba(255,255,255,.55);
    }
    .md-guide pre code {
      display: block;
      padding: 0;
      background: transparent;
      color: inherit;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 13px;
      line-height: 1.55;
      white-space: pre;
    }
    .md-guide a.text-link {
      display: inline;
      padding: 0;
      border-radius: 0;
      color: #9b521b;
      background: transparent;
      text-decoration: underline;
      text-decoration-thickness: 2px;
      text-underline-offset: 3px;
      font-weight: 850;
    }
    .guide-image-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
      align-items: start;
    }
    .guide-image-grid.single { grid-template-columns: minmax(0, 1fr); }
    .guide-image-grid figure {
      margin: 0;
      border: 1px solid rgba(79, 57, 31, 0.15);
      border-radius: 18px;
      padding: 10px;
      background: #fff;
      box-shadow: 0 18px 42px rgba(78, 54, 28, 0.13);
    }
    .guide-image-grid img {
      display: block;
      width: 100%;
      max-height: 640px;
      object-fit: contain;
      border-radius: 12px;
      background: #fff;
    }
    body:not(.theme-light) .md-guide code { color: #ffd59a; background: rgba(0,0,0,.38); }
    body:not(.theme-light) .md-guide pre { background: rgba(0,0,0,.38); }
    body:not(.theme-light) .guide-image-grid figure {
      border-color: rgba(255,255,255,.14);
      box-shadow: 0 18px 42px rgba(0,0,0,.28);
    }
    @media (max-width: 1040px) {
      .guide-hero h1 { white-space: normal; }
      .md-section-head {
        align-items: flex-start;
        flex-direction: column;
      }
      .md-section-head h2 { text-align: left; }
      .guide-image-grid { grid-template-columns: 1fr; }
    }
""",
    )


APP_SHELL_STYLE = """
    body {
      padding: 0;
      min-height: 100vh;
      overflow-x: hidden;
      color: #07111f;
      background: #f8fbff;
    }
    body.theme-light {
      --bg: #f8fbff;
      --panel: #ffffff;
      --line: #dfe6ef;
      --text: #07111f;
      --muted: #5f6e85;
      --accent: #020617;
      --accent-contrast: #ffffff;
      --surface: #ffffff;
      --surface-strong: #f3f6fa;
      --surface-panel: #ffffff;
      --field-bg: #ffffff;
      --control-line: #dfe6ef;
      --control-line-soft: #e8edf4;
      --text-soft: #334155;
      --text-faint: #64748b;
      --text-button: #07111f;
      --status-text: #475569;
      --good-text: #047857;
      --warn-text: #925500;
      --danger-text: #b42318;
      --shadow: 0 22px 70px rgba(15, 23, 42, 0.08);
      --accent-glow: rgba(2, 6, 23, 0.16);
      --body-bg: #f8fbff;
    }
    .yv-app, .yv-app * { letter-spacing: 0; }
    .ui-icon {
      width: 17px;
      height: 17px;
      flex: 0 0 auto;
      display: inline-block;
      color: currentColor;
      stroke-width: 2.2;
    }
    .yv-app {
      min-height: 100vh;
      display: grid;
      grid-template-columns: 220px minmax(0, 1fr);
      background: #f8fbff;
    }
    .yv-app a {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      border-radius: 999px;
      padding: 0;
      color: inherit;
      background: transparent;
      text-decoration: none;
      font-weight: 700;
    }
    .yv-sidebar {
      position: sticky;
      top: 0;
      height: 100vh;
      display: flex;
      flex-direction: column;
      gap: 14px;
      padding: 16px;
      overflow-y: auto;
      background: #f4f3ef;
      border-right: 1px solid #dedbd4;
    }
    .workspace-button, .user-chip, .upgrade-card {
      border: 1px solid #d7d4cc;
      background: #ffffff;
      box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
    }
    .workspace-button {
      display: flex;
      align-items: center;
      gap: 8px;
      width: 100%;
      min-height: 38px;
      border-radius: 8px;
      padding: 7px 10px;
      color: #020617;
      font: inherit;
      font-size: 13px;
      font-weight: 800;
      text-align: left;
      cursor: default;
    }
    .workspace-button > span:nth-child(2) {
      min-width: 0;
      flex: 1 1 auto;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .language-toggle {
      display: inline-grid;
      place-items: center;
      width: 30px;
      height: 30px;
      flex: 0 0 auto;
      border: 1px solid #d7d4cc;
      border-radius: 999px;
      color: #020617;
      background: #ffffff;
      font: inherit;
      font-size: 17px;
      line-height: 1;
      cursor: pointer;
    }
    .language-toggle:hover,
    .language-toggle:focus-visible {
      border-color: #020617;
      box-shadow: 0 0 0 2px rgba(2, 6, 23, 0.10);
      outline: none;
    }
    .flag-vn {
      position: relative;
      display: grid;
      place-items: center;
      width: 22px;
      height: 15px;
      border-radius: 3px;
      color: #ffde00;
      background: #da251d;
      box-shadow: inset 0 0 0 1px rgba(0, 0, 0, 0.08);
      overflow: hidden;
    }
    .flag-vn span {
      font-size: 9px;
      line-height: 1;
      transform: translateY(-0.3px);
    }
    .flag-en {
      display: grid;
      place-items: center;
      width: 22px;
      height: 15px;
      border-radius: 3px;
      color: #ffffff;
      background: #020617;
      font-size: 8px;
      font-weight: 950;
      line-height: 1;
    }
    .workspace-logo {
      display: block;
      width: 24px;
      height: 24px;
      flex: 0 0 auto;
      border-radius: 7px;
      object-fit: cover;
    }
    .workspace-mark, .avatar-mark {
      display: grid;
      place-items: center;
      width: 24px;
      height: 24px;
      flex: 0 0 auto;
      border-radius: 7px;
      color: #ffffff;
      background: #020617;
      font-size: 11px;
      font-weight: 900;
    }
    .sidebar-nav { display: grid; gap: 4px; }
    .nav-section-label {
      margin: 12px 0 3px;
      padding: 0 12px;
      color: #6b7890;
      font-size: 11px;
      font-weight: 800;
      text-transform: uppercase;
    }
    .nav-item {
      width: 100%;
      min-height: 34px;
      justify-content: flex-start !important;
      border-radius: 10px !important;
      padding: 8px 12px !important;
      color: #334155 !important;
      background: transparent !important;
      font-size: 13px;
      font-weight: 650;
    }
    .nav-item:hover, .nav-item.active {
      color: #020617 !important;
      background: #ffffff !important;
      box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
    }
    .nav-icon {
      display: inline-grid;
      place-items: center;
      width: 18px;
      height: 18px;
      flex: 0 0 auto;
      border-radius: 999px;
      color: #0f172a;
    }
    .nav-icon .ui-icon {
      width: 16px;
      height: 16px;
    }
    .sidebar-spacer { flex: 1 1 auto; min-height: 24px; }
    .upgrade-card {
      border-color: #facc15;
      border-radius: 12px;
      padding: 12px;
      background: #f7fbff;
    }
    .upgrade-card strong {
      display: block;
      color: #d97706;
      font-size: 11px;
      text-transform: uppercase;
    }
    .upgrade-card span {
      display: block;
      margin-top: 5px;
      color: #102033;
      font-size: 12px;
      font-weight: 750;
      line-height: 1.35;
    }
    .user-chip {
      display: flex;
      align-items: center;
      gap: 8px;
      border-radius: 9px;
      padding: 8px;
    }
    .user-chip strong {
      display: block;
      max-width: 126px;
      overflow: hidden;
      color: #020617;
      font-size: 12px;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .user-chip span { color: #64748b; font-size: 11px; }
    .yv-main {
      min-width: 0;
      padding: 38px 34px;
      background: #f8fbff;
    }
    .page-frame { width: min(100%, 1180px); margin: 0 auto; }
    .page-head { margin-bottom: 22px; }
    .page-head-with-actions {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 16px;
    }
    .page-head-with-actions > div:first-child { min-width: 0; }
    .page-actions {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 10px;
      flex: 0 0 auto;
      flex-wrap: wrap;
    }
    .eyebrow {
      margin: 0 0 10px;
      color: #8392ad;
      font-size: 12px;
      font-weight: 850;
      text-transform: uppercase;
    }
    .page-head h1 {
      margin: 0;
      color: #020617;
      font-size: clamp(28px, 3vw, 42px);
      line-height: 1.05;
    }
    .page-head p {
      max-width: 760px;
      margin: 12px 0 0;
      color: #475569;
      font-size: 15px;
      line-height: 1.65;
    }
    .primary-btn, .secondary-btn, .ghost-btn, .small-link, .icon-btn, .start {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      min-height: 38px;
      border-radius: 999px;
      border: 1px solid #dbe3ed;
      padding: 9px 15px;
      background: #ffffff;
      color: #07111f;
      font: inherit;
      font-size: 13px;
      font-weight: 800;
      text-decoration: none;
      cursor: pointer;
    }
    .primary-btn, .start {
      border-color: #020617;
      background: #020617;
      color: #ffffff;
    }
    .ghost-btn { background: #f8fbff; }
    .feature-list { display: grid; gap: 16px; }
    .feature-card {
      display: grid;
      grid-template-columns: 140px minmax(0, 1fr) auto;
      align-items: center;
      gap: 18px;
      min-height: 176px;
      border: 1px solid #dfe6ef;
      border-radius: 12px;
      padding: 0 16px 0 0;
      background: #ffffff;
      box-shadow: 0 18px 50px rgba(15, 23, 42, 0.05);
      overflow: hidden;
    }
    .feature-media { width: 140px; height: 176px; overflow: hidden; background: #020617; }
    .feature-media video, .feature-media img {
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }
    .feature-fallback {
      height: 100%;
      display: grid;
      padding: 16px;
      color: #ffffff;
      background: #020617;
      text-align: center;
      font-size: 15px;
      font-weight: 900;
      line-height: 1.25;
    }
    .feature-body h2 {
      margin: 0 0 8px;
      color: #020617;
      font-size: 22px;
      line-height: 1.15;
    }
    .feature-body p {
      max-width: 640px;
      margin: 0 0 14px;
      color: #42526a;
      font-size: 14px;
      line-height: 1.55;
    }
    .feature-actions {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }
    .circle-arrow {
      width: 38px;
      height: 38px;
      border: 0;
      border-radius: 999px !important;
      color: #ffffff !important;
      background: #020617 !important;
      font-size: 18px;
      font-weight: 900;
      cursor: pointer;
    }
    .quick-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 14px;
      margin-top: 28px;
    }
    .quick-card {
      min-height: 138px;
      display: flex !important;
      flex-direction: column;
      justify-content: space-between !important;
      align-items: flex-start !important;
      border: 1px solid #dfe6ef;
      border-radius: 12px !important;
      padding: 18px !important;
      background: #ffffff !important;
      box-shadow: 0 14px 38px rgba(15, 23, 42, 0.04);
    }
    .quick-card h3 {
      display: flex;
      align-items: center;
      gap: 8px;
      margin: 0 0 7px;
      color: #020617;
      font-size: 17px;
    }
    .quick-card p { margin: 0; color: #5f6e85; font-size: 13px; line-height: 1.5; }
    .studio-layout {
      min-width: 0;
      display: grid;
      grid-template-columns: minmax(0, 1fr) 406px;
      min-height: calc(100vh - 76px);
      border-left: 1px solid #e3e9f2;
    }
    .studio-main {
      min-width: 0;
      display: flex;
      flex-direction: column;
      padding-right: 18px;
    }
    .studio-top {
      min-width: 0;
      padding: 0 0 16px;
      border-bottom: 1px solid #e3e9f2;
    }
    .studio-steps-line {
      display: flex;
      gap: 12px;
      margin-top: 8px;
      color: #64748b;
      font-size: 13px;
    }
    .mode-tabs {
      display: inline-flex;
      gap: 3px;
      margin-top: 14px;
      border: 1px solid #dfe6ef;
      border-radius: 999px;
      padding: 4px;
      background: #ffffff;
    }
    .mode-tabs a {
      min-height: 30px;
      border-radius: 999px !important;
      padding: 7px 13px !important;
      color: #334155 !important;
      font-size: 13px;
      font-weight: 800;
    }
    .mode-tabs a.active { color: #ffffff !important; background: #020617 !important; }
    .editor-panel {
      flex: 1 1 auto;
      min-height: 500px;
      display: flex;
      flex-direction: column;
      background: #ffffff;
    }
    .script-editor {
      flex: 1 1 auto;
      width: 100%;
      min-height: 440px;
      border: 0;
      resize: vertical;
      padding: 24px 16px;
      color: #0f172a;
      background: transparent;
      font: inherit;
      font-size: 15px;
      line-height: 1.65;
      outline: none;
    }
    .editor-bottom {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      border-top: 1px solid #e3e9f2;
      padding: 14px 16px;
      background: #ffffff;
    }
    .setup-panel {
      position: sticky;
      top: 0;
      height: 100vh;
      overflow-y: auto;
      padding-left: 18px;
      background: #f8fbff;
    }
    .setup-card {
      border: 1px solid #dfe6ef;
      border-radius: 8px;
      padding: 16px;
      margin-bottom: 16px;
      background: #ffffff;
    }
    .setup-card h2, .setup-card h3 {
      display: flex;
      align-items: center;
      gap: 8px;
      margin: 0 0 14px;
      color: #020617;
      font-size: 15px;
      line-height: 1.25;
    }
    .voice-preview-panel {
      display: grid;
      gap: 8px;
      margin: 10px 0;
    }
    .voice-preview-panel audio {
      width: 100%;
      min-height: 38px;
    }
    .field, .check {
      display: grid;
      gap: 7px;
      margin-bottom: 12px;
      color: #07111f;
      font-size: 13px;
      font-weight: 750;
    }
    .field > span:not(.source-note), .field-label {
      color: #8090ad;
      font-size: 11px;
      font-weight: 850;
      text-transform: uppercase;
    }
    input, select, textarea {
      width: 100%;
      border: 1px solid #dbe3ed;
      border-radius: 10px;
      padding: 10px 12px;
      color: #0f172a;
      background: #ffffff;
      font: inherit;
      font-size: 13px;
    }
    .hidden-file-input {
      position: absolute;
      width: 1px;
      height: 1px;
      overflow: hidden;
      clip: rect(0 0 0 0);
      clip-path: inset(50%);
      white-space: nowrap;
    }
    .segmented {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px;
    }
    .segmented button, .tabs button {
      border: 1px solid #dbe3ed;
      border-radius: 10px;
      padding: 9px 8px;
      background: #ffffff;
      color: #334155;
      font: inherit;
      font-size: 12px;
      font-weight: 850;
      cursor: pointer;
    }
    .segmented button.active, .tabs button.active {
      border-color: #020617;
      background: #020617;
      color: #ffffff;
    }
    .tabs { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .config-row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .setting-summary {
      margin-top: 8px;
      color: #64748b;
      font-size: 12px;
      font-weight: 750;
    }
    .project-list-compact {
      display: grid;
      gap: 8px;
      max-height: 260px;
      overflow: auto;
      padding: 0;
      margin: 0;
      list-style: none;
    }
    .project-row {
      display: grid;
      gap: 8px;
      border: 1px solid #dfe6ef;
      border-radius: 10px;
      padding: 10px;
      background: #ffffff;
      cursor: pointer;
    }
    .project-row.selected {
      border-color: #020617;
      box-shadow: inset 0 0 0 1px #020617;
    }
    .project-name {
      color: #020617;
      font-size: 13px;
      font-weight: 850;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .project-slide-count, .status-pill { color: #64748b; font-size: 12px; font-weight: 750; }
    .status-pill.ok { color: #047857; }
    .status-pill.bad { color: #b42318; }
    .project-row .actions { display: flex; gap: 6px; flex-wrap: wrap; }
    .status {
      border: 1px solid #dfe6ef;
      border-radius: 10px;
      padding: 10px 12px;
      margin: 12px 0;
      background: #ffffff;
      color: #475569;
      font-size: 13px;
      line-height: 1.45;
    }
    .status.good { color: #047857; border-color: #a7f3d0; background: #f0fdf4; }
    .status.bad, .render-state.failed { color: #b42318; border-color: #fecaca; background: #fff1f2; }
    .render-state {
      border: 1px solid #dfe6ef;
      border-radius: 10px;
      padding: 12px;
      margin-top: 12px;
      background: #ffffff;
    }
    .state-head { display: flex; align-items: center; gap: 8px; color: #020617; font-size: 13px; }
    .state-list { margin: 10px 0 0; padding-left: 18px; color: #475569; font-size: 12px; line-height: 1.5; }
    .mode-toggle { display: flex; gap: 10px; margin-bottom: 12px; color: #334155; font-size: 13px; font-weight: 750; }
    .advanced-settings summary {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      cursor: pointer;
      color: #020617;
      font-weight: 850;
      list-style: none;
    }
    .advanced-settings summary::-webkit-details-marker { display: none; }
    #advancedStateLabel {
      color: #64748b;
      font-size: 12px;
      font-weight: 750;
      white-space: nowrap;
    }
    .form-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-top: 12px; }
    .empty {
      border: 1px dashed #dbe3ed;
      border-radius: 10px;
      padding: 16px;
      color: #64748b;
      background: #ffffff;
      list-style: none;
    }
    .library-toolbar {
      display: grid;
      grid-template-columns: minmax(240px, 1fr) auto;
      align-items: end;
      gap: 12px;
      margin: 18px 0 20px;
    }
    .library-actions {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 10px;
      flex-wrap: wrap;
    }
    .source-note {
      margin-top: 7px;
      color: #64748b;
      font-size: 12px;
      line-height: 1.45;
      word-break: break-word;
    }
    .source-path {
      display: block;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      text-transform: none;
      overflow-wrap: anywhere;
    }
    .mini-flow {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 9px;
    }
    .mini-flow span {
      border: 1px solid #dbe3ed;
      border-radius: 999px;
      padding: 5px 8px;
      color: #475569;
      background: #f8fbff;
      font-size: 11px;
      font-weight: 850;
      line-height: 1;
    }
    .stat-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }
    .stat-card {
      border: 1px solid #dfe6ef;
      border-radius: 8px;
      padding: 14px;
      background: #ffffff;
    }
    .stat-card strong {
      display: block;
      color: #020617;
      font-size: 24px;
      line-height: 1;
    }
    .stat-card span {
      display: block;
      margin-top: 7px;
      color: #64748b;
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
    }
    .template-gallery {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 14px;
      padding: 0;
      margin: 0;
      list-style: none;
    }
    .template-pack-list {
      display: grid;
      gap: 18px;
    }
    .template-pack-section {
      display: grid;
      gap: 12px;
    }
    .template-pack-head {
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 12px;
      border-bottom: 1px solid #dfe6ef;
      padding-bottom: 10px;
    }
    .template-pack-head h2 {
      margin: 0;
      color: #020617;
      font-size: 22px;
      line-height: 1.15;
    }
    .template-card {
      min-width: 0;
      display: grid;
      border: 1px solid #dfe6ef;
      border-radius: 8px;
      overflow: hidden;
      background: #ffffff;
      box-shadow: 0 14px 38px rgba(15, 23, 42, 0.04);
    }
    .template-card.selected {
      border-color: #020617;
      box-shadow: inset 0 0 0 1px #020617, 0 18px 46px rgba(15, 23, 42, 0.08);
    }
    .template-select-btn.active {
      border-color: #020617;
      color: #ffffff;
      background: #020617;
    }
    .project-library-card .select-btn.active {
      border-color: #020617;
      color: #ffffff;
      background: #020617;
      box-shadow: 0 10px 26px rgba(15, 23, 42, 0.16);
    }
    .project-library-card .select-btn.active .ui-icon {
      color: #ffffff;
    }
    .template-preview {
      aspect-ratio: 9 / 13;
      background: #020617;
      overflow: hidden;
    }
    .template-preview video,
    .template-preview img {
      width: 100%;
      height: 100%;
      display: block;
      object-fit: cover;
    }
    .template-preview-fallback {
      height: 100%;
      display: grid;
      place-items: center;
      padding: 18px;
      color: #ffffff;
      background: linear-gradient(135deg, #020617, #253247);
      text-align: center;
      font-size: 18px;
      font-weight: 900;
      line-height: 1.2;
    }
    .template-card-body {
      min-width: 0;
      display: grid;
      gap: 12px;
      padding: 14px;
    }
    .template-card-body h2 {
      margin: 0;
      color: #020617;
      font-size: 16px;
      line-height: 1.25;
      word-break: break-word;
    }
    .template-meta {
      display: flex;
      gap: 7px;
      flex-wrap: wrap;
    }
    .template-badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border: 1px solid #dbe3ed;
      border-radius: 999px;
      padding: 6px 9px;
      color: #334155;
      background: #f8fbff;
      font-size: 12px;
      font-weight: 800;
    }
    .template-badge.ok {
      color: #047857;
      border-color: #a7f3d0;
      background: #f0fdf4;
    }
    .template-badge.warn {
      color: #925500;
      border-color: #fed7aa;
      background: #fffbeb;
    }
    .template-actions {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .template-flow {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin: 18px 0;
    }
    .template-flow-step {
      border: 1px solid #dfe6ef;
      border-radius: 8px;
      padding: 14px;
      background: #ffffff;
    }
    .template-flow-step strong {
      display: flex;
      align-items: center;
      gap: 8px;
      color: #020617;
      font-size: 14px;
    }
    .template-flow-step span {
      color: #64748b;
      font-size: 11px;
      font-weight: 850;
      text-transform: uppercase;
    }
    .template-flow-step p {
      margin: 8px 0 0;
      color: #64748b;
      font-size: 12px;
      line-height: 1.5;
    }
    .template-live-layout {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 320px;
      gap: 14px;
      align-items: start;
    }
    .template-live-panel {
      min-width: 0;
      display: grid;
      gap: 10px;
    }
    .template-live-frame-wrap {
      height: min(74vh, 860px);
      min-height: 560px;
      border: 1px solid #dfe6ef;
      border-radius: 8px;
      overflow: hidden;
      background: #020617;
      box-shadow: 0 18px 46px rgba(15, 23, 42, 0.08);
    }
    .template-live-frame {
      display: block;
      width: 100%;
      height: 100%;
      border: 0;
      background: #020617;
    }
    .template-live-side {
      position: sticky;
      top: 20px;
      display: grid;
      gap: 12px;
    }
    .template-editor-grid {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(360px, 430px);
      gap: 16px;
      align-items: start;
    }
    .template-editor-preview {
      min-width: 0;
      position: sticky;
      top: 20px;
    }
    .template-editor-panel {
      min-width: 0;
      display: grid;
      gap: 12px;
    }
    .template-editor-form {
      display: grid;
      gap: 12px;
    }
    .template-editor-two-cols {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }
    .project-library-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
      padding: 0;
      margin: 0;
      list-style: none;
    }
    .project-library-card {
      grid-template-columns: 150px minmax(0, 1fr);
      align-items: stretch;
      gap: 0;
      padding: 0;
      overflow: hidden;
      cursor: default;
    }
    .project-card-preview {
      min-height: 232px;
      background: #020617;
    }
    .project-card-preview video {
      width: 100%;
      height: 100%;
      min-height: 232px;
      display: block;
      object-fit: cover;
    }
    .project-card-fallback {
      height: 100%;
      min-height: 232px;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 6px;
      place-items: center;
      padding: 16px;
      color: #ffffff;
      background: linear-gradient(135deg, #020617, #334155);
      text-align: center;
      font-size: 17px;
      font-weight: 900;
      line-height: 1.25;
    }
    .project-card-fallback small {
      color: #cbd5e1;
      font-size: 12px;
      font-weight: 800;
    }
    .project-card-body {
      min-width: 0;
      display: grid;
      gap: 12px;
      padding: 14px;
    }
    .project-card-title {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 10px;
    }
    .project-card-title h2 {
      margin: 0;
      color: #020617;
      font-size: 16px;
      line-height: 1.25;
      word-break: break-word;
    }
    .project-card-actions,
    .asset-row,
    .final-upload-actions,
    .platform-actions {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .asset-row {
      padding-top: 2px;
    }
    .asset-chip {
      min-height: 30px;
      border: 1px solid #dbe3ed;
      border-radius: 999px;
      padding: 6px 10px;
      color: #334155;
      background: #ffffff;
      font-size: 12px;
      font-weight: 800;
      text-decoration: none;
    }
    .asset-chip.disabled {
      color: #94a3b8;
      background: #f8fafc;
      cursor: default;
    }
    .publish-workspace {
      display: grid;
      grid-template-columns: minmax(0, 340px) minmax(0, 1fr);
      gap: 18px;
      align-items: start;
    }
    .publish-side {
      position: sticky;
      top: 20px;
      display: grid;
      gap: 14px;
    }
    .publish-main {
      min-width: 0;
      display: grid;
      gap: 14px;
    }
    .platform-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
    }
    .connections-layout {
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 14px;
      align-items: start;
    }
    .connection-form-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }
    .connection-create-form {
      display: none;
    }
    .connection-create-form.open {
      position: fixed;
      z-index: 210;
      left: 50%;
      top: 50%;
      display: grid;
      width: min(760px, calc(100vw - 40px));
      max-height: calc(100vh - 40px);
      overflow: auto;
      transform: translate(-50%, -50%);
      box-shadow: 0 24px 80px rgba(15, 23, 42, 0.28);
    }
    .connection-form-grid .field.full,
    .connection-projects,
    .connection-notes {
      grid-column: 1 / -1;
    }
    .connection-project-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 7px;
      margin-top: 8px;
    }
    .project-check {
      display: flex;
      align-items: center;
      gap: 7px;
      min-width: 0;
      border: 1px solid #dbe3ed;
      border-radius: 999px;
      padding: 7px 9px;
      color: #334155;
      background: #ffffff;
      font-size: 12px;
      font-weight: 800;
    }
    .project-check input {
      width: auto;
      flex: 0 0 auto;
      margin: 0;
      padding: 0;
    }
    .project-check span {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .connection-project-picker {
      display: grid;
      gap: 8px;
      margin-top: 8px;
    }
    .connection-project-picker-trigger {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      width: 100%;
      min-height: 42px;
      border: 1px solid #dbe3ed;
      border-radius: 8px;
      padding: 8px 10px;
      color: #0f172a;
      background: #ffffff;
      font: inherit;
      font-size: 13px;
      font-weight: 850;
      cursor: pointer;
    }
    .connection-project-picker-trigger[aria-expanded="true"] {
      border-color: #0f172a;
      box-shadow: 0 0 0 3px rgba(15, 23, 42, 0.08);
    }
    .connection-project-picker-label {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .connection-project-picker-meta {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      flex: 0 0 auto;
      color: #64748b;
      font-size: 11px;
      font-weight: 850;
    }
    .connection-project-picker-chevron {
      width: 14px;
      height: 14px;
    }
    .connection-project-menu {
      border: 1px solid #dbe3ed;
      border-radius: 8px;
      padding: 10px;
      background: #ffffff;
      box-shadow: 0 18px 45px rgba(15, 23, 42, 0.12);
    }
    .connection-project-menu-head {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto auto;
      gap: 8px;
      align-items: center;
    }
    .connection-project-menu input[type="search"] {
      min-height: 36px;
      border: 1px solid #dbe3ed;
      border-radius: 8px;
      padding: 8px 10px;
      font: inherit;
      font-size: 12px;
      font-weight: 750;
    }
    .connection-project-picker-action {
      min-height: 34px;
      border: 1px solid #dbe3ed;
      border-radius: 8px;
      padding: 7px 9px;
      color: #334155;
      background: #f8fbff;
      font: inherit;
      font-size: 11px;
      font-weight: 900;
      cursor: pointer;
      white-space: nowrap;
    }
    .connection-project-picker .connection-project-grid {
      max-height: 248px;
      overflow: auto;
      padding-right: 2px;
    }
    .connection-project-picker-empty {
      margin-top: 8px;
      color: #64748b;
      font-size: 12px;
      font-weight: 800;
    }
    .connections-list {
      display: grid;
      gap: 12px;
      padding: 0;
      margin: 0;
      list-style: none;
    }
    .connections-browser {
      min-width: 0;
      display: grid;
      gap: 12px;
    }
    .connections-toolbar {
      display: grid;
      grid-template-columns: minmax(220px, 1.35fr) repeat(3, minmax(140px, 0.8fr));
      gap: 10px;
      align-items: end;
      border: 1px solid #dfe6ef;
      border-radius: 8px;
      padding: 12px;
      background: #ffffff;
    }
    .connection-search-field {
      min-width: 0;
    }
    .connection-filter-summary {
      color: #64748b;
      font-size: 12px;
      font-weight: 850;
    }
    .connections-groups {
      display: grid;
      gap: 14px;
    }
    .connections-flat {
      display: grid;
      gap: 12px;
    }
    .connection-group {
      display: grid;
      gap: 10px;
    }
    .connection-group-head,
    .connection-studio-strip {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      border: 1px solid #dfe6ef;
      border-radius: 8px;
      padding: 12px 14px;
      background: #ffffff;
    }
    .connection-group-head h2 {
      margin: 2px 0 0;
      color: #020617;
      font-size: 16px;
      line-height: 1.2;
    }
    .connection-group-count {
      flex: 0 0 auto;
      border: 1px solid #dbe3ed;
      border-radius: 999px;
      padding: 7px 10px;
      color: #475569;
      background: #f8fbff;
      font-size: 12px;
      font-weight: 850;
    }
    .connection-studio-strip {
      margin: 12px 0 14px;
    }
    .connection-studio-strip strong {
      display: block;
      margin-top: 2px;
      color: #020617;
      font-size: 17px;
    }
    .connection-studio-strip p {
      margin: 4px 0 0;
      color: #64748b;
      font-size: 12px;
      font-weight: 750;
      line-height: 1.45;
    }
    .connection-studio-strip code {
      color: #0f172a;
      font-size: 11px;
      overflow-wrap: anywhere;
    }
    .connection-empty-state {
      border: 1px dashed #cbd5e1;
      border-radius: 8px;
      padding: 18px;
      color: #64748b;
      background: #f8fbff;
      text-align: center;
      font-size: 13px;
      font-weight: 800;
    }
    .connection-card {
      display: grid;
      gap: 10px;
      border: 1px solid #dfe6ef;
      border-radius: 8px;
      padding: 15px;
      background: #ffffff;
    }
    .connection-card-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
    }
    .connection-title {
      min-width: 0;
      display: flex;
      gap: 10px;
      align-items: flex-start;
    }
    .connection-title h2 {
      margin: 0;
      color: #020617;
      font-size: 17px;
      line-height: 1.2;
      overflow-wrap: anywhere;
    }
    .connection-title p {
      margin: 4px 0 0;
      color: #64748b;
      font-size: 12px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }
    .connection-mark {
      display: grid;
      place-items: center;
      width: 34px;
      height: 34px;
      flex: 0 0 auto;
      border-radius: 10px;
      color: #ffffff;
      background: #020617;
      font-size: 11px;
      font-weight: 900;
    }
    .connection-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 7px;
    }
    .connection-chip {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      border: 1px solid #dbe3ed;
      border-radius: 999px;
      padding: 6px 8px;
      color: #475569;
      background: #f8fbff;
      font-size: 12px;
      font-weight: 800;
      line-height: 1;
    }
    .connection-chip-url {
      max-width: min(100%, 260px);
    }
    .connection-chip-url span {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .connection-chip.ok {
      border-color: #bbf7d0;
      color: #047857;
      background: #ecfdf5;
    }
    .connection-chip.warn {
      border-color: #fde68a;
      color: #92400e;
      background: #fffbeb;
    }
    .connection-chip.bad {
      border-color: #fecaca;
      color: #b91c1c;
      background: #fef2f2;
    }
    .connection-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      padding-top: 2px;
    }
    .connection-card[data-connection-id="system:elevenlabs:proxy"] .connection-delete-btn {
      display: none;
    }
    .connection-status {
      margin: 10px 0 0;
    }
    .connection-modal {
      width: min(100%, 760px);
      max-height: calc(100vh - 40px);
      overflow: auto;
    }
    .platform-card {
      border: 1px solid #dfe6ef;
      border-radius: 8px;
      padding: 16px;
      background: #ffffff;
    }
    .platform-card-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 14px;
    }
    .platform-title {
      display: flex;
      align-items: center;
      gap: 10px;
      color: #020617;
      font-size: 16px;
      font-weight: 900;
    }
    .field-icon {
      display: inline-grid;
      place-items: center;
      width: 28px;
      height: 28px;
      flex: 0 0 auto;
      border-radius: 8px;
      color: #ffffff;
      background: #020617;
      font-size: 11px;
      font-weight: 900;
    }
    .platform-youtube .field-icon { background: #ef4444; }
    .platform-facebook .field-icon { background: #2563eb; }
    .upload-panel {
      display: grid;
      gap: 14px;
    }
    .upload-field textarea {
      min-height: 148px;
      resize: vertical;
    }
    .field-label-between {
      display: flex !important;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }
    .field-label-main {
      display: inline-flex;
      align-items: center;
      gap: 8px;
    }
    .copy-field-btn {
      width: 30px;
      height: 30px;
      border: 1px solid #dbe3ed;
      border-radius: 9px;
      color: #334155;
      background: #ffffff;
      font: inherit;
      font-size: 12px;
      font-weight: 900;
      cursor: pointer;
    }
    .upload-btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      min-height: 38px;
      border-radius: 999px;
      border: 1px solid #020617;
      padding: 9px 14px;
      color: #ffffff;
      background: #020617;
      font: inherit;
      font-size: 13px;
      font-weight: 850;
      cursor: pointer;
      white-space: nowrap;
    }
    .upload-btn.secondary {
      border-color: #dbe3ed;
      color: #07111f;
      background: #ffffff;
    }
    .upload-btn:disabled,
    .primary-btn:disabled,
    .secondary-btn:disabled,
    .start:disabled {
      opacity: 0.52;
      cursor: not-allowed;
    }
    .platform-account-list {
      display: grid;
      gap: 8px;
      margin-bottom: 12px;
    }
    .platform-account {
      width: 100%;
      display: grid;
      grid-template-columns: 28px minmax(0, 1fr) auto;
      gap: 8px;
      align-items: center;
      border: 1px solid #dbe3ed;
      border-radius: 10px;
      padding: 8px;
      background: #f8fbff;
      text-align: left;
      font: inherit;
      cursor: pointer;
    }
    .platform-account-avatar {
      display: grid;
      place-items: center;
      width: 28px;
      height: 28px;
      border-radius: 999px;
      color: #ffffff;
      background: #020617;
      font-size: 11px;
      font-weight: 900;
    }
    .platform-account-body {
      min-width: 0;
      display: grid;
      gap: 2px;
    }
    .platform-account-name {
      color: #020617;
      font-size: 13px;
      font-weight: 850;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .platform-account-id {
      color: #64748b;
      font-size: 11px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .platform-account-options {
      display: grid;
      gap: 6px;
      padding-left: 12px;
    }
    .upload-status,
    .upload-result {
      border: 1px solid #dfe6ef;
      border-radius: 10px;
      padding: 11px 12px;
      color: #475569;
      background: #ffffff;
      font-size: 13px;
      line-height: 1.45;
    }
    .upload-status.good,
    .upload-result.good { color: #047857; border-color: #a7f3d0; background: #f0fdf4; }
    .upload-status.bad,
    .upload-result.bad { color: #b42318; border-color: #fecaca; background: #fff1f2; }
    .upload-status.warn,
    .upload-result.warn { color: #925500; border-color: #fed7aa; background: #fffbeb; }
    .upload-result a { color: inherit; text-decoration: underline; }
    .modal-backdrop {
      position: fixed;
      inset: 0;
      z-index: 20;
      display: grid;
      place-items: center;
      padding: 20px;
      background: rgba(15, 23, 42, 0.42);
    }
    .modal-card {
      width: min(100%, 520px);
      border: 1px solid #dfe6ef;
      border-radius: 10px;
      padding: 20px;
      background: #ffffff;
      box-shadow: 0 24px 80px rgba(15, 23, 42, 0.24);
    }
    .modal-close {
      float: right;
      width: 32px;
      height: 32px;
      border: 1px solid #dbe3ed;
      border-radius: 999px;
      background: #ffffff;
      color: #020617;
      font: inherit;
      font-weight: 900;
      cursor: pointer;
    }
    .create-project-modal h3,
    .template-modal h3 {
      margin: 0;
      color: #020617;
      font-size: 26px;
      line-height: 1.15;
    }
    .create-project-form,
    .template-form {
      display: grid;
      gap: 2px;
      margin-top: 16px;
    }
    .template-modal {
      width: min(100%, 760px);
      max-height: calc(100vh - 40px);
      overflow: auto;
    }
    .template-form textarea[name="script"],
    .template-form textarea[name="rules"] {
      min-height: 150px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      line-height: 1.55;
    }
    .template-form textarea[name="preview_settings"] {
      min-height: 120px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      line-height: 1.55;
    }
    .create-project-status,
    .template-status {
      margin-bottom: 0;
    }
    .modal-copy {
      color: #64748b;
      font-size: 13px;
      line-height: 1.55;
    }
    .modal-actions {
      display: flex;
      justify-content: flex-end;
      gap: 8px;
      margin-top: 14px;
      flex-wrap: wrap;
    }
    .storyboard-modal-card {
      width: min(100%, 920px);
      max-height: calc(100vh - 40px);
      overflow: auto;
    }
    .storyboard-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 14px;
    }
    .storyboard-head h3 {
      margin: 0;
      color: #020617;
      font-size: 24px;
      line-height: 1.15;
    }
    .storyboard-head p {
      margin: 6px 0 0;
      color: #64748b;
      font-size: 13px;
      line-height: 1.5;
    }
    .storyboard-tabs {
      display: inline-flex;
      gap: 4px;
      border: 1px solid #dfe6ef;
      border-radius: 999px;
      padding: 4px;
      background: #f8fbff;
      margin-bottom: 14px;
    }
    .storyboard-tabs button {
      min-height: 32px;
      border: 0;
      border-radius: 999px;
      padding: 7px 13px;
      color: #334155;
      background: transparent;
      font: inherit;
      font-size: 13px;
      font-weight: 850;
      cursor: pointer;
    }
    .storyboard-tabs button.active {
      color: #ffffff;
      background: #020617;
    }
    .storyboard-status {
      margin-bottom: 12px;
      color: #64748b;
      font-size: 13px;
      line-height: 1.5;
    }
    .storyboard-scene-list {
      display: grid;
      gap: 10px;
      max-height: 430px;
      overflow: auto;
      padding: 0;
      margin: 0;
      list-style: none;
    }
    .storyboard-scene {
      display: grid;
      grid-template-columns: 44px minmax(0, 1fr);
      gap: 12px;
      border: 1px solid #dfe6ef;
      border-radius: 8px;
      padding: 12px;
      background: #ffffff;
    }
    .storyboard-scene-index {
      display: grid;
      place-items: center;
      width: 34px;
      height: 34px;
      border-radius: 999px;
      color: #ffffff;
      background: #020617;
      font-size: 12px;
      font-weight: 900;
    }
    .storyboard-scene strong {
      display: block;
      margin-bottom: 5px;
      color: #020617;
      font-size: 13px;
    }
    .storyboard-scene p {
      margin: 0;
      color: #334155;
      font-size: 13px;
      line-height: 1.55;
      overflow-wrap: anywhere;
    }
    .storyboard-media-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
    }
    .storyboard-media-card {
      min-height: 116px;
      display: grid;
      align-content: space-between;
      gap: 12px;
      border: 1px solid #dfe6ef;
      border-radius: 8px;
      padding: 13px;
      background: #ffffff;
    }
    .storyboard-media-card strong {
      color: #020617;
      font-size: 14px;
    }
    .storyboard-media-card span {
      color: #64748b;
      font-size: 12px;
      line-height: 1.4;
    }
    .slide-composer {
      display: grid;
      gap: 12px;
      margin-top: 14px;
      border: 1px solid #dfe6ef;
      border-radius: 8px;
      padding: 14px;
      background: #ffffff;
    }
    .slide-composer-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
    }
    .slide-composer-head h2 {
      margin: 0;
      color: #020617;
      font-size: 20px;
      line-height: 1.2;
    }
    .slide-composer-grid {
      display: grid;
      grid-template-columns: minmax(150px, 0.8fr) minmax(260px, 1.2fr);
      gap: 12px;
      align-items: start;
    }
    .studio-slide-list {
      order: 1;
      display: grid;
      gap: 8px;
      margin: 0;
      padding: 0;
      list-style: none;
      max-height: 620px;
      overflow: auto;
    }
    .studio-slide-list button {
      width: 100%;
      min-height: 44px;
      justify-content: flex-start;
      align-items: flex-start;
      border-radius: 8px;
      font-size: 12px;
      line-height: 1.35;
      text-align: left;
      white-space: normal;
    }
    .studio-slide-list button.active {
      color: #ffffff;
      background: #020617;
      border-color: #020617;
    }
    .studio-slide-preview-shell {
      order: 3;
      grid-column: 1 / -1;
      width: min(100%, 360px);
      justify-self: center;
      min-width: 0;
      aspect-ratio: 9 / 16;
      max-height: 700px;
      border: 1px solid #dfe6ef;
      border-radius: 8px;
      overflow: hidden;
      background: #020617;
    }
    .studio-slide-preview-shell iframe {
      width: 100%;
      height: 100%;
      border: 0;
      display: block;
      background: #020617;
    }
    .studio-slide-editor {
      order: 2;
      display: grid;
      gap: 10px;
      min-width: 0;
    }
    .studio-slide-editor .field textarea {
      min-height: 130px;
    }
    .slide-media-current {
      min-height: 34px;
      border: 1px solid #dbe3ed;
      border-radius: 8px;
      padding: 8px 10px;
      color: #475569;
      background: #f8fbff;
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    .slide-composer-status {
      margin: 0;
    }
    @media (min-width: 1500px) {
      .slide-composer-grid {
        grid-template-columns: 180px minmax(260px, 1fr) minmax(280px, 340px);
      }
      .studio-slide-preview-shell {
        order: 0;
        grid-column: auto;
        width: 100%;
        justify-self: stretch;
      }
      .studio-slide-list,
      .studio-slide-editor {
        order: 0;
      }
    }
    [hidden] { display: none !important; }
    @media (max-width: 980px) {
      .yv-app { grid-template-columns: 1fr; }
      .yv-sidebar {
        position: sticky;
        top: 0;
        z-index: 20;
        min-width: 0;
        max-width: 100vw;
        height: auto;
        gap: 8px;
        padding: 10px 16px;
        overflow: hidden;
        border-right: 0;
        border-bottom: 1px solid #dedbd4;
      }
      .workspace-button { min-height: 34px; }
      .sidebar-nav {
        display: flex;
        gap: 6px;
        width: 100%;
        margin: 0;
        padding: 0 0 4px;
        overflow-x: auto;
        scrollbar-width: none;
      }
      .sidebar-nav::-webkit-scrollbar { display: none; }
      .nav-section-label, .sidebar-spacer, .upgrade-card, .user-chip { display: none; }
      .nav-item {
        width: auto;
        min-width: max-content;
        min-height: 32px;
        padding: 7px 10px !important;
        white-space: nowrap;
      }
      .yv-main { min-width: 0; max-width: 100vw; padding: 22px 16px; overflow: hidden; }
      .feature-card, .studio-layout, .quick-grid, .library-toolbar, .project-library-grid, .publish-workspace, .platform-grid, .connections-layout, .connections-toolbar, .connection-form-grid, .connection-project-grid, .stat-grid, .storyboard-media-grid, .template-gallery, .template-flow, .template-live-layout, .template-editor-grid, .template-editor-two-cols, .slide-composer-grid { grid-template-columns: 1fr; }
      .connection-project-menu-head { grid-template-columns: 1fr; }
      .library-actions { justify-content: flex-start; }
      .feature-card { padding: 0 0 16px; }
      .feature-media { width: 100%; height: 220px; }
      .studio-steps-line { flex-wrap: wrap; row-gap: 4px; }
      .mode-tabs {
        display: flex;
        width: 100%;
        max-width: 100%;
        overflow-x: auto;
        scrollbar-width: none;
      }
      .mode-tabs::-webkit-scrollbar { display: none; }
      .mode-tabs a { flex: 0 0 auto; min-width: 96px; padding: 7px 10px !important; white-space: nowrap; }
      .editor-panel { min-height: 360px; }
      .script-editor { min-height: 300px; }
      .setup-panel { position: static; height: auto; padding: 18px 0 0; }
      .studio-main { padding-right: 0; }
      .publish-side, .template-live-side, .template-editor-preview { position: static; }
      .template-live-frame-wrap { height: 620px; min-height: 520px; }
      .project-library-card { grid-template-columns: 1fr; }
      .project-card-preview, .project-card-preview video, .project-card-fallback { min-height: 220px; }
      .storyboard-head { display: grid; }
      .page-head-with-actions {
        display: grid;
      }
      .page-actions {
        justify-content: flex-start;
      }
    }
"""


UI_ICONS = {
    "home": '<path d="m3 10 9-7 9 7"/><path d="M5 10v10h14V10"/><path d="M10 20v-6h4v6"/>',
    "sparkles": '<path d="m12 3-1.9 5.1L5 10l5.1 1.9L12 17l1.9-5.1L19 10l-5.1-1.9Z"/><path d="M5 3v4"/><path d="M3 5h4"/><path d="M19 17v4"/><path d="M17 19h4"/>',
    "file-up": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6"/><path d="M12 18v-6"/><path d="m9 15 3-3 3 3"/>',
    "mic": '<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><path d="M12 19v3"/>',
    "layout-template": '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M3 9h18"/><path d="M9 21V9"/>',
    "folder": '<path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7l-2-2H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2Z"/>',
    "share": '<circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="m8.6 13.5 6.8 4"/><path d="m15.4 6.5-6.8 4"/>',
    "upload": '<path d="M12 13V3"/><path d="m8 7 4-4 4 4"/><path d="M20 17.6A5 5 0 0 1 18 18H7a5 5 0 0 1-1-9.9A7 7 0 0 1 19 9"/>',
    "download": '<path d="M12 3v10"/><path d="m8 9 4 4 4-4"/><path d="M20 17.6A5 5 0 0 1 18 18H7a5 5 0 0 1-1-9.9A7 7 0 0 1 19 9"/>',
    "volume": '<path d="M11 5 6 9H2v6h4l5 4Z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M19 5a9 9 0 0 1 0 14"/>',
    "play": '<circle cx="12" cy="12" r="10"/><path d="m10 8 6 4-6 4Z"/>',
    "film": '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M7 3v18"/><path d="M17 3v18"/><path d="M3 8h4"/><path d="M3 16h4"/><path d="M17 8h4"/><path d="M17 16h4"/>',
    "image": '<rect width="18" height="18" x="3" y="3" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21"/>',
    "settings": '<path d="M12.2 2h-.4a2 2 0 0 0-2 2v.2a2 2 0 0 1-1 1.7l-.4.2a2 2 0 0 1-2 0l-.2-.1a2 2 0 0 0-2.7.7l-.2.4a2 2 0 0 0 .7 2.7l.2.1a2 2 0 0 1 1 1.7v.5a2 2 0 0 1-1 1.7l-.2.1a2 2 0 0 0-.7 2.7l.2.4a2 2 0 0 0 2.7.7l.2-.1a2 2 0 0 1 2 0l.4.2a2 2 0 0 1 1 1.7v.2a2 2 0 0 0 2 2h.4a2 2 0 0 0 2-2v-.2a2 2 0 0 1 1-1.7l.4-.2a2 2 0 0 1 2 0l.2.1a2 2 0 0 0 2.7-.7l.2-.4a2 2 0 0 0-.7-2.7l-.2-.1a2 2 0 0 1-1-1.7v-.5a2 2 0 0 1 1-1.7l.2-.1a2 2 0 0 0 .7-2.7l-.2-.4a2 2 0 0 0-2.7-.7l-.2.1a2 2 0 0 1-2 0l-.4-.2a2 2 0 0 1-1-1.7V4a2 2 0 0 0-2-2Z"/><circle cx="12" cy="12" r="3"/>',
    "external": '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
    "copy": '<rect width="14" height="14" x="8" y="8" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
    "trash": '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/>',
    "refresh": '<path d="M21 12a9 9 0 0 1-9 9 9.8 9.8 0 0 1-6.7-2.7L3 16"/><path d="M3 21v-5h5"/><path d="M3 12a9 9 0 0 1 15.8-6L21 8"/><path d="M21 3v5h-5"/>',
    "hard-drive": '<path d="M22 12H2"/><path d="m5.4 5.4-3 6A2 2 0 0 0 2.2 13l2.4 4.8A2 2 0 0 0 6.4 19h11.2a2 2 0 0 0 1.8-1.2l2.4-4.8a2 2 0 0 0-.2-1.6l-3-6A2 2 0 0 0 16.8 4H7.2a2 2 0 0 0-1.8 1.4Z"/><circle cx="18" cy="16" r="1"/>',
    "layers": '<path d="m12 2 10 5-10 5L2 7Z"/><path d="m2 17 10 5 10-5"/><path d="m2 12 10 5 10-5"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "wand": '<path d="M15 4V2"/><path d="M15 16v-2"/><path d="M8 9H6"/><path d="M20 9h-2"/><path d="m17.8 6.2 1.4-1.4"/><path d="m5 19 8-8"/><path d="m11.2 6.2-1.4-1.4"/><path d="m17.8 11.8 1.4 1.4"/>',
    "book": '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H22"/><path d="M6.5 2H22v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2Z"/>',
    "key": '<circle cx="7.5" cy="15.5" r="5.5"/><path d="m12 11 8-8"/><path d="m17 6 2 2"/><path d="m14 9 2 2"/>',
    "plus": '<path d="M12 5v14"/><path d="M5 12h14"/>',
    "globe": '<circle cx="12" cy="12" r="10"/><path d="M2 12h20"/><path d="M12 2a15.3 15.3 0 0 1 0 20"/><path d="M12 2a15.3 15.3 0 0 0 0 20"/>',
    "chevron-down": '<path d="m6 9 6 6 6-6"/>',
    "close": '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
}


def ui_icon(name: str, class_name: str = "ui-icon") -> str:
    paths = UI_ICONS.get(name)
    if not paths:
        return ""
    return (
        f'<svg class="{html.escape(class_name)}" viewBox="0 0 24 24" fill="none" '
        f'stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{paths}</svg>'
    )


def icon_label(icon: str, label: str) -> str:
    icon_html = ui_icon(icon)
    return f"{icon_html}<span>{html.escape(label)}</span>" if icon_html else html.escape(label)


def app_nav_item(label: str, href: str, active: bool, icon: str = "") -> str:
    active_class = " active" if active else ""
    icon_html = f'<span class="nav-icon">{ui_icon(icon)}</span>' if icon else ""
    return f'<a class="nav-item{active_class}" href="{html.escape(href)}">{icon_html}<span>{html.escape(label)}</span></a>'


def app_language_switch() -> str:
    lang = active_language()
    next_lang = "en" if lang == "vi" else "vi"
    flag_html = '<span class="flag-vn" aria-hidden="true"><span>★</span></span>' if lang == "vi" else '<span class="flag-en" aria-hidden="true">EN</span>'
    label = tx("Chuyển sang tiếng Anh", "Switch to Vietnamese")
    return f"""
      <button class="language-toggle" type="button" data-language-toggle="{next_lang}" aria-label="{html.escape(label)}" title="{html.escape(label)}">
        {flag_html}
      </button>
    """


def app_sidebar(active: str, project_count: int, ready_count: int) -> str:
    return f"""
    <aside class="yv-sidebar">
      <div class="workspace-button">
        <img src="/web/viro-icon.svg" alt="Viro" class="workspace-logo" />
        <span>Viro</span>
        {app_language_switch()}
      </div>
      <nav class="sidebar-nav" aria-label="App navigation">
        {app_nav_item(tx("Trang chủ", "Home"), "/home", active == "home", "home")}
        <div class="nav-section-label">Studio</div>
        {app_nav_item("AI Script", "/studio?inputMode=ai", active == "studio", "sparkles")}
        {app_nav_item(tx("Bài viết", "Article"), "/studio?inputMode=import", active == "studio-import", "file-up")}
        {app_nav_item(tx("Thủ công", "Manual"), "/studio?inputMode=voiceScript", active == "studio-manual", "mic")}
        <div class="nav-section-label">{html.escape(tx("Sản xuất", "Production"))}</div>
        {app_nav_item("Template", "/templates", active == "templates", "layout-template")}
        {app_nav_item("Project", "/projects", active == "projects", "folder")}
        {app_nav_item(tx("Tài khoản & Key", "Secret Hub"), "/connections", active == "connections", "key")}
        {app_nav_item(tx("Nền tảng", "Platforms"), "/platforms", active == "platforms", "share")}
        {app_nav_item("Publish", "/upload", active == "publish", "upload")}
        {app_nav_item(tx("Hướng dẫn audio", "Audio guide"), "/elevenlabs-guide", active == "audio", "volume")}
      </nav>
      <div class="sidebar-spacer"></div>
      <div class="upgrade-card">
        <strong>{html.escape(tx("Studio local", "Local studio"))}</strong>
        <span>{project_count} project, {ready_count} video</span>
      </div>
      <div class="user-chip">
        <span class="avatar-mark">T</span>
        <div><strong>Template 3</strong><span>{html.escape(tx("Workspace render local", "Local render workspace"))}</span></div>
      </div>
    </aside>
    """


def project_preview(projects: list[dict], title: str) -> str:
    video_project = next((project for project in projects if project.get("video_url")), None)
    if video_project:
        return f'<video muted playsinline preload="metadata" src="{html.escape(video_project["video_url"])}"></video>'
    return f'<div class="feature-fallback">{html.escape(title)}</div>'


def app_project_data_script(projects: list[dict], selected_project: str) -> str:
    templates = list_templates()
    voice_connections = studio_voice_connections()
    return f"""
  <script>
    window.__INITIAL_PROJECT__ = {json.dumps(selected_project, ensure_ascii=False)};
    window.__PROJECTS__ = {json.dumps(projects, ensure_ascii=False)};
    window.__TEMPLATES__ = {json.dumps(templates, ensure_ascii=False)};
    window.__VOICE_CONNECTIONS__ = {json.dumps(voice_connections, ensure_ascii=False)};
    window.__DEFAULT_VOICE_CONNECTION_ID__ = {json.dumps(selected_studio_voice_connection_id(voice_connections), ensure_ascii=False)};
    window.__VIRO_LANG__ = {json.dumps(active_language(), ensure_ascii=False)};
    window.__PROJECT_SOURCE_ROOT__ = {json.dumps(str(SLIDE_ROOT), ensure_ascii=False)};
    window.__TEMPLATE_SOURCE_ROOT__ = {json.dumps(str(TEMPLATE_ROOT), ensure_ascii=False)};
  </script>
  <script src="/web/render_page.js?v=20260607-ai-storyboard-v1"></script>
"""


def create_project_modal_html(selected_template: str = "") -> str:
    templates = list_templates()
    selected_template = selected_template_name(templates, selected_template)
    template_options = template_options_html(templates, selected_template)
    voice_options = studio_voice_connection_options_html(include_empty=True)
    lang = active_language()
    return f"""
      <div class="modal-backdrop" id="createProjectModal" hidden>
        <div class="modal-card create-project-modal" role="dialog" aria-modal="true" aria-labelledby="createProjectTitle">
          <button class="modal-close" id="closeCreateProject" type="button" aria-label="{html.escape(tx("Đóng", "Close"))}">x</button>
          <p class="eyebrow">{html.escape(tx("Project mới", "New project"))}</p>
          <h3 id="createProjectTitle">{html.escape(tx("Tạo project", "Create project"))}</h3>
          <p class="modal-copy">{html.escape(tx("Project là một folder local riêng, được tạo từ template và có metadata riêng.", "A project is its own local folder, created from a template with separate metadata."))}</p>
          <form id="createProjectForm" class="create-project-form">
            <input type="hidden" name="input_mode" value="ai" />
            <label class="field">
              <span>{html.escape(tx("Tên project", "Project name"))}</span>
              <input name="name" type="text" maxlength="120" autocomplete="off" placeholder="{html.escape(tx("VD: bản tin AI hôm nay", "Example: daily AI news"))}" />
            </label>
            <label class="field">
              <span>Template</span>
              <select name="template" {"disabled" if not templates else ""}>{template_options}</select>
            </label>
            <label class="field">
              <span>{html.escape(tx("Ngôn ngữ", "Language"))}</span>
              <select name="language">
                <option value="vi" {"selected" if lang == "vi" else ""}>Tiếng Việt</option>
                <option value="en" {"selected" if lang == "en" else ""}>English</option>
              </select>
            </label>
            <label class="field">
              <span>{html.escape(tx("Ghi chú", "Notes"))}</span>
              <textarea name="notes" rows="3" maxlength="500" placeholder="{html.escape(tx("Mục tiêu, kênh đăng, key dự kiến...", "Goal, publishing channel, planned key..."))}"></textarea>
            </label>
            <label class="field">
              <span>{html.escape(tx("Giọng đọc API", "Voice API"))}</span>
              <select name="voice_connection_id">{voice_options}</select>
            </label>
            <div class="modal-actions">
              <button class="secondary-btn" id="cancelCreateProject" type="button">{icon_label("trash", tx("Hủy", "Cancel"))}</button>
              <button class="primary-btn" type="submit">{icon_label("plus", tx("Tạo project", "Create project"))}</button>
            </div>
            <div class="status warn create-project-status" hidden></div>
          </form>
        </div>
      </div>
    """


def template_management_modals_html() -> str:
    templates = list_templates()
    base_options = template_options_html(templates, selected_template_name(templates, "viro-slide-starter"))
    voice_options = studio_voice_connection_options_html(include_empty=True)
    return f"""
      <div class="modal-backdrop" id="createTemplateModal" hidden>
        <div class="modal-card template-modal" role="dialog" aria-modal="true" aria-labelledby="createTemplateTitle">
          <button class="modal-close" id="closeCreateTemplate" type="button" aria-label="Đóng">x</button>
          <p class="eyebrow">Template mới</p>
          <h3 id="createTemplateTitle">Tạo template</h3>
          <p class="modal-copy">Template mới sẽ được clone từ một template gốc để có đủ runtime, style, motion và file cấu hình.</p>
          <form id="createTemplateForm" class="template-form">
            <label class="field">
              <span>Tên template</span>
              <input name="title" type="text" maxlength="120" autocomplete="off" placeholder="VD: News explainer 60s" required />
            </label>
            <label class="field">
              <span>Clone từ</span>
              <select name="base_template" {"disabled" if not templates else ""}>{base_options}</select>
            </label>
            <label class="field">
              <span>Mô tả</span>
              <textarea name="description" rows="3" maxlength="1000" placeholder="Template này phù hợp với dạng video nào?"></textarea>
            </label>
            <label class="field">
              <span>Tags</span>
              <input name="tags" type="text" maxlength="240" placeholder="news, explainer, short" />
            </label>
            <div class="template-editor-two-cols">
              <label class="field">
                <span>Pack</span>
                <input name="pack" type="text" maxlength="80" placeholder="VD: News Shorts" />
              </label>
              <label class="field">
                <span>Variant</span>
                <input name="variant" type="text" maxlength="80" placeholder="VD: Vertical 9:16" />
              </label>
            </div>
            <div class="template-editor-two-cols">
              <label class="field">
                <span>Tỉ lệ</span>
                <input name="aspect_ratio" type="text" maxlength="24" value="9:16" />
              </label>
              <label class="field">
                <span>Nền tảng</span>
                <input name="platforms" type="text" maxlength="240" placeholder="TikTok, YouTube Shorts, Reels" />
              </label>
            </div>
            <label class="field">
              <span>Voice API mặc định</span>
              <select name="voice_connection_id">{voice_options}</select>
            </label>
            <label class="field">
              <span>Script mặc định</span>
              <textarea name="script" rows="5" placeholder="Mỗi dòng là một slide mặc định. Bỏ trống để dùng script từ template gốc."></textarea>
            </label>
            <div class="modal-actions">
              <button class="secondary-btn" id="cancelCreateTemplate" type="button">{icon_label("trash", "Hủy")}</button>
              <button class="primary-btn" type="submit">{icon_label("plus", "Tạo template")}</button>
            </div>
            <div class="status warn template-status" hidden></div>
          </form>
        </div>
      </div>
      <div class="modal-backdrop" id="importTemplateModal" hidden>
        <div class="modal-card template-modal" role="dialog" aria-modal="true" aria-labelledby="importTemplateTitle">
          <button class="modal-close" id="closeImportTemplate" type="button" aria-label="Đóng">x</button>
          <p class="eyebrow">Template package</p>
          <h3 id="importTemplateTitle">Import template</h3>
          <p class="modal-copy">Chọn file <code>.zip</code> đã export từ Viro. App sẽ tạo folder template mới, tự đổi slug nếu tên đã tồn tại.</p>
          <form id="importTemplateForm" class="template-form">
            <div class="field">
              <span>File ZIP</span>
              <input class="hidden-file-input" id="templateArchiveFile" name="archive" type="file" accept=".zip,application/zip,application/x-zip-compressed" required />
              <label class="secondary-btn" for="templateArchiveFile">{icon_label("upload", "Chọn file ZIP")}</label>
              <small class="source-note" id="templateArchiveName">Chưa chọn file.</small>
            </div>
            <label class="field">
              <span>Tên hiển thị mới</span>
              <input name="title" type="text" maxlength="120" autocomplete="off" placeholder="Bỏ trống để dùng tên trong package" />
            </label>
            <label class="field">
              <span>Slug folder mới</span>
              <input name="slug" type="text" maxlength="120" autocomplete="off" placeholder="Bỏ trống để tự tạo slug" />
            </label>
            <label class="field">
              <span>Mô tả</span>
              <textarea name="description" rows="3" maxlength="1000" placeholder="Ghi chú mục đích sau khi import"></textarea>
            </label>
            <div class="modal-actions">
              <button class="secondary-btn" id="cancelImportTemplate" type="button">{icon_label("trash", "Hủy")}</button>
              <button class="primary-btn" type="submit">{icon_label("upload", "Import template")}</button>
            </div>
            <div class="status warn template-status" hidden></div>
          </form>
        </div>
      </div>
      <div class="modal-backdrop" id="editTemplateModal" hidden>
        <div class="modal-card template-modal" role="dialog" aria-modal="true" aria-labelledby="editTemplateTitle">
          <button class="modal-close" id="closeEditTemplate" type="button" aria-label="Đóng">x</button>
          <p class="eyebrow">Template editor</p>
          <h3 id="editTemplateTitle">Sửa template</h3>
          <p class="modal-copy">Chỉnh metadata, script mặc định, rules và preview settings của template. Runtime HTML/CSS/JS vẫn được sửa trực tiếp trong folder template khi cần.</p>
          <form id="editTemplateForm" class="template-form">
            <input type="hidden" name="template" />
            <label class="field">
              <span>Folder</span>
              <input name="template_display" type="text" disabled />
            </label>
            <label class="field">
              <span>Tên hiển thị</span>
              <input name="title" type="text" maxlength="120" autocomplete="off" />
            </label>
            <label class="field">
              <span>Mô tả</span>
              <textarea name="description" rows="3" maxlength="1000"></textarea>
            </label>
            <label class="field">
              <span>Tags</span>
              <input name="tags" type="text" maxlength="240" placeholder="news, explainer, short" />
            </label>
            <div class="template-editor-two-cols">
              <label class="field">
                <span>Pack</span>
                <input name="pack" type="text" maxlength="80" placeholder="VD: News Shorts" />
              </label>
              <label class="field">
                <span>Variant</span>
                <input name="variant" type="text" maxlength="80" placeholder="VD: Vertical 9:16" />
              </label>
            </div>
            <div class="template-editor-two-cols">
              <label class="field">
                <span>Tỉ lệ</span>
                <input name="aspect_ratio" type="text" maxlength="24" />
              </label>
              <label class="field">
                <span>Nền tảng</span>
                <input name="platforms" type="text" maxlength="240" placeholder="TikTok, YouTube Shorts, Reels" />
              </label>
            </div>
            <label class="field">
              <span>Voice API mặc định</span>
              <select name="voice_connection_id">{voice_options}</select>
            </label>
            <label class="field">
              <span>script-90s.txt</span>
              <textarea name="script" rows="7"></textarea>
            </label>
            <label class="field">
              <span>TEMPLATE_RULES.md</span>
              <textarea name="rules" rows="6"></textarea>
            </label>
            <label class="field">
              <span>preview-settings.json</span>
              <textarea name="preview_settings" rows="6" placeholder='{{"theme": {{...}}}}'></textarea>
            </label>
            <div class="modal-actions">
              <button class="secondary-btn" id="cancelEditTemplate" type="button">{icon_label("trash", "Hủy")}</button>
              <button class="primary-btn" type="submit">{icon_label("check", "Lưu template")}</button>
            </div>
            <div class="status warn template-status" hidden></div>
          </form>
        </div>
      </div>
    """


def save_project_as_template_modal_html() -> str:
    return f"""
      <div class="modal-backdrop" id="saveProjectTemplateModal" hidden>
        <div class="modal-card template-modal" role="dialog" aria-modal="true" aria-labelledby="saveProjectTemplateTitle">
          <button class="modal-close" id="closeSaveProjectTemplate" type="button" aria-label="Đóng">x</button>
          <p class="eyebrow">Project snapshot</p>
          <h3 id="saveProjectTemplateTitle">Lưu thành template</h3>
          <p class="modal-copy">Tạo template mới từ project đang chọn. Output video, audio input, project metadata và publish metadata sẽ không được copy.</p>
          <form id="saveProjectTemplateForm" class="template-form">
            <input type="hidden" name="project" />
            <label class="field">
              <span>Project nguồn</span>
              <input name="project_display" type="text" disabled />
            </label>
            <label class="field">
              <span>Tên template mới</span>
              <input name="title" type="text" maxlength="120" autocomplete="off" placeholder="VD: News explainer base" required />
            </label>
            <label class="field">
              <span>Mô tả</span>
              <textarea name="description" rows="3" maxlength="1000" placeholder="Template này sinh ra từ project nào, dùng cho nội dung gì?"></textarea>
            </label>
            <div class="template-editor-two-cols">
              <label class="field">
                <span>Pack</span>
                <input name="pack" type="text" maxlength="80" placeholder="VD: News Shorts" />
              </label>
              <label class="field">
                <span>Variant</span>
                <input name="variant" type="text" maxlength="80" placeholder="VD: Vertical 9:16" />
              </label>
            </div>
            <div class="template-editor-two-cols">
              <label class="field">
                <span>Tỉ lệ</span>
                <input name="aspect_ratio" type="text" maxlength="24" value="9:16" />
              </label>
              <label class="field">
                <span>Nền tảng</span>
                <input name="platforms" type="text" maxlength="240" placeholder="TikTok, YouTube Shorts, Reels" />
              </label>
            </div>
            <label class="field">
              <span>Tags</span>
              <input name="tags" type="text" maxlength="240" placeholder="news, explainer, short" />
            </label>
            <div class="modal-actions">
              <button class="secondary-btn" id="cancelSaveProjectTemplate" type="button">{icon_label("trash", "Hủy")}</button>
              <button class="primary-btn" type="submit">{icon_label("check", "Lưu template mới")}</button>
            </div>
            <div class="status warn template-status" hidden></div>
          </form>
        </div>
      </div>
    """


def render_app_shell(
    *,
    title: str,
    active: str,
    main_html: str,
    selected_project: str = "",
    projects: list[dict] | None = None,
    extra_style: str = "",
    extra_script: str = "",
) -> bytes:
    projects = projects if projects is not None else list_projects()
    ready_count = sum(1 for project in projects if project.get("video_url"))
    body = f"""
  <div class="yv-app">
    {app_sidebar(active, len(projects), ready_count)}
    <main class="yv-main">
      {main_html}
    </main>
    {create_project_modal_html()}
    {template_management_modals_html()}
    {save_project_as_template_modal_html()}
  </div>
"""
    script = app_project_data_script(projects, selected_project) + extra_script
    return render_page_shell(
        title=title,
        body=body,
        extra_style=APP_SHELL_STYLE + extra_style,
        extra_script=script,
    )


def app_selected_project(projects: list[dict], selected_project: str | None) -> str:
    project_names = {project["name"] for project in projects}
    return selected_project if selected_project in project_names else (projects[0]["name"] if projects else "")


def selected_template_name(templates: list[dict], selected_template: str | None = None) -> str:
    names = {template["name"] for template in templates}
    if selected_template in names:
        return str(selected_template)
    starter = next((template["name"] for template in templates if template["name"] == "viro-slide-starter"), "")
    return starter or (templates[0]["name"] if templates else "")


def template_options_html(templates: list[dict], selected_template: str) -> str:
    return "\n".join(
        f'<option value="{html.escape(template["name"])}" {"selected" if template["name"] == selected_template else ""}>{html.escape(template["name"])}</option>'
        for template in templates
    )


def render_template_preview(template: dict) -> str:
    if template.get("demo_url"):
        poster = f' poster="{html.escape(str(template["preview_url"]))}"' if template.get("preview_url") else ""
        return f'<video muted playsinline loop preload="metadata"{poster} src="{html.escape(str(template["demo_url"]))}"></video>'
    if template.get("preview_url"):
        return f'<img src="{html.escape(str(template["preview_url"]))}" alt="{html.escape(template["name"])} preview" loading="lazy" />'
    return f'<div class="template-preview-fallback">{ui_icon("layout-template")}<span>{html.escape(template["name"].replace("-", " "))}</span></div>'


def render_template_card(template: dict, selected_template: str) -> str:
    name = html.escape(template["name"])
    title = html.escape(str(template.get("title") or template["name"]))
    description = html.escape(str(template.get("description") or ""))
    pack = html.escape(str(template.get("pack") or "General"))
    variant = html.escape(str(template.get("variant") or "Default"))
    platforms = ", ".join(str(item) for item in template.get("platforms", []) if str(item).strip())
    ready = bool(template.get("has_index") and template.get("has_style") and template.get("has_app"))
    ready_class = "ok" if ready else "warn"
    ready_label = "dùng được" if ready else "chưa đủ file"
    preview_settings = "có cấu hình" if template.get("has_preview_settings") else "chưa có cấu hình"
    selected_class = " selected" if template["name"] == selected_template else ""
    edit_url = html.escape(str(template.get("edit_url") or f"/template/{quote(str(template['name']))}/edit"))
    return f"""
      <li class="template-card{selected_class}" data-template="{name}">
        <div class="template-preview">{render_template_preview(template)}</div>
        <div class="template-card-body">
          <h2>{title}</h2>
          {f'<p class="source-note">{description}</p>' if description else ''}
          <div class="mini-flow">
            <span>{pack}</span>
            <span>{variant}</span>
            <span>{html.escape(str(template.get("aspect_ratio") or "9:16"))}</span>
            {f'<span>{html.escape(platforms)}</span>' if platforms else ''}
          </div>
          <div class="template-meta">
            <span class="template-badge {ready_class}">{ui_icon("check")}{ready_label}</span>
            <span class="template-badge">{ui_icon("layers")}{template["script_count"] or "?"} slides</span>
            <span class="template-badge">{ui_icon("settings")}{preview_settings}</span>
          </div>
          <div class="template-actions">
            <button class="secondary-btn template-select-btn" type="button" data-template="{name}">{icon_label("check", "Chọn")}</button>
            <a class="primary-btn" href="{edit_url}">{icon_label("settings", "Sửa")}</a>
            <a class="secondary-btn" href="{html.escape(str(template["url"]))}" target="_blank" rel="noreferrer">{icon_label("external", "Xem trước")}</a>
          </div>
        </div>
      </li>
    """


def render_template_detail_html(template_name: str) -> bytes:
    projects = list_projects()
    templates = list_templates()
    selected_project = app_selected_project(projects, None)
    template = next((item for item in templates if item["name"] == template_name), None)
    if not template:
        raise FileNotFoundError(f"Template not found: {template_name}")
    name = html.escape(str(template["name"]))
    ready = bool(template.get("has_index") and template.get("has_style") and template.get("has_app"))
    status_class = "ok" if ready else "warn"
    status_label = "Dùng được" if ready else "Thiếu file"
    preview_src = f"/template/{quote(str(template['name']))}/index.html?embed=1&autostart=1"
    studio_url = f"/studio?template={quote(str(template['name']))}"
    main_html = f"""
      <div class="page-frame">
        <header class="page-head page-head-with-actions">
          <div>
            <p class="eyebrow">Template Preview</p>
            <h1>{name}</h1>
            <p>Preview motion/audio của template trong shell Viro. Khi sản xuất video, tạo project rồi mở Studio.</p>
          </div>
          <div class="page-actions">
            <a class="secondary-btn" href="/templates">{icon_label("layout-template", "Template")}</a>
            <a class="primary-btn" href="{studio_url}">{icon_label("sparkles", "Mở Studio")}</a>
          </div>
        </header>
        <div class="template-live-layout">
          <section class="template-live-panel" aria-label="Live template preview">
            <div class="template-live-frame-wrap">
              <iframe class="template-live-frame" src="{preview_src}" title="{name} preview" loading="eager"></iframe>
            </div>
          </section>
          <aside class="template-live-side">
            <section class="setup-card">
              <h2>{icon_label("layout-template", "Template")}</h2>
              <div class="template-meta">
                <span class="template-badge {status_class}">{ui_icon("check")}{status_label}</span>
                <span class="template-badge">{ui_icon("layers")}{template["script_count"] or "?"} slides</span>
                <span class="template-badge">{ui_icon("settings")}{"Có setting" if template.get("has_preview_settings") else "Chưa có setting"}</span>
              </div>
              <p class="source-note source-path">Source: {html.escape(str(TEMPLATE_ROOT / str(template["name"])))}</p>
              <div class="form-actions">
                <a class="primary-btn" href="/template/{quote(str(template["name"]))}/edit">{icon_label("settings", "Sửa template")}</a>
                <a class="secondary-btn" href="{studio_url}">{icon_label("sparkles", "Mở Studio")}</a>
              </div>
            </section>
            <section class="setup-card">
              <h2>{icon_label("layers", "Flow")}</h2>
              <div class="template-flow" style="grid-template-columns: 1fr; margin: 0;">
                <div class="template-flow-step"><span>01</span><strong>{ui_icon("folder")}Project</strong><p>Project giữ metadata, script, media và output riêng.</p></div>
                <div class="template-flow-step"><span>02</span><strong>{ui_icon("layout-template")}Template</strong><p>Template chỉ giữ layout, motion và runtime.</p></div>
                <div class="template-flow-step"><span>03</span><strong>{ui_icon("film")}Render</strong><p>Studio ghép script, voice và slide thành MP4.</p></div>
              </div>
            </section>
          </aside>
        </div>
      </div>
    """
    return render_app_shell(title=f"{template['name']} | Viro", active="templates", main_html=main_html, selected_project=selected_project, projects=projects)


def render_template_editor_html(template_name: str) -> bytes:
    projects = list_projects()
    selected_project = app_selected_project(projects, None)
    detail = template_detail_response(template_name)
    template = detail.get("template")
    if not template:
        raise FileNotFoundError(f"Template not found: {template_name}")
    editable = detail.get("editable") if isinstance(detail.get("editable"), dict) else {}
    name = str(template["name"])
    name_html = html.escape(name)
    title_value = html.escape(str(editable.get("title") or template.get("title") or name), quote=True)
    description_value = html.escape(str(editable.get("description") or ""))
    tags_value = html.escape(str(editable.get("tags") or ""), quote=True)
    pack_value = html.escape(str(editable.get("pack") or "General"), quote=True)
    variant_value = html.escape(str(editable.get("variant") or "Default"), quote=True)
    aspect_value = html.escape(str(editable.get("aspect_ratio") or "9:16"), quote=True)
    platforms_value = html.escape(str(editable.get("platforms") or ""), quote=True)
    script_value = html.escape(str(editable.get("script") or ""))
    rules_value = html.escape(str(editable.get("rules") or ""))
    preview_settings_value = html.escape(str(editable.get("preview_settings") or ""))
    voice_options = studio_voice_connection_options_html(
        str(editable.get("voice_connection_id") or ""),
        include_empty=True,
    )
    preview_src = f"/template/{quote(name)}/index.html?embed=1&autostart=1"
    export_url = f"/api/templates/export?template={quote(name)}"
    main_html = f"""
      <div class="page-frame template-editor-page">
        <header class="page-head page-head-with-actions">
          <div>
            <p class="eyebrow">Template editor</p>
            <h1>{name_html}</h1>
            <p>Chỉnh layout metadata, script mẫu, rules và preview settings của template.</p>
          </div>
          <div class="page-actions">
            <a class="secondary-btn" href="/templates?template={quote(name)}">{icon_label("layout-template", "Template")}</a>
            <a class="secondary-btn" href="{export_url}">{icon_label("download", "Export")}</a>
            <a class="primary-btn" href="/studio?template={quote(name)}">{icon_label("sparkles", "Mở Studio")}</a>
          </div>
        </header>
        <div class="template-editor-grid">
          <section class="template-editor-preview" aria-label="Template preview">
            <div class="template-live-frame-wrap">
              <iframe id="templateEditorPreview" class="template-live-frame" src="{preview_src}" title="{name_html} preview" loading="eager"></iframe>
            </div>
          </section>
          <aside class="template-editor-panel">
            <form id="templateEditorForm" class="template-form template-editor-form" data-stay="editor">
              <input type="hidden" name="template" value="{name_html}" />
              <section class="setup-card">
                <h2>{icon_label("layout-template", "Thông tin")}</h2>
                <label class="field">
                  <span>Folder</span>
                  <input name="template_display" type="text" value="{name_html}" disabled />
                </label>
                <label class="field">
                  <span>Tên hiển thị</span>
                  <input name="title" type="text" maxlength="120" autocomplete="off" value="{title_value}" />
                </label>
                <label class="field">
                  <span>Mô tả</span>
                  <textarea name="description" rows="3" maxlength="1000">{description_value}</textarea>
                </label>
                <div class="template-editor-two-cols">
                  <label class="field">
                    <span>Pack</span>
                    <input name="pack" type="text" maxlength="80" value="{pack_value}" placeholder="VD: News Shorts" />
                  </label>
                  <label class="field">
                    <span>Variant</span>
                    <input name="variant" type="text" maxlength="80" value="{variant_value}" placeholder="VD: TikTok 9:16" />
                  </label>
                </div>
                <div class="template-editor-two-cols">
                  <label class="field">
                    <span>Tỉ lệ</span>
                    <input name="aspect_ratio" type="text" maxlength="24" value="{aspect_value}" placeholder="9:16" />
                  </label>
                  <label class="field">
                    <span>Nền tảng</span>
                    <input name="platforms" type="text" maxlength="240" value="{platforms_value}" placeholder="TikTok, YouTube Shorts, Reels" />
                  </label>
                </div>
                <label class="field">
                  <span>Tags</span>
                  <input name="tags" type="text" maxlength="240" value="{tags_value}" placeholder="news, explainer, short" />
                </label>
                <label class="field">
                  <span>Voice API mặc định</span>
                  <select name="voice_connection_id">{voice_options}</select>
                </label>
              </section>
              <section class="setup-card">
                <h2>{icon_label("file-text", "Nội dung mẫu")}</h2>
                <label class="field">
                  <span>script-90s.txt</span>
                  <textarea name="script" rows="8">{script_value}</textarea>
                </label>
                <label class="field">
                  <span>TEMPLATE_RULES.md</span>
                  <textarea name="rules" rows="7">{rules_value}</textarea>
                </label>
                <label class="field">
                  <span>preview-settings.json</span>
                  <textarea name="preview_settings" rows="7" spellcheck="false">{preview_settings_value}</textarea>
                </label>
                <div class="form-actions">
                  <button class="primary-btn" type="submit">{icon_label("check", "Lưu template")}</button>
                  <a class="secondary-btn" href="/template/{quote(name)}/" target="_blank" rel="noreferrer">{icon_label("external", "Xem trước")}</a>
                </div>
                <div class="status warn template-status" hidden></div>
              </section>
            </form>
          </aside>
        </div>
      </div>
    """
    return render_app_shell(title=f"Edit {name} | Viro", active="templates", main_html=main_html, selected_project=selected_project, projects=projects)


def render_templates_html(selected_template: str | None = None) -> bytes:
    projects = list_projects()
    templates = list_templates()
    selected_project = app_selected_project(projects, None)
    selected_template = selected_template_name(templates, selected_template)
    cards = "\n".join(render_template_card(template, selected_template) for template in templates)
    pack_sections: list[str] = []
    for pack in dict.fromkeys(str(template.get("pack") or "General") for template in templates):
        pack_templates = [template for template in templates if str(template.get("pack") or "General") == pack]
        pack_cards = "\n".join(render_template_card(template, selected_template) for template in pack_templates)
        pack_sections.append(
            f"""
            <section class="template-pack-section">
              <div class="template-pack-head">
                <div>
                  <p class="eyebrow">Template pack</p>
                  <h2>{html.escape(pack)}</h2>
                </div>
                <span class="template-badge">{len(pack_templates)} template</span>
              </div>
              <ol class="template-gallery">{pack_cards}</ol>
            </section>
            """
        )
    if pack_sections:
        cards = "\n".join(pack_sections)
    if not cards:
        cards = '<li class="empty">Chưa tìm thấy template trong thư mục template.</li>'
    complete_count = sum(1 for template in templates if template.get("has_index") and template.get("has_style") and template.get("has_app"))
    with_demo_count = sum(1 for template in templates if template.get("demo_url") or template.get("preview_url"))
    main_html = f"""
      <div class="page-frame">
        <header class="page-head page-head-with-actions">
          <div>
            <p class="eyebrow">{html.escape(tx("Hệ thống template", "Template system"))}</p>
            <h1>Template</h1>
            <p>{html.escape(tx("Template giữ layout, style và motion. Project lưu script, storyboard, slide và output.", "Templates keep layout, style, and motion. Projects keep script, storyboard, slides, and output."))}</p>
          </div>
        </header>
        <section class="template-flow" aria-label="Template generation model">
          <div class="template-flow-step"><span>01</span><strong>{ui_icon("layout-template")}Template</strong><p>Layout, style, motion và safe zone.</p></div>
          <div class="template-flow-step"><span>02</span><strong>{ui_icon("sparkles")}Storyboard</strong><p>Script tách thành scene và voice line.</p></div>
          <div class="template-flow-step"><span>03</span><strong>{ui_icon("layers")}Slide</strong><p>Mỗi scene thành một slide.</p></div>
          <div class="template-flow-step"><span>04</span><strong>{ui_icon("film")}Render</strong><p>Ghép slide, voice và timing thành MP4.</p></div>
        </section>
        <section class="stat-grid" aria-label="Template status summary">
          <div class="stat-card"><strong>{len(templates)}</strong><span>Tổng template</span></div>
          <div class="stat-card"><strong>{complete_count}</strong><span>Dùng được</span></div>
          <div class="stat-card"><strong>{with_demo_count}</strong><span>Có preview</span></div>
        </section>
        <div class="library-toolbar">
          <label class="field">
            <span>Template đang chọn</span>
            <select id="templateSelect" {"disabled" if not templates else ""}>{template_options_html(templates, selected_template)}</select>
            <small class="source-note source-path">Source: {html.escape(str(TEMPLATE_ROOT))}</small>
          </label>
          <div class="library-actions">
            <button class="primary-btn" id="createTemplateButton" type="button" data-create-template-open>{icon_label("plus", "Tạo template")}</button>
            <button class="secondary-btn" id="importTemplateButton" type="button" data-import-template-open>{icon_label("upload", "Import")}</button>
            <a class="secondary-btn" id="exportSelectedTemplate" href="/api/templates/export?template={quote(selected_template)}">{icon_label("download", "Export")}</a>
            <a class="secondary-btn" id="editSelectedTemplate" data-template="{html.escape(selected_template)}" href="/template/{quote(selected_template)}/edit">{icon_label("settings", "Sửa template")}</a>
            <a class="secondary-btn" id="templatePreviewAction" href="/template/{quote(selected_template)}/" target="_blank" rel="noreferrer">{icon_label("external", "Xem trước")}</a>
            <button class="secondary-btn" id="refreshProjects" type="button">{icon_label("refresh", tx("Làm mới", "Refresh"))}</button>
          </div>
        </div>
        <div class="template-pack-list">{cards}</div>
      </div>
    """
    return render_app_shell(title="Templates | Viro", active="templates", main_html=main_html, selected_project=selected_project, projects=projects)


def render_app_home_html(selected_project: str | None = None) -> bytes:
    projects = list_projects()
    selected_project = app_selected_project(projects, selected_project)
    main_html = f"""
      <div class="page-frame">
        <header class="page-head page-head-with-actions">
          <div>
            <p class="eyebrow">{html.escape(tx("Trung tâm sản xuất", "Production hub"))}</p>
            <h1>Viro Studio</h1>
            <p>{html.escape(tx("Tạo project, chọn template, render video và publish trong một workspace.", "Create projects, choose templates, render videos, and publish from one workspace."))}</p>
          </div>
          <div class="page-actions">
            <button class="primary-btn" type="button" data-create-project-open>{icon_label("plus", tx("Tạo project", "Create project"))}</button>
          </div>
        </header>
        <section class="feature-list" aria-label="Studio modes">
          <article class="feature-card">
            <div class="feature-media">{project_preview(projects, "AI Script")}</div>
            <div class="feature-body">
              <h2>AI Script to Video</h2>
              <p>Nhập ý tưởng, tạo storyboard và render video dọc.</p>
              <div class="feature-actions">
                <a class="ghost-btn" href="/elevenlabs-guide">{icon_label("volume", "Hướng dẫn audio")}</a>
              </div>
            </div>
          </article>
          <article class="feature-card">
            <div class="feature-media">{project_preview(projects, "Article")}</div>
            <div class="feature-body">
              <h2>Bài viết thành video</h2>
              <p>Dán URL hoặc bài viết để tạo video ngắn.</p>
              <div class="feature-actions">
                <a class="ghost-btn" href="/projects">{icon_label("folder", "Xem project")}</a>
              </div>
            </div>
          </article>
          <article class="feature-card">
            <div class="feature-media">{project_preview(projects, "Script thủ công")}</div>
            <div class="feature-body">
              <h2>Script thủ công</h2>
              <p>Dùng script hoặc voiceover có sẵn.</p>
              <div class="feature-actions">
                <a class="ghost-btn" href="/platforms">{icon_label("share", "Nền tảng publish")}</a>
              </div>
            </div>
          </article>
        </section>
        <section class="quick-grid" aria-label="Quick actions">
          <a class="quick-card" href="/templates"><div><h3>{icon_label("layout-template", "Template")}</h3><p>{html.escape(tx("Chọn template trước khi tạo storyboard và sinh slide.", "Choose a template before creating storyboards and slides."))}</p></div><span>{ui_icon("external")}</span></a>
          <a class="quick-card" href="/projects"><div><h3>{icon_label("folder", "Project")}</h3><p>{html.escape(tx("Quản lý output, mở lại Studio, tải MP4/audio/timing.", "Manage outputs, reopen Studio, and download MP4/audio/timing."))}</p></div><span>{ui_icon("external")}</span></a>
          <a class="quick-card" href="/connections"><div><h3>{icon_label("key", tx("Tài khoản & Key", "Secret Hub"))}</h3><p>{html.escape(tx("Quản lý account, token và project đang dùng.", "Manage accounts, tokens, and project usage."))}</p></div><span>{ui_icon("external")}</span></a>
          <a class="quick-card" href="/platforms"><div><h3>{icon_label("share", tx("Nền tảng", "Platforms"))}</h3><p>{html.escape(tx("Kiểm tra YouTube/Facebook publish setup hiện có.", "Check the current YouTube/Facebook publishing setup."))}</p></div><span>{ui_icon("external")}</span></a>
          <a class="quick-card" href="/upload"><div><h3>{icon_label("upload", "Publish")}</h3><p>Mở màn hình publish chi tiết.</p></div><span>{ui_icon("external")}</span></a>
        </section>
      </div>
    """
    return render_app_shell(title="Viro Studio", active="home", main_html=main_html, selected_project=selected_project, projects=projects)


def project_option_html(projects: list[dict], selected_project: str) -> str:
    return "\n".join(
        f'<option value="{html.escape(project["name"])}" {"selected" if project["name"] == selected_project else ""}>{html.escape(project["name"])}</option>'
        for project in projects
    )


def compact_project_rows(projects: list[dict], selected_project: str) -> str:
    rows = []
    for project in projects:
        name = html.escape(project["name"])
        status = "Có script" if project["has_script"] else "Thiếu script"
        status_class = "ok" if project["has_script"] else "bad"
        selected_class = " selected" if project["name"] == selected_project else ""
        rows.append(
            f"""
            <li class="project-row{selected_class}" data-project="{name}">
              <span class="project-name">{name}</span>
              <span class="project-slide-count">{project["script_count"] or "?"} slide</span>
              <span class="status-pill {status_class}">{status}</span>
              <div class="actions">
                <button class="select-btn secondary-btn" type="button" data-project="{name}">{icon_label("check", "Chọn")}</button>
                <a class="secondary-btn" href="{html.escape(project["url"])}" target="_blank" rel="noreferrer">{icon_label("external", "Xem")}</a>
                <button class="copy-script-btn secondary-btn" type="button" data-project="{name}" title="Copy script">{icon_label("copy", "Script")}</button>
              </div>
            </li>
            """
        )
    return "\n".join(rows) or '<li class="empty">Chưa có project nào trong source folder.</li>'


def project_output_assets(project: dict) -> list[dict]:
    project_dir = Path(str(project.get("source_path") or ""))
    specs = [
        ("MP4", "output/final_video.mp4"),
        ("Voice", "output/full_voiceover.mp3"),
        ("Edge voice", "output/edge_full_voiceover.mp3"),
        ("Timing", "output/timing.json"),
        ("Word timing", "output/subtitle-word-timing.json"),
    ]
    assets = []
    for label, rel_path in specs:
        path = project_dir / rel_path
        if not path.exists():
            continue
        url = project.get("video_url") if rel_path == "output/final_video.mp4" else project_url(project["name"]) + quote_relative_url(rel_path)
        assets.append({"label": label, "url": url, "size": path.stat().st_size})
    render_versions = project.get("render_versions") if isinstance(project.get("render_versions"), list) else []
    if render_versions:
        latest = render_versions[0]
        assets.append(
            {
                "label": f"Bản riêng: {latest.get('name') or 'render.mp4'}",
                "url": latest.get("url"),
                "size": latest.get("size", 0),
            }
        )
    return assets


def project_asset_links(project: dict) -> str:
    assets = project_output_assets(project)
    if not assets:
        return '<span class="asset-chip disabled">Chưa có asset output</span>'
    return "\n".join(
        f'<a class="asset-chip" href="{html.escape(str(asset["url"]))}" target="_blank" rel="noreferrer">{html.escape(str(asset["label"]))}</a>'
        for asset in assets
    )


def render_project_library_card(project: dict, selected_project: str) -> str:
    raw_name = project["name"]
    name = html.escape(raw_name)
    selected_class = " selected" if raw_name == selected_project else ""
    has_video = bool(project.get("video_url"))
    has_output = bool(project.get("has_output"))
    preview = (
        f'<video muted playsinline preload="metadata" src="{html.escape(str(project["video_url"]))}"></video>'
        if has_video
        else f'<div class="project-card-fallback"><span>Chưa có video</span><small>{project["script_count"] or "?"} slide</small></div>'
    )
    video_slot = (
        f'<a class="asset-chip" href="{html.escape(str(project["video_url"]))}" target="_blank" rel="noreferrer">Mở MP4</a>'
        if has_video
        else '<span class="asset-chip disabled">Chưa có MP4</span>'
    )
    delete_disabled = "" if has_output else " disabled"
    delete_disabled_class = "" if has_output else " disabled"
    script_status = "Có script" if project.get("has_script") else "Thiếu script"
    script_class = "ok" if project.get("has_script") else "bad"
    video_status = "Đã render" if has_video else "Chưa render"
    video_class = "ok" if has_video else "bad"
    return f"""
      <li class="project-row project-library-card{selected_class}" data-project="{name}">
        <div class="project-card-preview">{preview}</div>
        <div class="project-card-body">
          <div class="project-card-title">
            <h2>{name}</h2>
            <span class="status-pill {video_class}">{video_status}</span>
          </div>
          <div class="asset-row">
            <span class="project-slide-count">{project["script_count"] or "?"} slide</span>
            <span class="status-pill {script_class}">{script_status}</span>
          </div>
          <div class="asset-row row-video-slot">{video_slot}</div>
          <div class="asset-row">{project_asset_links(project)}</div>
          <div class="project-card-actions actions">
            <button class="select-btn secondary-btn" type="button" data-project="{name}">{icon_label("check", "Chọn")}</button>
            <a class="secondary-btn" href="/studio?project={quote(raw_name)}">{icon_label("wand", "Studio")}</a>
            <button class="secondary-btn save-project-template-btn" type="button" data-project="{name}">{icon_label("layout-template", "Lưu template")}</button>
            <a class="secondary-btn" href="{html.escape(str(project["url"]))}" target="_blank" rel="noreferrer">{icon_label("external", "Xem trước")}</a>
            <button class="copy-script-btn secondary-btn" type="button" data-project="{name}">{icon_label("copy", "Script")}</button>
            <button class="delete-output-btn secondary-btn{delete_disabled_class}" type="button" data-project="{name}"{delete_disabled}>{icon_label("trash", "Xóa output")}</button>
          </div>
        </div>
      </li>
    """


def render_projects_html(selected_project: str | None = None) -> bytes:
    projects = list_projects()
    selected_project = app_selected_project(projects, selected_project)
    ready_count = sum(1 for project in projects if project.get("video_url"))
    script_ready_count = sum(1 for project in projects if project.get("has_script"))
    options = project_option_html(projects, selected_project)
    cards = "\n".join(render_project_library_card(project, selected_project) for project in projects)
    if not cards:
        cards = '<li class="empty">Chưa có project nào trong source folder.</li>'
    main_html = f"""
      <div class="page-frame">
        <header class="page-head page-head-with-actions">
          <div>
            <p class="eyebrow">{html.escape(tx("Thư viện sản xuất", "Project library"))}</p>
            <h1>Project</h1>
            <p>{html.escape(tx("Chọn project, xem output, quay lại Studio hoặc Publish.", "Choose a project, review outputs, then return to Studio or Publish."))}</p>
          </div>
          <div class="page-actions">
            <button class="primary-btn" type="button" data-create-project-open>{icon_label("plus", tx("Tạo project", "Create project"))}</button>
          </div>
        </header>
        <div class="library-toolbar">
          <label class="field project-select-field">
            <span>{html.escape(tx("Project đang chọn", "Selected project"))}</span>
            <select id="projectSelect" {"disabled" if not projects else ""}>{options}</select>
            <small class="source-note source-path">Source: {html.escape(str(SLIDE_ROOT))}</small>
          </label>
          <div class="library-actions">
            <button class="secondary-btn" id="refreshProjects" type="button">{icon_label("refresh", tx("Làm mới", "Refresh"))}</button>
            <button class="secondary-btn" type="button" data-source-root-select>{icon_label("folder", tx("Đổi source", "Source"))}</button>
          </div>
        </div>
        <div class="status warn" id="renderStatus" hidden></div>
        <section class="stat-grid" aria-label="Project status summary">
          <div class="stat-card"><strong>{len(projects)}</strong><span>Tổng project</span></div>
          <div class="stat-card"><strong>{script_ready_count}</strong><span>Có script</span></div>
          <div class="stat-card"><strong>{ready_count}</strong><span>Có MP4</span></div>
        </section>
        <ol class="project-library-grid">{cards}</ol>
      </div>
    """
    return render_app_shell(title="Projects | Viro", active="projects", main_html=main_html, selected_project=selected_project, projects=projects)


def connection_provider_options(selected: str = "elevenlabs") -> str:
    selected = normalize_connection_provider(selected)
    return "\n".join(
        f'<option value="{html.escape(provider)}" {"selected" if provider == selected else ""}>{html.escape(label)}</option>'
        for provider, label in CONNECTION_PROVIDERS.items()
    )


def connection_kind_options(selected: str = "api_key") -> str:
    selected = normalize_connection_kind(selected)
    return "\n".join(
        f'<option value="{html.escape(kind)}" {"selected" if kind == selected else ""}>{html.escape(label)}</option>'
        for kind, label in CONNECTION_KINDS.items()
    )


def project_checks_html(projects: list[dict], selected_projects: list[str], field_name: str = "project_ids") -> str:
    selected = set(selected_projects)
    if not projects:
        return '<span class="empty">Chưa có project để gán.</span>'
    return "\n".join(
        f"""
        <label class="project-check">
          <input type="checkbox" name="{html.escape(field_name)}" value="{html.escape(project["name"])}" {"checked" if project["name"] in selected else ""} />
          <span>{html.escape(project["name"])}</span>
        </label>
        """
        for project in projects
    )


def project_multi_pick_html(projects: list[dict], selected_projects: list[str], field_name: str = "project_ids") -> str:
    selected = set(selected_projects)
    selected_count = sum(1 for project in projects if project["name"] in selected)
    label = f"{selected_count} project đã chọn" if selected_count else "Chưa gán project"
    total_label = f"{len(projects)} project"
    if not projects:
        return """
          <div class="connection-project-picker" data-project-picker>
            <button class="connection-project-picker-trigger" type="button" disabled>
              <span class="connection-project-picker-label">Chưa có project để gán</span>
            </button>
          </div>
        """

    return f"""
      <div class="connection-project-picker" data-project-picker>
        <button class="connection-project-picker-trigger" type="button" aria-expanded="false">
          <span class="connection-project-picker-label" data-project-picker-label>{html.escape(label)}</span>
          <span class="connection-project-picker-meta">{html.escape(total_label)} {ui_icon("chevron-down", "connection-project-picker-chevron")}</span>
        </button>
        <div class="connection-project-menu" data-project-picker-menu hidden>
          <div class="connection-project-menu-head">
            <input type="search" data-project-picker-search placeholder="Tìm project..." autocomplete="off" />
            <button class="connection-project-picker-action" type="button" data-project-picker-select-all>Chọn tất cả</button>
            <button class="connection-project-picker-action" type="button" data-project-picker-clear>Bỏ chọn</button>
          </div>
          <div class="connection-project-grid">{project_checks_html(projects, selected_projects, field_name)}</div>
          <div class="connection-project-picker-empty" data-project-picker-empty hidden>Không có project phù hợp.</div>
        </div>
      </div>
    """


def connection_status_class(connection: dict) -> str:
    status = str(connection.get("status") or "")
    if status == "ready":
        return "ok"
    if status == "available":
        return "warn"
    return "bad"


def connection_status_label(connection: dict) -> str:
    status = str(connection.get("status") or "")
    if status == "ready":
        return "Sẵn sàng"
    if status == "available":
        return "Có thể dùng"
    return "Cần cấu hình"


def render_connection_card(connection: dict, projects: list[dict]) -> str:
    connection_id = html.escape(str(connection["id"]))
    project_ids = connection.get("project_ids") or []
    project_count = len(project_ids)
    managed = bool(connection.get("managed"))
    status_class = connection_status_class(connection)
    source = html.escape(str(connection.get("source") or "registry"))
    account_id = html.escape(str(connection.get("account_id") or ""))
    endpoint_url = str(connection.get("endpoint_url") or "")
    endpoint_chip = ""
    if endpoint_url:
        parsed_endpoint = urlparse(endpoint_url)
        endpoint_label = f"{parsed_endpoint.netloc}{parsed_endpoint.path or '/'}"[:72]
        endpoint_chip = f'<span class="connection-chip connection-chip-url">{icon_label("external", endpoint_label)}</span>'
    secret_label = str(connection.get("secret_label") or connection.get("secret_mask") or "Chưa lưu secret")
    provider_initial = html.escape(str(connection.get("provider_label") or "?")[:2].upper())
    provider = str(connection.get("provider") or "")
    kind = str(connection.get("kind") or "")
    status = str(connection.get("status") or "")
    system_proxy = str(connection.get("id") or "") == "system:elevenlabs:proxy"
    if system_proxy:
        managed = False
    search_text = " ".join(
        [
            str(connection.get("name") or ""),
            str(connection.get("provider_label") or ""),
            str(connection.get("kind_label") or ""),
            str(connection.get("account_id") or ""),
            str(connection.get("endpoint_url") or ""),
            str(connection.get("secret_label") or ""),
            str(connection.get("notes") or ""),
            " ".join(project_ids),
        ]
    ).lower()
    data_attrs = {
        "data-connection-id": str(connection.get("id") or ""),
        "data-name": str(connection.get("name") or ""),
        "data-provider": provider,
        "data-provider-label": str(connection.get("provider_label") or ""),
        "data-kind": kind,
        "data-kind-label": str(connection.get("kind_label") or ""),
        "data-status": status,
        "data-account-id": str(connection.get("account_id") or ""),
        "data-endpoint-url": endpoint_url,
        "data-secret-label": str(connection.get("secret_label") or ""),
        "data-notes": str(connection.get("notes") or ""),
        "data-project-ids": json.dumps(project_ids, ensure_ascii=False),
        "data-secret-configured": "1" if connection.get("secret_configured") else "0",
        "data-search": search_text,
    }
    data_attr_html = " ".join(f'{key}="{html.escape(value, quote=True)}"' for key, value in data_attrs.items())
    studio_button = ""
    if (not managed or system_proxy) and ((provider == "elevenlabs" and kind == "api_key") or (provider == "apikeyrotator" and kind == "proxy_key")):
        studio_button = f'<button class="secondary-btn connection-studio-btn" type="button" data-connection-id="{connection_id}">{icon_label("wand", "Dùng Studio")}</button>'
    edit_button = "" if managed else f'<button class="secondary-btn connection-edit-btn" type="button" data-connection-id="{connection_id}">{icon_label("settings", "Sửa")}</button>'
    delete_button = "" if managed else f'<button class="secondary-btn connection-delete-btn" type="button" data-connection-id="{connection_id}">{icon_label("trash", "Xóa")}</button>'
    return f"""
      <li class="connection-card" {data_attr_html}>
        <div class="connection-card-head">
          <div class="connection-title">
            <span class="connection-mark">{provider_initial}</span>
            <div>
              <h2>{html.escape(str(connection.get("name") or "Untitled"))}</h2>
              <p>{html.escape(str(connection.get("provider_label")))} · {html.escape(str(connection.get("kind_label")))}{f" · {account_id}" if account_id else ""}</p>
            </div>
          </div>
          <span class="connection-chip {status_class}">{connection_status_label(connection)}</span>
        </div>
        <div class="connection-meta">
          <span class="connection-chip">{icon_label("key", secret_label)}</span>
          {endpoint_chip}
          <span class="connection-chip">{project_count} project</span>
          <span class="connection-chip">{source}</span>
        </div>
        <form class="connection-assign-form" data-connection-id="{connection_id}">
          <input type="hidden" name="id" value="{connection_id}" />
          <div class="connection-actions">
            <button class="secondary-btn connection-test-btn" type="button" data-connection-id="{connection_id}">{icon_label("refresh", "Test")}</button>
            {studio_button}
            {edit_button}
            {delete_button}
          </div>
          <div class="status warn connection-status" hidden></div>
        </form>
      </li>
    """


def render_connection_groups(connections: list[dict], projects: list[dict]) -> str:
    if not connections:
        return '<div class="connection-empty-state">Chưa có kết nối nào.</div>'
    groups: dict[tuple[str, str, str, str], list[dict]] = {}
    for connection in sorted(
        connections,
        key=lambda item: (
            str(item.get("provider_label") or ""),
            str(item.get("kind_label") or ""),
            str(item.get("name") or "").lower(),
        ),
    ):
        key = (
            str(connection.get("provider") or ""),
            str(connection.get("kind") or ""),
            str(connection.get("provider_label") or ""),
            str(connection.get("kind_label") or ""),
        )
        groups.setdefault(key, []).append(connection)

    parts = []
    for (provider, kind, provider_label, kind_label), items in groups.items():
        cards = "\n".join(render_connection_card(connection, projects) for connection in items)
        group_label = f"{provider_label} / {kind_label}"
        parts.append(
            f"""
            <section class="connection-group" data-provider="{html.escape(provider)}" data-kind="{html.escape(kind)}">
              <div class="connection-group-head">
                <div>
                  <span class="eyebrow">{html.escape(provider_label)}</span>
                  <h2>{html.escape(group_label)}</h2>
                </div>
                <span class="connection-group-count" data-group-count>{len(items)} account</span>
              </div>
              <ol class="connections-list">{cards}</ol>
            </section>
            """
        )
    return "\n".join(parts)


def render_connection_cards(connections: list[dict], projects: list[dict]) -> str:
    if not connections:
        return '<div class="connection-empty-state">Chưa có kết nối nào.</div>'
    sorted_connections = sorted(
        connections,
        key=lambda item: (
            str(item.get("provider_label") or ""),
            str(item.get("kind_label") or ""),
            str(item.get("name") or "").lower(),
        ),
    )
    cards = "\n".join(render_connection_card(connection, projects) for connection in sorted_connections)
    return f'<ol class="connections-list">{cards}</ol>'


def connection_provider_filter_options() -> str:
    return '<option value="">Tất cả provider</option>' + "\n".join(
        f'<option value="{html.escape(provider)}">{html.escape(label)}</option>'
        for provider, label in CONNECTION_PROVIDERS.items()
    )


def connection_kind_filter_options() -> str:
    return '<option value="">Tất cả loại</option>' + "\n".join(
        f'<option value="{html.escape(kind)}">{html.escape(label)}</option>'
        for kind, label in CONNECTION_KINDS.items()
    )


def render_connections_html(selected_project: str | None = None) -> bytes:
    projects = list_projects()
    selected_project = app_selected_project(projects, selected_project)
    data = connections_response()
    connections = data["connections"]
    cards_html = render_connection_cards(connections, projects)
    cards = cards_html
    if not cards:
        cards = '<li class="empty">Chưa có kết nối nào.</li>'
    selected_projects = [selected_project] if selected_project else []
    studio_auth = data.get("studio_auth", {})
    studio_auth_label = "Sẵn sàng" if studio_auth.get("studio_auth_ready") else "Chưa cấu hình"
    rotator_url = html.escape(str(studio_auth.get("builtin_rotator_base_url") or builtin_elevenlabs_rotator_base_url()))
    main_html = f"""
      <div class="page-frame">
        <header class="page-head">
          <p class="eyebrow">Account / Secret Hub</p>
          <h1>Tài khoản & Key</h1>
          <p>Quản lý account, API key, token và project đang dùng.</p>
        </header>
        <section class="stat-grid" aria-label="Connection summary">
          <div class="stat-card"><strong>{data["stats"]["connections"]}</strong><span>Tổng account</span></div>
          <div class="stat-card"><strong>{data["stats"]["ready_connections"]}</strong><span>Sẵn sàng</span></div>
          <div class="stat-card"><strong>{data["stats"]["used_connections"]}</strong><span>Đã gán project</span></div>
        </section>
        <section class="connection-studio-strip">
          <div>
            <span class="eyebrow">Studio auth</span>
            <strong>{html.escape(studio_auth_label)}</strong>
            <p>Viro Key Rotate: <code>{rotator_url}</code>. Đang có {data["stats"]["elevenlabs_keys"]} ElevenLabs key trong Secret Hub.</p>
          </div>
          <div class="connection-actions">
            <button class="primary-btn" id="openCreateConnection" type="button">{icon_label("plus", "Thêm account/key")}</button>
            <a class="secondary-btn" href="/studio">{icon_label("wand", "Mở Studio")}</a>
          </div>
        </section>
        <div class="connections-layout">
          <form class="setup-card connection-create-form">
            <h2>{icon_label("key", "Thêm account/key")}</h2>
            <div class="connection-form-grid">
              <label class="field full"><span>Tên</span><input name="name" type="text" maxlength="80" placeholder="ElevenLabs Production" required /></label>
              <label class="field"><span>Provider</span><select name="provider">{connection_provider_options()}</select></label>
              <label class="field"><span>Loại</span><select name="kind">{connection_kind_options()}</select></label>
              <label class="field"><span>Account/ID</span><input name="account_id" type="text" maxlength="160" placeholder="email / id" /></label>
              <label class="field"><span>Nhãn key</span><input name="secret_label" type="text" maxlength="120" placeholder="prod / backup" /></label>
              <label class="field full"><span>Endpoint URL</span><input name="endpoint_url" type="url" maxlength="300" placeholder="Để trống nếu provider có endpoint mặc định" /></label>
              <label class="field full"><span>Key/token</span><input name="secret_value" type="password" autocomplete="off" placeholder="Dán key hoặc token" /></label>
              <label class="field full connection-notes"><span>Ghi chú</span><textarea name="notes" rows="3" maxlength="500" placeholder="Quota, owner, mục đích..."></textarea></label>
              <div class="connection-projects">
                <span class="eyebrow">Dùng cho project</span>
                {project_multi_pick_html(projects, selected_projects)}
              </div>
            </div>
            <div class="form-actions">
              <button class="secondary-btn" id="testConnectionDraft" type="button">{icon_label("refresh", "Test key")}</button>
              <button class="secondary-btn" id="cancelCreateConnection" type="button">{icon_label("trash", "Hủy")}</button>
              <button class="primary-btn" type="submit">{icon_label("check", "Test & lưu")}</button>
            </div>
            <div class="status warn connection-status" hidden></div>
          </form>
          <section class="connections-browser">
            <div class="connections-toolbar">
              <label class="field connection-search-field">
                <span>Tìm account/key</span>
                <input id="connectionSearch" type="search" placeholder="Tên, provider, key label, project..." autocomplete="off" />
              </label>
              <label class="field">
                <span>Provider</span>
                <select id="connectionProviderFilter">{connection_provider_filter_options()}</select>
              </label>
              <label class="field">
                <span>Loại</span>
                <select id="connectionKindFilter">{connection_kind_filter_options()}</select>
              </label>
              <label class="field">
                <span>Trạng thái</span>
                <select id="connectionStatusFilter">
                  <option value="">Tất cả trạng thái</option>
                  <option value="ready">Sẵn sàng</option>
                  <option value="available">Có thể dùng</option>
                  <option value="needs_setup">Cần cấu hình</option>
                </select>
              </label>
            </div>
            <div class="connection-filter-summary" id="connectionFilterSummary">{len(connections)} account/key</div>
            <div class="connections-flat">{cards_html}</div>
            <div class="connection-empty-state" id="connectionFilterEmpty" hidden>Không có account/key phù hợp bộ lọc.</div>
          </section>
        </div>
        <div class="modal-backdrop" id="editConnectionModal" hidden>
          <div class="modal-card connection-modal" role="dialog" aria-modal="true" aria-labelledby="editConnectionTitle">
            <button class="modal-close" id="closeEditConnection" type="button" aria-label="Đóng">{ui_icon("close")}</button>
            <p class="eyebrow">Account / Secret Hub</p>
            <h3 id="editConnectionTitle">Sửa account/key</h3>
            <form id="editConnectionForm" class="connection-edit-form">
              <input type="hidden" name="id" />
              <div class="connection-form-grid">
                <label class="field full"><span>Tên</span><input name="name" type="text" maxlength="80" required /></label>
                <label class="field"><span>Provider</span><select name="provider">{connection_provider_options()}</select></label>
                <label class="field"><span>Loại</span><select name="kind">{connection_kind_options()}</select></label>
                <label class="field"><span>Account/ID</span><input name="account_id" type="text" maxlength="160" /></label>
                <label class="field"><span>Nhãn key</span><input name="secret_label" type="text" maxlength="120" /></label>
                <label class="field full"><span>Endpoint URL</span><input name="endpoint_url" type="url" maxlength="300" placeholder="Để trống nếu provider có endpoint mặc định" /></label>
                <label class="field full"><span>Key/token mới</span><input name="secret_value" type="password" autocomplete="off" placeholder="Để trống nếu giữ key cũ" /></label>
                <label class="field full connection-notes"><span>Ghi chú</span><textarea name="notes" rows="3" maxlength="500"></textarea></label>
                <div class="connection-projects">
                  <span class="eyebrow">Dùng cho project</span>
                  {project_multi_pick_html(projects, [], "project_ids")}
                </div>
              </div>
              <div class="form-actions">
                <button class="secondary-btn" id="testEditConnection" type="button">{icon_label("refresh", "Test key")}</button>
                <button class="secondary-btn" id="cancelEditConnection" type="button">{icon_label("trash", "Hủy")}</button>
                <button class="primary-btn" type="submit">{icon_label("check", "Test & lưu")}</button>
              </div>
              <div class="status warn connection-status" hidden></div>
            </form>
          </div>
        </div>
      </div>
    """
    return render_app_shell(title="Secret Hub | Viro", active="connections", main_html=main_html, selected_project=selected_project, projects=projects)


def render_platforms_html(selected_project: str | None = None) -> bytes:
    projects = list_projects()
    selected_project = app_selected_project(projects, selected_project)
    ready_count = sum(1 for project in projects if project.get("video_url"))
    options = project_option_html(projects, selected_project)
    selected_label = html.escape(selected_project or "Chưa chọn project")
    main_html = f"""
      <div class="page-frame">
        <header class="page-head">
          <p class="eyebrow">Phân phối</p>
          <h1>Nền tảng</h1>
          <p>Chọn project, kiểm tra metadata và upload YouTube/Facebook.</p>
        </header>
        <div class="publish-workspace">
          <aside class="publish-side">
            <section class="setup-card">
              <h2>{icon_label("folder", "Project")}</h2>
              <label class="field project-select-field">
                <span>Project nguồn</span>
                <select id="projectSelect" {"disabled" if not projects else ""}>{options}</select>
              </label>
              <div class="project-video-badge" id="projectVideoBadge">Đang kiểm tra output</div>
              <p class="source-note source-path">{ready_count} project đã render trong {html.escape(str(SLIDE_ROOT))}</p>
              <div class="form-actions">
                <a class="secondary-btn" id="previewLink" href="#" target="_blank" rel="noreferrer">{icon_label("external", "Xem trước")}</a>
                <a class="secondary-btn" id="existingVideoLink" href="#" target="_blank" rel="noreferrer" hidden>{icon_label("film", "Video")}</a>
                <button class="secondary-btn reveal-output-btn" id="revealOutput" type="button" hidden>{icon_label("hard-drive", "Mở output")}</button>
                <a class="secondary-btn" id="uploadCenterLink" href="/upload" target="_blank" rel="noreferrer" hidden>{icon_label("upload", "Publish")}</a>
              </div>
            </section>
            <section class="setup-card">
              <h2>{icon_label("check", "Checklist publish")}</h2>
              <ol class="state-list">
                <li>Project đang chọn đã có final_video.mp4.</li>
                <li>Load metadata hoặc tự gợi ý.</li>
                <li>Tài khoản nền tảng đã kết nối trước khi upload.</li>
              </ol>
            </section>
          </aside>
          <section class="publish-main">
            <div class="upload-empty" id="uploadEmpty" hidden>
              <strong id="uploadEmptyProject">{selected_label} chưa có final_video.mp4</strong>
              <span>Render project trong Studio trước khi publish.</span>
              <a class="secondary-btn" href="/studio?project={quote(selected_project)}">{icon_label("wand", "Mở Studio")}</a>
            </div>
            <div class="upload-panel" id="uploadPanel" hidden>
              <div class="platform-grid">
                <section class="platform-card platform-youtube">
                  <div class="platform-card-head">
                    <div class="platform-title"><span class="field-icon">YT</span><span>YouTube</span></div>
                    <a class="secondary-btn" href="/upload-guide/youtube" target="_blank" rel="noreferrer">{icon_label("book", "Hướng dẫn")}</a>
                  </div>
                  <div class="platform-account-list" id="youtubeAccountList" hidden></div>
                  <label class="field upload-field field-title">
                    <span class="field-label field-label-between">
                      <span class="field-label-main"><span>Tiêu đề</span></span>
                      <button class="copy-field-btn" data-copy-target="uploadTitle" data-copy-label="YouTube Title" type="button" aria-label="Copy YouTube Title" title="Copy YouTube Title">C</button>
                    </span>
                    <input id="uploadTitle" type="text" maxlength="100" placeholder="Tiêu đề YouTube" />
                  </label>
                  <label class="field upload-field field-description">
                    <span class="field-label field-label-between">
                      <span class="field-label-main"><span>Mô tả</span></span>
                      <button class="copy-field-btn" data-copy-target="youtubeDescription" data-copy-label="YouTube Description" type="button" aria-label="Copy YouTube Description" title="Copy YouTube Description">C</button>
                    </span>
                    <textarea id="youtubeDescription" rows="7" maxlength="5000" placeholder="Mô tả YouTube"></textarea>
                  </label>
                  <label class="field upload-field compact field-youtube">
                    <span>Chế độ YouTube</span>
                    <select id="youtubePrivacy">
                      <option value="private" selected>Private</option>
                      <option value="unlisted">Unlisted</option>
                      <option value="public">Public</option>
                    </select>
                  </label>
                  <div class="platform-actions">
                    <button class="upload-btn youtube" id="connectYoutube" type="button">{ui_icon("upload")}<span>Kết nối</span></button>
                    <button class="upload-btn youtube" id="uploadYoutube" type="button">{ui_icon("upload")}<span>Upload YouTube</span></button>
                  </div>
                </section>
                <section class="platform-card platform-facebook">
                  <div class="platform-card-head">
                    <div class="platform-title"><span class="field-icon">FB</span><span>Facebook Reels</span></div>
                    <a class="secondary-btn" href="/upload-guide/facebook" target="_blank" rel="noreferrer">{icon_label("book", "Hướng dẫn")}</a>
                  </div>
                  <div class="platform-account-list" id="facebookAccountList" hidden></div>
                  <label class="field upload-field field-description">
                    <span class="field-label field-label-between">
                      <span class="field-label-main"><span>Caption</span></span>
                      <button class="copy-field-btn" data-copy-target="facebookCaption" data-copy-label="Facebook Caption" type="button" aria-label="Copy Facebook Caption" title="Copy Facebook Caption">C</button>
                    </span>
                    <textarea id="facebookCaption" rows="7" maxlength="5000" placeholder="Caption Facebook Reels"></textarea>
                  </label>
                  <label class="field upload-field field-source-comment">
                    <span class="field-label field-label-between">
                      <span class="field-label-main"><span>Comment nguồn</span></span>
                      <button class="copy-field-btn" data-copy-target="facebookSourceComment" data-copy-label="Source comment" type="button" aria-label="Copy Source comment" title="Copy Source comment">C</button>
                    </span>
                    <input id="facebookSourceComment" type="text" maxlength="1000" placeholder="Source: https://..." />
                  </label>
                  <label class="field upload-field compact field-facebook">
                    <span>Trạng thái Reels</span>
                    <select id="facebookVideoState">
                      <option value="DRAFT" selected>Draft</option>
                      <option value="PUBLISHED">Publish now</option>
                    </select>
                  </label>
                  <div class="platform-actions">
                    <button class="upload-btn facebook secondary" id="openFacebookConfig" type="button"><span>+</span><span>Thêm Page</span></button>
                    <button class="upload-btn facebook" id="uploadFacebook" type="button"><span>FB</span><span>Upload Reels</span></button>
                  </div>
                </section>
              </div>
              <div class="final-upload-actions">
                <button class="upload-btn both" id="uploadBothPublic" type="button" disabled>{ui_icon("check")}<span>Upload public YouTube + Reels</span></button>
                <button class="upload-btn facebook secondary" id="commentFacebookSource" type="button" disabled>{ui_icon("copy")}<span>Comment nguồn</span></button>
              </div>
              <div class="upload-status" id="uploadStatus" hidden></div>
              <div class="upload-result" id="uploadResult" hidden></div>
            </div>
            <div class="modal-backdrop" id="facebookConfigModal" hidden>
              <div class="modal-card facebook-config-modal" role="dialog" aria-modal="true" aria-labelledby="facebookConfigTitle">
                <button class="modal-close" id="closeFacebookConfig" type="button" aria-label="Đóng">x</button>
                <p class="eyebrow">Facebook Page</p>
                <h3 id="facebookConfigTitle">Thêm Page</h3>
                <p class="modal-copy">Dán Page ID và Page access token đã extend. Token được lưu local và không hiển thị lại trên UI.</p>
                <label class="field facebook-config-field">
                  <span>Facebook Page ID</span>
                  <input id="facebookPageId" type="text" inputmode="numeric" autocomplete="off" placeholder="Page ID" />
                </label>
                <label class="field facebook-config-field">
                  <span>Page access token</span>
                  <input id="facebookPageAccessToken" type="password" autocomplete="off" placeholder="Page access token" />
                </label>
                <div class="config-state" id="facebookConfigState" hidden></div>
                <div class="modal-actions">
                  <button class="upload-btn secondary" id="cancelFacebookConfig" type="button">{ui_icon("trash")}<span>Hủy</span></button>
                  <button class="upload-btn facebook" id="saveFacebookConfig" type="button">{ui_icon("check")}<span>Lưu Page</span></button>
                </div>
              </div>
            </div>
          </section>
        </div>
      </div>
    """
    return render_app_shell(title="Platforms | Viro", active="platforms", main_html=main_html, selected_project=selected_project, projects=projects)


def render_studio_html(input_mode: str = "ai", selected_project: str | None = None, selected_template: str | None = None) -> bytes:
    projects = list_projects()
    templates = list_templates()
    selected_project = app_selected_project(projects, selected_project)
    selected_template = selected_template_name(templates, selected_template)
    normalized_mode = input_mode if input_mode in {"ai", "import", "voiceScript"} else "ai"
    active = "studio" if normalized_mode == "ai" else ("studio-import" if normalized_mode == "import" else "studio-manual")
    mode_title = {"ai": "Văn bản thành video", "import": "Bài viết thành video", "voiceScript": "Kịch bản thủ công"}[normalized_mode]
    placeholder = {
        "ai": "Nhập ý tưởng hoặc dàn bài bạn muốn chuyển thành video ngắn...",
        "import": "Dán URL, tóm tắt bài viết, hoặc nội dung nguồn cần biến thành video...",
        "voiceScript": "Nhập voiceover. Mỗi dòng trống sẽ tách scene; các dòng ngắn liên tiếp có thể gộp thành một lời thoại.",
    }[normalized_mode]
    options = project_option_html(projects, selected_project)
    template_options = template_options_html(templates, selected_template)
    selected_project_data = next((project for project in projects if project["name"] == selected_project), {})
    selected_voice_connection_id = str(selected_project_data.get("voice_connection_id") or "")
    voice_options = studio_voice_connection_options_html(selected_voice_connection_id)
    ai_options = studio_ai_connection_options_html()
    rows = compact_project_rows(projects, selected_project)
    mode_tabs = f"""
      <div class="mode-tabs" aria-label="Studio input modes">
        <a class="{'active' if normalized_mode == 'ai' else ''}" data-input-mode="ai" href="/studio?inputMode=ai&template={quote(selected_template)}&project={quote(selected_project)}">{icon_label("sparkles", "AI Script")}</a>
        <a class="{'active' if normalized_mode == 'import' else ''}" data-input-mode="import" href="/studio?inputMode=import&template={quote(selected_template)}&project={quote(selected_project)}">{icon_label("file-up", "Bài viết")}</a>
        <a class="{'active' if normalized_mode == 'voiceScript' else ''}" data-input-mode="voiceScript" href="/studio?inputMode=voiceScript&template={quote(selected_template)}&project={quote(selected_project)}">{icon_label("mic", "Thủ công")}</a>
      </div>
    """
    main_html = f"""
      <div class="studio-layout">
        <section class="studio-main">
          <div class="studio-top">
            <header class="page-head">
              <h1>{html.escape(mode_title)}</h1>
              <div class="studio-steps-line"><span>Prompt</span><span>›</span><span>Storyboard</span><span>›</span><span>Preview voice</span><span>›</span><span>Generate video</span></div>
              {mode_tabs}
            </header>
          </div>
          <div class="editor-panel">
            <textarea class="script-editor" id="studioInput" placeholder="{html.escape(placeholder)}"></textarea>
            <div class="editor-bottom">
              <button class="secondary-btn" id="studioMediaButton" type="button">{icon_label("image", "Gắn media")}</button>
              <button class="primary-btn" id="createStoryboard" type="button">{icon_label("layers", "Tạo storyboard")}</button>
            </div>
          </div>
          <section class="slide-composer" id="slideComposer" data-project="{html.escape(selected_project)}">
            <div class="slide-composer-head">
              <div>
                <p class="eyebrow">Project slides</p>
                <h2>Slide editor</h2>
                <p class="source-note">Sửa text, hiệu ứng và media theo từng slide của project đang chọn.</p>
              </div>
              <div class="form-actions">
                <button class="secondary-btn" id="reloadSlideComposer" type="button">{icon_label("refresh", "Load")}</button>
                <button class="primary-btn" id="saveSlideComposer" type="button">{icon_label("check", "Lưu slide")}</button>
              </div>
            </div>
            <div class="status warn slide-composer-status" id="slideComposerStatus" hidden></div>
            <div class="slide-composer-grid">
              <ol class="studio-slide-list" id="studioSlideList"></ol>
              <div class="studio-slide-preview-shell">
                <iframe id="studioSlidePreview" src="/slide/{quote(selected_project)}/?studioPreview=1" title="Slide preview" loading="lazy"></iframe>
              </div>
              <form class="studio-slide-editor" id="studioSlideEditor">
                <label class="field">
                  <span>Slide text</span>
                  <textarea id="studioSlideText" rows="6" placeholder="Voice line của slide đang chọn"></textarea>
                </label>
                <label class="field">
                  <span>On-screen text</span>
                  <input id="studioSlideScreenText" type="text" placeholder="Text ngắn hiển thị trên slide" />
                </label>
                <label class="field">
                  <span>Visual / media direction</span>
                  <textarea id="studioSlideVisualDirection" rows="4" placeholder="Gợi ý visual, media, framing cho slide này"></textarea>
                </label>
                <label class="field">
                  <span>Duration estimate</span>
                  <input id="studioSlideDuration" type="number" min="1" max="60" step="0.5" placeholder="Giây" />
                </label>
                <div class="config-row">
                  <label class="field">
                    <span>Chuyển slide</span>
                    <select id="studioSlideTransition">
                      <option value="minimal">Minimal</option>
                      <option value="rise">Rise</option>
                      <option value="sweep">Sweep</option>
                      <option value="bass">Bass</option>
                      <option value="chime">Chime</option>
                      <option value="gong">Gong</option>
                      <option value="dramatic">Dramatic</option>
                      <option value="chord">Chord</option>
                      <option value="alarm">Alarm</option>
                      <option value="retro">Retro</option>
                    </select>
                  </label>
                  <label class="field">
                    <span>Hiện element</span>
                    <select id="studioSlideReveal">
                      <option value="ping">Ping</option>
                      <option value="pop">Pop</option>
                      <option value="chime">Chime</option>
                      <option value="click">Click</option>
                      <option value="bubble">Bubble</option>
                      <option value="sparkle">Sparkle</option>
                      <option value="blip">Blip</option>
                      <option value="tick">Tick</option>
                      <option value="bell">Bell</option>
                    </select>
                  </label>
                </div>
                <div class="field">
                  <span>Media slide</span>
                  <input class="hidden-file-input" id="studioSlideMediaFile" type="file" accept="image/*,video/mp4,video/webm,video/quicktime" />
                  <label class="secondary-btn" for="studioSlideMediaFile">{icon_label("upload", "Chọn ảnh/video")}</label>
                  <div class="slide-media-current" id="studioSlideMediaCurrent">Chưa gắn media.</div>
                </div>
                <div class="config-row">
                  <label class="field">
                    <span>Fit</span>
                    <select id="studioSlideMediaFit">
                      <option value="cover">Cover</option>
                      <option value="contain">Contain</option>
                      <option value="fill">Fill</option>
                    </select>
                  </label>
                  <label class="field">
                    <span>Vị trí</span>
                    <select id="studioSlideMediaPosition">
                      <option value="center">Center</option>
                      <option value="top">Top</option>
                      <option value="bottom">Bottom</option>
                      <option value="left">Left</option>
                      <option value="right">Right</option>
                    </select>
                  </label>
                </div>
                <button class="secondary-btn" id="clearSlideMedia" type="button">{icon_label("trash", "Gỡ media")}</button>
              </form>
            </div>
          </section>
        </section>
        <aside class="setup-panel">
          <div class="setup-card">
            <h2>{icon_label("layout-template", "Template")}</h2>
            <label class="field"><span>Template tạo video</span><select id="templateSelect" {"disabled" if not templates else ""}>{template_options}</select></label>
            <div class="mini-flow" aria-label="Template render flow"><span>Template</span><span>Storyboard</span><span>Slide</span><span>MP4</span></div>
            <div class="form-actions">
              <a class="secondary-btn" id="templatePreviewLink" href="/template/{quote(selected_template)}/" target="_blank" rel="noreferrer">{icon_label("external", "Xem trước")}</a>
              <a class="secondary-btn" href="/templates">{icon_label("layout-template", "Chọn template")}</a>
            </div>
          </div>
          <div class="setup-card">
            <h2>{icon_label("folder", "Project")}</h2>
            <label class="field"><span>{html.escape(tx("Project nguồn", "Source project"))}</span><select id="projectSelect" {"disabled" if not projects else ""}>{options}</select></label>
            <div class="project-video-badge" id="projectVideoBadge">Đang kiểm tra output</div>
            <div class="form-actions">
              <button class="primary-btn" type="button" data-create-project-open data-template="{html.escape(selected_template)}" data-input-mode="{html.escape(normalized_mode)}">{icon_label("plus", tx("Tạo project", "Create project"))}</button>
              <a class="secondary-btn" id="previewLink" href="#" target="_blank" rel="noreferrer">{icon_label("external", "Xem trước")}</a>
              <a class="secondary-btn" id="existingVideoLink" href="#" target="_blank" rel="noreferrer" hidden>{icon_label("film", "Video")}</a>
              <a class="secondary-btn" id="uploadCenterLink" href="/upload" target="_blank" rel="noreferrer" hidden>{icon_label("upload", "Publish")}</a>
            </div>
          </div>
          <div class="setup-card">
            <h2>{icon_label("sparkles", "AI storyboard")}</h2>
            <label class="field"><span>AI account</span><select id="aiCredentialSelect">{ai_options}</select></label>
            <label class="field"><span>Model</span><input id="aiStoryboardModel" type="text" value="gpt-4.1-mini" /></label>
            <span class="config-state" id="aiStoryboardState">Chọn OpenAI key để tạo storyboard bằng AI thật.</span>
            <div class="form-actions" style="margin-top:10px">
              <a class="secondary-btn" href="/connections">{icon_label("key", "Thêm OpenAI key")}</a>
            </div>
          </div>
          <div class="setup-card">
            <h2>{icon_label("settings", "Thiết lập video")}</h2>
            <div class="segmented" aria-label="Video length">
              <button type="button" data-duration="auto">Auto<br><small>AI chọn</small></button>
              <button type="button" class="active" data-duration="short">Ngắn<br><small>~60s</small></button>
              <button type="button" data-duration="medium">Vừa<br><small>~90s</small></button>
              <button type="button" data-duration="long">Dài<br><small>~120s</small></button>
            </div>
            <div class="setting-summary" id="videoDurationSummary">Mục tiêu: ngắn ~60s</div>
            <div class="config-row" style="margin-top:12px">
              <label class="field"><span>Kích thước render</span><select id="renderSize"><option value="1080x1920" selected>1080 x 1920</option><option value="720x1280">720 x 1280</option></select></label>
              <label class="field"><span>Tốc độ</span><input id="renderSpeed" type="number" min="0.5" max="2" step="0.05" value="1.1" /></label>
            </div>
            <label class="field"><span>Platform</span><select id="targetPlatform"><option value="shorts" selected>YouTube Shorts</option><option value="tiktok">TikTok</option><option value="reels">Instagram Reels</option><option value="general">General 9:16</option></select></label>
            <div class="speed-presets" aria-label="Speed presets">
              <button class="secondary-btn speed-preset active" type="button" data-speed="1.1">1.1</button>
              <button class="secondary-btn speed-preset" type="button" data-speed="1.15">1.15</button>
              <button class="secondary-btn speed-preset" type="button" data-speed="1.2">1.2</button>
              <button class="secondary-btn speed-preset" type="button" data-speed="1.25">1.25</button>
            </div>
          </div>
          <div class="setup-card">
            <h2>{icon_label("mic", "Giọng đọc")}</h2>
            <div class="tabs">
              <button class="active" data-engine="elevenlabs" type="button">ElevenLabs</button>
              <button data-engine="edgetts" type="button">Edge TTS</button>
            </div>
            <div data-pane="elevenlabs">
              <div class="mode-toggle" aria-label="ElevenLabs mode">
                <label><input type="radio" name="elevenMode" value="upload" /> Tải file</label>
                <label><input type="radio" name="elevenMode" value="tts" checked /> API</label>
              </div>
              <div data-eleven-mode-pane="upload">
                <div class="field"><span>File voiceover đầy đủ</span><input class="hidden-file-input" id="elevenFile" type="file" accept="audio/*,.mp3,.wav,.m4a,.aac,.ogg" /><label class="secondary-btn" for="elevenFile">{icon_label("upload", "Chọn file")}</label></div>
                <span class="file-picker-name" id="elevenFileName">Chưa chọn file</span>
              </div>
              <div data-eleven-mode-pane="tts" hidden>
                <div class="api-key-panel" id="elevenApiKeyPanel">
                  <label class="field"><span>Account/API</span><select id="elevenCredentialSelect">{voice_options}</select></label>
                  <label class="field" id="elevenManualKeyField"><span>ElevenLabs API key mới</span><input id="elevenApiKey" type="password" autocomplete="off" placeholder="Dán key mới để lưu vào Secret Hub/config" /></label>
                  <span class="config-state" id="elevenApiKeyState">Chưa kiểm tra API key</span>
                </div>
              </div>
            </div>
            <div data-pane="edgetts" hidden>
              <label class="field"><span>Edge voice</span><input id="edgeVoice" type="text" value="vi-VN-HoaiMyNeural" /></label>
            </div>
          </div>
          <details class="setup-card advanced-settings" id="advancedSettings">
            <summary><span>Nâng cao</span><span id="advancedStateLabel">Đang ẩn</span></summary>
            <div class="advanced-body">
              <div data-advanced-engine="elevenlabs">
                <div data-eleven-mode-pane="tts" hidden>
                  <label class="field"><span>Voice ID</span><input id="elevenVoice" type="text" placeholder="JBFqnCBsd6RMkjVDRZzb" /></label>
                  <label class="field"><span>APIKeyRotator proxy base URL</span><input id="elevenProxyBaseUrl" type="url" placeholder="http://localhost:8000/proxy/elevenlabs" /></label>
                  <label class="field"><span>APIKeyRotator X-Proxy-Key</span><input id="elevenProxyKey" type="password" autocomplete="off" placeholder="GLOBAL_PROXY_KEYS value" /></label>
                  <span class="config-state" id="elevenProxyKeyState">Chưa kiểm tra proxy</span>
                  <label class="check"><input id="elevenForce" type="checkbox" /> Tạo lại audio cache</label>
                </div>
              </div>
              <div data-advanced-engine="edgetts" hidden>
                <label class="check"><input id="edgeForce" type="checkbox" /> Tạo lại audio cache</label>
                <label class="check"><input id="edgePerSlide" type="checkbox" /> Tạo từng slide</label>
              </div>
            </div>
          </details>
          <div class="setup-card">
            <h2>Output</h2>
            <div class="status warn" id="renderStatus" hidden></div>
            <button class="secondary-btn" id="previewVoice" type="button" {"disabled" if not projects else ""}>{icon_label("mic", "Preview voice")}</button>
            <div class="voice-preview-panel" id="voicePreviewPanel" hidden>
              <audio id="voicePreviewAudio" controls preload="none"></audio>
              <span class="config-state" id="voicePreviewState">Chưa có voice preview.</span>
            </div>
            <button class="start" id="startRender" type="button" {"disabled" if not projects else ""}>{icon_label("play", "Generate video")}</button>
            <button class="secondary-btn stop-render-btn" id="stopRender" type="button" hidden>{icon_label("trash", "Dừng render")}</button>
            <div class="form-actions">
              <a class="secondary-btn" id="videoLink" href="#" target="_blank" rel="noreferrer" hidden>{icon_label("film", "Mở video")}</a>
              <button class="secondary-btn reveal-output-btn" id="revealOutput" type="button" hidden>{icon_label("hard-drive", "Mở output")}</button>
            </div>
            <div class="render-state" id="renderState" hidden>
              <div class="state-head"><span class="state-dot"></span><strong id="stateTitle">Trạng thái render</strong></div>
              <ol class="state-list" id="stateList"></ol>
            </div>
            <div class="upload-empty" id="uploadEmpty" hidden><strong id="uploadEmptyProject">Project chưa có final_video.mp4</strong></div>
          </div>
          <div class="setup-card">
            <h2>Project trong source</h2>
            <ol class="project-list-compact">{rows}</ol>
          </div>
        </aside>
      </div>
      <div class="modal-backdrop" id="storyboardModal" hidden>
        <div class="modal-card storyboard-modal-card" role="dialog" aria-modal="true" aria-labelledby="storyboardTitle">
          <div class="storyboard-head">
            <div>
              <p class="eyebrow">Story editor</p>
              <h3 id="storyboardTitle">Storyboard</h3>
              <p>Kiểm tra scene, load script và tiếp tục render.</p>
            </div>
            <button class="modal-close" id="closeStoryboardModal" type="button" aria-label="Đóng">x</button>
          </div>
          <div class="storyboard-tabs" aria-label="Storyboard tabs">
            <button class="active" id="storyboardTabScenes" type="button" data-storyboard-tab="scenes">Scene <span id="storyboardSceneCount">0</span></button>
            <button id="storyboardTabMedia" type="button" data-storyboard-tab="media">Media</button>
          </div>
          <div class="storyboard-status" id="storyboardStatus">Chưa load storyboard.</div>
          <section data-storyboard-panel="scenes">
            <ol class="storyboard-scene-list" id="storyboardSceneList"></ol>
          </section>
          <section data-storyboard-panel="media" hidden>
            <div class="storyboard-media-grid" id="storyboardMediaGrid"></div>
          </section>
          <div class="modal-actions">
            <button class="secondary-btn" id="storyboardLoadScript" type="button">Load script project</button>
            <button class="secondary-btn" id="storyboardSaveScript" type="button">Lưu script vào project</button>
            <button class="primary-btn" id="storyboardContinue" type="button">Tiếp tục render</button>
          </div>
        </div>
      </div>
    """
    return render_app_shell(title="Studio | Viro", active=active, main_html=main_html, selected_project=selected_project, projects=projects)


def render_home_html(selected_project: str | None = None) -> bytes:
    projects = list_projects()
    project_names = {project["name"] for project in projects}
    selected_project = selected_project if selected_project in project_names else (projects[0]["name"] if projects else "")
    selected_project_data = next((project for project in projects if project["name"] == selected_project), {})
    voice_options = studio_voice_connection_options_html(str(selected_project_data.get("voice_connection_id") or ""))

    rows = []
    for project in projects:
        name = html.escape(project["name"])
        href = html.escape(project["url"])
        has_video = bool(project["video_url"])
        can_delete_output = bool(project["has_output"])
        video_link = (
            f'<a class="small-link icon-btn" href="{html.escape(project["video_url"])}" target="_blank" rel="noreferrer"><span class="btn-icon">▶</span><span>Mở</span></a>'
            if has_video
            else '<span class="icon-btn disabled"><span class="btn-icon">▶</span><span>Mở</span></span>'
        )
        delete_disabled = "" if can_delete_output else " disabled"
        delete_disabled_class = "" if can_delete_output else " disabled"
        status = "Sẵn sàng" if project["has_script"] else "Thiếu script"
        status_class = "ok" if project["has_script"] else "bad"
        selected_class = " selected" if project["name"] == selected_project else ""
        rows.append(
            f"""
            <li class="project-row{selected_class}" data-project="{name}">
              <div class="project-main">
                <span class="project-name">{name}</span>
                <span class="project-slide-count">{project["script_count"] or "?"} slide</span>
              </div>
              <span class="status-pill {status_class}">{status}</span>
              <div class="actions">
                <button class="select-btn icon-btn" type="button" data-project="{name}"><span class="btn-icon">✓</span><span>Chọn</span></button>
                <a class="small-link icon-btn" href="{href}" target="_blank" rel="noreferrer"><span class="btn-icon">↗</span><span>Xem</span></a>
                <button class="copy-script-btn icon-btn" type="button" data-project="{name}" title="Copy script-90s.txt"><span class="btn-icon">⧉</span><span>Script</span></button>
                <span class="row-video-slot">{video_link}</span>
                <button class="delete-output-btn icon-btn{delete_disabled_class}" type="button" data-project="{name}"{delete_disabled}><span class="btn-icon">×</span><span>Xoá</span></button>
              </div>
            </li>
            """
        )

    body = "\n".join(rows) or '<li class="empty">Chưa có dự án slide nào.</li>'
    return render_page_shell(
        title="Video Studio",
        body=f"""
  <header class="dashboard-header">
    <div class="brand-lockup">
      <img src="/web/viro-icon.svg" alt="Viro" class="brand-mark" />
      <div>
        <h1>Viro Studio</h1>
        <p>Tạo, render, kiểm tra và đăng video dọc trong một luồng.</p>
      </div>
    </div>
    <div class="header-tools">
      <a class="refresh-btn icon-btn guide-header-btn" href="/elevenlabs-guide#manual" target="_blank" rel="noreferrer"><span class="btn-icon">?</span><span>Audio guide</span></a>
      <a class="refresh-btn icon-btn guide-header-btn" href="/elevenlabs-guide#api-credit" target="_blank" rel="noreferrer"><span class="btn-icon">?</span><span>API guide</span></a>
      <button class="refresh-btn icon-btn theme-toggle" type="button" data-theme-toggle><span class="btn-icon">☾</span><span>Theme</span></button>
      <button class="refresh-btn icon-btn" type="button" data-source-root-select><span class="btn-icon">⌕</span><span>Source</span></button>
      <a class="refresh-btn icon-btn" href="/upload" target="_blank" rel="noreferrer"><span class="btn-icon">↑</span><span>Publish</span></a>
      <button class="refresh-btn icon-btn" id="refreshProjects" type="button"><span class="btn-icon">↻</span><span>Refresh</span></button>
      <div class="header-stat"><strong>{len(projects)}</strong><span>projects</span></div>
    </div>
  </header>

  <main class="dashboard-shell">
    <section class="render-machine panel">
      <div class="render-heading">
        <p class="kicker">Quy trình sản xuất</p>
        <h2><span class="render-lead-icon">✦</span><span>Studio render</span></h2>
      </div>

      <div class="selected-box">
        <div>
          <strong id="selectedName">Chưa chọn dự án</strong>
          <span id="selectedBadge">Chọn project trong Library</span>
        </div>
      </div>

      <div class="studio-steps" aria-label="Studio workflow">
        <div class="studio-step active"><span>01</span><strong>Chọn</strong><small>Project</small></div>
        <div class="studio-step"><span>02</span><strong>Voice</strong><small>Upload / TTS</small></div>
        <div class="studio-step"><span>03</span><strong>Render</strong><small>MP4</small></div>
        <div class="studio-step"><span>04</span><strong>Publish</strong><small>YouTube / Reels</small></div>
      </div>
      <div class="status warn" id="renderStatus" hidden></div>

      <div class="tabs">
        <button class="tab active" data-engine="elevenlabs" type="button"><span class="tab-icon">🎙</span><span>ElevenLabs</span></button>
        <button class="tab" data-engine="edgetts" type="button"><span class="tab-icon">⚡</span><span>Edge TTS</span></button>
      </div>

      <div data-pane="elevenlabs">
        <div class="mode-toggle" aria-label="Chọn kiểu ElevenLabs">
          <label><input type="radio" name="elevenMode" value="upload" /> Tải file</label>
          <label><input type="radio" name="elevenMode" value="tts" checked /> API</label>
        </div>
        <div data-eleven-mode-pane="upload">
          <label class="field primary-field">
            <span class="field-label"><span class="field-icon">↑</span><span>File voiceover đầy đủ</span></span>
            <span class="file-picker">
              <input id="elevenFile" type="file" accept="audio/*,.mp3,.wav,.m4a,.aac,.ogg" />
              <span class="file-picker-button">Chọn file</span>
              <span class="file-picker-name" id="elevenFileName">Chưa chọn file</span>
            </span>
          </label>
        </div>
        <div data-eleven-mode-pane="tts" hidden>
          <div class="api-key-panel" id="elevenApiKeyPanel">
            <label class="field api-key-field">
              <span class="field-label"><span class="field-icon">🔑</span><span>Account/API</span></span>
              <select id="elevenCredentialSelect">{voice_options}</select>
            </label>
            <label class="field api-key-field">
              <span class="field-label"><span class="field-icon">🔑</span><span>ElevenLabs API key mới</span></span>
              <input id="elevenApiKey" type="password" autocomplete="off" placeholder="Dán API key rồi lưu vào config/tts.json" />
            </label>
            <div class="eleven-actions api-key-actions">
              <span class="config-state" id="elevenApiKeyState">Chưa kiểm tra API key</span>
            </div>
          </div>
        </div>
      </div>

      <div data-pane="edgetts" hidden>
      </div>

      <div class="form-actions render-primary-actions">
        <button class="start" id="startRender" type="button" {"disabled" if not projects else ""}><span class="btn-icon">▶</span><span>Bắt đầu render</span></button>
        <button class="icon-btn stop-render-btn" id="stopRender" type="button" hidden><span class="btn-icon">■</span><span>Dừng render</span></button>
        <a class="small-link" id="videoLink" href="#" target="_blank" rel="noreferrer" hidden>Mở video cuối</a>
        <button class="icon-btn reveal-output-btn" id="revealOutput" type="button" hidden><span class="btn-icon">⌕</span><span>Mở trong Finder</span></button>
      </div>

      <div class="render-state" id="renderState" hidden>
        <div class="state-head">
          <span class="state-dot"></span>
          <strong id="stateTitle">Trạng thái render</strong>
        </div>
        <ol class="state-list" id="stateList"></ol>
      </div>

      <details class="advanced-settings" id="advancedSettings">
        <summary>
          <span class="advanced-summary-main"><span class="btn-icon">⚙</span><span>Cài đặt nâng cao</span></span>
          <span class="advanced-summary-state" id="advancedStateLabel">Đang ẩn</span>
        </summary>
        <div class="advanced-body">
          <div data-advanced-engine="elevenlabs">
            <div data-eleven-mode-pane="tts" hidden>
              <label class="field">
                <span class="field-label"><span class="field-icon">♪</span><span>Voice ID, mặc định giọng “Nhật”</span></span>
                <input id="elevenVoice" type="text" placeholder="JBFqnCBsd6RMkjVDRZzb" />
              </label>
              <label class="field">
                <span class="field-label"><span class="field-icon">-></span><span>APIKeyRotator proxy base URL</span></span>
                <input id="elevenProxyBaseUrl" type="url" placeholder="http://localhost:8000/proxy/elevenlabs" />
              </label>
              <label class="field">
                <span class="field-label"><span class="field-icon">#</span><span>APIKeyRotator X-Proxy-Key</span></span>
                <input id="elevenProxyKey" type="password" autocomplete="off" placeholder="GLOBAL_PROXY_KEYS value" />
              </label>
              <div class="eleven-actions api-key-actions">
                <span class="config-state" id="elevenProxyKeyState">Chưa kiểm tra APIKeyRotator proxy</span>
              </div>
              <label class="check">
                <input id="elevenForce" type="checkbox" />
                Tạo lại audio cache
              </label>
            </div>
          </div>

          <div data-advanced-engine="edgetts" hidden>
            <label class="field">
              <span class="field-label"><span class="field-icon">◆</span><span>Giọng Edge TTS</span></span>
              <input id="edgeVoice" type="text" value="vi-VN-HoaiMyNeural" />
            </label>
            <label class="check">
              <input id="edgeForce" type="checkbox" />
              Tạo lại audio cache
            </label>
            <label class="check">
              <input id="edgePerSlide" type="checkbox" />
              Tạo từng slide
            </label>
          </div>

          <label class="field">
            <span class="field-label"><span class="field-icon">↯</span><span>Tốc độ audio</span></span>
            <input id="renderSpeed" type="number" min="0.5" max="2" step="0.05" value="1.1" />
          </label>
          <div class="speed-presets" aria-label="Chọn nhanh tốc độ audio">
            <button class="speed-preset active" type="button" data-speed="1.1">1.1</button>
            <button class="speed-preset" type="button" data-speed="1.15">1.15</button>
            <button class="speed-preset" type="button" data-speed="1.2">1.2</button>
            <button class="speed-preset" type="button" data-speed="1.25">1.25</button>
          </div>
          <label class="field">
            <span class="field-label"><span class="field-icon">▣</span><span>Độ phân giải render</span></span>
            <select id="renderSize">
              <option value="1080x1920" selected>1080 x 1920 (mặc định, nét hơn)</option>
              <option value="720x1280">720 x 1280 (nhẹ hơn, đỡ tải máy)</option>
            </select>
          </label>
        </div>
      </details>

    </section>

    <aside class="slide-list panel">
      <div class="panel-head">
        <div>
          <p class="kicker">Library</p>
          <h2>Projects trong source folder</h2>
        </div>
      </div>
      <div class="list-head">
        <span>Project</span>
        <span>Tình trạng</span>
        <span>Hành động</span>
      </div>
      <ol class="project-list" id="projectList">
        {body}
      </ol>
    </aside>
  </main>
""",
        extra_style="""
    body {
      height: 100vh;
      overflow: hidden;
      display: flex;
      flex-direction: column;
      padding: 28px min(4vw, 48px);
    }
    .dashboard-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 24px;
      width: 100%;
      max-width: 1760px;
      flex: 0 0 auto;
      margin: 0 auto 22px;
    }
    .brand-lockup {
      display: flex;
      align-items: center;
      gap: 18px;
    }
    .brand-mark {
      width: 78px;
      height: 78px;
      border-radius: 18px;
      object-fit: cover;
      box-shadow: 0 14px 34px rgba(0, 0, 0, 0.36);
    }
    .dashboard-header h1 {
      margin: 0;
      font-size: clamp(32px, 4vw, 44px);
      line-height: 1;
      letter-spacing: 0;
    }
    .brand-lockup p {
      margin: 10px 0 0;
      color: var(--muted);
      font-size: clamp(18px, 2.4vw, 26px);
      line-height: 1;
      letter-spacing: 0;
    }
    .kicker { margin: 0 0 6px; color: var(--muted); font-size: 11px; font-weight: 900; letter-spacing: 0.16em; text-transform: uppercase; }
    .header-tools {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      flex: 1 1 auto;
      min-width: 0;
      justify-content: flex-end;
    }
    .guide-header-btn {
      white-space: nowrap;
      padding-inline: 14px;
    }
    .header-stat {
      min-width: 116px;
      border: 1px solid var(--line);
      border-radius: 22px;
      padding: 14px 16px;
      background: var(--surface);
      text-align: center;
    }
    .header-stat strong { display: block; color: var(--accent); font-size: 32px; line-height: 1; }
    .header-stat span { color: var(--muted); font-size: 12px; font-weight: 900; text-transform: uppercase; letter-spacing: 0.12em; }
    .dashboard-shell {
      display: grid;
      grid-template-columns: minmax(360px, 520px) minmax(480px, 1fr);
      gap: 18px;
      width: 100%;
      max-width: 1360px;
      flex: 1 1 auto;
      min-height: 0;
      overflow: hidden;
      margin: 0 auto;
      align-items: stretch;
    }
    .panel {
      border: 1px solid var(--line);
      border-radius: 24px;
      background: var(--panel);
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
    }
    .render-machine {
      width: 100%;
      justify-self: stretch;
      align-self: start;
      height: auto;
      max-height: 100%;
      min-height: 0;
      padding: 16px 16px 24px;
      position: static;
      overflow-y: auto;
      overscroll-behavior: contain;
      scroll-padding-bottom: 40px;
      -webkit-overflow-scrolling: touch;
    }
    .slide-list {
      height: 100%;
      min-height: 0;
      padding: 16px;
      overflow-y: auto;
      overscroll-behavior: contain;
      -webkit-overflow-scrolling: touch;
    }
    .panel-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; margin-bottom: 12px; }
    h2 { margin: 0; font-size: 22px; letter-spacing: 0; }
    .render-heading { text-align: center; margin: 0 0 14px; }
    .render-heading h2 {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 9px;
    }
    .render-heading .kicker { margin-bottom: 3px; }
    .render-lead-icon,
    .tab-icon,
    .field-icon {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 22px;
      height: 22px;
      flex: 0 0 auto;
      border-radius: 999px;
      color: #fff8ed;
      background: linear-gradient(135deg, #f2b261, #d88435);
      box-shadow: 0 7px 18px rgba(242, 178, 101, 0.22);
      font-size: 12px;
      line-height: 1;
      text-shadow: none;
    }
    .render-lead-icon {
      width: 28px;
      height: 28px;
      color: #fff8ed;
      background: linear-gradient(135deg, #a78bfa, #7c3aed);
      box-shadow: 0 9px 24px rgba(124, 58, 237, 0.26);
      font-size: 14px;
    }
    .render-machine .field,
    .render-machine .tabs,
    .render-machine .check,
    .render-machine .mode-toggle,
    .render-machine .eleven-actions,
    .render-machine .speed-presets,
    .render-machine .form-actions {
      max-width: 440px;
      margin-left: auto;
      margin-right: auto;
    }
    .field { display: grid; gap: 7px; margin: 10px 0; }
    .field span { color: var(--muted); font-size: 12px; font-weight: 900; }
    .field-label { display: inline-flex; align-items: center; gap: 7px; }
    .mode-toggle {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 8px;
      margin: 10px auto 12px;
    }
    .mode-toggle label {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 7px;
      min-height: 38px;
      border: 1px solid var(--control-line);
      border-radius: 13px;
      color: var(--text-soft);
      background: var(--surface);
      font-size: 12px;
      font-weight: 900;
    }
    .mode-toggle label:has(input:checked) {
      color: var(--accent-contrast);
      background: var(--accent);
      border-color: rgba(255, 255, 255, 0.16);
    }
    .eleven-actions {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin: 8px auto 10px;
    }
    .config-state {
      min-width: 0;
      color: var(--muted);
      font-size: 12px;
      font-weight: 900;
      line-height: 1.3;
    }
    .api-key-field { margin-top: 8px; }
    .api-key-panel[hidden] { display: none !important; }
    .api-key-actions {
      align-items: center;
      margin-top: 6px;
      margin-bottom: 14px;
    }
    .save-voice-btn {
      min-height: 36px;
      padding: 8px 12px;
    }
    .engine-note {
      max-width: 440px;
      margin: 10px auto 12px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      line-height: 1.45;
    }
    .engine-note code {
      color: var(--text);
      font-weight: 900;
    }
    .engine-note.compact {
      margin-top: 8px;
      margin-bottom: 12px;
      padding: 10px 12px;
      border: 1px solid var(--control-line-soft);
      border-radius: 14px;
      background: var(--surface);
    }
    .primary-field { margin-bottom: 12px; }
    .render-primary-actions { margin-top: 12px; }
    .advanced-settings {
      max-width: 440px;
      margin: 14px auto 0;
      border: 1px solid var(--control-line-soft);
      border-radius: 18px;
      background: var(--surface-panel);
      overflow: hidden;
    }
    .advanced-settings summary {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      min-height: 48px;
      padding: 10px 12px;
      cursor: pointer;
      list-style: none;
      color: var(--text-soft);
      font-weight: 950;
    }
    .advanced-settings summary::-webkit-details-marker { display: none; }
    .advanced-settings summary::after {
      content: '⌄';
      width: 24px;
      height: 24px;
      border-radius: 999px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      color: var(--accent-contrast);
      background: var(--accent);
      transition: transform 0.18s ease;
      flex: 0 0 auto;
    }
    .advanced-settings[open] summary::after { transform: rotate(180deg); }
    .advanced-summary-main {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
    }
    .advanced-summary-main .btn-icon {
      color: #fff8ed;
      background: linear-gradient(135deg, #a78bfa, #7c3aed);
      box-shadow: 0 7px 18px rgba(124, 58, 237, 0.24);
    }
    .advanced-summary-state {
      margin-left: auto;
      color: var(--muted);
      font-size: 11px;
      font-weight: 900;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      white-space: nowrap;
    }
    .advanced-body {
      display: grid;
      gap: 0;
      padding: 0 12px 12px;
      border-top: 1px solid var(--control-line-soft);
    }
    .advanced-body .field:first-child { margin-top: 12px; }
    .field input[type="file"] {
      min-height: 54px;
      padding: 8px;
      color: var(--text-soft);
      background: rgba(255, 255, 255, 0.035);
    }
    body.theme-light .field input[type="file"] {
      background: rgba(255, 251, 244, 0.62);
    }
    .field input[type="file"]::file-selector-button {
      min-height: 36px;
      margin-right: 12px;
      border: 0;
      border-radius: 11px;
      padding: 8px 13px;
      color: var(--accent-contrast);
      background: linear-gradient(135deg, var(--accent), #d88435);
      font-family: inherit;
      font-weight: 950;
      cursor: pointer;
    }
    .advanced-settings {
      background: rgba(255, 255, 255, 0.032);
      box-shadow: none;
    }
    body.theme-light .advanced-settings {
      background: rgba(255, 251, 244, 0.46);
    }
    .advanced-settings summary {
      min-height: 44px;
      padding: 8px 10px;
      font-size: 15px;
      letter-spacing: 0;
    }
    .advanced-summary-main .btn-icon {
      width: 24px;
      height: 24px;
      font-size: 11px;
    }
    .advanced-summary-state {
      font-size: 10px;
      opacity: 0.62;
      text-transform: none;
      letter-spacing: 0.02em;
    }
    .advanced-settings summary::after {
      width: 22px;
      height: 22px;
      font-size: 12px;
    }
    .advanced-body {
      gap: 10px;
      padding: 12px;
    }
    .advanced-body .field,
    .advanced-body .check,
    .advanced-body .eleven-actions,
    .advanced-body .speed-presets {
      max-width: none;
      margin-left: 0;
      margin-right: 0;
    }
    .advanced-body .field {
      display: grid;
      grid-template-columns: minmax(132px, 0.72fr) minmax(0, 1fr);
      align-items: center;
      gap: 10px 14px;
      margin-top: 0;
      margin-bottom: 0;
    }
    .advanced-body .field:first-child { margin-top: 0; }
    .advanced-body .field-label {
      align-self: center;
      gap: 8px;
      min-width: 0;
    }
    .advanced-body .field-label span:last-child {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .advanced-body .field input,
    .advanced-body .field select {
      min-height: 42px;
      border-radius: 14px;
    }
    .advanced-body .speed-presets {
      justify-content: flex-start;
      width: auto;
      margin-top: -2px;
      margin-left: calc(132px + 14px);
      margin-bottom: 0;
      gap: 7px;
    }
    .advanced-body .speed-preset {
      min-height: 34px;
      padding: 8px 13px;
      border-radius: 12px;
    }
    .advanced-body .check {
      min-height: 40px;
      margin-top: 8px;
      margin-bottom: 8px;
      padding: 9px 11px;
      border: 1px solid var(--control-line-soft);
      border-radius: 13px;
      background: var(--surface);
    }
    .advanced-body .eleven-actions {
      justify-content: flex-start;
      flex-wrap: wrap;
      margin-top: 0;
      margin-bottom: 8px;
    }
    .file-picker {
      width: 100%;
      min-height: 54px;
      display: grid;
      grid-template-columns: auto minmax(0, 1fr);
      align-items: center;
      gap: 12px;
      border: 1px solid var(--control-line);
      border-radius: 14px;
      padding: 8px;
      color: var(--text-soft);
      background: rgba(255, 255, 255, 0.035);
      cursor: pointer;
    }
    body.theme-light .file-picker {
      background: rgba(255, 251, 244, 0.62);
    }
    .file-picker input[type="file"] {
      position: absolute;
      width: 1px;
      height: 1px;
      opacity: 0;
      pointer-events: none;
    }
    .file-picker-button {
      min-height: 36px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border-radius: 11px;
      padding: 8px 14px;
      color: var(--accent-contrast);
      background: linear-gradient(135deg, var(--accent), #d88435);
      font-size: 12px;
      font-weight: 950;
      white-space: nowrap;
    }
    .file-picker-name {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      color: var(--text-soft);
      font-size: 13px;
      font-weight: 800;
    }
    .advanced-settings[open] .advanced-body {
      max-height: none;
      overflow: visible;
      padding-bottom: 20px;
    }
    @media (max-width: 720px) {
      .advanced-body .field { grid-template-columns: 1fr; }
      .advanced-body .speed-presets { margin-left: 0; }
    }
    .field-label .field-icon {
      color: #fff8ed;
      background: linear-gradient(135deg, #34d399, #16a34a);
      box-shadow: 0 7px 18px rgba(22, 163, 74, 0.22);
      font-size: 11px;
    }
    [data-pane="elevenlabs"] .field-icon {
      background: linear-gradient(135deg, #f472b6, #be185d);
      box-shadow: 0 7px 18px rgba(244, 114, 182, 0.22);
    }
    [data-pane="edgetts"] .field-icon {
      background: linear-gradient(135deg, #34d399, #16a34a);
      box-shadow: 0 7px 18px rgba(22, 163, 74, 0.22);
    }
    .field input,
    .field select {
      width: 100%;
      min-height: 40px;
      border: 1px solid var(--control-line);
      border-radius: 13px;
      padding: 9px 11px;
      color: var(--text);
      background: var(--field-bg);
    }
    .speed-presets {
      display: flex;
      align-items: center;
      justify-content: center;
      width: fit-content;
      max-width: 100%;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: -2px;
      margin-bottom: 12px;
    }
    .speed-preset {
      min-height: 34px;
      padding: 8px 14px;
      border: 1px solid var(--control-line-faint);
      border-radius: 12px;
      color: var(--text-soft);
      background: var(--surface);
      font-size: 12px;
      font-weight: 900;
      cursor: pointer;
    }
    .speed-preset.active {
      color: var(--accent-contrast);
      background: var(--accent);
      border-color: transparent;
    }
    .selected-box {
      max-width: 440px;
      margin: 0 auto;
      border: 1px solid var(--control-line-soft);
      border-radius: 16px;
      padding: 10px 11px;
      background: var(--surface);
      text-align: center;
    }
    .selected-box strong { display: block; margin-bottom: 3px; }
    .selected-box span { display: block; color: var(--muted); font-size: 12px; }
    .studio-steps {
      max-width: 440px;
      margin: 12px auto 0;
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 7px;
    }
    .studio-step {
      min-width: 0;
      min-height: 70px;
      display: grid;
      align-content: center;
      gap: 3px;
      border: 1px solid var(--control-line-soft);
      border-radius: 14px;
      padding: 8px 6px;
      background: var(--surface);
      text-align: center;
    }
    .studio-step span {
      color: var(--accent);
      font-size: 10px;
      font-weight: 950;
      letter-spacing: 0.08em;
    }
    .studio-step strong {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      color: var(--text);
      font-size: 12px;
      font-weight: 950;
    }
    .studio-step small {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      color: var(--muted);
      font-size: 10px;
      font-weight: 800;
    }
    .studio-step.active {
      border-color: rgba(242, 178, 101, 0.44);
      background:
        linear-gradient(135deg, rgba(242, 178, 101, 0.18), rgba(34, 197, 94, 0.08)),
        var(--surface);
      box-shadow: 0 12px 30px rgba(242, 178, 101, 0.10);
    }
    .form-actions,
    .actions { display: flex; align-items: center; justify-content: flex-end; gap: 6px; flex-wrap: nowrap; white-space: nowrap; }
    .status {
      border: 1px solid var(--control-line-soft);
      border-radius: 16px;
      padding: 9px 11px;
      background: var(--surface);
      color: var(--status-text);
      font-size: 13px;
      line-height: 1.45;
      margin-top: 10px;
    }
    .render-machine .status {
      max-width: 440px;
      margin: 10px auto 0;
      text-align: center;
    }
    .status.good { border-color: rgba(242, 178, 101, 0.35); color: var(--good-text); }
    .status.warn { border-color: rgba(255, 171, 64, 0.38); color: var(--warn-text); }
    .status.bad { border-color: rgba(255, 82, 82, 0.42); color: var(--danger-text); }
    .cache-warning {
      margin-top: 10px;
      border: 1px solid rgba(255, 171, 64, 0.38);
      border-radius: 16px;
      padding: 10px 12px;
      color: var(--warning-text);
      background: rgba(255, 171, 64, 0.08);
      font-size: 12px;
      line-height: 1.45;
    }
    .cache-warning strong { display: block; margin-bottom: 6px; color: var(--warn-text); }
    .cache-warning ul { margin: 0; padding-left: 18px; }
    .cache-warning li + li { margin-top: 6px; }
    .header-warning { max-width: 820px; }
    .tabs {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      margin-top: 12px;
      margin-bottom: 12px;
    }
    .tab,
    .select-btn,
    .refresh-btn,
    .delete-output-btn,
    .icon-btn {
      border: 1px solid var(--control-line-faint);
      border-radius: 13px;
      padding: 8px 10px;
      cursor: pointer;
      color: var(--text-soft);
      background: var(--surface);
      font-weight: 900;
      gap: 6px;
      min-height: 36px;
      font-size: 12px;
      line-height: 1;
      flex: 0 0 auto;
    }
    .tab {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
    }
    .tab.active .tab-icon {
      color: #170d05;
      background: rgba(255, 245, 230, 0.58);
      box-shadow: none;
      text-shadow: none;
    }
    .tab[data-engine="elevenlabs"] .tab-icon {
      color: #fff8ed;
      background: linear-gradient(135deg, #f472b6, #be185d);
      box-shadow: 0 7px 18px rgba(244, 114, 182, 0.22);
    }
    .tab[data-engine="edgetts"] .tab-icon {
      color: #170d05;
      background: linear-gradient(135deg, #facc15, #f97316);
      box-shadow: 0 7px 18px rgba(250, 204, 21, 0.20);
    }
    .tab.active .tab-icon { color: #170d05; background: rgba(255, 245, 230, 0.58); box-shadow: none; }
    .icon-btn { display: inline-flex; align-items: center; justify-content: center; text-decoration: none; }
    .icon-btn[hidden],
    .small-link[hidden] { display: none !important; }
    .btn-icon {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 20px;
      height: 20px;
      border-radius: 999px;
      color: #fff8ed;
      background: linear-gradient(135deg, #f2b261, #d88435);
      font-size: 11px;
      font-weight: 950;
      line-height: 1;
      box-shadow: 0 7px 18px rgba(242, 178, 101, 0.22);
      text-shadow: none;
    }
    .small-link .btn-icon,
    .select-btn .btn-icon {
      color: #fff8ed;
      background: linear-gradient(135deg, #f2b261, #d88435);
    }
    .theme-toggle .btn-icon {
      color: #fff8ed;
      background: linear-gradient(135deg, #a78bfa, #7c3aed);
      box-shadow: 0 7px 18px rgba(124, 58, 237, 0.24);
    }
    #refreshProjects .btn-icon {
      color: #f5f9ff;
      background: linear-gradient(135deg, #c084fc, #7c3aed);
      box-shadow: 0 7px 18px rgba(124, 58, 237, 0.24);
    }
    a[href="/upload"].icon-btn .btn-icon,
    #startRender .btn-icon {
      color: #170d05;
      background: linear-gradient(135deg, #facc15, #f97316);
      box-shadow: 0 7px 18px rgba(250, 204, 21, 0.20);
    }
    .select-btn .btn-icon {
      color: #170d05;
      background: linear-gradient(135deg, #fde68a, #f59e0b);
      box-shadow: 0 7px 18px rgba(245, 158, 11, 0.20);
    }
    .small-link.icon-btn .btn-icon {
      color: #170d05;
      background: linear-gradient(135deg, #fde68a, #facc15);
      box-shadow: 0 7px 18px rgba(250, 204, 21, 0.20);
    }
    .row-video-slot .small-link.icon-btn .btn-icon,
    a.small-link.icon-btn[href*="/output/final_video.mp4"] .btn-icon {
      color: #06210f;
      background: linear-gradient(135deg, #86efac, #22c55e);
      box-shadow: 0 7px 18px rgba(34, 197, 94, 0.22);
    }
    .reveal-output-btn .btn-icon {
      color: #fff8ed;
      background: linear-gradient(135deg, #c084fc, #7c3aed);
      box-shadow: 0 7px 18px rgba(124, 58, 237, 0.22);
    }
    .stop-render-btn {
      color: var(--delete-text);
      background: rgba(255, 82, 82, 0.12);
      border-color: rgba(255, 82, 82, 0.28);
    }
    .stop-render-btn .btn-icon {
      color: #fff5f5;
      background: linear-gradient(135deg, #ff8d8d, #b94747);
      box-shadow: 0 7px 18px rgba(255, 82, 82, 0.22);
      text-shadow: none;
    }
    .delete-output-btn .btn-icon {
      color: #fff5f5;
      background: linear-gradient(135deg, #ff8d8d, #b94747);
      box-shadow: 0 7px 18px rgba(255, 82, 82, 0.22);
      text-shadow: none;
    }
    .icon-btn.disabled,
    .icon-btn:disabled {
      opacity: 0.42;
      cursor: not-allowed;
    }
    .icon-btn.disabled {
      pointer-events: none;
    }
    .tab.active,
    .select-btn.active {
      color: var(--accent-contrast);
      background: linear-gradient(135deg, var(--accent), #d88435);
      border-color: transparent;
      box-shadow: 0 10px 28px var(--accent-glow);
    }
    .select-btn.active .btn-icon,
    .refresh-btn .btn-icon {
      color: #170d05;
      background: rgba(255, 245, 230, 0.58);
      box-shadow: none;
      text-shadow: none;
    }
    .refresh-btn {
      min-height: 42px;
      color: var(--accent-contrast);
      background: linear-gradient(135deg, var(--accent), #d88435);
      border-color: transparent;
      box-shadow: 0 12px 32px var(--accent-glow);
    }
    .refresh-btn.guide-header-btn {
      color: #2f1d03;
      background: linear-gradient(135deg, #facc15, #f59e0b);
      box-shadow: 0 12px 32px rgba(245, 158, 11, 0.24);
    }
    .refresh-btn.guide-header-btn .btn-icon {
      color: #2f1d03;
      background: rgba(255, 255, 255, 0.82);
    }
    .delete-output-btn {
      color: var(--delete-text);
      background: rgba(255, 82, 82, 0.12);
      border-color: rgba(255, 82, 82, 0.28);
    }
    .delete-output-btn:hover { background: rgba(255, 82, 82, 0.2); }
    .check { display: flex; align-items: center; gap: 9px; margin: 8px 0 12px; color: var(--status-text); font-size: 13px; }
    .start {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 9px;
      min-height: 42px;
      border: 0;
      border-radius: 14px;
      padding: 0 18px;
      color: var(--accent-contrast);
      background: linear-gradient(135deg, var(--accent), #d88435);
      font-weight: 900;
      cursor: pointer;
    }
    .render-machine .form-actions { justify-content: center; }
    .start:disabled { cursor: wait; opacity: 0.6; }
    .upload-panel {
      max-width: 440px;
      margin: 14px auto 0;
      border: 1px solid rgba(242, 178, 101, 0.16);
      border-radius: 20px;
      padding: 14px;
      background:
        radial-gradient(circle at 20% 0%, rgba(242, 178, 101, 0.10), transparent 40%),
        var(--surface-panel);
    }
    .upload-panel[hidden] { display: none !important; }
    .upload-head { text-align: center; margin-bottom: 10px; }
    .upload-head h3 { margin: 0 0 5px; font-size: 18px; letter-spacing: 0; }
    .upload-head span {
      display: block;
      color: var(--text-faint);
      font-size: 12px;
      line-height: 1.45;
    }
    .upload-field { margin-top: 10px; margin-bottom: 0; }
    .upload-field textarea {
      width: 100%;
      resize: vertical;
      min-height: 104px;
      border: 1px solid var(--control-line);
      border-radius: 13px;
      padding: 9px 11px;
      color: var(--text);
      background: var(--field-bg);
      font-family: inherit;
      line-height: 1.45;
    }
    .upload-field.compact { max-width: 240px; }
    .upload-actions {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      margin-top: 12px;
    }
    .upload-btn {
      min-height: 38px;
      border: 1px solid var(--control-line);
      border-radius: 13px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 7px;
      color: var(--text-button);
      background: var(--surface);
      font-size: 12px;
      font-weight: 950;
      cursor: pointer;
    }
    .upload-btn.youtube { border-color: rgba(255, 82, 82, 0.34); background: rgba(255, 82, 82, 0.12); }
    .upload-btn.facebook { border-color: rgba(74, 144, 226, 0.34); }
    .upload-btn:disabled { cursor: not-allowed; opacity: 0.46; }
    .upload-status,
    .upload-result {
      margin-top: 10px;
      border: 1px solid var(--control-line-soft);
      border-radius: 14px;
      padding: 9px 10px;
      color: var(--status-text);
      background: var(--surface);
      font-size: 12px;
      line-height: 1.45;
    }
    .upload-status.good,
    .upload-result.good { border-color: rgba(242, 178, 101, 0.34); color: var(--good-text); }
    .upload-status.bad,
    .upload-result.bad { border-color: rgba(255, 82, 82, 0.40); color: var(--danger-text); }
    .upload-status.warn,
    .upload-result.warn { border-color: rgba(255, 171, 64, 0.34); color: var(--warn-text); }
    .upload-result a { color: var(--accent); font-weight: 900; }
    .render-state {
      width: 100%;
      margin: 14px 0 0;
      border: 1px solid rgba(242, 178, 101, 0.16);
      border-radius: 16px;
      padding: 12px;
      color: var(--good-text);
      background: var(--surface-panel);
    }
    .state-head {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 9px;
      color: var(--text);
      font-size: 13px;
    }
    .state-dot {
      width: 9px;
      height: 9px;
      border-radius: 999px;
      background: var(--accent);
      box-shadow: 0 0 16px rgba(242, 178, 101, 0.65);
    }
    .render-state.running .state-dot {
      animation: statePulse 1s ease-in-out infinite;
    }
    .render-state.failed .state-dot {
      background: #ff5252;
      box-shadow: 0 0 16px rgba(255, 82, 82, 0.65);
      animation: none;
    }
    .render-state.cancelled .state-dot {
      background: #ffab40;
      box-shadow: 0 0 16px rgba(255, 171, 64, 0.55);
      animation: none;
    }
    .state-list {
      display: grid;
      gap: 7px;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .state-list li {
      display: block;
      overflow: hidden;
      border-radius: 10px;
      padding: 8px 9px;
      color: var(--text-soft);
      background: var(--surface);
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 12px;
      line-height: 1.45;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .state-list li::before {
      content: none;
    }
    @keyframes statePulse {
      0%, 100% { transform: scale(0.92); opacity: 0.65; }
      50% { transform: scale(1.28); opacity: 1; }
    }
    .list-head,
    .project-row {
      display: grid;
      grid-template-columns: minmax(220px, 1fr) 112px minmax(0, 460px);
      gap: 12px;
      align-items: center;
    }
    .list-head {
      padding: 0 18px 10px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 900;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }
    .list-head span:last-child {
      justify-self: center;
      text-align: center;
    }
    .project-list {
      display: grid;
      gap: 10px;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .project-row {
      border: 1px solid var(--line);
      border-radius: 17px;
      padding: 13px 14px;
      background: var(--surface);
      transition: border-color 0.18s ease, background 0.18s ease, box-shadow 0.18s ease, transform 0.18s ease;
    }
    .project-row .actions {
      display: grid;
      grid-template-columns: 82px 74px 88px 70px 64px;
      justify-content: end;
      gap: 7px;
      min-width: 0;
      white-space: nowrap;
    }
    .project-row .actions .icon-btn,
    .project-row .actions .select-btn,
    .project-row .actions .small-link,
    .project-row .actions .delete-output-btn {
      width: 100%;
      min-width: 0;
      min-height: 42px;
      padding: 8px 8px;
      border-radius: 14px;
      font-size: 12px;
    }
    .project-row .actions .btn-icon {
      width: 22px;
      height: 22px;
      flex: 0 0 22px;
    }
    .project-row .row-video-slot {
      min-width: 0;
      width: 100%;
    }
    .project-row.selected {
      border-color: rgba(34, 197, 94, 0.95);
      background:
        radial-gradient(circle at 0% 50%, rgba(34, 197, 94, 0.20), transparent 36%),
        linear-gradient(135deg, rgba(34, 197, 94, 0.14), rgba(255, 255, 255, 0.055));
      box-shadow:
        0 0 0 2px rgba(34, 197, 94, 0.26),
        0 18px 52px rgba(34, 197, 94, 0.15);
      transform: translateY(-2px);
    }
    .project-row.selected .project-name { color: #eafff2; }
    body.theme-light .project-row.selected .project-name { color: #20170f; }
    body.theme-light .project-row.selected {
      border-color: rgba(22, 163, 74, 0.95);
      background:
        radial-gradient(circle at 0% 50%, rgba(34, 197, 94, 0.20), transparent 36%),
        linear-gradient(135deg, rgba(34, 197, 94, 0.13), rgba(255, 248, 236, 0.78));
      box-shadow:
        0 0 0 2px rgba(22, 163, 74, 0.26),
        0 18px 52px rgba(22, 163, 74, 0.15);
    }
    .project-main { display: grid; gap: 4px; min-width: 0; }
    .project-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 18px; font-weight: 900; letter-spacing: 0; }
    .project-slide-count {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: fit-content;
      min-height: 26px;
      padding: 4px 10px;
      border: 1px solid rgba(242, 178, 101, 0.36);
      border-radius: 999px;
      color: var(--accent-contrast);
      background: linear-gradient(135deg, #f2b261, #b86b2a);
      box-shadow: 0 10px 24px rgba(242, 178, 101, 0.18);
      font-size: 12px;
      font-weight: 950;
    }
    .status-pill {
      justify-self: start;
      border-radius: 999px;
      padding: 7px 11px;
      font-size: 12px;
      font-weight: 900;
      white-space: nowrap;
    }
    .status-pill.ok { color: var(--accent-contrast); background: var(--accent); }
    .status-pill.bad { color: #fff0f0; background: rgba(255, 82, 82, 0.22); border: 1px solid rgba(255, 82, 82, 0.35); }
    .muted, .empty { color: var(--muted); }
    .row-video-slot { display: inline-flex; align-items: center; flex: 0 0 auto; }
    [hidden] { display: none !important; }
    @media (max-width: 1060px) {
      body { height: auto; min-height: 100vh; overflow: auto; display: block; }
      .dashboard-header { align-items: flex-start; }
      .dashboard-shell { grid-template-columns: 1fr; min-height: auto; overflow: visible; }
      .render-machine { align-self: auto; height: auto; min-height: 0; position: static; max-height: none; overflow: visible; }
      .slide-list { height: auto; overflow: visible; }
    }
    @media (max-width: 720px) {
      body { padding: 20px 14px; }
      .dashboard-header { display: grid; }
      .header-tools { justify-content: flex-start; }
      .studio-steps { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .list-head { display: none; }
      .project-row { grid-template-columns: 1fr; align-items: stretch; }
      .project-row .actions {
        grid-template-columns: repeat(5, minmax(0, 1fr));
        justify-content: stretch;
        overflow: visible;
        padding-bottom: 2px;
      }
      .project-row .actions .icon-btn,
      .project-row .actions .select-btn,
      .project-row .actions .small-link,
      .project-row .actions .delete-output-btn {
        padding: 8px 6px;
        font-size: 11px;
      }
    }
""",
        extra_script=f"""
  <script>
    window.__PROJECTS__ = {json.dumps(projects, ensure_ascii=False)};
    window.__INITIAL_PROJECT__ = {json.dumps(selected_project, ensure_ascii=False)};
    window.__PROJECT_SOURCE_ROOT__ = {json.dumps(str(SLIDE_ROOT), ensure_ascii=False)};
  </script>
  <script src="/web/render_page.js?v=20260607-ai-storyboard-v1"></script>
""",
    )


def render_upload_html(selected_project: str | None = None) -> bytes:
    projects = list_projects()
    project_names = {project["name"] for project in projects}
    output_projects = [project for project in projects if project["video_url"]]
    if selected_project not in project_names:
        selected_project = output_projects[0]["name"] if output_projects else (projects[0]["name"] if projects else "")

    options = "\n".join(
        f'<option value="{html.escape(project["name"])}" {"selected" if project["name"] == selected_project else ""}>'
        f'{html.escape(project["name"])}'
        "</option>"
        for project in projects
    )
    return render_page_shell(
        title="Publish Studio",
        body=f"""
  <main class="upload-shell">
    <section class="upload-machine">
      <div class="top-upload-bar">
        <div class="top-project-row">
          <img src="/web/viro-icon.svg" alt="Viro" class="upload-brand-mark" />
          <label class="field project-select-field field-project top-project-field">
          <span class="project-field-head">
            <span class="field-label"><span class="field-icon">P</span><span>Project</span></span>
            <span class="project-video-badge" id="projectVideoBadge">final_video</span>
          </span>
            <select id="projectSelect" {"disabled" if not projects else ""}>
              {options}
            </select>
          </label>
        </div>
        <div class="top-upload-actions">
          <span class="ready-pill"><strong>{len(output_projects)}</strong><span>sẵn sàng</span></span>
          <div class="header-tools">
            <button class="refresh-btn icon-btn theme-toggle" type="button" data-theme-toggle><span class="btn-icon">☾</span><span>Theme</span></button>
            <button class="refresh-btn icon-btn" type="button" data-source-root-select><span class="btn-icon">⌕</span><span>Source</span></button>
            <a class="refresh-btn icon-btn" href="/"><span class="btn-icon">←</span><span>Studio</span></a>
            <button class="refresh-btn icon-btn" id="refreshProjects" type="button"><span class="btn-icon">↻</span><span>Refresh</span></button>
          </div>
        </div>
      </div>

      <div class="status warn" id="renderStatus" hidden></div>

      <div class="upload-empty" id="uploadEmpty" hidden>
        <strong id="uploadEmptyProject">Project này chưa có final_video.mp4</strong>
        <span>Render project trong Viro Studio trước, rồi quay lại Publish Studio để đăng.</span>
        <a class="small-link" href="/">Về Studio</a>
      </div>

      <div class="upload-panel" id="uploadPanel" hidden>
        <div class="platform-grid">
          <section class="platform-card platform-youtube">
            <div class="platform-card-head">
              <div class="platform-title"><span class="field-icon">YT</span><span>YouTube</span></div>
              <a class="small-link icon-btn platform-guide-link" href="/upload-guide/youtube" target="_blank" rel="noreferrer"><span class="btn-icon">?</span><span>Hướng dẫn YouTube</span></a>
            </div>
            <div class="platform-account-list" id="youtubeAccountList" hidden></div>
            <div class="field upload-field field-title">
              <span class="field-label field-label-between">
                <span class="field-label-main"><span class="field-icon">T</span><span>YouTube Title</span></span>
                <button class="copy-field-btn" data-copy-target="uploadTitle" data-copy-label="YouTube Title" type="button" aria-label="Copy YouTube Title" title="Copy YouTube Title"><span class="btn-icon">⧉</span></button>
              </span>
              <input id="uploadTitle" type="text" maxlength="100" placeholder="Tiêu đề YouTube" />
            </div>
            <div class="field upload-field field-description">
              <span class="field-label field-label-between">
                <span class="field-label-main"><span class="field-icon">≡</span><span>YouTube Description</span></span>
                <button class="copy-field-btn" data-copy-target="youtubeDescription" data-copy-label="YouTube Description" type="button" aria-label="Copy YouTube Description" title="Copy YouTube Description"><span class="btn-icon">⧉</span></button>
              </span>
              <textarea id="youtubeDescription" rows="7" maxlength="5000" placeholder="Mô tả YouTube, có nguồn và hashtag"></textarea>
            </div>
            <label class="field upload-field compact field-youtube">
              <span class="field-label"><span>YouTube visibility</span></span>
              <select id="youtubePrivacy">
                <option value="private" selected>Private - duyệt trước</option>
                <option value="unlisted">Unlisted - có link mới xem</option>
                <option value="public">Public - đăng công khai</option>
              </select>
            </label>
            <div class="platform-actions">
              <button class="upload-btn youtube" id="connectYoutube" type="button"><span class="btn-icon">▶</span><span>Connect</span></button>
              <button class="upload-btn youtube" id="uploadYoutube" type="button"><span class="btn-icon">↑</span><span>Upload YouTube</span></button>
            </div>
          </section>
          <section class="platform-card platform-facebook">
            <div class="platform-card-head">
              <div class="platform-title"><span class="field-icon">f</span><span>Facebook Reels</span></div>
              <a class="small-link icon-btn platform-guide-link" href="/upload-guide/facebook" target="_blank" rel="noreferrer"><span class="btn-icon">?</span><span>Hướng dẫn Facebook</span></a>
            </div>
            <div class="platform-account-list" id="facebookAccountList" hidden></div>
            <div class="field upload-field field-description">
              <span class="field-label field-label-between">
                <span class="field-label-main"><span class="field-icon">≡</span><span>Facebook Caption</span></span>
                <button class="copy-field-btn" data-copy-target="facebookCaption" data-copy-label="Facebook Caption" type="button" aria-label="Copy Facebook Caption" title="Copy Facebook Caption"><span class="btn-icon">⧉</span></button>
              </span>
              <textarea id="facebookCaption" rows="7" maxlength="5000" placeholder="Caption Reels, không chứa link nguồn"></textarea>
            </div>
            <div class="field upload-field field-source-comment">
              <span class="field-label field-label-between">
                <span class="field-label-main"><span class="field-icon">↳</span><span>Source comment</span></span>
                <button class="copy-field-btn" data-copy-target="facebookSourceComment" data-copy-label="Source comment" type="button" aria-label="Copy Source comment" title="Copy Source comment"><span class="btn-icon">⧉</span></button>
              </span>
              <input id="facebookSourceComment" type="text" maxlength="1000" placeholder="Nguồn: https://..." />
            </div>
            <label class="field upload-field compact field-facebook">
              <span class="field-label"><span>Reels status</span></span>
              <select id="facebookVideoState">
                <option value="DRAFT" selected>Draft - duyệt trước</option>
                <option value="PUBLISHED">Publish now</option>
              </select>
            </label>
            <div class="platform-actions">
              <button class="upload-btn facebook secondary" id="openFacebookConfig" type="button"><span class="btn-icon">+</span><span>Thêm Page</span></button>
              <button class="upload-btn facebook" id="uploadFacebook" type="button"><span class="btn-icon">f</span><span>Upload Facebook Reels</span></button>
            </div>
          </section>
        </div>
        <div class="final-upload-actions">
          <button class="upload-btn both" id="uploadBothPublic" type="button" disabled><span class="btn-icon">✓</span><span>Upload Public YouTube + Reels</span></button>
          <button class="upload-btn facebook secondary" id="commentFacebookSource" type="button" disabled><span class="btn-icon">↳</span><span>Comment source</span></button>
        </div>
        <div class="upload-status" id="uploadStatus" hidden></div>
        <div class="upload-result" id="uploadResult" hidden></div>
      </div>
      <div class="modal-backdrop" id="facebookConfigModal" hidden>
        <div class="modal-card facebook-config-modal" role="dialog" aria-modal="true" aria-labelledby="facebookConfigTitle">
          <button class="modal-close" id="closeFacebookConfig" type="button" aria-label="Đóng">×</button>
          <p class="kicker">Facebook Page</p>
          <h3 id="facebookConfigTitle">Thêm Page</h3>
          <p class="modal-copy">Dán Page ID và Page access token đã extend. Token sẽ được lưu vào <code>config/social-upload.json</code> và không hiện lại trên UI.</p>
          <div class="field upload-field compact facebook-config-field">
            <span class="field-label"><span class="field-icon">ID</span><span>Facebook Page ID</span></span>
            <input id="facebookPageId" type="text" inputmode="numeric" autocomplete="off" placeholder="Dán Page ID" />
          </div>
          <div class="field upload-field compact facebook-config-field">
            <span class="field-label"><span class="field-icon">🔑</span><span>Page access token</span></span>
            <input id="facebookPageAccessToken" type="password" autocomplete="off" placeholder="Dán Page access token" />
          </div>
          <div class="modal-actions">
            <button class="upload-btn secondary" id="cancelFacebookConfig" type="button"><span class="btn-icon">×</span><span>Huỷ</span></button>
            <button class="upload-btn facebook" id="saveFacebookConfig" type="button"><span class="btn-icon">✓</span><span>Lưu Page</span></button>
          </div>
        </div>
      </div>
    </section>
  </main>
""",
        extra_style="""
    body {
      --upload-page-max: 1800px;
      padding: 24px clamp(24px, 3vw, 56px);
    }
    body:not(.theme-light) {
      --body-bg: #000;
      --surface: rgba(255, 255, 255, 0.075);
      --surface-strong: rgba(255, 255, 255, 0.12);
      --surface-panel: rgba(8, 10, 14, 0.94);
      --field-bg: rgba(0, 0, 0, 0.58);
      --control-line: rgba(255, 255, 255, 0.20);
      --control-line-soft: rgba(255, 255, 255, 0.16);
      --text-faint: rgba(246, 255, 249, 0.72);
      --status-text: rgba(246, 255, 249, 0.84);
      --shadow: 0 24px 80px rgba(0, 0, 0, 0.72);
    }
    .upload-header {
      display: grid;
      grid-template-columns: minmax(320px, 0.86fr) minmax(420px, 1fr);
      align-items: center;
      justify-content: space-between;
      gap: 24px;
      width: min(100%, var(--upload-page-max));
      max-width: var(--upload-page-max);
      margin: 0 auto 22px;
    }
    .upload-title-block {
      display: flex;
      align-items: center;
      gap: 14px;
      min-height: 74px;
    }
    .upload-brand-mark {
      width: 58px;
      height: 58px;
      flex: 0 0 58px;
      border-radius: 16px;
      object-fit: cover;
      box-shadow: 0 14px 34px rgba(0, 0, 0, 0.24);
    }
    .upload-header h1 {
      margin: 0;
      font-size: clamp(36px, 4.4vw, 54px);
      line-height: 0.95;
      letter-spacing: 0;
      white-space: nowrap;
    }
    .kicker { margin: 0 0 6px; color: var(--muted); font-size: 11px; font-weight: 900; letter-spacing: 0.16em; text-transform: uppercase; }
    .header-tools {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      justify-content: end;
      align-items: stretch;
      width: min(100%, 520px);
      justify-self: end;
    }
    .upload-header .refresh-btn {
      min-height: 42px;
      padding: 8px 12px;
      width: 100%;
      white-space: nowrap;
    }
    .upload-shell {
      width: min(100%, var(--upload-page-max));
      max-width: var(--upload-page-max);
      margin: 0 auto;
    }
    .upload-machine { display: grid; gap: 10px; }
    .top-upload-bar {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: center;
      gap: 14px;
      border: 1px solid rgba(242, 178, 101, 0.16);
      border-radius: 22px;
      padding: 10px 12px;
      background:
        radial-gradient(circle at 0% 0%, rgba(34, 197, 94, 0.11), transparent 30%),
        var(--surface-panel);
      box-shadow: var(--shadow);
    }
    body:not(.theme-light) .top-upload-bar {
      border-color: rgba(34, 197, 94, 0.36);
      background:
        radial-gradient(circle at 4% 0%, rgba(34, 197, 94, 0.16), transparent 34%),
        linear-gradient(150deg, rgba(10, 28, 22, 0.97), rgba(2, 8, 7, 0.98));
      box-shadow:
        0 0 0 1px rgba(34, 197, 94, 0.10),
        0 22px 68px rgba(0, 0, 0, 0.72);
    }
    .top-project-row {
      display: flex;
      align-items: center;
      gap: 12px;
      min-width: 0;
    }
    .top-upload-bar .upload-brand-mark {
      width: 44px;
      height: 44px;
      flex: 0 0 44px;
      border-radius: 14px;
    }
    .top-project-field {
      display: grid;
      grid-template-columns: auto minmax(260px, 1fr);
      align-items: center;
      gap: 10px;
      width: 100%;
      min-width: 0;
    }
    .top-project-field .project-field-head {
      justify-content: flex-start;
      flex-wrap: nowrap;
      min-width: 0;
    }
    .top-project-field select {
      min-height: 42px;
      padding-block: 8px;
      font-size: 14px;
    }
    .top-upload-actions {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 10px;
      min-width: 0;
    }
    .top-upload-actions .ready-pill {
      min-height: 40px;
      padding: 7px 11px;
    }
    .top-upload-actions .header-tools {
      width: auto;
      grid-template-columns: repeat(4, minmax(96px, auto));
    }
    .top-upload-actions .refresh-btn {
      min-height: 40px;
      padding: 8px 11px;
    }
    .project-picker {
      display: grid;
      gap: 10px;
      border: 1px solid rgba(242, 178, 101, 0.16);
      border-radius: 22px;
      padding: 14px;
      background: var(--surface-panel);
      box-shadow: var(--shadow);
    }
    body:not(.theme-light) .project-picker {
      border-color: rgba(34, 197, 94, 0.42);
      background:
        radial-gradient(circle at 4% 0%, rgba(34, 197, 94, 0.18), transparent 34%),
        linear-gradient(150deg, rgba(10, 28, 22, 0.97), rgba(2, 8, 7, 0.98));
      box-shadow:
        0 0 0 1px rgba(34, 197, 94, 0.10),
        0 24px 80px rgba(0, 0, 0, 0.76);
    }
    .project-picker-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 14px;
    }
    h2 { margin: 0; font-size: 22px; letter-spacing: 0; }
    .ready-pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      border: 1px solid rgba(242, 178, 101, 0.26);
      border-radius: 999px;
      padding: 7px 11px;
      color: var(--accent-contrast);
      background: var(--accent);
      font-size: 12px;
      font-weight: 950;
      white-space: nowrap;
      box-shadow: 0 12px 32px var(--accent-glow);
    }
    .ready-pill strong { font-size: 16px; line-height: 1; }
    .ready-pill span { text-transform: uppercase; letter-spacing: 0.08em; }
    .field-project,
    .project-summary {
      --field-accent: #22c55e;
      --field-accent-2: #86efac;
      --field-tint: rgba(34, 197, 94, 0.13);
      --field-border: rgba(34, 197, 94, 0.36);
      --field-icon-text: #052e16;
    }
    .field-title {
      --field-accent: #f59e0b;
      --field-accent-2: #fde68a;
      --field-tint: rgba(245, 158, 11, 0.14);
      --field-border: rgba(245, 158, 11, 0.38);
      --field-icon-text: #2b1602;
    }
    .field-description {
      --field-accent: #06b6d4;
      --field-accent-2: #67e8f9;
      --field-tint: rgba(6, 182, 212, 0.13);
      --field-border: rgba(6, 182, 212, 0.36);
      --field-icon-text: #031f26;
    }
    .field-youtube {
      --field-accent: #ef4444;
      --field-accent-2: #fca5a5;
      --field-tint: rgba(239, 68, 68, 0.13);
      --field-border: rgba(239, 68, 68, 0.40);
      --field-icon-text: #300808;
    }
    .field-facebook {
      --field-accent: #7c3aed;
      --field-accent-2: #c4b5fd;
      --field-tint: rgba(124, 58, 237, 0.13);
      --field-border: rgba(124, 58, 237, 0.40);
      --field-icon-text: #f5f3ff;
    }
    .field-source-comment {
      --field-accent: #7c3aed;
      --field-accent-2: #c4b5fd;
      --field-tint: rgba(124, 58, 237, 0.10);
      --field-border: rgba(124, 58, 237, 0.32);
      --field-icon-text: #f5f3ff;
      max-width: 520px;
    }
    .field { display: grid; gap: 7px; margin: 0; }
    .field > span { color: var(--muted); font-size: 12px; font-weight: 900; }
    .field-label,
    .summary-label {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      justify-self: start;
      color: var(--field-accent-2, var(--muted));
    }
    .field-label-between {
      width: 100%;
      justify-content: space-between;
      gap: 12px;
    }
    .field-label-main {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
    }
    .project-field-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      flex-wrap: wrap;
    }
    .project-video-badge {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border-radius: 999px;
      padding: 7px 11px;
      font-size: 11px;
      font-weight: 950;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      white-space: nowrap;
    }
    .project-video-badge.ready {
      color: #052e16;
      border: 1px solid rgba(34, 197, 94, 0.36);
      background: linear-gradient(135deg, #86efac, #22c55e);
      box-shadow: 0 12px 28px rgba(34, 197, 94, 0.24);
    }
    .project-video-badge.missing {
      color: #32160a;
      border: 1px solid rgba(251, 146, 60, 0.36);
      background: linear-gradient(135deg, #fed7aa, #fb923c);
      box-shadow: 0 12px 28px rgba(251, 146, 60, 0.20);
    }
    .field-icon {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 24px;
      height: 24px;
      border-radius: 9px;
      color: var(--field-icon-text, #170d05);
      background: linear-gradient(135deg, var(--field-accent-2, #fde68a), var(--field-accent, #f2b261));
      font-size: 10px;
      font-weight: 950;
      letter-spacing: 0;
      line-height: 1;
      box-shadow: 0 10px 24px color-mix(in srgb, var(--field-accent, #f2b261) 26%, transparent);
    }
    .field input,
    .field select,
    .field textarea {
      width: 100%;
      min-height: 46px;
      border: 1px solid var(--control-line);
      border-radius: 15px;
      padding: 11px 13px;
      color: var(--text);
      background: var(--field-bg);
      font-family: inherit;
      line-height: 1.45;
    }
    body:not(.theme-light) .field input,
    body:not(.theme-light) .field select,
    body:not(.theme-light) .field textarea {
      border-color: rgba(255, 255, 255, 0.22);
      background: rgba(0, 0, 0, 0.66);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04);
    }
    body:not(.theme-light) .field-project select,
    body:not(.theme-light) .field-title input,
    body:not(.theme-light) .field-description textarea,
    body:not(.theme-light) .field-source-comment input,
    body:not(.theme-light) .field-youtube select,
    body:not(.theme-light) .field-facebook select {
      border-color: var(--field-border);
      background:
        linear-gradient(135deg, var(--field-tint), rgba(255, 255, 255, 0.025)),
        rgba(0, 0, 0, 0.68);
    }
    body:not(.theme-light) .field input:focus,
    body:not(.theme-light) .field select:focus,
    body:not(.theme-light) .field textarea:focus {
      outline: none;
      border-color: rgba(242, 178, 101, 0.58);
      box-shadow:
        0 0 0 3px rgba(242, 178, 101, 0.14),
        inset 0 1px 0 rgba(255, 255, 255, 0.05);
    }
    .platform-card .upload-field.compact.field-youtube,
    .platform-card .upload-field.compact.field-facebook {
      width: min(100%, 260px);
      max-width: 260px;
    }
    .project-select-field select {
      min-height: 48px;
      border-color: rgba(242, 178, 101, 0.22);
      border-radius: 16px;
      background:
        linear-gradient(135deg, rgba(242, 178, 101, 0.08), var(--surface)),
        var(--field-bg);
      font-size: 15px;
      font-weight: 850;
    }
    body:not(.theme-light) .project-select-field select {
      border-color: rgba(34, 197, 94, 0.38);
      background:
        linear-gradient(135deg, rgba(34, 197, 94, 0.14), rgba(255, 255, 255, 0.035)),
        rgba(0, 0, 0, 0.70);
    }
    body:not(.theme-light) .project-select-field {
      border: 1px solid var(--field-border);
      border-radius: 18px;
      padding: 10px;
      background:
        linear-gradient(135deg, var(--field-tint), rgba(255, 255, 255, 0.025)),
        rgba(0, 0, 0, 0.24);
    }
    .field textarea { resize: vertical; min-height: 150px; }
    .field-source-comment input {
      min-height: 44px;
      padding-block: 9px;
      font-size: 14px;
    }
    .copy-field-btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      flex: 0 0 auto;
      width: 32px;
      height: 32px;
      border: 1px solid var(--field-border);
      border-radius: 11px;
      padding: 0;
      color: var(--field-accent-2);
      background:
        linear-gradient(135deg, rgba(255, 255, 255, 0.88), rgba(255, 255, 255, 0.62)),
        var(--surface);
      font: inherit;
      font-size: 11px;
      font-weight: 950;
      letter-spacing: 0.02em;
      cursor: pointer;
      transition: transform 120ms ease, border-color 120ms ease, box-shadow 120ms ease;
      white-space: nowrap;
    }
    .copy-field-btn:hover {
      transform: translateY(-1px);
      border-color: color-mix(in srgb, var(--field-accent) 72%, white 28%);
      box-shadow: 0 10px 24px color-mix(in srgb, var(--field-accent) 20%, transparent);
    }
    .copy-field-btn:focus-visible {
      outline: none;
      box-shadow:
        0 0 0 3px color-mix(in srgb, var(--field-accent) 22%, transparent),
        0 10px 24px color-mix(in srgb, var(--field-accent) 18%, transparent);
    }
    .copy-field-btn .btn-icon {
      font-size: 14px;
      line-height: 1;
    }
    body:not(.theme-light) .copy-field-btn {
      border-color: var(--field-border);
      background:
        linear-gradient(135deg, color-mix(in srgb, var(--field-tint) 76%, rgba(255, 255, 255, 0.03)), rgba(0, 0, 0, 0.28)),
        rgba(0, 0, 0, 0.42);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.05);
    }
    body:not(.theme-light) .copy-field-btn:hover {
      border-color: color-mix(in srgb, var(--field-accent-2) 66%, transparent);
    }
    .project-summary {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      padding: 0;
    }
    body:not(.theme-light) .project-summary {
      border: 1px solid rgba(34, 197, 94, 0.24);
      border-radius: 16px;
      padding: 12px 14px;
      background:
        linear-gradient(135deg, rgba(34, 197, 94, 0.10), rgba(255, 255, 255, 0.035)),
        rgba(0, 0, 0, 0.34);
    }
    .project-summary > div { min-width: 0; }
    .summary-label {
      display: inline-flex;
      margin-bottom: 7px;
      color: var(--field-accent-2);
      font-size: 11px;
      font-weight: 900;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }
    .project-summary strong {
      display: block;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      margin-bottom: 3px;
      font-size: 20px;
      letter-spacing: 0;
    }
    .quick-links { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 2px; }
    .status,
    .upload-empty,
    .upload-status,
    .upload-result {
      border: 1px solid var(--control-line-soft);
      border-radius: 14px;
      padding: 10px 11px;
      color: var(--status-text);
      background: var(--surface);
      font-size: 13px;
      line-height: 1.45;
    }
    .status.good,
    .upload-status.good,
    .upload-result.good { border-color: rgba(242, 178, 101, 0.34); color: var(--good-text); }
    .status.bad,
    .upload-status.bad,
    .upload-result.bad { border-color: rgba(255, 82, 82, 0.40); color: var(--danger-text); }
    .status.warn,
    .upload-status.warn,
    .upload-result.warn { border-color: rgba(255, 171, 64, 0.34); color: var(--warn-text); }
    .upload-empty { display: grid; gap: 8px; }
    .upload-empty strong { color: var(--warn-text); }
    .upload-panel {
      border: 1px solid rgba(242, 178, 101, 0.16);
      border-radius: 22px;
      padding: 14px;
      background:
        radial-gradient(circle at 20% 0%, rgba(242, 178, 101, 0.10), transparent 40%),
        var(--surface-panel);
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
    }
    body:not(.theme-light) .upload-panel {
      border-color: rgba(168, 85, 247, 0.42);
      background:
        radial-gradient(circle at 50% 0%, rgba(168, 85, 247, 0.18), transparent 34%),
        linear-gradient(150deg, rgba(26, 17, 39, 0.97), rgba(6, 5, 12, 0.98));
      box-shadow:
        0 0 0 1px rgba(168, 85, 247, 0.10),
        0 24px 80px rgba(0, 0, 0, 0.78);
    }
    .upload-head { text-align: center; margin-bottom: 8px; }
    .upload-head h3 { margin: 0 0 5px; font-size: 20px; letter-spacing: 0; }
    .upload-head span { display: block; color: var(--text-faint); font-size: 12px; line-height: 1.45; }
    .upload-field { margin-top: 8px; }
    body:not(.theme-light) .upload-panel .upload-field {
      border: 1px solid var(--field-border);
      border-radius: 17px;
      padding: 10px;
      background:
        radial-gradient(circle at 0% 0%, var(--field-tint), transparent 34%),
        rgba(0, 0, 0, 0.24);
    }
    .upload-field.compact { max-width: none; }
    .platform-grid {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      gap: clamp(12px, 1.4vw, 22px);
      margin-top: 0;
      align-items: stretch;
    }
    .platform-card {
      border: 1px solid var(--field-border);
      border-radius: 18px;
      padding: 12px;
      background:
        radial-gradient(circle at 0% 0%, var(--field-tint), transparent 36%),
        var(--surface);
    }
    .platform-youtube { --field-accent: #ef4444; --field-accent-2: #fca5a5; --field-tint: rgba(239, 68, 68, 0.12); --field-border: rgba(239, 68, 68, 0.32); --field-icon-text: #300808; }
    .platform-facebook { --field-accent: #7c3aed; --field-accent-2: #c4b5fd; --field-tint: rgba(124, 58, 237, 0.12); --field-border: rgba(124, 58, 237, 0.32); --field-icon-text: #f5f3ff; }
    .platform-card-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 10px;
    }
    .platform-title {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: var(--field-accent-2);
      font-size: 13px;
      font-weight: 950;
      letter-spacing: 0;
    }
    .platform-guide-link {
      min-height: 34px;
      padding: 7px 10px;
      flex: 0 0 auto;
      border-color: var(--field-border);
      font-size: 11px;
    }
    .platform-guide-link .btn-icon {
      color: #fff8ed;
      background: linear-gradient(135deg, var(--field-accent-2), var(--field-accent));
      box-shadow: 0 7px 18px color-mix(in srgb, var(--field-accent, #f2b261) 24%, transparent);
    }
    .platform-account-list {
      display: grid;
      gap: 8px;
      margin-top: 10px;
      position: relative;
      z-index: 3;
    }
    .platform-account-list.open {
      z-index: 80;
    }
    .platform-account {
      display: grid;
      grid-template-columns: 42px minmax(0, 1fr);
      align-items: center;
      gap: 10px;
      width: 100%;
      border: 1px solid var(--field-border);
      border-radius: 16px;
      padding: 9px;
      background: var(--surface);
      color: inherit;
      font: inherit;
      text-align: left;
    }
    button.platform-account {
      cursor: pointer;
      transition: transform 0.16s ease, border-color 0.16s ease, box-shadow 0.16s ease, background 0.16s ease;
    }
    button.platform-account:hover:not(:disabled) {
      transform: translateY(-1px);
      border-color: var(--field-accent-2);
      box-shadow: 0 14px 30px color-mix(in srgb, var(--field-accent, #f2b261) 16%, transparent);
    }
    button.platform-account:disabled {
      cursor: default;
      opacity: 0.78;
    }
    .platform-account.active {
      border-color: var(--field-accent-2);
      background:
        linear-gradient(135deg, var(--field-tint), rgba(255, 255, 255, 0.04)),
        var(--surface);
      box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--field-accent, #f2b261) 18%, transparent);
    }
    .platform-account-trigger {
      grid-template-columns: 42px minmax(0, 1fr) auto;
    }
    .account-chevron {
      display: inline-flex !important;
      align-items: center;
      justify-content: center;
      min-width: auto !important;
      overflow: visible !important;
      color: var(--text-faint);
      font-size: 18px;
      font-weight: 950;
      line-height: 1;
      transition: transform 0.16s ease;
    }
    .platform-account-list.open .account-chevron {
      transform: rotate(180deg);
    }
    .platform-account-options {
      position: absolute;
      top: calc(100% + 8px);
      left: 0;
      right: 0;
      display: none;
      gap: 7px;
      z-index: 20;
      border: 1px solid var(--field-border);
      border-radius: 18px;
      padding: 8px;
      background: #fff7ea;
      box-shadow: 0 22px 48px rgba(0, 0, 0, 0.24);
    }
    .platform-account-list.open .platform-account-options {
      display: grid;
    }
    .platform-account-option {
      border-color: rgba(79, 57, 31, 0.12);
      background: #fffbf4;
    }
    body:not(.theme-light) .platform-account-options {
      background: #111318;
      box-shadow: 0 24px 54px rgba(0, 0, 0, 0.46);
    }
    body:not(.theme-light) .platform-account-option {
      border-color: rgba(255, 255, 255, 0.10);
      background: #171a21;
    }
    .platform-account img {
      width: 42px;
      height: 42px;
      border-radius: 999px;
      object-fit: cover;
      background: var(--surface-strong);
    }
    .platform-account strong,
    .platform-account span {
      display: block;
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .platform-account strong {
      color: var(--text);
      font-size: 13px;
      font-weight: 950;
    }
    .platform-account span {
      margin-top: 3px;
      color: var(--text-faint);
      font-size: 11px;
      font-weight: 800;
    }
    .platform-card .upload-field {
      margin-top: 10px;
      padding: 0;
      border: 0;
      background: transparent;
    }
    .platform-card .field-source-comment {
      max-width: 520px;
    }
    .platform-card .field-source-comment input {
      min-height: 44px;
      padding-block: 9px;
      font-size: 14px;
    }
    .facebook-config-field {
      display: grid;
      grid-template-columns: minmax(120px, 0.64fr) minmax(0, 1fr);
      align-items: center;
      gap: 9px;
      margin: 0 !important;
    }
    .facebook-config-field input {
      min-height: 42px;
      font-size: 13px;
    }
    .config-actions {
      align-items: center;
      margin-top: 0;
    }
    .modal-backdrop {
      position: fixed;
      inset: 0;
      z-index: 200;
      display: grid;
      place-items: center;
      padding: 24px;
      background: rgba(11, 7, 3, 0.44);
      backdrop-filter: blur(18px);
    }
    .modal-card {
      position: relative;
      width: min(100%, 560px);
      border: 1px solid var(--field-border);
      border-radius: 24px;
      padding: 22px;
      background:
        radial-gradient(circle at 0% 0%, var(--field-tint), transparent 36%),
        var(--panel);
      box-shadow: 0 28px 90px rgba(0, 0, 0, 0.42);
    }
    .facebook-config-modal {
      width: min(100%, 720px);
      border-color: rgba(124, 58, 237, 0.34);
      color: #21170f;
      background:
        radial-gradient(circle at 10% 16%, rgba(124, 58, 237, 0.17), transparent 34%),
        radial-gradient(circle at 92% 0%, rgba(251, 146, 60, 0.18), transparent 34%),
        linear-gradient(145deg, #fffaf1 0%, #f4ebff 56%, #fff7e6 100%);
      box-shadow:
        0 34px 95px rgba(44, 31, 18, 0.34),
        inset 0 0 0 1px rgba(255, 255, 255, 0.62);
    }
    body:not(.theme-light) .modal-card {
      background:
        radial-gradient(circle at 12% 0%, rgba(168, 85, 247, 0.20), transparent 38%),
        linear-gradient(150deg, rgba(26, 17, 39, 0.98), rgba(6, 5, 12, 0.99));
      box-shadow: 0 32px 100px rgba(0, 0, 0, 0.72);
    }
    body:not(.theme-light) .facebook-config-modal {
      border-color: rgba(168, 85, 247, 0.48);
      color: #fff7ed;
      background:
        radial-gradient(circle at 10% 12%, rgba(168, 85, 247, 0.28), transparent 34%),
        radial-gradient(circle at 92% 0%, rgba(249, 115, 22, 0.16), transparent 36%),
        linear-gradient(145deg, #18111f 0%, #27172e 58%, #100f15 100%);
      box-shadow:
        0 34px 110px rgba(0, 0, 0, 0.78),
        inset 0 0 0 1px rgba(255, 255, 255, 0.08);
    }
    .modal-card h3 {
      margin: 0 0 8px;
      font-size: 30px;
      letter-spacing: 0;
    }
    .facebook-config-modal .kicker,
    .facebook-config-modal h3,
    .facebook-config-modal .field-label,
    .facebook-config-modal .field-label span {
      color: currentColor;
    }
    .modal-copy {
      margin: 0 0 16px;
      color: var(--text-faint);
      font-size: 13px;
      line-height: 1.45;
      font-weight: 750;
    }
    .facebook-config-modal .modal-copy {
      max-width: 610px;
      color: rgba(33, 23, 15, 0.74);
      font-size: 14px;
      font-weight: 850;
    }
    body:not(.theme-light) .facebook-config-modal .modal-copy {
      color: rgba(255, 247, 237, 0.78);
    }
    .modal-copy code {
      color: var(--text);
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 12px;
    }
    .facebook-config-modal .modal-copy code {
      color: #4c1d95;
      background: rgba(255, 255, 255, 0.58);
      border-radius: 8px;
      padding: 2px 5px;
      font-weight: 950;
    }
    body:not(.theme-light) .facebook-config-modal .modal-copy code {
      color: #fde68a;
      background: rgba(0, 0, 0, 0.28);
    }
    .facebook-config-modal .facebook-config-field {
      border: 1px solid rgba(124, 58, 237, 0.20);
      border-radius: 18px;
      padding: 10px 12px;
      background: rgba(255, 255, 255, 0.52);
      box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.40);
    }
    .facebook-config-modal .facebook-config-field + .facebook-config-field {
      margin-top: 10px !important;
    }
    .facebook-config-modal .facebook-config-field input {
      color: #21170f;
      border-color: rgba(94, 71, 45, 0.24);
      background: rgba(255, 252, 247, 0.95);
    }
    .facebook-config-modal .facebook-config-field input::placeholder {
      color: rgba(33, 23, 15, 0.52);
    }
    body:not(.theme-light) .facebook-config-modal .facebook-config-field {
      border-color: rgba(255, 255, 255, 0.13);
      background: rgba(0, 0, 0, 0.22);
      box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.05);
    }
    body:not(.theme-light) .facebook-config-modal .facebook-config-field input {
      color: #fff7ed;
      border-color: rgba(255, 255, 255, 0.18);
      background: rgba(0, 0, 0, 0.38);
    }
    .modal-close {
      position: absolute;
      top: 14px;
      right: 14px;
      width: 34px;
      height: 34px;
      border: 1px solid var(--control-line);
      border-radius: 999px;
      color: var(--text);
      background: var(--surface);
      font: inherit;
      font-size: 20px;
      font-weight: 900;
      cursor: pointer;
    }
    .modal-actions {
      display: flex;
      justify-content: flex-end;
      gap: 9px;
      margin-top: 16px;
    }
    body:not(.theme-light) .upload-panel .platform-card .upload-field {
      padding: 0;
      border: 0;
      background: transparent;
    }
    .platform-actions {
      display: flex;
      align-items: center;
      justify-content: flex-start;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 10px;
    }
    .final-upload-actions {
      display: flex;
      justify-content: center;
      align-items: center;
      flex-wrap: wrap;
      gap: 9px;
      margin-top: 18px;
      padding-top: 14px;
      border-top: 1px solid var(--control-line-soft);
    }
    .upload-btn,
    .refresh-btn,
    .icon-btn {
      min-height: 40px;
      border: 1px solid var(--control-line);
      border-radius: 13px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 7px;
      color: var(--text-button);
      background: var(--surface);
      font-size: 12px;
      font-weight: 950;
      cursor: pointer;
      text-decoration: none;
    }
    .platform-actions .upload-btn {
      width: auto;
      min-height: 38px;
      padding: 8px 12px;
      flex: 0 0 auto;
      justify-content: center;
    }
    .refresh-btn {
      color: var(--accent-contrast);
      background: linear-gradient(135deg, var(--accent), #d88435);
      border-color: transparent;
      box-shadow: 0 12px 32px var(--accent-glow);
    }
    .refresh-btn .btn-icon {
      color: #170d05;
      background: rgba(255, 245, 230, 0.58);
      box-shadow: none;
    }
    .theme-toggle .btn-icon {
      color: #fff8ed;
      background: linear-gradient(135deg, #a78bfa, #7c3aed);
      box-shadow: 0 7px 18px rgba(124, 58, 237, 0.24);
    }
    #refreshProjects .btn-icon {
      color: #f5f9ff;
      background: linear-gradient(135deg, #c084fc, #7c3aed);
      box-shadow: 0 7px 18px rgba(124, 58, 237, 0.24);
    }
    .upload-btn.youtube { border-color: rgba(255, 82, 82, 0.34); background: rgba(255, 82, 82, 0.12); }
    .upload-btn.facebook { border-color: rgba(74, 144, 226, 0.34); }
    .upload-btn.both {
      min-width: min(100%, 360px);
      min-height: 44px;
      padding: 10px 18px;
      border-color: rgba(34, 197, 94, 0.42);
      background: rgba(34, 197, 94, 0.14);
    }
    body:not(.theme-light) .small-link {
      border: 1px solid rgba(255, 255, 255, 0.14);
      background: rgba(255, 255, 255, 0.10);
    }
    .upload-btn:disabled { cursor: not-allowed; opacity: 0.46; }
    .btn-icon {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 20px;
      height: 20px;
      border-radius: 999px;
      color: #fff8ed;
      background: linear-gradient(135deg, #f2b261, #d88435);
      font-size: 11px;
      font-weight: 950;
      line-height: 1;
      box-shadow: 0 7px 18px rgba(242, 178, 101, 0.22);
      text-shadow: none;
    }
    .upload-btn.youtube .btn-icon {
      color: #fff5f5;
      background: linear-gradient(135deg, #ff5252, #b71c1c);
      box-shadow: 0 7px 18px rgba(255, 82, 82, 0.22);
      text-shadow: none;
    }
    .upload-btn.facebook .btn-icon {
      color: #fff8ed;
      background: linear-gradient(135deg, #c084fc, #7c3aed);
      box-shadow: 0 7px 18px rgba(124, 58, 237, 0.24);
      text-shadow: none;
    }
    .upload-btn.both .btn-icon {
      color: #052e16;
      background: linear-gradient(135deg, #86efac, #22c55e);
      box-shadow: 0 7px 18px rgba(34, 197, 94, 0.24);
      text-shadow: none;
    }
    .upload-status {
      width: fit-content;
      max-width: min(100%, 860px);
      margin: 14px auto 0;
    }
    .upload-result { margin-top: 10px; }
    .small-link { color: var(--text); background: var(--surface-strong); }
    .upload-result a { color: var(--accent); font-weight: 900; background: transparent; padding: 0; }
    [hidden] { display: none !important; }
    @media (max-width: 980px) {
      .upload-header { display: grid; }
      .header-tools { justify-content: stretch; justify-self: stretch; width: 100%; }
    }
    @media (max-width: 720px) {
      body { padding: 20px 14px; }
      .project-picker-head,
      .project-summary { align-items: flex-start; flex-direction: column; }
      .upload-header { grid-template-columns: 1fr; gap: 16px; }
      .upload-title-block { min-height: 0; }
      .header-tools { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .platform-card-head { align-items: flex-start; flex-direction: column; }
      .platform-grid { grid-template-columns: 1fr; }
      .facebook-config-field { grid-template-columns: 1fr; }
      .modal-actions { flex-direction: column-reverse; }
      .modal-actions .upload-btn { width: 100%; }
    }
""",
        extra_script=f"""
  <script>
    window.__PROJECTS__ = {json.dumps(projects, ensure_ascii=False)};
    window.__INITIAL_PROJECT__ = {json.dumps(selected_project, ensure_ascii=False)};
    window.__PROJECT_SOURCE_ROOT__ = {json.dumps(str(SLIDE_ROOT), ensure_ascii=False)};
  </script>
  <script src="/web/render_page.js?v=20260607-ai-storyboard-v1"></script>
""",
    )


def render_page_shell(title: str, body: str, extra_style: str = "", extra_script: str = "") -> bytes:
    lang = active_language()
    html_text = f"""<!DOCTYPE html>
<html lang="{html.escape(lang)}">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{html.escape(title)}</title>
  <link rel="icon" href="/web/viro-icon.svg?v=20260607-viro-icon" type="image/svg+xml" />
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Be+Vietnam+Pro:wght@400;500;600;700;800;900&display=swap" rel="stylesheet" />
  <style>
    :root {{
      color-scheme: dark;
      --bg: #020403;
      --panel: rgba(7, 11, 9, 0.88);
      --line: rgba(242, 178, 101, 0.16);
      --text: #f4fff8;
      --muted: rgba(244, 255, 248, 0.62);
      --accent: #f2b261;
      --accent-contrast: #170d05;
      --body-bg:
        radial-gradient(circle at 16% 10%, rgba(242, 178, 101, 0.12), transparent 24rem),
        radial-gradient(circle at 90% 14%, rgba(255, 171, 64, 0.08), transparent 24rem),
        linear-gradient(135deg, #000, var(--bg));
      --surface: rgba(255, 255, 255, 0.045);
      --surface-strong: rgba(255, 255, 255, 0.075);
      --surface-panel: rgba(0, 0, 0, 0.42);
      --field-bg: rgba(0, 0, 0, 0.42);
      --control-line: rgba(255, 255, 255, 0.12);
      --control-line-soft: rgba(255, 255, 255, 0.1);
      --control-line-faint: rgba(255, 255, 255, 0.11);
      --text-soft: rgba(246, 255, 249, 0.78);
      --text-faint: rgba(246, 255, 249, 0.62);
      --text-button: rgba(246, 255, 249, 0.86);
      --status-text: rgba(246, 255, 249, 0.74);
      --good-text: #ffd59a;
      --warn-text: #ffd699;
      --danger-text: #ffb8b8;
      --delete-text: #ffd6d6;
      --warning-text: #ffe4ba;
      --shadow: 0 22px 70px rgba(0, 0, 0, 0.42);
      --accent-glow: rgba(242, 178, 101, 0.20);
      --font-ui: "Be Vietnam Pro", ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    body.theme-light {{
      color-scheme: light;
      --bg: #f3e6cf;
      --panel: rgba(255, 248, 236, 0.88);
      --line: rgba(79, 57, 31, 0.16);
      --text: #20170f;
      --muted: rgba(32, 23, 15, 0.62);
      --accent: #a4622a;
      --accent-contrast: #fff8ea;
      --body-bg:
        radial-gradient(circle at 18% 12%, rgba(164, 98, 42, 0.14), transparent 28rem),
        radial-gradient(circle at 88% 18%, rgba(204, 136, 52, 0.16), transparent 24rem),
        linear-gradient(135deg, #fff8eb, var(--bg));
      --surface: rgba(79, 57, 31, 0.055);
      --surface-strong: rgba(79, 57, 31, 0.09);
      --surface-panel: rgba(255, 248, 236, 0.72);
      --field-bg: rgba(255, 251, 244, 0.86);
      --control-line: rgba(79, 57, 31, 0.16);
      --control-line-soft: rgba(79, 57, 31, 0.12);
      --control-line-faint: rgba(79, 57, 31, 0.11);
      --text-soft: rgba(32, 23, 15, 0.78);
      --text-faint: rgba(32, 23, 15, 0.62);
      --text-button: rgba(32, 23, 15, 0.84);
      --status-text: rgba(32, 23, 15, 0.72);
      --good-text: #8f4f19;
      --warn-text: #885100;
      --danger-text: #9f2e2e;
      --delete-text: #842f2f;
      --warning-text: #754900;
      --shadow: 0 22px 70px rgba(78, 54, 28, 0.13);
      --accent-glow: rgba(164, 98, 42, 0.16);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: var(--font-ui);
      font-feature-settings: "ss01" 1, "cv01" 1;
      color: var(--text);
      background: var(--body-bg);
      padding: 44px min(5vw, 72px);
    }}
    header {{ max-width: 920px; margin-bottom: 34px; }}
    h1 {{ margin: 0 0 10px; font-size: clamp(36px, 6vw, 76px); line-height: 0.92; letter-spacing: 0; }}
    header p {{ max-width: 720px; color: var(--muted); font-size: 17px; line-height: 1.6; }}
    code {{ color: var(--accent); }}
    a {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border-radius: 999px;
      padding: 10px 14px;
      color: var(--accent-contrast);
      background: var(--accent);
      text-decoration: none;
      font-weight: 800;
    }}
    .small-link {{ color: var(--text); background: var(--surface-strong); }}
{extra_style}
  </style>
</head>
<body class="theme-light">
{body}
{extra_script}
</body>
</html>
"""
    return html_text.encode("utf-8")


def slide_url_to_path(request_path: str) -> Path:
    path = urlparse(request_path).path
    if path == "/slide":
        return SLIDE_ROOT
    if not path.startswith("/slide/"):
        raise ValueError("Not a slide URL.")

    tail = path[len("/slide/"):]
    project_part, _, relative_part = tail.partition("/")
    project = validate_project_name(project_part)
    project_dir = require_slide_project(project)
    if not relative_part:
        return project_dir

    relative_text = unquote(relative_part)
    if "\x00" in relative_text:
        raise ValueError("Invalid slide asset path.")
    target = (project_dir / relative_text).resolve()
    try:
        target.relative_to(project_dir.resolve())
    except ValueError as exc:
        raise ValueError("Invalid slide asset path.") from exc
    return target


def slide_index_request_project(request_path: str) -> str | None:
    path = urlparse(request_path).path
    match = re.fullmatch(r"/slide/([^/]+)/(?:index\.html)?", path)
    if not match:
        return None
    return validate_project_name(unquote(match.group(1)))


def render_slide_index_html(request_path: str) -> bytes:
    project = slide_index_request_project(request_path)
    if not project:
        raise ValueError("Not a slide index URL.")
    project_dir = require_slide_project(project)
    html_text = (project_dir / "index.html").read_text(encoding="utf-8")
    script_tag = '<script src="/web/slide_media_runtime.js?v=20260607-studio-preview-v1"></script>'
    if "slide_media_runtime.js" not in html_text:
        html_text = html_text.replace("</body>", f"{script_tag}\n</body>", 1)
    return html_text.encode("utf-8")



class WebHandler(SimpleHTTPRequestHandler):
    server_version = "VideoTemplateWeb/1.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(REPO_ROOT), **kwargs)

    def handle(self) -> None:
        try:
            super().handle()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return

    def translate_path(self, path: str) -> str:
        parsed_path = urlparse(path).path
        if parsed_path == "/slide" or parsed_path.startswith("/slide/"):
            try:
                return str(slide_url_to_path(path))
            except Exception:
                return str(REPO_ROOT / "__missing_slide_asset__")
        return super().translate_path(path)

    def log_message(self, format: str, *args: object) -> None:
        timestamp = time.strftime("%H:%M:%S")
        if format == '"%s" %s %s' and len(args) >= 3:
            request_line = str(args[0])
            parts = request_line.split()
            method = parts[0] if parts else self.command
            target = parts[1] if len(parts) > 1 else self.path
            status = str(args[1])
            size = str(args[2])
            try:
                status_code = int(status)
            except ValueError:
                status_code = 0
            if 200 <= status_code < 300:
                icon, status_style = "✅", "green"
            elif 300 <= status_code < 400:
                icon, status_style = "↪", "cyan"
            elif 400 <= status_code < 500:
                icon, status_style = "⚠️", "yellow"
            else:
                icon, status_style = "❌", "red"
            method_style = "blue" if method == "GET" else "magenta"
            size_text = "" if size == "-" else f" · {size}B"
            print(
                f"{icon} {color_text(timestamp, 'dim')}  "
                f"{color_text(method, 'bold', method_style)} {color_text(target, 'cyan')}  "
                f"{color_text(status, 'bold', status_style)}{color_text(size_text, 'dim')}",
                file=sys.stderr,
            )
            return

        print(f"ℹ️  {color_text(timestamp, 'dim')}  {format % args}", file=sys.stderr)

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        parsed_path = urlparse(self.path).path
        if self.path.startswith("/web/") or (parsed_path.startswith("/slide/") and parsed_path.endswith((".js", ".css"))):
            self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.end_headers()

    def send_json(self, status: int, payload: object) -> None:
        data = json_dumps(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_html(self, status: int, data: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_binary(self, status: int, data: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_download(self, status: int, data: bytes, content_type: str, filename: str) -> None:
        safe_filename = re.sub(r'[^A-Za-z0-9._-]+', "-", str(filename or "download.bin")).strip("-") or "download.bin"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'attachment; filename="{safe_filename}"')
        self.end_headers()
        self.wfile.write(data)

    def send_redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            raise ValueError("Missing request body.")
        if length > MAX_UPLOAD_BYTES * 2:
            raise ValueError("Request body is too large.")
        data = self.rfile.read(length)
        return json.loads(data.decode("utf-8"))

    def proxy_elevenlabs_rotator_request(self, parsed) -> None:
        config = elevenlabs_config()
        expected_proxy_key = elevenlabs_proxy_key(config)
        provided_proxy_key = str(self.headers.get("X-Proxy-Key") or "").strip()
        if not expected_proxy_key or provided_proxy_key != expected_proxy_key:
            self.send_json(401, {"error": "Invalid Viro Key Rotate proxy key."})
            return

        relative_path = parsed.path[len(BUILTIN_ELEVENLABS_ROTATOR_PATH):]
        if not relative_path.startswith("/v1/"):
            self.send_json(404, {"error": "Unsupported ElevenLabs proxy path."})
            return
        content_length = int(self.headers.get("Content-Length") or "0")
        if content_length <= 0:
            self.send_json(400, {"error": "Missing proxy request body."})
            return
        if content_length > MAX_UPLOAD_BYTES:
            self.send_json(413, {"error": "Proxy request body is too large."})
            return
        body = self.rfile.read(content_length)
        keys = usable_elevenlabs_key_connections()
        if not keys:
            self.send_json(400, {"error": "No ElevenLabs API key in Secret Hub."})
            return

        query = f"?{parsed.query}" if parsed.query else ""
        target_url = f"https://api.elevenlabs.io{relative_path}{query}"
        retry_statuses = {401, 403, 408, 409, 425, 429, 500, 502, 503, 504}
        last_error_body = b""
        last_error_code = 502
        last_error_type = "application/json; charset=utf-8"

        for attempt in range(len(keys)):
            key = select_elevenlabs_rotator_key()
            headers = {
                "Accept": self.headers.get("Accept") or "audio/mpeg",
                "Content-Type": self.headers.get("Content-Type") or "application/json",
                "User-Agent": "ViroBuiltInKeyRotate/1.0",
                "xi-api-key": key["secret_value"],
            }
            request = Request(target_url, data=body, headers=headers, method=self.command)
            try:
                with urlopen(request, timeout=180) as response:
                    response_body = response.read()
                    content_type = response.headers.get("Content-Type") or "application/octet-stream"
                    self.send_binary(int(response.status), response_body, content_type)
                    return
            except HTTPError as exc:
                last_error_code = int(exc.code)
                last_error_type = exc.headers.get("Content-Type") or "application/json; charset=utf-8"
                last_error_body = exc.read()
                if exc.code not in retry_statuses or attempt >= len(keys) - 1:
                    break
            except URLError as exc:
                last_error_code = 502
                last_error_type = "application/json; charset=utf-8"
                last_error_body = json_dumps({"error": f"ElevenLabs proxy request failed: {exc.reason}"})
                if attempt >= len(keys) - 1:
                    break

        if not last_error_body:
            last_error_body = json_dumps({"error": "ElevenLabs proxy request failed."})
        self.send_binary(last_error_code, last_error_body, last_error_type)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        REQUEST_CONTEXT.language = request_language(self.headers, parsed)
        REQUEST_CONTEXT.origin = request_origin(self.headers)

        if path in {"/favicon.ico", "/web/favicon.ico"}:
            self.send_redirect("/web/viro-icon.svg?v=20260607-viro-icon")
            return

        if slide_index_request_project(self.path):
            try:
                self.send_html(200, render_slide_index_html(self.path))
            except Exception as exc:
                self.send_html(404, render_app_shell(
                    title="Slide not found | Viro",
                    active="projects",
                    main_html=f'<div class="page-frame"><header class="page-head"><p class="eyebrow">Project</p><h1>Không mở được slide</h1><p>{html.escape(str(exc))}</p></header></div>',
                    selected_project=None,
                    projects=list_projects(),
                ))
            return

        if path in {"/", "/home", "/home/"}:
            selected = (parse_qs(parsed.query).get("project") or [None])[0]
            self.send_html(200, render_app_home_html(selected))
            return

        if path in {"/studio", "/studio/"}:
            query = parse_qs(parsed.query)
            selected = (query.get("project") or [None])[0]
            selected_template = (query.get("template") or [None])[0]
            input_mode = (query.get("inputMode") or ["ai"])[0]
            self.send_html(200, render_studio_html(input_mode, selected, selected_template))
            return

        if path in {"/templates", "/templates/"}:
            selected_template = (parse_qs(parsed.query).get("template") or [None])[0]
            self.send_html(200, render_templates_html(selected_template))
            return

        template_edit_match = re.fullmatch(r"/template/([^/]+)/edit/?", path)
        if template_edit_match:
            template_name = unquote(template_edit_match.group(1))
            try:
                self.send_html(200, render_template_editor_html(template_name))
            except FileNotFoundError as exc:
                self.send_html(404, render_app_shell(
                    title="Template not found | Viro",
                    active="templates",
                    main_html=f'<div class="page-frame"><header class="page-head"><p class="eyebrow">Template</p><h1>Không tìm thấy template</h1><p>{html.escape(str(exc))}</p></header></div>',
                    selected_project=None,
                    projects=list_projects(),
                ))
            return

        template_detail_match = re.fullmatch(r"/template/([^/]+)/?", path)
        if template_detail_match:
            template_name = unquote(template_detail_match.group(1))
            try:
                self.send_html(200, render_template_detail_html(template_name))
            except FileNotFoundError as exc:
                self.send_html(404, render_app_shell(
                    title="Template not found | Viro",
                    active="templates",
                    main_html=f'<div class="page-frame"><header class="page-head"><p class="eyebrow">Template</p><h1>Không tìm thấy template</h1><p>{html.escape(str(exc))}</p></header></div>',
                    selected_project=None,
                    projects=list_projects(),
                ))
            return

        if path in {"/projects", "/projects/"}:
            selected = (parse_qs(parsed.query).get("project") or [None])[0]
            self.send_html(200, render_projects_html(selected))
            return

        if path in {"/connections", "/connections/", "/secret-hub", "/secret-hub/"}:
            selected = (parse_qs(parsed.query).get("project") or [None])[0]
            self.send_html(200, render_connections_html(selected))
            return

        if path in {"/platforms", "/platforms/"}:
            selected = (parse_qs(parsed.query).get("project") or [None])[0]
            self.send_html(200, render_platforms_html(selected))
            return

        if path in {"/upload", "/upload/"}:
            selected = (parse_qs(parsed.query).get("project") or [None])[0]
            target = "/platforms"
            if selected:
                target += f"?project={quote(selected)}"
            self.send_redirect(target)
            return

        if path in {"/upload-guide/youtube", "/upload-guide/youtube/"}:
            self.send_html(200, render_social_upload_guide_html("youtube"))
            return

        if path in {"/upload-guide/facebook", "/upload-guide/facebook/"}:
            self.send_html(200, render_social_upload_guide_html("facebook"))
            return

        if path in {"/elevenlabs-guide", "/elevenlabs-guide/"}:
            self.send_html(200, render_elevenlabs_guide_html())
            return

        if path == "/api/health":
            self.send_json(
                200,
                {
                    "ok": True,
                    "root": str(REPO_ROOT),
                    "source_root": str(SLIDE_ROOT),
                    "source_mode": source_root_mode(),
                    "projects": len(list_projects()),
                    "templates": len(list_templates()),
                },
            )
            return

        if path == "/api/source-root":
            self.send_json(200, source_root_response())
            return

        if path == "/api/projects":
            self.send_json(200, {"projects": list_projects()})
            return

        if path == "/api/templates/export":
            template_name = (parse_qs(parsed.query).get("template") or [""])[0]
            try:
                archive = export_template_archive(template_name)
                self.send_download(200, archive["data"], archive["content_type"], archive["filename"])
            except FileNotFoundError as exc:
                self.send_json(404, {"error": str(exc)})
            except Exception as exc:
                self.send_json(400, {"error": str(exc)})
            return

        if path == "/api/templates":
            self.send_json(200, template_response())
            return

        if path == "/api/templates/edit":
            template_name = (parse_qs(parsed.query).get("template") or [""])[0]
            try:
                self.send_json(200, template_detail_response(template_name))
            except Exception as exc:
                self.send_json(400, {"error": str(exc)})
            return

        if path == "/api/connections":
            try:
                self.send_json(200, connections_response())
            except Exception as exc:
                self.send_json(400, {"error": str(exc)})
            return

        if path == "/api/social/status":
            try:
                self.send_json(200, social_status())
            except Exception as exc:
                self.send_json(400, {"error": str(exc)})
            return

        if path == "/api/tts/elevenlabs/config":
            try:
                self.send_json(200, elevenlabs_studio_public_config(getattr(REQUEST_CONTEXT, "origin", "")))
            except Exception as exc:
                self.send_json(400, {"error": str(exc)})
            return

        if path in {"/api/rotator/elevenlabs", "/api/rotator/elevenlabs/health"}:
            try:
                data = elevenlabs_studio_public_config(getattr(REQUEST_CONTEXT, "origin", ""))
                self.send_json(
                    200,
                    {
                        "ok": True,
                        "provider": "elevenlabs",
                        "base_url": data.get("builtin_rotator_base_url"),
                        "configured": data.get("builtin_rotator_configured"),
                        "registry_key_count": data.get("registry_key_count"),
                    },
                )
            except Exception as exc:
                self.send_json(400, {"error": str(exc)})
            return

        if path == "/api/social/upload-metadata":
            project = (parse_qs(parsed.query).get("project") or [""])[0]
            try:
                require_slide_project(project)
                self.send_json(200, build_upload_metadata(project))
            except FileNotFoundError as exc:
                self.send_json(404, {"error": str(exc)})
            except Exception as exc:
                self.send_json(400, {"error": str(exc)})
            return

        if path == "/api/social/youtube/connect":
            project = (parse_qs(parsed.query).get("project") or [""])[0]
            try:
                require_slide_project(project)
                self.send_redirect(start_youtube_oauth(project))
            except Exception as exc:
                self.send_html(400, social_callback_html("Không thể kết nối YouTube", str(exc), ok=False))
            return

        if path == "/api/social/youtube/callback":
            try:
                project = finish_youtube_oauth(parse_qs(parsed.query))
                message = f"YouTube đã kết nối cho project {project}." if project else "YouTube đã kết nối."
                self.send_html(200, social_callback_html("Đã kết nối YouTube", message))
            except Exception as exc:
                self.send_html(400, social_callback_html("Kết nối YouTube thất bại", str(exc), ok=False))
            return

        if path == "/api/preview-settings":
            project = (parse_qs(parsed.query).get("project") or [""])[0]
            try:
                settings = read_preview_settings(project)
            except Exception as exc:
                self.send_json(400, {"error": str(exc)})
                return
            self.send_json(200, {"project": project, "settings": settings})
            return

        if path == "/api/slide-script":
            project = (parse_qs(parsed.query).get("project") or [""])[0]
            try:
                result = read_slide_script(project)
            except Exception as exc:
                self.send_json(400, {"error": str(exc)})
                return
            self.send_json(200, result)
            return

        if path == "/api/slide-composer":
            project = (parse_qs(parsed.query).get("project") or [""])[0]
            try:
                result = slide_composer_response(project)
            except Exception as exc:
                self.send_json(400, {"error": str(exc)})
                return
            self.send_json(200, result)
            return

        if path == "/api/studio-state":
            project = (parse_qs(parsed.query).get("project") or [""])[0]
            try:
                payload = {"project": project, "state": read_studio_state(project), "voice_preview": voice_preview_state(project)}
            except Exception as exc:
                self.send_json(400, {"error": str(exc)})
                return
            self.send_json(200, payload)
            return

        if path == "/render":
            self.send_redirect("/studio")
            return

        if path.startswith("/render/"):
            project = path.split("/render/", 1)[1].strip("/")
            self.send_redirect(f"/studio?project={quote(unquote(project))}")
            return

        if path.startswith("/api/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            job = get_job(job_id)
            if not job:
                self.send_json(404, {"error": "Job not found."})
                return
            self.send_json(200, job)
            return

        super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        REQUEST_CONTEXT.language = request_language(self.headers, parsed)
        REQUEST_CONTEXT.origin = request_origin(self.headers)

        if parsed.path.startswith(f"{BUILTIN_ELEVENLABS_ROTATOR_PATH}/"):
            self.proxy_elevenlabs_rotator_request(parsed)
            return

        if parsed.path != "/api/render":
            if parsed.path == "/api/preview-settings":
                try:
                    payload = self.read_json_body()
                    result = write_preview_settings(payload)
                except Exception as exc:
                    self.send_json(400, {"error": str(exc)})
                    return
                self.send_json(200, result)
                return

            if parsed.path == "/api/source-root":
                try:
                    if has_active_jobs():
                        raise RuntimeError("Đang có job render chạy, đợi xong rồi đổi source folder.")
                    payload = self.read_json_body()
                    source_root = str(payload.get("sourceRoot") or payload.get("source_root") or "").strip()
                    if not source_root:
                        raise ValueError("Missing source root.")
                    require_project_collection_source(Path(source_root).expanduser().resolve())
                    configure_source_root(source_root)
                    result = source_root_response()
                except RuntimeError as exc:
                    self.send_json(409, {"error": str(exc)})
                    return
                except Exception as exc:
                    self.send_json(400, {"error": str(exc)})
                    return
                self.send_json(200, result)
                return

            if parsed.path == "/api/source-root/select":
                try:
                    if has_active_jobs():
                        raise RuntimeError("Đang có job render chạy, đợi xong rồi đổi source folder.")
                    selected_root = choose_source_root_dialog()
                    require_project_collection_source(selected_root.expanduser().resolve())
                    configure_source_root(selected_root)
                    result = source_root_response()
                except RuntimeError as exc:
                    self.send_json(409, {"error": str(exc)})
                    return
                except Exception as exc:
                    self.send_json(400, {"error": str(exc)})
                    return
                self.send_json(200, result)
                return

            if parsed.path == "/api/projects/create":
                try:
                    payload = self.read_json_body()
                    result = create_project_from_template(payload)
                except RuntimeError as exc:
                    self.send_json(409, {"error": str(exc)})
                    return
                except FileExistsError as exc:
                    self.send_json(409, {"error": str(exc)})
                    return
                except Exception as exc:
                    self.send_json(400, {"error": str(exc)})
                    return
                self.send_json(201, result)
                return

            if parsed.path == "/api/templates/import":
                try:
                    payload = self.read_json_body()
                    result = import_template_archive(payload)
                except FileExistsError as exc:
                    self.send_json(409, {"error": str(exc)})
                    return
                except zipfile.BadZipFile:
                    self.send_json(400, {"error": "Template archive không phải file ZIP hợp lệ."})
                    return
                except Exception as exc:
                    self.send_json(400, {"error": str(exc)})
                    return
                self.send_json(201, result)
                return

            if parsed.path == "/api/templates/create":
                try:
                    payload = self.read_json_body()
                    result = create_template_from_template(payload)
                except FileExistsError as exc:
                    self.send_json(409, {"error": str(exc)})
                    return
                except Exception as exc:
                    self.send_json(400, {"error": str(exc)})
                    return
                self.send_json(201, result)
                return

            if parsed.path == "/api/templates/from-project":
                try:
                    payload = self.read_json_body()
                    result = create_template_from_project(payload)
                except FileExistsError as exc:
                    self.send_json(409, {"error": str(exc)})
                    return
                except Exception as exc:
                    self.send_json(400, {"error": str(exc)})
                    return
                self.send_json(201, result)
                return

            if parsed.path == "/api/templates/update":
                try:
                    payload = self.read_json_body()
                    result = update_template(payload)
                except Exception as exc:
                    self.send_json(400, {"error": str(exc)})
                    return
                self.send_json(200, result)
                return

            if parsed.path == "/api/preview-bgm":
                try:
                    payload = self.read_json_body()
                    result = upload_preview_bgm(payload)
                except Exception as exc:
                    self.send_json(400, {"error": str(exc)})
                    return
                self.send_json(200, result)
                return

            if parsed.path == "/api/slide-media":
                try:
                    payload = self.read_json_body()
                    result = upload_slide_media(payload)
                except Exception as exc:
                    self.send_json(400, {"error": str(exc)})
                    return
                self.send_json(200, result)
                return

            if parsed.path == "/api/tts/elevenlabs/config":
                try:
                    payload = self.read_json_body()
                    api_key = str(payload.get("api_key") or "").strip()
                    voice_id = str(payload.get("voice_id") or "").strip()
                    proxy_base_url_present = "proxy_base_url" in payload or "proxyBaseUrl" in payload
                    proxy_key_present = "proxy_key" in payload or "proxyKey" in payload
                    proxy_base_url = payload.get("proxy_base_url", payload.get("proxyBaseUrl"))
                    proxy_key = payload.get("proxy_key", payload.get("proxyKey"))
                    if api_key:
                        result = update_elevenlabs_api_key(api_key)
                    elif voice_id:
                        result = update_elevenlabs_voice_id(voice_id)
                    elif proxy_base_url_present or proxy_key_present:
                        result = update_elevenlabs_proxy_config(
                            str(proxy_base_url or "") if proxy_base_url_present else None,
                            str(proxy_key or "") if proxy_key_present else None,
                        )
                    else:
                        raise ValueError("Missing ElevenLabs voice_id, api_key, proxy_base_url, or proxy_key.")
                except Exception as exc:
                    self.send_json(400, {"error": str(exc)})
                    return
                self.send_json(200, result)
                return

            if parsed.path == "/api/slide-script":
                try:
                    payload = self.read_json_body()
                    result = write_slide_script(payload)
                except Exception as exc:
                    self.send_json(400, {"error": str(exc)})
                    return
                self.send_json(200, result)
                return

            if parsed.path == "/api/slide-composer":
                try:
                    payload = self.read_json_body()
                    result = save_slide_composer(payload)
                except Exception as exc:
                    self.send_json(400, {"error": str(exc)})
                    return
                self.send_json(200, result)
                return

            if parsed.path == "/api/storyboard/generate":
                try:
                    payload = self.read_json_body()
                    result = generate_storyboard(payload)
                except Exception as exc:
                    self.send_json(400, {"error": str(exc)})
                    return
                self.send_json(200, result)
                return

            if parsed.path == "/api/voice-preview":
                try:
                    payload = self.read_json_body()
                    job = create_voice_preview_job(payload)
                except RuntimeError as exc:
                    self.send_json(409, {"error": str(exc)})
                    return
                except Exception as exc:
                    self.send_json(400, {"error": str(exc)})
                    return
                self.send_json(202, job)
                return

            if parsed.path == "/api/connections":
                try:
                    payload = self.read_json_body()
                    result = upsert_connection(payload)
                except Exception as exc:
                    self.send_json(400, {"error": str(exc)})
                    return
                self.send_json(200, result)
                return

            if parsed.path == "/api/connections/assign":
                try:
                    payload = self.read_json_body()
                    result = assign_connection_projects(payload)
                except Exception as exc:
                    self.send_json(400, {"error": str(exc)})
                    return
                self.send_json(200, result)
                return

            if parsed.path == "/api/connections/use-for-studio":
                try:
                    payload = self.read_json_body()
                    result = use_connection_for_studio(payload, getattr(REQUEST_CONTEXT, "origin", ""))
                except Exception as exc:
                    self.send_json(400, {"error": str(exc)})
                    return
                self.send_json(200, result)
                return

            if parsed.path == "/api/connections/test":
                try:
                    payload = self.read_json_body()
                    result = test_connection_health(payload)
                except Exception as exc:
                    self.send_json(400, {"error": str(exc)})
                    return
                self.send_json(200, result)
                return

            if parsed.path == "/api/connections/delete":
                try:
                    payload = self.read_json_body()
                    result = delete_connection(payload)
                except Exception as exc:
                    self.send_json(400, {"error": str(exc)})
                    return
                self.send_json(200, result)
                return

            if parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/cancel"):
                job_id = parsed.path.split("/api/jobs/", 1)[1].rsplit("/cancel", 1)[0].strip("/")
                try:
                    result = cancel_job(job_id)
                    if not result:
                        self.send_json(404, {"error": "Job not found."})
                        return
                except Exception as exc:
                    self.send_json(400, {"error": str(exc)})
                    return
                self.send_json(200, result)
                return

            if parsed.path == "/api/output/delete":
                try:
                    payload = self.read_json_body()
                    result = delete_project_output(payload)
                except RuntimeError as exc:
                    self.send_json(409, {"error": str(exc)})
                    return
                except Exception as exc:
                    self.send_json(400, {"error": str(exc)})
                    return
                self.send_json(200, result)
                return

            if parsed.path == "/api/output/reveal":
                try:
                    payload = self.read_json_body()
                    result = reveal_project_output(payload)
                except FileNotFoundError as exc:
                    self.send_json(404, {"error": str(exc)})
                    return
                except RuntimeError as exc:
                    self.send_json(409, {"error": str(exc)})
                    return
                except Exception as exc:
                    self.send_json(400, {"error": str(exc)})
                    return
                self.send_json(200, result)
                return

            if parsed.path == "/api/social/youtube/upload":
                try:
                    payload = self.read_json_body()
                    require_payload_project(payload)
                    result = youtube_upload_video(payload)
                except FileNotFoundError as exc:
                    self.send_json(404, {"error": str(exc)})
                    return
                except RuntimeError as exc:
                    self.send_json(409, {"error": str(exc)})
                    return
                except Exception as exc:
                    self.send_json(400, {"error": str(exc)})
                    return
                self.send_json(200, result)
                return

            if parsed.path == "/api/social/youtube/active-channel":
                try:
                    payload = self.read_json_body()
                    result = set_youtube_active_channel(str(payload.get("channelId") or payload.get("channel_id") or ""))
                except Exception as exc:
                    self.send_json(400, {"error": str(exc)})
                    return
                self.send_json(200, result)
                return

            if parsed.path == "/api/social/facebook/active-page":
                try:
                    payload = self.read_json_body()
                    result = set_facebook_active_page(str(payload.get("pageId") or payload.get("page_id") or ""))
                except Exception as exc:
                    self.send_json(400, {"error": str(exc)})
                    return
                self.send_json(200, result)
                return

            if parsed.path == "/api/social/facebook/config":
                try:
                    payload = self.read_json_body()
                    result = update_facebook_page_config(
                        str(payload.get("pageId") or payload.get("page_id") or ""),
                        str(payload.get("pageAccessToken") or payload.get("page_access_token") or ""),
                    )
                except Exception as exc:
                    self.send_json(400, {"error": str(exc)})
                    return
                self.send_json(200, result)
                return

            if parsed.path == "/api/social/facebook/upload":
                try:
                    payload = self.read_json_body()
                    require_payload_project(payload)
                    result = facebook_upload_video(payload)
                except FileNotFoundError as exc:
                    self.send_json(404, {"error": str(exc)})
                    return
                except RuntimeError as exc:
                    self.send_json(409, {"error": str(exc)})
                    return
                except Exception as exc:
                    self.send_json(400, {"error": str(exc)})
                    return
                self.send_json(200, result)
                return

            if parsed.path == "/api/social/facebook/comment-source":
                try:
                    payload = self.read_json_body()
                    require_payload_project(payload)
                    result = facebook_comment_source(payload)
                except FileNotFoundError as exc:
                    self.send_json(404, {"error": str(exc)})
                    return
                except RuntimeError as exc:
                    self.send_json(409, {"error": str(exc)})
                    return
                except Exception as exc:
                    self.send_json(400, {"error": str(exc)})
                    return
                self.send_json(200, result)
                return

            self.send_json(404, {"error": "Unknown endpoint."})
            return

        try:
            payload = self.read_json_body()
            job = create_job(payload)
        except RuntimeError as exc:
            self.send_json(409, {"error": str(exc)})
            return
        except Exception as exc:
            self.send_json(400, {"error": str(exc)})
            return

        self.send_json(202, job)


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the local slide render web UI.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--source-root",
        "--slide-root",
        default=None,
        help="Folder chứa các project slide, hoặc chính một project folder có index.html.",
    )
    args = parser.parse_args()
    try:
        source_root = args.source_root or os.environ.get("VIRO_SOURCE_ROOT") or os.environ.get("VIRO_SLIDE_ROOT")
        configure_source_root(source_root or DEFAULT_SLIDE_ROOT)
    except Exception as exc:
        parser.error(str(exc))

    server = ThreadingHTTPServer((args.host, args.port), WebHandler)
    url = f"http://localhost:{args.port}"
    print()
    print(color_text("🚀  Viro Studio local", "bold", "green"))
    print(f"   {color_text('🌐 Studio', 'cyan')}: {color_text(url, 'underline', 'bold')}")
    print(f"   {color_text('📂 Workspace', 'cyan')}: {REPO_ROOT}")
    print(f"   {color_text('🗂 Source root', 'cyan')}: {SLIDE_ROOT} ({source_root_mode()})")
    print(f"   {color_text('■ Dừng server', 'yellow')}: Ctrl+C")
    print(f"   {color_text('↻ Chạy lại server', 'yellow')}: python3 web_server.py --source-root {shlex.quote(str(SLIDE_ROOT))}")
    print()
    sys.stdout.flush()
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\n{color_text('👋 Đã dừng server.', 'yellow')}")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

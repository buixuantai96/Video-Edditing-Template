from __future__ import annotations

import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_ROOT = REPO_ROOT / "config"
SOCIAL_UPLOAD_CONFIG = CONFIG_ROOT / "social-upload.json"
SOCIAL_UPLOAD_EXAMPLE = CONFIG_ROOT / "social-upload.example.json"


def read_social_config() -> dict:
    if not SOCIAL_UPLOAD_CONFIG.exists():
        return {}
    try:
        data = json.loads(SOCIAL_UPLOAD_CONFIG.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Social upload config is invalid JSON: {SOCIAL_UPLOAD_CONFIG}") from exc
    return data if isinstance(data, dict) else {}


def write_social_config(data: dict) -> None:
    SOCIAL_UPLOAD_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    SOCIAL_UPLOAD_CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(SOCIAL_UPLOAD_CONFIG, 0o600)
    except OSError:
        pass


def social_config_hint() -> str:
    return "YouTube chưa cấu hình. Bấm Hướng dẫn YouTube để xem cách tạo OAuth Client và điền config."

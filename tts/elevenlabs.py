from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .common import generate_project_tts

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "tts.json"
DEFAULT_MODEL_ID = "eleven_v3"
DEFAULT_OUTPUT_FORMAT = "mp3_44100_128"
COOLDOWN = 1.0
DEFAULT_PROXY_RETRIES = 3
DEFAULT_PROXY_TIMEOUT = 180


def full_voiceover_model(model_id: str) -> bool:
    return str(model_id or "").strip() == "eleven_v3"


def text_context_supported(model_id: str) -> bool:
    return not full_voiceover_model(model_id)


def api_safe_config(config: dict) -> dict:
    api_config = dict(config)
    api_config.pop("speed", None)
    if isinstance(api_config.get("voice_settings"), dict):
        settings = dict(api_config["voice_settings"])
        settings.pop("speed", None)
        api_config["voice_settings"] = settings
    return api_config


def full_script_text(lines: list[str]) -> str:
    return "\n\n".join(line.strip() for line in lines if line.strip())


def read_tts_config(config_path: Path | None = None) -> dict:
    path = config_path or DEFAULT_CONFIG_PATH
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"TTS config is invalid JSON: {path}") from exc
    return data if isinstance(data, dict) else {}


def write_tts_config(data: dict, config_path: Path | None = None) -> None:
    path = config_path or DEFAULT_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def elevenlabs_config(config_path: Path | None = None) -> dict:
    config = read_tts_config(config_path)
    data = config.get("elevenlabs", {}) if isinstance(config, dict) else {}
    return data if isinstance(data, dict) else {}


def elevenlabs_public_config(config_path: Path | None = None) -> dict:
    config = elevenlabs_config(config_path)
    proxy_base_url = elevenlabs_proxy_base_url(config)
    return {
        "config_path": str(config_path or DEFAULT_CONFIG_PATH),
        "voice_id": str(config.get("voice_id") or ""),
        "model_id": str(config.get("model_id") or DEFAULT_MODEL_ID),
        "output_format": str(config.get("output_format") or DEFAULT_OUTPUT_FORMAT),
        "api_key_configured": bool(str(config.get("api_key") or os.environ.get("ELEVENLABS_API_KEY") or "").strip()),
        "proxy_base_url": proxy_base_url,
        "proxy_key_configured": bool(elevenlabs_proxy_key(config)),
        "auth_mode": "proxy" if proxy_base_url else "direct",
    }


def update_elevenlabs_voice_id(voice_id: str, config_path: Path | None = None) -> dict:
    voice_id = str(voice_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", voice_id):
        raise ValueError("Invalid ElevenLabs voice id.")
    config = read_tts_config(config_path)
    elevenlabs = config.get("elevenlabs", {}) if isinstance(config, dict) else {}
    if not isinstance(elevenlabs, dict):
        elevenlabs = {}
    elevenlabs["voice_id"] = voice_id
    elevenlabs.setdefault("model_id", DEFAULT_MODEL_ID)
    elevenlabs.setdefault("output_format", DEFAULT_OUTPUT_FORMAT)
    config["elevenlabs"] = elevenlabs
    write_tts_config(config, config_path)
    return elevenlabs_public_config(config_path)


def update_elevenlabs_api_key(api_key: str, config_path: Path | None = None) -> dict:
    api_key = str(api_key or "").strip()
    if len(api_key) < 16 or not re.fullmatch(r"[A-Za-z0-9_.-]+", api_key):
        raise ValueError("Invalid ElevenLabs API key.")
    config = read_tts_config(config_path)
    elevenlabs = config.get("elevenlabs", {}) if isinstance(config, dict) else {}
    if not isinstance(elevenlabs, dict):
        elevenlabs = {}
    elevenlabs["api_key"] = api_key
    elevenlabs.setdefault("model_id", DEFAULT_MODEL_ID)
    elevenlabs.setdefault("output_format", DEFAULT_OUTPUT_FORMAT)
    config["elevenlabs"] = elevenlabs
    write_tts_config(config, config_path)
    return elevenlabs_public_config(config_path)


def update_elevenlabs_proxy_config(
    proxy_base_url: str | None = None,
    proxy_key: str | None = None,
    config_path: Path | None = None,
) -> dict:
    config = read_tts_config(config_path)
    elevenlabs = config.get("elevenlabs", {}) if isinstance(config, dict) else {}
    if not isinstance(elevenlabs, dict):
        elevenlabs = {}

    if proxy_base_url is not None:
        proxy_base_url = str(proxy_base_url or "").strip().rstrip("/")
        if proxy_base_url:
            if not re.fullmatch(r"https?://[^\s]+", proxy_base_url):
                raise ValueError("Invalid APIKeyRotator proxy base URL.")
            elevenlabs["proxy_base_url"] = proxy_base_url
        else:
            elevenlabs.pop("proxy_base_url", None)

    if proxy_key is not None:
        proxy_key = str(proxy_key or "").strip()
        if proxy_key:
            if len(proxy_key) < 8 or re.search(r"\s", proxy_key):
                raise ValueError("Invalid APIKeyRotator proxy key.")
            elevenlabs["proxy_key"] = proxy_key
        else:
            elevenlabs.pop("proxy_key", None)

    elevenlabs.setdefault("model_id", DEFAULT_MODEL_ID)
    elevenlabs.setdefault("output_format", DEFAULT_OUTPUT_FORMAT)
    config["elevenlabs"] = elevenlabs
    write_tts_config(config, config_path)
    return elevenlabs_public_config(config_path)


def apply_runtime_overrides(
    config: dict,
    *,
    proxy_base_url: str | None = None,
    proxy_key: str | None = None,
) -> dict:
    next_config = dict(config)
    if proxy_base_url is not None:
        value = str(proxy_base_url or "").strip().rstrip("/")
        if value:
            if not re.fullmatch(r"https?://[^\s]+", value):
                raise ValueError("Invalid APIKeyRotator proxy base URL.")
            next_config["proxy_base_url"] = value
        else:
            next_config.pop("proxy_base_url", None)
    if proxy_key is not None:
        value = str(proxy_key or "").strip()
        if value:
            if len(value) < 8 or re.search(r"\s", value):
                raise ValueError("Invalid APIKeyRotator proxy key.")
            next_config["proxy_key"] = value
        else:
            next_config.pop("proxy_key", None)
    return next_config


def elevenlabs_api_key(api_key: str | None = None, config: dict | None = None) -> str:
    key = str(api_key or os.environ.get("ELEVENLABS_API_KEY") or (config or {}).get("api_key") or "").strip()
    if not key:
        raise ValueError("Missing ElevenLabs API key. Set ELEVENLABS_API_KEY or config/tts.json elevenlabs.api_key.")
    return key


def elevenlabs_proxy_base_url(config: dict | None = None) -> str:
    return str(os.environ.get("ELEVENLABS_PROXY_BASE_URL") or (config or {}).get("proxy_base_url") or "").strip().rstrip("/")


def elevenlabs_proxy_key(config: dict | None = None) -> str:
    return str(os.environ.get("ELEVENLABS_PROXY_KEY") or (config or {}).get("proxy_key") or "").strip()


def elevenlabs_auth_configured(api_key: str | None = None, config: dict | None = None) -> None:
    if elevenlabs_proxy_base_url(config):
        if not elevenlabs_proxy_key(config):
            raise ValueError("Missing APIKeyRotator proxy key. Set ELEVENLABS_PROXY_KEY or config/tts.json elevenlabs.proxy_key.")
        return
    elevenlabs_api_key(api_key, config)


def elevenlabs_voice_id(voice: str | None = None, config: dict | None = None) -> str:
    voice_id = str(voice or (config or {}).get("voice_id") or "").strip()
    if not voice_id:
        raise ValueError("Missing ElevenLabs voice id. Pass --voice VOICE_ID or set config/tts.json elevenlabs.voice_id.")
    return voice_id


def voice_settings(config: dict) -> dict:
    raw = config.get("voice_settings", {}) if isinstance(config.get("voice_settings"), dict) else config
    settings = {}
    for key in ("stability", "similarity_boost", "style", "speed"):
        if key in raw and raw[key] not in (None, ""):
            settings[key] = raw[key]
    if "use_speaker_boost" in raw:
        settings["use_speaker_boost"] = bool(raw["use_speaker_boost"])
    return settings


def write_audio_result(audio: object, audio_file: Path) -> None:
    if isinstance(audio, (bytes, bytearray)):
        audio_file.write_bytes(bytes(audio))
        return
    with audio_file.open("wb") as f:
        for chunk in audio:
            if chunk:
                f.write(chunk)


def elevenlabs_payload(
    text: str,
    *,
    model_id: str,
    config: dict,
    previous_text: str | None = None,
    next_text: str | None = None,
) -> dict:
    payload = {
        "text": text,
        "model_id": model_id,
    }
    if previous_text:
        payload["previous_text"] = previous_text
    if next_text:
        payload["next_text"] = next_text
    settings = voice_settings(config)
    if settings:
        payload["voice_settings"] = settings
    return payload


def request_elevenlabs_audio_via_proxy(
    text: str,
    audio_file: Path,
    *,
    voice_id: str,
    model_id: str,
    output_format: str,
    config: dict,
    previous_text: str | None = None,
    next_text: str | None = None,
) -> None:
    proxy_base_url = elevenlabs_proxy_base_url(config)
    proxy_key = elevenlabs_proxy_key(config)
    if not proxy_base_url:
        raise ValueError("Missing APIKeyRotator proxy base URL.")
    if not proxy_key:
        raise ValueError("Missing APIKeyRotator proxy key.")

    query = urlencode({"output_format": output_format}) if output_format else ""
    url = f"{proxy_base_url}/v1/text-to-speech/{quote(voice_id, safe='')}"
    if query:
        url = f"{url}?{query}"
    body = json.dumps(
        elevenlabs_payload(
            text,
            model_id=model_id,
            config=config,
            previous_text=previous_text,
            next_text=next_text,
        ),
        ensure_ascii=False,
    ).encode("utf-8")
    headers = {
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
        "X-Proxy-Key": proxy_key,
    }

    retries = int(config.get("proxy_retries") or DEFAULT_PROXY_RETRIES)
    timeout = int(config.get("proxy_timeout") or DEFAULT_PROXY_TIMEOUT)
    retry_statuses = {401, 403, 408, 409, 425, 429, 500, 502, 503, 504}
    last_error: Exception | None = None
    for attempt in range(1, max(1, retries) + 1):
        request = Request(url, data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=timeout) as response:
                audio_file.write_bytes(response.read())
                return
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            last_error = RuntimeError(f"APIKeyRotator/ElevenLabs failed: HTTP {exc.code}: {detail[:800]}")
            if exc.code not in retry_statuses or attempt >= retries:
                break
        except URLError as exc:
            last_error = RuntimeError(f"APIKeyRotator/ElevenLabs request failed: {exc}")
            if attempt >= retries:
                break
        if attempt < retries:
            time.sleep(min(2.0, 0.4 * attempt))

    raise last_error or RuntimeError("APIKeyRotator/ElevenLabs request failed.")


def request_elevenlabs_audio(
    text: str,
    audio_file: Path,
    *,
    api_key: str | None,
    voice_id: str,
    model_id: str,
    output_format: str,
    config: dict,
    previous_text: str | None = None,
    next_text: str | None = None,
) -> None:
    if elevenlabs_proxy_base_url(config):
        request_elevenlabs_audio_via_proxy(
            text,
            audio_file,
            voice_id=voice_id,
            model_id=model_id,
            output_format=output_format,
            config=config,
            previous_text=previous_text,
            next_text=next_text,
        )
        return

    try:
        from elevenlabs.client import ElevenLabs
    except ImportError as exc:
        raise RuntimeError("Missing ElevenLabs SDK. Install requirements with: pip install -r requirements.txt") from exc

    elevenlabs = ElevenLabs(api_key=elevenlabs_api_key(api_key, config))
    kwargs = {
        "text": text,
        "voice_id": voice_id,
        "model_id": model_id,
        "output_format": output_format,
    }
    if previous_text:
        kwargs["previous_text"] = previous_text
    if next_text:
        kwargs["next_text"] = next_text
    settings = voice_settings(config)
    if settings:
        try:
            from elevenlabs import VoiceSettings

            kwargs["voice_settings"] = VoiceSettings(**settings)
        except Exception:
            kwargs["voice_settings"] = settings
    try:
        audio = elevenlabs.text_to_speech.convert(**kwargs)
    except Exception as exc:
        raise RuntimeError(f"ElevenLabs API failed: {exc}") from exc
    write_audio_result(audio, audio_file)


async def generate_elevenlabs_full_audio(
    slide_dir: Path,
    output_dir: Path,
    lines: list[str],
    *,
    voice: str | None = None,
    model_id: str | None = None,
    output_format: str | None = None,
    api_key: str | None = None,
    config_path: Path | None = None,
    proxy_base_url: str | None = None,
    proxy_key: str | None = None,
    full_text: str | None = None,
    force: bool = False,
) -> Path:
    del slide_dir
    config = apply_runtime_overrides(
        dict(elevenlabs_config(config_path)),
        proxy_base_url=proxy_base_url,
        proxy_key=proxy_key,
    )
    api_config = api_safe_config(config)
    elevenlabs_auth_configured(api_key, config)
    using_proxy = bool(elevenlabs_proxy_base_url(config))
    resolved_key = None if using_proxy else elevenlabs_api_key(api_key, config)
    resolved_voice = elevenlabs_voice_id(voice, config)
    resolved_model = str(model_id or config.get("model_id") or DEFAULT_MODEL_ID).strip()
    resolved_output_format = str(output_format or config.get("output_format") or DEFAULT_OUTPUT_FORMAT).strip()
    text = full_text if full_text is not None else full_script_text(lines)
    if not text.strip():
        raise ValueError("No script text to send to ElevenLabs.")

    output_dir.mkdir(parents=True, exist_ok=True)
    audio_file = output_dir / "elevenlabs_full_voiceover.mp3"
    meta_file = output_dir / "elevenlabs_full_voiceover.meta.json"
    metadata = {
        "engine": "elevenlabs",
        "mode": "full_voiceover",
        "text": text,
        "lines": lines,
        "voice_id": resolved_voice,
        "model_id": resolved_model,
        "output_format": resolved_output_format,
        "voice_settings": voice_settings(api_config),
        "auth_mode": "proxy" if using_proxy else "direct",
        "proxy_base_url": elevenlabs_proxy_base_url(config) if using_proxy else "",
    }
    cache_matches = False
    if meta_file.exists():
        try:
            cache_matches = json.loads(meta_file.read_text(encoding="utf-8")) == metadata
        except json.JSONDecodeError:
            cache_matches = False
    if not force and cache_matches and audio_file.exists() and audio_file.stat().st_size > 0:
        print(f"Full ElevenLabs TTS: {audio_file} (cached)")
        return audio_file

    if audio_file.exists():
        audio_file.unlink()
    print(
        f"Voice: ElevenLabs {resolved_voice} "
        f"({resolved_model}, {resolved_output_format}, full script in one request, "
        f"{'APIKeyRotator proxy' if using_proxy else 'direct API'})"
    )
    print(f"Sending full script to ElevenLabs: {len(lines)} slides, {len(text)} chars")
    await asyncio.to_thread(
        request_elevenlabs_audio,
        text,
        audio_file,
        api_key=resolved_key,
        voice_id=resolved_voice,
        model_id=resolved_model,
        output_format=resolved_output_format,
        config=api_config,
    )
    meta_file.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Full ElevenLabs TTS saved: {audio_file}")
    return audio_file


async def generate_elevenlabs_tts(
    slide_dir: Path,
    output_dir: Path,
    lines: list[str],
    *,
    voice: str | None = None,
    model_id: str | None = None,
    output_format: str | None = None,
    speed: float | None = None,
    api_key: str | None = None,
    config_path: Path | None = None,
    proxy_base_url: str | None = None,
    proxy_key: str | None = None,
    force: bool = False,
) -> None:
    config = apply_runtime_overrides(
        dict(elevenlabs_config(config_path)),
        proxy_base_url=proxy_base_url,
        proxy_key=proxy_key,
    )
    api_config = api_safe_config(config)
    post_process_speed = float(speed) if speed is not None else 1.0
    elevenlabs_auth_configured(api_key, config)
    using_proxy = bool(elevenlabs_proxy_base_url(config))
    resolved_key = None if using_proxy else elevenlabs_api_key(api_key, config)
    resolved_voice = elevenlabs_voice_id(voice, config)
    resolved_model = str(model_id or config.get("model_id") or DEFAULT_MODEL_ID).strip()
    resolved_output_format = str(output_format or config.get("output_format") or DEFAULT_OUTPUT_FORMAT).strip()
    use_text_context = text_context_supported(resolved_model)
    context_note = "context on" if use_text_context else "context off for this model"
    auth_note = "APIKeyRotator proxy" if using_proxy else "direct API"
    print(f"Voice: ElevenLabs {resolved_voice} ({resolved_model}, {resolved_output_format}, ffmpeg speed {post_process_speed:g}x, {context_note}, {auth_note})")

    def context_for(index: int) -> tuple[str | None, str | None]:
        if not use_text_context:
            return None, None
        previous_text = lines[index - 1] if index > 0 else None
        next_text = lines[index + 1] if index < len(lines) - 1 else None
        return previous_text, next_text

    async def line_generator(index: int, text: str, audio_file: Path, subtitle_file: Path) -> None:
        previous_text, next_text = context_for(index)
        await asyncio.to_thread(
            request_elevenlabs_audio,
            text,
            audio_file,
            api_key=resolved_key,
            voice_id=resolved_voice,
            model_id=resolved_model,
            output_format=resolved_output_format,
            config=api_config,
            previous_text=previous_text,
            next_text=next_text,
        )

    def cache_metadata(index: int, text: str) -> dict:
        previous_text, next_text = context_for(index)
        return {
            "engine": "elevenlabs",
            "text": text,
            "voice_id": resolved_voice,
            "model_id": resolved_model,
            "output_format": resolved_output_format,
            "voice_settings": voice_settings(api_config),
            "text_context": use_text_context,
            "previous_text": previous_text,
            "next_text": next_text,
            "ffmpeg_speed": f"{post_process_speed:.6g}",
            "auth_mode": "proxy" if using_proxy else "direct",
            "proxy_base_url": elevenlabs_proxy_base_url(config) if using_proxy else "",
        }

    await generate_project_tts(
        slide_dir,
        output_dir,
        lines,
        line_generator,
        force=force,
        cooldown=float(config.get("cooldown") or COOLDOWN),
        post_process_speed=post_process_speed,
        cache_metadata=cache_metadata,
    )

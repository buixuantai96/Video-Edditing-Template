from __future__ import annotations

import json
import secrets
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import read_social_config, social_config_hint, write_social_config
from .metadata import build_upload_metadata, final_video_path_for_project, require_slide_project

YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
YOUTUBE_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
YOUTUBE_OAUTH_SCOPE = f"{YOUTUBE_UPLOAD_SCOPE} {YOUTUBE_READONLY_SCOPE}"
OAUTH_STATES: dict[str, dict] = {}
OAUTH_STATES_LOCK = threading.Lock()


def youtube_config(config: dict | None = None) -> dict:
    config = read_social_config() if config is None else config
    youtube = config.get("youtube", {}) if isinstance(config, dict) else {}
    if not isinstance(youtube, dict):
        youtube = {}
    return youtube


def youtube_redirect_uri(youtube: dict) -> str:
    return str(youtube.get("redirect_uri") or "http://localhost:8765/api/social/youtube/callback")


def youtube_is_configured(youtube: dict) -> bool:
    return bool(str(youtube.get("client_id") or "").strip() and str(youtube.get("client_secret") or "").strip())


def youtube_is_connected(youtube: dict) -> bool:
    tokens = youtube_active_tokens(youtube)
    return bool(tokens.get("refresh_token") or tokens.get("access_token"))


def youtube_channels(youtube: dict) -> list[dict]:
    raw_channels = youtube.get("channels")
    channels = raw_channels if isinstance(raw_channels, list) else []
    normalized = []
    seen = set()
    for channel in channels:
        if not isinstance(channel, dict):
            continue
        channel_id = str(channel.get("id") or "").strip()
        tokens = channel.get("tokens", {}) if isinstance(channel.get("tokens"), dict) else {}
        if not channel_id or channel_id in seen:
            continue
        seen.add(channel_id)
        normalized.append({
            "id": channel_id,
            "title": str(channel.get("title") or "").strip(),
            "thumbnail": str(channel.get("thumbnail") or "").strip(),
            "tokens": tokens,
        })
    legacy_tokens = youtube.get("tokens", {}) if isinstance(youtube.get("tokens"), dict) else {}
    legacy_channel = youtube.get("channel", {}) if isinstance(youtube.get("channel"), dict) else {}
    legacy_id = str(legacy_channel.get("id") or youtube.get("active_channel_id") or "").strip()
    if legacy_tokens and legacy_id and legacy_id not in seen:
        normalized.append({
            "id": legacy_id,
            "title": str(legacy_channel.get("title") or "").strip(),
            "thumbnail": str(legacy_channel.get("thumbnail") or "").strip(),
            "tokens": legacy_tokens,
        })
    return normalized


def youtube_active_channel_id(youtube: dict) -> str:
    active_channel_id = str(youtube.get("active_channel_id") or "").strip()
    if active_channel_id:
        return active_channel_id
    channels = youtube_channels(youtube)
    return str(channels[0].get("id") or "") if channels else ""


def youtube_active_channel(youtube: dict) -> dict:
    channels = youtube_channels(youtube)
    active_channel_id = youtube_active_channel_id(youtube)
    for channel in channels:
        if channel.get("id") == active_channel_id:
            return channel
    return channels[0] if channels else {}


def youtube_active_tokens(youtube: dict) -> dict:
    channel = youtube_active_channel(youtube)
    tokens = channel.get("tokens", {}) if isinstance(channel.get("tokens"), dict) else {}
    return tokens


def youtube_public_channel_entry(channel: dict) -> dict:
    return {
        "id": str(channel.get("id") or ""),
        "title": str(channel.get("title") or ""),
        "thumbnail": str(channel.get("thumbnail") or ""),
    }


def youtube_public_channel(youtube: dict) -> dict:
    return youtube_public_channel_entry(youtube_active_channel(youtube))


def youtube_public_channels(youtube: dict) -> list[dict]:
    active_channel_id = youtube_active_channel_id(youtube)
    result = []
    for channel in youtube_channels(youtube):
        public_channel = youtube_public_channel_entry(channel)
        public_channel["active"] = public_channel.get("id") == active_channel_id
        result.append(public_channel)
    return result


def youtube_store_channel(youtube: dict, channel: dict, tokens: dict) -> None:
    channel_id = str(channel.get("id") or "").strip()
    if not channel_id:
        raise RuntimeError("YouTube channel lookup did not return a channel id.")
    channels = youtube_channels(youtube)
    next_channel = {
        "id": channel_id,
        "title": str(channel.get("title") or "").strip(),
        "thumbnail": str(channel.get("thumbnail") or "").strip(),
        "tokens": tokens,
    }
    replaced = False
    for index, existing in enumerate(channels):
        if existing.get("id") == channel_id:
            channels[index] = next_channel
            replaced = True
            break
    if not replaced:
        channels.append(next_channel)
    youtube["channels"] = channels
    youtube["active_channel_id"] = channel_id


def youtube_update_active_tokens(config: dict, youtube: dict, tokens: dict) -> None:
    youtube_update_channel_tokens(config, youtube, youtube_active_channel_id(youtube), tokens)


def youtube_update_channel_tokens(config: dict, youtube: dict, channel_id: str, tokens: dict) -> None:
    channel_id = str(channel_id or "").strip()
    channels = youtube_channels(youtube)
    for channel in channels:
        if channel.get("id") == channel_id:
            channel["tokens"] = tokens
            youtube["channels"] = channels
            config["youtube"] = youtube
            write_social_config(config)
            return
    raise RuntimeError("YouTube channel is missing. Click Thêm channel and choose that channel again.")


def youtube_access_token_for_channel(config: dict, youtube: dict, channel: dict) -> str:
    channel_id = str(channel.get("id") or "").strip()
    tokens = channel.get("tokens", {}) if isinstance(channel.get("tokens"), dict) else {}
    access_token = str(tokens.get("access_token") or "")
    expires_at = float(tokens.get("expires_at") or 0)
    if access_token and expires_at > time.time() + 90:
        return access_token
    refresh_token = str(tokens.get("refresh_token") or "")
    if not refresh_token:
        raise RuntimeError("YouTube channel is missing a refresh token. Click Thêm channel and choose that channel again.")

    payload = urlencode(
        {
            "client_id": youtube["client_id"],
            "client_secret": youtube["client_secret"],
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")
    request = Request(
        "https://oauth2.googleapis.com/token",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=60) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"YouTube token refresh failed: HTTP {exc.code}: {detail}") from exc

    tokens.update(
        {
            "access_token": data["access_token"],
            "expires_at": time.time() + float(data.get("expires_in") or 3600),
        }
    )
    youtube_update_channel_tokens(config, youtube, channel_id, tokens)
    return str(data["access_token"])


def youtube_channel_profile(config: dict, youtube: dict, channel: dict) -> dict:
    profile = youtube_public_channel_entry(channel)
    channel_id = profile["id"]
    if not channel_id:
        return profile
    try:
        fresh_profile = youtube_fetch_channel(youtube_access_token_for_channel(config, youtube, channel))
    except RuntimeError as exc:
        profile["error"] = str(exc)
        return profile
    fresh_channel_id = str(fresh_profile.get("id") or "").strip()
    if fresh_channel_id and fresh_channel_id != channel_id:
        profile["error"] = f"YouTube token returned channel {fresh_channel_id}, expected {channel_id}."
        return profile
    if fresh_profile:
        profile.update(fresh_profile)
    return profile


def youtube_channels_status(config: dict, youtube: dict) -> list[dict]:
    active_channel_id = youtube_active_channel_id(youtube)
    result = []
    for channel in youtube_channels(youtube):
        profile = youtube_channel_profile(config, youtube, channel)
        profile["active"] = profile.get("id") == active_channel_id
        result.append(profile)
    return result


def set_youtube_active_channel(channel_id: str) -> dict:
    channel_id = str(channel_id or "").strip()
    if not channel_id:
        raise ValueError("Missing YouTube channel id.")
    config = read_social_config()
    youtube = youtube_config(config)
    if not any(channel.get("id") == channel_id for channel in youtube_channels(youtube)):
        raise ValueError("YouTube channel id is not connected. Click Thêm channel and choose that channel first.")
    youtube["active_channel_id"] = channel_id
    config["youtube"] = youtube
    write_social_config(config)
    return {"ok": True, "active_channel_id": channel_id}


def youtube_fetch_channel(access_token: str) -> dict:
    request = Request(
        "https://www.googleapis.com/youtube/v3/channels?" + urlencode({"part": "snippet", "mine": "true"}),
        headers={"Authorization": f"Bearer {access_token}"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=60) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"YouTube channel lookup failed: HTTP {exc.code}: {detail}") from exc
    items = data.get("items") if isinstance(data, dict) else []
    if not isinstance(items, list) or not items:
        return {}
    item = items[0] if isinstance(items[0], dict) else {}
    snippet = item.get("snippet", {}) if isinstance(item.get("snippet"), dict) else {}
    thumbnails = snippet.get("thumbnails", {}) if isinstance(snippet.get("thumbnails"), dict) else {}
    thumbnail = ""
    for key in ("default", "medium", "high"):
        entry = thumbnails.get(key, {}) if isinstance(thumbnails.get(key), dict) else {}
        thumbnail = str(entry.get("url") or thumbnail)
    return {
        "id": str(item.get("id") or ""),
        "title": str(snippet.get("title") or ""),
        "thumbnail": thumbnail,
    }


def youtube_exchange_code(code: str, youtube: dict) -> dict:
    payload = urlencode(
        {
            "code": code,
            "client_id": youtube["client_id"],
            "client_secret": youtube["client_secret"],
            "redirect_uri": youtube_redirect_uri(youtube),
            "grant_type": "authorization_code",
        }
    ).encode("utf-8")
    request = Request(
        "https://oauth2.googleapis.com/token",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"YouTube OAuth failed: HTTP {exc.code}: {detail}") from exc


def youtube_refresh_access_token(config: dict, youtube: dict) -> str:
    channel = youtube_active_channel(youtube)
    if not channel:
        raise RuntimeError("YouTube is not connected. Click Thêm channel first.")
    return youtube_access_token_for_channel(config, youtube, channel)


def start_youtube_oauth(project: str) -> str:
    config = read_social_config()
    youtube = youtube_config(config)
    if not youtube_is_configured(youtube):
        raise ValueError(social_config_hint())
    require_slide_project(project)
    state = secrets.token_urlsafe(24)
    with OAUTH_STATES_LOCK:
        OAUTH_STATES[state] = {"project": project, "created_at": time.time()}
    params = {
        "client_id": youtube["client_id"],
        "redirect_uri": youtube_redirect_uri(youtube),
        "response_type": "code",
        "scope": YOUTUBE_OAUTH_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)


def finish_youtube_oauth(query: dict[str, list[str]]) -> str:
    error = (query.get("error") or [""])[0]
    if error:
        raise RuntimeError(f"YouTube OAuth cancelled: {error}")
    code = (query.get("code") or [""])[0]
    state = (query.get("state") or [""])[0]
    if not code or not state:
        raise ValueError("Missing OAuth code/state.")
    with OAUTH_STATES_LOCK:
        state_data = OAUTH_STATES.pop(state, None)
    if not state_data:
        raise ValueError("OAuth state is invalid or expired. Try adding the YouTube channel again.")

    config = read_social_config()
    youtube = youtube_config(config)
    if not youtube_is_configured(youtube):
        raise ValueError(social_config_hint())
    token_data = youtube_exchange_code(code, youtube)
    channel = youtube_fetch_channel(str(token_data["access_token"]))
    existing_channel = next((item for item in youtube_channels(youtube) if item.get("id") == channel.get("id")), {})
    existing_tokens = existing_channel.get("tokens", {}) if isinstance(existing_channel.get("tokens"), dict) else {}
    refresh_token = token_data.get("refresh_token") or existing_tokens.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("Google did not return a refresh_token. Try removing the app grant and add this channel again.")
    tokens = {
        "access_token": token_data["access_token"],
        "refresh_token": refresh_token,
        "expires_at": time.time() + float(token_data.get("expires_in") or 3600),
        "scope": token_data.get("scope", YOUTUBE_OAUTH_SCOPE),
        "token_type": token_data.get("token_type", "Bearer"),
    }
    youtube_store_channel(youtube, channel, tokens)
    youtube.pop("tokens", None)
    youtube.pop("channel", None)
    config["youtube"] = youtube
    write_social_config(config)
    return str(state_data.get("project") or "")


def youtube_upload_video(payload: dict) -> dict:
    project = str(payload.get("project") or "").strip()
    video_path = final_video_path_for_project(project)
    metadata = build_upload_metadata(project)
    title = str(payload.get("title") or metadata["title"]).strip()[:100]
    description = str(payload.get("description") or metadata["description"]).strip()[:5000]
    privacy_status = str(payload.get("privacyStatus") or "private").strip()
    if privacy_status not in {"private", "unlisted", "public"}:
        raise ValueError("privacyStatus must be private, unlisted, or public.")

    config = read_social_config()
    youtube = youtube_config(config)
    if not youtube_is_configured(youtube):
        raise ValueError(social_config_hint())
    access_token = youtube_refresh_access_token(config, youtube)

    init_payload = {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": "22",
            "tags": payload.get("tags") if isinstance(payload.get("tags"), list) else metadata["tags"],
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False,
        },
    }
    init_body = json.dumps(init_payload).encode("utf-8")
    init_url = "https://www.googleapis.com/upload/youtube/v3/videos?" + urlencode(
        {"uploadType": "resumable", "part": "snippet,status"}
    )
    init_request = Request(
        init_url,
        data=init_body,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=utf-8",
            "X-Upload-Content-Length": str(video_path.stat().st_size),
            "X-Upload-Content-Type": "video/mp4",
        },
        method="POST",
    )
    try:
        with urlopen(init_request, timeout=60) as response:
            upload_url = response.headers.get("Location")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"YouTube upload init failed: HTTP {exc.code}: {detail}") from exc
    if not upload_url:
        raise RuntimeError("YouTube upload init did not return an upload URL.")

    upload_request = Request(
        upload_url,
        data=video_path.read_bytes(),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "video/mp4",
            "Content-Length": str(video_path.stat().st_size),
        },
        method="PUT",
    )
    try:
        with urlopen(upload_request, timeout=600) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"YouTube upload failed: HTTP {exc.code}: {detail}") from exc

    video_id = result.get("id")
    if not video_id:
        raise RuntimeError(f"YouTube upload response did not include a video id: {result}")
    return {
        "ok": True,
        "platform": "youtube",
        "project": project,
        "video_id": video_id,
        "url": f"https://youtu.be/{video_id}",
        "studio_url": f"https://studio.youtube.com/video/{video_id}/edit",
        "privacyStatus": privacy_status,
        "message": "Uploaded to YouTube. Review in YouTube Studio before publishing.",
    }

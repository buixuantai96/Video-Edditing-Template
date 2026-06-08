from __future__ import annotations

from .config import SOCIAL_UPLOAD_CONFIG, read_social_config, social_config_hint
from .facebook import (
    facebook_active_page_id,
    facebook_config,
    facebook_config_hint,
    facebook_graph_version,
    facebook_is_configured,
    facebook_page_profile,
    facebook_pages_status,
)
from .youtube import (
    youtube_active_channel_id,
    youtube_config,
    youtube_channels_status,
    youtube_is_configured,
    youtube_is_connected,
    youtube_redirect_uri,
)


def social_status() -> dict:
    config = read_social_config()
    youtube = youtube_config(config)
    facebook = facebook_config(config)
    youtube_channels = youtube_channels_status(config, youtube)
    youtube_channel = next((channel for channel in youtube_channels if channel.get("active")), None)
    facebook_configured = facebook_is_configured(facebook)
    facebook_pages = facebook_pages_status(facebook)
    facebook_page = next((page for page in facebook_pages if page.get("active")), None)
    if not facebook_page and facebook_configured:
        facebook_page = facebook_page_profile(facebook)
    return {
        "config_path": str(SOCIAL_UPLOAD_CONFIG),
        "platforms": {
            "youtube": {
                "configured": youtube_is_configured(youtube),
                "connected": youtube_is_connected(youtube),
                "active_channel_id": youtube_active_channel_id(youtube),
                "channel": youtube_channel or (youtube_channels[0] if youtube_channels else {}),
                "channels": youtube_channels,
                "redirect_uri": youtube_redirect_uri(youtube),
                "message": "" if youtube_is_configured(youtube) else social_config_hint(),
            },
            "facebook": {
                "configured": facebook_configured,
                "connected": facebook_configured,
                "available": facebook_configured,
                "graph_version": facebook_graph_version(facebook),
                "active_page_id": facebook_active_page_id(facebook),
                "page_id": facebook_active_page_id(facebook),
                "page": facebook_page or {},
                "pages": facebook_pages,
                "video_state": str(facebook.get("video_state") or "DRAFT").upper(),
                "message": "" if facebook_configured else facebook_config_hint(),
            },
        },
    }

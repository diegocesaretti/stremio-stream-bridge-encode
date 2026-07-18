"""Subtitle matching and Google Cast playback helpers."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .aggregator import StremioAddonManager
from .api import StremioStreamServerClient
from .const import (
    CONF_SUBTITLE_BASE_URL,
    CONF_SUBTITLE_CONVERT_VTT,
    CONF_SUBTITLE_LANGUAGES,
    CONF_SUBTITLE_MODE,
    DEFAULT_SUBTITLE_BASE_URL,
    DEFAULT_SUBTITLE_CONVERT_VTT,
    DEFAULT_SUBTITLE_LANGUAGES,
    DEFAULT_SUBTITLE_MODE,
)
from .subtitle_proxy import SubtitleProxy, SubtitleProxyError

_LOGGER = logging.getLogger(__name__)

_LANGUAGE_ALIASES = {
    "es": {"es", "spa", "esp", "spanish"},
    "en": {"en", "eng", "english"},
    "pt": {"pt", "por", "pob", "portuguese", "brazilian"},
    "it": {"it", "ita", "italian"},
    "fr": {"fr", "fra", "fre", "french"},
    "de": {"de", "deu", "ger", "german"},
}


def parse_languages(value: object) -> list[str]:
    """Parse a comma/newline-separated preferred language list."""
    if isinstance(value, list):
        raw_values = [str(item) for item in value]
    else:
        raw_values = str(value or "").replace("\n", ",").split(",")
    result: list[str] = []
    for raw in raw_values:
        language = raw.strip().lower()
        if language and language not in result:
            result.append(language)
    return result


def subtitle_extra_from_stream(stream: Mapping[str, Any]) -> dict[str, str]:
    """Build Stremio subtitle matching extras from stream behavior hints."""
    hints = stream.get("behaviorHints")
    if not isinstance(hints, Mapping):
        return {}
    result: dict[str, str] = {}
    for source, target in (
        ("videoHash", "videoHash"),
        ("videoSize", "videoSize"),
        ("filename", "filename"),
    ):
        value = hints.get(source)
        if isinstance(value, (str, int, float)) and str(value):
            result[target] = str(value)
    return result


def choose_subtitle(
    subtitles: list[dict[str, Any]], preferred_languages: object
) -> dict[str, Any] | None:
    """Choose the first subtitle matching language preference order."""
    if not subtitles:
        return None
    preferences = parse_languages(preferred_languages)
    if not preferences:
        return subtitles[0]

    def aliases(language: str) -> set[str]:
        language = language.lower()
        for values in _LANGUAGE_ALIASES.values():
            if language in values:
                return values
        return {language}

    for preference in preferences:
        accepted = aliases(preference)
        for subtitle in subtitles:
            language = str(subtitle.get("lang") or "und").lower()
            if language in accepted:
                return subtitle
    return None


async def async_prepare_subtitle_track(
    manager: StremioAddonManager,
    server: StremioStreamServerClient,
    proxy: SubtitleProxy,
    options: Mapping[str, Any],
    media_type: str,
    media_id: str,
    stream: dict[str, Any],
    *,
    disabled: bool = False,
) -> dict[str, Any] | None:
    """Resolve one preferred subtitle track for playback."""
    if disabled or options.get(CONF_SUBTITLE_MODE, DEFAULT_SUBTITLE_MODE) == "disabled":
        return None
    subtitles = await manager.get_subtitles(
        media_type,
        media_id,
        subtitle_extra_from_stream(stream),
        stream,
    )
    subtitle = choose_subtitle(
        subtitles,
        options.get(CONF_SUBTITLE_LANGUAGES, DEFAULT_SUBTITLE_LANGUAGES),
    )
    if subtitle is None:
        _LOGGER.warning(
            "No subtitle matched preferred languages %s for %s/%s",
            options.get(CONF_SUBTITLE_LANGUAGES, DEFAULT_SUBTITLE_LANGUAGES),
            media_type,
            media_id,
        )
        return None
    raw_url = str(subtitle["url"])
    convert = bool(
        options.get(CONF_SUBTITLE_CONVERT_VTT, DEFAULT_SUBTITLE_CONVERT_VTT)
    )
    if convert:
        try:
            proxy_url = await proxy.async_prepare_url(
                raw_url,
                str(options.get(CONF_SUBTITLE_BASE_URL, DEFAULT_SUBTITLE_BASE_URL) or ""),
            )
            _LOGGER.info(
                "Prepared %s subtitle for %s/%s through Home Assistant proxy",
                subtitle.get("lang"),
                media_type,
                media_id,
            )
            return {
                "url": proxy_url,
                "lang": str(subtitle.get("lang") or "und"),
                "mime": "text/vtt",
                "provider": subtitle.get("_bridge_addon_name"),
            }
        except SubtitleProxyError as err:
            # Keep compatibility with stream-server variants that expose the Stremio
            # subtitle conversion endpoint, but make the failure visible in logs.
            _LOGGER.warning(
                "Home Assistant subtitle proxy failed for %s/%s: %s; falling back "
                "to stream-server conversion",
                media_type,
                media_id,
                err,
            )
            return {
                "url": server.build_subtitle_vtt_url(raw_url),
                "lang": str(subtitle.get("lang") or "und"),
                "mime": "text/vtt",
                "provider": subtitle.get("_bridge_addon_name"),
            }
    lowered = raw_url.lower().split("?", 1)[0]
    mime = "text/vtt" if lowered.endswith(".vtt") else "application/x-subrip"
    return {
        "url": raw_url,
        "lang": str(subtitle.get("lang") or "und"),
        "mime": mime,
        "provider": subtitle.get("_bridge_addon_name"),
    }


def is_cast_player(hass: HomeAssistant, entity_id: str | None) -> bool:
    """Return whether a target entity belongs to Home Assistant's Cast integration."""
    if not entity_id:
        return False
    registry_entry = er.async_get(hass).async_get(entity_id)
    return registry_entry is not None and registry_entry.platform == "cast"


def cast_media_source_payload(
    video_url: str,
    video_mime: str,
    subtitle: dict[str, Any] | None,
    *,
    title: str | None = None,
    thumbnail: str | None = None,
) -> str:
    """Build the integration-specific JSON understood by the Cast media player."""
    payload: dict[str, Any] = {
        "app_name": "default_media_receiver",
        "media_id": video_url,
        "media_type": video_mime,
        "stream_type": "BUFFERED",
    }
    if title:
        payload["title"] = title
    if thumbnail:
        payload["thumb"] = thumbnail
    if subtitle:
        payload.update(
            {
                "subtitles": subtitle["url"],
                "subtitles_lang": subtitle["lang"],
                "subtitles_mime": subtitle["mime"],
                "subtitle_id": 1,
            }
        )
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def cast_service_extra(
    subtitle: dict[str, Any] | None,
    *,
    title: str | None = None,
    thumbnail: str | None = None,
) -> dict[str, Any]:
    """Build media_player.play_media extra parameters for Cast."""
    extra: dict[str, Any] = {"stream_type": "BUFFERED"}
    if title or thumbnail:
        metadata: dict[str, Any] = {}
        if title:
            metadata["title"] = title
        if thumbnail:
            metadata["images"] = [{"url": thumbnail}]
        extra["metadata"] = metadata
    if subtitle:
        extra.update(
            {
                "subtitles": subtitle["url"],
                "subtitles_lang": subtitle["lang"],
                "subtitles_mime": subtitle["mime"],
                "subtitle_id": 1,
            }
        )
    return extra

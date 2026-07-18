"""Build and validate encode-first playback URLs for Chromecast 1st generation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import logging
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .api import StremioBridgeError, StremioStreamServerClient
from .const import (
    CHROMECAST_V1_AUDIO_BITRATE,
    CHROMECAST_V1_AUDIO_CHANNELS,
    CHROMECAST_V1_AUDIO_CODEC,
    CHROMECAST_V1_AUDIO_PROFILE,
    CHROMECAST_V1_AUDIO_SAMPLE_RATE,
    CHROMECAST_V1_MAX_FPS,
    CHROMECAST_V1_MAX_HEIGHT,
    CHROMECAST_V1_MAX_VIDEO_BITRATE,
    CHROMECAST_V1_MAX_WIDTH,
    CHROMECAST_V1_VIDEO_CODEC,
    CHROMECAST_V1_VIDEO_LEVEL,
    CHROMECAST_V1_VIDEO_PROFILE,
)

_LOGGER = logging.getLogger(__name__)
_HLS_MIME = "application/vnd.apple.mpegurl"


def _with_chromecast_v1_contract(url: str) -> str:
    """Attach an explicit Chromecast Gen 1 output contract to an HLS URL.

    Recent stream-server builds may ignore some of these compatibility keys. They
    remain useful for patched builds and make the requested output unambiguous.
    Validation still rejects a source when the resulting HLS manifest cannot be
    created.
    """
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(
        {
            "forceTranscoding": "1",
            "videoCodecs": CHROMECAST_V1_VIDEO_CODEC,
            "videoProfile": CHROMECAST_V1_VIDEO_PROFILE,
            "videoLevel": CHROMECAST_V1_VIDEO_LEVEL,
            "maxWidth": str(CHROMECAST_V1_MAX_WIDTH),
            "maxHeight": str(CHROMECAST_V1_MAX_HEIGHT),
            "maxFrameRate": str(CHROMECAST_V1_MAX_FPS),
            "maxVideoBitrate": str(CHROMECAST_V1_MAX_VIDEO_BITRATE),
            "audioCodecs": CHROMECAST_V1_AUDIO_CODEC,
            "audioProfile": CHROMECAST_V1_AUDIO_PROFILE,
            "maxAudioChannels": str(CHROMECAST_V1_AUDIO_CHANNELS),
            "audioBitrate": str(CHROMECAST_V1_AUDIO_BITRATE),
            "audioSampleRate": str(CHROMECAST_V1_AUDIO_SAMPLE_RATE),
        }
    )
    return urlunsplit(parsed._replace(query=urlencode(query)))


def prepare_playback(
    server: StremioStreamServerClient,
    stream: Mapping[str, Any],
    options: Mapping[str, Any],
    *,
    profile: str,
    cast_target: bool = False,
) -> tuple[str, str]:
    """Resolve one source and always request on-the-fly transcoding.

    ``options``, ``profile`` and ``cast_target`` stay in the signature for API
    compatibility with existing callers. This encode-focused variant deliberately
    ignores direct-play and format compatibility preferences.
    """
    del options, profile, cast_target
    resolved_url = server.resolve_stream(stream)
    encoded_url = server.build_compatible_hls_url(
        resolved_url,
        force_transcoding=True,
        max_audio_channels=CHROMECAST_V1_AUDIO_CHANNELS,
    )
    return _with_chromecast_v1_contract(encoded_url), _HLS_MIME


async def prepare_first_playable(
    server: StremioStreamServerClient,
    candidates: Sequence[dict[str, Any]],
    options: Mapping[str, Any],
    *,
    profile: str,
    cast_target: bool = False,
) -> tuple[dict[str, Any], str, str]:
    """Return the first candidate whose encoded HLS output validates.

    There is intentionally no direct-play fallback. If transcoding fails, the next
    ranked source is tried; returning the original MKV/HEVC/DTS source would defeat
    the compatibility guarantee this project is built around.
    """
    if not candidates:
        raise StremioBridgeError("No stream candidates are available")

    failures: list[str] = []
    for position, stream in enumerate(candidates):
        try:
            url, mime_type = prepare_playback(
                server,
                stream,
                options,
                profile=profile,
                cast_target=cast_target,
            )
        except StremioBridgeError as err:
            failures.append(str(err))
            continue

        valid, reason = await server.async_validate_media_url(url, mime_type)
        if valid:
            if position:
                _LOGGER.info(
                    "Selected encoded fallback stream %s after %s rejected candidate(s)",
                    position + 1,
                    position,
                )
            _LOGGER.debug("Prepared Chromecast Gen 1 HLS URL %s", url)
            return stream, url, mime_type

        failure = reason or "encoded HLS validation failed"
        failures.append(failure)
        _LOGGER.warning(
            "Skipping source %s because its encoded HLS output failed: %s",
            position + 1,
            failure,
        )

    detail = "; ".join(failures[-3:])
    raise StremioBridgeError(
        "All selected sources failed Chromecast Gen 1 transcoding"
        + (f": {detail}" if detail else "")
    )

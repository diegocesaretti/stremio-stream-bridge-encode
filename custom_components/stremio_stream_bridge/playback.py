"""Prepare player-compatible media URLs and validate automatic candidates."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import logging
from typing import Any

from .api import (
    StremioBridgeError,
    StremioStreamServerClient,
    guess_stream_mime_type,
)
from .const import (
    CONF_AUDIO_MODE,
    DEFAULT_AUDIO_MODE,
    PROFILE_SPORTS,
)
from .stream_selector import direct_play_compatibility_rank

_LOGGER = logging.getLogger(__name__)
_HLS_MIME = "application/vnd.apple.mpegurl"
_INCOMPATIBLE_AUDIO_MARKERS = (
    "dts",
    "truehd",
    "eac3",
    "e-ac-3",
    "ddp",
    "dolby digital plus",
    "ac3",
    "ac-3",
    "7.1",
)


def _stream_text(stream: Mapping[str, Any]) -> str:
    hints = stream.get("behaviorHints")
    filename = hints.get("filename") if isinstance(hints, Mapping) else None
    return " ".join(
        str(value).lower()
        for value in (
            stream.get("name"),
            stream.get("title"),
            stream.get("description"),
            filename,
        )
        if value
    )


def needs_compatible_hls(stream: Mapping[str, Any], resolved_url: str) -> bool:
    """Return whether the stream is likely to need AAC/HLS compatibility."""
    lowered_url = resolved_url.lower().split("?", 1)[0]
    if lowered_url.endswith((".m3u8", ".mpd")):
        return False
    if stream.get("infoHash"):
        return True
    text = _stream_text(stream)
    if text.endswith((".mkv", ".avi")) or ".mkv" in text or ".avi" in text:
        return True
    return any(marker in text for marker in _INCOMPATIBLE_AUDIO_MARKERS)


def prepare_playback(
    server: StremioStreamServerClient,
    stream: Mapping[str, Any],
    options: Mapping[str, Any],
    *,
    profile: str,
    cast_target: bool = False,
) -> tuple[str, str]:
    """Resolve a stream while keeping the proven direct path as the default.

    v0.4.0 attempted to route likely-incompatible files through ``hlsv2`` in
    automatic mode. Some stream-server builds expose the endpoint but cannot
    actually create the playlist, which broke media that previously played.
    Automatic is therefore a backwards-compatible alias for direct playback.
    HLS transcoding is used only when the user explicitly selects
    ``force_transcode``.
    """
    resolved_url = server.resolve_stream(stream)
    mode = str(options.get(CONF_AUDIO_MODE, DEFAULT_AUDIO_MODE))

    # Never wrap an existing live playlist. Its tokens, Referer headers and
    # relative segment URLs must remain exactly as supplied by the add-on.
    lowered_url = resolved_url.lower().split("?", 1)[0]
    is_playlist = lowered_url.endswith((".m3u8", ".mpd"))
    if mode != "force_transcode" or profile == PROFILE_SPORTS or is_playlist:
        return resolved_url, guess_stream_mime_type(
            stream, resolved_url, cast_target=cast_target
        )

    return (
        server.build_compatible_hls_url(
            resolved_url,
            force_transcoding=True,
            max_audio_channels=2,
        ),
        _HLS_MIME,
    )


async def prepare_first_playable(
    server: StremioStreamServerClient,
    candidates: Sequence[dict[str, Any]],
    options: Mapping[str, Any],
    *,
    profile: str,
    cast_target: bool = False,
) -> tuple[dict[str, Any], str, str]:
    """Resolve ranked candidates and fall back safely when HLS conversion fails."""
    if not candidates:
        raise StremioBridgeError("No stream candidates are available")

    failures: list[str] = []
    mode = str(options.get(CONF_AUDIO_MODE, DEFAULT_AUDIO_MODE))
    ordered_candidates = list(candidates)
    if cast_target and len(ordered_candidates) > 1:
        # Stable sort: direct-play compatibility wins, while the ideal-link
        # quality/seed/size order is preserved inside each compatibility tier.
        ordered_candidates.sort(key=direct_play_compatibility_rank)

    for position, stream in enumerate(ordered_candidates):
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
                    "Selected fallback stream %s after %s rejected candidate(s)",
                    position + 1,
                    position,
                )
            _LOGGER.debug("Prepared stream URL %s with MIME %s", url, mime_type)
            return stream, url, mime_type

        failures.append(reason or "playlist validation failed")
        _LOGGER.warning(
            "Skipping unavailable automatic stream candidate %s: %s",
            position + 1,
            reason or "validation failed",
        )

        # A failed hlsv2 conversion must never make a formerly-working stream
        # unusable. Restore the original stream-server URL immediately.
        if mode == "force_transcode" and "/hlsv2/" in url:
            try:
                direct_url = server.resolve_stream(stream)
                direct_mime = guess_stream_mime_type(
                    stream, direct_url, cast_target=cast_target
                )
                direct_valid, direct_reason = await server.async_validate_media_url(
                    direct_url, direct_mime
                )
            except StremioBridgeError as err:
                failures.append(str(err))
            else:
                if direct_valid:
                    _LOGGER.warning(
                        "hlsv2 audio conversion failed; falling back to direct playback"
                    )
                    return stream, direct_url, direct_mime
                failures.append(direct_reason or "direct fallback validation failed")

    detail = "; ".join(failures[-3:])
    raise StremioBridgeError(
        "All automatically selected stream links failed validation"
        + (f": {detail}" if detail else "")
    )


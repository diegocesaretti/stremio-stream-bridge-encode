"""Human-readable stream labels and automatic quality selection."""

from __future__ import annotations

import re
from typing import Any

from .aggregator import stream_key

_QUALITY_PATTERNS = (
    (2160, re.compile(r"(?:\b2160p?\b|\b4k\b|uhd)", re.IGNORECASE)),
    (1080, re.compile(r"\b1080[pi]?\b", re.IGNORECASE)),
    (720, re.compile(r"\b720[pi]?\b", re.IGNORECASE)),
    (480, re.compile(r"\b480[pi]?\b", re.IGNORECASE)),
    (360, re.compile(r"\b360[pi]?\b", re.IGNORECASE)),
)
_SIZE_RE = re.compile(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(TiB|TB|GiB|GB|MiB|MB)\b", re.IGNORECASE)
_SEED_PATTERNS = (
    re.compile(r"(?:👤|🌱|seeders?|seeds?)\s*[:=]?\s*(\d+)", re.IGNORECASE),
    re.compile(r"(\d+)\s*(?:seeders?|seeds?)\b", re.IGNORECASE),
)
_H264_NAME_RE = re.compile(
    r"(?<![a-z0-9])(?:x264|h[._ -]?264|avc)(?![a-z0-9])",
    re.IGNORECASE,
)
_H265_NAME_RE = re.compile(
    r"(?<![a-z0-9])(?:x265|h[._ -]?265|hevc)(?![a-z0-9])",
    re.IGNORECASE,
)


def stream_text(stream: dict[str, Any]) -> str:
    """Join fields commonly used by Stremio stream add-ons."""
    hints = stream.get("behaviorHints")
    filename = hints.get("filename") if isinstance(hints, dict) else None
    return "\n".join(
        str(value)
        for value in (
            stream.get("name"),
            stream.get("title"),
            stream.get("description"),
            filename,
        )
        if value
    )


def parse_quality(stream: dict[str, Any]) -> int:
    text = stream_text(stream)
    for value, pattern in _QUALITY_PATTERNS:
        if pattern.search(text):
            return value
    return 0


def parse_size_gb(stream: dict[str, Any]) -> float | None:
    hints = stream.get("behaviorHints")
    if isinstance(hints, dict):
        video_size = hints.get("videoSize")
        if isinstance(video_size, (int, float)) and video_size > 0:
            return float(video_size) / (1024**3)
    match = _SIZE_RE.search(stream_text(stream))
    if not match:
        return None
    value = float(match.group(1).replace(",", "."))
    unit = match.group(2).lower()
    if unit in {"tib", "tb"}:
        return value * 1024
    if unit in {"mib", "mb"}:
        return value / 1024
    return value


def parse_seeders(stream: dict[str, Any]) -> int:
    text = stream_text(stream)
    for pattern in _SEED_PATTERNS:
        if match := pattern.search(text):
            return int(match.group(1))
    return 0


def _source_text(stream: dict[str, Any]) -> str:
    hints = stream.get("behaviorHints")
    filename = str(hints.get("filename") or "") if isinstance(hints, dict) else ""
    url = str(stream.get("url") or "").split("?", 1)[0]
    return f"{filename} {url} {stream_text(stream)}".lower()


def parse_container(stream: dict[str, Any]) -> str | None:
    """Return a compact container label from URL or filename hints."""
    source = _source_text(stream)
    for extension, label in (
        (".m3u8", "HLS"),
        (".mpd", "DASH"),
        (".mp4", "MP4"),
        (".m4v", "MP4"),
        (".mkv", "MKV"),
        (".webm", "WebM"),
        (".avi", "AVI"),
        (".m2ts", "M2TS"),
        (".ts", "TS"),
    ):
        if extension in source:
            return label
    return None


def parse_named_video_codec(stream: dict[str, Any]) -> str | None:
    """Return H.264 or HEVC using only add-on names, descriptions and filename hints."""
    text = stream_text(stream)
    if _H265_NAME_RE.search(text):
        return "HEVC"
    if _H264_NAME_RE.search(text):
        return "H.264"
    return None


def _named_codec_rank(stream: dict[str, Any], *, h264_available: bool) -> int:
    """Prefer named H.264 results while retaining HEVC as a final fallback."""
    if not h264_available:
        return 0
    codec = parse_named_video_codec(stream)
    if codec == "H.264":
        return 0
    if codec == "HEVC":
        return 2
    return 1


def parse_video_codec(stream: dict[str, Any]) -> str | None:
    """Return the video codec advertised by common torrent release names."""
    if named_codec := parse_named_video_codec(stream):
        return named_codec
    source = _source_text(stream)
    if any(marker in source for marker in ("x265", "h265", "h.265", "hevc")):
        return "HEVC"
    if "av1" in source:
        return "AV1"
    if any(marker in source for marker in ("x264", "h264", "h.264", "avc")):
        return "H.264"
    if "vp9" in source:
        return "VP9"
    if "vp8" in source:
        return "VP8"
    return None


def parse_audio_codec(stream: dict[str, Any]) -> str | None:
    """Return the audio codec advertised by common torrent release names."""
    source = _source_text(stream)
    if "truehd" in source:
        return "TrueHD"
    if "dts" in source:
        return "DTS"
    if any(marker in source for marker in ("eac3", "e-ac-3", "ddp", "dd+")):
        return "E-AC-3"
    if any(marker in source for marker in ("ac3", "ac-3", "dolby digital")):
        return "AC-3"
    if "opus" in source:
        return "Opus"
    if "vorbis" in source:
        return "Vorbis"
    if "aac" in source:
        return "AAC"
    if "mp3" in source:
        return "MP3"
    return None


def parse_audio_channels(stream: dict[str, Any]) -> str | None:
    """Return an advertised channel layout when present in the release name."""
    source = _source_text(stream)
    if any(marker in source for marker in ("7.1", "7ch")):
        return "7.1"
    if any(marker in source for marker in ("5.1", "6ch")):
        return "5.1"
    if any(marker in source for marker in ("2.0", "2ch", "stereo")):
        return "2.0"
    return None


def cast_compatibility_tier(stream: dict[str, Any]) -> int:
    """Classify direct-play confidence for a conservative Google Cast target.

    0 is explicitly compatible, 1 is unknown/likely, and 2 is known risky.
    The ranking uses add-on release text, so it is deliberately conservative.
    """
    container = parse_container(stream)
    video = parse_video_codec(stream)
    audio = parse_audio_codec(stream)
    channels = parse_audio_channels(stream)

    if container in {"HLS", "DASH"}:
        return 0
    if container in {"MKV", "AVI"}:
        return 2
    if video in {"HEVC", "AV1"}:
        return 2
    if audio in {"DTS", "TrueHD", "E-AC-3", "AC-3"}:
        return 2
    if channels in {"5.1", "7.1"}:
        return 2

    explicitly_safe_container = container in {"MP4", "WebM", "TS", "M2TS"}
    explicitly_safe_video = video in {"H.264", "VP8", "VP9"}
    explicitly_safe_audio = audio in {"AAC", "MP3", "Opus", "Vorbis"}
    if explicitly_safe_container and explicitly_safe_video and explicitly_safe_audio:
        return 0
    return 1


def stream_label(stream: dict[str, Any], position: int | None = None) -> str:
    """Build a compact label suited to Home Assistant's media browser."""
    quality = parse_quality(stream)
    size = parse_size_gb(stream)
    seeders = parse_seeders(stream)
    container = parse_container(stream)
    video_codec = parse_video_codec(stream)
    audio_codec = parse_audio_codec(stream)
    audio_channels = parse_audio_channels(stream)
    provider = stream.get("_bridge_addon_name")
    parts: list[str] = []
    if position is not None:
        parts.append(str(position + 1))
    if quality:
        parts.append("4K" if quality == 2160 else f"{quality}p")
    if container:
        parts.append(container)
    if video_codec:
        parts.append(video_codec)
    if audio_codec:
        parts.append(audio_codec)
    if audio_channels:
        parts.append(audio_channels)
    if size is not None:
        parts.append(f"{size:.1f} GB" if size >= 1 else f"{size * 1024:.0f} MB")
    if seeders:
        parts.append(f"{seeders} semillas")
    if provider:
        parts.append(str(provider))
    if parts:
        return " · ".join(parts)
    text = stream_text(stream).replace("\n", " · ").strip()
    return text[:110] or "Stream"


def direct_play_compatibility_rank(stream: dict[str, Any]) -> tuple[int, int, int, int]:
    """Rank a stream for direct playback on Cast and browser players."""
    source = _source_text(stream)
    container = parse_container(stream)
    video = parse_video_codec(stream)
    audio = parse_audio_codec(stream)

    container_rank = {
        "HLS": 0,
        "DASH": 0,
        "MP4": 0,
        "WebM": 1,
        "TS": 2,
        "M2TS": 2,
        None: 3,
        "MKV": 5,
        "AVI": 6,
    }.get(container, 4)
    video_rank = {"H.264": 0, "VP9": 1, "VP8": 1, None: 2, "HEVC": 4, "AV1": 5}.get(
        video, 3
    )
    audio_rank = {
        "AAC": 0,
        "MP3": 0,
        "Opus": 1,
        "Vorbis": 1,
        None: 2,
        "AC-3": 3,
        "E-AC-3": 4,
        "DTS": 5,
        "TrueHD": 6,
    }.get(audio, 3)
    multichannel_rank = (
        1 if any(marker in source for marker in ("5.1", "7.1", "atmos")) else 0
    )
    return (
        cast_compatibility_tier(stream),
        container_rank,
        video_rank,
        audio_rank + multichannel_rank,
    )


def _filtered_candidates(
    streams: list[dict[str, Any]],
    max_size_gb: float,
    exclude_keywords: str,
) -> list[dict[str, Any]]:
    excluded = [word.strip().lower() for word in exclude_keywords.split(",") if word.strip()]

    def allowed(stream: dict[str, Any], enforce_size: bool = True) -> bool:
        text = stream_text(stream).lower()
        if any(
            re.search(
                rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])",
                text,
                re.IGNORECASE,
            )
            for keyword in excluded
        ):
            return False
        size = parse_size_gb(stream)
        return not (enforce_size and max_size_gb > 0 and size is not None and size > max_size_gb)

    candidates = [stream for stream in streams if allowed(stream)]
    if not candidates:
        candidates = [stream for stream in streams if allowed(stream, enforce_size=False)]
    return candidates or streams


def order_ideal_streams(
    streams: list[dict[str, Any]],
    max_size_gb: float,
    exclude_keywords: str,
    *,
    preferred_quality: str = "1080p",
    prefer_direct_play: bool = False,
    strict_compatibility: bool = False,
) -> list[dict[str, Any]]:
    """Rank all usable links for automatic playback and fallback.

    Compatibility is applied first when requested. When an H.264/x264 name is
    available it is preferred over unknown codecs and H.265/x265/HEVC names, then
    quality, seed count and file size decide the retry order.
    """
    if not streams:
        return []
    candidates = _filtered_candidates(streams, max_size_gb, exclude_keywords)
    if prefer_direct_play and strict_compatibility:
        compatible = [stream for stream in candidates if cast_compatibility_tier(stream) < 2]
        if compatible:
            candidates = compatible
    target_map = {"2160p": 2160, "1080p": 1080, "720p": 720, "480p": 480}
    target = target_map.get(preferred_quality)
    named_h264_available = any(
        parse_named_video_codec(stream) == "H.264" for stream in candidates
    )

    def quality_rank(quality: int) -> tuple[int, int]:
        if preferred_quality == "lowest":
            return (0 if quality else 1, quality or 9999)
        if target is not None:
            if quality == target:
                return (0, 0)
            if 0 < quality < target:
                return (1, target - quality)
            if quality > target:
                return (2, quality - target)
            return (3, 9999)
        # auto: prefer the highest known quality.
        return (0 if quality else 1, -quality)

    def rank(stream: dict[str, Any]) -> tuple[Any, ...]:
        size = parse_size_gb(stream)
        compatibility = (
            direct_play_compatibility_rank(stream) if prefer_direct_play else ()
        )
        return (
            *compatibility,
            _named_codec_rank(stream, h264_available=named_h264_available),
            *quality_rank(parse_quality(stream)),
            -parse_seeders(stream),
            size if size is not None else 9999,
            stream_key(stream),
        )

    return sorted(candidates, key=rank)


def choose_ideal_stream(
    streams: list[dict[str, Any]],
    max_size_gb: float,
    exclude_keywords: str,
) -> dict[str, Any]:
    """Choose the first ranked ideal link."""
    ordered = order_ideal_streams(streams, max_size_gb, exclude_keywords)
    if not ordered:
        raise ValueError("No streams to select")
    return ordered[0]


def choose_best_stream(
    streams: list[dict[str, Any]],
    preferred_quality: str,
    max_size_gb: float,
    exclude_keywords: str,
) -> dict[str, Any]:
    """Select a practical stream using quality, size, release tags and seeds."""
    if not streams:
        raise ValueError("No streams to select")
    candidates = _filtered_candidates(streams, max_size_gb, exclude_keywords)

    target_map = {"2160p": 2160, "1080p": 1080, "720p": 720, "480p": 480}
    target = target_map.get(preferred_quality)
    named_h264_available = any(
        parse_named_video_codec(stream) == "H.264" for stream in candidates
    )

    def quality_rank(quality: int) -> tuple[int, int]:
        if preferred_quality == "lowest":
            return (0 if quality else 1, quality or 9999)
        if target is not None:
            if quality == target:
                return (0, 0)
            if 0 < quality < target:
                return (1, target - quality)
            if quality > target:
                return (2, quality - target)
            return (3, 9999)
        return (0 if quality else 1, -quality)

    def rank(stream: dict[str, Any]) -> tuple[Any, ...]:
        quality = parse_quality(stream)
        size = parse_size_gb(stream)
        return (
            _named_codec_rank(stream, h264_available=named_h264_available),
            *quality_rank(quality),
            -parse_seeders(stream),
            size if size is not None else 9999,
            stream_key(stream),
        )

    return min(candidates, key=rank)

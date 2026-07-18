"""Human-readable stream labels and format-neutral source ranking."""

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
    """Return a compact container label for display only."""
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
    """Return a codec label for display only; it never changes ranking."""
    text = stream_text(stream)
    if _H265_NAME_RE.search(text):
        return "HEVC"
    if _H264_NAME_RE.search(text):
        return "H.264"
    return None


def parse_video_codec(stream: dict[str, Any]) -> str | None:
    """Return the video codec advertised by a release name, for labels only."""
    if named_codec := parse_named_video_codec(stream):
        return named_codec
    source = _source_text(stream)
    if "av1" in source:
        return "AV1"
    if "vp9" in source:
        return "VP9"
    if "vp8" in source:
        return "VP8"
    return None


def parse_audio_codec(stream: dict[str, Any]) -> str | None:
    """Return the audio codec advertised by a release name, for labels only."""
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
    """Return an advertised channel layout, for labels only."""
    source = _source_text(stream)
    if any(marker in source for marker in ("7.1", "7ch")):
        return "7.1"
    if any(marker in source for marker in ("5.1", "6ch")):
        return "5.1"
    if any(marker in source for marker in ("2.0", "2ch", "stereo")):
        return "2.0"
    return None


def cast_compatibility_tier(stream: dict[str, Any]) -> int:
    """Return a neutral compatibility tier.

    Kept for backwards-compatible imports. All formats are accepted because every
    selected source is expected to be transcoded before playback.
    """
    del stream
    return 0


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
    """Return a neutral rank retained for backwards-compatible imports."""
    del stream
    return (0, 0, 0, 0)


def _filtered_candidates(
    streams: list[dict[str, Any]],
    max_size_gb: float,
    exclude_keywords: str,
) -> list[dict[str, Any]]:
    """Apply non-format filters: bad release tags and optional maximum source size."""
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
    """Rank usable links by quality, seed count and size only.

    The direct-play flags are accepted for compatibility with older callers but are
    deliberately ignored. Containers and codecs never remove or promote a source.
    """
    del prefer_direct_play, strict_compatibility
    if not streams:
        return []
    candidates = _filtered_candidates(streams, max_size_gb, exclude_keywords)
    target_map = {"2160p": 2160, "1080p": 1080, "720p": 720, "480p": 480}
    target = target_map.get(preferred_quality)

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
        size = parse_size_gb(stream)
        return (
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
    """Choose the first ranked format-neutral link."""
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
    """Select a source by quality, seed count and size, never by format."""
    ordered = order_ideal_streams(
        streams,
        max_size_gb,
        exclude_keywords,
        preferred_quality=preferred_quality,
    )
    if not ordered:
        raise ValueError("No streams to select")
    return ordered[0]

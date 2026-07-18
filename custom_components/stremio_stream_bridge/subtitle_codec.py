"""Pure subtitle decoding and WebVTT conversion helpers."""

from __future__ import annotations

import gzip
from io import BytesIO
import re
from zipfile import BadZipFile, ZipFile

MAX_SUBTITLE_BYTES = 8 * 1024 * 1024
_SUPPORTED_ARCHIVE_SUFFIXES = (".srt", ".vtt", ".txt", ".sub")
_TIMING_LINE = re.compile(
    r"^(?P<start>\d{1,2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*"
    r"(?P<end>\d{1,2}:\d{2}:\d{2}[,.]\d{3})(?P<settings>.*)$"
)


class SubtitleDecodeError(ValueError):
    """Raised when subtitle bytes cannot be converted safely."""


def unpack_subtitle_bytes(payload: bytes) -> bytes:
    """Unpack a plain, gzip or ZIP subtitle payload."""
    if len(payload) > MAX_SUBTITLE_BYTES:
        raise SubtitleDecodeError("Subtitle response is too large")

    if payload.startswith(b"\x1f\x8b"):
        try:
            payload = gzip.decompress(payload)
        except OSError as err:
            raise SubtitleDecodeError("Invalid gzip subtitle response") from err
    elif payload.startswith(b"PK\x03\x04"):
        try:
            with ZipFile(BytesIO(payload)) as archive:
                candidates = [
                    info
                    for info in archive.infolist()
                    if not info.is_dir()
                    and info.filename.lower().endswith(_SUPPORTED_ARCHIVE_SUFFIXES)
                ]
                if not candidates:
                    raise SubtitleDecodeError("ZIP does not contain a supported subtitle")
                candidates.sort(key=lambda item: item.file_size)
                if candidates[0].file_size > MAX_SUBTITLE_BYTES:
                    raise SubtitleDecodeError("Subtitle inside ZIP is too large")
                payload = archive.read(candidates[0])
        except BadZipFile as err:
            raise SubtitleDecodeError("Invalid ZIP subtitle response") from err

    if len(payload) > MAX_SUBTITLE_BYTES:
        raise SubtitleDecodeError("Unpacked subtitle is too large")
    return payload


def decode_subtitle_text(payload: bytes) -> str:
    """Decode subtitle bytes using common encodings used by subtitle sites."""
    payload = unpack_subtitle_bytes(payload)
    encodings = ["utf-8-sig"]
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")) or (
        payload and payload.count(b"\x00") / len(payload) > 0.15
    ):
        encodings.append("utf-16")
    encodings.extend(("cp1252", "latin-1"))
    for encoding in encodings:
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise SubtitleDecodeError("Could not detect subtitle text encoding")


def subtitle_to_webvtt(text: str) -> str:
    """Convert common SubRip text into a standards-friendly WebVTT document."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    if normalized.lstrip().upper().startswith("WEBVTT"):
        body = normalized.lstrip()
        return body if body.endswith("\n") else f"{body}\n"

    upper_head = normalized[:200].upper()
    if "[SCRIPT INFO]" in upper_head or "[V4+ STYLES]" in upper_head:
        raise SubtitleDecodeError("ASS/SSA subtitles are not supported by the lightweight converter")

    lines = normalized.split("\n")
    result = ["WEBVTT", ""]
    index = 0
    cue_count = 0
    while index < len(lines):
        line = lines[index].strip("\ufeff")
        if not line.strip():
            if result and result[-1] != "":
                result.append("")
            index += 1
            continue

        # SubRip numeric cue identifiers are optional in WebVTT and are best removed.
        if line.strip().isdigit() and index + 1 < len(lines):
            if _TIMING_LINE.match(lines[index + 1].strip()):
                index += 1
                line = lines[index].strip()

        timing = _TIMING_LINE.match(line.strip())
        if timing:
            cue_count += 1
            start = timing.group("start").replace(",", ".")
            end = timing.group("end").replace(",", ".")
            result.append(f"{start} --> {end}{timing.group('settings')}")
        else:
            result.append(line)
        index += 1

    if cue_count == 0:
        raise SubtitleDecodeError("Subtitle does not contain valid SRT/WebVTT cue timings")
    return "\n".join(result).rstrip() + "\n"

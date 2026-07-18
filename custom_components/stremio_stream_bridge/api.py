"""HTTP clients and stream resolution for Stremio Stream Bridge."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
import logging
import mimetypes
import re
import secrets
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

from aiohttp import ClientError, ClientSession, ClientTimeout

REQUEST_TIMEOUT = ClientTimeout(total=20)
_TORRENT_MEDIA_PATH_RE = re.compile(r"/[0-9a-fA-F]{40,64}/-?\d+$")
_LOGGER = logging.getLogger(__name__)


class StremioBridgeError(Exception):
    """Base error for Stremio Stream Bridge."""


class StremioConnectionError(StremioBridgeError):
    """Raised when a remote service cannot be reached."""


class StremioProtocolError(StremioBridgeError):
    """Raised when a Stremio response is invalid or unsupported."""


def normalize_url(url: str) -> str:
    """Normalize and validate an HTTP(S) URL."""
    value = url.strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise StremioProtocolError(f"Invalid HTTP(S) URL: {url}")
    return value.rstrip("/")


class StremioAddonClient:
    """Minimal client for the Stremio add-on protocol."""

    def __init__(self, session: ClientSession, manifest_url: str) -> None:
        self._session = session
        self.manifest_url = self._normalize_manifest_url(manifest_url)
        self.base_url = self.manifest_url[: -len("/manifest.json")]

    @staticmethod
    def _normalize_manifest_url(manifest_url: str) -> str:
        value = normalize_url(manifest_url)
        if value.endswith("/manifest.json"):
            return value
        if value.endswith("manifest.json"):
            return value[: -len("manifest.json")].rstrip("/") + "/manifest.json"
        return f"{value}/manifest.json"

    async def _get_json(self, url: str) -> dict[str, Any]:
        try:
            async with self._session.get(url, timeout=REQUEST_TIMEOUT) as response:
                response.raise_for_status()
                payload = await response.json(content_type=None)
        except (ClientError, TimeoutError, ValueError) as err:
            raise StremioConnectionError(f"Failed requesting {url}: {err}") from err

        if not isinstance(payload, dict):
            raise StremioProtocolError(f"Expected JSON object from {url}")
        return payload

    async def get_manifest(self) -> dict[str, Any]:
        """Return and minimally validate the add-on manifest."""
        manifest = await self._get_json(self.manifest_url)
        for key in ("id", "name", "version", "resources"):
            if key not in manifest:
                raise StremioProtocolError(f"Manifest is missing required key: {key}")
        return manifest

    async def get_catalog(
        self,
        media_type: str,
        catalog_id: str,
        extra: Mapping[str, str] | str | None = None,
    ) -> list[dict[str, Any]]:
        """Return catalog metadata previews."""
        path = f"catalog/{quote(media_type, safe='')}/{quote(catalog_id, safe='')}"
        if extra:
            extra_value = urlencode(extra) if isinstance(extra, Mapping) else str(extra)
            path += f"/{quote(extra_value, safe='=&,:%+')}"
        payload = await self._get_json(f"{self.base_url}/{path}.json")
        metas = payload.get("metas", [])
        if not isinstance(metas, list):
            raise StremioProtocolError("Catalog response does not contain a metas list")
        return [meta for meta in metas if isinstance(meta, dict)]

    async def get_meta(self, media_type: str, media_id: str) -> dict[str, Any]:
        """Return full metadata for one item."""
        url = f"{self.base_url}/meta/{quote(media_type, safe='')}/{quote(media_id, safe='')}.json"
        payload = await self._get_json(url)
        meta = payload.get("meta")
        if not isinstance(meta, dict):
            raise StremioProtocolError("Meta response does not contain a meta object")
        return meta

    async def get_streams(self, media_type: str, media_id: str) -> list[dict[str, Any]]:
        """Return streams for one movie, episode or video."""
        url = f"{self.base_url}/stream/{quote(media_type, safe='')}/{quote(media_id, safe='')}.json"
        payload = await self._get_json(url)
        streams = payload.get("streams", [])
        if not isinstance(streams, list):
            raise StremioProtocolError("Stream response does not contain a streams list")
        return [stream for stream in streams if isinstance(stream, dict)]

    async def get_subtitles(
        self,
        media_type: str,
        media_id: str,
        extra: Mapping[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return subtitle tracks for one movie or episode."""
        base_path = f"subtitles/{quote(media_type, safe='')}/{quote(media_id, safe='')}"
        path = base_path
        if extra:
            extra_value = urlencode(extra)
            path += f"/{quote(extra_value, safe='=&,:%+')}"
        try:
            payload = await self._get_json(f"{self.base_url}/{path}.json")
        except StremioConnectionError:
            if not extra:
                raise
            # Some deployed subtitle add-ons declare the standard extras but only
            # route the plain resource URL. Retry by IMDb/episode id alone.
            _LOGGER.debug(
                "Subtitle request with extras failed for %s; retrying without extras",
                self.manifest_url,
            )
            payload = await self._get_json(f"{self.base_url}/{base_path}.json")
        subtitles = payload.get("subtitles", [])
        if not isinstance(subtitles, list):
            raise StremioProtocolError("Subtitle response does not contain a subtitles list")
        return [subtitle for subtitle in subtitles if isinstance(subtitle, dict)]


class StremioStreamServerClient:
    """Client and URL resolver for the Stremio streaming server."""

    def __init__(self, session: ClientSession, base_url: str) -> None:
        self._session = session
        self.base_url = f"{normalize_url(base_url)}/"

    async def get_settings(self) -> dict[str, Any]:
        """Check server connectivity and return settings."""
        url = f"{self.base_url}settings"
        try:
            async with self._session.get(url, timeout=REQUEST_TIMEOUT) as response:
                response.raise_for_status()
                payload = await response.json(content_type=None)
        except (ClientError, TimeoutError, ValueError) as err:
            raise StremioConnectionError(f"Failed requesting {url}: {err}") from err

        if not isinstance(payload, dict):
            raise StremioProtocolError("Streaming server settings response is not an object")
        return payload

    async def async_validate_media_url(self, url: str, mime_type: str) -> tuple[bool, str | None]:
        """Validate playlists and pre-warm direct torrent streams.

        Cast receivers fail fast when the streaming server is still resolving the
        torrent metadata.  A tiny Range request gives the server time to select the
        file and obtain the first piece, and lets automatic playback fall back to a
        different source when a torrent is dead.
        """
        parsed = urlsplit(url)
        lowered = url.lower().split("?", 1)[0]
        is_local_torrent = (
            url.startswith(self.base_url)
            and _TORRENT_MEDIA_PATH_RE.fullmatch(parsed.path) is not None
        )
        is_playlist = mime_type in {
            "application/vnd.apple.mpegurl",
            "application/x-mpegurl",
            "application/dash+xml",
        } or lowered.endswith((".m3u8", ".mpd"))

        if is_local_torrent:
            try:
                async with self._session.get(
                    url,
                    headers={"Range": "bytes=0-65535"},
                    timeout=ClientTimeout(total=35),
                ) as response:
                    if response.status not in {200, 206}:
                        return False, f"torrent prebuffer returned HTTP {response.status}"
                    content_type = response.headers.get("Content-Type", "").lower()
                    if content_type.startswith(("application/json", "text/html")):
                        return False, f"torrent endpoint returned {content_type or 'an error page'}"
                    initial_chunk = await response.content.read(65536)
                    if not initial_chunk:
                        return False, "torrent endpoint returned no media data"
            except (ClientError, TimeoutError) as err:
                return False, f"torrent prebuffer failed: {err}"
            return True, None

        if not is_playlist or not any(marker in url for marker in ("/proxy/", "/hlsv2/")):
            return True, None

        timeout = ClientTimeout(total=30 if "/hlsv2/" in url else 12)
        try:
            async with self._session.get(url, timeout=timeout) as response:
                if response.status >= 400:
                    return False, f"HTTP {response.status}"
                payload = await response.content.read(131072)
        except (ClientError, TimeoutError) as err:
            return False, str(err)

        if lowered.endswith(".m3u8") or "mpegurl" in mime_type:
            text = payload.decode("utf-8", errors="ignore")
            if "#EXTM3U" not in text:
                return False, "response is not an HLS manifest"
        elif lowered.endswith(".mpd") or mime_type == "application/dash+xml":
            text = payload.decode("utf-8", errors="ignore").lower()
            if "<mpd" not in text:
                return False, "response is not a DASH manifest"
        return True, None

    def resolve_stream(self, stream: Mapping[str, Any]) -> str:
        """Convert a Stremio stream object into a URL playable by a media player."""
        raw_url = stream.get("url")
        if isinstance(raw_url, str) and raw_url:
            if raw_url.startswith("magnet:"):
                return self.resolve_magnet(raw_url)
            if raw_url.startswith(("http://", "https://")):
                return self._resolve_http_url(raw_url, stream)

        external_url = stream.get("externalUrl")
        if isinstance(external_url, str) and external_url.startswith(("http://", "https://")):
            return external_url

        info_hash = stream.get("infoHash")
        if isinstance(info_hash, str) and info_hash:
            file_idx = stream.get("fileIdx", -1)
            if file_idx is None:
                file_idx = -1
            try:
                file_idx = int(file_idx)
            except (TypeError, ValueError) as err:
                raise StremioProtocolError("Invalid fileIdx in stream response") from err

            trackers = self._extract_trackers(stream)
            filename = self._extract_filename(stream)
            return self.build_torrent_url(info_hash, file_idx, trackers, filename)

        yt_id = stream.get("ytId")
        if isinstance(yt_id, str) and yt_id:
            return f"{self.base_url}yt/{quote(yt_id, safe='')}"

        raise StremioProtocolError("Unsupported Stremio stream object")

    def resolve_magnet(self, magnet_url: str) -> str:
        """Convert a magnet URI to a Stremio streaming server URL."""
        parsed = urlsplit(magnet_url)
        query = parse_qs(parsed.query)
        exact_topics = query.get("xt", [])
        btih = next(
            (
                value[len("urn:btih:") :]
                for value in exact_topics
                if value.lower().startswith("urn:btih:")
            ),
            None,
        )
        if not btih:
            raise StremioProtocolError("Magnet URI does not contain a BTIH hash")
        info_hash = self._normalize_btih(btih)
        trackers = query.get("tr", [])
        display_name = query.get("dn", [None])[0]
        return self.build_torrent_url(info_hash, -1, trackers, display_name)

    def build_subtitle_vtt_url(self, subtitle_url: str) -> str:
        """Proxy and convert an external subtitle to WebVTT through stream-server."""
        if not subtitle_url.startswith(("http://", "https://")):
            raise StremioProtocolError("Subtitle URL must use HTTP(S)")
        return f"{self.base_url}subtitles.vtt?{urlencode({'from': subtitle_url})}"

    def build_compatible_hls_url(
        self,
        media_url: str,
        *,
        force_transcoding: bool = False,
        max_audio_channels: int = 2,
    ) -> str:
        """Build Stremio hlsv2 URL targeting H.264 video and AAC audio."""
        if not media_url.startswith(("http://", "https://")):
            raise StremioProtocolError("HLS source URL must use HTTP(S)")
        params: list[tuple[str, str]] = [
            ("mediaURL", media_url),
            ("videoCodecs", "h264"),
            ("audioCodecs", "aac"),
            ("maxAudioChannels", str(max(1, int(max_audio_channels)))),
        ]
        if force_transcoding:
            params.append(("forceTranscoding", "1"))
        session_id = secrets.token_hex(12)
        return f"{self.base_url}hlsv2/{session_id}/master.m3u8?{urlencode(params)}"

    def build_torrent_url(
        self,
        info_hash: str,
        file_idx: int = -1,
        trackers: list[str] | None = None,
        filename: str | None = None,
    ) -> str:
        """Build /{infoHash}/{fileIdx} URL understood by stream-server."""
        normalized_hash = self._normalize_btih(info_hash)
        base = f"{self.base_url}{quote(normalized_hash, safe='')}/{file_idx}"
        params: list[tuple[str, str]] = []
        for tracker in trackers or []:
            if tracker:
                params.append(("tr", tracker))
        if filename:
            params.append(("f", filename))
        return f"{base}?{urlencode(params)}" if params else base

    def _resolve_http_url(self, raw_url: str, stream: Mapping[str, Any]) -> str:
        behavior_hints = stream.get("behaviorHints")
        if not isinstance(behavior_hints, Mapping):
            return raw_url
        proxy_headers = behavior_hints.get("proxyHeaders")
        if not isinstance(proxy_headers, Mapping):
            return raw_url

        request_headers = proxy_headers.get("request")
        response_headers = proxy_headers.get("response")
        if not isinstance(request_headers, Mapping) and not isinstance(response_headers, Mapping):
            return raw_url

        parsed = urlsplit(raw_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        proxy_params: list[tuple[str, str]] = [("d", origin)]
        if isinstance(request_headers, Mapping):
            proxy_params.extend(("h", f"{key}:{value}") for key, value in request_headers.items())
        if isinstance(response_headers, Mapping):
            proxy_params.extend(("r", f"{key}:{value}") for key, value in response_headers.items())

        proxy_descriptor = urlencode(proxy_params)
        path = parsed.path.lstrip("/")
        proxy_url = f"{self.base_url}proxy/{proxy_descriptor}/{path}"
        return urlunsplit(("", "", proxy_url, parsed.query, parsed.fragment))

    @staticmethod
    def _extract_trackers(stream: Mapping[str, Any]) -> list[str]:
        values: list[str] = []
        for field in ("sources", "announce"):
            raw_sources = stream.get(field)
            if not isinstance(raw_sources, list):
                continue
            for source in raw_sources:
                if not isinstance(source, str) or source.startswith("dht:"):
                    continue
                if source.startswith("tracker:"):
                    source = source[len("tracker:") :]
                if source:
                    values.append(source)
        return list(dict.fromkeys(values))

    @staticmethod
    def _extract_filename(stream: Mapping[str, Any]) -> str | None:
        behavior_hints = stream.get("behaviorHints")
        if not isinstance(behavior_hints, Mapping):
            return None
        filename = behavior_hints.get("filename")
        return filename if isinstance(filename, str) and filename else None

    @staticmethod
    def _normalize_btih(value: str) -> str:
        raw = value.strip()
        if len(raw) in {40, 64} and all(char in "0123456789abcdefABCDEF" for char in raw):
            return raw.lower()
        if len(raw) in {32, 52}:
            try:
                padding = "=" * ((8 - len(raw) % 8) % 8)
                decoded = base64.b32decode(raw.upper() + padding)
            except (binascii.Error, ValueError) as err:
                raise StremioProtocolError("Invalid base32 BTIH hash") from err
            return decoded.hex()
        raise StremioProtocolError("Unsupported BTIH hash format")


def guess_mime_type(url: str) -> str:
    """Guess a practical MIME type for Home Assistant media_player.play_media."""
    lowered = url.lower().split("?", 1)[0]
    if lowered.endswith(".m3u8"):
        return "application/vnd.apple.mpegurl"
    if lowered.endswith(".mpd"):
        return "application/dash+xml"
    mime_type, _ = mimetypes.guess_type(lowered)
    return mime_type or "video/mp4"


def guess_stream_mime_type(
    stream: Mapping[str, Any],
    resolved_url: str,
    *,
    cast_target: bool = False,
) -> str:
    """Return a practical MIME type for the resolved resource.

    Cast's default receiver does not advertise Matroska support.  Earlier bridge
    versions labelled stream-server torrent endpoints as ``video/mp4`` because
    their URL has no extension; that allowed some MKV files to play video even
    when the audio codec was unsupported.  Preserve that compatibility as a last
    resort while automatic selection prefers genuinely Cast-safe MP4 sources.
    """
    lowered_url = resolved_url.lower().split("?", 1)[0]
    if lowered_url.endswith(".m3u8"):
        return "application/vnd.apple.mpegurl"
    if lowered_url.endswith(".mpd"):
        return "application/dash+xml"

    parsed = urlsplit(resolved_url)
    torrent_endpoint = _TORRENT_MEDIA_PATH_RE.fullmatch(parsed.path) is not None
    hints = stream.get("behaviorHints")
    if isinstance(hints, Mapping):
        filename = hints.get("filename")
        if isinstance(filename, str) and filename:
            mime_type, _ = mimetypes.guess_type(filename.lower())
            if cast_target and torrent_endpoint and mime_type in {
                "video/matroska",
                "video/x-matroska",
                "video/x-msvideo",
            }:
                return "video/mp4"
            if mime_type:
                return mime_type
    return guess_mime_type(resolved_url)


def parse_manifest_urls(value: object) -> list[str]:
    """Parse manifest URLs stored as a list or entered one per line."""
    if isinstance(value, list):
        raw_values = [str(item) for item in value]
    elif isinstance(value, str):
        # Stremio configured manifest URLs commonly contain commas inside their
        # encoded provider/language settings. Treat only line breaks as separators.
        raw_values = value.splitlines()
    else:
        raw_values = []
    result: list[str] = []
    for raw in raw_values:
        raw = raw.strip()
        if not raw:
            continue
        normalized = StremioAddonClient._normalize_manifest_url(raw)
        if normalized not in result:
            result.append(normalized)
    return result

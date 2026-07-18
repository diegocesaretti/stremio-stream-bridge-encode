"""Ephemeral WebVTT proxy served directly by Home Assistant."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging
import secrets
from time import monotonic
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .subtitle_codec import SubtitleDecodeError, decode_subtitle_text, subtitle_to_webvtt

_LOGGER = logging.getLogger(__name__)
_PROXY_PATH = "/api/stremio_stream_bridge/subtitle/{token}.vtt"
_CACHE_SECONDS = int(timedelta(hours=6).total_seconds())
_FETCH_TIMEOUT = ClientTimeout(total=30)


class SubtitleProxyError(Exception):
    """Raised when a subtitle cannot be downloaded or exposed."""


@dataclass(slots=True)
class _CachedSubtitle:
    content: bytes
    expires_at: float


class SubtitleProxy:
    """Download, normalize and temporarily expose subtitle tracks."""

    def __init__(self, hass: HomeAssistant, session: ClientSession) -> None:
        self.hass = hass
        self._session = session
        self._cache: dict[str, _CachedSubtitle] = {}

    async def async_prepare_url(
        self, source_url: str, base_url_override: str | None = None
    ) -> str:
        """Download a subtitle now and return a LAN-reachable WebVTT URL."""
        parsed = urlsplit(source_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise SubtitleProxyError("Subtitle provider returned an invalid URL")

        try:
            async with self._session.get(
                source_url,
                timeout=_FETCH_TIMEOUT,
                allow_redirects=True,
                headers={"User-Agent": "Home Assistant Stremio Stream Bridge/0.3.1"},
            ) as response:
                response.raise_for_status()
                payload = await response.read()
        except (ClientError, TimeoutError) as err:
            raise SubtitleProxyError(f"Could not download subtitle: {err}") from err

        try:
            text = decode_subtitle_text(payload)
            webvtt = subtitle_to_webvtt(text).encode("utf-8")
        except SubtitleDecodeError as err:
            raise SubtitleProxyError(str(err)) from err

        base_url = self._resolve_base_url(base_url_override)
        self._purge_expired()
        token = secrets.token_urlsafe(24)
        self._cache[token] = _CachedSubtitle(
            content=webvtt,
            expires_at=monotonic() + _CACHE_SECONDS,
        )
        return f"{base_url}{_PROXY_PATH.format(token=token)}"

    def get(self, token: str) -> bytes | None:
        """Return cached WebVTT bytes for a valid token."""
        self._purge_expired()
        cached = self._cache.get(token)
        return cached.content if cached else None

    def _resolve_base_url(self, override: str | None) -> str:
        if override and override.strip():
            value = override.strip().rstrip("/")
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise SubtitleProxyError("Subtitle base URL must be a valid HTTP(S) URL")
            return value
        try:
            return get_url(
                self.hass,
                prefer_external=False,
                allow_internal=True,
                allow_external=True,
                allow_cloud=False,
            ).rstrip("/")
        except NoURLAvailableError as err:
            raise SubtitleProxyError(
                "Home Assistant has no usable internal URL; configure the subtitle base URL"
            ) from err

    def _purge_expired(self) -> None:
        now = monotonic()
        expired = [token for token, item in self._cache.items() if item.expires_at <= now]
        for token in expired:
            self._cache.pop(token, None)


class SubtitleProxyView(HomeAssistantView):
    """Serve one already-downloaded WebVTT track to a media receiver."""

    url = _PROXY_PATH
    name = "api:stremio_stream_bridge:subtitle"
    requires_auth = False

    def __init__(self, proxy: SubtitleProxy) -> None:
        self._proxy = proxy

    async def get(self, request: web.Request, token: str) -> web.Response:
        content = self._proxy.get(token)
        if content is None:
            raise web.HTTPNotFound()
        return web.Response(
            body=content,
            content_type="text/vtt",
            charset="utf-8",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": f"public, max-age={_CACHE_SECONDS}",
                "Content-Disposition": 'inline; filename="subtitle.vtt"',
            },
        )

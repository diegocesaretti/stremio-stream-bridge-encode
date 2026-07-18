"""Config flow for Stremio Stream Bridge."""

from __future__ import annotations

import hashlib
import logging
import socket
from typing import Any
from urllib.parse import urlsplit

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .aggregator import StremioAddonManager, manifest_has_resource
from .api import (
    StremioAddonClient,
    StremioBridgeError,
    StremioConnectionError,
    StremioStreamServerClient,
    normalize_url,
    parse_manifest_urls,
)
from .const import (
    AUDIO_MODE_OPTIONS,
    CONF_AUDIO_MODE,
    CONF_CATALOG_MANIFEST_URLS,
    CONF_CAST_COMPATIBILITY_FILTER,
    CONF_CAST_RESET_BEFORE_PLAY,
    CONF_DEFAULT_MEDIA_PLAYER,
    CONF_EXCLUDE_KEYWORDS,
    CONF_FAILURE_NOTIFY_HA,
    CONF_FALLBACK_ENABLED,
    CONF_FALLBACK_SOURCE_COUNT,
    CONF_IDEAL_LINK_FILTER,
    CONF_LATIN_MANIFEST_URLS,
    CONF_MAX_SIZE_GB,
    CONF_PLAYBACK_START_TIMEOUT,
    CONF_PLAY_IDEAL_ON_SELECT,
    CONF_PREFERRED_QUALITY,
    CONF_SPORTS_MANIFEST_URLS,
    CONF_STREAM_MANIFEST_URLS,
    CONF_STREAMING_SERVER_URL,
    CONF_STOP_BEFORE_PLAY,
    CONF_SUBTITLE_BASE_URL,
    CONF_SUBTITLE_CONVERT_VTT,
    CONF_SUBTITLE_LANGUAGES,
    CONF_SUBTITLE_MANIFEST_URLS,
    CONF_SUBTITLE_MODE,
    CONF_TVOVERLAY_DURATION,
    CONF_TVOVERLAY_ENABLED,
    CONF_TVOVERLAY_SERVICE,
    CONF_TVOVERLAY_TARGET,
    DEFAULT_AUDIO_MODE,
    DEFAULT_CAST_COMPATIBILITY_FILTER,
    DEFAULT_CAST_RESET_BEFORE_PLAY,
    DEFAULT_CINEMETA_MANIFEST,
    DEFAULT_EXCLUDE_KEYWORDS,
    DEFAULT_FAILURE_NOTIFY_HA,
    DEFAULT_FALLBACK_ENABLED,
    DEFAULT_FALLBACK_SOURCE_COUNT,
    DEFAULT_IDEAL_LINK_FILTER,
    DEFAULT_LATIN_MANIFEST,
    DEFAULT_MAX_SIZE_GB,
    DEFAULT_OPENSUBTITLES_MANIFEST,
    DEFAULT_PLAYBACK_START_TIMEOUT,
    DEFAULT_PLAY_IDEAL_ON_SELECT,
    DEFAULT_PREFERRED_QUALITY,
    DEFAULT_SPORTS_MANIFEST,
    DEFAULT_STOP_BEFORE_PLAY,
    DEFAULT_STREAMING_SERVER_URL,
    DEFAULT_SUBTITLE_BASE_URL,
    DEFAULT_SUBTITLE_CONVERT_VTT,
    DEFAULT_SUBTITLE_LANGUAGES,
    DEFAULT_SUBTITLE_MODE,
    DEFAULT_TVOVERLAY_DURATION,
    DEFAULT_TVOVERLAY_ENABLED,
    DEFAULT_TVOVERLAY_SERVICE,
    DEFAULT_TVOVERLAY_TARGET,
    DEFAULT_TORRENTIO_MANIFEST,
    DOMAIN,
    QUALITY_OPTIONS,
    SUBTITLE_MODE_OPTIONS,
)

_LOGGER = logging.getLogger(__name__)


def _as_lines(value: object, fallback: str = "") -> str:
    if value is None:
        return fallback
    return "\n".join(parse_manifest_urls(value))


def _clients(session, urls: list[str]) -> list[StremioAddonClient]:
    return [StremioAddonClient(session, url) for url in urls]


def _discover_lan_base_url(streaming_server_url: str, home_assistant_port: int) -> str:
    """Discover the HA LAN address used to reach the configured PC."""
    parsed = urlsplit(streaming_server_url)
    host = parsed.hostname
    if not host:
        return ""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    target: tuple[Any, ...]
    if family == socket.AF_INET6:
        target = (host, parsed.port or 11470, 0, 0)
    else:
        target = (host, parsed.port or 11470)
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        sock.connect(target)
        local_ip = str(sock.getsockname()[0])
    if not local_ip or local_ip.startswith("127.") or local_ip == "::1":
        return ""
    host_text = f"[{local_ip}]" if ":" in local_ip else local_ip
    return f"http://{host_text}:{home_assistant_port}"


async def _recommended_subtitle_base_url(hass, server_url: str) -> str:
    api = getattr(hass.config, "api", None)
    port = int(getattr(api, "port", 8123) or 8123)
    try:
        discovered = await hass.async_add_executor_job(
            _discover_lan_base_url, server_url, port
        )
        if discovered:
            return discovered
    except (OSError, ValueError):
        pass
    try:
        return get_url(
            hass,
            allow_internal=True,
            allow_external=False,
            allow_cloud=False,
            allow_ip=True,
        )
    except NoURLAvailableError:
        return ""


def _connection_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    player_key = (
        vol.Required(
            CONF_DEFAULT_MEDIA_PLAYER,
            default=defaults[CONF_DEFAULT_MEDIA_PLAYER],
        )
        if defaults.get(CONF_DEFAULT_MEDIA_PLAYER)
        else vol.Required(CONF_DEFAULT_MEDIA_PLAYER)
    )
    return vol.Schema(
        {
            vol.Required(
                CONF_STREAMING_SERVER_URL,
                default=defaults.get(
                    CONF_STREAMING_SERVER_URL, DEFAULT_STREAMING_SERVER_URL
                ),
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
            vol.Required(
                CONF_CATALOG_MANIFEST_URLS,
                default=_as_lines(
                    defaults.get(CONF_CATALOG_MANIFEST_URLS), DEFAULT_CINEMETA_MANIFEST
                ),
            ): TextSelector(TextSelectorConfig(multiline=True)),
            vol.Required(
                CONF_STREAM_MANIFEST_URLS,
                default=_as_lines(
                    defaults.get(CONF_STREAM_MANIFEST_URLS), DEFAULT_TORRENTIO_MANIFEST
                ),
            ): TextSelector(TextSelectorConfig(multiline=True)),
            vol.Optional(
                CONF_SUBTITLE_MANIFEST_URLS,
                default=_as_lines(
                    defaults.get(CONF_SUBTITLE_MANIFEST_URLS),
                    DEFAULT_OPENSUBTITLES_MANIFEST,
                ),
            ): TextSelector(TextSelectorConfig(multiline=True)),
            vol.Optional(
                CONF_LATIN_MANIFEST_URLS,
                default=_as_lines(
                    defaults.get(CONF_LATIN_MANIFEST_URLS), DEFAULT_LATIN_MANIFEST
                ),
            ): TextSelector(TextSelectorConfig(multiline=True)),
            vol.Optional(
                CONF_SPORTS_MANIFEST_URLS,
                default=_as_lines(
                    defaults.get(CONF_SPORTS_MANIFEST_URLS), DEFAULT_SPORTS_MANIFEST
                ),
            ): TextSelector(TextSelectorConfig(multiline=True)),
            player_key: EntitySelector(EntitySelectorConfig(domain="media_player")),
        }
    )


async def _validate(
    hass,
    server_url: str,
    catalog_urls: list[str],
    stream_urls: list[str],
    subtitle_urls: list[str] | None = None,
    latin_urls: list[str] | None = None,
    sports_urls: list[str] | None = None,
) -> tuple[StremioAddonManager, dict[str, Any]]:
    """Validate the mandatory core; optional profiles are best-effort."""
    session = async_get_clientsession(hass)
    server = StremioStreamServerClient(session, server_url)
    manager = StremioAddonManager(
        _clients(session, catalog_urls),
        _clients(session, stream_urls),
        _clients(session, subtitle_urls or []),
        _clients(session, latin_urls or []),
        _clients(session, sports_urls or []),
    )
    settings = await server.get_settings()
    await manager.async_refresh()
    if not manager.catalogs():
        raise StremioBridgeError("No configured add-on exposes catalogs")
    if not any(
        "stream" in addon.roles and manifest_has_resource(addon.manifest, "stream")
        for addon in manager.addons
    ):
        raise StremioBridgeError("No default stream provider was loaded")

    # Do not reject the whole integration because a third-party optional provider
    # is temporarily unavailable. Its error remains visible on the connectivity sensor.
    optional_urls = set((subtitle_urls or []) + (latin_urls or []) + (sports_urls or []))
    optional_errors = {
        url: error for url, error in manager.errors.items() if url in optional_urls
    }
    if optional_errors:
        _LOGGER.warning("Optional Stremio providers unavailable: %s", optional_errors)
    return manager, settings


def _description_placeholders(
    recommended_subtitle_url: str = "",
) -> dict[str, str]:
    return {
        "recommended_server_url": DEFAULT_STREAMING_SERVER_URL,
        "recommended_subtitle_base_url": recommended_subtitle_url
        or "http://IP_DE_HOME_ASSISTANT:8123",
        "default_latin_manifest": DEFAULT_LATIN_MANIFEST,
        "default_sports_manifest": DEFAULT_SPORTS_MANIFEST,
    }


class StremioStreamBridgeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle configuration from the Home Assistant UI."""

    VERSION = 8

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                server_url = normalize_url(user_input[CONF_STREAMING_SERVER_URL])
                catalog_urls = parse_manifest_urls(user_input[CONF_CATALOG_MANIFEST_URLS])
                stream_urls = parse_manifest_urls(user_input[CONF_STREAM_MANIFEST_URLS])
                subtitle_urls = parse_manifest_urls(
                    user_input.get(CONF_SUBTITLE_MANIFEST_URLS, "")
                )
                latin_urls = parse_manifest_urls(
                    user_input.get(CONF_LATIN_MANIFEST_URLS, "")
                )
                sports_urls = parse_manifest_urls(
                    user_input.get(CONF_SPORTS_MANIFEST_URLS, "")
                )
                if not catalog_urls or not stream_urls:
                    raise StremioBridgeError(
                        "At least one catalog and stream manifest is required"
                    )
                await _validate(
                    self.hass,
                    server_url,
                    catalog_urls,
                    stream_urls,
                    subtitle_urls,
                    latin_urls,
                    sports_urls,
                )
            except StremioConnectionError as err:
                _LOGGER.error("Could not connect while configuring Stremio bridge: %s", err)
                errors["base"] = "cannot_connect"
            except StremioBridgeError as err:
                _LOGGER.error("Invalid Stremio bridge configuration: %s", err)
                errors["base"] = "invalid_config"
            else:
                unique_id = hashlib.sha256(server_url.encode()).hexdigest()
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                data = {
                    CONF_STREAMING_SERVER_URL: server_url,
                    CONF_CATALOG_MANIFEST_URLS: catalog_urls,
                    CONF_STREAM_MANIFEST_URLS: stream_urls,
                    CONF_SUBTITLE_MANIFEST_URLS: subtitle_urls,
                    CONF_LATIN_MANIFEST_URLS: latin_urls,
                    CONF_SPORTS_MANIFEST_URLS: sports_urls,
                    CONF_DEFAULT_MEDIA_PLAYER: user_input[CONF_DEFAULT_MEDIA_PLAYER],
                }
                return self.async_create_entry(title="Stremio Media", data=data)

        return self.async_show_form(
            step_id="user",
            data_schema=_connection_schema(user_input),
            errors=errors,
            description_placeholders=_description_placeholders(),
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return StremioStreamBridgeOptionsFlow()


class StremioStreamBridgeOptionsFlow(config_entries.OptionsFlowWithReload):
    """Change providers and playback preferences, then reload automatically."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        current = {**self.config_entry.data, **self.config_entry.options}
        displayed = {**current, **(user_input or {})}
        server_default = str(
            displayed.get(CONF_STREAMING_SERVER_URL, DEFAULT_STREAMING_SERVER_URL)
        )
        recommended_subtitle_url = await _recommended_subtitle_base_url(
            self.hass, server_default
        )

        if user_input is not None:
            try:
                server_url = normalize_url(user_input[CONF_STREAMING_SERVER_URL])
                catalog_urls = parse_manifest_urls(user_input[CONF_CATALOG_MANIFEST_URLS])
                stream_urls = parse_manifest_urls(user_input[CONF_STREAM_MANIFEST_URLS])
                subtitle_urls = parse_manifest_urls(
                    user_input.get(CONF_SUBTITLE_MANIFEST_URLS, "")
                )
                latin_urls = parse_manifest_urls(
                    user_input.get(CONF_LATIN_MANIFEST_URLS, "")
                )
                sports_urls = parse_manifest_urls(
                    user_input.get(CONF_SPORTS_MANIFEST_URLS, "")
                )
                if not catalog_urls or not stream_urls:
                    raise StremioBridgeError("Manifest lists cannot be empty")
                await _validate(
                    self.hass,
                    server_url,
                    catalog_urls,
                    stream_urls,
                    subtitle_urls,
                    latin_urls,
                    sports_urls,
                )
            except StremioConnectionError as err:
                _LOGGER.error("Could not connect while saving Stremio options: %s", err)
                errors["base"] = "cannot_connect"
            except StremioBridgeError as err:
                _LOGGER.error("Invalid Stremio options: %s", err)
                errors["base"] = "invalid_config"
            else:
                options = {
                    **user_input,
                    CONF_STREAMING_SERVER_URL: server_url,
                    CONF_CATALOG_MANIFEST_URLS: catalog_urls,
                    CONF_STREAM_MANIFEST_URLS: stream_urls,
                    CONF_SUBTITLE_MANIFEST_URLS: subtitle_urls,
                    CONF_LATIN_MANIFEST_URLS: latin_urls,
                    CONF_SPORTS_MANIFEST_URLS: sports_urls,
                }
                return self.async_create_entry(data=options)

        subtitle_base_default = str(
            displayed.get(CONF_SUBTITLE_BASE_URL, "")
            or recommended_subtitle_url
            or DEFAULT_SUBTITLE_BASE_URL
        )
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_STREAMING_SERVER_URL,
                    default=server_default,
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
                vol.Required(
                    CONF_DEFAULT_MEDIA_PLAYER,
                    default=displayed.get(CONF_DEFAULT_MEDIA_PLAYER),
                ): EntitySelector(EntitySelectorConfig(domain="media_player")),
                vol.Required(
                    CONF_CATALOG_MANIFEST_URLS,
                    default=_as_lines(
                        displayed.get(CONF_CATALOG_MANIFEST_URLS),
                        DEFAULT_CINEMETA_MANIFEST,
                    ),
                ): TextSelector(TextSelectorConfig(multiline=True)),
                vol.Required(
                    CONF_STREAM_MANIFEST_URLS,
                    default=_as_lines(
                        displayed.get(CONF_STREAM_MANIFEST_URLS),
                        DEFAULT_TORRENTIO_MANIFEST,
                    ),
                ): TextSelector(TextSelectorConfig(multiline=True)),
                vol.Optional(
                    CONF_LATIN_MANIFEST_URLS,
                    default=_as_lines(
                        displayed.get(CONF_LATIN_MANIFEST_URLS), DEFAULT_LATIN_MANIFEST
                    ),
                ): TextSelector(TextSelectorConfig(multiline=True)),
                vol.Optional(
                    CONF_SPORTS_MANIFEST_URLS,
                    default=_as_lines(
                        displayed.get(CONF_SPORTS_MANIFEST_URLS),
                        DEFAULT_SPORTS_MANIFEST,
                    ),
                ): TextSelector(TextSelectorConfig(multiline=True)),
                vol.Optional(
                    CONF_SUBTITLE_MANIFEST_URLS,
                    default=_as_lines(
                        displayed.get(CONF_SUBTITLE_MANIFEST_URLS),
                        DEFAULT_OPENSUBTITLES_MANIFEST,
                    ),
                ): TextSelector(TextSelectorConfig(multiline=True)),
                vol.Required(
                    CONF_PLAY_IDEAL_ON_SELECT,
                    default=displayed.get(
                        CONF_PLAY_IDEAL_ON_SELECT, DEFAULT_PLAY_IDEAL_ON_SELECT
                    ),
                ): BooleanSelector(),
                vol.Required(
                    CONF_IDEAL_LINK_FILTER,
                    default=displayed.get(
                        CONF_IDEAL_LINK_FILTER, DEFAULT_IDEAL_LINK_FILTER
                    ),
                ): BooleanSelector(),
                vol.Required(
                    CONF_CAST_COMPATIBILITY_FILTER,
                    default=displayed.get(
                        CONF_CAST_COMPATIBILITY_FILTER,
                        DEFAULT_CAST_COMPATIBILITY_FILTER,
                    ),
                ): BooleanSelector(),
                vol.Required(
                    CONF_STOP_BEFORE_PLAY,
                    default=displayed.get(
                        CONF_STOP_BEFORE_PLAY, DEFAULT_STOP_BEFORE_PLAY
                    ),
                ): BooleanSelector(),
                vol.Required(
                    CONF_CAST_RESET_BEFORE_PLAY,
                    default=displayed.get(
                        CONF_CAST_RESET_BEFORE_PLAY,
                        DEFAULT_CAST_RESET_BEFORE_PLAY,
                    ),
                ): BooleanSelector(),
                vol.Required(
                    CONF_FALLBACK_ENABLED,
                    default=displayed.get(
                        CONF_FALLBACK_ENABLED, DEFAULT_FALLBACK_ENABLED
                    ),
                ): BooleanSelector(),
                vol.Required(
                    CONF_FALLBACK_SOURCE_COUNT,
                    default=displayed.get(
                        CONF_FALLBACK_SOURCE_COUNT, DEFAULT_FALLBACK_SOURCE_COUNT
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=1,
                        max=10,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_PLAYBACK_START_TIMEOUT,
                    default=displayed.get(
                        CONF_PLAYBACK_START_TIMEOUT,
                        DEFAULT_PLAYBACK_START_TIMEOUT,
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=5,
                        max=60,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_FAILURE_NOTIFY_HA,
                    default=displayed.get(
                        CONF_FAILURE_NOTIFY_HA, DEFAULT_FAILURE_NOTIFY_HA
                    ),
                ): BooleanSelector(),
                vol.Required(
                    CONF_TVOVERLAY_ENABLED,
                    default=displayed.get(
                        CONF_TVOVERLAY_ENABLED, DEFAULT_TVOVERLAY_ENABLED
                    ),
                ): BooleanSelector(),
                vol.Optional(
                    CONF_TVOVERLAY_SERVICE,
                    default=displayed.get(
                        CONF_TVOVERLAY_SERVICE, DEFAULT_TVOVERLAY_SERVICE
                    ),
                ): TextSelector(TextSelectorConfig(multiline=False)),
                vol.Optional(
                    CONF_TVOVERLAY_TARGET,
                    default=displayed.get(
                        CONF_TVOVERLAY_TARGET, DEFAULT_TVOVERLAY_TARGET
                    ),
                ): TextSelector(TextSelectorConfig(multiline=False)),
                vol.Required(
                    CONF_TVOVERLAY_DURATION,
                    default=displayed.get(
                        CONF_TVOVERLAY_DURATION, DEFAULT_TVOVERLAY_DURATION
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=1,
                        max=60,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_AUDIO_MODE,
                    default=displayed.get(CONF_AUDIO_MODE, DEFAULT_AUDIO_MODE),
                ): SelectSelector(SelectSelectorConfig(options=AUDIO_MODE_OPTIONS)),
                vol.Required(
                    CONF_PREFERRED_QUALITY,
                    default=displayed.get(
                        CONF_PREFERRED_QUALITY, DEFAULT_PREFERRED_QUALITY
                    ),
                ): SelectSelector(SelectSelectorConfig(options=QUALITY_OPTIONS)),
                vol.Required(
                    CONF_MAX_SIZE_GB,
                    default=displayed.get(CONF_MAX_SIZE_GB, DEFAULT_MAX_SIZE_GB),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=200,
                        step=0.5,
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_EXCLUDE_KEYWORDS,
                    default=displayed.get(
                        CONF_EXCLUDE_KEYWORDS, DEFAULT_EXCLUDE_KEYWORDS
                    ),
                ): TextSelector(TextSelectorConfig(multiline=False)),
                vol.Required(
                    CONF_SUBTITLE_MODE,
                    default=displayed.get(CONF_SUBTITLE_MODE, DEFAULT_SUBTITLE_MODE),
                ): SelectSelector(SelectSelectorConfig(options=SUBTITLE_MODE_OPTIONS)),
                vol.Required(
                    CONF_SUBTITLE_LANGUAGES,
                    default=displayed.get(
                        CONF_SUBTITLE_LANGUAGES, DEFAULT_SUBTITLE_LANGUAGES
                    ),
                ): TextSelector(TextSelectorConfig(multiline=False)),
                vol.Required(
                    CONF_SUBTITLE_CONVERT_VTT,
                    default=displayed.get(
                        CONF_SUBTITLE_CONVERT_VTT, DEFAULT_SUBTITLE_CONVERT_VTT
                    ),
                ): BooleanSelector(),
                vol.Optional(
                    CONF_SUBTITLE_BASE_URL,
                    default=subtitle_base_default,
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            description_placeholders=_description_placeholders(
                recommended_subtitle_url
            ),
        )

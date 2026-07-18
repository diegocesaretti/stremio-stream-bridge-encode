"""Playback status and failure notifications for Home Assistant and TvOverlay."""

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

from homeassistant.core import HomeAssistant

from .const import (
    CONF_FAILURE_NOTIFY_HA,
    CONF_TVOVERLAY_DURATION,
    CONF_TVOVERLAY_ENABLED,
    CONF_TVOVERLAY_SERVICE,
    CONF_TVOVERLAY_TARGET,
    DEFAULT_FAILURE_NOTIFY_HA,
    DEFAULT_TVOVERLAY_DURATION,
    DEFAULT_TVOVERLAY_ENABLED,
    DEFAULT_TVOVERLAY_SERVICE,
    DEFAULT_TVOVERLAY_TARGET,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def _service_target(value: str, domain: str) -> dict[str, str] | None:
    """Interpret an optional TvOverlay target for the selected service."""
    target = value.strip()
    if not target:
        return None
    for prefix, key in (
        ("device:", "device_id"),
        ("entity:", "entity_id"),
        ("area:", "area_id"),
        ("target:", "target"),
        ("host:", "host"),
    ):
        if target.lower().startswith(prefix):
            return {key: target[len(prefix) :].strip()}
    if domain == "tvoverlay_ui":
        return {"target": target}
    return {"target": target}


def failure_message(title: str, attempts: int, reasons: Sequence[str]) -> str:
    """Build a compact failure message for Home Assistant and TvOverlay."""
    text = f"No se pudo reproducir «{title}». Se probaron {attempts} fuente(s)."
    useful = [reason.strip() for reason in reasons if reason and reason.strip()]
    if useful:
        text += " Último error: " + useful[-1][:240]
    return text


async def _async_send_tvoverlay(
    hass: HomeAssistant,
    options: Mapping[str, Any],
    *,
    message: str,
    poster: str | None,
    context: Any | None = None,
) -> None:
    """Send one best-effort TvOverlay notification using the configured service."""
    if not bool(options.get(CONF_TVOVERLAY_ENABLED, DEFAULT_TVOVERLAY_ENABLED)):
        return

    service_name = str(
        options.get(CONF_TVOVERLAY_SERVICE) or DEFAULT_TVOVERLAY_SERVICE
    ).strip()
    if "." not in service_name:
        _LOGGER.warning(
            "TvOverlay notification is enabled but service '%s' is invalid",
            service_name,
        )
        return

    domain, service = service_name.split(".", 1)
    has_service = getattr(hass.services, "has_service", None)
    if (
        domain == "tvoverlay_ui"
        and service == "notify"
        and callable(has_service)
        and not has_service(domain, service)
        and has_service("notify", "tvoverlaynotify")
    ):
        # v0.5.0 could persist the older TvOverlay UI service as an option.
        # Prefer the installed notify platform at runtime without migrating or
        # rewriting the user's config entry.
        domain, service = DEFAULT_TVOVERLAY_SERVICE.split(".", 1)

    duration = int(
        options.get(CONF_TVOVERLAY_DURATION, DEFAULT_TVOVERLAY_DURATION)
        or DEFAULT_TVOVERLAY_DURATION
    )
    target = _service_target(
        str(options.get(CONF_TVOVERLAY_TARGET, DEFAULT_TVOVERLAY_TARGET) or ""),
        domain,
    )

    if domain == "notify":
        data: dict[str, Any] = {
            "title": "Stremio Stream Bridge",
            "message": message,
            "data": {
                "seconds": duration,
                "corner": "top_end",
            },
        }
        if poster:
            data["data"].update({"image": poster, "largeIcon": poster})
    elif domain == "tvoverlay_ui":
        data = {
            "title": "Stremio Stream Bridge",
            "message": message,
            "source": "Home Assistant",
            "corner": "top_end",
            "duration": duration,
        }
        if poster:
            data.update(
                {
                    "large_icon": poster,
                    "media_type": "image",
                    "media_url": poster,
                }
            )
    else:
        data = {
            "title": "Stremio Stream Bridge",
            "message": message,
            "duration": duration,
        }
        if poster:
            data["image"] = poster

    if target:
        data.update(target)
    try:
        await hass.services.async_call(
            domain,
            service,
            data,
            blocking=False,
            context=context,
        )
    except Exception as err:  # noqa: BLE001 - overlay errors are non-fatal.
        _LOGGER.warning("Could not send TvOverlay notification: %s", err)


async def async_notify_source_search(
    hass: HomeAssistant,
    options: Mapping[str, Any],
    *,
    title: str,
    poster: str | None,
    attempt: int,
    total: int,
    context: Any | None = None,
) -> None:
    """Notify TvOverlay that a ranked source is being prepared."""
    await _async_send_tvoverlay(
        hass,
        options,
        message=f"Buscando una fuente para «{title}»… ({attempt}/{total})",
        poster=poster,
        context=context,
    )


async def async_notify_playback_started(
    hass: HomeAssistant,
    options: Mapping[str, Any],
    *,
    title: str,
    poster: str | None,
    context: Any | None = None,
) -> None:
    """Notify TvOverlay only after the selected player reports playback."""
    await _async_send_tvoverlay(
        hass,
        options,
        message=f"Estás viendo «{title}».",
        poster=poster,
        context=context,
    )


async def async_notify_playback_failure(
    hass: HomeAssistant,
    options: Mapping[str, Any],
    *,
    title: str,
    poster: str | None,
    attempts: int,
    reasons: Sequence[str],
    context: Any | None = None,
) -> None:
    """Send a persistent HA notification and an optional TvOverlay message."""
    message = failure_message(title, attempts, reasons)

    if bool(options.get(CONF_FAILURE_NOTIFY_HA, DEFAULT_FAILURE_NOTIFY_HA)):
        persistent_message = message
        if poster:
            persistent_message += f"\n\n![Portada]({poster})"
        try:
            await hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "Stremio Stream Bridge",
                    "message": persistent_message,
                    "notification_id": f"{DOMAIN}_playback_failure",
                },
                blocking=False,
                context=context,
            )
        except Exception as err:  # noqa: BLE001 - notification cannot break playback.
            _LOGGER.warning("Could not create Home Assistant failure notification: %s", err)

    await _async_send_tvoverlay(
        hass,
        options,
        message=message,
        poster=poster,
        context=context,
    )

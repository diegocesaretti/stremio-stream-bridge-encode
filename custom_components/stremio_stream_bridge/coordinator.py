"""Coordinator for Stremio Stream Bridge."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .aggregator import StremioAddonManager
from .api import StremioBridgeError, StremioStreamServerClient
from .const import DEFAULT_SCAN_INTERVAL_SECONDS, DOMAIN


class StremioBridgeCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Poll the stream server and all configured add-on manifests."""

    def __init__(
        self,
        hass: HomeAssistant,
        manager: StremioAddonManager,
        server: StremioStreamServerClient,
    ) -> None:
        super().__init__(
            hass,
            logger=logging.getLogger(__name__),
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL_SECONDS),
        )
        self.manager = manager
        self.server = server

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            settings = await self.server.get_settings()
            addons = await self.manager.async_refresh()
        except StremioBridgeError as err:
            raise UpdateFailed(str(err)) from err
        return {
            "settings": settings,
            "addons": [
                {
                    "id": addon.id,
                    "name": addon.name,
                    "version": addon.manifest.get("version"),
                    "manifest_url": addon.client.manifest_url,
                    "roles": sorted(addon.roles),
                }
                for addon in addons
            ],
            "addon_errors": dict(self.manager.errors),
        }

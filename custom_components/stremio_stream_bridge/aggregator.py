"""Aggregate Stremio add-ons by the resources declared in their manifests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Any

from .api import StremioAddonClient, StremioBridgeError, StremioProtocolError
from .const import PROFILE_DEFAULT, PROFILE_LATIN, PROFILE_SPORTS

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class LoadedAddon:
    """A validated add-on client and its current manifest."""

    client: StremioAddonClient
    manifest: dict[str, Any]
    roles: frozenset[str]

    @property
    def id(self) -> str:
        return str(self.manifest.get("id") or self.client.manifest_url)

    @property
    def name(self) -> str:
        return str(self.manifest.get("name") or self.id)


class StremioAddonManager:
    """Route catalog, metadata, stream and subtitle requests across add-ons."""

    def __init__(
        self,
        catalog_clients: list[StremioAddonClient],
        stream_clients: list[StremioAddonClient],
        subtitle_clients: list[StremioAddonClient] | None = None,
        latin_clients: list[StremioAddonClient] | None = None,
        sports_clients: list[StremioAddonClient] | None = None,
    ) -> None:
        roles_by_url: dict[str, set[str]] = {}
        clients_by_url: dict[str, StremioAddonClient] = {}
        for role, clients in (
            ("catalog", catalog_clients),
            ("stream", stream_clients),
            ("subtitle", subtitle_clients or []),
            (PROFILE_LATIN, latin_clients or []),
            (PROFILE_SPORTS, sports_clients or []),
        ):
            for client in clients:
                clients_by_url[client.manifest_url] = client
                roles_by_url.setdefault(client.manifest_url, set()).add(role)
        self._clients = clients_by_url
        self._roles = roles_by_url
        self.addons: list[LoadedAddon] = []
        self.errors: dict[str, str] = {}
        self.last_subtitle_errors: dict[str, str] = {}

    async def async_refresh(self) -> list[LoadedAddon]:
        """Refresh every manifest, retaining all successfully loaded add-ons."""
        clients = list(self._clients.values())
        results = await asyncio.gather(
            *(client.get_manifest() for client in clients), return_exceptions=True
        )
        addons: list[LoadedAddon] = []
        errors: dict[str, str] = {}
        for client, result in zip(clients, results, strict=True):
            if isinstance(result, BaseException):
                errors[client.manifest_url] = str(result)
                continue
            addons.append(
                LoadedAddon(
                    client=client,
                    manifest=result,
                    roles=frozenset(self._roles.get(client.manifest_url, set())),
                )
            )
        if not addons:
            detail = "; ".join(errors.values()) or "No add-on manifests were loaded"
            raise StremioProtocolError(detail)
        self.addons = addons
        self.errors = errors
        return addons

    def has_profile(self, profile: str) -> bool:
        """Return whether at least one loaded add-on belongs to a special profile."""
        if profile == PROFILE_DEFAULT:
            return True
        return any(profile in addon.roles for addon in self.addons)

    def catalogs(
        self,
        media_type: str | None = None,
        profile: str = PROFILE_DEFAULT,
    ) -> list[tuple[LoadedAddon, dict[str, Any]]]:
        """Return catalog declarations for one playback profile."""
        role = "catalog" if profile == PROFILE_DEFAULT else profile
        result = self._catalogs_for_role(role, media_type)
        # Most language-specific stream add-ons do not expose their own catalog.
        # Mirror the regular Cinemeta catalog while retaining the Latin stream profile.
        if profile == PROFILE_LATIN and not result:
            result = self._catalogs_for_role("catalog", media_type)
        return result

    def _catalogs_for_role(
        self, role: str, media_type: str | None
    ) -> list[tuple[LoadedAddon, dict[str, Any]]]:
        result: list[tuple[LoadedAddon, dict[str, Any]]] = []
        seen: set[tuple[str, str, str]] = set()
        for addon in self.addons:
            if role not in addon.roles:
                continue
            catalogs = addon.manifest.get("catalogs", [])
            if not isinstance(catalogs, list):
                continue
            for catalog in catalogs:
                if not isinstance(catalog, dict):
                    continue
                catalog_type = catalog.get("type")
                catalog_id = catalog.get("id")
                if not isinstance(catalog_type, str) or not isinstance(catalog_id, str):
                    continue
                if media_type and catalog_type != media_type:
                    continue
                key = (addon.client.manifest_url, catalog_type, catalog_id)
                if key in seen:
                    continue
                seen.add(key)
                result.append((addon, catalog))
        return result

    def get_addon(self, manifest_url: str) -> LoadedAddon:
        """Return a loaded add-on by manifest URL."""
        for addon in self.addons:
            if addon.client.manifest_url == manifest_url:
                return addon
        raise StremioProtocolError(f"Add-on is not loaded: {manifest_url}")

    async def get_catalog(
        self,
        manifest_url: str,
        media_type: str,
        catalog_id: str,
        extra: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch one catalog from its declaring add-on."""
        addon = self.get_addon(manifest_url)
        return await addon.client.get_catalog(media_type, catalog_id, extra)

    async def get_meta(
        self,
        media_type: str,
        media_id: str,
        profile: str = PROFILE_DEFAULT,
    ) -> dict[str, Any]:
        """Return metadata from the first compatible provider that succeeds."""
        preferred_role = "catalog" if profile == PROFILE_DEFAULT else profile
        providers = [addon for addon in self.addons if preferred_role in addon.roles]
        if profile != PROFILE_DEFAULT:
            providers.extend(
                addon
                for addon in self.addons
                if "catalog" in addon.roles and addon not in providers
            )
        errors: list[str] = []
        for addon in providers:
            if not supports_resource(addon.manifest, "meta", media_type, media_id):
                continue
            try:
                return await addon.client.get_meta(media_type, media_id)
            except StremioBridgeError as err:
                errors.append(f"{addon.name}: {err}")
        detail = "; ".join(errors) or "No compatible metadata provider"
        raise StremioProtocolError(detail)

    async def get_streams(
        self,
        media_type: str,
        media_id: str,
        profile: str = PROFILE_DEFAULT,
    ) -> list[dict[str, Any]]:
        """Fetch and merge streams from compatible providers in one profile."""
        role = "stream" if profile == PROFILE_DEFAULT else profile
        providers = [
            addon
            for addon in self.addons
            if role in addon.roles
            and supports_resource(addon.manifest, "stream", media_type, media_id)
        ]
        if not providers:
            raise StremioProtocolError(f"No compatible {profile} stream provider")
        results = await asyncio.gather(
            *(addon.client.get_streams(media_type, media_id) for addon in providers),
            return_exceptions=True,
        )
        merged: list[dict[str, Any]] = []
        errors: list[str] = []
        seen: set[str] = set()
        for addon, result in zip(providers, results, strict=True):
            if isinstance(result, BaseException):
                errors.append(f"{addon.name}: {result}")
                continue
            for provider_index, stream in enumerate(result):
                enriched = dict(stream)
                enriched["_bridge_addon_name"] = addon.name
                enriched["_bridge_addon_url"] = addon.client.manifest_url
                enriched["_bridge_provider_index"] = provider_index
                enriched["_bridge_profile"] = profile
                key = stream_key(enriched)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(enriched)
        if not merged and errors:
            raise StremioProtocolError("; ".join(errors))
        return merged

    async def get_subtitles(
        self,
        media_type: str,
        media_id: str,
        extra: dict[str, str] | None = None,
        stream: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch and merge subtitle tracks from streams and subtitle providers."""
        merged: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        if stream is not None:
            embedded = stream.get("subtitles", [])
            if isinstance(embedded, list):
                for subtitle in embedded:
                    if isinstance(subtitle, dict):
                        _append_subtitle(merged, seen, subtitle, "Stream")

        providers = [
            addon
            for addon in self.addons
            if "subtitle" in addon.roles
            and supports_resource(addon.manifest, "subtitles", media_type, media_id)
        ]
        if not providers:
            self.last_subtitle_errors = {}
            return merged

        results = await asyncio.gather(
            *(addon.client.get_subtitles(media_type, media_id, extra) for addon in providers),
            return_exceptions=True,
        )
        errors: dict[str, str] = {}
        for addon, result in zip(providers, results, strict=True):
            if isinstance(result, BaseException):
                errors[addon.name] = str(result)
                _LOGGER.warning(
                    "Subtitle provider %s failed for %s/%s: %s",
                    addon.name,
                    media_type,
                    media_id,
                    result,
                )
                continue
            for subtitle in result:
                _append_subtitle(merged, seen, subtitle, addon.name)
        self.last_subtitle_errors = errors
        return merged

    async def search(
        self, query: str, media_types: tuple[str, ...] = ("movie", "series")
    ) -> list[dict[str, Any]]:
        """Search default catalogs that advertise the Stremio `search` extra."""
        requests: list[tuple[LoadedAddon, dict[str, Any]]] = []
        for media_type in media_types:
            for addon, catalog in self.catalogs(media_type, PROFILE_DEFAULT):
                if catalog_supports_extra(catalog, "search"):
                    requests.append((addon, catalog))
                    break
        results = await asyncio.gather(
            *(
                addon.client.get_catalog(
                    str(catalog["type"]), str(catalog["id"]), {"search": query}
                )
                for addon, catalog in requests
            ),
            return_exceptions=True,
        )
        metas: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for (addon, catalog), result in zip(requests, results, strict=True):
            if isinstance(result, BaseException):
                continue
            media_type = str(catalog["type"])
            for meta in result:
                media_id = meta.get("id")
                if not isinstance(media_id, str):
                    continue
                key = (media_type, media_id)
                if key in seen:
                    continue
                seen.add(key)
                enriched = dict(meta)
                enriched["_bridge_media_type"] = media_type
                enriched["_bridge_catalog_addon"] = addon.name
                metas.append(enriched)
        return metas


def _append_subtitle(
    result: list[dict[str, Any]],
    seen: set[tuple[str, str]],
    subtitle: dict[str, Any],
    provider: str,
) -> None:
    url = subtitle.get("url")
    lang = subtitle.get("lang")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return
    if not isinstance(lang, str) or not lang:
        lang = "und"
    key = (url, lang.lower())
    if key in seen:
        return
    seen.add(key)
    enriched = dict(subtitle)
    enriched["lang"] = lang.lower()
    enriched["_bridge_addon_name"] = provider
    result.append(enriched)


def manifest_has_resource(manifest: dict[str, Any], resource_name: str) -> bool:
    """Return whether a manifest declares a resource name."""
    resources = manifest.get("resources", [])
    if not isinstance(resources, list):
        return False
    return any(
        resource == resource_name
        or (isinstance(resource, dict) and resource.get("name") == resource_name)
        for resource in resources
    )


def supports_resource(
    manifest: dict[str, Any], resource_name: str, media_type: str, media_id: str
) -> bool:
    """Return whether a manifest advertises a resource for this content."""
    resources = manifest.get("resources", [])
    if not isinstance(resources, list):
        return False
    matched: dict[str, Any] | None = None
    for resource in resources:
        if resource == resource_name:
            matched = {}
            break
        if isinstance(resource, dict) and resource.get("name") == resource_name:
            matched = resource
            break
    if matched is None:
        return False
    types = matched.get("types") or manifest.get("types")
    if isinstance(types, list) and types and media_type not in types:
        return False
    prefixes = matched.get("idPrefixes") or manifest.get("idPrefixes")
    if isinstance(prefixes, list) and prefixes:
        return any(isinstance(prefix, str) and media_id.startswith(prefix) for prefix in prefixes)
    return True


def catalog_extra(catalog: dict[str, Any], name: str) -> dict[str, Any] | None:
    """Return a normalized catalog extra declaration."""
    extras = catalog.get("extra", [])
    if isinstance(extras, list):
        for extra in extras:
            if extra == name:
                return {"name": name}
            if isinstance(extra, dict) and extra.get("name") == name:
                return extra
    supported = catalog.get("extraSupported", [])
    if isinstance(supported, list) and name in supported:
        return {"name": name}
    return None


def catalog_supports_extra(catalog: dict[str, Any], name: str) -> bool:
    return catalog_extra(catalog, name) is not None


def catalog_required_extras(catalog: dict[str, Any]) -> list[str]:
    """Return required extra names from both current manifest formats."""
    result: list[str] = []
    raw_required = catalog.get("extraRequired", [])
    if isinstance(raw_required, list):
        result.extend(str(value) for value in raw_required if isinstance(value, str))
    extras = catalog.get("extra", [])
    if isinstance(extras, list):
        for extra in extras:
            if isinstance(extra, dict) and extra.get("isRequired") and isinstance(
                extra.get("name"), str
            ):
                result.append(extra["name"])
    return list(dict.fromkeys(result))


def stream_key(stream: dict[str, Any]) -> str:
    """Build a stable-enough key for a stream across repeated provider queries."""
    info_hash = stream.get("infoHash")
    if isinstance(info_hash, str) and info_hash:
        return f"bt:{info_hash.lower()}:{stream.get('fileIdx', -1)}"
    for field in ("url", "externalUrl", "ytId"):
        value = stream.get(field)
        if isinstance(value, str) and value:
            return f"{field}:{value}"
    return "text:" + "|".join(
        str(stream.get(field) or "") for field in ("name", "title", "description")
    )

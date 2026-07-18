"""Browse aggregate Stremio catalogs through Home Assistant Media Sources."""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from homeassistant.components.media_player import BrowseError, MediaClass, MediaType
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant

try:  # Media source search lands after Home Assistant 2026.7.2.
    from homeassistant.components.media_player import SearchMedia
except ImportError:  # pragma: no cover - depends on installed Home Assistant.
    SearchMedia = None  # type: ignore[assignment,misc]

from . import StremioBridgeRuntime, find_stream_by_key
from .aggregator import (
    catalog_extra,
    catalog_required_extras,
    catalog_supports_extra,
    stream_key,
)
from .api import StremioBridgeError, StremioProtocolError
from .const import (
    CONF_CAST_COMPATIBILITY_FILTER,
    CONF_EXCLUDE_KEYWORDS,
    CONF_FALLBACK_ENABLED,
    CONF_FALLBACK_SOURCE_COUNT,
    CONF_IDEAL_LINK_FILTER,
    CONF_MAX_SIZE_GB,
    CONF_PLAY_IDEAL_ON_SELECT,
    CONF_PREFERRED_QUALITY,
    DEFAULT_CAST_COMPATIBILITY_FILTER,
    DEFAULT_EXCLUDE_KEYWORDS,
    DEFAULT_FALLBACK_ENABLED,
    DEFAULT_FALLBACK_SOURCE_COUNT,
    DEFAULT_IDEAL_LINK_FILTER,
    DEFAULT_MAX_SIZE_GB,
    DEFAULT_PLAY_IDEAL_ON_SELECT,
    DEFAULT_PREFERRED_QUALITY,
    DOMAIN,
    NAME,
    PROFILE_DEFAULT,
    PROFILE_LATIN,
    PROFILE_SPORTS,
)
from .failure_notifications import async_notify_playback_failure
from .playback_supervisor import (
    async_monitor_initial_and_fallback,
    async_prepare_candidate,
    limit_ranked_candidates,
)
from .session_control import async_prepare_player_session
from .stream_selector import choose_best_stream, order_ideal_streams, stream_label
from .subtitle_support import (
    async_prepare_subtitle_track,
    cast_media_source_payload,
    cast_service_extra,
    is_cast_player,
)

_LOGGER = logging.getLogger(__name__)

MEDIA_SOURCE_SEARCH_SUPPORTED = (
    SearchMedia is not None and hasattr(MediaSource, "async_search_media")
)


async def async_get_media_source(hass: HomeAssistant) -> "StremioBridgeMediaSource":
    """Set up the media source platform."""
    return StremioBridgeMediaSource(hass)


class StremioBridgeMediaSource(MediaSource):
    """Expose aggregate Stremio catalogs and stream choices."""

    name = NAME

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(DOMAIN)
        self.hass = hass

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        try:
            if not item.identifier:
                return self._root()
            payload = _decode(item.identifier)
            kind = payload.get("kind")
            if kind == "entry":
                return self._browse_entry(payload)
            if kind == "profile":
                return self._browse_profile(payload)
            if kind == "type":
                return self._browse_type(payload)
            if kind == "catalog":
                return await self._browse_catalog(payload)
            if kind == "filter":
                return self._browse_filter(payload)
            if kind == "meta":
                return await self._browse_meta(payload)
            if kind == "season":
                return await self._browse_season(payload)
            if kind == "stream_choices":
                return await self._browse_stream_choices(payload)
            if kind == "search_results":
                return self._browse_search_results(payload)
            raise BrowseError("Unknown Stremio media source item")
        except StremioBridgeError as err:
            raise BrowseError(str(err)) from err
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as err:
            raise BrowseError("Invalid Stremio media source identifier") from err

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        try:
            payload = _decode(item.identifier)
            if payload.get("kind") != "video":
                raise Unresolvable("Selected item is not playable")
            entry = self._entry(payload["entry_id"])
            runtime: StremioBridgeRuntime = entry.runtime_data
            profile = str(payload.get("profile") or PROFILE_DEFAULT)
            streams = await runtime.manager.get_streams(
                payload["type"], payload["id"], profile
            )
            if not streams:
                raise Unresolvable("No stream provider returned a source")
            current = {**entry.data, **entry.options}
            player = item.target_media_player
            cast_target = is_cast_player(self.hass, player)

            if payload.get("selection") == "auto":
                if profile == PROFILE_SPORTS:
                    candidates = list(streams)
                else:
                    max_size = float(current.get(CONF_MAX_SIZE_GB, DEFAULT_MAX_SIZE_GB))
                    excluded = str(
                        current.get(CONF_EXCLUDE_KEYWORDS, DEFAULT_EXCLUDE_KEYWORDS)
                    )
                    if bool(
                        current.get(CONF_IDEAL_LINK_FILTER, DEFAULT_IDEAL_LINK_FILTER)
                    ):
                        candidates = order_ideal_streams(
                            streams,
                            max_size,
                            excluded,
                            preferred_quality=str(
                                current.get(
                                    CONF_PREFERRED_QUALITY, DEFAULT_PREFERRED_QUALITY
                                )
                            ),
                            prefer_direct_play=cast_target,
                            strict_compatibility=cast_target
                            and bool(
                                current.get(
                                    CONF_CAST_COMPATIBILITY_FILTER,
                                    DEFAULT_CAST_COMPATIBILITY_FILTER,
                                )
                            ),
                        )
                    else:
                        selected = choose_best_stream(
                            streams,
                            str(
                                current.get(
                                    CONF_PREFERRED_QUALITY, DEFAULT_PREFERRED_QUALITY
                                )
                            ),
                            max_size,
                            excluded,
                        )
                        remaining = order_ideal_streams(
                            [stream for stream in streams if stream is not selected],
                            max_size,
                            excluded,
                            preferred_quality=str(
                                current.get(
                                    CONF_PREFERRED_QUALITY, DEFAULT_PREFERRED_QUALITY
                                )
                            ),
                            prefer_direct_play=cast_target,
                            strict_compatibility=cast_target
                            and bool(
                                current.get(
                                    CONF_CAST_COMPATIBILITY_FILTER,
                                    DEFAULT_CAST_COMPATIBILITY_FILTER,
                                )
                            ),
                        )
                        candidates = [selected, *remaining]
            else:
                key = str(payload.get("stream_key") or "")
                selected = find_stream_by_key(streams, key)
                if selected is None:
                    raise Unresolvable(
                        "That stream is no longer available; reopen the list"
                    )
                candidates = [selected]

            ranked = limit_ranked_candidates(candidates, current)
            if not ranked:
                raise Unresolvable("No ranked stream candidates are available")

            if player:
                old_task = runtime.playback_tasks.pop(player, None)
                if old_task is not None and not old_task.done():
                    old_task.cancel()
                await async_prepare_player_session(
                    self.hass,
                    player,
                    current,
                    cast_target=cast_target,
                )

            prepared = None
            prepared_index = 0
            preparation_errors: list[str] = []
            for index, candidate in enumerate(ranked):
                try:
                    prepared = await async_prepare_candidate(
                        runtime.server,
                        candidate,
                        current,
                        profile=profile,
                        cast_target=cast_target,
                    )
                except StremioBridgeError as err:
                    preparation_errors.append(
                        f"{stream_label(candidate)}: validation failed: {err}"
                    )
                    continue
                prepared_index = index
                break

            title = str(payload.get("name") or payload["id"])
            poster = payload.get("poster")
            if prepared is None:
                if player:
                    await async_notify_playback_failure(
                        self.hass,
                        current,
                        title=title,
                        poster=poster,
                        attempts=len(ranked),
                        reasons=preparation_errors,
                    )
                detail = "; ".join(preparation_errors[-3:])
                raise Unresolvable(
                    "All ranked stream URLs failed validation"
                    + (f": {detail}" if detail else "")
                )

            active_candidates = ranked[prepared_index:]
            subtitles_disabled = (
                payload.get("subtitles") == "off"
                or profile in {PROFILE_LATIN, PROFILE_SPORTS}
            )

            async def extra_factory(stream: dict[str, Any]) -> dict[str, Any] | None:
                if not cast_target:
                    return None
                subtitle = await async_prepare_subtitle_track(
                    runtime.manager,
                    runtime.server,
                    runtime.subtitle_proxy,
                    current,
                    payload["type"],
                    payload["id"],
                    stream,
                    disabled=subtitles_disabled,
                )
                return cast_service_extra(
                    subtitle,
                    title=title,
                    thumbnail=poster,
                )

            subtitle = None
            if cast_target:
                subtitle = await async_prepare_subtitle_track(
                    runtime.manager,
                    runtime.server,
                    runtime.subtitle_proxy,
                    current,
                    payload["type"],
                    payload["id"],
                    prepared.stream,
                    disabled=subtitles_disabled,
                )

            if player:
                task = self.hass.async_create_task(
                    async_monitor_initial_and_fallback(
                        self.hass,
                        runtime.server,
                        active_candidates[1:],
                        current,
                        player=player,
                        profile=profile,
                        cast_target=cast_target,
                        extra_factory=extra_factory,
                        title=title,
                        poster=poster,
                        initial_label=stream_label(prepared.stream),
                        initial_url=prepared.url,
                        total_attempts=len(ranked),
                        initial_attempt_number=prepared_index + 1,
                        prior_failures=preparation_errors,
                    ),
                    f"{DOMAIN} playback fallback for {player}",
                )
                runtime.playback_tasks[player] = task

                def _remove_finished(done_task, *, target=player) -> None:
                    if runtime.playback_tasks.get(target) is done_task:
                        runtime.playback_tasks.pop(target, None)
                    if done_task.cancelled():
                        return
                    try:
                        done_task.result()
                    except Exception as err:  # noqa: BLE001 - background monitor log.
                        _LOGGER.error(
                            "Playback fallback monitor failed for %s: %s",
                            target,
                            err,
                        )

                task.add_done_callback(_remove_finished)

            if cast_target:
                cast_payload = cast_media_source_payload(
                    prepared.url,
                    prepared.mime_type,
                    subtitle,
                    title=title,
                    thumbnail=poster,
                )
                return PlayMedia(cast_payload, "cast")
            return PlayMedia(prepared.url, prepared.mime_type)
        except StremioBridgeError as err:
            raise Unresolvable(str(err)) from err
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as err:
            raise Unresolvable("Invalid Stremio media source identifier") from err

    async def async_search_media(self, item: MediaSourceItem, query):
        """Search aggregate catalog providers on Home Assistant versions that support it."""
        if not MEDIA_SOURCE_SEARCH_SUPPORTED:
            raise BrowseError("This Home Assistant version does not expose media-source search")
        entry = self._entry_for_item(item)
        runtime: StremioBridgeRuntime = entry.runtime_data
        media_types = self._search_media_types(query)
        metas = await runtime.manager.search(str(query.search_query), media_types)
        results = [
            self._meta_preview(entry.entry_id, meta, profile=PROFILE_DEFAULT)
            for meta in metas
        ]
        return SearchMedia(result=results)

    def _root(self) -> BrowseMediaSource:
        entries = self._entries()
        return _node(
            identifier=None,
            media_class=MediaClass.APP,
            media_content_type=MediaType.APPS,
            title=NAME,
            can_play=False,
            can_expand=True,
            can_search=bool(MEDIA_SOURCE_SEARCH_SUPPORTED and len(entries) == 1),
            children_media_class=MediaClass.APP,
            children=[
                _node(
                    identifier=_encode({"kind": "entry", "entry_id": entry.entry_id}),
                    media_class=MediaClass.APP,
                    media_content_type=MediaType.APP,
                    title=entry.title,
                    can_play=False,
                    can_expand=True,
                    can_search=MEDIA_SOURCE_SEARCH_SUPPORTED,
                )
                for entry in entries
            ],
        )

    def _browse_entry(self, payload: dict[str, Any]) -> BrowseMediaSource:
        entry = self._entry(payload["entry_id"])
        runtime: StremioBridgeRuntime = entry.runtime_data
        children: list[BrowseMediaSource] = []
        available_types = sorted(
            {
                str(catalog.get("type"))
                for _, catalog in runtime.manager.catalogs(profile=PROFILE_DEFAULT)
                if catalog.get("type") in {"movie", "series"}
            },
            key=lambda value: (value != "movie", value),
        )
        for media_type in available_types:
            children.append(
                self._type_node(entry.entry_id, media_type, PROFILE_DEFAULT)
            )
        if runtime.manager.has_profile(PROFILE_LATIN):
            children.append(
                _node(
                    identifier=_encode(
                        {
                            "kind": "profile",
                            "entry_id": entry.entry_id,
                            "profile": PROFILE_LATIN,
                        }
                    ),
                    media_class=MediaClass.DIRECTORY,
                    media_content_type=MediaType.VIDEO,
                    title="Audio Latino",
                    can_play=False,
                    can_expand=True,
                    children_media_class=MediaClass.DIRECTORY,
                )
            )
        if runtime.manager.has_profile(PROFILE_SPORTS):
            children.append(
                _node(
                    identifier=_encode(
                        {
                            "kind": "profile",
                            "entry_id": entry.entry_id,
                            "profile": PROFILE_SPORTS,
                        }
                    ),
                    media_class=MediaClass.DIRECTORY,
                    media_content_type=MediaType.CHANNEL,
                    title="F1 y Deportes",
                    can_play=False,
                    can_expand=True,
                    children_media_class=MediaClass.DIRECTORY,
                )
            )
        if runtime.last_search_query:
            children.insert(
                0,
                _node(
                    identifier=_encode(
                        {"kind": "search_results", "entry_id": entry.entry_id}
                    ),
                    media_class=MediaClass.DIRECTORY,
                    media_content_type=MediaType.VIDEO,
                    title=f"Búsqueda: {runtime.last_search_query}",
                    can_play=False,
                    can_expand=True,
                    children_media_class=MediaClass.VIDEO,
                ),
            )
        return _node(
            identifier=_encode(payload),
            media_class=MediaClass.APP,
            media_content_type=MediaType.APP,
            title=entry.title,
            can_play=False,
            can_expand=True,
            can_search=MEDIA_SOURCE_SEARCH_SUPPORTED,
            children_media_class=MediaClass.DIRECTORY,
            children=children,
        )

    def _browse_profile(self, payload: dict[str, Any]) -> BrowseMediaSource:
        entry = self._entry(payload["entry_id"])
        runtime: StremioBridgeRuntime = entry.runtime_data
        profile = str(payload["profile"])
        available_types = sorted(
            {str(catalog.get("type")) for _, catalog in runtime.manager.catalogs(profile=profile)}
        )
        children = [self._type_node(entry.entry_id, media_type, profile) for media_type in available_types]
        title = "Audio Latino" if profile == PROFILE_LATIN else "F1 y Deportes"
        return _node(
            identifier=_encode(payload),
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.VIDEO,
            title=title,
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.DIRECTORY,
            children=children,
        )

    def _type_node(self, entry_id: str, media_type: str, profile: str) -> BrowseMediaSource:
        return _node(
            identifier=_encode(
                {
                    "kind": "type",
                    "entry_id": entry_id,
                    "type": media_type,
                    "profile": profile,
                }
            ),
            media_class=MediaClass.DIRECTORY,
            media_content_type=_media_type(media_type),
            title=_type_title(media_type),
            can_play=False,
            can_expand=True,
            can_search=MEDIA_SOURCE_SEARCH_SUPPORTED and profile == PROFILE_DEFAULT,
            children_media_class=MediaClass.DIRECTORY,
        )
    def _browse_type(self, payload: dict[str, Any]) -> BrowseMediaSource:
        entry = self._entry(payload["entry_id"])
        runtime: StremioBridgeRuntime = entry.runtime_data
        media_type = payload["type"]
        profile = str(payload.get("profile") or PROFILE_DEFAULT)
        children: list[BrowseMediaSource] = []
        for addon, catalog in runtime.manager.catalogs(media_type, profile):
            children.append(
                _node(
                    identifier=_encode(
                        {
                            "kind": "catalog",
                            "entry_id": entry.entry_id,
                            "type": media_type,
                            "profile": profile,
                            "catalog_id": catalog["id"],
                            "manifest_url": addon.client.manifest_url,
                            "name": catalog.get("name") or catalog["id"],
                            "extra": {},
                        }
                    ),
                    media_class=MediaClass.DIRECTORY,
                    media_content_type=_media_type(media_type),
                    title=str(catalog.get("name") or catalog["id"]),
                    can_play=False,
                    can_expand=True,
                    children_media_class=_media_class(media_type),
                    thumbnail=addon.manifest.get("logo"),
                )
            )
        return _node(
            identifier=_encode(payload),
            media_class=MediaClass.DIRECTORY,
            media_content_type=_media_type(media_type),
            title=_type_title(media_type),
            can_play=False,
            can_expand=True,
            can_search=MEDIA_SOURCE_SEARCH_SUPPORTED and profile == PROFILE_DEFAULT,
            children_media_class=MediaClass.DIRECTORY,
            children=children,
        )
    async def _browse_catalog(self, payload: dict[str, Any]) -> BrowseMediaSource:
        entry = self._entry(payload["entry_id"])
        runtime: StremioBridgeRuntime = entry.runtime_data
        catalog = self._catalog(runtime, payload)
        extra = {
            str(key): str(value)
            for key, value in dict(payload.get("extra") or {}).items()
        }
        missing_required = [
            name for name in catalog_required_extras(catalog) if name not in extra
        ]
        if missing_required:
            return self._catalog_required_options(payload, catalog, missing_required[0])

        metas = await runtime.manager.get_catalog(
            payload["manifest_url"], payload["type"], payload["catalog_id"], extra
        )
        children: list[BrowseMediaSource] = []
        if "genre" not in extra and catalog_supports_extra(catalog, "genre"):
            options = _extra_options(catalog, "genre")
            if options:
                children.append(
                    _node(
                        identifier=_encode(
                            {
                                **payload,
                                "kind": "filter",
                                "filter_name": "genre",
                                "filter_options": options,
                            }
                        ),
                        media_class=MediaClass.GENRE,
                        media_content_type=_media_type(payload["type"]),
                        title="Filtrar por género",
                        can_play=False,
                        can_expand=True,
                        children_media_class=MediaClass.GENRE,
                    )
                )
        children.extend(
            self._meta_preview(
                entry.entry_id, meta, payload["type"], str(payload.get("profile") or PROFILE_DEFAULT)
            )
            for meta in metas
        )
        if metas and catalog_supports_extra(catalog, "skip"):
            next_extra = dict(extra)
            next_extra["skip"] = str(int(extra.get("skip", "0")) + len(metas))
            children.append(
                _node(
                    identifier=_encode({**payload, "extra": next_extra}),
                    media_class=MediaClass.DIRECTORY,
                    media_content_type=_media_type(payload["type"]),
                    title="Más resultados…",
                    can_play=False,
                    can_expand=True,
                    children_media_class=_media_class(payload["type"]),
                )
            )
        title = str(payload.get("name") or payload["catalog_id"])
        if genre := extra.get("genre"):
            title = f"{title} · {genre}"
        return _node(
            identifier=_encode(payload),
            media_class=MediaClass.DIRECTORY,
            media_content_type=_media_type(payload["type"]),
            title=title,
            can_play=False,
            can_expand=True,
            children_media_class=_media_class(payload["type"]),
            children=children,
        )

    def _catalog_required_options(
        self, payload: dict[str, Any], catalog: dict[str, Any], extra_name: str
    ) -> BrowseMediaSource:
        options = _extra_options(catalog, extra_name)
        children = [
            _node(
                identifier=_encode(
                    {
                        **payload,
                        "extra": {**dict(payload.get("extra") or {}), extra_name: option},
                    }
                ),
                media_class=MediaClass.GENRE,
                media_content_type=_media_type(payload["type"]),
                title=option,
                can_play=False,
                can_expand=True,
                children_media_class=_media_class(payload["type"]),
            )
            for option in options
        ]
        return _node(
            identifier=_encode(payload),
            media_class=MediaClass.DIRECTORY,
            media_content_type=_media_type(payload["type"]),
            title=str(payload.get("name") or payload["catalog_id"]),
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.GENRE,
            children=children,
        )

    def _browse_filter(self, payload: dict[str, Any]) -> BrowseMediaSource:
        filter_name = str(payload["filter_name"])
        children = [
            _node(
                identifier=_encode(
                    {
                        **payload,
                        "kind": "catalog",
                        "extra": {
                            **dict(payload.get("extra") or {}),
                            filter_name: str(option),
                        },
                    }
                ),
                media_class=MediaClass.GENRE,
                media_content_type=_media_type(payload["type"]),
                title=str(option),
                can_play=False,
                can_expand=True,
                children_media_class=_media_class(payload["type"]),
            )
            for option in payload.get("filter_options", [])
        ]
        return _node(
            identifier=_encode(payload),
            media_class=MediaClass.GENRE,
            media_content_type=_media_type(payload["type"]),
            title="Géneros",
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.GENRE,
            children=children,
        )

    async def _browse_meta(self, payload: dict[str, Any]) -> BrowseMediaSource:
        entry = self._entry(payload["entry_id"])
        runtime: StremioBridgeRuntime = entry.runtime_data
        profile = str(payload.get("profile") or PROFILE_DEFAULT)
        try:
            meta = await runtime.manager.get_meta(payload["type"], payload["id"], profile)
        except StremioBridgeError:
            meta = {
                "id": payload["id"],
                "name": payload.get("name") or payload["id"],
                "poster": payload.get("poster"),
            }
        if payload["type"] != "series":
            return await self._stream_choices_node(payload, meta)

        videos = (
            [video for video in meta.get("videos", []) if isinstance(video, dict)]
            if isinstance(meta.get("videos"), list)
            else []
        )
        seasons = sorted(
            {
                int(video.get("season", 0) or 0)
                for video in videos
                if video.get("season") is not None
            }
        )
        if not seasons:
            return await self._stream_choices_node(payload, meta)
        children = [
            _node(
                identifier=_encode(
                    {
                        "kind": "season",
                        "entry_id": entry.entry_id,
                        "type": payload["type"],
                        "profile": profile,
                        "id": payload["id"],
                        "season": season,
                        "name": meta.get("name") or payload["id"],
                        "poster": meta.get("poster"),
                    }
                ),
                media_class=MediaClass.SEASON,
                media_content_type=MediaType.SEASON,
                title=f"Temporada {season}",
                can_play=False,
                can_expand=True,
                thumbnail=meta.get("poster"),
                children_media_class=MediaClass.EPISODE,
            )
            for season in seasons
        ]
        return _node(
            identifier=_encode(payload),
            media_class=MediaClass.TV_SHOW,
            media_content_type=MediaType.TVSHOW,
            title=str(meta.get("name") or payload["id"]),
            can_play=False,
            can_expand=True,
            thumbnail=meta.get("poster") or meta.get("background"),
            children_media_class=MediaClass.SEASON,
            children=children,
        )
    async def _browse_season(self, payload: dict[str, Any]) -> BrowseMediaSource:
        entry = self._entry(payload["entry_id"])
        runtime: StremioBridgeRuntime = entry.runtime_data
        profile = str(payload.get("profile") or PROFILE_DEFAULT)
        meta = await runtime.manager.get_meta(payload["type"], payload["id"], profile)
        season = int(payload["season"])
        videos = sorted(
            (
                video
                for video in meta.get("videos", [])
                if isinstance(video, dict) and int(video.get("season", 0) or 0) == season
            ),
            key=lambda video: int(video.get("episode", 0) or 0),
        )
        direct = self._direct_play_enabled(entry)
        children: list[BrowseMediaSource] = []
        for video in videos:
            video_id = video.get("id")
            if not isinstance(video_id, str):
                continue
            episode = int(video.get("episode", 0) or 0)
            title = str(video.get("title") or video.get("name") or video_id)
            poster = video.get("thumbnail") or meta.get("poster")
            identifier = (
                self._video_identifier(
                    entry.entry_id, payload["type"], video_id, title, poster, profile
                )
                if direct
                else _encode(
                    {
                        "kind": "stream_choices",
                        "entry_id": entry.entry_id,
                        "type": payload["type"],
                        "profile": profile,
                        "id": video_id,
                        "name": title,
                        "poster": poster,
                    }
                )
            )
            children.append(
                _node(
                    identifier=identifier,
                    media_class=MediaClass.EPISODE,
                    media_content_type=MediaType.EPISODE,
                    title=f"E{episode:02d} · {title}",
                    can_play=direct,
                    can_expand=not direct,
                    thumbnail=poster,
                    children_media_class=None if direct else MediaClass.VIDEO,
                )
            )
        return _node(
            identifier=_encode(payload),
            media_class=MediaClass.SEASON,
            media_content_type=MediaType.SEASON,
            title=f"{payload.get('name') or payload['id']} · Temporada {season}",
            can_play=False,
            can_expand=True,
            thumbnail=payload.get("poster"),
            children_media_class=MediaClass.EPISODE,
            children=children,
        )
    async def _browse_stream_choices(self, payload: dict[str, Any]) -> BrowseMediaSource:
        entry = self._entry(payload["entry_id"])
        runtime: StremioBridgeRuntime = entry.runtime_data
        streams = await runtime.manager.get_streams(
            payload["type"],
            payload["id"],
            str(payload.get("profile") or PROFILE_DEFAULT),
        )
        return self._build_stream_choices(payload, streams)

    async def _stream_choices_node(
        self, payload: dict[str, Any], meta: dict[str, Any]
    ) -> BrowseMediaSource:
        stream_payload = {
            "kind": "stream_choices",
            "entry_id": payload["entry_id"],
            "type": payload["type"],
            "profile": str(payload.get("profile") or PROFILE_DEFAULT),
            "id": payload["id"],
            "name": meta.get("name") or payload.get("name") or payload["id"],
            "poster": meta.get("poster") or payload.get("poster"),
        }
        entry = self._entry(payload["entry_id"])
        runtime: StremioBridgeRuntime = entry.runtime_data
        streams = await runtime.manager.get_streams(
            payload["type"],
            payload["id"],
            str(payload.get("profile") or PROFILE_DEFAULT),
        )
        return self._build_stream_choices(stream_payload, streams)

    def _build_stream_choices(
        self, payload: dict[str, Any], streams: list[dict[str, Any]]
    ) -> BrowseMediaSource:
        children: list[BrowseMediaSource] = []
        if streams:
            entry = self._entry(payload["entry_id"])
            current = {**entry.data, **entry.options}
            ideal_enabled = bool(
                current.get(CONF_IDEAL_LINK_FILTER, DEFAULT_IDEAL_LINK_FILTER)
            )
            profile = str(payload.get("profile") or PROFILE_DEFAULT)
            fallback_enabled = bool(
                current.get(CONF_FALLBACK_ENABLED, DEFAULT_FALLBACK_ENABLED)
            )
            fallback_count = int(
                current.get(
                    CONF_FALLBACK_SOURCE_COUNT, DEFAULT_FALLBACK_SOURCE_COUNT
                )
            )
            fallback_suffix = (
                f" · hasta {fallback_count} fuentes" if fallback_enabled else ""
            )
            auto_title = (
                f"▶ Reproducir evento{fallback_suffix}"
                if profile == PROFILE_SPORTS
                else (
                    f"▶ Enlace ideal{fallback_suffix}"
                    if ideal_enabled
                    else f"▶ Reproducir automáticamente{fallback_suffix}"
                )
            )
            base_video_payload = {
                "kind": "video",
                "entry_id": payload["entry_id"],
                "type": payload["type"],
                "profile": str(payload.get("profile") or PROFILE_DEFAULT),
                "id": payload["id"],
                "selection": "auto",
                "name": payload.get("name"),
                "poster": payload.get("poster"),
            }
            if profile in {PROFILE_LATIN, PROFILE_SPORTS}:
                base_video_payload["subtitles"] = "off"
            children.append(
                _node(
                    identifier=_encode(base_video_payload),
                    media_class=MediaClass.VIDEO,
                    media_content_type=MediaType.VIDEO,
                    title=auto_title,
                    can_play=True,
                    can_expand=False,
                    thumbnail=payload.get("poster"),
                )
            )
            children.append(
                _node(
                    identifier=_encode({**base_video_payload, "subtitles": "off"}),
                    media_class=MediaClass.VIDEO,
                    media_content_type=MediaType.VIDEO,
                    title=(
                        "▶ Enlace ideal · sin subtítulos"
                        if ideal_enabled
                        else "▶ Automático · sin subtítulos"
                    ),
                    can_play=True,
                    can_expand=False,
                    thumbnail=payload.get("poster"),
                )
            )
        for position, stream in enumerate(streams[:40]):
            children.append(
                _node(
                    identifier=_encode(
                        {
                            "kind": "video",
                            "entry_id": payload["entry_id"],
                            "type": payload["type"],
                            "profile": str(payload.get("profile") or PROFILE_DEFAULT),
                            "id": payload["id"],
                            "selection": "stream",
                            "stream_key": stream_key(stream),
                            "name": payload.get("name"),
                            "poster": payload.get("poster"),
                        }
                    ),
                    media_class=MediaClass.VIDEO,
                    media_content_type=MediaType.VIDEO,
                    title=stream_label(stream, position),
                    can_play=True,
                    can_expand=False,
                    thumbnail=payload.get("poster"),
                )
            )
        return _node(
            identifier=_encode(payload),
            media_class=_media_class(payload["type"]),
            media_content_type=_media_type(payload["type"]),
            title=str(payload.get("name") or payload["id"]),
            can_play=False,
            can_expand=True,
            thumbnail=payload.get("poster"),
            children_media_class=MediaClass.VIDEO,
            children=children,
        )

    def _browse_search_results(self, payload: dict[str, Any]) -> BrowseMediaSource:
        entry = self._entry(payload["entry_id"])
        runtime: StremioBridgeRuntime = entry.runtime_data
        children = [
            self._meta_preview(entry.entry_id, meta, profile=PROFILE_DEFAULT)
            for meta in runtime.last_search_results
        ]
        return _node(
            identifier=_encode(payload),
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.VIDEO,
            title=f"Búsqueda: {runtime.last_search_query or ''}",
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.VIDEO,
            children=children,
        )

    def _meta_preview(
        self,
        entry_id: str,
        meta: dict[str, Any],
        fallback_type: str = "movie",
        profile: str = PROFILE_DEFAULT,
    ) -> BrowseMediaSource:
        media_type = str(
            meta.get("_bridge_media_type") or meta.get("type") or fallback_type
        )
        media_id = str(meta.get("id") or "")
        name = str(meta.get("name") or meta.get("title") or media_id)
        poster = meta.get("poster") or meta.get("background")
        entry = self._entry(entry_id)
        direct = self._direct_play_enabled(entry) and media_type != "series"
        identifier = (
            self._video_identifier(entry_id, media_type, media_id, name, poster, profile)
            if direct
            else _encode(
                {
                    "kind": "meta",
                    "entry_id": entry_id,
                    "type": media_type,
                    "profile": profile,
                    "id": media_id,
                    "name": name,
                    "poster": poster,
                }
            )
        )
        return _node(
            identifier=identifier,
            media_class=_media_class(media_type),
            media_content_type=_media_type(media_type),
            title=name,
            can_play=direct,
            can_expand=not direct,
            thumbnail=poster,
            children_media_class=(
                None
                if direct
                else (MediaClass.SEASON if media_type == "series" else MediaClass.VIDEO)
            ),
        )

    def _direct_play_enabled(self, entry: ConfigEntry) -> bool:
        current = {**entry.data, **entry.options}
        return bool(
            current.get(CONF_PLAY_IDEAL_ON_SELECT, DEFAULT_PLAY_IDEAL_ON_SELECT)
        )

    @staticmethod
    def _video_identifier(
        entry_id: str,
        media_type: str,
        media_id: str,
        name: str,
        poster: Any,
        profile: str,
    ) -> str:
        payload: dict[str, Any] = {
            "kind": "video",
            "entry_id": entry_id,
            "type": media_type,
            "profile": profile,
            "id": media_id,
            "selection": "auto",
            "name": name,
            "poster": poster,
        }
        if profile in {PROFILE_LATIN, PROFILE_SPORTS}:
            payload["subtitles"] = "off"
        return _encode(payload)

    def _catalog(
        self, runtime: StremioBridgeRuntime, payload: dict[str, Any]
    ) -> dict[str, Any]:
        addon = runtime.manager.get_addon(payload["manifest_url"])
        for catalog in addon.manifest.get("catalogs", []):
            if (
                isinstance(catalog, dict)
                and catalog.get("type") == payload["type"]
                and catalog.get("id") == payload["catalog_id"]
            ):
                return catalog
        raise StremioProtocolError("Catalog is no longer declared by the add-on")

    def _entry_for_item(self, item: MediaSourceItem) -> ConfigEntry:
        if item.identifier:
            payload = _decode(item.identifier)
            if entry_id := payload.get("entry_id"):
                return self._entry(str(entry_id))
        entries = self._entries()
        if len(entries) == 1:
            return entries[0]
        raise BrowseError("Select one Stremio Stream Bridge entry before searching")

    @staticmethod
    def _search_media_types(query) -> tuple[str, ...]:
        filter_classes = getattr(query, "media_filter_classes", None)
        if not filter_classes:
            return ("movie", "series")
        values = {str(value) for value in filter_classes}
        result: list[str] = []
        if str(MediaClass.MOVIE) in values or MediaClass.MOVIE.value in values:
            result.append("movie")
        if str(MediaClass.TV_SHOW) in values or MediaClass.TV_SHOW.value in values:
            result.append("series")
        return tuple(result) or ("movie", "series")

    def _entries(self) -> list[ConfigEntry]:
        return [
            entry
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            if entry.state is ConfigEntryState.LOADED
            and getattr(entry, "runtime_data", None) is not None
        ]

    def _entry(self, entry_id: str) -> ConfigEntry:
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if (
            entry is None
            or entry.state is not ConfigEntryState.LOADED
            or getattr(entry, "runtime_data", None) is None
        ):
            raise StremioProtocolError("Stremio Stream Bridge entry is not loaded")
        return entry


def _node(*, can_search: bool = False, **kwargs: Any) -> BrowseMediaSource:
    """Create a browse node, adding the search flag only on supporting HA versions."""
    if MEDIA_SOURCE_SEARCH_SUPPORTED:
        kwargs["can_search"] = can_search
    return BrowseMediaSource(domain=DOMAIN, **kwargs)


def _extra_options(catalog: dict[str, Any], name: str) -> list[str]:
    extra = catalog_extra(catalog, name)
    options = extra.get("options", []) if isinstance(extra, dict) else []
    if not options and name == "genre":
        options = catalog.get("genres", [])
    return [str(option) for option in options if isinstance(option, (str, int))]


def _encode(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode(token: str) -> dict[str, Any]:
    padding = "=" * ((4 - len(token) % 4) % 4)
    payload = json.loads(base64.urlsafe_b64decode(token + padding))
    if not isinstance(payload, dict):
        raise ValueError("Identifier payload must be an object")
    return payload


def _type_title(media_type: str) -> str:
    return {
        "movie": "Películas",
        "series": "Series",
        "tv": "TV en vivo",
        "channel": "Canales",
        "sport": "Deportes",
    }.get(media_type, media_type.replace("_", " ").title())


def _media_class(media_type: str) -> MediaClass:
    if media_type == "movie":
        return MediaClass.MOVIE
    if media_type == "series":
        return MediaClass.TV_SHOW
    if media_type in {"tv", "channel"}:
        return MediaClass.CHANNEL
    return MediaClass.VIDEO


def _media_type(media_type: str) -> MediaType:
    if media_type == "movie":
        return MediaType.MOVIE
    if media_type == "series":
        return MediaType.TVSHOW
    if media_type in {"tv", "channel"}:
        return MediaType.CHANNEL
    return MediaType.VIDEO

"""Public search and content-resolution helpers for voice and automation clients."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from difflib import SequenceMatcher
import logging
import re
from typing import Any, Callable, Protocol
import unicodedata

from .const import (
    DEFAULT_CAST_COMPATIBILITY_FILTER,
    DEFAULT_EXCLUDE_KEYWORDS,
    DEFAULT_MAX_SIZE_GB,
    DEFAULT_PREFERRED_QUALITY,
    PROFILE_DEFAULT,
    PROFILE_LATIN,
    PROFILE_SPORTS,
)
from .stream_selector import order_ideal_streams, parse_seeders

_LOGGER = logging.getLogger(__name__)
_YEAR_PATTERN = re.compile(r"(?<!\d)(?:18|19|20|21)\d{2}(?!\d)")
_PUBLIC_RESULT_KEYS = (
    "media_id", "media_type", "title", "year", "poster", "background", "description"
)
_MAX_STREAM_PROBES = 5
_MAX_EPISODE_PROBES = 120
_STREAM_PROBE_CONCURRENCY = 6

StreamOrderer = Callable[[list[dict[str, Any]], str], list[dict[str, Any]]]


class SearchRuntime(Protocol):
    manager: Any
    last_search_query: str | None
    last_search_results: list[dict[str, Any]]


@dataclass(slots=True)
class RankedResult:
    result: dict[str, Any]
    original: dict[str, Any]
    index: int
    score: float
    title_ratio: float
    word_overlap: float
    exact_title: bool
    year_matches: bool


@dataclass(slots=True)
class StreamBackedSelection:
    ranked: RankedResult
    selected: dict[str, Any]
    seeders: int
    has_stream: bool
    order: int


def normalize_title(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = "".join(char if char.isalnum() else " " for char in text)
    return " ".join(text.split())


def extract_year(value: object) -> int | None:
    if isinstance(value, dict):
        for key in ("year", "releaseInfo", "released"):
            year = extract_year(value.get(key))
            if year is not None:
                return year
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1800 <= value <= 2199 else None
    if isinstance(value, float) and value.is_integer():
        return extract_year(int(value))
    match = _YEAR_PATTERN.search(str(value or ""))
    return int(match.group(0)) if match else None


def normalize_public_result(meta: dict[str, Any], media_type: str | None = None) -> dict[str, Any]:
    resolved_type = media_type or meta.get("_bridge_media_type") or meta.get("type") or None
    title = meta.get("name") or meta.get("title") or ""
    result: dict[str, Any] = {
        "media_id": meta.get("id") or meta.get("media_id") or None,
        "media_type": str(resolved_type) if resolved_type else None,
        "title": str(title),
        "year": extract_year(meta),
        "poster": meta.get("poster") or None,
        "background": meta.get("background") or None,
        "description": meta.get("description") or meta.get("overview") or None,
    }
    return {key: result[key] for key in _PUBLIC_RESULT_KEYS}


def _usable_public_results(raw_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for raw in raw_results:
        public = normalize_public_result(raw)
        if public["media_id"] and public["media_type"] and public["title"]:
            results.append(public)
    return results


async def async_search_and_store(runtime: SearchRuntime, query: str, media_type: str = "all") -> dict[str, Any]:
    clean_query = query.strip()
    media_types = ("movie", "series") if media_type == "all" else (media_type,)
    runtime.last_search_query = clean_query
    raw_results = await runtime.manager.search(clean_query, media_types)
    runtime.last_search_results = raw_results
    results = _usable_public_results(raw_results)
    return {"ok": True, "query": clean_query, "media_type": media_type, "count": len(results), "results": results}


def rank_results(raw_results: list[dict[str, Any]], query: str, *, requested_year: int | None = None) -> list[RankedResult]:
    normalized_query = normalize_title(query)
    query_words = set(normalized_query.split())
    ranked: list[RankedResult] = []
    for index, raw in enumerate(raw_results):
        public = normalize_public_result(raw)
        if not public["media_id"] or not public["media_type"] or not public["title"]:
            continue
        normalized_candidate = normalize_title(public["title"])
        if not normalized_candidate:
            continue
        candidate_words = set(normalized_candidate.split())
        exact_title = normalized_candidate == normalized_query
        starts_query = normalized_candidate.startswith(normalized_query)
        ratio = SequenceMatcher(None, normalized_query, normalized_candidate).ratio()
        overlap = len(query_words & candidate_words) / len(query_words) if query_words else 0.0
        candidate_year = public["year"]
        year_matches = requested_year is None or candidate_year == requested_year
        score = ratio * 100.0 + overlap * 55.0
        if exact_title:
            score += 1000.0
        if starts_query and not exact_title:
            score += 120.0
        if requested_year is not None:
            score += 240.0 if candidate_year == requested_year else -240.0
        score -= index * 0.001
        ranked.append(RankedResult(public, raw, index, score, ratio, overlap, exact_title, year_matches))
    return sorted(ranked, key=lambda item: (-item.score, item.index))


def _plausible_ranked_results(ranked: list[RankedResult], *, requested_year: int | None, limit: int = _MAX_STREAM_PROBES) -> list[RankedResult]:
    if not ranked:
        return []
    eligible = [item for item in ranked if requested_year is None or item.year_matches]
    if not eligible:
        return []
    exact = [item for item in eligible if item.exact_title]
    if exact:
        return exact[:limit]
    top = eligible[0]
    if top.title_ratio < 0.45 and top.word_overlap == 0:
        return []
    floor = max(0.45, top.title_ratio - 0.18)
    plausible = [item for item in eligible if item.title_ratio >= floor and (item.word_overlap > 0 or item.title_ratio >= 0.8)]
    return (plausible or [top])[:limit]


def select_ranked_result(ranked: list[RankedResult], *, requested_year: int | None = None) -> tuple[str, RankedResult | None]:
    plausible = _plausible_ranked_results(ranked, requested_year=requested_year)
    return ("exact", plausible[0]) if plausible else ("not_found", None)


def response_base(query: str, profile: str, media_type: str, year: int | None, season: int | None, episode: int | None) -> dict[str, Any]:
    return {
        "ok": True, "status": "not_found", "query": query, "profile": profile,
        "requested": {"media_type": media_type, "year": year, "season": season, "episode": episode},
        "selected": None, "results": [],
    }


def error_response(*, query: str, profile: str, media_type: str, year: int | None, season: int | None, episode: int | None, message: str) -> dict[str, Any]:
    return {**response_base(query, profile, media_type, year, season, episode), "ok": False, "status": "error", "error": message}


def _safe_int(value: object, *, minimum: int) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= minimum else None


def _episode_records(meta: dict[str, Any]) -> list[dict[str, Any]]:
    videos = meta.get("videos", [])
    if not isinstance(videos, list):
        return []
    episodes: list[dict[str, Any]] = []
    seen: set[tuple[int, int, str]] = set()
    for video in videos:
        if not isinstance(video, dict):
            continue
        media_id = video.get("id")
        season = _safe_int(video.get("season"), minimum=0)
        episode = _safe_int(video.get("episode"), minimum=1)
        if not isinstance(media_id, str) or season is None or episode is None:
            continue
        key = (season, episode, media_id)
        if key in seen:
            continue
        seen.add(key)
        episodes.append({
            "media_id": media_id, "season": season, "episode": episode,
            "title": str(video.get("title") or video.get("name") or f"Episode {episode}"),
            "thumbnail": video.get("thumbnail") or None,
            "released": video.get("released") or None,
            "description": video.get("description") or None,
        })
    return sorted(episodes, key=lambda item: (item["season"], item["episode"]))


def _merge_public(base: dict[str, Any], detailed: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    detail_public = normalize_public_result(detailed, str(base.get("media_type") or ""))
    for key, value in detail_public.items():
        if value not in (None, ""):
            merged[key] = value
    return merged


def _default_stream_orderer(streams: list[dict[str, Any]], profile: str) -> list[dict[str, Any]]:
    del profile
    return order_ideal_streams(
        streams, DEFAULT_MAX_SIZE_GB, DEFAULT_EXCLUDE_KEYWORDS,
        preferred_quality=DEFAULT_PREFERRED_QUALITY,
        prefer_direct_play=DEFAULT_CAST_COMPATIBILITY_FILTER,
        strict_compatibility=DEFAULT_CAST_COMPATIBILITY_FILTER,
    )


async def _best_ideal_stream_seeders(manager: Any, media_type: str, media_id: str, profile: str, stream_orderer: StreamOrderer, semaphore: asyncio.Semaphore) -> tuple[int, bool]:
    try:
        async with semaphore:
            streams = await manager.get_streams(media_type, media_id, profile)
    except Exception:
        _LOGGER.debug("Stream probe failed for %s/%s", media_type, media_id, exc_info=True)
        return 0, False
    ordered = stream_orderer(list(streams), profile)
    return (parse_seeders(ordered[0]), True) if ordered else (0, False)


def _episode_public(series_id: str, series_public: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    return {
        "media_id": item["media_id"], "media_type": "series", "series_id": series_id,
        "series_title": series_public["title"], "title": item["title"],
        "year": extract_year(item["released"]), "season": item["season"], "episode": item["episode"],
        "poster": item["thumbnail"] or series_public.get("poster"),
        "background": series_public.get("background"),
        "description": item["description"] or series_public.get("description"),
    }


async def _probe_movie(manager: Any, ranked: RankedResult, *, profile: str, stream_orderer: StreamOrderer, semaphore: asyncio.Semaphore, order: int) -> StreamBackedSelection:
    chosen = ranked.result
    try:
        detailed = await manager.get_meta("movie", str(chosen["media_id"]), PROFILE_DEFAULT)
    except Exception:
        _LOGGER.debug("Movie metadata enrichment failed for %s", chosen["media_id"])
    else:
        chosen = _merge_public(chosen, detailed)
    seeders, has_stream = await _best_ideal_stream_seeders(manager, "movie", str(chosen["media_id"]), profile, stream_orderer, semaphore)
    return StreamBackedSelection(ranked, chosen, seeders, has_stream, order)


async def _series_candidates(manager: Any, base: dict[str, Any], ranked: RankedResult, *, profile: str, season: int | None, episode: int | None, stream_orderer: StreamOrderer, semaphore: asyncio.Semaphore, order_offset: int) -> tuple[list[StreamBackedSelection], dict[str, Any] | None]:
    series_id = str(ranked.result["media_id"])
    metadata_profile = PROFILE_LATIN if profile == PROFILE_LATIN else PROFILE_DEFAULT
    try:
        meta = await manager.get_meta("series", series_id, metadata_profile)
    except Exception:
        _LOGGER.debug("Could not resolve series metadata for %s", series_id, exc_info=True)
        return [], None
    series_public = _merge_public(ranked.result, meta)
    all_episodes = _episode_records(meta)
    available_seasons = sorted({item["season"] for item in all_episodes})
    episodes = all_episodes
    if season is not None:
        episodes = [item for item in episodes if item["season"] == season]
    if episode is not None:
        episodes = [item for item in episodes if item["episode"] == episode]
    if not episodes:
        response = {**base, "status": "episode_not_found", "available_seasons": available_seasons}
        if season is not None:
            response["available_episodes"] = [
                {"season": item["season"], "episode": item["episode"], "title": item["title"]}
                for item in all_episodes if item["season"] == season
            ][:50]
        return [], response
    episodes = episodes[:_MAX_EPISODE_PROBES]

    async def probe(item: dict[str, Any], index: int) -> StreamBackedSelection:
        seeders, has_stream = await _best_ideal_stream_seeders(manager, "series", str(item["media_id"]), profile, stream_orderer, semaphore)
        return StreamBackedSelection(ranked, _episode_public(series_id, series_public, item), seeders, has_stream, order_offset + index)

    return list(await asyncio.gather(*(probe(item, index) for index, item in enumerate(episodes)))), None


def _choose_stream_backed(candidates: list[StreamBackedSelection]) -> StreamBackedSelection | None:
    if not candidates:
        return None
    return min(candidates, key=lambda item: (0 if item.has_stream else 1, -item.seeders, -item.ranked.score, item.order))


async def async_resolve_content(
    manager: Any, *, query: str, media_type: str = "all", profile: str = PROFILE_DEFAULT,
    year: int | None = None, season: int | None = None, episode: int | None = None,
    limit: int = 5, stream_orderer: StreamOrderer | None = None,
) -> dict[str, Any]:
    """Resolve directly, choosing by seeds after the ideal-link filter."""
    clean_query = query.strip()
    limit = max(1, min(int(limit), 10))
    base = response_base(clean_query, profile, media_type, year, season, episode)
    if profile == PROFILE_SPORTS:
        return {**base, "ok": False, "status": "unsupported", "error": "Sports profile search is not supported yet"}
    media_types = ("movie", "series") if media_type == "all" else (media_type,)
    try:
        raw_results = await manager.search(clean_query, media_types)
    except Exception:
        _LOGGER.exception("Catalog search failed while resolving %r", clean_query)
        return {**base, "ok": False, "status": "error", "error": "Catalog provider error"}
    ranked = rank_results(raw_results, clean_query, requested_year=year)
    plausible = _plausible_ranked_results(ranked, requested_year=year, limit=min(limit, _MAX_STREAM_PROBES))
    if not plausible:
        return {**base, "status": "not_found"}
    orderer = stream_orderer or _default_stream_orderer
    semaphore = asyncio.Semaphore(_STREAM_PROBE_CONCURRENCY)
    candidates: list[StreamBackedSelection] = []
    explicit_episode_error: dict[str, Any] | None = None
    order_offset = 0
    for item in plausible:
        if item.result["media_type"] == "series":
            selections, error = await _series_candidates(
                manager, base, item, profile=profile, season=season, episode=episode,
                stream_orderer=orderer, semaphore=semaphore, order_offset=order_offset,
            )
            candidates.extend(selections)
            order_offset += max(1, len(selections))
            if error is not None and explicit_episode_error is None:
                explicit_episode_error = error
        else:
            candidates.append(await _probe_movie(
                manager, item, profile=profile, stream_orderer=orderer,
                semaphore=semaphore, order=order_offset,
            ))
            order_offset += 1
    chosen = _choose_stream_backed(candidates)
    if chosen is None:
        return explicit_episode_error or {**base, "status": "not_found"}
    selected = dict(chosen.selected)
    selected["seeders"] = chosen.seeders
    selected["selection_reason"] = "ideal_stream_seeders"
    return {**base, "status": "exact", "selected": selected}

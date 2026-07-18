"""Tests for direct voice content resolution by ideal-stream seed count."""

import asyncio
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
import types

ROOT = Path(__file__).parents[1] / "custom_components" / "stremio_stream_bridge"
PACKAGE = "stremio_stream_bridge_content_resolver_test"
pkg = types.ModuleType(PACKAGE)
pkg.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = pkg


def load(name: str):
    spec = spec_from_file_location(f"{PACKAGE}.{name}", ROOT / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CONST = load("const")
RESOLVER = load("resolver")

MOVIES = [
    {"id": "tt0087182", "name": "Dune", "year": 1984, "_bridge_media_type": "movie"},
    {"id": "tt1160419", "name": "Dune", "year": 2021, "_bridge_media_type": "movie"},
]
SERIES_META = {
    "id": "tt0903747",
    "name": "Breaking Bad",
    "releaseInfo": "2008–2013",
    "poster": "https://image.example/bb.jpg",
    "videos": [
        {"id": "tt0903747:1:1", "season": 1, "episode": 1, "title": "Pilot"},
        {"id": "tt0903747:1:2", "season": 1, "episode": 2, "title": "Cat's in the Bag"},
        {"id": "tt0903747:2:1", "season": 2, "episode": 1, "title": "Seven Thirty-Seven"},
    ],
}


class FakeManager:
    def __init__(self, results, metas=None, streams=None, *, search_error=None):
        self.results = list(results)
        self.metas = metas or {}
        self.streams = streams or {}
        self.search_error = search_error
        self.search_calls = []
        self.meta_calls = []
        self.stream_calls = []

    async def search(self, query, media_types):
        self.search_calls.append((query, media_types))
        if self.search_error:
            raise self.search_error
        return list(self.results)

    async def get_meta(self, media_type, media_id, profile=CONST.PROFILE_DEFAULT):
        self.meta_calls.append((media_type, media_id, profile))
        return self.metas[(media_type, media_id)]

    async def get_streams(self, media_type, media_id, profile=CONST.PROFILE_DEFAULT):
        self.stream_calls.append((media_type, media_id, profile))
        return list(self.streams.get((media_type, media_id), []))


def resolve(manager, **kwargs):
    return asyncio.run(RESOLVER.async_resolve_content(manager, **kwargs))


def safe_stream(seeds, title="1080p MP4 H264 AAC"):
    return {"title": f"{title} 👤 {seeds}", "url": "https://example/video.mp4"}


def risky_stream(seeds):
    return {
        "title": f"1080p MKV HEVC DTS 5.1 👤 {seeds}",
        "behaviorHints": {"filename": "movie.1080p.x265.DTS.5.1.mkv"},
        "infoHash": "a" * 40,
    }


def test_normalization_removes_case_punctuation_and_accents():
    assert RESOLVER.normalize_title("  El NIÑO: ¡Acción! 2 ") == "el nino accion 2"


def test_same_title_movie_auto_selects_highest_seeded_ideal_source():
    manager = FakeManager(
        MOVIES,
        metas={("movie", item["id"]): item for item in MOVIES},
        streams={
            ("movie", "tt0087182"): [safe_stream(20)],
            ("movie", "tt1160419"): [safe_stream(250)],
        },
    )
    result = resolve(manager, query="Dune", media_type="movie")
    assert result["status"] == "exact"
    assert result["selected"]["media_id"] == "tt1160419"
    assert result["selected"]["seeders"] == 250
    assert result["selected"]["selection_reason"] == "ideal_stream_seeders"


def test_requested_year_still_has_priority_over_seed_count():
    manager = FakeManager(
        MOVIES,
        metas={("movie", item["id"]): item for item in MOVIES},
        streams={
            ("movie", "tt0087182"): [safe_stream(900)],
            ("movie", "tt1160419"): [safe_stream(10)],
        },
    )
    result = resolve(manager, query="Dune", media_type="movie", year=2021)
    assert result["selected"]["media_id"] == "tt1160419"


def test_ideal_filter_is_applied_before_comparing_seed_count():
    manager = FakeManager(
        MOVIES,
        metas={("movie", item["id"]): item for item in MOVIES},
        streams={
            ("movie", "tt0087182"): [risky_stream(1000), safe_stream(10)],
            ("movie", "tt1160419"): [safe_stream(20)],
        },
    )
    result = resolve(manager, query="Dune", media_type="movie")
    assert result["selected"]["media_id"] == "tt1160419"
    assert result["selected"]["seeders"] == 20


def test_series_without_episode_auto_selects_episode_with_most_seeders():
    search = [{"id": "tt0903747", "name": "Breaking Bad", "_bridge_media_type": "series"}]
    manager = FakeManager(
        search,
        metas={("series", "tt0903747"): SERIES_META},
        streams={
            ("series", "tt0903747:1:1"): [safe_stream(5)],
            ("series", "tt0903747:1:2"): [safe_stream(150)],
            ("series", "tt0903747:2:1"): [safe_stream(30)],
        },
    )
    result = resolve(manager, query="Breaking Bad", media_type="series")
    assert result["status"] == "exact"
    assert result["selected"]["media_id"] == "tt0903747:1:2"
    assert result["selected"]["season"] == 1
    assert result["selected"]["episode"] == 2
    assert result["selected"]["seeders"] == 150


def test_series_with_season_auto_selects_most_seeded_episode_in_that_season():
    search = [{"id": "tt0903747", "name": "Breaking Bad", "_bridge_media_type": "series"}]
    manager = FakeManager(
        search,
        metas={("series", "tt0903747"): SERIES_META},
        streams={
            ("series", "tt0903747:1:1"): [safe_stream(5)],
            ("series", "tt0903747:1:2"): [safe_stream(150)],
            ("series", "tt0903747:2:1"): [safe_stream(900)],
        },
    )
    result = resolve(manager, query="Breaking Bad", media_type="series", season=1)
    assert result["selected"]["media_id"] == "tt0903747:1:2"


def test_specific_episode_is_respected_even_when_another_has_more_seeders():
    search = [{"id": "tt0903747", "name": "Breaking Bad", "_bridge_media_type": "series"}]
    manager = FakeManager(
        search,
        metas={("series", "tt0903747"): SERIES_META},
        streams={
            ("series", "tt0903747:1:1"): [safe_stream(5)],
            ("series", "tt0903747:1:2"): [safe_stream(500)],
        },
    )
    result = resolve(manager, query="Breaking Bad", media_type="series", season=1, episode=1)
    assert result["selected"]["media_id"] == "tt0903747:1:1"


def test_missing_explicit_episode_remains_episode_not_found():
    search = [{"id": "tt0903747", "name": "Breaking Bad", "_bridge_media_type": "series"}]
    manager = FakeManager(search, metas={("series", "tt0903747"): SERIES_META})
    result = resolve(manager, query="Breaking Bad", media_type="series", season=9, episode=1)
    assert result["status"] == "episode_not_found"
    assert result["selected"] is None


def test_unrelated_results_are_not_found_without_stream_probes():
    manager = FakeManager([{"id": "tt1", "name": "Completely Different", "_bridge_media_type": "movie"}])
    result = resolve(manager, query="titulo absolutamente inexistente 928371")
    assert result["status"] == "not_found"
    assert manager.stream_calls == []


def test_streamless_tie_uses_catalog_order_without_confirmation():
    manager = FakeManager(MOVIES, metas={("movie", item["id"]): item for item in MOVIES})
    result = resolve(manager, query="Dune", media_type="movie")
    assert result["status"] == "exact"
    assert result["selected"]["media_id"] == "tt0087182"
    assert result["selected"]["seeders"] == 0


def test_search_still_stores_raw_results_and_returns_public_response():
    manager = FakeManager(MOVIES)
    runtime = types.SimpleNamespace(manager=manager, last_search_query=None, last_search_results=[])
    response = asyncio.run(RESOLVER.async_search_and_store(runtime, " Dune ", "movie"))
    assert runtime.last_search_query == "Dune"
    assert runtime.last_search_results == MOVIES
    assert response["count"] == 2
    assert "_bridge_media_type" not in response["results"][0]


def test_sports_profile_remains_unsupported():
    result = resolve(FakeManager([]), query="Formula 1", profile="sports")
    assert result["ok"] is False
    assert result["status"] == "unsupported"


def test_provider_error_is_structured_and_does_not_leak_details():
    result = resolve(FakeManager([], search_error=RuntimeError("secret token")), query="Matrix")
    assert result["status"] == "error"
    assert "secret" not in result["error"]

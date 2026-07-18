"""Tests for add-on routing without requiring Home Assistant."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
import types

import pytest

ROOT = Path(__file__).parents[1] / "custom_components" / "stremio_stream_bridge"
PACKAGE = "stremio_stream_bridge_test"
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


API = load("api")
AGG = load("aggregator")
SELECTOR = load("stream_selector")


class FakeClient:
    def __init__(
        self, url, manifest, *, catalogs=None, meta=None, streams=None, subtitles=None
    ):
        self.manifest_url = url
        self.base_url = url.removesuffix("/manifest.json")
        self._manifest = manifest
        self._catalogs = catalogs or {}
        self._meta = meta or {}
        self._streams = streams or {}
        self._subtitles = subtitles or {}

    async def get_manifest(self):
        return self._manifest

    async def get_catalog(self, media_type, catalog_id, extra=None):
        key = (media_type, catalog_id, tuple(sorted((extra or {}).items())))
        return self._catalogs.get(key, self._catalogs.get((media_type, catalog_id), []))

    async def get_meta(self, media_type, media_id):
        return self._meta[(media_type, media_id)]

    async def get_streams(self, media_type, media_id):
        return self._streams.get((media_type, media_id), [])

    async def get_subtitles(self, media_type, media_id, extra=None):
        return self._subtitles.get((media_type, media_id), [])


CINEMETA = {
    "id": "com.linvo.cinemeta",
    "name": "Cinemeta",
    "version": "3",
    "resources": ["catalog", "meta"],
    "types": ["movie", "series"],
    "idPrefixes": ["tt"],
    "catalogs": [
        {
            "type": "movie",
            "id": "top",
            "name": "Popular",
            "extraSupported": ["search", "genre", "skip"],
        }
    ],
}
TORRENTIO = {
    "id": "torrentio",
    "name": "Torrentio",
    "version": "1",
    "resources": [
        {"name": "stream", "types": ["movie", "series"], "idPrefixes": ["tt"]}
    ],
    "types": ["movie", "series"],
    "catalogs": [],
}

OPENSUBTITLES = {
    "id": "opensubtitles",
    "name": "OpenSubtitles v3",
    "version": "1",
    "resources": ["subtitles"],
    "types": ["movie", "series"],
    "idPrefixes": ["tt"],
    "catalogs": [],
}


def test_resource_matching():
    assert AGG.supports_resource(TORRENTIO, "stream", "movie", "tt0133093")
    assert not AGG.supports_resource(TORRENTIO, "stream", "movie", "local-video")
    assert not AGG.supports_resource(TORRENTIO, "meta", "movie", "tt0133093")


def test_stream_key():
    assert AGG.stream_key({"infoHash": "ABC", "fileIdx": 2}) == "bt:abc:2"


def test_torrentio_style_selection_prefers_1080_and_avoids_cam():
    streams = [
        {
            "name": "Torrentio\n4K HDR",
            "title": "Movie 2160p\n👤 20 💾 18.4 GB",
        },
        {
            "name": "Torrentio\n1080p",
            "title": "Movie BluRay DTS 1080p\n👤 82 💾 7.2 GB",
        },
        {
            "name": "Torrentio\n1080p CAM",
            "title": "Movie HDCAM 1080p\n👤 900 💾 2.0 GB",
        },
    ]
    selected = SELECTOR.choose_best_stream(streams, "1080p", 12, "CAM, HDCAM, TS")
    assert "BluRay" in selected["title"]
    # TS must not accidentally reject the DTS token.
    assert SELECTOR.parse_seeders(selected) == 82


def test_stream_label():
    stream = {
        "name": "Torrentio 1080p",
        "title": "Movie 1080p 👤 12 💾 3.5 GB",
        "_bridge_addon_name": "Torrentio",
    }
    label = SELECTOR.stream_label(stream, 0)
    assert "1080p" in label
    assert "3.5 GB" in label
    assert "12 semillas" in label


@pytest.mark.asyncio
async def test_manager_combines_catalog_and_stream_providers():
    catalog_client = FakeClient(
        "https://catalog/manifest.json",
        CINEMETA,
        catalogs={
            ("movie", "top"): [{"id": "tt0133093", "name": "The Matrix", "type": "movie"}],
            ("movie", "top", (("search", "Matrix"),)): [
                {"id": "tt0133093", "name": "The Matrix", "type": "movie"}
            ],
        },
        meta={("movie", "tt0133093"): {"id": "tt0133093", "name": "The Matrix"}},
    )
    stream_client = FakeClient(
        "https://streams/manifest.json",
        TORRENTIO,
        streams={
            ("movie", "tt0133093"): [
                {"infoHash": "0123456789abcdef0123456789abcdef01234567", "fileIdx": 0}
            ]
        },
    )
    manager = AGG.StremioAddonManager([catalog_client], [stream_client])
    await manager.async_refresh()
    assert manager.catalogs("movie")[0][1]["id"] == "top"
    assert (await manager.get_meta("movie", "tt0133093"))["name"] == "The Matrix"
    streams = await manager.get_streams("movie", "tt0133093")
    assert streams[0]["_bridge_addon_name"] == "Torrentio"
    results = await manager.search("Matrix", ("movie",))
    assert results[0]["_bridge_media_type"] == "movie"


def test_ideal_link_prefers_1080_highest_seeders_then_smallest():
    streams = [
        {"title": "Movie 1080p 👤 50 💾 2.0 GB"},
        {"title": "Movie 1080p 👤 120 💾 5.0 GB"},
        {"title": "Movie 1080p 👤 120 💾 3.0 GB"},
        {"title": "Movie 2160p 👤 500 💾 10.0 GB"},
    ]
    selected = SELECTOR.choose_ideal_stream(streams, 12, "CAM, TS")
    assert "120" in selected["title"]
    assert "3.0 GB" in selected["title"]


@pytest.mark.asyncio
async def test_manager_combines_subtitle_provider_and_stream_subtitles():
    stream_client = FakeClient(
        "https://streams/manifest.json",
        TORRENTIO,
    )
    subtitle_client = FakeClient(
        "https://subs/manifest.json",
        OPENSUBTITLES,
        subtitles={
            ("movie", "tt0133093"): [
                {"id": "es", "url": "https://subs.example/es.srt", "lang": "spa"}
            ]
        },
    )
    manager = AGG.StremioAddonManager([], [stream_client], [subtitle_client])
    await manager.async_refresh()
    subtitles = await manager.get_subtitles(
        "movie",
        "tt0133093",
        {"filename": "movie.mkv"},
        {
            "subtitles": [
                {"id": "en", "url": "https://subs.example/en.vtt", "lang": "eng"}
            ]
        },
    )
    assert [subtitle["lang"] for subtitle in subtitles] == ["eng", "spa"]

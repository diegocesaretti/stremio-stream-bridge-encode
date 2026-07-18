"""Tests for v0.4 provider profiles, audio compatibility and Cast styling."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
import types

import pytest

ROOT = Path(__file__).parents[1] / "custom_components" / "stremio_stream_bridge"
PACKAGE = "stremio_stream_bridge_v04_test"
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
PLAYBACK = load("playback")
STYLE = load("cast_style")
CONST = load("const")


class FakeClient:
    def __init__(self, url, manifest, *, streams=None):
        self.manifest_url = url
        self.base_url = url.removesuffix("/manifest.json")
        self._manifest = manifest
        self._streams = streams or {}

    async def get_manifest(self):
        return self._manifest

    async def get_catalog(self, media_type, catalog_id, extra=None):
        return []

    async def get_meta(self, media_type, media_id):
        return {"id": media_id, "name": media_id}

    async def get_streams(self, media_type, media_id):
        return self._streams.get((media_type, media_id), [])

    async def get_subtitles(self, media_type, media_id, extra=None):
        return []


CATALOG = {
    "id": "catalog",
    "name": "Catalog",
    "resources": ["catalog", "meta"],
    "types": ["movie", "series"],
    "catalogs": [{"type": "movie", "id": "top", "name": "Popular"}],
}
LATIN = {
    "id": "latin",
    "name": "Latino",
    "resources": [{"name": "stream", "types": ["movie", "series"]}],
    "types": ["movie", "series"],
    "catalogs": [],
}
SPORTS = {
    "id": "sports",
    "name": "Sports",
    "resources": ["catalog", "meta", "stream"],
    "types": ["tv"],
    "catalogs": [{"type": "tv", "id": "live", "name": "Live"}],
}


@pytest.mark.asyncio
async def test_latin_profile_mirrors_default_catalog_and_uses_only_latin_streams():
    catalog = FakeClient("https://catalog/manifest.json", CATALOG)
    latin = FakeClient(
        "https://latin/manifest.json",
        LATIN,
        streams={
            ("movie", "tt1"): [{"url": "https://video.example/latino.mp4"}]
        },
    )
    manager = AGG.StremioAddonManager([catalog], [], latin_clients=[latin])
    await manager.async_refresh()
    assert manager.catalogs("movie", CONST.PROFILE_LATIN)[0][1]["id"] == "top"
    streams = await manager.get_streams("movie", "tt1", CONST.PROFILE_LATIN)
    assert streams[0]["_bridge_addon_name"] == "Latino"


@pytest.mark.asyncio
async def test_sports_profile_uses_own_catalog():
    sports = FakeClient("https://sports/manifest.json", SPORTS)
    manager = AGG.StremioAddonManager([], [], sports_clients=[sports])
    await manager.async_refresh()
    assert manager.catalogs("tv", CONST.PROFILE_SPORTS)[0][1]["id"] == "live"


def test_automatic_audio_mode_preserves_direct_torrent_playback():
    server = API.StremioStreamServerClient(object(), "http://server:11470")
    stream = {
        "infoHash": "0123456789abcdef0123456789abcdef01234567",
        "fileIdx": 0,
        "behaviorHints": {"filename": "movie.mkv"},
    }
    url, mime = PLAYBACK.prepare_playback(
        server,
        stream,
        {CONST.CONF_AUDIO_MODE: "automatic"},
        profile=CONST.PROFILE_DEFAULT,
    )
    assert "/hlsv2/" not in url
    assert "/0123456789abcdef0123456789abcdef01234567/0" in url
    assert mime == "video/matroska"


def test_sports_hls_is_not_wrapped_twice():
    server = API.StremioStreamServerClient(object(), "http://server:11470")
    stream = {"url": "https://sports.example/live.m3u8"}
    url, mime = PLAYBACK.prepare_playback(
        server,
        stream,
        {CONST.CONF_AUDIO_MODE: "automatic"},
        profile=CONST.PROFILE_SPORTS,
    )
    assert url == "https://sports.example/live.m3u8"
    assert mime == "application/vnd.apple.mpegurl"


def test_cast_subtitle_style_removes_black_outline_and_window():
    message = {
        "type": "LOAD",
        "media": {
            "tracks": [{"trackId": 1}],
            "textTrackStyle": {"edgeType": "OUTLINE", "edgeColor": "#000000FF"},
        },
    }
    STYLE.remove_subtitle_edges(message)
    style = message["media"]["textTrackStyle"]
    assert style["edgeType"] == "NONE"
    assert style["edgeColor"] == "#00000000"
    assert style["windowType"] == "NONE"


def test_force_transcode_audio_mode_sets_force_flag():
    server = API.StremioStreamServerClient(object(), "http://server:11470")
    stream = {"url": "https://video.example/movie.mp4"}
    url, mime = PLAYBACK.prepare_playback(
        server,
        stream,
        {CONST.CONF_AUDIO_MODE: "force_transcode"},
        profile=CONST.PROFILE_DEFAULT,
    )
    assert "/hlsv2/" in url
    assert "forceTranscoding=1" in url
    assert "audioCodecs=aac" in url
    assert mime == "application/vnd.apple.mpegurl"


def test_hls_url_mime_wins_over_filename_hint():
    stream = {
        "url": "https://video.example/live.m3u8",
        "behaviorHints": {"filename": "movie.mp4"},
    }
    assert (
        API.guess_stream_mime_type(stream, "http://server:11470/proxy/options/live.m3u8")
        == "application/vnd.apple.mpegurl"
    )


@pytest.mark.asyncio
async def test_automatic_playback_skips_dead_proxied_playlist():
    class FakeServer:
        def resolve_stream(self, stream):
            return stream["url"]

        def build_compatible_hls_url(self, media_url, **kwargs):
            return media_url

        async def async_validate_media_url(self, url, mime_type):
            if "dead" in url:
                return False, "HTTP 403"
            return True, None

    candidates = [
        {"url": "http://server:11470/proxy/options/dead.m3u8"},
        {"url": "http://server:11470/proxy/options/working.m3u8"},
    ]
    stream, url, mime = await PLAYBACK.prepare_first_playable(
        FakeServer(),
        candidates,
        {CONST.CONF_AUDIO_MODE: "direct"},
        profile=CONST.PROFILE_SPORTS,
    )
    assert stream is candidates[1]
    assert url.endswith("working.m3u8")
    assert mime == "application/vnd.apple.mpegurl"


def test_v043_safe_defaults_are_populated():
    assert CONST.DEFAULT_STREAMING_SERVER_URL == "http://192.168.1.145:11470"
    assert CONST.DEFAULT_AUDIO_MODE == "direct"
    assert "language=spanish,latino" in CONST.DEFAULT_LATIN_MANIFEST
    assert CONST.DEFAULT_SPORTS_MANIFEST == (
        "https://stremverse1.alwaysdata.net/manifest.json"
    )


@pytest.mark.asyncio
async def test_optional_provider_failure_does_not_block_core_addons():
    class FailingClient(FakeClient):
        async def get_manifest(self):
            raise API.StremioConnectionError("optional provider unavailable")

    catalog = FakeClient("https://catalog/manifest.json", CATALOG)
    default_stream = FakeClient("https://stream/manifest.json", LATIN)
    optional_sports = FailingClient("https://sports/manifest.json", SPORTS)
    manager = AGG.StremioAddonManager(
        [catalog],
        [default_stream],
        sports_clients=[optional_sports],
    )
    addons = await manager.async_refresh()
    assert len(addons) == 2
    assert "https://sports/manifest.json" in manager.errors
    assert manager.catalogs("movie", CONST.PROFILE_DEFAULT)


@pytest.mark.asyncio
async def test_failed_forced_hls_falls_back_to_direct_url():
    class FakeServer:
        def resolve_stream(self, stream):
            return "http://server:11470/hash/0?f=movie.mkv"

        def build_compatible_hls_url(self, media_url, **kwargs):
            return "http://server:11470/hlsv2/session/master.m3u8?mediaURL=x"

        async def async_validate_media_url(self, url, mime_type):
            if "/hlsv2/" in url:
                return False, "HTTP 500"
            return True, None

    stream = {
        "infoHash": "0123456789abcdef0123456789abcdef01234567",
        "fileIdx": 0,
        "behaviorHints": {"filename": "movie.mkv"},
    }
    selected, url, mime = await PLAYBACK.prepare_first_playable(
        FakeServer(),
        [stream],
        {CONST.CONF_AUDIO_MODE: "force_transcode"},
        profile=CONST.PROFILE_DEFAULT,
    )
    assert selected is stream
    assert "/hlsv2/" not in url
    assert url.endswith("movie.mkv")
    assert mime == "video/matroska"


def test_cast_ideal_order_prefers_mp4_h264_aac_over_seeded_mkv_x265():
    mkv = {
        "name": "1080p HEVC",
        "title": "500 seeders · 2.0 GB",
        "behaviorHints": {"filename": "movie.1080p.x265.DTS.mkv"},
    }
    mp4 = {
        "name": "1080p x264 AAC",
        "title": "80 seeders · 3.0 GB",
        "behaviorHints": {"filename": "movie.1080p.x264.AAC.mp4"},
    }
    ordered = load("stream_selector").order_ideal_streams(
        [mkv, mp4],
        12.0,
        "CAM, TS",
        prefer_direct_play=True,
    )
    assert ordered[0] is mp4


def test_cast_mkv_torrent_restores_legacy_mp4_mime():
    stream = {
        "infoHash": "0123456789abcdef0123456789abcdef01234567",
        "fileIdx": 0,
        "behaviorHints": {"filename": "movie.1080p.x264.mkv"},
    }
    server = API.StremioStreamServerClient(object(), "http://server:11470")
    url = server.resolve_stream(stream)
    assert API.guess_stream_mime_type(stream, url, cast_target=True) == "video/mp4"
    assert API.guess_stream_mime_type(stream, url, cast_target=False) == "video/matroska"


def test_stream_label_exposes_container():
    selector = load("stream_selector")
    stream = {
        "name": "1080p",
        "behaviorHints": {"filename": "movie.x264.AAC.mp4"},
    }
    assert "MP4" in selector.stream_label(stream)


def test_strict_cast_filter_drops_known_incompatible_when_alternative_exists():
    selector = load("stream_selector")
    incompatible = {
        "name": "1080p HEVC DTS",
        "title": "900 seeders · 1.5 GB",
        "behaviorHints": {"filename": "movie.1080p.x265.DTS.mkv"},
    }
    compatible = {
        "name": "1080p H264 AAC",
        "title": "20 seeders · 4.0 GB",
        "behaviorHints": {"filename": "movie.1080p.x264.AAC.mp4"},
    }
    ordered = selector.order_ideal_streams(
        [incompatible, compatible],
        12.0,
        "CAM, TS",
        prefer_direct_play=True,
        strict_compatibility=True,
    )
    assert ordered == [compatible]


def test_strict_cast_filter_keeps_incompatible_when_it_is_the_only_source():
    selector = load("stream_selector")
    incompatible = {
        "name": "1080p HEVC DTS",
        "behaviorHints": {"filename": "movie.1080p.x265.DTS.mkv"},
    }
    ordered = selector.order_ideal_streams(
        [incompatible],
        12.0,
        "CAM, TS",
        prefer_direct_play=True,
        strict_compatibility=True,
    )
    assert ordered == [incompatible]


def test_stream_label_exposes_video_and_audio_codecs():
    selector = load("stream_selector")
    stream = {
        "name": "1080p",
        "behaviorHints": {"filename": "movie.x264.AAC.mp4"},
    }
    label = selector.stream_label(stream)
    assert "H.264" in label
    assert "AAC" in label


def test_v044_session_and_compatibility_defaults_are_enabled():
    assert CONST.DEFAULT_CAST_COMPATIBILITY_FILTER is True
    assert CONST.DEFAULT_STOP_BEFORE_PLAY is True


def test_cast_filter_prefers_stereo_aac_over_aac_5_1():
    selector = load("stream_selector")
    surround = {
        "name": "1080p x264 AAC5.1",
        "title": "400 seeders · 2.0 GB",
        "behaviorHints": {"filename": "movie.1080p.x264.AAC5.1.mp4"},
    }
    stereo = {
        "name": "1080p x264 AAC 2.0",
        "title": "30 seeders · 3.0 GB",
        "behaviorHints": {"filename": "movie.1080p.x264.AAC.2.0.mp4"},
    }
    ordered = selector.order_ideal_streams(
        [surround, stereo],
        12.0,
        "CAM, TS",
        prefer_direct_play=True,
        strict_compatibility=True,
    )
    assert ordered == [stereo]


def test_ranked_sources_follow_selected_quality_before_seed_count():
    selector = load("stream_selector")
    source_1080 = {
        "name": "1080p x264 AAC",
        "title": "900 seeders · 2.0 GB",
        "behaviorHints": {"filename": "movie.1080p.x264.AAC.mp4"},
    }
    source_720 = {
        "name": "720p x264 AAC",
        "title": "20 seeders · 1.0 GB",
        "behaviorHints": {"filename": "movie.720p.x264.AAC.mp4"},
    }
    ordered = selector.order_ideal_streams(
        [source_1080, source_720],
        12.0,
        "CAM, TS",
        preferred_quality="720p",
        prefer_direct_play=True,
    )
    assert ordered[0] is source_720


def test_v050_fallback_defaults_are_enabled():
    assert CONST.DEFAULT_CAST_RESET_BEFORE_PLAY is True
    assert CONST.DEFAULT_FALLBACK_ENABLED is True
    assert CONST.DEFAULT_FALLBACK_SOURCE_COUNT == 5
    assert CONST.DEFAULT_PLAYBACK_START_TIMEOUT == 15
    assert CONST.DEFAULT_FAILURE_NOTIFY_HA is True

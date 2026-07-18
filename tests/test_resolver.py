"""Resolver smoke tests that do not require Home Assistant."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

API_PATH = Path(__file__).parents[1] / "custom_components" / "stremio_stream_bridge" / "api.py"
SPEC = spec_from_file_location("stremio_stream_bridge_api", API_PATH)
assert SPEC is not None and SPEC.loader is not None
API = module_from_spec(SPEC)
SPEC.loader.exec_module(API)
StremioStreamServerClient = API.StremioStreamServerClient


class DummySession:
    pass


def test_info_hash_url() -> None:
    client = StremioStreamServerClient(DummySession(), "http://192.168.1.50:11470")
    url = client.resolve_stream(
        {
            "infoHash": "0123456789abcdef0123456789abcdef01234567",
            "fileIdx": 3,
            "sources": ["tracker:https://tracker.example/announce", "dht:abc"],
            "behaviorHints": {"filename": "movie.mkv"},
        }
    )
    assert url.startswith("http://192.168.1.50:11470/0123456789abcdef0123456789abcdef01234567/3?")
    assert "tr=https%3A%2F%2Ftracker.example%2Fannounce" in url
    assert "f=movie.mkv" in url


def test_magnet_url() -> None:
    client = StremioStreamServerClient(DummySession(), "http://server:11470")
    url = client.resolve_magnet(
        "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=Movie"
    )
    assert "/0123456789abcdef0123456789abcdef01234567/-1" in url


def test_direct_url() -> None:
    client = StremioStreamServerClient(DummySession(), "http://server:11470")
    assert client.resolve_stream({"url": "https://video.example/movie.mp4"}) == (
        "https://video.example/movie.mp4"
    )


def test_proxy_headers() -> None:
    client = StremioStreamServerClient(DummySession(), "http://server:11470")
    url = client.resolve_stream(
        {
            "url": "https://video.example/path/movie.mp4?token=1",
            "behaviorHints": {"proxyHeaders": {"request": {"Referer": "https://origin.example"}}},
        }
    )
    assert url.startswith("http://server:11470/proxy/")
    assert "token=1" in url


def test_base32_btih_conversion() -> None:
    client = StremioStreamServerClient(DummySession(), "http://server:11470")
    # 20 zero bytes encoded as base32.
    url = client.build_torrent_url("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    assert "/0000000000000000000000000000000000000000/-1" in url


def test_uppercase_magnet_prefix() -> None:
    client = StremioStreamServerClient(DummySession(), "http://server:11470")
    url = client.resolve_magnet("magnet:?xt=URN:BTIH:0123456789ABCDEF0123456789ABCDEF01234567")
    assert "/0123456789abcdef0123456789abcdef01234567/-1" in url


def test_subtitle_vtt_url() -> None:
    client = StremioStreamServerClient(DummySession(), "http://server:11470")
    url = client.build_subtitle_vtt_url("https://subs.example/movie.srt?token=abc")
    assert url.startswith("http://server:11470/subtitles.vtt?from=")
    assert "https%3A%2F%2Fsubs.example%2Fmovie.srt%3Ftoken%3Dabc" in url


def test_configured_manifest_url_with_commas_stays_single_url() -> None:
    url = (
        "https://torrentio.strem.fun/"
        "providers=yts,eztv,1337x|language=spanish,latino/manifest.json"
    )
    assert API.parse_manifest_urls(url) == [url]


class FakeBody:
    async def read(self, _size: int) -> bytes:
        return b"x"


class FakeResponse:
    status = 206
    headers = {"Content-Type": "video/mp4"}
    content = FakeBody()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class RecordingSession:
    def __init__(self) -> None:
        self.headers = None

    def get(self, _url, *, headers=None, timeout=None):
        self.headers = headers
        return FakeResponse()


async def _validate_prebuffer() -> None:
    session = RecordingSession()
    client = StremioStreamServerClient(session, "http://server:11470")
    ok, reason = await client.async_validate_media_url(
        "http://server:11470/0123456789abcdef0123456789abcdef01234567/0?f=movie.mp4",
        "video/mp4",
    )
    assert ok is True
    assert reason is None
    assert session.headers == {"Range": "bytes=0-65535"}


def test_torrent_url_is_prebuffered_before_playback() -> None:
    import asyncio

    asyncio.run(_validate_prebuffer())

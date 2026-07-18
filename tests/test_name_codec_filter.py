"""Name-only H.264/H.265 preference tests."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
import types

ROOT = Path(__file__).parents[1] / "custom_components" / "stremio_stream_bridge"
PACKAGE = "stremio_stream_bridge_name_filter_test"

pkg = types.ModuleType(PACKAGE)
pkg.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = pkg

aggregator = types.ModuleType(f"{PACKAGE}.aggregator")
aggregator.stream_key = lambda stream: str(stream.get("url") or stream.get("name") or "")
sys.modules[aggregator.__name__] = aggregator

spec = spec_from_file_location(f"{PACKAGE}.stream_selector", ROOT / "stream_selector.py")
assert spec is not None and spec.loader is not None
SELECTOR = module_from_spec(spec)
sys.modules[spec.name] = SELECTOR
spec.loader.exec_module(SELECTOR)


def stream(name: str, *, seeds: int, url_id: str) -> dict:
    return {
        "name": name,
        "description": f"{seeds} seeders",
        "url": f"https://video.example/{url_id}",
    }


def test_named_codec_recognizes_common_h264_and_h265_spellings():
    for name in ("Movie.x264.1080p", "Movie H264", "Movie H.264", "Movie AVC"):
        assert SELECTOR.parse_named_video_codec({"name": name}) == "H.264"
    for name in ("Movie.x265.1080p", "Movie H265", "Movie H.265", "Movie HEVC"):
        assert SELECTOR.parse_named_video_codec({"name": name}) == "HEVC"


def test_name_filter_does_not_probe_codec_from_url():
    item = {"name": "Movie 1080p", "url": "https://video.example/movie-x264.mp4"}
    assert SELECTOR.parse_named_video_codec(item) is None
    assert SELECTOR.parse_video_codec(item) == "H.264"


def test_h264_name_wins_over_h265_even_with_fewer_seeders():
    h265 = stream("Movie 1080p x265", seeds=900, url_id="hevc")
    h264 = stream("Movie 1080p x264", seeds=12, url_id="avc")

    ordered = SELECTOR.order_ideal_streams([h265, h264], 0, "")

    assert ordered == [h264, h265]


def test_h265_remains_available_as_fallback():
    h265 = stream("Movie 1080p HEVC", seeds=900, url_id="hevc")
    unknown = stream("Movie 1080p WEB-DL", seeds=100, url_id="unknown")
    h264 = stream("Movie 720p H.264", seeds=5, url_id="avc")

    ordered = SELECTOR.order_ideal_streams([h265, unknown, h264], 0, "")

    assert ordered[0] is h264
    assert ordered[-1] is h265
    assert set(map(id, ordered)) == {id(h264), id(h265), id(unknown)}


def test_existing_ranking_is_unchanged_when_no_h264_name_exists():
    h265 = stream("Movie 1080p x265", seeds=100, url_id="hevc")
    unknown = stream("Movie 720p WEB-DL", seeds=10, url_id="unknown")

    ordered = SELECTOR.order_ideal_streams([unknown, h265], 0, "")

    assert ordered[0] is h265


def test_manual_best_stream_uses_same_name_preference():
    h265 = stream("Movie 1080p x265", seeds=1000, url_id="hevc")
    h264 = stream("Movie 720p x264", seeds=1, url_id="avc")

    selected = SELECTOR.choose_best_stream([h265, h264], "1080p", 0, "")

    assert selected is h264

"""Tests for subtitle decoding and WebVTT conversion."""

from importlib.util import module_from_spec, spec_from_file_location
from io import BytesIO
from pathlib import Path
import gzip
from zipfile import ZipFile

PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "stremio_stream_bridge"
    / "subtitle_codec.py"
)
SPEC = spec_from_file_location("stremio_stream_bridge_subtitle_codec", PATH)
assert SPEC is not None and SPEC.loader is not None
CODEC = module_from_spec(SPEC)
SPEC.loader.exec_module(CODEC)

SRT = """1
00:00:01,250 --> 00:00:03,500
Hola, Córdoba.

2
00:00:04,000 --> 00:00:05,000
Segundo diálogo.
"""


def test_srt_to_webvtt() -> None:
    result = CODEC.subtitle_to_webvtt(SRT)
    assert result.startswith("WEBVTT\n\n")
    assert "00:00:01.250 --> 00:00:03.500" in result
    assert "\n1\n" not in result


def test_existing_webvtt_is_preserved() -> None:
    source = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello\n"
    assert CODEC.subtitle_to_webvtt(source) == source


def test_gzip_subtitle() -> None:
    payload = gzip.compress(SRT.encode("utf-8"))
    assert "Hola" in CODEC.decode_subtitle_text(payload)


def test_zip_subtitle() -> None:
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("movie.srt", SRT.encode("utf-8"))
    assert "Segundo" in CODEC.decode_subtitle_text(output.getvalue())


def test_windows_1252_subtitle() -> None:
    payload = SRT.replace("Córdoba", "acción").encode("cp1252")
    assert "acción" in CODEC.decode_subtitle_text(payload)

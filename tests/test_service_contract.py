"""Static regression checks for the Home Assistant service contract."""

import ast
import json
from pathlib import Path

ROOT = Path(__file__).parents[1]
INIT_PATH = ROOT / "custom_components" / "stremio_stream_bridge" / "__init__.py"
SERVICES_PATH = ROOT / "custom_components" / "stremio_stream_bridge" / "services.yaml"
MANIFEST_PATH = ROOT / "custom_components" / "stremio_stream_bridge" / "manifest.json"


def _source() -> str:
    return INIT_PATH.read_text(encoding="utf-8")


def test_existing_services_remain_registered_and_resolve_is_added():
    source = _source()
    for name in (
        "SERVICE_PLAY", "SERVICE_PLAY_URL", "SERVICE_SEARCH", "SERVICE_REFRESH",
        "SERVICE_SUBTITLE_DIAGNOSTICS", "SERVICE_RESOLVE",
    ):
        assert f"DOMAIN,\n        {name}," in source or f"DOMAIN, {name}," in source


def test_search_uses_optional_response_and_resolve_uses_only_response():
    source = _source()
    registrations = source[source.index("    hass.services.async_register(DOMAIN, SERVICE_PLAY"):]
    search_start = registrations.index("SERVICE_SEARCH,")
    resolve_start = registrations.index("SERVICE_RESOLVE,")
    subtitle_start = registrations.index("SERVICE_SUBTITLE_DIAGNOSTICS,")
    assert "SupportsResponse.OPTIONAL" in registrations[search_start:resolve_start]
    assert "SupportsResponse.ONLY" in registrations[resolve_start:subtitle_start]


def test_play_handler_still_delegates_to_existing_playback_pipeline():
    tree = ast.parse(_source())
    setup = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup")
    play = next(node for node in setup.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_play")
    calls = {node.func.id for node in ast.walk(play) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert "async_play_ranked_candidates" in calls
    assert "async_resolve_content" not in calls


def test_services_yaml_documents_search_and_resolve():
    text = SERVICES_PATH.read_text(encoding="utf-8")
    assert "search:" in text
    assert "resolve:" in text
    assert "No inicia reproducción" in text


def test_manifest_is_052_without_config_entry_migration_increment():
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert manifest["version"] == "0.5.2"
    assert "VERSION = 8" in (ROOT / "custom_components" / "stremio_stream_bridge" / "config_flow.py").read_text(encoding="utf-8")

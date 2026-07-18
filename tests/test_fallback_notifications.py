"""Tests for ranked fallback limits, startup detection and failure notifications."""

import asyncio
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
import types

ROOT = Path(__file__).parents[1] / "custom_components" / "stremio_stream_bridge"
PACKAGE = "stremio_stream_bridge_fallback_test"
pkg = types.ModuleType(PACKAGE)
pkg.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = pkg

ha = types.ModuleType("homeassistant")
ha_const = types.ModuleType("homeassistant.const")
ha_const.ATTR_ENTITY_ID = "entity_id"
ha_core = types.ModuleType("homeassistant.core")
ha_core.HomeAssistant = object
sys.modules.setdefault("homeassistant", ha)
sys.modules["homeassistant.const"] = ha_const
sys.modules["homeassistant.core"] = ha_core


def load(name: str):
    spec = spec_from_file_location(f"{PACKAGE}.{name}", ROOT / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CONST = load("const")
load("aggregator")
load("api")
load("stream_selector")
load("playback")
load("session_control")
NOTIFY = load("failure_notifications")
SUPERVISOR = load("playback_supervisor")


def test_ranked_candidates_default_to_five_and_can_be_disabled():
    candidates = [{"id": index} for index in range(8)]
    limited = SUPERVISOR.limit_ranked_candidates(candidates, {})
    assert [item["id"] for item in limited] == [0, 1, 2, 3, 4]
    one = SUPERVISOR.limit_ranked_candidates(
        candidates,
        {CONST.CONF_FALLBACK_ENABLED: False},
    )
    assert one == [candidates[0]]


class SequencedStates:
    def __init__(self, states):
        self.states = list(states)
        self.index = 0

    def get(self, _entity_id):
        value, content_id = self.states[min(self.index, len(self.states) - 1)]
        self.index += 1
        return types.SimpleNamespace(
            state=value,
            attributes={"media_content_id": content_id},
        )


async def _wait_for_start():
    expected = "http://server/movie.mp4"
    hass = types.SimpleNamespace(
        states=SequencedStates(
            [
                ("off", ""),
                ("buffering", expected),
                ("playing", expected),
            ]
        )
    )
    original_sleep = SUPERVISOR.asyncio.sleep

    async def no_sleep(_delay):
        return None

    SUPERVISOR.asyncio.sleep = no_sleep
    try:
        return await SUPERVISOR.async_wait_for_playback_start(
            hass,
            "media_player.tv",
            timeout=5,
            expected_url=expected,
        )
    finally:
        SUPERVISOR.asyncio.sleep = original_sleep


def test_player_must_reach_playing_for_the_expected_url():
    ok, reason = asyncio.run(_wait_for_start())
    assert ok is True
    assert reason == "playing"


class FakeServices:
    def __init__(self):
        self.calls = []

    async def async_call(self, domain, service, data, **kwargs):
        self.calls.append((domain, service, data, kwargs))


async def _send_failure_notifications():
    services = FakeServices()
    hass = types.SimpleNamespace(services=services)
    await NOTIFY.async_notify_playback_failure(
        hass,
        {
            CONST.CONF_FAILURE_NOTIFY_HA: True,
            CONST.CONF_TVOVERLAY_ENABLED: True,
            CONST.CONF_TVOVERLAY_SERVICE: "notify.tvoverlaynotify",
            CONST.CONF_TVOVERLAY_DURATION: 12,
        },
        title="Película de prueba",
        poster="https://image.example/poster.jpg",
        attempts=5,
        reasons=["timeout"],
    )
    return services.calls


def test_failure_notifies_home_assistant_and_tvoverlay_with_cover():
    calls = asyncio.run(_send_failure_notifications())
    assert calls[0][0:2] == ("persistent_notification", "create")
    assert "poster.jpg" in calls[0][2]["message"]
    assert calls[1][0:2] == ("notify", "tvoverlaynotify")
    assert calls[1][2]["data"]["image"].endswith("poster.jpg")
    assert calls[1][2]["data"]["seconds"] == 12


async def _run_ranked_retry():
    candidates = [{"name": "first"}, {"name": "second"}]
    attempts = []
    original_attempt = SUPERVISOR._async_attempt_candidate

    async def fake_attempt(*args, **kwargs):
        candidate = args[2]
        attempts.append(candidate["name"])
        if candidate["name"] == "first":
            return False, None, "timeout"
        prepared = SUPERVISOR.PreparedCandidate(
            candidate,
            "http://server/second.mp4",
            "video/mp4",
        )
        return True, prepared, "playing"

    async def no_extra(_stream):
        return None

    SUPERVISOR._async_attempt_candidate = fake_attempt
    try:
        result = await SUPERVISOR.async_play_ranked_candidates(
            types.SimpleNamespace(),
            object(),
            candidates,
            {
                CONST.CONF_FALLBACK_ENABLED: True,
                CONST.CONF_FALLBACK_SOURCE_COUNT: 5,
            },
            player="media_player.tv",
            profile=CONST.PROFILE_DEFAULT,
            cast_target=True,
            extra_factory=no_extra,
            title="Test",
            poster=None,
        )
    finally:
        SUPERVISOR._async_attempt_candidate = original_attempt
    return attempts, result


def test_ranked_playback_tries_next_source_after_failure():
    attempts, result = asyncio.run(_run_ranked_retry())
    assert attempts == ["first", "second"]
    assert result.stream["name"] == "second"


async def _send_tvoverlay_ui_notification():
    services = FakeServices()
    hass = types.SimpleNamespace(services=services)
    await NOTIFY.async_notify_playback_failure(
        hass,
        {
            CONST.CONF_FAILURE_NOTIFY_HA: False,
            CONST.CONF_TVOVERLAY_ENABLED: True,
            CONST.CONF_TVOVERLAY_SERVICE: "tvoverlay_ui.notify",
            CONST.CONF_TVOVERLAY_TARGET: "living_room_tv",
            CONST.CONF_TVOVERLAY_DURATION: 9,
        },
        title="Película de prueba",
        poster="https://image.example/poster.jpg",
        attempts=5,
        reasons=["timeout"],
    )
    return services.calls


def test_tvoverlay_ui_uses_stable_target_and_supported_cover_fields():
    calls = asyncio.run(_send_tvoverlay_ui_notification())
    assert calls[0][0:2] == ("tvoverlay_ui", "notify")
    data = calls[0][2]
    assert data["target"] == "living_room_tv"
    assert data["media_type"] == "image"
    assert data["media_url"].endswith("poster.jpg")
    assert data["large_icon"].endswith("poster.jpg")
    assert data["duration"] == 9


async def _send_progress_notifications():
    services = FakeServices()
    hass = types.SimpleNamespace(services=services)
    options = {
        CONST.CONF_TVOVERLAY_ENABLED: True,
        CONST.CONF_TVOVERLAY_SERVICE: "notify.tvoverlaynotify",
        CONST.CONF_TVOVERLAY_DURATION: 8,
    }
    await NOTIFY.async_notify_source_search(
        hass,
        options,
        title="The Matrix",
        poster="https://image.example/matrix.jpg",
        attempt=2,
        total=5,
    )
    await NOTIFY.async_notify_playback_started(
        hass,
        options,
        title="The Matrix",
        poster="https://image.example/matrix.jpg",
    )
    return services.calls


def test_tvoverlay_notifies_source_search_and_success():
    calls = asyncio.run(_send_progress_notifications())
    assert [call[0:2] for call in calls] == [
        ("notify", "tvoverlaynotify"),
        ("notify", "tvoverlaynotify"),
    ]
    assert "Buscando una fuente" in calls[0][2]["message"]
    assert "(2/5)" in calls[0][2]["message"]
    assert calls[1][2]["message"] == "Estás viendo «The Matrix»."
    assert calls[1][2]["data"]["image"].endswith("matrix.jpg")


def test_tvoverlay_default_service_is_notify_tvoverlaynotify():
    assert CONST.DEFAULT_TVOVERLAY_SERVICE == "notify.tvoverlaynotify"


class AvailableServices(FakeServices):
    def has_service(self, domain, service):
        return (domain, service) == ("notify", "tvoverlaynotify")


async def _send_legacy_service_fallback():
    services = AvailableServices()
    hass = types.SimpleNamespace(services=services)
    await NOTIFY.async_notify_playback_started(
        hass,
        {
            CONST.CONF_TVOVERLAY_ENABLED: True,
            CONST.CONF_TVOVERLAY_SERVICE: "tvoverlay_ui.notify",
        },
        title="The Matrix",
        poster=None,
    )
    return services.calls


def test_legacy_tvoverlay_ui_setting_falls_back_to_notify_service():
    calls = asyncio.run(_send_legacy_service_fallback())
    assert calls[0][0:2] == ("notify", "tvoverlaynotify")

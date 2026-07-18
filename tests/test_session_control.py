"""Tests for the pre-playback player stop/session release helper."""

import asyncio
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
import types

ROOT = Path(__file__).parents[1] / "custom_components" / "stremio_stream_bridge"
PACKAGE = "stremio_stream_bridge_session_test"
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
SESSION = load("session_control")


class FakeStateMachine:
    def __init__(self, state="playing"):
        self.state = state

    def get(self, _entity_id):
        return types.SimpleNamespace(state=self.state)


class FakeServices:
    def __init__(self, states):
        self.calls = []
        self.states = states

    async def async_call(self, domain, service, data, **kwargs):
        self.calls.append((domain, service, data, kwargs))
        if service == "media_stop":
            self.states.state = "idle"
        elif service == "turn_off":
            self.states.state = "off"


async def _run_stop_test(*, cast_target=False):
    states = FakeStateMachine()
    services = FakeServices(states)
    hass = types.SimpleNamespace(states=states, services=services)
    original_sleep = SESSION.asyncio.sleep

    async def no_sleep(_delay):
        return None

    SESSION.asyncio.sleep = no_sleep
    try:
        stopped = await SESSION.async_prepare_player_session(
            hass,
            "media_player.tv",
            {
                CONST.CONF_STOP_BEFORE_PLAY: True,
                CONST.CONF_CAST_RESET_BEFORE_PLAY: True,
            },
            cast_target=cast_target,
        )
    finally:
        SESSION.asyncio.sleep = original_sleep
    return stopped, states, services


def test_current_player_is_stopped_before_new_stream():
    stopped, states, services = asyncio.run(_run_stop_test())
    assert stopped is True
    assert states.state == "idle"
    assert services.calls[0][0:3] == (
        "media_player",
        "media_stop",
        {"entity_id": "media_player.tv"},
    )


def test_cast_receiver_is_stopped_and_turned_off_before_new_stream():
    stopped, states, services = asyncio.run(_run_stop_test(cast_target=True))
    assert stopped is True
    assert states.state == "off"
    assert [call[1] for call in services.calls] == ["media_stop", "turn_off"]


def test_cast_reset_still_turns_off_receiver_when_stop_toggle_is_disabled():
    states = FakeStateMachine("idle")
    services = FakeServices(states)
    hass = types.SimpleNamespace(states=states, services=services)
    original_sleep = SESSION.asyncio.sleep

    async def no_sleep(_delay):
        return None

    SESSION.asyncio.sleep = no_sleep
    try:
        changed = asyncio.run(
            SESSION.async_prepare_player_session(
                hass,
                "media_player.tv",
                {
                    CONST.CONF_STOP_BEFORE_PLAY: False,
                    CONST.CONF_CAST_RESET_BEFORE_PLAY: True,
                },
                cast_target=True,
            )
        )
    finally:
        SESSION.asyncio.sleep = original_sleep

    assert changed is True
    assert states.state == "off"
    assert [call[1] for call in services.calls] == ["turn_off"]

"""Prepare the target player and streaming session before starting new media."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CAST_RESET_BEFORE_PLAY,
    CONF_STOP_BEFORE_PLAY,
    DEFAULT_CAST_RESET_BEFORE_PLAY,
    DEFAULT_STOP_BEFORE_PLAY,
)

_LOGGER = logging.getLogger(__name__)
_STOP_SETTLE_SECONDS = 0.8
_CAST_OFF_TIMEOUT_SECONDS = 5.0
_POLL_SECONDS = 0.25


async def _async_wait_until_inactive(
    hass: HomeAssistant,
    player: str,
    *,
    timeout: float,
) -> bool:
    """Wait until a player reports an inactive state."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        state = hass.states.get(player)
        if state is None or state.state in {"off", "idle", "standby"}:
            return True
        await asyncio.sleep(_POLL_SECONDS)
    return False


async def _async_call_player_service(
    hass: HomeAssistant,
    service: str,
    player: str,
    *,
    context: Any | None,
) -> bool:
    try:
        await hass.services.async_call(
            "media_player",
            service,
            {ATTR_ENTITY_ID: player},
            blocking=True,
            context=context,
        )
    except Exception as err:  # noqa: BLE001 - cleanup must not block all playback.
        _LOGGER.warning(
            "Could not call media_player.%s on %s before playback: %s",
            service,
            player,
            err,
        )
        return False
    return True


async def async_prepare_player_session(
    hass: HomeAssistant,
    player: str | None,
    options: dict[str, Any],
    *,
    cast_target: bool = False,
    context: Any | None = None,
) -> bool:
    """Stop old media and fully close the Cast receiver before new playback.

    For Home Assistant Cast entities, ``turn_off`` quits the currently running
    Cast application rather than powering off the television. This produces a
    clean receiver session and releases the previous stream-server HTTP reader.
    """
    if not player:
        return False

    stop_enabled = bool(
        options.get(CONF_STOP_BEFORE_PLAY, DEFAULT_STOP_BEFORE_PLAY)
    )
    reset_cast = cast_target and bool(
        options.get(CONF_CAST_RESET_BEFORE_PLAY, DEFAULT_CAST_RESET_BEFORE_PLAY)
    )
    if not stop_enabled and not reset_cast:
        return False

    changed = False
    state = hass.states.get(player)
    state_value = state.state if state is not None else None

    if stop_enabled and state_value not in {
        None,
        "off",
        "idle",
        "standby",
        "unavailable",
        "unknown",
    }:
        changed = await _async_call_player_service(
            hass,
            "media_stop",
            player,
            context=context,
        ) or changed
        await asyncio.sleep(_STOP_SETTLE_SECONDS)

    if reset_cast:
        # Even an idle Cast entity may still have the Default Media Receiver app
        # open. Quit it so stale sessions cannot interfere with the next source.
        current = hass.states.get(player)
        if current is None or current.state != "off":
            changed = await _async_call_player_service(
                hass,
                "turn_off",
                player,
                context=context,
            ) or changed
        inactive = await _async_wait_until_inactive(
            hass,
            player,
            timeout=_CAST_OFF_TIMEOUT_SECONDS,
        )
        if not inactive:
            # One defensive second pass handles a stale media controller that
            # ignored the first stop while the receiver app was shutting down.
            await _async_call_player_service(
                hass,
                "media_stop",
                player,
                context=context,
            )
            await _async_call_player_service(
                hass,
                "turn_off",
                player,
                context=context,
            )
            inactive = await _async_wait_until_inactive(
                hass,
                player,
                timeout=2.0,
            )
        if not inactive:
            current = hass.states.get(player)
            _LOGGER.warning(
                "Cast player %s did not report off/idle after two reset attempts "
                "(state=%s); playback startup verification will reject stale media",
                player,
                current.state if current is not None else "missing",
            )
        else:
            _LOGGER.debug("Cast receiver %s is inactive and ready", player)
    elif changed:
        await _async_wait_until_inactive(hass, player, timeout=2.0)

    return changed

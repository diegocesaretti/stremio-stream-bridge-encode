"""Supervise playback startup and retry ranked Stremio sources."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant

from .api import StremioBridgeError, StremioStreamServerClient
from .const import (
    CONF_FALLBACK_ENABLED,
    CONF_FALLBACK_SOURCE_COUNT,
    CONF_PLAYBACK_START_TIMEOUT,
    DEFAULT_FALLBACK_ENABLED,
    DEFAULT_FALLBACK_SOURCE_COUNT,
    DEFAULT_PLAYBACK_START_TIMEOUT,
)
from .failure_notifications import (
    async_notify_playback_failure,
    async_notify_playback_started,
    async_notify_source_search,
)
from .playback import prepare_first_playable
from .session_control import async_prepare_player_session
from .stream_selector import stream_label

_LOGGER = logging.getLogger(__name__)
_POLL_SECONDS = 0.5
_MIN_STARTUP_GRACE_SECONDS = 4.0
_SUCCESS_STATES = {"playing", "paused"}
_TRANSITION_STATES = {"buffering", "on", "opening"}
_INACTIVE_STATES = {"off", "idle", "standby"}

ExtraFactory = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]


@dataclass(slots=True)
class PreparedCandidate:
    """One resolved candidate ready for media_player.play_media."""

    stream: dict[str, Any]
    url: str
    mime_type: str


def limit_ranked_candidates(
    candidates: Sequence[dict[str, Any]], options: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Return the configured number of ordered automatic candidates."""
    if not candidates:
        return []
    if not bool(options.get(CONF_FALLBACK_ENABLED, DEFAULT_FALLBACK_ENABLED)):
        return [candidates[0]]
    count = int(
        options.get(CONF_FALLBACK_SOURCE_COUNT, DEFAULT_FALLBACK_SOURCE_COUNT)
        or DEFAULT_FALLBACK_SOURCE_COUNT
    )
    return list(candidates[: max(1, min(count, 10))])


async def async_prepare_candidate(
    server: StremioStreamServerClient,
    candidate: dict[str, Any],
    options: Mapping[str, Any],
    *,
    profile: str,
    cast_target: bool,
) -> PreparedCandidate:
    """Resolve and validate one candidate without silently trying another."""
    stream, url, mime_type = await prepare_first_playable(
        server,
        [candidate],
        options,
        profile=profile,
        cast_target=cast_target,
    )
    return PreparedCandidate(stream, url, mime_type)


async def async_send_play_media(
    hass: HomeAssistant,
    player: str,
    prepared: PreparedCandidate,
    *,
    extra: dict[str, Any] | None,
    context: Any | None = None,
) -> None:
    """Send one prepared source to a Home Assistant media player."""
    data: dict[str, Any] = {
        ATTR_ENTITY_ID: player,
        "media_content_id": prepared.url,
        "media_content_type": prepared.mime_type,
    }
    if extra:
        data["extra"] = extra
    await hass.services.async_call(
        "media_player",
        "play_media",
        data,
        blocking=True,
        context=context,
    )


async def async_wait_for_playback_start(
    hass: HomeAssistant,
    player: str,
    *,
    timeout: float,
    expected_url: str | None = None,
) -> tuple[bool, str]:
    """Wait until the selected player actually reports usable playback."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = started + max(3.0, timeout)
    seen_transition = False
    observed: list[str] = []
    initial_value: str | None = None
    state_changed = False

    while loop.time() < deadline:
        state = hass.states.get(player)
        value = state.state if state is not None else "missing"
        if initial_value is None:
            initial_value = value
        elif value != initial_value:
            state_changed = True
        if not observed or observed[-1] != value:
            observed.append(value)

        attrs = state.attributes if state is not None else {}
        content_id = str(
            attrs.get("media_content_id")
            or attrs.get("media_id")
            or attrs.get("content_id")
            or ""
        )
        content_matches = bool(
            expected_url
            and content_id
            and (content_id == expected_url or expected_url in content_id)
        )
        if value in _SUCCESS_STATES:
            if not expected_url or content_matches or state_changed or seen_transition:
                return True, value
            if not content_id and loop.time() - started >= 3.0:
                # Some media_player platforms never expose media_content_id.
                return True, value
        if value in _TRANSITION_STATES:
            seen_transition = True
        if (
            value in _INACTIVE_STATES
            and loop.time() - started >= _MIN_STARTUP_GRACE_SECONDS
            and seen_transition
        ):
            return False, f"player returned to {value} after starting"
        if value in {"unavailable", "unknown"} and loop.time() - started >= 3.0:
            return False, f"player state is {value}"
        await asyncio.sleep(_POLL_SECONDS)

    trail = " → ".join(observed[-6:]) or "no state"
    return False, f"did not reach playing within {timeout:g}s ({trail})"


async def _async_attempt_candidate(
    hass: HomeAssistant,
    server: StremioStreamServerClient,
    candidate: dict[str, Any],
    options: Mapping[str, Any],
    *,
    player: str,
    profile: str,
    cast_target: bool,
    extra_factory: ExtraFactory,
    timeout: float,
    context: Any | None,
) -> tuple[bool, PreparedCandidate | None, str]:
    """Reset the player, resolve one source, send it and confirm startup."""
    try:
        await async_prepare_player_session(
            hass,
            player,
            dict(options),
            cast_target=cast_target,
            context=context,
        )
        prepared = await async_prepare_candidate(
            server,
            candidate,
            options,
            profile=profile,
            cast_target=cast_target,
        )
        extra = await extra_factory(prepared.stream)
        await async_send_play_media(
            hass,
            player,
            prepared,
            extra=extra,
            context=context,
        )
        ok, reason = await async_wait_for_playback_start(
            hass,
            player,
            timeout=timeout,
            expected_url=prepared.url,
        )
        return ok, prepared, reason
    except asyncio.CancelledError:
        raise
    except Exception as err:  # noqa: BLE001 - each source is independently retryable.
        return False, None, str(err)


async def async_play_ranked_candidates(
    hass: HomeAssistant,
    server: StremioStreamServerClient,
    candidates: Sequence[dict[str, Any]],
    options: Mapping[str, Any],
    *,
    player: str,
    profile: str,
    cast_target: bool,
    extra_factory: ExtraFactory,
    title: str,
    poster: str | None,
    context: Any | None = None,
) -> PreparedCandidate:
    """Try ranked candidates until playback starts or all options are exhausted."""
    ranked = limit_ranked_candidates(candidates, options)
    if not ranked:
        raise StremioBridgeError("No stream candidates are available")

    timeout = float(
        options.get(CONF_PLAYBACK_START_TIMEOUT, DEFAULT_PLAYBACK_START_TIMEOUT)
        or DEFAULT_PLAYBACK_START_TIMEOUT
    )
    failures: list[str] = []

    for index, candidate in enumerate(ranked, start=1):
        _LOGGER.info(
            "Trying ranked source %s/%s for %s: %s",
            index,
            len(ranked),
            title,
            stream_label(candidate),
        )
        await async_notify_source_search(
            hass,
            options,
            title=title,
            poster=poster,
            attempt=index,
            total=len(ranked),
            context=context,
        )
        ok, prepared, reason = await _async_attempt_candidate(
            hass,
            server,
            candidate,
            options,
            player=player,
            profile=profile,
            cast_target=cast_target,
            extra_factory=extra_factory,
            timeout=timeout,
            context=context,
        )
        if ok and prepared is not None:
            if index > 1:
                _LOGGER.warning(
                    "Playback succeeded with fallback source %s/%s for %s",
                    index,
                    len(ranked),
                    title,
                )
            await async_notify_playback_started(
                hass,
                options,
                title=title,
                poster=poster,
                context=context,
            )
            return prepared
        failures.append(f"{stream_label(candidate)}: {reason}")
        _LOGGER.warning(
            "Ranked source %s/%s failed for %s: %s",
            index,
            len(ranked),
            title,
            reason,
        )

    await async_notify_playback_failure(
        hass,
        options,
        title=title,
        poster=poster,
        attempts=len(ranked),
        reasons=failures,
        context=context,
    )
    raise StremioBridgeError(
        f"None of the {len(ranked)} ranked sources reached playing state"
    )


async def async_monitor_initial_and_fallback(
    hass: HomeAssistant,
    server: StremioStreamServerClient,
    remaining_candidates: Sequence[dict[str, Any]],
    options: Mapping[str, Any],
    *,
    player: str,
    profile: str,
    cast_target: bool,
    extra_factory: ExtraFactory,
    title: str,
    poster: str | None,
    initial_label: str,
    initial_url: str,
    total_attempts: int,
    initial_attempt_number: int = 1,
    prior_failures: Sequence[str] = (),
) -> None:
    """Monitor a media-source play command and retry remaining sources internally."""
    timeout = float(
        options.get(CONF_PLAYBACK_START_TIMEOUT, DEFAULT_PLAYBACK_START_TIMEOUT)
        or DEFAULT_PLAYBACK_START_TIMEOUT
    )
    failures: list[str] = list(prior_failures)
    try:
        await async_notify_source_search(
            hass,
            options,
            title=title,
            poster=poster,
            attempt=initial_attempt_number,
            total=total_attempts,
        )
        # The Cast/media-player integration receives the PlayMedia return value after
        # this task is scheduled, so allow the original service call to begin first.
        await asyncio.sleep(0.75)
        ok, reason = await async_wait_for_playback_start(
            hass,
            player,
            timeout=timeout,
            expected_url=initial_url,
        )
        if ok:
            await async_notify_playback_started(
                hass,
                options,
                title=title,
                poster=poster,
            )
            return
        failures.append(f"{initial_label}: {reason}")
        _LOGGER.warning("Initial media-source candidate failed for %s: %s", title, reason)

        for offset, candidate in enumerate(
            remaining_candidates, start=initial_attempt_number + 1
        ):
            await async_notify_source_search(
                hass,
                options,
                title=title,
                poster=poster,
                attempt=offset,
                total=total_attempts,
            )
            ok, prepared, reason = await _async_attempt_candidate(
                hass,
                server,
                candidate,
                options,
                player=player,
                profile=profile,
                cast_target=cast_target,
                extra_factory=extra_factory,
                timeout=timeout,
                context=None,
            )
            if ok and prepared is not None:
                _LOGGER.warning(
                    "Media-source fallback succeeded with source %s/%s for %s",
                    offset,
                    total_attempts,
                    title,
                )
                await async_notify_playback_started(
                    hass,
                    options,
                    title=title,
                    poster=poster,
                )
                return
            failures.append(f"{stream_label(candidate)}: {reason}")
            _LOGGER.warning(
                "Media-source source %s/%s failed for %s: %s",
                offset,
                total_attempts,
                title,
                reason,
            )

        await async_notify_playback_failure(
            hass,
            options,
            title=title,
            poster=poster,
            attempts=total_attempts,
            reasons=failures,
        )
    except asyncio.CancelledError:
        _LOGGER.debug("Playback fallback monitor for %s was cancelled", player)
        raise

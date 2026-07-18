"""Adjust Google Cast subtitle styling emitted by PyChromecast."""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)
_PATCHED = False


def remove_subtitle_edges(message: Any) -> Any:
    """Mutate a Cast LOAD message so text tracks have no outline or window."""
    if not isinstance(message, dict):
        return message
    media = message.get("media")
    if not isinstance(media, dict) or not media.get("tracks"):
        return message
    media["textTrackStyle"] = {
        "backgroundColor": "#00000000",
        "foregroundColor": "#FFFFFFFF",
        "edgeType": "NONE",
        "edgeColor": "#00000000",
        "windowType": "NONE",
        "windowColor": "#00000000",
        "fontScale": 1.0,
    }
    return message


def install_no_edge_subtitle_patch() -> bool:
    """Patch PyChromecast LOAD messages once for borderless subtitles."""
    global _PATCHED  # noqa: PLW0603
    if _PATCHED:
        return True
    try:
        from pychromecast.controllers.media import BaseMediaPlayer
    except ImportError:
        _LOGGER.warning("PyChromecast is unavailable; subtitle edge patch was not installed")
        return False

    original: Callable[..., Any] = BaseMediaPlayer.send_message
    if getattr(original, "_ssb_borderless", False):
        _PATCHED = True
        return True

    def send_message(self, data, *args, **kwargs):
        remove_subtitle_edges(data)
        return original(self, data, *args, **kwargs)

    setattr(send_message, "_ssb_borderless", True)
    BaseMediaPlayer.send_message = send_message
    _PATCHED = True
    _LOGGER.info("Installed borderless Cast subtitle style")
    return True

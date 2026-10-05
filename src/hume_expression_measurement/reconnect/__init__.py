# Maintained by hand and listed in .fernignore, so SDK regeneration leaves it in place.

"""
Realtime sessions that reconnect when the server restarts or the connection drops, and send each
new server session the last `session.update`. A session cannot be resumed: each reconnect is a new
server session with a new session ID.
"""

from ._endpoints import (
    AsyncReconnectingAudioSession,
    AsyncReconnectingVideoSession,
    ReconnectingAudioSession,
    ReconnectingVideoSession,
    connect_audio,
    connect_video,
)
from ._session import Reconnected, Reconnecting, SessionEnded

__all__ = [
    "AsyncReconnectingAudioSession",
    "AsyncReconnectingVideoSession",
    "Reconnected",
    "Reconnecting",
    "ReconnectingAudioSession",
    "ReconnectingVideoSession",
    "SessionEnded",
    "connect_audio",
    "connect_video",
]

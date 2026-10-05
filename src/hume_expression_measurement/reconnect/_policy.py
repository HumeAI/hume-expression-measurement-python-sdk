# Maintained by hand and listed in .fernignore, so SDK regeneration leaves it in place.

import dataclasses
import random as _random
import typing

# How a connection that ended is followed up: not at all, at once, or after a backoff.
Followup = typing.Literal["stop", "immediate", "backoff"]


@dataclasses.dataclass(frozen=True)
class Ending:
    """How a connection ended, as far as the client could see."""

    # 1006 when the connection dropped without a close frame.
    code: int
    opened: bool
    client_closing: bool
    http_status: typing.Optional[int] = None
    session_closed_reason: typing.Optional[str] = None


def followup_for(ending: Ending) -> Followup:
    if ending.client_closing:
        return "stop"
    if ending.http_status is not None:
        status = ending.http_status
        return "backoff" if status >= 500 or status in (408, 429) else "stop"
    if not ending.opened:
        return "backoff"
    # Servers released before close code 1012 end a drained session with 1000.
    if ending.session_closed_reason == "server_shutdown":
        return "immediate"
    if ending.code in (1001, 1006, 1012):
        return "immediate"
    if ending.code in (1011, 1013, 1014):
        return "backoff"
    return "stop"


@dataclasses.dataclass(frozen=True)
class Backoff:
    # Consecutive attempts allowed before giving up.
    max_attempts: int = 6
    # The backoff before the first retry, which doubles with each further attempt.
    initial_delay: float = 1.0
    max_delay: float = 30.0


# Spreads the reconnects of every session a restarting server drains at once.
IMMEDIATE_JITTER = 0.25


def delay_for(
    attempt: int,
    followup: Followup,
    backoff: Backoff,
    random: typing.Callable[[], float] = _random.random,
) -> float:
    """The wait in seconds before `attempt`, counting from 1."""
    if followup == "immediate" and attempt == 1:
        return random() * IMMEDIATE_JITTER
    ceiling: float = min(backoff.max_delay, backoff.initial_delay * 2 ** (attempt - 1))
    return ceiling * (0.5 + random() / 2)

"""Process lifecycle flags for graceful shutdown.

The danger: the container is asked to stop while an engine tick is midway
through an order submit -> confirm sequence in execution.py. We must never
abandon an order we've already placed. Two mechanisms cooperate:

1. A shutdown flag (this module). The engine checks it before STARTING new
   order work (new entries), so a shutdown that arrives between cycles stops
   new positions from opening. Work already begun is never interrupted.
2. APScheduler ``shutdown(wait=True)`` in the app lifespan (bounded by a
   timeout) lets the currently-running tick finish before the loop exits.
"""

from __future__ import annotations

_shutting_down = False


def request_shutdown() -> None:
    global _shutting_down
    _shutting_down = True


def is_shutting_down() -> bool:
    return _shutting_down


def reset() -> None:
    """Test helper — clear the flag between cases."""
    global _shutting_down
    _shutting_down = False

from sqlalchemy.orm import Session

from qt import security
from qt.broker.alpaca import (
    LIVE_BASE_URL,
    LIVE_SECRET_KEY_ID,
    LIVE_SECRET_KEY_SECRET,
    PAPER_BASE_URL,
    SECRET_KEY_ID,
    SECRET_KEY_SECRET,
    AlpacaClient,
)

# WHICH CREDENTIALS AND WHICH HOST, per mode. One table, because the pairing is
# the dangerous part and it should be readable in one place rather than inferred
# from two `if` statements in different functions.
#
# The invariant, and the only one that matters here: the LIVE credentials appear
# with the LIVE host and nowhere else, and the live host appears with the live
# credentials and nowhere else. Cross-wire it either way and the failure is
# silent in the direction that costs money — paper keys against the live host
# merely 401, but a paper-mode client pointed at the live host places real
# orders while every label in the app says paper.
#
# SHADOW USES THE PAPER CREDENTIALS DELIBERATELY. Shadow places no order at all,
# but it does read quotes, bars and the clock, and those calls need a key. Giving
# it the live pair would put the live account one bug away from a shadow
# strategy — the mode whose entire purpose is to touch nothing.
_TARGETS: dict[str, tuple[str, str, str]] = {
    "shadow": (SECRET_KEY_ID, SECRET_KEY_SECRET, PAPER_BASE_URL),
    "paper": (SECRET_KEY_ID, SECRET_KEY_SECRET, PAPER_BASE_URL),
    "live": (LIVE_SECRET_KEY_ID, LIVE_SECRET_KEY_SECRET, LIVE_BASE_URL),
}


def places_orders(mode: str | None) -> bool:
    """Whether this mode submits orders to a broker at all.

    THE POINT OF HAVING THIS AT ALL. Before live existed, "does this touch the
    broker" was written everywhere as `mode == "paper"`, and "not paper" silently
    meant "do nothing" — a safe default with only shadow and paper, and exactly
    the wrong one the moment a third mode appears. Left alone, a live strategy
    would have journalled its decisions and placed NOTHING on the way in, and —
    worse — could never have been exited on the way out, because `close_trade`
    asked the same question. A live position with no way to close it is the worst
    single outcome in this codebase.

    Written as an inclusion list rather than `!= "shadow"` for the same reason:
    a fourth mode added later must default to NOT trading, and have to be added
    here deliberately."""
    return (mode or "").strip().lower() in ("paper", "live")


def broker_target(mode: str | None) -> tuple[str, str, str] | None:
    """(key-secret name, secret-secret name, base URL) for a mode, or None.

    Pure, so the wiring can be asserted exhaustively without a broker or a
    database. An unknown mode returns None rather than falling back to paper: a
    mode nobody recognises must not silently acquire the ability to trade, and
    'defaults to paper' is the kind of helpfulness that hides a typo until it is
    a live one."""
    return _TARGETS.get((mode or "").strip().lower())


def get_client(session: Session, mode: str = "paper") -> AlpacaClient | None:
    """Build the client for `mode` from the stored (encrypted) keys.

    None when that mode's keys are not stored — which for `live` is the normal
    state and is what keeps live unreachable until Werner enters his own live
    credentials. Callers already treat None as "not configured, do nothing",
    so an unconfigured live book simply does not trade.

    Defaults to paper so the many existing callers that ask for "the client"
    keep the behaviour they had before modes existed.
    """
    target = broker_target(mode)
    if target is None:
        return None
    key_name, secret_name, base_url = target
    key_id = security.get_secret(session, key_name)
    key_secret = security.get_secret(session, secret_name)
    if not key_id or not key_secret:
        return None
    return AlpacaClient(key_id=key_id, key_secret=key_secret, base_url=base_url)


def live_credentials_stored(session: Session) -> bool:
    """Whether a live key PAIR is on file. The gate on offering live at all.

    Both halves are required: a half-configured live account is not a usable
    one, and reporting it as available would let a strategy be promoted to live
    and then quietly fail to trade."""
    return bool(
        security.get_secret(session, LIVE_SECRET_KEY_ID)
        and security.get_secret(session, LIVE_SECRET_KEY_SECRET)
    )

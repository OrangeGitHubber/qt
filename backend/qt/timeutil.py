"""One place that turns a stored datetime into a timestamp the API can honestly
emit.

QT stores UTC everywhere, but SQLite has no timezone type: a
``DateTime(timezone=True)`` column round-trips as a NAIVE datetime, tzinfo
silently dropped. Serialise that with ``.isoformat()`` and the API hands out
"2026-08-02T18:15:00" — which every consumer, a browser first among them, reads
as LOCAL time. The value is right and the label is missing, so the reader
mislabels it, and nothing anywhere raises.

So the offset is stamped at the source, once, here. The rule is a restatement of
what the database already means rather than an assumption: naive means UTC.

NOT for dates. A day bucket ("2026-08-02") is a calendar day, not an instant —
giving it a time and an offset would move it across the dateline for anyone east
or west of UTC and break day-by-day matching. Pass those through untouched.

This module deliberately imports nothing from ``qt``, so any layer can use it.
"""

from datetime import datetime, timezone

__all__ = ["utc_aware", "iso_utc"]


def utc_aware(dt: datetime | None) -> datetime | None:
    """The same moment, guaranteed to carry a timezone.

    A naive datetime is declared UTC (that is what QT stores); one that already
    knows its offset is returned untouched, so a non-UTC value is never silently
    relabelled. None stays None — "no timestamp" is a real answer.
    """
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def iso_utc(dt: datetime | None) -> str | None:
    """``dt`` as an ISO-8601 string that carries a UTC offset, or None.

    The API-boundary form of :func:`utc_aware`: every timestamp QT emits should
    go through this, so the offset is never left to the reader to guess.
    """
    aware = utc_aware(dt)
    return aware.isoformat() if aware else None

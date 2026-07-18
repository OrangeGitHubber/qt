"""Data-persistence guard.

Werner lost his config and trade history once because an unraid volume
mapping was inverted, so the app's ``/data`` silently became a Docker
*anonymous* volume (created by ``VOLUME /data`` in the old Dockerfile).
Everything worked until an image refresh recreated the container and
orphaned that volume — keys and DB gone, and the app just showed a fresh
setup wizard as if nothing were wrong.

This module detects, conservatively, whether the resolved data dir is a
real mounted volume or an ephemeral container layer, and surfaces a couple
of related boot-time consistency checks (secrets present but instance key
missing, instance-key age).

Design: the decision is a PURE function (:func:`evaluate_persistence`) so
it can be unit-tested for every branch on any OS with no real filesystem.
The impure wrapper only gathers the raw inputs (stat, /proc, /.dockerenv).

Bias: only ever report ``False`` (not persistent) when we are confident.
Anything ambiguous — local dev, no /proc, can't stat — returns ``None``
("unknown, assume fine, don't warn"). A false alarm here is worse than a
miss because the whole point is that Werner trusts the banner.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("qt.persistence")

DOCKERENV = "/.dockerenv"


def _parse_mount_points(mountinfo: str) -> set[str]:
    """Extract the set of mount points from /proc/self/mountinfo contents.

    Each line's 5th field (index 4) is the mount point. Format:
    ``36 35 98:0 /mnt1 /mnt2 rw,noatime master:1 - ext3 /dev/root rw,errors``
    """
    points: set[str] = set()
    for line in mountinfo.splitlines():
        parts = line.split()
        if len(parts) >= 5:
            points.add(parts[4])
    return points


def evaluate_persistence(
    *,
    in_container: bool,
    data_path: str,
    data_dir_st_dev: int | None,
    root_st_dev: int | None,
    mountinfo: str | None,
) -> tuple[bool | None, str]:
    """Decide whether ``data_path`` is on a persistent mounted volume.

    Returns ``(verdict, reason)`` where verdict is:
      * ``True``  — confident it IS a mounted volume (persistent)
      * ``False`` — confident it is NOT (ephemeral container layer)
      * ``None``  — cannot tell; caller must treat this as "assume fine"

    Pure: every input is passed in, so tests cover all branches on any OS.
    """
    if not in_container:
        return None, "not running in a container — persistence check skipped"

    # Strongest positive signal: the data path is itself a mount point.
    if mountinfo is not None:
        points = _parse_mount_points(mountinfo)
        if data_path in points:
            return True, f"{data_path} is a mounted volume (persistent)"

    if data_dir_st_dev is None or root_st_dev is None:
        return None, "could not stat the data dir or root filesystem"

    if data_dir_st_dev == root_st_dev:
        # Same device as / means the data dir lives on the container's
        # ephemeral overlay/image layer, not on a mounted volume. With
        # `VOLUME /data` removed from the Dockerfile, a missing `-v` lands
        # here — exactly the failure we want to catch loudly.
        return (
            False,
            f"{data_path} is on the container's ephemeral layer "
            "(same device as /) — it is NOT a mounted volume, so data will "
            "be lost when the container is recreated",
        )

    # Different device but not found as a mount point in mountinfo (or no
    # mountinfo): it's a separate filesystem, which is what a bind mount
    # looks like. Treat as persistent.
    return True, f"{data_path} is on a separate device from / — looks persistent"


def _st_dev(path: Path) -> int | None:
    try:
        return os.stat(path).st_dev
    except OSError:
        return None


def _read_mountinfo() -> str | None:
    try:
        return Path("/proc/self/mountinfo").read_text()
    except OSError:
        return None


def data_persistence(data_path: Path) -> tuple[bool | None, str]:
    """Gather raw inputs and evaluate persistence for the given data dir."""
    in_container = os.path.exists(DOCKERENV)
    return evaluate_persistence(
        in_container=in_container,
        data_path=str(data_path),
        data_dir_st_dev=_st_dev(data_path),
        root_st_dev=_st_dev(Path("/")),
        mountinfo=_read_mountinfo(),
    )


# ---------------------------------------------------------------------------
# Boot-time consistency: instance key vs. encrypted secrets in the DB.
# Captured ONCE at startup, before anything can lazily recreate the key.
# ---------------------------------------------------------------------------

_boot: dict = {
    "captured": False,
    "secrets_without_key": False,
    "instance_key_created_at": None,
    "data_persistent": None,
    "data_persistent_reason": "",
}


def capture_boot_state(data_path: Path, secrets_count: int) -> dict:
    """Record boot-time facts. Call once in the app lifespan, right after the
    DB is up but before any secret is decrypted (which would recreate a
    missing key file and hide the mismatch).

    ``secrets_count`` is the number of rows in the secrets table.
    """
    key_file = data_path / "instance.key"
    key_exists = key_file.exists()

    persistent, reason = data_persistence(data_path)

    created_at: str | None = None
    if key_exists:
        try:
            from datetime import datetime, timezone

            created_at = datetime.fromtimestamp(
                key_file.stat().st_mtime, tz=timezone.utc
            ).isoformat()
        except OSError:
            created_at = None

    _boot.update(
        captured=True,
        secrets_without_key=(secrets_count > 0 and not key_exists),
        instance_key_created_at=created_at,
        data_persistent=persistent,
        data_persistent_reason=reason,
    )
    return dict(_boot)


def boot_state() -> dict:
    """The cached boot diagnostics for the status API. Recomputes the live
    persistence verdict each call (cheap) so a status refresh reflects
    reality, but keeps the boot-only ``secrets_without_key`` flag."""
    return dict(_boot)

"""Unit tests for the data-persistence detector.

The decision logic is pure, so every branch is exercised here with
synthetic stat/mountinfo inputs — no real filesystem, runs on any OS.
"""

import pytest

from qt.services import persistence
from qt.services.persistence import evaluate_persistence


@pytest.fixture(autouse=True)
def _reset_boot_state():
    """capture_boot_state mutates module-level cache; keep tests isolated so
    they never leak a stale verdict into the shared /api/status view."""
    snapshot = dict(persistence._boot)
    yield
    persistence._boot.clear()
    persistence._boot.update(snapshot)

# A trimmed but realistic /proc/self/mountinfo. Field index 4 is the mount
# point. The root overlay is device 0:1; /data is a separate bind mount.
MOUNTINFO_WITH_DATA = """\
22 96 0:1 / / rw,relatime - overlay overlay rw
30 22 0:5 / /proc rw,nosuid - proc proc rw
41 22 259:2 /appdata/qt /data rw,relatime - ext4 /dev/nvme0n1p2 rw
"""

MOUNTINFO_NO_DATA = """\
22 96 0:1 / / rw,relatime - overlay overlay rw
30 22 0:5 / /proc rw,nosuid - proc proc rw
"""


def test_not_in_container_is_unknown():
    verdict, reason = evaluate_persistence(
        in_container=False,
        data_path="/data",
        data_dir_st_dev=10,
        root_st_dev=10,
        mountinfo=None,
    )
    assert verdict is None
    assert "container" in reason


def test_mount_point_present_is_persistent():
    verdict, reason = evaluate_persistence(
        in_container=True,
        data_path="/data",
        # Even if the devices happened to match, an explicit mount wins.
        data_dir_st_dev=10,
        root_st_dev=10,
        mountinfo=MOUNTINFO_WITH_DATA,
    )
    assert verdict is True
    assert "persistent" in reason


def test_same_device_no_mount_is_not_persistent():
    verdict, reason = evaluate_persistence(
        in_container=True,
        data_path="/data",
        data_dir_st_dev=42,
        root_st_dev=42,
        mountinfo=MOUNTINFO_NO_DATA,
    )
    assert verdict is False
    assert "NOT a mounted volume" in reason


def test_same_device_no_mountinfo_is_not_persistent():
    # No /proc available but stat says same device as root -> ephemeral layer.
    verdict, reason = evaluate_persistence(
        in_container=True,
        data_path="/data",
        data_dir_st_dev=7,
        root_st_dev=7,
        mountinfo=None,
    )
    assert verdict is False


def test_different_device_without_mountinfo_is_persistent():
    verdict, reason = evaluate_persistence(
        in_container=True,
        data_path="/data",
        data_dir_st_dev=99,
        root_st_dev=1,
        mountinfo=None,
    )
    assert verdict is True
    assert "persistent" in reason


def test_cannot_stat_is_unknown():
    verdict, reason = evaluate_persistence(
        in_container=True,
        data_path="/data",
        data_dir_st_dev=None,
        root_st_dev=1,
        mountinfo=MOUNTINFO_NO_DATA,
    )
    assert verdict is None
    assert "stat" in reason


def test_parse_mount_points_extracts_field_five():
    points = persistence._parse_mount_points(MOUNTINFO_WITH_DATA)
    assert "/data" in points
    assert "/" in points
    assert "/proc" in points


def test_parse_mount_points_ignores_short_lines():
    points = persistence._parse_mount_points("garbage\n22 96 0:1 / /ok rw\n")
    assert points == {"/ok"}


def test_data_persistence_on_dev_machine_is_unknown(tmp_path):
    # This test process is not a container (no /.dockerenv), so the live
    # wrapper must return None regardless of the host filesystem layout —
    # this is the "no false alarm on local dev" guarantee.
    verdict, _ = persistence.data_persistence(tmp_path)
    assert verdict is None


def test_capture_boot_state_flags_secrets_without_key(tmp_path):
    # DB reports secrets but no instance.key on disk -> mismatch flagged.
    state = persistence.capture_boot_state(tmp_path, secrets_count=2)
    assert state["secrets_without_key"] is True
    assert state["instance_key_created_at"] is None


def test_capture_boot_state_ok_when_key_present(tmp_path):
    (tmp_path / "instance.key").write_bytes(b"x")
    state = persistence.capture_boot_state(tmp_path, secrets_count=2)
    assert state["secrets_without_key"] is False
    assert state["instance_key_created_at"] is not None


def test_capture_boot_state_ok_when_no_secrets(tmp_path):
    state = persistence.capture_boot_state(tmp_path, secrets_count=0)
    assert state["secrets_without_key"] is False

# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the ambient temperature aggregator (backend/ambient_temp.py).

Pure logic — no MQTT. Runs under pytest, or standalone:
``python tests/test_ambient_temp.py``.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend.ambient_temp import AmbientTemp  # noqa: E402


def test_empty_snapshot() -> None:
    a = AmbientTemp()
    s = a.snapshot()
    assert s.has_data is False
    assert s.available is False
    assert s.current_c is None and s.min_c is None and s.max_c is None


def test_average_and_extremes() -> None:
    a = AmbientTemp()
    for v in (20.0, 22.0, 24.0):
        assert a.update(v) is True
    s = a.snapshot()
    assert s.has_data is True
    assert s.available is True
    assert abs(s.current_c - 22.0) < 1e-6        # mean of the window
    assert s.min_c == 20.0 and s.max_c == 24.0   # extremes of real values


def test_min_max_track_spikes_not_average() -> None:
    a = AmbientTemp()
    a.update(20.0)
    a.update(35.0)   # brief spike
    a.update(20.0)
    s = a.snapshot()
    assert s.max_c == 35.0                         # spike kept in max
    assert abs(s.current_c - 25.0) < 1e-6          # but average is 25


def test_rejects_out_of_range() -> None:
    a = AmbientTemp()
    assert a.update(999.0) is False
    assert a.update(-60.0) is False
    assert a.update("nan-ish") is False
    assert a.snapshot().has_data is False


def test_offline_status_does_not_hide_fresh_reading() -> None:
    # A stale retained "offline" (a common Arduino LWT, delivered the instant we
    # subscribe) must NOT hide a live reading: availability is based on data
    # freshness, not the status topic.
    a = AmbientTemp()
    a.update(21.0)
    a.set_status("offline")
    s = a.snapshot()
    assert s.available is True
    assert s.current_c == 21.0
    assert s.min_c == 21.0 and s.max_c == 21.0 and s.has_data is True


if __name__ == "__main__":
    test_empty_snapshot()
    test_average_and_extremes()
    test_min_max_track_spikes_not_average()
    test_rejects_out_of_range()
    test_offline_status_does_not_hide_fresh_reading()
    print("ok — ambient_temp tests passed")

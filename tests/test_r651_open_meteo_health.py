from funghi_r6.cli import _open_meteo_health


def _rec(code: str, ts: str) -> dict:
    return {
        "code": code,
        "fetched_at": ts,
        "payload": {},
    }


def test_open_meteo_health_complete_and_reachable_are_explicit():
    cache = {
        "current": {
            "A": _rec("A", "2026-10-02T11:00:00+00:00"),
            "B": _rec("B", "2026-10-02T11:00:01+00:00"),
        },
        "forecast": {
            "A": _rec("A", "2026-10-02T11:00:02+00:00"),
            "B": _rec("B", "2026-10-02T11:00:03+00:00"),
        },
        "soil": {
            "A": _rec("A", "2026-10-02T11:00:04+00:00"),
            "B": _rec("B", "2026-10-02T11:00:05+00:00"),
        },
    }

    health = _open_meteo_health(cache, {}, 2)

    assert health["reachable"] is True
    assert health["acquired"] == 2
    assert health["complete"] is True
    assert health["current"] == 2
    assert health["forecast"] == 2
    assert health["soil"] == 2
    assert health["last_success_at"] == "2026-10-02T11:00:05+00:00"
    assert health["reference_time"] == health["last_success_at"]
    assert health["last_error"] is None
    assert health["errors"] == {}


def test_open_meteo_health_keeps_reachability_separate_from_completeness():
    cache = {
        "current": {
            "A": _rec("A", "2026-10-02T11:00:00+00:00"),
            "B": _rec("B", "2026-10-02T11:00:01+00:00"),
        },
        "forecast": {
            "A": _rec("A", "2026-10-02T11:00:02+00:00"),
            "B": _rec("B", "2026-10-02T11:00:03+00:00"),
        },
        "soil": {
            "A": _rec("A", "2026-10-02T11:00:04+00:00"),
        },
    }
    errors = {"soil": ["batch 1 B..B: timeout"]}

    health = _open_meteo_health(cache, errors, 2)

    assert health["reachable"] is True
    assert health["acquired"] == 1
    assert health["complete"] is False
    assert health["soil"] == 1
    assert "soil:" in health["last_error"]
    assert health["errors"] == {"soil": ["batch 1 B..B: timeout"]}


def test_open_meteo_health_unreachable_when_no_data_exist():
    health = _open_meteo_health(
        {"current": {}, "forecast": {}, "soil": {}},
        {
            "current": ["network down"],
            "forecast": ["network down"],
            "soil": ["network down"],
        },
        418,
    )

    assert health["reachable"] is False
    assert health["acquired"] == 0
    assert health["complete"] is False
    assert health["last_success_at"] is None
    assert health["last_error"] is not None

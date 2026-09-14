from datetime import datetime
from zoneinfo import ZoneInfo

import intraday_outcome_resolver as resolver
import db_manager


ET = ZoneInfo("America/New_York")


def _obs(minute, spot):
    return {"observed_at": f"2026-09-14T10:{minute:02d}:00-04:00", "spot": spot}


def test_evaluate_put_wall_uses_direction_and_path_excursions():
    event = {
        "kind": "put_wall_breach", "detected_at": "2026-09-14T14:15:20+00:00",
        "payload": {"entry_spot": 100.0},
    }

    outcome = resolver.evaluate_signal_path(event, [_obs(15, 100), _obs(30, 98), _obs(45, 101)], 30)

    assert outcome["return_pct"] == 1.0
    assert outcome["directional_success"] is False
    assert outcome["mfe_pct"] == 2.0
    assert outcome["mae_pct"] == -1.0


def test_evaluate_signal_path_waits_for_horizon():
    event = {
        "kind": "call_wall_breach", "detected_at": "2026-09-14T14:15:20+00:00",
        "payload": {"entry_spot": 100.0},
    }

    assert resolver.evaluate_signal_path(event, [_obs(15, 100), _obs(30, 102)], 30) is None


def test_pinning_scores_range_not_direction():
    event = {
        "kind": "pinning_high", "detected_at": "2026-09-14T14:15:00+00:00",
        "payload": {"entry_spot": 100.0},
    }

    outcome = resolver.evaluate_signal_path(event, [_obs(15, 100), _obs(30, 101)], 15)

    assert outcome["directional_success"] is True
    assert outcome["return_pct"] == 1.0


def test_unusual_activity_does_not_invent_direction():
    event = {
        "kind": "unusual_activity", "detected_at": "2026-09-14T14:15:00+00:00",
        "payload": {"entry_spot": 100.0},
    }

    outcome = resolver.evaluate_signal_path(event, [_obs(15, 100), _obs(30, 103)], 15)

    assert outcome["directional_success"] is None
    assert outcome["return_pct"] == 3.0


def test_resolve_available_outcomes_writes_only_reached_horizons(tmp_path):
    db_path = tmp_path / "history.db"
    event_id = db_manager.save_signal_event(
        "TSLA", "2026-09-14T14:15:20+00:00", "2026-09-14", "call_wall_breach",
        "urgent", "urgent", "test", "call", {"entry_spot": 100.0}, db_path=db_path,
    )
    for minute, spot in ((15, 100.0), (30, 102.0)):
        db_manager.save_intraday_observation({
            "symbol": "TSLA", "observed_at": f"2026-09-14T10:{minute:02d}:00-04:00",
            "trading_date": "2026-09-14", "spot": spot,
            "unusual_activity_count": 0, "policy_version": "v1",
        }, db_path=db_path)

    resolved = resolver.resolve_available_outcomes("TSLA", db_path=db_path)

    assert resolved == 1
    rows = db_manager.get_signal_outcomes("TSLA", db_path=db_path)
    assert [(row["event_id"], row["horizon_minutes"]) for row in rows] == [(event_id, 15)]

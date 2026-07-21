from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from stats_bot.coinbase import CoinbaseFeed
from stats_bot.macro_policy import MacroLocalPolicy


ROOT = Path(__file__).resolve().parents[1]
POLICY = (
    ROOT
    / "strategy"
    / "surface_spline_b5x5_p1_hl120_no_session_c01_m01_v1"
    / "strategy_manifest.json"
)
LONDON = ZoneInfo("Europe/London")


def _snapshot(*, ts: datetime, day_type: str, tte: float, distance_bps: float, up_ask: float = 0.45) -> dict[str, object]:
    anchor = 64_000.0
    return {
        "timestamp_london": ts,
        "day_type": day_type,
        "canonical_tte_seconds": tte,
        "elapsed_seconds_since_market_start": 300.0 - tte,
        "cb_close_1s": anchor * (1.0 + distance_bps / 10_000.0),
        "window_open_btc_price": anchor,
        "cb_price_to_beat_anchor": anchor,
        "cb_signed_bps_raw": distance_bps,
        "local_volatility_1h_bps": 35.0,
        "local_volatility_status": "ready",
        "up_bid": up_ask - 0.01,
        "up_ask": up_ask,
        "up_mid": up_ask - 0.005,
        "up_spread": 0.01,
        "down_bid": 1.0 - up_ask - 0.01,
        "down_ask": 1.0 - up_ask,
        "down_mid": 1.0 - up_ask - 0.005,
        "down_spread": 0.01,
    }


def test_production_policy_uses_polymarket_settlement_labels() -> None:
    policy = MacroLocalPolicy(POLICY)
    assert policy.strategy_id == "surface_spline_b5x5_p1_hl120_no_session_c01_m01_v1"
    assert policy.policy_type == "continuous_surface"
    assert policy.signal_source == "coinbase_btc_usd"
    assert policy.signal_feature == "move_dz"
    assert policy.settlement_label == "polymarket_gamma_resolved_outcome"


def test_policy_selects_hl120_for_weekdays_and_weekends() -> None:
    policy = MacroLocalPolicy(POLICY)
    weekday = {"day_type": "weekday"}
    weekend = {"day_type": "weekend"}
    assert policy._branch(weekday)["name"] == "ALL|POOLED"
    assert policy._branch(weekend)["name"] == "ALL|POOLED"
    assert policy.surface_day_type_mode == "excluded"
    assert policy.surface_model.rows > 0


def test_weekend_policy_uses_move_dz_branch() -> None:
    active = MacroLocalPolicy(POLICY)
    for distance_bps in (-14.0, -4.0, 4.0, 14.0):
        for tte in (75.0, 135.0, 195.0, 240.0):
            snapshot = _snapshot(
                ts=datetime(2026, 7, 4, 10, 0, tzinfo=LONDON),
                day_type="weekend",
                tte=tte,
                distance_bps=distance_bps,
            )
            active_decision = active.evaluate(dict(snapshot), start_block_seconds=45)
            assert active._branch(snapshot)["name"] == "ALL|POOLED"
            assert active_decision.policy_record is not None
            assert active_decision.selected_trade_value is not None
            assert active_decision.policy_record.signal_name == "move_dz"
            assert active_decision.policy_record.move_z_bucket == "continuous"


def test_z6_sessions_use_london_clock() -> None:
    policy = MacroLocalPolicy(POLICY)
    assert policy.macro_session(datetime(2026, 7, 1, 5, 59, tzinfo=LONDON)) == ("ASIA_04_08", "h04_08")
    assert policy.macro_session(datetime(2026, 7, 1, 8, 0, tzinfo=LONDON)) == ("EUROPE_08_12", "h08_12")
    assert policy.macro_session(datetime(2026, 7, 1, 12, 0, tzinfo=LONDON)) == ("US_OPEN_12_16", "h12_16")
    assert policy.macro_session(datetime(2026, 7, 1, 20, 0, tzinfo=LONDON)) == ("US_LATE_20_00", "h20_00")


def test_us_late_session_policy_lookup_is_populated() -> None:
    policy = MacroLocalPolicy(POLICY)
    snapshot = _snapshot(
        ts=datetime(2026, 7, 1, 21, 0, tzinfo=LONDON),
        day_type="weekday",
        tte=130.0,
        distance_bps=4.0,
    )
    decision = policy.evaluate(snapshot, start_block_seconds=45)
    assert snapshot["macro_session"] == "US_LATE_20_00"
    assert decision.policy_record is not None
    assert decision.policy_key is not None
    assert not any(str(part).startswith("h") for part in decision.policy_key)
    assert not any(str(part).startswith("tte_") for part in decision.policy_key)


def test_entry_profiles_match_fixed_full_raw_oos_spec() -> None:
    policy = MacroLocalPolicy(POLICY)
    for day_type in ("weekday", "weekend"):
        for session in ("h00_04", "h04_08", "h08_12", "h12_16", "h16_20", "h20_00"):
            continuation = policy.entry_parameters(day_type, session, "continuation")
            mean_reversion = policy.entry_parameters(day_type, session, "mean_reversion")
            assert (continuation.edge, continuation.min_tte, continuation.max_tte) == (0.01, 45, 265)
            assert (mean_reversion.edge, mean_reversion.min_tte, mean_reversion.max_tte) == (0.01, 45, 265)


def test_continuation_tte_blocks_only_outside_fixed_profile() -> None:
    policy = MacroLocalPolicy(POLICY)
    profile = policy.entry_parameters("weekday", "h04_08", "continuation")
    assert profile.min_tte == 45
    assert profile.max_tte == 265


def test_move_z_buckets_are_right_closed_like_research() -> None:
    policy = MacroLocalPolicy(POLICY)
    edges = [0.25, 0.50, 1.00]
    assert policy._move_z_bucket(-1.00, edges) == "z_00"
    assert policy._move_z_bucket(-0.50, edges) == "z_01"
    assert policy._move_z_bucket(0.25, edges) == "z_03"
    assert policy._move_z_bucket(1.01, edges) == "z_06"


def test_local_volatility_uses_only_completed_minutes() -> None:
    feed = CoinbaseFeed()
    now = datetime(2026, 7, 1, 12, 30, 15, tzinfo=ZoneInfo("UTC"))
    current_minute = int(now.timestamp()) // 60 * 60
    with feed._lock:
        for offset in range(61):
            minute = current_minute - (61 - offset) * 60
            feed._minute_closes[minute] = 60_000.0 * math.exp(offset * 0.0001)
        feed._minute_closes[current_minute] = 1.0
    value, status, age = feed.local_volatility_1h_bps(now)
    assert status == "ready"
    assert age == 0
    assert value is not None and math.isclose(value, math.sqrt(60) * 1.0, rel_tol=1e-9)


def test_missing_local_volatility_blocks_entry_cleanly() -> None:
    policy = MacroLocalPolicy(POLICY)
    decision = policy.evaluate(
        {
            "timestamp_london": datetime(2026, 7, 1, 10, 0, tzinfo=LONDON),
            "day_type": "weekday",
            "canonical_tte_seconds": 180.0,
            "elapsed_seconds_since_market_start": 120.0,
            "cb_close_1s": 64_000.0,
            "window_open_btc_price": 64_000.0,
            "cb_signed_bps_raw": 3.0,
            "local_volatility_1h_bps": float("nan"),
            "local_volatility_status": "LOCAL_VOL_WARMUP",
        },
        start_block_seconds=45,
    )
    assert not decision.viable
    assert "LOCAL_VOL_WARMUP" in decision.blockers


def test_policy_match_fields_are_populated_when_edge_fails() -> None:
    policy = MacroLocalPolicy(POLICY)
    snapshot = _snapshot(
        ts=datetime(2026, 7, 1, 7, 0, tzinfo=LONDON),
        day_type="weekday",
        tte=130.0,
        distance_bps=-14.0,
        up_ask=0.24,
    )
    decision = policy.evaluate(snapshot, start_block_seconds=45)
    assert decision.policy_record is not None
    assert decision.selected_trade_value is not None
    assert decision.selected_policy_edge is not None
    assert decision.entry_edge_threshold == 0.01
    assert snapshot["policy_signal_name"] == "move_dz"
    assert snapshot["policy_support"] > 0
    assert snapshot["policy_level"] == "surface"
    assert snapshot["selected_move_dz_bucket"] == snapshot["selected_move_z_bucket"]
    assert snapshot["selected_move_dz_bucket"] == "continuous"
    assert snapshot["selected_policy_stressed_edge"] is not None


def test_surface_policy_requires_edge_to_survive_ask_plus_one_cent() -> None:
    class BarelyPositiveModel:
        rows = 1

        @staticmethod
        def predict_one(*, move_dz: float, selected_ask: float, side: str) -> float:
            return selected_ask + 0.015

    policy = MacroLocalPolicy(POLICY)
    policy.surface_model = BarelyPositiveModel()  # type: ignore[assignment]
    snapshot = _snapshot(
        ts=datetime(2026, 7, 1, 10, 0, tzinfo=LONDON),
        day_type="weekday",
        tte=130.0,
        distance_bps=8.0,
        up_ask=0.45,
    )
    decision = policy.evaluate(snapshot, start_block_seconds=45)
    assert not decision.viable
    assert "POLICY_NO_SIDE_EDGE" in decision.blockers
    assert decision.selected_policy_edge is not None
    assert decision.selected_policy_edge >= 0.01
    assert snapshot["selected_policy_stressed_edge"] < 0.01

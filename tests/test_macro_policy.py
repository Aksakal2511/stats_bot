from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from stats_bot.coinbase import CoinbaseFeed
from stats_bot.macro_policy import MacroLocalPolicy


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "strategy" / "z6_recency45_weekday_v1" / "policy_bundle.json"
LONDON = ZoneInfo("Europe/London")


def test_production_policy_uses_polymarket_settlement_labels() -> None:
    policy = MacroLocalPolicy(POLICY)
    assert policy.strategy_id == "z6_recency45_weekday_v1"
    assert policy.signal_source == "coinbase_btc_usd"
    assert policy.settlement_label == "polymarket_gamma_resolved_outcome"


def test_policy_selects_recency_for_weekdays_and_lifetime_for_weekends() -> None:
    policy = MacroLocalPolicy(POLICY)
    weekday = {"day_type": "weekday"}
    weekend = {"day_type": "weekend"}
    assert policy._branch(weekday)["name"] == "weekday_recency45_s10"
    assert policy._branch(weekend)["name"] == "weekend_lifetime"


def test_weekend_policy_uses_clean_lifetime_branch() -> None:
    active = MacroLocalPolicy(POLICY)
    for signed_bps in (-14.0, -4.0, 4.0, 14.0):
        for tte in (75.0, 135.0, 195.0, 240.0):
            snapshot = {
                "timestamp_london": datetime(2026, 7, 4, 10, 0, tzinfo=LONDON),
                "day_type": "weekend",
                "canonical_tte_seconds": tte,
                "elapsed_seconds_since_market_start": 300.0 - tte,
                "cb_signed_bps_raw": signed_bps,
                "local_volatility_1h_bps": 35.0,
                "local_volatility_status": "ready",
                "up_bid": 0.44,
                "up_ask": 0.45,
                "up_mid": 0.445,
                "up_spread": 0.01,
                "down_bid": 0.54,
                "down_ask": 0.55,
                "down_mid": 0.545,
                "down_spread": 0.01,
            }
            active_decision = active.evaluate(dict(snapshot), start_block_seconds=45)
            assert active._branch(snapshot)["name"] == "weekend_lifetime"
            assert active_decision.policy_record is not None
            assert active_decision.selected_trade_value is not None


def test_z6_sessions_use_london_clock() -> None:
    policy = MacroLocalPolicy(POLICY)
    assert policy.macro_session(datetime(2026, 7, 1, 5, 59, tzinfo=LONDON)) == ("ASIA", "h00_06")
    assert policy.macro_session(datetime(2026, 7, 1, 6, 0, tzinfo=LONDON)) == ("EUROPE", "h06_12")
    assert policy.macro_session(datetime(2026, 7, 1, 12, 0, tzinfo=LONDON)) == ("US_OPEN", "h12_18")
    assert policy.macro_session(datetime(2026, 7, 1, 18, 0, tzinfo=LONDON)) == ("US_LATE", "h18_24")


def test_entry_profiles_match_fixed_clean_ffm_oos_spec() -> None:
    policy = MacroLocalPolicy(POLICY)
    for day_type in ("weekday", "weekend"):
        for session in ("h00_06", "h06_12", "h12_18", "h18_24"):
            continuation = policy.entry_parameters(day_type, session, "continuation")
            mean_reversion = policy.entry_parameters(day_type, session, "mean_reversion")
            assert (continuation.edge, continuation.min_tte, continuation.max_tte) == (0.04, 60, 239)
            assert (mean_reversion.edge, mean_reversion.min_tte, mean_reversion.max_tte) == (0.04, 60, 255)


def test_continuation_tte_blocks_only_outside_fixed_profile() -> None:
    policy = MacroLocalPolicy(POLICY)
    profile = policy.entry_parameters("weekday", "h06_12", "continuation")
    assert profile.min_tte == 60
    assert profile.max_tte == 239


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
    snapshot = {
        "timestamp_london": datetime(2026, 7, 1, 7, 0, tzinfo=LONDON),
        "day_type": "weekday",
        "canonical_tte_seconds": 130.0,
        "elapsed_seconds_since_market_start": 170.0,
        "cb_signed_bps_raw": -14.0,
        "local_volatility_1h_bps": 60.0,
        "local_volatility_status": "ready",
        "up_bid": 0.23,
        "up_ask": 0.24,
        "up_mid": 0.235,
        "up_spread": 0.01,
        "down_bid": 0.50,
        "down_ask": 0.50,
    }
    decision = policy.evaluate(snapshot, start_block_seconds=45)
    assert not decision.viable
    assert "POLICY_NO_SIDE_EDGE" in decision.blockers
    assert decision.policy_record is not None
    assert decision.selected_side == "UP"
    assert decision.selected_trade_value is not None
    assert decision.selected_policy_edge is not None
    assert decision.entry_edge_threshold == 0.04
    assert snapshot["policy_support"] > 0
    assert snapshot["policy_level"] == "final"
    assert snapshot["selected_move_z_bucket"] == "z_00"

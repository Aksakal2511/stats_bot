from __future__ import annotations

import json
import inspect
import math
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from stats_bot.final_strategy import (
    FinalBtc5mStrategy,
    P15PauseGuard,
    selected_twap_dz,
    twap_effective_time_seconds,
)
from stats_bot.features import _canonical_bps_path_features
from stats_bot.bot import StatsBot
from stats_bot.strategy import EntryDecision


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "model_artifacts/btc5m_v1_aug14_sep14/final_strategy.json"
HOLDOUT = Path(
    "/Volumes/Kate_DATA/policy_finder/twap/data/artifacts/gcp_returned/"
    "stage23_locked_holdout/stage23_locked_holdout_sep15_18_btc_20260928/"
    "cache/frozen_holdout_predictions.parquet"
)


def test_twap_dz_dense_equal_weight_geometry() -> None:
    expected = (120.0 - 60.0) + sum(k * k for k in range(1, 60)) / 3600.0
    assert math.isclose(twap_effective_time_seconds(120.0), expected, rel_tol=0.0, abs_tol=1e-12)
    up = selected_twap_dz(1.25, 50.0, 120.0, "UP")
    down = selected_twap_dz(-1.25, 50.0, 120.0, "DOWN")
    assert up is not None and down is not None
    assert math.isclose(up, -down, rel_tol=0.0, abs_tol=1e-12)


def test_canonical_bps_path_ols_and_side_symmetry() -> None:
    start = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    rows = [
        {"timestamp_utc": start + timedelta(seconds=i), "close_1s": 100.0 * (1.0 + i / 10_000.0)}
        for i in range(61)
    ]
    result = _canonical_bps_path_features(rows, start + timedelta(seconds=60), 100.0)
    assert result["up_BPS_15__available"] is True
    assert result["up_BPS_60__available"] is True
    assert math.isclose(float(result["up_BPS_15"]), 15.0, abs_tol=1e-10)
    assert math.isclose(float(result["up_BPS_60"]), 60.0, abs_tol=1e-10)
    assert math.isclose(float(result["up_BPS_ACCEL_5_15"]), 0.0, abs_tol=1e-10)
    assert math.isclose(float(result["down_BPS_60"]), -60.0, abs_tol=1e-10)


def test_frozen_stage23_probability_and_branch_parity(tmp_path: Path) -> None:
    strategy = FinalBtc5mStrategy(ARTIFACT, tmp_path / "state.json")
    frame = pd.read_parquet(HOLDOUT).iloc[::997].reset_index(drop=True)
    raw, p0, final, branches = [], [], [], []
    for row in frame.itertuples(index=False):
        p_raw, _ = strategy._raw_probability(float(row.selected_ask), float(row.TWAP_DZ__VOL_1H))
        style = "continuation" if row.TWAP_DZ__VOL_1H > 0 else "mean_reversion"
        base = strategy._isotonic(p_raw, float(row.selected_ask), style)
        features = {
            "BPS_15": row.BPS_15,
            "BPS_60": row.BPS_60,
            "BPS_ACCEL_5_15": row.BPS_ACCEL_5_15,
            "BPS_15__available": row.BPS_15__available,
            "BPS_60__available": row.BPS_60__available,
            "BPS_ACCEL_5_15__available": row.BPS_ACCEL_5_15__available,
        }
        probability, branch = strategy._residual(base, features)
        raw.append(p_raw)
        p0.append(base)
        final.append(probability)
        branches.append(branch)
    assert np.max(np.abs(np.asarray(raw) - frame.p_raw.to_numpy(float))) <= 5e-12
    assert np.max(np.abs(np.asarray(p0) - frame.p0.to_numpy(float))) <= 2e-9
    assert np.max(np.abs(np.asarray(final) - frame.p_final.to_numpy(float))) <= 2e-9
    assert branches == frame.branch_used.astype(str).tolist()


def test_branch_precedence_and_base_fallback(tmp_path: Path) -> None:
    strategy = FinalBtc5mStrategy(ARTIFACT, tmp_path / "state.json")
    both = {
        "BPS_15": 1.0, "BPS_60": 2.0, "BPS_ACCEL_5_15": 3.0,
        "BPS_15__available": True, "BPS_60__available": True,
        "BPS_ACCEL_5_15__available": True,
    }
    assert strategy._residual(.55, both)[1] == "BPS15_BPS60"
    both["BPS_60__available"] = False
    assert strategy._residual(.55, both)[1] == "BPS15_ACCEL"
    both["BPS_15__available"] = False
    probability, branch = strategy._residual(.55, both)
    assert branch == "BASE_ONLY"
    assert probability == .55


def test_fixed_c25_continuation_only_decision(tmp_path: Path) -> None:
    strategy = FinalBtc5mStrategy(ARTIFACT, tmp_path / "state.json")
    now = datetime(2026, 9, 1, 12, 2, tzinfo=timezone.utc)
    snapshot = {
        "timestamp_utc": now,
        "canonical_window_id": "btc-updown-5m-test",
        "canonical_tte_seconds": 180.0,
        "local_volatility_1h_bps": 50.0,
        "up_raw_move_dz": 5.0,
        "down_raw_move_dz": -5.0,
        "up_bid": .40,
        "up_ask": .41,
        "down_bid": .58,
        "down_ask": .59,
    }
    decision = strategy.evaluate(snapshot)
    assert decision.selected_trade_style == "continuation"
    assert decision.entry_edge_threshold == .025
    assert decision.selected_side == "UP"
    assert snapshot["C_required"] == .025
    assert snapshot["entry_rule_mr_edge"] == 99.0


def test_upper_tte_block_preserves_model_diagnostics_without_detector_observation(tmp_path: Path) -> None:
    strategy = FinalBtc5mStrategy(ARTIFACT, tmp_path / "state.json")
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    snapshot = {
        "timestamp_utc": now,
        "canonical_window_id": "btc-updown-5m-diagnostic",
        "canonical_tte_seconds": 240.0,
        "local_volatility_1h_bps": 50.0,
        "up_raw_move_dz": 5.0,
        "down_raw_move_dz": -5.0,
        "up_bid": .40,
        "up_ask": .41,
        "down_bid": .58,
        "down_ask": .59,
        "up_BPS_15": 1.0,
        "up_BPS_60": 2.0,
        "up_BPS_ACCEL_5_15": .5,
        "up_BPS_15__available": True,
        "up_BPS_60__available": True,
        "up_BPS_ACCEL_5_15__available": True,
        "down_BPS_15": -1.0,
        "down_BPS_60": -2.0,
        "down_BPS_ACCEL_5_15": -.5,
        "down_BPS_15__available": True,
        "down_BPS_60__available": True,
        "down_BPS_ACCEL_5_15__available": True,
    }
    decision = strategy.evaluate(snapshot)
    assert decision.viable is False
    assert decision.blockers == ("TTE_OUT_OF_RANGE",)
    assert decision.selected_side == "UP"
    assert snapshot["TWAP_DZ"] is not None
    assert snapshot["p_base_raw"] is not None
    assert snapshot["p_base_iso"] is not None
    assert snapshot["p_final"] is not None
    assert snapshot["BPS_15"] == 1.0
    assert snapshot["BPS_60"] == 2.0
    assert snapshot["bps_branch"] == "BPS15_BPS60"
    assert not strategy.pause_guard.observations


def test_upper_tte_neutral_state_keeps_signed_twap_diagnostics(tmp_path: Path) -> None:
    strategy = FinalBtc5mStrategy(ARTIFACT, tmp_path / "state.json")
    snapshot = {
        "timestamp_utc": datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
        "canonical_window_id": "btc-updown-5m-neutral",
        "canonical_tte_seconds": 240.0,
        "local_volatility_1h_bps": 50.0,
        "up_raw_move_dz": 0.0,
        "down_raw_move_dz": -0.0,
        "up_bid": .49,
        "up_ask": .50,
        "down_bid": .49,
        "down_ask": .50,
    }
    decision = strategy.evaluate(snapshot)
    assert decision.blockers == ("TTE_OUT_OF_RANGE",)
    assert snapshot["up_TWAP_DZ"] == 0.0
    assert snapshot["down_TWAP_DZ"] == -0.0
    for side in ("up", "down"):
        assert snapshot[f"{side}_p_base_raw"] is not None
        assert snapshot[f"{side}_p_base_iso"] is not None
        assert snapshot[f"{side}_p_final"] is not None
        assert snapshot[f"{side}_model_edge"] is not None
        assert snapshot[f"{side}_diagnostic_style"] == "non_continuation"


def test_p15_transition_no_extension_rearm_and_restart(tmp_path: Path) -> None:
    spec = json.loads(ARTIFACT.read_text())["detector"]
    state_path = tmp_path / "detector.json"
    guard = P15PauseGuard(spec, state_path)
    start = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    for index in range(4):
        guard.observe(start + timedelta(minutes=index), f"m{index}", .030)
    guard.observe(start + timedelta(minutes=5), "m4", .030)
    assert guard.detector_state == "BAD"
    assert guard.last_transition == "ENTER_BAD"
    original_end = guard.pause_until
    assert original_end == start + timedelta(minutes=20)
    guard.observe(start + timedelta(minutes=10), "m5", .030)
    assert guard.pause_until == original_end
    assert guard.detector_state == "BAD"
    assert guard.last_transition == "ENTER_BAD"
    assert guard.last_transition_at == start + timedelta(minutes=5)

    restarted = P15PauseGuard(spec, state_path)
    assert restarted.pause_until == original_end
    assert restarted.detector_state == "BAD"
    assert restarted.last_transition == "ENTER_BAD"
    for minute in (15, 20, 25, 30):
        for index in range(20):
            restarted.observe(start + timedelta(minutes=minute, seconds=index), f"r{minute}-{index}", .060)
    assert restarted.detector_state == "NORMAL"
    assert restarted.last_transition == "EXIT_BAD"
    assert not restarted.is_paused(start + timedelta(minutes=25))

    restarted.observations.clear()
    for index in range(4):
        restarted.observe(start + timedelta(minutes=34, seconds=index), f"n{index}", .020)
    restarted.observe(start + timedelta(minutes=35), "n4", .020)
    assert restarted.detector_state == "BAD"
    assert restarted.last_transition == "ENTER_BAD"
    assert restarted.pause_until == start + timedelta(minutes=50)


def test_p15_counterfactual_count_is_distinct_by_market_and_persists(tmp_path: Path) -> None:
    spec = json.loads(ARTIFACT.read_text())["detector"]
    state_path = tmp_path / "detector.json"
    guard = P15PauseGuard(spec, state_path)

    assert guard.mark_counterfactual("market-a") is True
    assert guard.mark_counterfactual("market-a") is False
    assert guard.mark_counterfactual("market-b") is True
    assert guard.counterfactual_blocked == 2

    restarted = P15PauseGuard(spec, state_path)
    assert restarted.counterfactual_blocked == 2
    assert restarted.counterfactual_markets == {"market-a", "market-b"}
    assert restarted.mark_counterfactual("market-a") is False


def test_artifact_is_development_only_and_mr_disabled() -> None:
    artifact = json.loads(ARTIFACT.read_text())
    assert artifact["training_start"] == "2026-08-14"
    assert artifact["training_end"] == "2026-09-14"
    assert artifact["parity_validation"]["holdout_used_for_fit"] is False
    assert artifact["eligibility"]["mr_enabled"] is False
    assert artifact["eligibility"]["required_edge"] == .025


def test_active_entry_path_excludes_mr_and_legacy_strategy_guards() -> None:
    init_source = inspect.getsource(StatsBot.__init__)
    entry_source = inspect.getsource(StatsBot._entry_decision_with_guard)
    run_source = inspect.getsource(StatsBot.run)
    assert "self.macro_policy = FinalBtc5mStrategy" in init_source
    assert "self.macro_policy.evaluate" in entry_source
    assert "P15_PAUSE" in entry_source
    for legacy_name in (
        "_cheap_low_vol_blocker",
        "_weak_momentum_blocker",
        "_low_liquidity_blocker",
        "_session_style_guard",
        "_apply_twap_lag_guard",
        "_apply_bps_yes_guard",
        "_maybe_run_move_dz_stoploss",
        "_maybe_run_pressure_defense",
    ):
        assert legacy_name not in entry_source
        assert legacy_name not in run_source


def test_startup_window_uses_minimal_snapshot_and_never_evaluates_strategy() -> None:
    now = datetime(2026, 9, 1, 12, 2, tzinfo=timezone.utc)

    class CoinbaseStub:
        @staticmethod
        def local_volatility_1h_bps(_: datetime) -> tuple[float, str, int]:
            return 42.0, "ready", 0

    class PauseGuardStub:
        @staticmethod
        def snapshot_fields(_: datetime) -> dict[str, object]:
            return {"detector_status": "WARMING", "detector_q20": .04, "detector_q30": .05}

    class StrategyStub:
        pause_guard = PauseGuardStub()

        @staticmethod
        def evaluate(*_: object, **__: object) -> EntryDecision:
            raise AssertionError("startup window must not evaluate the strategy")

    bot = object.__new__(StatsBot)
    bot.current_window_ts = 100
    bot.startup_window_ts = 100
    bot.current_market = SimpleNamespace(
        slug="btc-updown-5m-startup",
        market_start_utc=now - timedelta(minutes=2),
        market_end_utc=now + timedelta(minutes=3),
    )
    bot.coinbase = CoinbaseStub()
    bot.config = SimpleNamespace(entry_start_block_seconds=0)
    bot.pm_rows = OrderedDict({1: {"up_bid": .49, "up_ask": .50, "down_bid": .49, "down_ask": .50}})
    bot.macro_policy = StrategyStub()
    bot.last_guard_blocker = None

    snapshot = bot._build_snapshot(now)
    assert snapshot is not None
    assert snapshot["startup_window_warmup"] is True
    assert snapshot["canonical_tte_seconds"] == 180.0
    assert "raw_move_dz" not in snapshot
    assert "up_BPS_15" not in snapshot

    decision = bot._entry_decision_with_guard(now, snapshot)
    assert decision is not None
    assert decision.viable is False
    assert decision.blockers == ("STARTUP_WINDOW_WARMUP",)
    assert snapshot["startup_window_status"] == "WAITING_FOR_NEXT_5M_BOUNDARY"

    resumed = EntryDecision(
        viable=False,
        blockers=("POLICY_NO_SIDE_EDGE",),
        policy_key=None,
        policy_record=None,
        selected_side="UP",
        selected_bid=.49,
        selected_ask=.50,
        selected_mid=.495,
        selected_spread=.01,
        selected_trade_value=.51,
        selected_policy_edge=.01,
        selected_trade_style="continuation",
        entry_edge_threshold=.025,
        entry_session_enabled=True,
        threshold_bps=0.0,
    )

    class ActiveStrategyStub:
        @staticmethod
        def evaluate(*_: object, **__: object) -> EntryDecision:
            return resumed

    bot.current_window_ts = 200
    bot.macro_policy = ActiveStrategyStub()
    assert bot._entry_decision_with_guard(now, {"startup_window_warmup": False}) is resumed

from __future__ import annotations

import threading
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from stats_bot.guards import (
    BPS_YES_CONFLICT_GUARD_START_TTE_SECONDS,
    DrawdownGuard,
    BpsYesGuard,
    SessionStyleDrawdownGuard,
    normalised_5_share_pnl,
)
from stats_bot.bot import (
    PresignLadderState,
    StatsBot,
    _execution_health_text,
    _policy_support_level,
    _submission_fill_attempt_counts,
)
from stats_bot.binance import BinanceTickerFeed
from stats_bot.features import compute_live_snapshot
from stats_bot.models import OpenPosition, PendingEntryOrder
from stats_bot.polymarket import BookSnapshot, MarketInfo, MarketResolution, OrderResult, SignedBuyOrder
from stats_bot.strategy import (
    EntryDecision,
    EntryEdgeThreshold,
    ExitDecision,
    VolatilityEdges,
    bucket_price,
    bucket_signed_bps,
    classify_day_type_and_session,
    entry_threshold_policy,
    evaluate_entry,
    evaluate_exit,
    load_entry_edge_thresholds,
    load_exit_thresholds,
    load_policy_cube,
    trade_style_from_bps_bucket,
)


ROOT = Path(__file__).resolve().parents[1]
POLICY_FILE = ROOT / "strategy" / "policy_cube" / "policy_cube_dual_side_hold_values_offset250_12bucket_2h_cb_sources.jsonl"
ENTRY_THRESHOLDS_FILE = ROOT / "strategy" / "entry_policy" / "offset250_12bucket_2h_cb_sources_hold_value_style_thresholds.csv"
EXIT_THRESHOLDS_FILE = ROOT / "strategy" / "exit_policy" / "offset250_12bucket_2h_hold_exit_thresholds.csv"
LEGACY_POLICY_ARTIFACTS_AVAILABLE = all(
    path.exists()
    for path in (
        POLICY_FILE,
        ENTRY_THRESHOLDS_FILE,
        EXIT_THRESHOLDS_FILE,
        ROOT / "strategy" / "entry_policy" / "final_edge_thresholds.csv",
    )
)
requires_legacy_policy_artifacts = pytest.mark.skipif(
    not LEGACY_POLICY_ARTIFACTS_AVAILABLE,
    reason="legacy policy-cube/threshold artifacts are optional and not shipped with the default macro policy",
)


def test_policy_support_level_handles_missing_snapshot() -> None:
    assert _policy_support_level(None) == ("NA", "NA")
    assert _policy_support_level({"policy_support": 42, "policy_level": "final"}) == (42, "final")


def test_submission_fill_attempt_counts_counts_filled_windows_and_retries() -> None:
    assert _submission_fill_attempt_counts(
        {"window-a": 1, "window-b": 2, "window-c": 0},
        {"window-a": 1, "window-b": 3, "window-c": 1},
    ) == (2, 5)


def test_execution_health_warms_until_five_posts() -> None:
    values = deque([439.0] * 4, maxlen=10)
    assert _execution_health_text(values, values, values).startswith("Warming (4/5 POSTs)")


def test_execution_health_reports_all_three_latency_definitions() -> None:
    client = deque([12.0] * 10, maxlen=10)
    clob = deque([400.0] * 9 + [900.0], maxlen=10)
    path = deque([430.0] * 9 + [930.0], maxlen=10)
    text = _execution_health_text(client, clob, path)
    assert text.startswith("Healthy (10/10 POSTs)")
    assert "client P95 12ms" in text
    assert "CLOB P50/P90 400/400ms" in text
    assert "path P90 430ms" in text


def test_execution_health_uses_ten_post_degraded_and_poor_thresholds() -> None:
    client = deque([12.0] * 10, maxlen=10)
    path = deque([500.0] * 10, maxlen=10)
    degraded = deque([400.0] * 8 + [1100.0, 1100.0], maxlen=10)
    poor = deque([400.0] * 8 + [1600.0, 1600.0], maxlen=10)

    assert _execution_health_text(client, degraded, path).startswith("Degraded")
    assert _execution_health_text(client, poor, path).startswith("Poor")


def _bucket_midpoint(label: str) -> float:
    if not label.startswith("p") or "_to_p" not in label:
        raise AssertionError(f"Unsupported price bucket: {label}")
    left, right = label[1:].split("_to_p", 1)
    return (float(left) + float(right)) / 200.0


def _single_side_record(cube, side: str, entry_edges=None):
    side = side.upper()
    for record in cube.records.values():
        value = record.side_trade_value(side)
        if entry_edges is not None:
            threshold_policy = entry_threshold_policy(
                entry_edges, record.key[0], record.key[1], record.side_trade_style(side)
            )
            if threshold_policy is None or threshold_policy.enabled is False:
                continue
            candidate_ask = value - threshold_policy.threshold - 0.02 if value is not None else None
            ask_bucket = record.key[5 if side == "UP" else 6]
            if candidate_ask is None or bucket_price(candidate_ask) != ask_bucket:
                continue
        if (
            record.side_value_valid(side)
            and value is not None
            and 0.35 <= value <= 0.95
        ):
            return record
    raise AssertionError(f"No suitable {side} policy record found")


def _quote(ask: float) -> tuple[float, float, float, float]:
    ask = round(float(ask), 4)
    bid = round(max(0.01, ask - 0.01), 4)
    mid = round((bid + ask) / 2.0, 4)
    spread = round(ask - bid, 4)
    return bid, ask, mid, spread


def _snapshot_for_record(record, *, up_ask: float, down_ask: float, bps: float = -2.0) -> dict:
    day_type, session, tte_bucket, bps_bucket, vol_bucket, up_bucket, down_bucket = record.key
    up_bid, up_ask, up_mid, up_spread = _quote(up_ask)
    down_bid, down_ask, down_mid, down_spread = _quote(down_ask)
    return {
        "day_type": day_type,
        "session_bucket": session,
        "tte_bucket": tte_bucket,
        "signed_bps_bucket": bps_bucket,
        "volatility_bucket": vol_bucket,
        "up_ask_bucket": up_bucket,
        "down_ask_bucket": down_bucket,
        "canonical_tte_seconds": 250,
        "elapsed_seconds_since_market_start": 50,
        "cb_signed_bps_raw": bps,
        "volatility_smoothed_raw": 1.0,
        "up_bid": up_bid,
        "up_ask": up_ask,
        "up_mid": up_mid,
        "up_spread": up_spread,
        "down_bid": down_bid,
        "down_ask": down_ask,
        "down_mid": down_mid,
        "down_spread": down_spread,
    }


def test_bucket_labels_match_policy_artifacts() -> None:
    assert bucket_signed_bps(-25.0) == "le_neg25"
    assert bucket_signed_bps(-24.9) == "neg25_to_neg20"
    assert bucket_signed_bps(0.0) == "near_zero"
    assert bucket_signed_bps(25.1) == "gt_pos25"
    assert bucket_price(0.15) == "p15_to_p25"
    assert bucket_price(0.85) == "p85_to_p95"
    assert bucket_price(0.9501) is None


def test_trade_style_falls_back_to_signed_bps_bucket() -> None:
    assert trade_style_from_bps_bucket("UP", "pos2p5_to_pos5") == "continuation"
    assert trade_style_from_bps_bucket("DOWN", "pos2p5_to_pos5") == "mean_reversion"
    assert trade_style_from_bps_bucket("UP", "neg5_to_neg2p5") == "mean_reversion"
    assert trade_style_from_bps_bucket("DOWN", "neg5_to_neg2p5") == "continuation"
    assert trade_style_from_bps_bucket("UP", "near_zero") == "neutral"


def test_london_session_map_matches_offset250_12bucket_2h_scheme() -> None:
    expected = {
        0: "ASIA_DEEP_LOW",
        1: "ASIA_DEEP_LOW",
        2: "ASIA_PRE_ACTIVE",
        3: "ASIA_PRE_ACTIVE",
        4: "ASIA_CORE_EARLY",
        5: "ASIA_CORE_EARLY",
        6: "ASIA_EUROPE_HANDOVER",
        7: "ASIA_EUROPE_HANDOVER",
        8: "EUROPE_OPEN",
        9: "EUROPE_OPEN",
        10: "EUROPE_MIDDAY",
        11: "EUROPE_MIDDAY",
        12: "US_PREOPEN",
        13: "US_PREOPEN",
        14: "US_OPEN_EARLY",
        15: "US_OPEN_EARLY",
        16: "US_OPEN_LATE",
        17: "US_OPEN_LATE",
        18: "US_LATE_CORE",
        19: "US_LATE_CORE",
        20: "US_AFTERHOURS_EARLY",
        21: "US_AFTERHOURS_EARLY",
        22: "US_AFTERHOURS_LATE",
        23: "US_AFTERHOURS_LATE",
    }
    for london_hour, session in expected.items():
        timestamp = datetime(2026, 6, 16, london_hour, tzinfo=ZoneInfo("Europe/London")).astimezone(timezone.utc)
        _, actual, london_ts = classify_day_type_and_session(timestamp)
        assert london_ts.hour == london_hour
        assert actual == session


@requires_legacy_policy_artifacts
def test_policy_cube_and_exit_thresholds_load() -> None:
    cube = load_policy_cube(POLICY_FILE)
    thresholds = load_exit_thresholds(EXIT_THRESHOLDS_FILE)
    entry_edges = load_entry_edge_thresholds(ENTRY_THRESHOLDS_FILE)
    old_entry_edges = load_entry_edge_thresholds(ROOT / "strategy" / "entry_policy" / "final_edge_thresholds.csv")
    assert sum(1 for _ in POLICY_FILE.open()) == 12727
    assert len(cube) == 12003
    assert thresholds[("weekday", "US_OPEN_EARLY")] == 2.0
    assert thresholds[("weekend", "US_AFTERHOURS_LATE")] == 2.0
    assert entry_edges[("weekday", "US_OPEN_EARLY", "continuation")] == EntryEdgeThreshold(
        threshold=0.12,
        enabled=True,
        source_bundle="",
        notes="hold_value_style_pair_refinement",
    )
    assert entry_edges[("weekday", "US_OPEN_EARLY", "mean_reversion")] == EntryEdgeThreshold(
        threshold=0.10,
        enabled=True,
        source_bundle="",
        notes="hold_value_style_pair_refinement",
    )
    assert entry_edges[("weekday", "US_OPEN_EARLY", "neutral")].enabled is False
    assert entry_threshold_policy(entry_edges, "weekday", "US_OPEN_EARLY", "continuation").threshold == 0.12
    assert entry_threshold_policy(entry_edges, "weekday", "US_OPEN_EARLY", "unknown") is None
    assert entry_edges[("weekday", "EUROPE_OPEN", "mean_reversion")].threshold == 0.10
    assert entry_edges[("weekend", "US_AFTERHOURS_LATE", "mean_reversion")].threshold == 0.09
    assert old_entry_edges[("weekday", "US_OPEN_EXPANSION")].threshold == 0.21
    assert old_entry_edges[("weekday", "US_OPEN_EXPANSION")].enabled is True


@requires_legacy_policy_artifacts
def test_entry_viability_chooses_best_executable_side_value() -> None:
    cube = load_policy_cube(POLICY_FILE)
    thresholds = load_exit_thresholds(EXIT_THRESHOLDS_FILE)
    entry_edges = load_entry_edge_thresholds(ENTRY_THRESHOLDS_FILE)
    record = _single_side_record(cube, "UP", entry_edges)
    entry_edge = entry_threshold_policy(entry_edges, record.key[0], record.key[1], record.side_trade_style("UP")).threshold
    up_ask = record.side_trade_value("UP") - entry_edge - 0.02
    down_ask = _bucket_midpoint(record.key[6])
    snapshot = _snapshot_for_record(record, up_ask=up_ask, down_ask=down_ask)
    decision = evaluate_entry(
        snapshot,
        cube,
        thresholds,
        entry_edges,
        min_price=0.15,
        max_price=0.85,
        min_tte=60,
        max_tte=255,
        start_block_seconds=45,
    )
    assert decision.viable
    assert decision.selected_side == "UP"
    assert decision.selected_policy_edge is not None
    assert decision.selected_policy_edge > entry_edge
    assert decision.entry_edge_threshold == entry_edge
    assert decision.selected_trade_style == record.side_trade_style("UP")
    assert decision.threshold_bps == thresholds[(record.key[0], record.key[1])]


@requires_legacy_policy_artifacts
def test_exit_logic_is_side_symmetric() -> None:
    cube = load_policy_cube(POLICY_FILE)
    entry_edges = load_entry_edge_thresholds(ENTRY_THRESHOLDS_FILE)
    up_record = _single_side_record(cube, "UP", entry_edges)
    down_record = _single_side_record(cube, "DOWN", entry_edges)
    up_entry_edge = entry_threshold_policy(entry_edges, up_record.key[0], up_record.key[1], up_record.side_trade_style("UP")).threshold
    down_entry_edge = entry_threshold_policy(
        entry_edges, down_record.key[0], down_record.key[1], down_record.side_trade_style("DOWN")
    ).threshold
    up_unsupported = up_record.side_trade_value("UP") - up_entry_edge + 0.02
    down_unsupported = down_record.side_trade_value("DOWN") - down_entry_edge + 0.02
    up_exit = evaluate_exit(
        held_side="UP",
        entry_bps=4.0,
        threshold_bps=2.0,
        entry_edge_threshold=up_entry_edge,
        snapshot=_snapshot_for_record(up_record, up_ask=up_unsupported, down_ask=_bucket_midpoint(up_record.key[6]), bps=1.9),
        policy_cube=cube,
    )
    down_exit = evaluate_exit(
        held_side="DOWN",
        entry_bps=-4.0,
        threshold_bps=2.0,
        entry_edge_threshold=down_entry_edge,
        snapshot=_snapshot_for_record(down_record, up_ask=_bucket_midpoint(down_record.key[5]), down_ask=down_unsupported, bps=-1.9),
        policy_cube=cube,
    )
    assert up_exit.should_exit
    assert down_exit.should_exit
    assert up_exit.signed_bps_materially_against
    assert down_exit.signed_bps_materially_against


@requires_legacy_policy_artifacts
def test_entry_window_blocks_first_45_seconds() -> None:
    cube = load_policy_cube(POLICY_FILE)
    thresholds = load_exit_thresholds(EXIT_THRESHOLDS_FILE)
    entry_edges = load_entry_edge_thresholds(ENTRY_THRESHOLDS_FILE)
    record = _single_side_record(cube, "UP", entry_edges)
    entry_edge = entry_threshold_policy(entry_edges, record.key[0], record.key[1], record.side_trade_style("UP")).threshold
    snapshot = _snapshot_for_record(
        record,
        up_ask=max(0.15, record.side_trade_value("UP") - entry_edge - 0.05),
        down_ask=_bucket_midpoint(record.key[6]),
    )
    snapshot["elapsed_seconds_since_market_start"] = 30
    snapshot["canonical_tte_seconds"] = 270
    decision = evaluate_entry(
        snapshot,
        cube,
        thresholds,
        entry_edges,
        min_price=0.15,
        max_price=0.85,
        min_tte=60,
        max_tte=255,
        start_block_seconds=45,
    )
    assert not decision.viable
    assert decision.entry_session_enabled is True
    assert "WINDOW_START_BLOCK_ACTIVE" in decision.blockers
    assert "TTE_OUTSIDE_ENTRY_RANGE" in decision.blockers


def test_bps_yes_guard_persistence_and_window_lock() -> None:
    guard = BpsYesGuard()
    now = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    snapshot = {"canonical_tte_seconds": 100, "cb_signed_bps_raw": 1.0, "up_mid": 0.44}
    reasons = []
    for offset in range(5):
        active, reason = guard.update_entry_guard(now_utc=now + timedelta(seconds=offset), snapshot=snapshot)
        assert active
        reasons.append(reason)
    assert "BPS_YES_CONFLICT_CONDITION_MET" in reasons
    assert guard.window_blocked
    active, reason = guard.update_entry_guard(now_utc=now + timedelta(seconds=6), snapshot={**snapshot, "up_mid": 0.50})
    assert active
    assert reason == "BPS_YES_CONFLICT_WINDOW_BLOCKED"


def test_bps_yes_guard_uses_fixed_thresholds_and_270s_tte_boundary() -> None:
    guard = BpsYesGuard()
    now = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    base = {"canonical_tte_seconds": 260, "local_volatility_1h_bps": 40.78}

    active, reason = guard.update_entry_guard(
        now_utc=now,
        snapshot={**base, "cb_signed_bps_raw": 0.049, "up_mid": 0.449},
    )
    assert not active
    assert reason is None

    active, reason = guard.update_entry_guard(
        now_utc=now + timedelta(seconds=1),
        snapshot={**base, "cb_signed_bps_raw": 0.051, "up_mid": 0.449},
    )
    assert active
    assert reason == "BPS_YES_CONFLICT_CONDITION_MET"

    guard.reset_window()
    active, reason = guard.update_entry_guard(
        now_utc=now,
        snapshot={
            **base,
            "canonical_tte_seconds": BPS_YES_CONFLICT_GUARD_START_TTE_SECONDS + 0.1,
            "cb_signed_bps_raw": 2.0,
            "up_mid": 0.20,
        },
    )
    assert not active
    assert reason is None


def test_bps_yes_guard_can_be_disabled_in_bot_entry_path() -> None:
    bot = object.__new__(StatsBot)
    bot.config = type(
        "Config",
        (),
        {
            "entry_start_block_seconds": 45,
            "bps_yes_guard_enabled": False,
            "session_style_drawdown_guard_enabled": False,
        },
    )()
    bot.guard = BpsYesGuard()
    bot.peak_drawdown_kill_active = False
    decision = EntryDecision(
        viable=True,
        blockers=(),
        policy_key=("weekday", "z_06", "p40_to_p45", "UP"),
        policy_record=None,
        selected_side="UP",
        selected_bid=0.43,
        selected_ask=0.44,
        selected_mid=0.435,
        selected_spread=0.01,
        selected_trade_value=0.51,
        selected_policy_edge=0.07,
        selected_trade_style="continuation",
        entry_edge_threshold=0.05,
        entry_session_enabled=True,
        threshold_bps=2.0,
    )
    bot.macro_policy = type("MacroPolicy", (), {"evaluate": lambda *_args, **_kwargs: decision})()

    guarded = bot._entry_decision_with_guard(
        datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc),
        {"canonical_tte_seconds": 260, "cb_signed_bps_raw": 1.0, "up_mid": 0.44},
    )

    assert guarded is not None
    assert guarded.viable
    assert not guarded.blockers
    assert bot.last_guard_blocker is None


def test_one_tick_spread_is_valid_maker_quote() -> None:
    bot = object.__new__(StatsBot)
    bot.config = type("Config", (), {"maker_min_spread_ticks": 1})()
    market_start = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    bot.current_market = MarketInfo(
        slug="btc-updown-5m-test",
        market_id="1",
        condition_id="condition",
        question="test",
        token_up="up",
        token_down="down",
        market_start_utc=market_start,
        market_end_utc=market_start + timedelta(seconds=300),
        tick_size=0.01,
    )
    book = BookSnapshot(
        bid=0.52,
        ask=0.53,
        mid=0.525,
        spread=0.01,
        bid_depth_total=10,
        ask_depth_total=10,
        best_bid_depth=5,
        best_ask_depth=5,
        captured_at_utc=market_start,
    )
    bot._side_book = lambda side: book
    price, spread_ticks, _ = bot._maker_quote("UP")
    assert spread_ticks == 1
    assert price == 0.52


def test_presign_ladder_covers_both_sides_at_one_cent_cadence() -> None:
    bot = object.__new__(StatsBot)
    bot.config = type("Config", (), {"min_entry_price": 0.15})()
    market_start = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    market = MarketInfo(
        slug="btc-updown-5m-test",
        market_id="1",
        condition_id="condition",
        question="test",
        token_up="up",
        token_down="down",
        market_start_utc=market_start,
        market_end_utc=market_start + timedelta(seconds=300),
        tick_size=0.01,
    )

    levels = bot._presign_price_levels(market, 0.15, 0.60)

    assert levels[0] == 0.15
    assert levels[-1] == 0.60
    assert len(levels) == 46
    assert len(levels) * 2 == 92


def test_presign_pop_consumes_signed_order_once() -> None:
    bot = object.__new__(StatsBot)
    bot._presign_lock = threading.Lock()
    signed = SignedBuyOrder(token_id="up", price=0.39, shares=5.0, order=object())
    key = bot._presign_order_key("UP", 0.39, 5.0)
    bot.presign_ladder = PresignLadderState(
        market_slug="btc-updown-5m-test",
        orders={key: signed},
    )

    first = bot._presign_pop("btc-updown-5m-test", "UP", 0.39, 5.0)
    second = bot._presign_pop("btc-updown-5m-test", "UP", 0.39, 5.0)

    assert first is signed
    assert second is None


def test_live_presigned_entry_uses_cached_wallet_without_refreshing() -> None:
    bot = object.__new__(StatsBot)
    bot.config = SimpleNamespace(
        paper_trading=False,
        observe_first_window=False,
        live_max_entry_attempts_per_window=2,
        stake_mode="min_order_size",
    )
    market_start = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    bot.current_market = MarketInfo(
        slug="btc-updown-5m-test",
        market_id="1",
        condition_id="condition",
        question="test",
        token_up="up",
        token_down="down",
        market_start_utc=market_start,
        market_end_utc=market_start + timedelta(seconds=300),
        tick_size=0.01,
        min_order_size=5.0,
    )
    bot.current_window_ts = int(market_start.timestamp())
    bot.startup_window_ts = None
    bot.pending_entry = None
    bot.active_position = None
    bot.entry_fills_by_window = {}
    bot.entry_attempts_by_window = {}
    bot.entry_attempt_orders_by_window = {}
    bot.last_entry_attempt_time_by_window = {}
    bot.wallet_balance_live = 42.0
    bot._can_enter_after_cooldown = lambda *args: True
    bot._reconcile_live_wallet_fills_for_window = lambda *args: False
    bot._live_retry_cooldown_remaining = lambda *args: 0.0
    bot._presigned_entries_available = lambda: True
    bot._fast_entry_quote = lambda *args: SimpleNamespace(
        price=0.40,
        shares=5.0,
        target_notional=2.0,
        min_order_size=5.0,
        spread_ticks=2,
        edge_at_ask=0.09,
        edge_at_limit=0.08,
        required_edge=0.05,
        execution_health_status="Healthy",
        tolerance_ticks=1,
    )
    signed = SignedBuyOrder(token_id="up", price=0.40, shares=5.0, order=object())
    bot._presign_pop = lambda *args: signed
    bot._entry_snapshot_with_meta = lambda *args, **kwargs: dict(args[1])
    bot._selected_token_id = lambda side: "up"
    bot._refresh_wallet_balance = lambda force=False: (_ for _ in ()).throw(AssertionError("wallet refresh on fast path"))
    events = []
    bot._log_event = lambda event_type, message, fields: events.append((event_type, message, fields))
    bot.pm = SimpleNamespace(
        post_signed_buy_order=lambda order: SimpleNamespace(
            ok=True,
            order_id="order-1",
            status="LIVE",
            message="",
            matched_shares=0.0,
            price=0.40,
        )
    )
    decision = SimpleNamespace(viable=True, selected_side="UP", policy_key_text="policy")

    bot._place_entry(market_start + timedelta(seconds=60), {"day_type": "weekday"}, decision)

    assert bot.pending_entry is not None
    assert bot.pending_entry.wallet_balance_before == 42.0
    event = next(event for event in events if event[0] == "fast_entry_placed")
    assert event[2]["wallet_balance_source"] == "cached"
    assert event[2]["submit_elapsed_ms"] >= 0
    assert event[2]["entry_path_elapsed_ms"] >= event[2]["submit_elapsed_ms"]


def test_presign_ladder_worker_logs_completion_prices() -> None:
    bot = object.__new__(StatsBot)
    bot._presign_lock = threading.Lock()
    bot._presign_generation = 1
    bot.presign_ladder = PresignLadderState(
        market_slug="btc-updown-5m-test",
        status="running",
        started_at_utc=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc),
        min_price=0.31,
        max_price=0.49,
        total_orders=4,
        generation=1,
    )
    bot._entry_target_size = lambda price, market: (5.0, price * 5.0, "min_order_size")

    class PM:
        def create_signed_buy_order(self, token_id, price, shares, market):
            return SignedBuyOrder(token_id=token_id, price=price, shares=shares, order=object())

    bot.pm = PM()
    events = []
    bot._log_event = lambda *args: events.append(args)
    market_start = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    market = MarketInfo(
        slug="btc-updown-5m-test",
        market_id="1",
        condition_id="condition",
        question="test",
        token_up="up",
        token_down="down",
        market_start_utc=market_start,
        market_end_utc=market_start + timedelta(seconds=300),
        tick_size=0.01,
    )

    bot._presign_ladder_worker(1, market, [0.31, 0.49], market_start)

    assert bot.presign_ladder.status == "complete"
    assert bot.presign_ladder.signed_orders == 4
    assert events[0][0] == "presign_ladder_finished"
    assert events[0][2]["min_price"] == 0.31
    assert events[0][2]["max_price"] == 0.49


def test_fast_presigned_quote_requires_edge_at_signed_limit() -> None:
    bot = object.__new__(StatsBot)
    bot.config = type(
        "Config",
        (),
        {
            "min_entry_price": 0.15,
            "max_entry_price": 0.60,
            "weekend_min_entry_price": 0.15,
            "weekend_max_entry_price": 0.65,
            "stake_mode": "min_order_size",
            "stake_usd": 5.0,
            "min_order_size_fallback": 5.0,
            "dust_share_threshold": 0.01,
        },
    )()
    bot.regime_recovery = type("Recovery", (), {"active_until_utc": None, "forced_block_until_utc": None})()
    market_start = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    bot.current_market = MarketInfo(
        slug="btc-updown-5m-test",
        market_id="1",
        condition_id="condition",
        question="test",
        token_up="up",
        token_down="down",
        market_start_utc=market_start,
        market_end_utc=market_start + timedelta(seconds=300),
        tick_size=0.01,
        min_order_size=5.0,
    )
    book = BookSnapshot(
        bid=0.41,
        ask=0.42,
        mid=0.415,
        spread=0.01,
        bid_depth_total=10,
        ask_depth_total=10,
        best_bid_depth=5,
        best_ask_depth=5,
        captured_at_utc=market_start,
    )
    bot._side_book = lambda side: book
    decision = EntryDecision(
        viable=True,
        blockers=(),
        policy_key=("weekday", "US_PREOPEN", "180-209", "near_zero", "low", "p40_to_p45", "p55_to_p60"),
        policy_record=None,
        selected_side="UP",
        selected_bid=0.41,
        selected_ask=0.42,
        selected_mid=0.415,
        selected_spread=0.01,
        selected_trade_value=0.525,
        selected_policy_edge=0.105,
        selected_trade_style="mean_reversion",
        entry_edge_threshold=0.10,
        entry_session_enabled=True,
        threshold_bps=2.0,
    )

    weak = bot._fast_entry_quote(market_start + timedelta(seconds=60), {"day_type": "weekday"}, decision)
    strong = bot._fast_entry_quote(
        market_start + timedelta(seconds=60),
        {"day_type": "weekday"},
        EntryDecision(**{**decision.__dict__, "selected_trade_value": 0.54, "selected_policy_edge": 0.12}),
    )

    assert weak is None
    assert strong is not None
    assert strong.price == 0.43
    assert round(strong.edge_at_limit, 6) == 0.11


def test_fast_presigned_quote_bumps_required_edge_by_execution_health() -> None:
    bot = object.__new__(StatsBot)
    bot.config = type(
        "Config",
        (),
        {
            "min_entry_price": 0.15,
            "max_entry_price": 0.80,
            "weekend_min_entry_price": 0.15,
            "weekend_max_entry_price": 0.80,
            "stake_mode": "min_order_size",
            "stake_usd": 5.0,
            "min_order_size_fallback": 5.0,
            "dust_share_threshold": 0.01,
        },
    )()
    market_start = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    bot.current_market = MarketInfo(
        slug="btc-updown-5m-test",
        market_id="1",
        condition_id="condition",
        question="test",
        token_up="up",
        token_down="down",
        market_start_utc=market_start,
        market_end_utc=market_start + timedelta(seconds=300),
        tick_size=0.01,
        min_order_size=5.0,
    )
    book = BookSnapshot(
        bid=0.41,
        ask=0.42,
        mid=0.415,
        spread=0.01,
        bid_depth_total=10,
        ask_depth_total=10,
        best_bid_depth=5,
        best_ask_depth=5,
        captured_at_utc=market_start,
    )
    bot._side_book = lambda side: book
    decision = EntryDecision(
        viable=True,
        blockers=(),
        policy_key=("weekday", "US", "180-209", "z_00", "p40_to_p50", "UP"),
        policy_record=None,
        selected_side="UP",
        selected_bid=0.41,
        selected_ask=0.42,
        selected_mid=0.415,
        selected_spread=0.01,
        selected_trade_value=0.50,
        selected_policy_edge=0.08,
        selected_trade_style="continuation",
        entry_edge_threshold=0.05,
        entry_session_enabled=True,
        threshold_bps=2.0,
    )

    bot.recent_clob_ack_ms = deque([500.0, 520.0, 530.0, 540.0, 550.0], maxlen=10)
    degraded = bot._fast_entry_quote(market_start + timedelta(seconds=60), {"day_type": "weekday"}, decision)
    assert degraded is not None
    assert degraded.execution_health_status == "Degraded"
    assert degraded.tolerance_ticks == 1
    assert degraded.price == 0.43
    assert round(degraded.edge_at_ask, 6) == 0.08
    assert round(degraded.edge_at_limit, 6) == 0.07
    assert round(degraded.required_edge, 6) == 0.06

    bot.recent_clob_ack_ms = deque([900.0, 920.0, 930.0, 940.0, 950.0], maxlen=10)
    poor = bot._fast_entry_quote(market_start + timedelta(seconds=60), {"day_type": "weekday"}, decision)
    assert poor is not None
    assert poor.execution_health_status == "Poor"
    assert poor.tolerance_ticks == 1
    assert poor.price == 0.43
    assert round(poor.edge_at_ask, 6) == 0.08
    assert round(poor.edge_at_limit, 6) == 0.07
    assert round(poor.required_edge, 6) == 0.07

    degraded_only = EntryDecision(**{**decision.__dict__, "selected_trade_value": 0.495, "selected_policy_edge": 0.075})
    bot.recent_clob_ack_ms = deque([500.0, 520.0, 530.0, 540.0, 550.0], maxlen=10)
    assert bot._fast_entry_quote(market_start + timedelta(seconds=60), {"day_type": "weekday"}, degraded_only) is not None
    bot.recent_clob_ack_ms = deque([900.0, 920.0, 930.0, 940.0, 950.0], maxlen=10)
    assert bot._fast_entry_quote(market_start + timedelta(seconds=60), {"day_type": "weekday"}, degraded_only) is None


def test_surplus_adaptive_fast_quote_spends_one_tick_only_with_spare_edge() -> None:
    bot = object.__new__(StatsBot)
    bot.config = SimpleNamespace(
        min_entry_price=0.15,
        max_entry_price=0.80,
        weekend_min_entry_price=0.15,
        weekend_max_entry_price=0.80,
        stake_mode="min_order_size",
        stake_usd=5.0,
        min_order_size_fallback=5.0,
        dust_share_threshold=0.01,
        surplus_adaptive_entry_enabled=True,
        surplus_adaptive_min_edge_surplus=0.015,
        surplus_adaptive_step_edge_surplus=0.010,
        surplus_adaptive_extra_ticks=2,
    )
    market_start = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    bot.current_market = MarketInfo(
        slug="btc-updown-5m-test",
        market_id="1",
        condition_id="condition",
        question="test",
        token_up="up",
        token_down="down",
        market_start_utc=market_start,
        market_end_utc=market_start + timedelta(seconds=300),
        tick_size=0.01,
        min_order_size=5.0,
    )
    bot.recent_clob_ack_ms = deque([350.0] * 10, maxlen=10)
    bot._side_book = lambda side: BookSnapshot(
        bid=0.69,
        ask=0.70,
        mid=0.695,
        spread=0.01,
        bid_depth_total=10,
        ask_depth_total=10,
        best_bid_depth=5,
        best_ask_depth=5,
        captured_at_utc=market_start,
    )
    decision = EntryDecision(
        viable=True,
        blockers=(),
        policy_key=("weekday", "US", "180-209", "z_00", "p70_to_p80", "UP"),
        policy_record=None,
        selected_side="UP",
        selected_bid=0.69,
        selected_ask=0.70,
        selected_mid=0.695,
        selected_spread=0.01,
        selected_trade_value=0.77,
        selected_policy_edge=0.07,
        selected_trade_style="continuation",
        entry_edge_threshold=0.04,
        entry_session_enabled=True,
        threshold_bps=2.0,
    )

    quote = bot._fast_entry_quote(market_start + timedelta(seconds=60), {"day_type": "weekday"}, decision)

    assert quote is not None
    assert quote.price == 0.72
    assert quote.tolerance_ticks == 2
    assert round(quote.edge_at_limit, 6) == 0.05
    assert round(quote.required_edge, 6) == 0.04


def test_surplus_adaptive_fast_quote_leaves_low_surplus_at_base_tick() -> None:
    bot = object.__new__(StatsBot)
    bot.config = SimpleNamespace(
        min_entry_price=0.15,
        max_entry_price=0.80,
        weekend_min_entry_price=0.15,
        weekend_max_entry_price=0.80,
        stake_mode="min_order_size",
        stake_usd=5.0,
        min_order_size_fallback=5.0,
        dust_share_threshold=0.01,
        surplus_adaptive_entry_enabled=True,
        surplus_adaptive_min_edge_surplus=0.015,
        surplus_adaptive_step_edge_surplus=0.010,
        surplus_adaptive_extra_ticks=2,
    )
    market_start = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    bot.current_market = MarketInfo(
        slug="btc-updown-5m-test",
        market_id="1",
        condition_id="condition",
        question="test",
        token_up="up",
        token_down="down",
        market_start_utc=market_start,
        market_end_utc=market_start + timedelta(seconds=300),
        tick_size=0.01,
        min_order_size=5.0,
    )
    bot.recent_clob_ack_ms = deque([350.0] * 10, maxlen=10)
    bot._side_book = lambda side: BookSnapshot(
        bid=0.69,
        ask=0.70,
        mid=0.695,
        spread=0.01,
        bid_depth_total=10,
        ask_depth_total=10,
        best_bid_depth=5,
        best_ask_depth=5,
        captured_at_utc=market_start,
    )
    decision = EntryDecision(
        viable=True,
        blockers=(),
        policy_key=("weekday", "US", "180-209", "z_00", "p70_to_p80", "UP"),
        policy_record=None,
        selected_side="UP",
        selected_bid=0.69,
        selected_ask=0.70,
        selected_mid=0.695,
        selected_spread=0.01,
        selected_trade_value=0.76,
        selected_policy_edge=0.06,
        selected_trade_style="continuation",
        entry_edge_threshold=0.04,
        entry_session_enabled=True,
        threshold_bps=2.0,
    )

    quote = bot._fast_entry_quote(market_start + timedelta(seconds=60), {"day_type": "weekday"}, decision)

    assert quote is not None
    assert quote.price == 0.71
    assert quote.tolerance_ticks == 1
    assert round(quote.edge_at_limit, 6) == 0.05


def test_surplus_adaptive_fast_quote_caps_high_surplus_at_two_extra_ticks() -> None:
    bot = object.__new__(StatsBot)
    bot.config = SimpleNamespace(
        min_entry_price=0.15,
        max_entry_price=0.80,
        weekend_min_entry_price=0.15,
        weekend_max_entry_price=0.80,
        stake_mode="min_order_size",
        stake_usd=5.0,
        min_order_size_fallback=5.0,
        dust_share_threshold=0.01,
        surplus_adaptive_entry_enabled=True,
        surplus_adaptive_min_edge_surplus=0.015,
        surplus_adaptive_step_edge_surplus=0.010,
        surplus_adaptive_extra_ticks=2,
    )
    market_start = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    bot.current_market = MarketInfo(
        slug="btc-updown-5m-test",
        market_id="1",
        condition_id="condition",
        question="test",
        token_up="up",
        token_down="down",
        market_start_utc=market_start,
        market_end_utc=market_start + timedelta(seconds=300),
        tick_size=0.01,
        min_order_size=5.0,
    )
    bot.recent_clob_ack_ms = deque([350.0] * 10, maxlen=10)
    bot._side_book = lambda side: BookSnapshot(
        bid=0.69,
        ask=0.70,
        mid=0.695,
        spread=0.01,
        bid_depth_total=10,
        ask_depth_total=10,
        best_bid_depth=5,
        best_ask_depth=5,
        captured_at_utc=market_start,
    )
    decision = EntryDecision(
        viable=True,
        blockers=(),
        policy_key=("weekday", "US", "180-209", "z_00", "p70_to_p80", "UP"),
        policy_record=None,
        selected_side="UP",
        selected_bid=0.69,
        selected_ask=0.70,
        selected_mid=0.695,
        selected_spread=0.01,
        selected_trade_value=0.79,
        selected_policy_edge=0.09,
        selected_trade_style="continuation",
        entry_edge_threshold=0.04,
        entry_session_enabled=True,
        threshold_bps=2.0,
    )

    quote = bot._fast_entry_quote(market_start + timedelta(seconds=60), {"day_type": "weekday"}, decision)

    assert quote is not None
    assert quote.price == 0.73
    assert quote.tolerance_ticks == 3
    assert round(quote.edge_at_limit, 6) == 0.06
    assert round(quote.required_edge, 6) == 0.04


def _pressure_defence_bot(*, result: OrderResult, paper_trading: bool = False, order_progress: list[object | None] | None = None):
    bot = object.__new__(StatsBot)
    market_start = datetime(2026, 7, 15, 18, 5, tzinfo=timezone.utc)
    bot.config = SimpleNamespace(
        pressure_defence_enabled=True,
        pressure_arm_tte_seconds=60,
        pressure_trigger_tte_seconds=40,
        pressure_max_tte_seconds=60,
        pressure_min_held_mid=0.75,
        pressure_max_held_mid=1.00,
        pressure_z_threshold=2.0,
        pressure_signed_volume_lookback_seconds=30,
        pressure_adverse_volume_btc=8.0,
        pressure_max_hedge_ask=0.99,
        pressure_hedge_fraction=1.0,
        pressure_hedge_limit_price=0.99,
        dust_share_threshold=0.01,
        paper_trading=paper_trading,
    )
    bot.current_market = MarketInfo(
        slug="btc-updown-5m-test",
        market_id="1",
        condition_id="condition",
        question="test",
        token_up="up",
        token_down="down",
        market_start_utc=market_start,
        market_end_utc=market_start + timedelta(seconds=300),
        tick_size=0.01,
        min_order_size=5.0,
    )
    bot.active_position = OpenPosition(
        token_id="up",
        market_slug=bot.current_market.slug,
        market_id=bot.current_market.market_id,
        market_question=bot.current_market.question,
        market_end_utc=bot.current_market.market_end_utc,
        held_side="UP",
        shares=5.0,
        avg_entry_price=0.76,
        entry_cost=3.80,
        entry_time_utc=market_start + timedelta(seconds=180),
        entry_bps=4.0,
        entry_day_type="weekday",
        entry_session_bucket="US_OPEN_LATE",
        entry_edge_threshold=0.04,
        entry_bps_threshold=2.0,
        entry_policy_key_text="policy",
        entry_snapshot={"entry_trade_style": "continuation"},
    )
    bot.balance = 100.0
    bot.wallet_balance_live = 100.0
    bot.pressure_hedges_submitted = 0
    bot.pressure_hedges_filled = 0
    bot.pressure_hedges_blocked = 0
    bot.last_pressure_defence = None
    bot._ffm_signed_volume_pressure = lambda held_side: {
        "pressure_source": "binance_spot_signed_volume",
        "pressure_adverse_volume_btc": 8.5,
        "pressure_signed_volume_btc": -8.5,
        "pressure_volume_threshold_btc": 8.0,
    }
    bot._side_book = lambda side: BookSnapshot(
        bid=0.29,
        ask=0.30,
        mid=0.295,
        spread=0.01,
        bid_depth_total=10.0,
        ask_depth_total=10.0,
        best_bid_depth=5.0,
        best_ask_depth=5.0,
        captured_at_utc=market_start,
    )
    bot._selected_token_id = lambda side: "down" if side == "DOWN" else "up"
    bot.recent_events = []
    bot._log_event = lambda event_type, message, fields: bot.recent_events.append((event_type, message, fields))

    class FakePolymarket:
        def __init__(self) -> None:
            self.post_count = 0
            self.order_progress = list(order_progress or [])

        def create_signed_buy_order(self, token_id, price, shares, market):
            return SignedBuyOrder(token_id=token_id, price=price, shares=shares, order=object())

        def post_signed_buy_order(self, signed_order):
            self.post_count += 1
            return result

        def get_order(self, order_id):
            if not self.order_progress:
                return None
            return self.order_progress.pop(0)

    bot.pm = FakePolymarket()
    snapshot = {"up_mid": 0.80, "canonical_tte_seconds": 40}
    return bot, market_start, snapshot


def test_pressure_defence_live_accepted_without_match_reconciles_later_fill() -> None:
    result = OrderResult(True, "hedge-1", "LIVE", matched_shares=0.0, price=0.30)
    bot, now, snapshot = _pressure_defence_bot(
        result=result,
        order_progress=[{"status": "filled", "size_matched": 5.0, "price": 0.41}],
    )

    bot._maybe_execute_pressure_defence(now, snapshot)
    bot._maybe_execute_pressure_defence(now + timedelta(seconds=1), snapshot)

    assert bot.pm.post_count == 1
    assert bot.pressure_hedges_submitted == 1
    assert bot.pressure_hedges_filled == 1
    assert bot.active_position is not None
    assert bot.active_position.hedge_leg is not None
    assert bot.active_position.hedge_leg.avg_price == 0.41
    assert bot.active_position.pressure_defence_status == "filled"
    assert bot.active_position.extra["pressure_defence_attempted"] is True
    assert bot.last_pressure_defence["status"] == "filled"


def test_pressure_defence_live_accepted_without_match_never_submits_second_order() -> None:
    result = OrderResult(True, "hedge-1", "LIVE", matched_shares=0.0, price=0.30)
    bot, now, snapshot = _pressure_defence_bot(result=result)

    for offset in range(5):
        bot._maybe_execute_pressure_defence(now + timedelta(seconds=offset), snapshot)

    assert bot.pm.post_count == 1
    assert bot.pressure_hedges_submitted == 1
    assert bot.pressure_hedges_filled == 0
    assert bot.active_position is not None
    assert bot.active_position.hedge_leg is None
    assert bot.active_position.pressure_defence_status == "submitted"
    assert bot.active_position.extra["pressure_defence_attempted"] is True


def test_pressure_defence_live_immediate_fill_does_not_repeat() -> None:
    result = OrderResult(True, "hedge-1", "MATCHED", matched_shares=5.0, price=0.30)
    bot, now, snapshot = _pressure_defence_bot(result=result)

    bot._maybe_execute_pressure_defence(now, snapshot)
    bot._maybe_execute_pressure_defence(now + timedelta(seconds=1), snapshot)

    assert bot.pm.post_count == 1
    assert bot.pressure_hedges_submitted == 1
    assert bot.pressure_hedges_filled == 1
    assert bot.active_position is not None
    assert bot.active_position.hedge_leg is not None
    assert bot.active_position.hedge_leg.side == "DOWN"
    assert bot.active_position.hedge_leg.shares == 5.0
    assert bot.active_position.pressure_defence_status == "filled"


def test_pressure_defence_limit_echo_accounts_at_trigger_ask() -> None:
    result = OrderResult(True, "hedge-1", "MATCHED", matched_shares=5.0, price=0.99, price_source="price")
    bot, now, snapshot = _pressure_defence_bot(result=result)

    bot._maybe_execute_pressure_defence(now, snapshot)

    assert bot.pressure_hedges_submitted == 1
    assert bot.pressure_hedges_filled == 1
    assert bot.active_position is not None
    assert bot.active_position.hedge_leg is not None
    assert bot.active_position.hedge_leg.avg_price == 0.30
    assert bot.active_position.hedge_leg.cost == 1.50
    assert bot.last_pressure_defence["hedge_price"] == 0.30
    assert bot.last_pressure_defence["hedge_price_source"] == "hedge_ask_at_trigger_limit_echo"
    submitted = [fields for event, _message, fields in bot.recent_events if event == "pressure_hedge_submitted"][-1]
    filled = [fields for event, _message, fields in bot.recent_events if event == "pressure_hedge_fill_reconciled"][-1]
    assert submitted["fill_price"] == 0.30
    assert submitted["fill_price_source"] == "hedge_ask_at_trigger_limit_echo"
    assert filled["fill_price"] == 0.30
    assert filled["fill_price_source"] == "hedge_ask_at_trigger_limit_echo"


def test_pressure_defence_reconcile_prefers_avg_price_over_limit_echo() -> None:
    result = OrderResult(True, "hedge-1", "LIVE", matched_shares=0.0, price=0.99, price_source="price")
    bot, now, snapshot = _pressure_defence_bot(
        result=result,
        order_progress=[{"status": "filled", "size_matched": 5.0, "price": 0.99, "avgPrice": 0.37}],
    )

    bot._maybe_execute_pressure_defence(now, snapshot)
    bot._maybe_execute_pressure_defence(now + timedelta(seconds=1), snapshot)

    assert bot.pressure_hedges_submitted == 1
    assert bot.pressure_hedges_filled == 1
    assert bot.active_position is not None
    assert bot.active_position.hedge_leg is not None
    assert bot.active_position.hedge_leg.avg_price == 0.37
    assert bot.last_pressure_defence["hedge_price"] == 0.37
    assert bot.last_pressure_defence["hedge_price_source"] == "avgPrice"


def test_pressure_defence_partial_fill_keeps_reconciling_without_second_order() -> None:
    result = OrderResult(True, "hedge-1", "LIVE", matched_shares=0.0, price=0.30)
    bot, now, snapshot = _pressure_defence_bot(
        result=result,
        order_progress=[
            {"status": "live", "size_matched": 2.0, "price": 0.35},
            {"status": "filled", "size_matched": 5.0, "price": 0.40},
        ],
    )

    bot._maybe_execute_pressure_defence(now, snapshot)
    bot._maybe_execute_pressure_defence(now + timedelta(seconds=1), snapshot)
    bot._maybe_execute_pressure_defence(now + timedelta(seconds=2), snapshot)

    assert bot.pm.post_count == 1
    assert bot.pressure_hedges_submitted == 1
    assert bot.pressure_hedges_filled == 1
    assert bot.active_position is not None
    assert bot.active_position.hedge_leg is not None
    assert bot.active_position.hedge_leg.shares == 5.0
    assert round(bot.active_position.hedge_leg.avg_price, 6) == 0.38
    assert bot.active_position.pressure_defence_status == "filled"


def test_pressure_defence_mean_reversion_position_can_trigger() -> None:
    result = OrderResult(True, "hedge-1", "MATCHED", matched_shares=5.0, price=0.30)
    bot, now, snapshot = _pressure_defence_bot(result=result)
    assert bot.active_position is not None
    bot.active_position.entry_snapshot["entry_trade_style"] = "mean_reversion"

    bot._maybe_execute_pressure_defence(now, snapshot)

    assert bot.pm.post_count == 1
    assert bot.active_position.hedge_leg is not None
    assert bot.active_position.pressure_defence_status == "filled"


def test_pressure_defence_requires_signed_volume_threshold() -> None:
    result = OrderResult(True, "hedge-1", "MATCHED", matched_shares=5.0, price=0.30)
    bot, now, snapshot = _pressure_defence_bot(result=result)
    bot._ffm_signed_volume_pressure = lambda held_side: {
        "pressure_source": "binance_spot_signed_volume",
        "pressure_adverse_volume_btc": 7.99,
        "pressure_signed_volume_btc": -7.99,
        "pressure_volume_threshold_btc": 8.0,
    }

    bot._maybe_execute_pressure_defence(now, snapshot)

    assert bot.pm.post_count == 0
    assert bot.active_position is not None
    assert bot.active_position.hedge_leg is None
    assert bot.active_position.pressure_defence_status == "armed"


def test_pressure_defence_replays_0930_manipulation_trigger_shape() -> None:
    result = OrderResult(True, "hedge-1", "MATCHED", matched_shares=5.0, price=0.23)
    bot, now, snapshot = _pressure_defence_bot(result=result)
    assert bot.active_position is not None
    bot.config.pressure_min_held_mid = 0.75
    bot.config.pressure_trigger_tte_seconds = 60
    bot.config.pressure_signed_volume_lookback_seconds = 40
    bot.config.pressure_adverse_volume_btc = 5.0
    bot.active_position.avg_entry_price = 0.37
    bot.binance_spot = BinanceTickerFeed(name="binance_spot", url="wss://example.invalid")
    bot._ffm_signed_volume_pressure = StatsBot._ffm_signed_volume_pressure.__get__(bot, StatsBot)

    trigger_second = int((now + timedelta(seconds=267)).timestamp())
    for second in range(trigger_second - 39, trigger_second + 1):
        bot.binance_spot.upsert_second_bar(second, 64242.0, 0.0, signed_size=0.0)
    bot.binance_spot.upsert_second_bar(trigger_second - 39, 64242.0, 0.00001, signed_size=0.00001)
    bot.binance_spot.upsert_second_bar(trigger_second, 64242.0, 17.42001, signed_size=-17.42001)
    bot._side_book = lambda side: BookSnapshot(
        bid=0.22 if side == "DOWN" else 0.77,
        ask=0.23 if side == "DOWN" else 0.78,
        mid=0.225 if side == "DOWN" else 0.775,
        spread=0.01,
        bid_depth_total=10.0,
        ask_depth_total=10.0,
        best_bid_depth=5.0,
        best_ask_depth=5.0,
        captured_at_utc=now,
    )
    snapshot = {
        "up_mid": 0.775,
        "down_mid": 0.225,
        "canonical_tte_seconds": 32.994,
    }

    bot._maybe_execute_pressure_defence(now + timedelta(seconds=267), snapshot)
    bot._maybe_execute_pressure_defence(now + timedelta(seconds=268), snapshot)

    assert bot.pm.post_count == 1
    assert bot.pressure_hedges_submitted == 1
    assert bot.pressure_hedges_filled == 1
    assert bot.active_position.hedge_leg is not None
    assert bot.active_position.hedge_leg.side == "DOWN"
    assert bot.active_position.hedge_leg.avg_price == 0.23
    assert bot.active_position.extra["pressure_defence_attempted"] is True
    assert round(bot.last_pressure_defence["held_mid"], 3) == 0.775
    assert round(bot.last_pressure_defence["pressure_signed_volume_btc"], 5) == -17.42
    assert round(bot.last_pressure_defence["pressure_adverse_volume_btc"], 5) == 17.42
    assert bot.last_pressure_defence["pressure_volume_lookback_seconds"] == 40
    assert bot.last_pressure_defence["pressure_volume_threshold_btc"] == 5.0


def test_pressure_defence_waiting_tte_refreshes_detail() -> None:
    result = OrderResult(True, "hedge-1", "MATCHED", matched_shares=5.0, price=0.30)
    bot, now, snapshot = _pressure_defence_bot(result=result)
    snapshot = {**snapshot, "canonical_tte_seconds": 50}

    bot._maybe_execute_pressure_defence(now, snapshot)

    assert bot.pm.post_count == 0
    assert bot.active_position is not None
    assert bot.active_position.pressure_defence_status == "armed"
    assert bot.active_position.pressure_defence_reason == "WAITING_FOR_TRIGGER_TTE"
    assert bot.last_pressure_defence is not None
    assert bot.last_pressure_defence["status"] == "armed"
    assert bot.last_pressure_defence["held_mid"] == 0.80
    assert bot.last_pressure_defence["pressure_adverse_volume_btc"] == 8.5


def test_pressure_defence_outside_held_band_records_signed_flow_detail() -> None:
    result = OrderResult(True, "hedge-1", "MATCHED", matched_shares=5.0, price=0.30)
    bot, now, snapshot = _pressure_defence_bot(result=result)
    snapshot = {**snapshot, "up_mid": 0.30, "canonical_tte_seconds": 10}

    bot._maybe_execute_pressure_defence(now, snapshot)

    assert bot.pm.post_count == 0
    assert bot.active_position is not None
    assert bot.active_position.pressure_defence_status == "blocked"
    assert bot.active_position.pressure_defence_reason == "HELD_MID_OUTSIDE_PRESSURE_BAND"
    assert bot.last_pressure_defence is not None
    assert bot.last_pressure_defence["status"] == "blocked"
    assert bot.last_pressure_defence["pressure_signed_volume_btc"] == -8.5


def test_paper_fast_limit_entry_fills_current_ask_after_limit_edge_check() -> None:
    bot = object.__new__(StatsBot)
    bot.config = type(
        "Config",
        (),
        {
            "paper_trading": True,
            "paper_fill_model": "fast_limit",
            "observe_first_window": False,
            "cooldown_seconds": 10,
            "min_entry_price": 0.15,
            "max_entry_price": 0.80,
            "weekend_min_entry_price": 0.15,
            "weekend_max_entry_price": 0.80,
            "stake_mode": "min_order_size",
            "stake_usd": 5.0,
            "min_order_size_fallback": 5.0,
            "dust_share_threshold": 0.01,
        },
    )()
    market_start = datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc)
    bot.current_market = MarketInfo(
        slug="btc-updown-5m-test",
        market_id="1",
        condition_id="condition",
        question="test",
        token_up="up",
        token_down="down",
        market_start_utc=market_start,
        market_end_utc=market_start + timedelta(seconds=300),
        tick_size=0.01,
        min_order_size=5.0,
    )
    bot.current_window_ts = market_start
    bot.startup_window_ts = None
    bot.pending_entry = None
    bot.active_position = None
    bot.balance = 100.0
    bot.wallet_balance_live = None
    bot.entry_fills_by_window = {}
    bot.last_exit_time_by_window = {}
    bot.recent_events = []
    bot.window_errors = []
    bot.regime_recovery = type("Recovery", (), {"active_until_utc": None, "forced_block_until_utc": None})()
    bot._log_event = lambda event_type, message, fields: bot.recent_events.append((event_type, message, fields))
    book = BookSnapshot(
        bid=0.41,
        ask=0.42,
        mid=0.415,
        spread=0.01,
        bid_depth_total=10,
        ask_depth_total=10,
        best_bid_depth=5,
        best_ask_depth=5,
        captured_at_utc=market_start,
    )
    bot._side_book = lambda side: book
    decision = EntryDecision(
        viable=True,
        blockers=(),
        policy_key=("weekday", "US_PREOPEN", "180-209", "near_zero", "low", "p40_to_p45", "p55_to_p60"),
        policy_record=None,
        selected_side="UP",
        selected_bid=0.41,
        selected_ask=0.42,
        selected_mid=0.415,
        selected_spread=0.01,
        selected_trade_value=0.54,
        selected_policy_edge=0.12,
        selected_trade_style="mean_reversion",
        entry_edge_threshold=0.10,
        entry_session_enabled=True,
        threshold_bps=2.0,
    )
    snapshot = {
        "day_type": "weekday",
        "session_bucket": "US_PREOPEN",
        "cb_signed_bps_raw": 0.5,
    }

    bot._place_entry(market_start + timedelta(seconds=60), snapshot, decision)

    assert bot.pending_entry is None
    assert bot.active_position is not None
    assert bot.active_position.avg_entry_price == 0.42
    assert bot.active_position.shares == 5.0
    assert bot.balance == 97.9
    fast_event = next(event for event in bot.recent_events if event[0] == "fast_entry_placed")
    assert fast_event[2]["limit_price"] == 0.43
    assert fast_event[2]["fill_price"] == 0.42
    assert bot.active_position.entry_snapshot["entry_execution_mode"] == "presigned_fast_limit"
    assert round(bot.active_position.entry_snapshot["entry_edge_at_limit"], 6) == 0.11


def test_min_order_size_entry_sizing_uses_market_contract_count() -> None:
    bot = object.__new__(StatsBot)
    bot.config = type(
        "Config",
        (),
        {
            "stake_mode": "min_order_size",
            "stake_usd": 5.0,
            "min_order_size_fallback": 5.0,
        },
    )()
    market_start = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    market = MarketInfo(
        slug="btc-updown-5m-test",
        market_id="1",
        condition_id="condition",
        question="test",
        token_up="up",
        token_down="down",
        market_start_utc=market_start,
        market_end_utc=market_start + timedelta(seconds=300),
        tick_size=0.01,
        min_order_size=5.0,
    )
    shares, notional, min_order_size = bot._entry_target_size(price=0.30, market=market)
    assert shares == 5.0
    assert notional == 1.5
    assert min_order_size == 5.0


def test_weekend_entry_uses_day_specific_entry_price_bounds() -> None:
    bot = object.__new__(StatsBot)
    bot.config = type(
        "Config",
        (),
        {
            "min_entry_price": 0.33,
            "max_entry_price": 0.56,
            "weekend_min_entry_price": 0.31,
            "weekend_max_entry_price": 0.49,
        },
    )()

    assert bot._entry_price_bounds_for_snapshot({"day_type": "weekday"}) == (0.33, 0.56)
    assert bot._entry_price_bounds_for_snapshot({"day_type": "weekend"}) == (0.31, 0.49)
    assert bot._entry_price_bounds_for_snapshot(None) == (0.33, 0.56)
    assert bot._max_entry_price_for_snapshot({"day_type": "weekend"}) == 0.49


def test_fixed_usd_entry_sizing_still_uses_configured_stake() -> None:
    bot = object.__new__(StatsBot)
    bot.config = type(
        "Config",
        (),
        {
            "stake_mode": "fixed_usd",
            "stake_usd": 5.0,
            "min_order_size_fallback": 5.0,
        },
    )()
    market_start = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    market = MarketInfo(
        slug="btc-updown-5m-test",
        market_id="1",
        condition_id="condition",
        question="test",
        token_up="up",
        token_down="down",
        market_start_utc=market_start,
        market_end_utc=market_start + timedelta(seconds=300),
        tick_size=0.01,
        min_order_size=5.0,
    )
    shares, notional, min_order_size = bot._entry_target_size(price=0.50, market=market)
    assert shares == 10.0
    assert notional == 5.0
    assert min_order_size == 5.0


def test_dynamic_entry_sizing_uses_bankroll_share_tiers() -> None:
    bot = object.__new__(StatsBot)
    bot.config = type(
        "Config",
        (),
        {
            "paper_trading": True,
            "stake_mode": "dynamic_shares",
            "stake_usd": 5.0,
            "min_order_size_fallback": 5.0,
        },
    )()
    bot.drawdown_guard = DrawdownGuard()
    bot.wallet_balance_live = None
    market_start = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    market = MarketInfo(
        slug="btc-updown-5m-test",
        market_id="1",
        condition_id="condition",
        question="test",
        token_up="up",
        token_down="down",
        market_start_utc=market_start,
        market_end_utc=market_start + timedelta(seconds=300),
        tick_size=0.01,
        min_order_size=5.0,
    )

    expected = [
        (49.99, 5.0),
        (50.0, 6.0),
        (60.0, 7.0),
        (70.0, 8.0),
        (84.99, 8.0),
        (85.0, 9.0),
        (100.0, 10.0),
        (125.0, 12.0),
        (150.0, 15.0),
    ]
    for balance, target_shares in expected:
        bot.balance = balance
        shares, notional, min_order_size = bot._entry_target_size(price=0.50, market=market)
        assert shares == target_shares
        assert notional == round(target_shares * 0.50, 6)
        assert min_order_size == 5.0


def test_ladder_entry_sizing_initializes_tier_and_uses_downgrade_buffer() -> None:
    bot = object.__new__(StatsBot)
    bot.config = type(
        "Config",
        (),
        {
            "paper_trading": True,
            "stake_mode": "ladder_shares",
            "stake_usd": 5.0,
            "min_order_size_fallback": 5.0,
        },
    )()
    bot.wallet_balance_live = None
    market_start = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    market = MarketInfo(
        slug="btc-updown-5m-test",
        market_id="1",
        condition_id="condition",
        question="test",
        token_up="up",
        token_down="down",
        market_start_utc=market_start,
        market_end_utc=market_start + timedelta(seconds=300),
        tick_size=0.01,
        min_order_size=5.0,
    )

    bot.balance = 1000.0
    shares, notional, min_order_size = bot._entry_target_size(price=0.50, market=market)
    assert shares == 32.0
    assert notional == 16.0
    assert min_order_size == 5.0

    bot.balance = 949.0
    shares, _, _ = bot._entry_target_size(price=0.50, market=market)
    assert shares == 32.0

    bot.balance = 899.0
    decision = bot._ladder_stake_decision()
    assert decision.target_shares == 26.0
    assert decision.reason == "Downgraded below $900 buffer"


def test_ladder_entry_sizing_requires_closed_trade_before_scale_up_after_external_balance_change() -> None:
    bot = object.__new__(StatsBot)
    bot.config = type("Config", (), {"paper_trading": True, "stake_mode": "ladder_shares"})()
    bot.wallet_balance_live = None

    bot.balance = 950.0
    assert bot._ladder_stake_decision().target_shares == 26.0

    bot.balance = 1000.0
    pending = bot._ladder_stake_decision()
    assert pending.target_shares == 26.0
    assert pending.reason == "Scale-up pending above $1,000 until one closed trade"

    bot._record_staking_closed_trade(1.0)
    promoted = bot._ladder_stake_decision()
    assert promoted.target_shares == 32.0
    assert promoted.reason is None


def test_ladder_entry_sizing_promotes_when_closed_trade_crosses_tier() -> None:
    bot = object.__new__(StatsBot)
    bot.config = type("Config", (), {"paper_trading": True, "stake_mode": "ladder_shares"})()
    bot.wallet_balance_live = None

    bot.balance = 950.0
    assert bot._ladder_stake_decision().target_shares == 26.0

    bot.balance = 1001.0
    bot._record_staking_closed_trade(51.0)
    assert bot._ladder_stake_decision().target_shares == 32.0


def test_dynamic_entry_sizing_disables_bonus_before_force_min_drawdown() -> None:
    bot = object.__new__(StatsBot)
    bot.config = type("Config", (), {"paper_trading": True, "stake_mode": "dynamic_shares"})()
    bot.wallet_balance_live = None
    bot.balance = 85.0
    bot.drawdown_guard = DrawdownGuard()
    bot.current_window_ts = None
    bot.staking_positive_streak_pnl = 1.25
    assert bot._dynamic_target_shares() == 10.0

    bot.balance = 80.0
    decision = bot._dynamic_stake_decision()
    assert decision.target_shares == 8.0
    assert decision.reason is None
    assert bot.staking_positive_streak_pnl == 0.0

    bot.balance = 91.0
    bot._sync_staking_high_water(100.0)
    decision = bot._dynamic_stake_decision()
    assert decision.target_shares == 5.0
    assert decision.reason and decision.reason.startswith("Reduced to 5 shares")


def test_dynamic_entry_sizing_uses_collateral_adjusted_drawdown_guard() -> None:
    bot = object.__new__(StatsBot)
    bot.config = type("Config", (), {"paper_trading": True, "stake_mode": "dynamic_shares"})()
    bot.wallet_balance_live = None
    bot.balance = 62.686723
    bot.drawdown_guard = DrawdownGuard()
    bot.current_window_ts = None
    now = datetime(2026, 6, 26, 9, 24, tzinfo=timezone.utc)
    bot._sync_staking_high_water(62.686723)
    bot.balance = 59.326723
    bot.active_position = OpenPosition(
        token_id="token",
        market_slug="btc-updown-5m-1782465600",
        market_id="1",
        market_question="test",
        market_end_utc=now + timedelta(minutes=1),
        held_side="DOWN",
        shares=8.0,
        avg_entry_price=0.42,
        entry_cost=3.36,
        entry_time_utc=now - timedelta(minutes=3),
        entry_bps=1.0,
        entry_day_type="weekday",
        entry_session_bucket="EUROPE_MIDDAY",
        entry_edge_threshold=0.1,
        entry_bps_threshold=2.0,
        entry_policy_key_text="weekday|EUROPE_MIDDAY|90-119|pos1_to_pos2p5|medium|p55_to_p60|p40_to_p45",
        entry_snapshot={},
    )

    decision = bot._dynamic_stake_decision()

    assert decision.target_shares == 6.0
    assert decision.drawdown_pct == 0.0
    assert decision.reason is None


def test_drawdown_guard_does_not_add_active_cost_to_stale_live_wallet() -> None:
    bot = object.__new__(StatsBot)
    bot.config = type("Config", (), {"paper_trading": False, "stake_mode": "dynamic_shares"})()
    bot.wallet_balance_live = 67.336723
    bot.balance = 63.416723
    bot.drawdown_guard = DrawdownGuard()
    bot.current_window_ts = None
    now = datetime(2026, 6, 26, 9, 46, tzinfo=timezone.utc)
    bot._sync_staking_high_water(67.336723)
    bot.active_position = OpenPosition(
        token_id="token",
        market_slug="btc-updown-5m-1782467100",
        market_id="1",
        market_question="test",
        market_end_utc=now + timedelta(minutes=4),
        held_side="UP",
        shares=7.0,
        avg_entry_price=0.56,
        entry_cost=3.92,
        entry_time_utc=now,
        entry_bps=1.0,
        entry_day_type="weekday",
        entry_session_bucket="EUROPE_MIDDAY",
        entry_edge_threshold=0.08,
        entry_bps_threshold=2.0,
        entry_policy_key_text="weekday|EUROPE_MIDDAY|180-209|pos1_to_pos2p5|high|p55_to_p60|p40_to_p45",
        entry_snapshot={},
    )

    decision = bot._dynamic_stake_decision()

    assert round(bot._bankroll_for_drawdown_guard(), 6) == 67.336723
    assert round(decision.high_water, 6) == 67.336723
    assert round(decision.drawdown_pct, 6) == 0.0
    assert not decision.recovery_active


def test_drawdown_guard_does_not_double_count_unreserved_pending_notional() -> None:
    bot = object.__new__(StatsBot)
    bot.config = type("Config", (), {"paper_trading": True, "stake_mode": "dynamic_shares"})()
    bot.wallet_balance_live = None
    bot.balance = 60.0
    bot.pending_entry = PendingEntryOrder(
        order_id="paper-entry-1",
        token_id="up",
        side="BUY",
        price=0.50,
        target_shares=8.0,
        created_at_utc=datetime(2026, 6, 26, 9, 24, tzinfo=timezone.utc),
        market_slug="btc-updown-5m-test",
        policy_key_text="policy",
        policy_side="UP",
        snapshot={},
        target_notional=4.0,
    )

    assert bot._bankroll_for_drawdown_guard() == 60.0

    bot.pending_entry.wallet_balance_before = 64.0
    assert bot._bankroll_for_drawdown_guard() == 64.0


def test_live_fill_syncs_stale_wallet_cache_to_known_post_fill_balance() -> None:
    bot = object.__new__(StatsBot)
    bot.config = type("Config", (), {"paper_trading": False, "dust_share_threshold": 0.01})()
    bot.wallet_balance_live = 67.336723
    bot.balance = 67.336723
    bot.entry_fills_by_window = {}
    bot.recent_events = []
    bot.window_errors = []
    bot.active_position = None
    bot._log_event = lambda event_type, message, fields: bot.recent_events.append((event_type, message, fields))
    now = datetime(2026, 6, 26, 9, 46, tzinfo=timezone.utc)
    bot.current_market = MarketInfo(
        slug="btc-updown-5m-test",
        market_id="1",
        condition_id="condition",
        question="test",
        token_up="up",
        token_down="down",
        market_start_utc=now - timedelta(minutes=1),
        market_end_utc=now + timedelta(minutes=4),
        tick_size=0.01,
        min_order_size=5.0,
    )
    pending = PendingEntryOrder(
        order_id="0xabc",
        token_id="up",
        side="BUY",
        price=0.56,
        target_shares=7.0,
        created_at_utc=now,
        market_slug=bot.current_market.slug,
        policy_key_text="weekday|EUROPE_MIDDAY|180-209|pos1_to_pos2p5|high|p55_to_p60|p40_to_p45",
        policy_side="UP",
        snapshot={
            "cb_signed_bps_raw": 1.0,
            "day_type": "weekday",
            "session_bucket": "EUROPE_MIDDAY",
            "entry_edge_threshold": 0.08,
            "entry_bps_threshold": 2.0,
        },
        target_notional=3.92,
        wallet_balance_before=67.336723,
    )
    pending.matched_shares_seen = 7.0

    bot._open_position_from_entry(pending, 7.0, 0.56, now)

    assert round(bot.balance, 6) == 63.416723
    assert round(bot.wallet_balance_live or 0.0, 6) == 63.416723
    assert bot.active_position is not None
    assert round(bot.active_position.entry_cost, 6) == 3.92


def test_dynamic_entry_sizing_adds_one_share_after_positive_streak_near_high_water() -> None:
    bot = object.__new__(StatsBot)
    bot.config = type("Config", (), {"paper_trading": True, "stake_mode": "dynamic_shares"})()
    bot.wallet_balance_live = None
    bot.balance = 70.0
    bot.drawdown_guard = DrawdownGuard()
    bot.current_window_ts = None
    bot.staking_positive_streak_pnl = 1.25

    decision = bot._dynamic_stake_decision()

    assert decision.target_shares == 9.0
    assert decision.reason and decision.reason.startswith("Increased stake tier")


def test_dynamic_entry_sizing_recovery_disables_positive_streak_bonus() -> None:
    bot = object.__new__(StatsBot)
    bot.config = type("Config", (), {"paper_trading": True, "stake_mode": "dynamic_shares"})()
    bot.wallet_balance_live = None
    bot.balance = 100.0
    bot.drawdown_guard = DrawdownGuard()
    bot.current_window_ts = 1_000
    bot.staking_positive_streak_pnl = 1.25
    bot._sync_staking_high_water(100.0)
    bot.balance = 86.0

    decision = bot._dynamic_stake_decision()

    assert decision.target_shares == 5.0
    assert decision.recovery_active
    assert decision.reason and decision.reason.startswith("Recovery mode")


def test_global_side_momentum_signal_is_observed_but_not_entry_blocking() -> None:
    bot = object.__new__(StatsBot)
    now = datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc)

    decision = type("Decision", (), {"selected_side": "UP"})()
    assert bot._side_momentum_block_details({"btc_mom_20s_bps": -2.6}, "UP") == (True, -2.6)
    assert bot._regime_entry_blockers(
        snapshot={"btc_mom_20s_bps": -2.6},
        decision=decision,
        now_utc=now,
    ) == ()
    assert bot._regime_entry_blockers(
        snapshot={"btc_mom_20s_bps": 2.6},
        decision=decision,
        now_utc=now,
    ) == ()

    decision = type("Decision", (), {"selected_side": "DOWN"})()
    assert bot._side_momentum_block_details({"btc_mom_20s_bps": 2.6}, "DOWN") == (True, 2.6)
    assert bot._regime_entry_blockers(
        snapshot={"btc_mom_20s_bps": 2.6},
        decision=decision,
        now_utc=now,
    ) == ()
    assert bot._regime_entry_blockers(
        snapshot={"btc_mom_20s_bps": 2.49},
        decision=decision,
        now_utc=now,
    ) == ()


def test_recent_damage_recovery_guard_is_disabled() -> None:
    bot = object.__new__(StatsBot)
    now = datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc)
    bot.recent_events = []
    bot._log_event = lambda event_type, message, fields: bot.recent_events.append((event_type, message, fields))

    for offset in range(4):
        bot._record_regime_closed_trade(-2.0, now + timedelta(minutes=offset))

    weak_decision = type(
        "Decision",
        (),
        {"selected_side": "UP", "selected_policy_edge": 0.115, "entry_edge_threshold": 0.11},
    )()
    strong_decision = type(
        "Decision",
        (),
        {"selected_side": "UP", "selected_policy_edge": 0.121, "entry_edge_threshold": 0.11},
    )()

    active, hard_block, state = bot._regime_recovery_status(now + timedelta(minutes=5))
    assert active is False
    assert hard_block is False
    assert state.trigger_pnl is None
    assert state.trigger_count == 0
    assert bot.recent_events == []
    assert bot._regime_entry_blockers(
        snapshot={"btc_mom_20s_bps": 0.0},
        decision=weak_decision,
        now_utc=now + timedelta(minutes=45),
    ) == ()
    assert bot._regime_entry_blockers(
        snapshot={"btc_mom_20s_bps": 0.0},
        decision=strong_decision,
        now_utc=now + timedelta(minutes=45),
    ) == ()


def test_low_liquidity_no_signal_guard_blocks_cheap_weak_low_vol_entries() -> None:
    bot = object.__new__(StatsBot)
    bot.config = SimpleNamespace(
        low_liquidity_no_signal_guard_enabled=True,
        low_liquidity_no_signal_max_price=0.40,
        low_liquidity_no_signal_max_abs_move_dz=0.50,
        low_liquidity_no_signal_max_vol_bps=10.0,
    )
    snapshot = {
        "selected_move_dz": 0.003,
        "local_volatility_1h_bps": 8.25,
    }
    decision = EntryDecision(
        viable=True,
        blockers=(),
        policy_key=("strategy", "surface", "p20_30", "UP"),
        policy_record=None,
        selected_side="UP",
        selected_bid=0.24,
        selected_ask=0.25,
        selected_mid=0.245,
        selected_spread=0.01,
        selected_trade_value=0.35,
        selected_policy_edge=0.10,
        selected_trade_style="continuation",
        entry_edge_threshold=0.04,
        entry_session_enabled=True,
        threshold_bps=0.0,
    )

    assert bot._low_liquidity_no_signal_entry_blockers(snapshot, decision) == ("LOW_LIQUIDITY_NO_SIGNAL",)
    assert snapshot["low_liquidity_no_signal_selected_price"] == 0.25
    assert snapshot["low_liquidity_no_signal_abs_move_dz"] == 0.003
    assert snapshot["low_liquidity_no_signal_local_vol_bps"] == 8.25


def test_low_liquidity_no_signal_guard_allows_stronger_or_higher_vol_entries() -> None:
    bot = object.__new__(StatsBot)
    bot.config = SimpleNamespace(
        low_liquidity_no_signal_guard_enabled=True,
        low_liquidity_no_signal_max_price=0.40,
        low_liquidity_no_signal_max_abs_move_dz=0.50,
        low_liquidity_no_signal_max_vol_bps=10.0,
    )
    decision = EntryDecision(
        viable=True,
        blockers=(),
        policy_key=("strategy", "surface", "p20_30", "UP"),
        policy_record=None,
        selected_side="UP",
        selected_bid=0.24,
        selected_ask=0.25,
        selected_mid=0.245,
        selected_spread=0.01,
        selected_trade_value=0.35,
        selected_policy_edge=0.10,
        selected_trade_style="continuation",
        entry_edge_threshold=0.04,
        entry_session_enabled=True,
        threshold_bps=0.0,
    )

    assert bot._low_liquidity_no_signal_entry_blockers(
        {"selected_move_dz": 0.60, "local_volatility_1h_bps": 8.25},
        decision,
    ) == ()
    assert bot._low_liquidity_no_signal_entry_blockers(
        {"selected_move_dz": 0.003, "local_volatility_1h_bps": 10.01},
        decision,
    ) == ()


def test_drawdown_guard_normalises_losses_by_share_count() -> None:
    assert normalised_5_share_pnl(-7.0, 10.0) == -3.5
    assert normalised_5_share_pnl(-5.6, 8.0) == -3.5


def test_drawdown_guard_no_longer_blocks_entries() -> None:
    guard = DrawdownGuard()

    blockers = guard.entry_blockers(
        snapshot={"day_type": "weekday", "session_bucket": "US_OPEN_EARLY", "volatility_bucket": "high"},
        trade_style="continuation",
        selected_policy_edge=0.20,
        entry_edge_threshold=0.12,
        open_positions=[],
    )

    guard.record_closed_trade(
        day_type="weekday",
        session_bucket="US_OPEN_EARLY",
        trade_style="continuation",
        pnl=-20.0,
        shares=5.0,
        market_slug="btc-updown-5m-1780000000",
    )

    assert blockers == ()
    assert guard.paused_cell_details(1780000000) == []
    assert guard.paused_cells == set()


def test_session_style_drawdown_guard_blocks_only_matching_slice() -> None:
    guard = SessionStyleDrawdownGuard(drawdown_per_share_usd=3.0)
    closed_at = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)

    key, pnl, activated = guard.record_closed_trade(
        london_date="2026-07-13",
        session_label="h06_12",
        trade_style="mean_reversion",
        pnl=10.0,
        closed_at_utc=closed_at,
        threshold_usd=15.0,
    )
    assert key == "2026-07-13|h06_12|mean_reversion"
    assert pnl == 10.0
    assert not activated
    assert guard.entry_blocker(
        london_date="2026-07-13",
        session_label="h06_12",
        session_name="EUROPE",
        trade_style="mean_reversion",
        threshold_usd=15.0,
    ) == ()
    detail = guard.details(
        london_date="2026-07-13",
        session_label="h06_12",
        session_name="EUROPE",
        trade_style="mean_reversion",
        threshold_usd=15.0,
    )
    assert detail["ladder_level"] == "normal"
    assert detail["edge_penalty"] == 0.0

    _, pnl, activated = guard.record_closed_trade(
        london_date="2026-07-13",
        session_label="h06_12",
        trade_style="mean_reversion",
        pnl=-16.0,
        closed_at_utc=closed_at,
        threshold_usd=15.0,
    )
    assert round(pnl, 2) == -6.0
    assert activated
    assert guard.entry_blocker(
        london_date="2026-07-13",
        session_label="h06_12",
        session_name="EUROPE",
        trade_style="mean_reversion",
        threshold_usd=15.0,
    ) == ("SESSION_STYLE_DRAWDOWN",)
    detail = guard.details(
        london_date="2026-07-13",
        session_label="h06_12",
        session_name="EUROPE",
        trade_style="mean_reversion",
        threshold_usd=15.0,
    )
    assert detail["ladder_level"] == "killed"

    assert guard.entry_blocker(
        london_date="2026-07-13",
        session_label="h06_12",
        session_name="EUROPE",
        trade_style="continuation",
        threshold_usd=15.0,
    ) == ()
    assert guard.entry_blocker(
        london_date="2026-07-13",
        session_label="h12_18",
        session_name="US_OPEN",
        trade_style="mean_reversion",
        threshold_usd=15.0,
    ) == ()
    assert guard.entry_blocker(
        london_date="2026-07-14",
        session_label="h06_12",
        session_name="EUROPE",
        trade_style="mean_reversion",
        threshold_usd=15.0,
    ) == ()


def test_session_style_drawdown_guard_reports_edge_ladder() -> None:
    guard = SessionStyleDrawdownGuard(
        drawdown_per_share_usd=3.0,
        degraded_edge_penalty=0.01,
        poor_edge_penalty=0.02,
    )
    closed_at = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)

    guard.record_closed_trade(
        london_date="2026-07-13",
        session_label="h12_18",
        trade_style="continuation",
        pnl=10.0,
        closed_at_utc=closed_at,
        threshold_usd=15.0,
    )
    guard.record_closed_trade(
        london_date="2026-07-13",
        session_label="h12_18",
        trade_style="continuation",
        pnl=-8.0,
        closed_at_utc=closed_at,
        threshold_usd=15.0,
    )
    degraded = guard.details(
        london_date="2026-07-13",
        session_label="h12_18",
        session_name="US_OPEN",
        trade_style="continuation",
        threshold_usd=15.0,
    )
    assert degraded["active"] is False
    assert degraded["ladder_level"] == "degraded"
    assert degraded["edge_penalty"] == 0.01

    guard.record_closed_trade(
        london_date="2026-07-13",
        session_label="h12_18",
        trade_style="continuation",
        pnl=-5.0,
        closed_at_utc=closed_at,
        threshold_usd=15.0,
    )
    poor = guard.details(
        london_date="2026-07-13",
        session_label="h12_18",
        session_name="US_OPEN",
        trade_style="continuation",
        threshold_usd=15.0,
    )
    assert poor["active"] is False
    assert poor["ladder_level"] == "poor"
    assert poor["edge_penalty"] == 0.02


def test_session_style_drawdown_guard_uses_fixed_two_hour_kill_only_bucket() -> None:
    bot = object.__new__(StatsBot)
    bot.config = type("Config", (), {"min_order_size_fallback": 5.0, "session_style_drawdown_guard_enabled": True})()
    bot.current_market = None
    bot.last_session_style_guard_detail = None
    bot.session_style_drawdown_guard = SessionStyleDrawdownGuard(drawdown_per_share_usd=1.5)
    closed_at = datetime(2026, 7, 13, 13, 0, tzinfo=timezone.utc)
    bot.session_style_drawdown_guard.record_closed_trade(
        london_date="2026-07-13",
        session_label="h14_16",
        trade_style="continuation",
        pnl=10.0,
        closed_at_utc=closed_at,
        threshold_usd=7.5,
    )
    bot.session_style_drawdown_guard.record_closed_trade(
        london_date="2026-07-13",
        session_label="h14_16",
        trade_style="continuation",
        pnl=-17.5,
        closed_at_utc=closed_at,
        threshold_usd=7.5,
    )
    snapshot = {"timestamp_london": datetime(2026, 7, 13, 14, 30, tzinfo=ZoneInfo("Europe/London"))}
    decision = EntryDecision(
        viable=True,
        blockers=(),
        policy_key=("weekday", "h12_18", "180-209", "z_04", "p40_to_p50", "UP"),
        policy_record=None,
        selected_side="UP",
        selected_bid=0.43,
        selected_ask=0.44,
        selected_mid=0.435,
        selected_spread=0.01,
        selected_trade_value=0.50,
        selected_policy_edge=0.055,
        selected_trade_style="continuation",
        entry_edge_threshold=0.05,
        entry_session_enabled=True,
        threshold_bps=2.0,
    )

    blockers, effective_edge = bot._session_style_entry_blockers(closed_at, snapshot, decision)
    assert blockers == ("SESSION_STYLE_DRAWDOWN",)
    assert effective_edge is None
    assert snapshot["session_style_guard_key"] == "2026-07-13|h14_16|continuation"
    assert snapshot["session_style_guard_threshold"] == 7.5
    assert snapshot["session_style_guard_ladder_level"] == "killed"
    assert snapshot["session_style_guard_edge_penalty"] == 0.0

    next_bucket_snapshot = {"timestamp_london": datetime(2026, 7, 13, 16, 0, tzinfo=ZoneInfo("Europe/London"))}
    blockers, effective_edge = bot._session_style_entry_blockers(closed_at, next_bucket_snapshot, decision)
    assert blockers == ()
    assert round(effective_edge, 6) == 0.05
    assert next_bucket_snapshot["session_style_guard_key"] == "2026-07-13|h16_18|continuation"


def test_live_cancel_reconciles_fill_before_clearing_pending_order() -> None:
    class FakePolymarket:
        def __init__(self) -> None:
            self.cancelled = False

        def get_order(self, order_id: str) -> dict:
            if self.cancelled:
                return {"status": "cancelled", "size_matched": 5.0, "price": 0.66}
            return {"status": "open", "size_matched": 0.0, "price": 0.66}

        def cancel_order(self, order_id: str) -> None:
            self.cancelled = True

    bot = object.__new__(StatsBot)
    bot.config = type("Config", (), {"paper_trading": False, "dust_share_threshold": 0.01})()
    bot.pm = FakePolymarket()
    bot.balance = 19.599689
    bot.entry_fills_by_window = {}
    bot.active_position = None
    bot.recent_events = []
    bot.window_errors = []
    bot._log_event = lambda event_type, message, fields: bot.recent_events.append((event_type, message, fields))
    bot._remember_error = lambda message: bot.window_errors.append(message)
    market_start = datetime(2026, 6, 15, 8, 45, tzinfo=timezone.utc)
    bot.current_market = MarketInfo(
        slug="btc-updown-5m-test",
        market_id="1",
        condition_id="condition",
        question="test",
        token_up="up",
        token_down="down",
        market_start_utc=market_start,
        market_end_utc=market_start + timedelta(seconds=300),
        tick_size=0.01,
        min_order_size=5.0,
    )
    bot.pending_entry = PendingEntryOrder(
        order_id="0xabc",
        token_id="up",
        side="BUY",
        price=0.66,
        target_shares=5.0,
        created_at_utc=market_start + timedelta(seconds=150),
        market_slug=bot.current_market.slug,
        policy_key_text="policy",
        policy_side="UP",
        snapshot={
            "cb_signed_bps_raw": 1.0,
            "day_type": "weekday",
            "session_bucket": "EUROPE_OPEN",
            "entry_edge_threshold": 0.11,
            "entry_bps_threshold": 2.0,
        },
    )

    bot._cancel_pending_entry("MAKER_PRICE_CHANGED")

    assert bot.pending_entry is None
    assert bot.active_position is not None
    assert bot.active_position.held_side == "UP"
    assert bot.active_position.shares == 5.0
    assert bot.active_position.avg_entry_price == 0.66
    assert bot.balance == 16.299689
    assert bot.entry_fills_by_window[bot.current_market.slug] == 1
    assert any(event[0] == "maker_entry_fill_reconciled" for event in bot.recent_events)
    assert any(event[0] == "entry_filled" for event in bot.recent_events)


def test_pending_entry_cancels_when_dynamic_target_changes_in_recovery() -> None:
    bot = object.__new__(StatsBot)
    bot.config = type(
        "Config",
        (),
        {
            "paper_trading": True,
            "paper_fill_model": "no_touch",
            "stake_mode": "dynamic_shares",
            "min_order_size_fallback": 5.0,
        },
    )()
    bot.wallet_balance_live = None
    bot.balance = 86.0
    bot.drawdown_guard = DrawdownGuard()
    bot.current_window_ts = None
    bot.staking_positive_streak_pnl = 0.0
    bot.active_position = None
    bot.recent_events = []
    bot.window_errors = []
    bot.replaced_entry = False
    bot._log_event = lambda event_type, message, fields: bot.recent_events.append((event_type, message, fields))
    now = datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc)
    bot._sync_staking_high_water(100.0)
    market_start = now - timedelta(seconds=60)
    bot.current_market = MarketInfo(
        slug="btc-updown-5m-test",
        market_id="1",
        condition_id="condition",
        question="test",
        token_up="up",
        token_down="down",
        market_start_utc=market_start,
        market_end_utc=market_start + timedelta(seconds=300),
        tick_size=0.01,
        min_order_size=5.0,
    )
    key = ("weekday", "US_OPEN_EARLY", "180-209", "pos1_to_pos2p5", "low", "p45_to_p50", "p50_to_p55")
    bot.pending_entry = PendingEntryOrder(
        order_id="paper-entry-1",
        token_id="up",
        side="BUY",
        price=0.50,
        target_shares=8.0,
        created_at_utc=now - timedelta(seconds=5),
        market_slug=bot.current_market.slug,
        policy_key_text="|".join(key),
        policy_side="UP",
        snapshot={},
    )
    decision = EntryDecision(
        viable=True,
        blockers=(),
        policy_key=key,
        policy_record=None,
        selected_side="UP",
        selected_bid=0.49,
        selected_ask=0.51,
        selected_mid=0.50,
        selected_spread=0.02,
        selected_trade_value=0.65,
        selected_policy_edge=0.14,
        selected_trade_style="mean_reversion",
        entry_edge_threshold=0.10,
        entry_session_enabled=True,
        threshold_bps=2.0,
    )
    bot._place_entry = lambda _now, _snapshot, _decision: setattr(bot, "replaced_entry", True)

    bot._manage_pending_entry(now, {"day_type": "weekday", "session_bucket": "US_OPEN_EARLY"}, decision)

    assert bot.pending_entry is None
    assert bot.replaced_entry is True
    assert any(event[1] == "STAKE_TARGET_CHANGED" for event in bot.recent_events)


def test_hold_to_resolution_suppresses_tactical_sell_execution() -> None:
    class FakePolymarket:
        def __init__(self) -> None:
            self.sell_called = False

        def get_order_book_snapshot(self, token_id: str) -> BookSnapshot:
            return BookSnapshot(
                bid=0.28,
                ask=0.29,
                mid=0.285,
                spread=0.01,
                bid_depth_total=10,
                ask_depth_total=10,
                best_bid_depth=5,
                best_ask_depth=5,
                captured_at_utc=datetime(2026, 6, 15, 8, 59, tzinfo=timezone.utc),
            )

        def place_immediate_sell(self, **kwargs):
            self.sell_called = True
            raise AssertionError("hold_to_resolution must not submit tactical sells")

    bot = object.__new__(StatsBot)
    bot.config = type(
        "Config",
        (),
        {
            "exit_policy_mode": "hold_to_resolution",
            "paper_trading": False,
            "dust_share_threshold": 0.01,
            "exit_retry_cadence_seconds": 1.0,
        },
    )()
    bot.pm = FakePolymarket()
    bot.recent_events = []
    bot.window_errors = []
    bot._log_event = lambda event_type, message, fields: bot.recent_events.append((event_type, message, fields))
    bot._remember_error = lambda message: bot.window_errors.append(message)
    market_start = datetime(2026, 6, 15, 8, 55, tzinfo=timezone.utc)
    bot.current_market = MarketInfo(
        slug="btc-updown-5m-test",
        market_id="1",
        condition_id="condition",
        question="test",
        token_up="up",
        token_down="down",
        market_start_utc=market_start,
        market_end_utc=market_start + timedelta(seconds=300),
        tick_size=0.01,
        min_order_size=5.0,
    )
    bot.active_position = OpenPosition(
        token_id="up",
        market_slug=bot.current_market.slug,
        market_id=bot.current_market.market_id,
        market_question=bot.current_market.question,
        market_end_utc=bot.current_market.market_end_utc,
        held_side="UP",
        shares=5.0,
        avg_entry_price=0.66,
        entry_cost=3.30,
        entry_time_utc=market_start + timedelta(seconds=150),
        entry_bps=4.0,
        entry_day_type="weekday",
        entry_session_bucket="EUROPE_OPEN",
        entry_edge_threshold=0.11,
        entry_bps_threshold=2.0,
        entry_policy_key_text="policy",
        entry_snapshot={},
    )
    exit_decision = ExitDecision(
        should_exit=True,
        reason="BPS_YES_CONFLICT_POSITION_EMERGENCY_EXIT",
        policy_no_longer_supportive=True,
        signed_bps_materially_against=True,
        current_policy_side="UP",
        current_policy_key=None,
        current_bps=4.0,
        entry_bps=4.0,
        threshold_bps=2.0,
        exit_bid=0.28,
    )

    bot._maybe_execute_exit(
        datetime(2026, 6, 15, 8, 59, tzinfo=timezone.utc),
        snapshot={"up_bid": 0.28},
        exit_decision=exit_decision,
        force_reason="BPS_YES_CONFLICT_POSITION_EMERGENCY_EXIT",
    )

    assert bot.active_position is not None
    assert bot.active_position.shares == 5.0
    assert bot.pm.sell_called is False
    assert any(event[0] == "exit_suppressed" for event in bot.recent_events)


def test_hold_to_resolution_uses_polymarket_settlement_outcome(tmp_path) -> None:
    class FakePolymarket:
        def resolved_market_outcome(self, *, slug: str, market_id: str | None = None, condition_id: str | None = None):
            assert slug == "btc-updown-5m-test"
            assert market_id == "1"
            return MarketResolution("DOWN", "gamma_outcome_prices:https://data.chain.link/streams/btc-usd", True)

    class CoinbaseShouldNotResolve:
        def resolution_price_at_or_after(self, end_ts: int) -> float:
            raise AssertionError("settlement must use Polymarket/Gamma outcome, not Coinbase")

    bot = object.__new__(StatsBot)
    bot.config = SimpleNamespace(
        session_style_drawdown_guard_enabled=False,
        paper_trading=True,
        stake_mode="min_order_size",
    )
    bot.pm = FakePolymarket()
    bot.coinbase = CoinbaseShouldNotResolve()
    bot.active_position = None
    bot.settling_positions = []
    bot.balance = 10.0
    bot.session_pnl = 0.0
    bot.realized_pnl = 0.0
    bot.total_wins = 0
    bot.total_losses = 0
    bot.trade_style_wins = Counter()
    bot.trade_style_losses = Counter()
    bot.last_exit_time_by_window = {}
    bot.recent_events = []
    bot.trades_log_path = tmp_path / "stats_trades.csv"
    bot._log_event = lambda event_type, message, fields: bot.recent_events.append((event_type, message, fields))
    market_start = datetime(2026, 6, 15, 8, 55, tzinfo=timezone.utc)
    position = OpenPosition(
        token_id="down",
        market_slug="btc-updown-5m-test",
        market_id="1",
        market_question="test",
        market_end_utc=market_start + timedelta(seconds=300),
        held_side="DOWN",
        shares=5.0,
        avg_entry_price=0.44,
        entry_cost=2.20,
        entry_time_utc=market_start + timedelta(seconds=150),
        entry_bps=-4.0,
        entry_day_type="weekday",
        entry_session_bucket="EUROPE_OPEN",
        entry_edge_threshold=0.11,
        entry_bps_threshold=2.0,
        entry_policy_key_text="weekday|EUROPE_OPEN|tte|neg5_to_neg2p5|vol|up|down",
        entry_snapshot={"entry_trade_style": "continuation", "condition_id": "condition"},
    )
    bot.active_position = position

    resolved = bot._resolve_position(position, market_start + timedelta(seconds=305))

    assert resolved is True
    assert bot.active_position is None
    assert bot.balance == 15.0
    assert bot.realized_pnl == 2.80
    assert bot.total_wins == 1
    assert bot.trade_style_wins["continuation"] == 1
    text = bot.trades_log_path.read_text()
    assert "DOWN" in text
    assert "gamma_outcome_prices:https://data.chain.link/streams/btc-usd" in text


def test_hold_to_resolution_waits_when_polymarket_outcome_unresolved(tmp_path) -> None:
    class FakePolymarket:
        def resolved_market_outcome(self, *, slug: str, market_id: str | None = None, condition_id: str | None = None):
            return MarketResolution(None, "gamma_not_closed", False)

    bot = object.__new__(StatsBot)
    bot.pm = FakePolymarket()
    bot.active_position = None
    bot.settling_positions = []
    bot.balance = 10.0
    bot.session_pnl = 0.0
    bot.realized_pnl = 0.0
    bot.total_wins = 0
    bot.total_losses = 0
    bot.trade_style_wins = Counter()
    bot.trade_style_losses = Counter()
    bot.last_exit_time_by_window = {}
    bot.recent_events = []
    bot.trades_log_path = tmp_path / "stats_trades.csv"
    bot._log_event = lambda event_type, message, fields: bot.recent_events.append((event_type, message, fields))
    market_start = datetime(2026, 6, 15, 8, 55, tzinfo=timezone.utc)
    position = OpenPosition(
        token_id="up",
        market_slug="btc-updown-5m-test",
        market_id="1",
        market_question="test",
        market_end_utc=market_start + timedelta(seconds=300),
        held_side="UP",
        shares=5.0,
        avg_entry_price=0.44,
        entry_cost=2.20,
        entry_time_utc=market_start + timedelta(seconds=150),
        entry_bps=4.0,
        entry_day_type="weekday",
        entry_session_bucket="EUROPE_OPEN",
        entry_edge_threshold=0.11,
        entry_bps_threshold=2.0,
        entry_policy_key_text="weekday|EUROPE_OPEN|tte|pos2p5_to_pos5|vol|up|down",
        entry_snapshot={"entry_trade_style": "continuation"},
    )
    bot.active_position = position

    resolved = bot._resolve_position(position, market_start + timedelta(seconds=305))

    assert resolved is False
    assert bot.active_position is None
    assert bot.settling_positions == [position]
    assert bot.balance == 10.0
    assert bot.total_wins == 0
    assert bot.total_losses == 0
    assert not bot.trades_log_path.exists()
    assert any(event[0] == "settlement_pending" and event[1] == "POLYMARKET_OUTCOME_NOT_RESOLVED" for event in bot.recent_events)


def test_live_blocks_retry_while_window_lifecycle_is_resting() -> None:
    class FakePolymarket:
        def __init__(self) -> None:
            self.buy_calls = 0

        def place_maker_buy(self, **kwargs):
            self.buy_calls += 1
            return type("Result", (), {"ok": True, "order_id": f"order-{self.buy_calls}", "status": "PLACED", "message": ""})()

    bot = object.__new__(StatsBot)
    bot.config = type(
        "Config",
        (),
        {
            "paper_trading": False,
            "observe_first_window": False,
            "maker_min_spread_ticks": 1,
            "stake_mode": "min_order_size",
            "stake_usd": 5.0,
            "min_order_size_fallback": 5.0,
            "dust_share_threshold": 0.01,
            "cooldown_seconds": 10,
            "live_entry_retry_cooldown_seconds": 8.0,
            "live_max_entry_attempts_per_window": 2,
            "live_wallet_reconcile_tolerance_usd": 0.02,
            "balance_refresh_seconds": 15,
        },
    )()
    bot.pm = FakePolymarket()
    bot.current_window_ts = 1
    bot.startup_window_ts = 0
    bot.pending_entry = None
    bot.active_position = None
    bot.entry_fills_by_window = {}
    bot.entry_attempts_by_window = {}
    bot.entry_lifecycle_by_window = {}
    bot.entry_attempt_orders_by_window = {}
    bot.last_entry_attempt_time_by_window = {}
    bot.last_exit_time_by_window = {}
    bot.wallet_balance_live = 100.0
    bot.wallet_balance_start_live = 100.0
    bot._last_balance_refresh_at = 0.0
    bot._refresh_wallet_balance = lambda force=False: 100.0
    bot.recent_events = []
    bot._log_event = lambda event_type, message, fields: bot.recent_events.append((event_type, message, fields))
    market_start = datetime(2026, 6, 15, 11, 35, tzinfo=timezone.utc)
    bot.current_market = MarketInfo(
        slug="btc-updown-5m-test",
        market_id="1",
        condition_id="condition",
        question="test",
        token_up="up",
        token_down="down",
        market_start_utc=market_start,
        market_end_utc=market_start + timedelta(seconds=300),
        tick_size=0.01,
        min_order_size=5.0,
    )
    bot._selected_token_id = lambda side: "up"
    bot._maker_quote = lambda side: (
        0.26,
        1,
        BookSnapshot(
            bid=0.26,
            ask=0.27,
            mid=0.265,
            spread=0.01,
            bid_depth_total=10,
            ask_depth_total=10,
            best_bid_depth=5,
            best_ask_depth=5,
            captured_at_utc=market_start,
        ),
    )
    decision = type(
        "Decision",
        (),
        {
            "viable": True,
            "selected_side": "UP",
            "policy_key_text": "policy",
            "selected_trade_value": 0.50,
            "selected_policy_edge": 0.20,
            "entry_edge_threshold": 0.12,
            "entry_session_enabled": True,
            "selected_trade_style": None,
            "threshold_bps": 2.0,
        },
    )()
    snapshot = {"day_type": "weekday", "session_bucket": "US_PREOPEN", "cb_signed_bps_raw": -5.0}

    bot._place_entry(market_start + timedelta(seconds=50), snapshot, decision)
    bot.pending_entry = None
    bot._place_entry(market_start + timedelta(seconds=55), snapshot, decision)
    bot._place_entry(market_start + timedelta(seconds=59), snapshot, decision)

    assert bot.pm.buy_calls == 1
    assert bot.entry_attempts_by_window[bot.current_market.slug] == 1
    assert bot.entry_lifecycle_by_window[bot.current_market.slug] == "RESTING"
    assert any(event[1] == "SKIPPED_ENTRY_LIFECYCLE_ACTIVE" for event in bot.recent_events)


def test_live_failed_maker_submit_consumes_retry_cooldown() -> None:
    class FakePolymarket:
        def __init__(self) -> None:
            self.buy_calls = 0

        def place_maker_buy(self, **kwargs):
            self.buy_calls += 1
            return type(
                "Result",
                (),
                {
                    "ok": False,
                    "order_id": None,
                    "status": "ERROR",
                    "message": "invalid post-only order: order crosses book",
                },
            )()

    bot = object.__new__(StatsBot)
    bot.config = type(
        "Config",
        (),
        {
            "paper_trading": False,
            "observe_first_window": False,
            "maker_min_spread_ticks": 1,
            "stake_mode": "min_order_size",
            "stake_usd": 5.0,
            "min_order_size_fallback": 5.0,
            "dust_share_threshold": 0.01,
            "cooldown_seconds": 10,
            "live_entry_retry_cooldown_seconds": 8.0,
            "live_max_entry_attempts_per_window": 2,
            "live_wallet_reconcile_tolerance_usd": 0.02,
            "balance_refresh_seconds": 15,
        },
    )()
    bot.pm = FakePolymarket()
    bot.current_window_ts = 1
    bot.startup_window_ts = 0
    bot.pending_entry = None
    bot.active_position = None
    bot.entry_fills_by_window = {}
    bot.entry_attempts_by_window = {}
    bot.entry_lifecycle_by_window = {}
    bot.entry_attempt_orders_by_window = {}
    bot.last_entry_attempt_time_by_window = {}
    bot.last_exit_time_by_window = {}
    bot.wallet_balance_live = 100.0
    bot.wallet_balance_start_live = 100.0
    bot._last_balance_refresh_at = 0.0
    bot._refresh_wallet_balance = lambda force=False: 100.0
    bot.recent_events = []
    bot.window_errors = []
    bot._log_event = lambda event_type, message, fields: bot.recent_events.append((event_type, message, fields))
    bot._remember_error = lambda message: bot.window_errors.append(message)
    market_start = datetime(2026, 6, 15, 11, 35, tzinfo=timezone.utc)
    bot.current_market = MarketInfo(
        slug="btc-updown-5m-test",
        market_id="1",
        condition_id="condition",
        question="test",
        token_up="up",
        token_down="down",
        market_start_utc=market_start,
        market_end_utc=market_start + timedelta(seconds=300),
        tick_size=0.01,
        min_order_size=5.0,
    )
    bot._selected_token_id = lambda side: "up"
    bot._maker_quote = lambda side: (
        0.26,
        1,
        BookSnapshot(
            bid=0.26,
            ask=0.27,
            mid=0.265,
            spread=0.01,
            bid_depth_total=10,
            ask_depth_total=10,
            best_bid_depth=5,
            best_ask_depth=5,
            captured_at_utc=market_start,
        ),
    )
    decision = type(
        "Decision",
        (),
        {
            "viable": True,
            "selected_side": "UP",
            "policy_key_text": "policy",
            "selected_trade_value": 0.50,
            "selected_policy_edge": 0.20,
            "entry_edge_threshold": 0.12,
            "entry_session_enabled": True,
            "selected_trade_style": None,
            "threshold_bps": 2.0,
        },
    )()
    snapshot = {"day_type": "weekday", "session_bucket": "US_PREOPEN", "cb_signed_bps_raw": -5.0}

    bot._place_entry(market_start + timedelta(seconds=50), snapshot, decision)
    bot._place_entry(market_start + timedelta(seconds=55), snapshot, decision)

    assert bot.pm.buy_calls == 1
    assert bot.entry_attempts_by_window[bot.current_market.slug] == 1
    assert bot.entry_lifecycle_by_window[bot.current_market.slug] == "FAILED"
    assert bot.pending_entry is None
    assert any(event[0] == "maker_entry_failed" for event in bot.recent_events)
    assert any(event[1] == "SKIPPED_LIVE_ENTRY_RETRY_COOLDOWN" for event in bot.recent_events)


def test_live_wallet_drop_reconciles_cancelled_attempt_before_retry() -> None:
    bot = object.__new__(StatsBot)
    bot.config = type(
        "Config",
        (),
        {
            "paper_trading": False,
            "dust_share_threshold": 0.01,
            "live_wallet_reconcile_tolerance_usd": 0.02,
            "balance_refresh_seconds": 15,
        },
    )()
    bot.balance = 22.913014
    bot.wallet_balance_live = 21.613014
    bot.wallet_balance_start_live = 22.913014
    bot._last_balance_refresh_at = 0.0
    bot.pm = type("FakePolymarket", (), {})()
    bot.entry_fills_by_window = {}
    bot.entry_attempt_orders_by_window = {}
    bot.active_position = None
    bot.pending_entry = None
    bot.recent_events = []
    bot.window_errors = []
    bot._log_event = lambda event_type, message, fields: bot.recent_events.append((event_type, message, fields))
    bot._remember_error = lambda message: bot.window_errors.append(message)
    bot._refresh_wallet_balance = lambda force=False: 21.613014
    market_start = datetime(2026, 6, 15, 11, 35, tzinfo=timezone.utc)
    bot.current_market = MarketInfo(
        slug="btc-updown-5m-test",
        market_id="1",
        condition_id="condition",
        question="test",
        token_up="up",
        token_down="down",
        market_start_utc=market_start,
        market_end_utc=market_start + timedelta(seconds=300),
        tick_size=0.01,
        min_order_size=5.0,
    )
    attempt = PendingEntryOrder(
        order_id="0xabc",
        token_id="up",
        side="BUY",
        price=0.26,
        target_shares=5.0,
        created_at_utc=market_start + timedelta(seconds=50),
        market_slug=bot.current_market.slug,
        policy_key_text="policy",
        policy_side="UP",
        snapshot={
            "cb_signed_bps_raw": -5.0,
            "day_type": "weekday",
            "session_bucket": "US_PREOPEN",
            "entry_edge_threshold": 0.12,
            "entry_bps_threshold": 2.0,
        },
        target_notional=1.3,
        wallet_balance_before=22.913014,
    )
    bot.entry_attempt_orders_by_window[bot.current_market.slug] = [attempt]

    reconciled = bot._reconcile_live_wallet_fills_for_window(
        market_start + timedelta(seconds=60),
        bot.current_market.slug,
    )

    assert reconciled is True
    assert bot.active_position is not None
    assert bot.active_position.held_side == "UP"
    assert bot.active_position.shares == 5.0
    assert bot.active_position.avg_entry_price == 0.26
    assert attempt.wallet_reconciled is True
    assert any(event[0] == "maker_entry_wallet_reconciled" for event in bot.recent_events)


def test_pending_fast_limit_stays_live_when_resting_price_still_has_edge() -> None:
    bot = object.__new__(StatsBot)
    bot.config = type(
        "Config",
        (),
        {
            "paper_trading": True,
            "paper_fill_model": "fast_limit",
            "stake_mode": "min_order_size",
            "entry_quote_max_age_seconds": 999.0,
        },
    )()
    bot.pending_entry = PendingEntryOrder(
        order_id="0xresting",
        token_id="down",
        side="BUY",
        price=0.56,
        target_shares=5.0,
        created_at_utc=datetime(2026, 7, 15, 16, 11, 4, tzinfo=timezone.utc),
        market_slug="btc-updown-5m-test",
        policy_key_text="weekday|h12_18|210-239|z_06|p55_to_p65|DOWN",
        policy_side="DOWN",
        snapshot={"entry_execution_mode": "presigned_fast_limit"},
        target_notional=2.8,
    )
    bot._fast_entry_quote = lambda *_args: None
    bot._cancel_pending_entry = lambda reason: (_ for _ in ()).throw(AssertionError(f"unexpected cancel: {reason}"))
    bot._log_latency = lambda *args, **kwargs: None

    decision = EntryDecision(
        viable=False,
        blockers=("POLICY_NO_SIDE_EDGE",),
        policy_key=("weekday", "h12_18", "210-239", "z_06", "p55_to_p65", "DOWN"),
        policy_record=None,
        selected_side="DOWN",
        selected_bid=0.55,
        selected_ask=0.56,
        selected_mid=0.555,
        selected_spread=0.01,
        selected_trade_value=0.605676,
        selected_policy_edge=0.045676,
        selected_trade_style="continuation",
        entry_edge_threshold=0.04,
        entry_session_enabled=True,
        threshold_bps=2.0,
    )

    bot._manage_pending_entry(datetime(2026, 7, 15, 16, 11, 5, tzinfo=timezone.utc), {}, decision)

    assert bot.pending_entry is not None
    assert bot.pending_entry.order_id == "0xresting"


def test_pending_fast_limit_keeps_policy_side_flip_when_pending_edge_survives() -> None:
    bot = object.__new__(StatsBot)
    bot.config = type(
        "Config",
        (),
        {
            "paper_trading": True,
            "paper_fill_model": "fast_limit",
            "stake_mode": "min_order_size",
            "entry_quote_max_age_seconds": 999.0,
            "entry_resting_max_age_seconds": 2.0,
            "policy_no_longer_viable_latch_seconds": 1.5,
        },
    )()
    bot.pending_entry = PendingEntryOrder(
        order_id="0xstale",
        token_id="up",
        side="BUY",
        price=0.41,
        target_shares=5.0,
        created_at_utc=datetime(2026, 7, 15, 16, 11, 21, tzinfo=timezone.utc),
        market_slug="btc-updown-5m-test",
        policy_key_text="weekday|h12_18|210-239|z_06|p40_to_p50|UP",
        policy_side="UP",
        snapshot={"entry_execution_mode": "presigned_fast_limit"},
        target_notional=2.05,
    )
    kept: list[tuple] = []
    bot._fast_entry_quote = lambda *_args: None
    bot._cancel_pending_entry = lambda reason: (_ for _ in ()).throw(AssertionError(f"unexpected cancel: {reason}"))
    bot._log_latency = lambda *args, **kwargs: kept.append((args, kwargs))
    decision = EntryDecision(
        viable=True,
        blockers=(),
        policy_key=("weekday", "h12_18", "210-239", "z_06", "p45_to_p55", "DOWN"),
        policy_record=None,
        selected_side="DOWN",
        selected_bid=0.46,
        selected_ask=0.47,
        selected_mid=0.465,
        selected_spread=0.01,
        selected_trade_value=0.52,
        selected_policy_edge=0.05,
        selected_trade_style="continuation",
        entry_edge_threshold=0.04,
        entry_session_enabled=True,
        threshold_bps=2.0,
    )

    bot._manage_pending_entry(
        datetime(2026, 7, 15, 16, 11, 21, 750_000, tzinfo=timezone.utc),
        {"up_policy_value": 0.46},
        decision,
    )

    assert bot.pending_entry is not None
    assert bot.pending_entry.order_id == "0xstale"
    assert any(kwargs.get("detail", "").startswith("policy_side_changed_kept") for _, kwargs in kept)


def test_pending_fast_limit_cancels_policy_side_flip_when_pending_edge_is_gone() -> None:
    bot = object.__new__(StatsBot)
    bot.config = type(
        "Config",
        (),
        {
            "paper_trading": True,
            "paper_fill_model": "fast_limit",
            "stake_mode": "min_order_size",
            "entry_quote_max_age_seconds": 999.0,
            "entry_resting_max_age_seconds": 2.0,
            "policy_no_longer_viable_latch_seconds": 1.5,
        },
    )()
    bot.pending_entry = PendingEntryOrder(
        order_id="0xstale",
        token_id="up",
        side="BUY",
        price=0.41,
        target_shares=5.0,
        created_at_utc=datetime(2026, 7, 15, 16, 11, 21, tzinfo=timezone.utc),
        market_slug="btc-updown-5m-test",
        policy_key_text="weekday|h12_18|210-239|z_06|p40_to_p50|UP",
        policy_side="UP",
        snapshot={"entry_execution_mode": "presigned_fast_limit", "entry_required_edge_at_limit": 0.04},
        target_notional=2.05,
    )
    cancelled: list[str] = []

    def cancel(reason: str) -> None:
        cancelled.append(reason)
        bot.pending_entry = None

    bot._cancel_pending_entry = cancel
    decision = EntryDecision(
        viable=True,
        blockers=(),
        policy_key=("weekday", "h12_18", "210-239", "z_06", "p45_to_p55", "DOWN"),
        policy_record=None,
        selected_side="DOWN",
        selected_bid=0.46,
        selected_ask=0.47,
        selected_mid=0.465,
        selected_spread=0.01,
        selected_trade_value=0.52,
        selected_policy_edge=0.05,
        selected_trade_style="continuation",
        entry_edge_threshold=0.04,
        entry_session_enabled=True,
        threshold_bps=2.0,
    )

    bot._manage_pending_entry(
        datetime(2026, 7, 15, 16, 11, 21, 750_000, tzinfo=timezone.utc),
        {"up_policy_value": 0.43},
        decision,
    )

    assert cancelled == ["POLICY_SIDE_CHANGED"]
    assert bot.pending_entry is None


def test_pending_fast_limit_cancels_when_blocked_policy_side_flips() -> None:
    bot = object.__new__(StatsBot)
    bot.config = type(
        "Config",
        (),
        {
            "paper_trading": True,
            "paper_fill_model": "fast_limit",
            "stake_mode": "min_order_size",
            "entry_quote_max_age_seconds": 999.0,
            "entry_resting_max_age_seconds": 2.0,
            "policy_no_longer_viable_latch_seconds": 1.5,
        },
    )()
    bot.pending_entry = PendingEntryOrder(
        order_id="0xblockedflip",
        token_id="up",
        side="BUY",
        price=0.41,
        target_shares=5.0,
        created_at_utc=datetime(2026, 7, 15, 16, 11, 21, tzinfo=timezone.utc),
        market_slug="btc-updown-5m-test",
        policy_key_text="weekday|h12_18|210-239|z_06|p40_to_p50|UP",
        policy_side="UP",
        snapshot={"entry_execution_mode": "presigned_fast_limit", "entry_required_edge_at_limit": 0.04},
        target_notional=2.05,
    )
    cancelled: list[str] = []

    def cancel(reason: str) -> None:
        cancelled.append(reason)
        bot.pending_entry = None

    bot._cancel_pending_entry = cancel
    decision = EntryDecision(
        viable=False,
        blockers=("POLICY_NO_SIDE_EDGE",),
        policy_key=("weekday", "h12_18", "210-239", "z_06", "p45_to_p55", "DOWN"),
        policy_record=None,
        selected_side="DOWN",
        selected_bid=0.46,
        selected_ask=0.47,
        selected_mid=0.465,
        selected_spread=0.01,
        selected_trade_value=0.505,
        selected_policy_edge=0.035,
        selected_trade_style="continuation",
        entry_edge_threshold=0.04,
        entry_session_enabled=True,
        threshold_bps=2.0,
    )

    bot._manage_pending_entry(datetime(2026, 7, 15, 16, 11, 21, 500_000, tzinfo=timezone.utc), {}, decision)

    assert cancelled == ["POLICY_SIDE_CHANGED"]
    assert bot.pending_entry is None


def test_pending_fast_limit_keeps_same_side_policy_key_change_when_resting_edge_survives() -> None:
    bot = object.__new__(StatsBot)
    bot.config = type(
        "Config",
        (),
        {
            "paper_trading": True,
            "paper_fill_model": "fast_limit",
            "stake_mode": "min_order_size",
            "entry_quote_max_age_seconds": 999.0,
            "entry_resting_max_age_seconds": 2.0,
            "policy_no_longer_viable_latch_seconds": 1.5,
        },
    )()
    bot.pending_entry = PendingEntryOrder(
        order_id="0xkeychange",
        token_id="up",
        side="BUY",
        price=0.41,
        target_shares=5.0,
        created_at_utc=datetime(2026, 7, 15, 16, 11, 21, tzinfo=timezone.utc),
        market_slug="btc-updown-5m-test",
        policy_key_text="weekday|old_bucket|UP",
        policy_side="UP",
        snapshot={"entry_execution_mode": "presigned_fast_limit", "entry_required_edge_at_limit": 0.04},
        target_notional=2.05,
    )
    kept: list[tuple] = []
    bot._fast_entry_quote = lambda *_args: None
    bot._cancel_pending_entry = lambda reason: (_ for _ in ()).throw(AssertionError(f"unexpected cancel: {reason}"))
    bot._log_latency = lambda *args, **kwargs: kept.append((args, kwargs))

    decision = EntryDecision(
        viable=True,
        blockers=(),
        policy_key=("weekday", "new_bucket", "UP"),
        policy_record=None,
        selected_side="UP",
        selected_bid=0.41,
        selected_ask=0.42,
        selected_mid=0.415,
        selected_spread=0.01,
        selected_trade_value=0.465,
        selected_policy_edge=0.045,
        selected_trade_style="continuation",
        entry_edge_threshold=0.04,
        entry_session_enabled=True,
        threshold_bps=2.0,
    )

    bot._manage_pending_entry(datetime(2026, 7, 15, 16, 11, 21, 750_000, tzinfo=timezone.utc), {}, decision)

    assert bot.pending_entry is not None
    assert bot.pending_entry.order_id == "0xkeychange"
    assert any(kwargs.get("detail", "").startswith("policy_key_changed_kept") for _, kwargs in kept)


def test_pending_maker_order_keeps_price_change_when_resting_edge_survives() -> None:
    bot = object.__new__(StatsBot)
    bot.config = type(
        "Config",
        (),
        {
            "paper_trading": True,
            "paper_fill_model": "fast_limit",
            "stake_mode": "min_order_size",
            "entry_quote_max_age_seconds": 999.0,
            "entry_resting_max_age_seconds": 2.0,
            "policy_no_longer_viable_latch_seconds": 1.5,
        },
    )()
    bot.pending_entry = PendingEntryOrder(
        order_id="0xmakerkeep",
        token_id="up",
        side="BUY",
        price=0.41,
        target_shares=5.0,
        created_at_utc=datetime(2026, 7, 15, 16, 11, 21, tzinfo=timezone.utc),
        market_slug="btc-updown-5m-test",
        policy_key_text="weekday|p40_to_p45|UP",
        policy_side="UP",
        snapshot={"entry_execution_mode": "maker_post_only", "entry_required_edge_at_limit": 0.04},
        target_notional=2.05,
    )
    kept: list[tuple] = []
    bot._maker_quote = lambda _side: (0.42, 1, None)
    bot._cancel_pending_entry = lambda reason: (_ for _ in ()).throw(AssertionError(f"unexpected cancel: {reason}"))
    bot._request_entry = lambda *_args: (_ for _ in ()).throw(AssertionError("unexpected re-request"))
    bot._log_latency = lambda *args, **kwargs: kept.append((args, kwargs))
    bot._log_event = lambda *args, **kwargs: None

    decision = EntryDecision(
        viable=True,
        blockers=(),
        policy_key=("weekday", "p40_to_p45", "UP"),
        policy_record=None,
        selected_side="UP",
        selected_bid=0.41,
        selected_ask=0.42,
        selected_mid=0.415,
        selected_spread=0.01,
        selected_trade_value=0.465,
        selected_policy_edge=0.045,
        selected_trade_style="continuation",
        entry_edge_threshold=0.04,
        entry_session_enabled=True,
        threshold_bps=2.0,
    )

    bot._manage_pending_entry(datetime(2026, 7, 15, 16, 11, 21, 750_000, tzinfo=timezone.utc), {}, decision)

    assert bot.pending_entry is not None
    assert bot.pending_entry.order_id == "0xmakerkeep"
    assert any(kwargs.get("detail", "").startswith("maker_price_changed_kept") for _, kwargs in kept)


def test_pending_fast_limit_cancels_and_retries_when_ask_stale_for_grace_period() -> None:
    bot = object.__new__(StatsBot)
    bot.config = type(
        "Config",
        (),
        {
            "paper_trading": True,
            "paper_fill_model": "fast_limit",
            "stake_mode": "min_order_size",
            "entry_quote_max_age_seconds": 999.0,
            "entry_resting_max_age_seconds": 2.0,
            "policy_no_longer_viable_latch_seconds": 1.5,
            "entry_stale_quote_max_ticks": 2,
            "entry_stale_quote_grace_seconds": 0.4,
        },
    )()
    started = datetime(2026, 7, 15, 16, 11, 21, tzinfo=timezone.utc)
    bot.pending_entry = PendingEntryOrder(
        order_id="0xstaleask",
        token_id="up",
        side="BUY",
        price=0.21,
        target_shares=5.0,
        created_at_utc=started,
        market_slug="btc-updown-5m-test",
        policy_key_text="policy|UP",
        policy_side="UP",
        snapshot={"entry_execution_mode": "presigned_fast_limit", "entry_required_edge_at_limit": 0.04},
        target_notional=1.05,
        stale_quote_started_at_utc=started + timedelta(milliseconds=100),
    )
    bot.active_position = None
    bot.entry_fills_by_window = {}
    bot._side_book = lambda _side: BookSnapshot(
        bid=0.23,
        ask=0.24,
        mid=0.235,
        spread=0.01,
        bid_depth_total=None,
        ask_depth_total=None,
        best_bid_depth=None,
        best_ask_depth=None,
        captured_at_utc=started,
    )
    bot._fast_entry_quote = lambda *_args: None
    bot._log_latency = lambda *args, **kwargs: None
    cancelled: list[str] = []
    requested: list[bool] = []

    def cancel(reason: str) -> None:
        cancelled.append(reason)
        bot.pending_entry = None

    def request(*_args, ignore_retry_cooldown: bool = False) -> None:
        requested.append(ignore_retry_cooldown)

    bot._cancel_pending_entry = cancel
    bot._request_entry = request
    decision = EntryDecision(
        viable=True,
        blockers=(),
        policy_key=("policy", "UP"),
        policy_record=None,
        selected_side="UP",
        selected_bid=0.20,
        selected_ask=0.21,
        selected_mid=0.205,
        selected_spread=0.01,
        selected_trade_value=0.28,
        selected_policy_edge=0.07,
        selected_trade_style="continuation",
        entry_edge_threshold=0.04,
        entry_session_enabled=True,
        threshold_bps=0.0,
    )

    bot._manage_pending_entry(started + timedelta(milliseconds=501), {}, decision)

    assert cancelled == ["ENTRY_STALE_QUOTE_DISTANCE"]
    assert requested == [True]


def test_pending_maker_order_uses_same_stale_ask_retry_mechanics() -> None:
    bot = object.__new__(StatsBot)
    bot.config = type(
        "Config",
        (),
        {
            "paper_trading": True,
            "paper_fill_model": "fast_limit",
            "stake_mode": "min_order_size",
            "entry_quote_max_age_seconds": 999.0,
            "entry_resting_max_age_seconds": 2.0,
            "policy_no_longer_viable_latch_seconds": 1.5,
            "entry_stale_quote_max_ticks": 2,
            "entry_stale_quote_grace_seconds": 0.4,
        },
    )()
    started = datetime(2026, 7, 15, 16, 11, 21, tzinfo=timezone.utc)
    bot.pending_entry = PendingEntryOrder(
        order_id="0xmakerstaleask",
        token_id="up",
        side="BUY",
        price=0.21,
        target_shares=5.0,
        created_at_utc=started,
        market_slug="btc-updown-5m-test",
        policy_key_text="policy|UP",
        policy_side="UP",
        snapshot={"entry_execution_mode": "maker_post_only", "entry_required_edge_at_limit": 0.04},
        target_notional=1.05,
        stale_quote_started_at_utc=started,
    )
    bot.active_position = None
    bot.entry_fills_by_window = {}
    bot._side_book = lambda _side: BookSnapshot(
        bid=0.23,
        ask=0.24,
        mid=0.235,
        spread=0.01,
        bid_depth_total=None,
        ask_depth_total=None,
        best_bid_depth=None,
        best_ask_depth=None,
        captured_at_utc=started,
    )
    bot._log_latency = lambda *args, **kwargs: None
    cancelled: list[str] = []
    requested: list[bool] = []

    def cancel(reason: str) -> None:
        cancelled.append(reason)
        bot.pending_entry = None

    bot._cancel_pending_entry = cancel
    bot._request_entry = lambda *_args, ignore_retry_cooldown=False: requested.append(ignore_retry_cooldown)
    decision = EntryDecision(
        viable=True,
        blockers=(),
        policy_key=("policy", "UP"),
        policy_record=None,
        selected_side="UP",
        selected_bid=0.20,
        selected_ask=0.21,
        selected_mid=0.205,
        selected_spread=0.01,
        selected_trade_value=0.28,
        selected_policy_edge=0.07,
        selected_trade_style="continuation",
        entry_edge_threshold=0.04,
        entry_session_enabled=True,
        threshold_bps=0.0,
    )

    bot._manage_pending_entry(started + timedelta(milliseconds=450), {}, decision)

    assert cancelled == ["ENTRY_STALE_QUOTE_DISTANCE"]
    assert requested == [True]


def test_pending_entry_keeps_ask_within_two_ticks_and_resets_stale_timer() -> None:
    bot = object.__new__(StatsBot)
    bot.config = type(
        "Config",
        (),
        {
            "paper_trading": True,
            "paper_fill_model": "fast_limit",
            "stake_mode": "min_order_size",
            "entry_quote_max_age_seconds": 999.0,
            "entry_resting_max_age_seconds": 2.0,
            "policy_no_longer_viable_latch_seconds": 1.5,
            "entry_stale_quote_max_ticks": 2,
            "entry_stale_quote_grace_seconds": 0.4,
        },
    )()
    started = datetime(2026, 7, 15, 16, 11, 21, tzinfo=timezone.utc)
    bot.pending_entry = PendingEntryOrder(
        order_id="0xnearask",
        token_id="up",
        side="BUY",
        price=0.21,
        target_shares=5.0,
        created_at_utc=started,
        market_slug="btc-updown-5m-test",
        policy_key_text="policy|UP",
        policy_side="UP",
        snapshot={"entry_execution_mode": "presigned_fast_limit", "entry_required_edge_at_limit": 0.04},
        target_notional=1.05,
        stale_quote_started_at_utc=started,
    )
    bot.entry_fills_by_window = {}
    bot._side_book = lambda _side: BookSnapshot(
        bid=0.22,
        ask=0.23,
        mid=0.225,
        spread=0.01,
        bid_depth_total=None,
        ask_depth_total=None,
        best_bid_depth=None,
        best_ask_depth=None,
        captured_at_utc=started,
    )
    bot._fast_entry_quote = lambda *_args: None
    bot._cancel_pending_entry = lambda reason: (_ for _ in ()).throw(AssertionError(f"unexpected cancel: {reason}"))
    bot._request_entry = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected re-request"))
    bot._log_latency = lambda *args, **kwargs: None
    decision = EntryDecision(
        viable=True,
        blockers=(),
        policy_key=("policy", "UP"),
        policy_record=None,
        selected_side="UP",
        selected_bid=0.20,
        selected_ask=0.21,
        selected_mid=0.205,
        selected_spread=0.01,
        selected_trade_value=0.28,
        selected_policy_edge=0.07,
        selected_trade_style="continuation",
        entry_edge_threshold=0.04,
        entry_session_enabled=True,
        threshold_bps=0.0,
    )

    bot._manage_pending_entry(started + timedelta(milliseconds=750), {}, decision)

    assert bot.pending_entry is not None
    assert bot.pending_entry.stale_quote_started_at_utc is None


def test_pending_stale_ask_cancel_does_not_retry_when_cancel_finds_fill() -> None:
    bot = object.__new__(StatsBot)
    bot.config = type(
        "Config",
        (),
        {
            "paper_trading": True,
            "paper_fill_model": "fast_limit",
            "stake_mode": "min_order_size",
            "entry_quote_max_age_seconds": 999.0,
            "entry_resting_max_age_seconds": 2.0,
            "policy_no_longer_viable_latch_seconds": 1.5,
            "entry_stale_quote_max_ticks": 2,
            "entry_stale_quote_grace_seconds": 0.4,
        },
    )()
    started = datetime(2026, 7, 15, 16, 11, 21, tzinfo=timezone.utc)
    bot.pending_entry = PendingEntryOrder(
        order_id="0xfillduringcancel",
        token_id="up",
        side="BUY",
        price=0.21,
        target_shares=5.0,
        created_at_utc=started,
        market_slug="btc-updown-5m-test",
        policy_key_text="policy|UP",
        policy_side="UP",
        snapshot={"entry_execution_mode": "presigned_fast_limit", "entry_required_edge_at_limit": 0.04},
        target_notional=1.05,
        stale_quote_started_at_utc=started,
    )
    bot.active_position = None
    bot.entry_fills_by_window = {}
    bot._side_book = lambda _side: BookSnapshot(
        bid=0.23,
        ask=0.24,
        mid=0.235,
        spread=0.01,
        bid_depth_total=None,
        ask_depth_total=None,
        best_bid_depth=None,
        best_ask_depth=None,
        captured_at_utc=started,
    )
    bot._log_latency = lambda *args, **kwargs: None
    requested: list[bool] = []

    def cancel(_reason: str) -> None:
        bot.pending_entry = None
        bot.entry_fills_by_window["btc-updown-5m-test"] = 1

    bot._cancel_pending_entry = cancel
    bot._request_entry = lambda *_args, ignore_retry_cooldown=False: requested.append(ignore_retry_cooldown)
    decision = EntryDecision(
        viable=True,
        blockers=(),
        policy_key=("policy", "UP"),
        policy_record=None,
        selected_side="UP",
        selected_bid=0.20,
        selected_ask=0.21,
        selected_mid=0.205,
        selected_spread=0.01,
        selected_trade_value=0.28,
        selected_policy_edge=0.07,
        selected_trade_style="continuation",
        entry_edge_threshold=0.04,
        entry_session_enabled=True,
        threshold_bps=0.0,
    )

    bot._manage_pending_entry(started + timedelta(milliseconds=450), {}, decision)

    assert requested == []


def test_pending_fast_limit_latches_short_no_longer_viable_same_side() -> None:
    bot = object.__new__(StatsBot)
    bot.config = type(
        "Config",
        (),
        {
            "paper_trading": True,
            "paper_fill_model": "fast_limit",
            "stake_mode": "min_order_size",
            "entry_quote_max_age_seconds": 999.0,
        },
    )()
    bot.pending_entry = PendingEntryOrder(
        order_id="0xlatch",
        token_id="up",
        side="BUY",
        price=0.56,
        target_shares=5.0,
        created_at_utc=datetime(2026, 7, 15, 16, 11, 21, tzinfo=timezone.utc),
        market_slug="btc-updown-5m-test",
        policy_key_text="policy|UP",
        policy_side="UP",
        snapshot={"entry_execution_mode": "presigned_fast_limit"},
        target_notional=2.8,
    )
    kept: list[tuple] = []
    bot._fast_entry_quote = lambda *_args: None
    bot._cancel_pending_entry = lambda reason: (_ for _ in ()).throw(AssertionError(f"unexpected cancel: {reason}"))
    bot._log_latency = lambda *args, **kwargs: kept.append((args, kwargs))

    decision = EntryDecision(
        viable=False,
        blockers=("POLICY_NO_SIDE_EDGE",),
        policy_key=("policy", "UP"),
        policy_record=None,
        selected_side="UP",
        selected_bid=0.55,
        selected_ask=0.56,
        selected_mid=0.555,
        selected_spread=0.01,
        selected_trade_value=0.575,
        selected_policy_edge=0.015,
        selected_trade_style="continuation",
        entry_edge_threshold=0.05,
        entry_session_enabled=True,
        threshold_bps=0.0,
    )

    bot._manage_pending_entry(datetime(2026, 7, 15, 16, 11, 22, tzinfo=timezone.utc), {}, decision)

    assert bot.pending_entry is not None
    assert bot.pending_entry.order_id == "0xlatch"
    assert any(kwargs.get("detail", "").startswith("policy_no_longer_viable_latch") for _, kwargs in kept)


def test_pending_fast_limit_hard_cancels_after_resting_max_age() -> None:
    bot = object.__new__(StatsBot)
    bot.config = type(
        "Config",
        (),
        {
            "paper_trading": True,
            "paper_fill_model": "fast_limit",
            "stake_mode": "min_order_size",
            "entry_quote_max_age_seconds": 999.0,
            "entry_resting_max_age_seconds": 1.0,
        },
    )()
    bot.pending_entry = PendingEntryOrder(
        order_id="0xoldresting",
        token_id="up",
        side="BUY",
        price=0.56,
        target_shares=5.0,
        created_at_utc=datetime(2026, 7, 15, 16, 11, 21, tzinfo=timezone.utc),
        market_slug="btc-updown-5m-test",
        policy_key_text="policy|UP",
        policy_side="UP",
        snapshot={"entry_execution_mode": "presigned_fast_limit", "entry_required_edge_at_limit": 0.05},
        target_notional=2.8,
    )
    cancelled: list[str] = []

    def cancel(reason: str) -> None:
        cancelled.append(reason)
        bot.pending_entry = None

    bot._fast_entry_quote = lambda *_args: None
    bot._cancel_pending_entry = cancel
    bot._log_latency = lambda *args, **kwargs: None
    decision = EntryDecision(
        viable=True,
        blockers=(),
        policy_key=("policy", "UP"),
        policy_record=None,
        selected_side="UP",
        selected_bid=0.55,
        selected_ask=0.56,
        selected_mid=0.555,
        selected_spread=0.01,
        selected_trade_value=0.625,
        selected_policy_edge=0.065,
        selected_trade_style="continuation",
        entry_edge_threshold=0.05,
        entry_session_enabled=True,
        threshold_bps=0.0,
    )

    bot._manage_pending_entry(datetime(2026, 7, 15, 16, 11, 22, 1_000, tzinfo=timezone.utc), {}, decision)

    assert cancelled == ["ENTRY_RESTING_MAX_AGE"]
    assert bot.pending_entry is None


def test_pending_fast_limit_no_longer_viable_latch_expires_after_one_and_half_seconds() -> None:
    bot = object.__new__(StatsBot)
    bot.config = type(
        "Config",
        (),
        {
            "paper_trading": True,
            "paper_fill_model": "fast_limit",
            "stake_mode": "min_order_size",
            "entry_quote_max_age_seconds": 999.0,
            "entry_resting_max_age_seconds": 2.0,
            "policy_no_longer_viable_latch_seconds": 1.5,
        },
    )()
    bot.pending_entry = PendingEntryOrder(
        order_id="0xexpired",
        token_id="up",
        side="BUY",
        price=0.56,
        target_shares=5.0,
        created_at_utc=datetime(2026, 7, 15, 16, 11, 21, tzinfo=timezone.utc),
        market_slug="btc-updown-5m-test",
        policy_key_text="policy|UP",
        policy_side="UP",
        snapshot={"entry_execution_mode": "presigned_fast_limit"},
        target_notional=2.8,
    )
    cancelled: list[str] = []

    def cancel(reason: str) -> None:
        cancelled.append(reason)
        bot.pending_entry = None

    bot._fast_entry_quote = lambda *_args: None
    bot._cancel_pending_entry = cancel
    bot._log_latency = lambda *args, **kwargs: None
    decision = EntryDecision(
        viable=False,
        blockers=("POLICY_NO_SIDE_EDGE",),
        policy_key=("policy", "UP"),
        policy_record=None,
        selected_side="UP",
        selected_bid=0.55,
        selected_ask=0.56,
        selected_mid=0.555,
        selected_spread=0.01,
        selected_trade_value=0.575,
        selected_policy_edge=0.015,
        selected_trade_style="continuation",
        entry_edge_threshold=0.05,
        entry_session_enabled=True,
        threshold_bps=0.0,
    )

    bot._manage_pending_entry(datetime(2026, 7, 15, 16, 11, 22, 600_000, tzinfo=timezone.utc), {}, decision)

    assert cancelled == ["POLICY_EDGE_NO_LONGER_VALID"]
    assert bot.pending_entry is None


def test_snapshot_continues_inside_last_60_seconds_even_when_policy_mutes() -> None:
    market_start = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    now_utc = market_start + timedelta(seconds=250)
    btc_history = []
    pm_history = []
    for offset in range(251):
        ts = market_start + timedelta(seconds=offset)
        btc_history.append(
            {
                "timestamp_utc": ts,
                "open_1s": 100000.0 + offset,
                "high_1s": 100000.5 + offset,
                "low_1s": 99999.5 + offset,
                "close_1s": 100000.0 + offset,
                "volume_1s": 1.0,
            }
        )
        pm_history.append(
            {
                "timestamp_utc": ts,
                "up_bid": 0.49,
                "up_ask": 0.50,
                "up_mid": 0.495,
                "up_spread": 0.01,
                "up_bid_depth_total": 10.0,
                "up_ask_depth_total": 12.0,
                "up_best_bid_depth": 5.0,
                "up_best_ask_depth": 6.0,
                "down_bid": 0.49,
                "down_ask": 0.50,
                "down_mid": 0.495,
                "down_spread": 0.01,
                "down_bid_depth_total": 9.0,
                "down_ask_depth_total": 11.0,
                "down_best_bid_depth": 4.0,
                "down_best_ask_depth": 5.0,
            }
        )

    snapshot = compute_live_snapshot(
        btc_history=pd.DataFrame(btc_history),
        pm_history=pd.DataFrame(pm_history),
        now_utc=now_utc,
        market_start_utc=market_start,
        market_end_utc=market_start + timedelta(seconds=300),
        window_open_btc_price=100000.0,
        volatility_edges=VolatilityEdges(
            smoothing_seconds=30,
            global_p33=0.1,
            global_p66=0.2,
            partitions={},
        ),
        market_slug="btc-updown-5m-test",
    )

    assert snapshot is not None
    assert snapshot["canonical_tte_seconds"] == 50.0
    assert snapshot["tte_bucket"] is None
    assert snapshot["up_mid"] == 0.495

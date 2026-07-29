from __future__ import annotations

import argparse
import collections
import csv
import logging
import math
import queue
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .binance import BinanceTickerFeed, adverse_signed_volume, adverse_z
from .coinbase import CoinbaseFeed
from .config import REPO_ROOT, BotConfig, build_config, parse_args
from .features import compute_live_snapshot
from .guards import DrawdownGuard, BpsYesGuard, SessionStyleDrawdownGuard
from .macro_policy import MacroLocalPolicy
from .models import HedgeLeg, OpenPosition, PendingEntryOrder, RuntimeEvent
from .polymarket import BookSnapshot, MarketInfo, OrderResult, PolymarketClient, SignedBuyOrder, WINDOW_SECONDS, round_price
from .polymarket_ref import PolymarketReferenceFeed
from .strategy import (
    EntryDecision,
    ExitDecision,
    PolicyCube,
    build_policy_key,
    evaluate_exit,
    finite_float,
    load_exit_thresholds,
    load_policy_cube,
    load_volatility_edges,
    trade_style_from_bps_bucket,
)


LINE = "=" * 112
LINE2 = "-" * 112
TZ_LONDON = "Europe/London"
LONDON_ZONE = ZoneInfo(TZ_LONDON)
MIN_ORDERABLE_SHARES = 0.01
CRYPTO_TAKER_FEE_RATE_DEFAULT = 0.07
DYNAMIC_SHARE_TIERS: tuple[tuple[float, float], ...] = (
    (0.0, 5.0),
    (50.0, 6.0),
    (60.0, 7.0),
    (70.0, 8.0),
    (85.0, 9.0),
    (100.0, 10.0),
    (125.0, 12.0),
    (150.0, 15.0),
)
LADDER_SHARE_TIERS: tuple[tuple[float, float, float | None], ...] = (
    (0.0, 5.0, None),
    (100.0, 6.0, 85.0),
    (150.0, 7.0, 130.0),
    (200.0, 9.0, 175.0),
    (250.0, 11.0, 220.0),
    (300.0, 13.0, 265.0),
    (400.0, 17.0, 360.0),
    (500.0, 20.0, 450.0),
    (750.0, 26.0, 675.0),
    (1000.0, 32.0, 900.0),
    (1500.0, 45.0, 1350.0),
    (2000.0, 57.0, 1800.0),
    (2500.0, 70.0, 2250.0),
    (3000.0, 82.0, 2700.0),
    (4000.0, 107.0, 3600.0),
    (5000.0, 132.0, 4500.0),
    (6000.0, 157.0, 5400.0),
    (7000.0, 182.0, 6300.0),
    (7500.0, 195.0, 6750.0),
    (8000.0, 207.0, 7200.0),
    (9000.0, 232.0, 8100.0),
    (10000.0, 257.0, 9000.0),
)
STAKE_DRAWDOWN_DISABLE_BONUS_PCT = 5.0
STAKE_DRAWDOWN_FORCE_MIN_PCT = 8.0
STAKE_DRAWDOWN_RECOVERY_PCT = 14.0
STAKE_DRAWDOWN_RECOVERY_EXIT_PCT = 8.0
SIDE_MOMENTUM_GUARD_WINDOW_SECONDS = 20
SIDE_MOMENTUM_GUARD_THRESHOLD_BPS = 2.5
PRESIGN_ENTRY_TOLERANCE_TICKS = 1
DEGRADED_ENTRY_TOLERANCE_TICKS = 1
POOR_ENTRY_TOLERANCE_TICKS = 1
DEGRADED_ENTRY_EDGE_BUMP = 0.01
POOR_ENTRY_EDGE_BUMP = 0.02
POLICY_NO_LONGER_VIABLE_LATCH_SECONDS = 1.0
POLICY_NO_LONGER_VIABLE_LATCH_MIN_EDGE = 0.01
EXECUTION_LATE_WARNING_SECONDS = 1.0
EXECUTION_HEALTH_MIN_SAMPLES = 5
EXECUTION_HEALTH_WINDOW = 10
ENTRY_LIFECYCLE_BLOCKING_STATES = {"SUBMITTING", "UNKNOWN", "RESTING", "FILLED"}
SESSION_STYLE_DRAWDOWN_PER_SHARE_USD = 1.5


def _order_status_is_fill_like(status: object | None) -> bool:
    return str(status or "").strip().lower() in {"filled", "matched"}


class _LondonFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc)
        london = ts.astimezone(LONDON_ZONE)
        return f"{london.strftime('%H:%M:%S')} {london.strftime('%Z')}"


def _setup_logging() -> logging.Logger:
    handler = logging.StreamHandler()
    handler.setFormatter(_LondonFormatter("%(asctime)s  %(message)s"))
    logging.root.handlers = [handler]
    logging.root.setLevel(logging.INFO)
    for noisy in ("urllib3", "requests", "websocket", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    for noisy in ("py_clob_client_v2", "py_clob_client_v2.client", "py_clob_client_v2.http_helpers.helpers"):
        logging.getLogger(noisy).setLevel(logging.CRITICAL)
    return logging.getLogger("stats_bot")


log = _setup_logging()


@dataclass(frozen=True)
class DynamicStakeDecision:
    target_shares: float
    bankroll: float
    high_water: float
    drawdown_pct: float
    recovery_active: bool = False
    recovery_anchor: float | None = None
    recovery_floor: float | None = None
    reason: str | None = None


@dataclass
class RegimeRecoveryState:
    recent_pnls: collections.deque[float] = field(default_factory=collections.deque)
    active_until_utc: datetime | None = None
    forced_block_until_utc: datetime | None = None
    trigger_pnl: float | None = None
    trigger_count: int = 0


@dataclass
class PresignLadderState:
    market_slug: str | None = None
    status: str = "idle"
    started_at_utc: datetime | None = None
    completed_at_utc: datetime | None = None
    elapsed_seconds: float | None = None
    min_price: float | None = None
    max_price: float | None = None
    total_orders: int = 0
    signed_orders: int = 0
    error_count: int = 0
    message: str = ""
    generation: int = 0
    orders: dict[tuple[str, float, float], SignedBuyOrder] = field(default_factory=dict)


@dataclass(frozen=True)
class FastEntryQuote:
    price: float
    shares: float
    target_notional: float
    min_order_size: float
    spread_ticks: int | None
    book: BookSnapshot
    edge_at_ask: float
    edge_at_limit: float
    required_edge: float
    execution_health_status: str = "Healthy"
    tolerance_ticks: int = PRESIGN_ENTRY_TOLERANCE_TICKS


@dataclass(frozen=True)
class EntrySubmission:
    submission_id: str
    market_slug: str
    token_id: str
    policy_side: str
    market: MarketInfo
    signed_order: SignedBuyOrder | None
    execution_mode: str
    quote: FastEntryQuote
    snapshot: dict[str, Any]
    policy_key_text: str
    wallet_balance_before: float | None
    attempt_count: int
    decision_at_utc: datetime
    submitted_at_utc: datetime


@dataclass(frozen=True)
class EntrySubmissionResult:
    submission: EntrySubmission
    result: OrderResult
    worker_started_at_utc: datetime
    completed_at_utc: datetime
    queue_delay_ms: float
    submit_elapsed_ms: float


def _fmt(value: object, digits: int = 4, blank: str = "NA") -> str:
    number = finite_float(value)
    if number is None:
        return blank
    return f"{number:.{digits}f}"


def _event_fields(fields: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in fields.items():
        if isinstance(value, datetime):
            clean[key] = value.isoformat()
        else:
            clean[key] = value
    return clean


def _policy_support_level(snapshot: dict[str, Any] | None) -> tuple[object, object]:
    values = snapshot or {}
    return values.get("policy_support", "NA"), values.get("policy_level", "NA")


def _submission_fill_attempt_counts(
    fills_by_window: dict[str, int], attempts_by_window: dict[str, int]
) -> tuple[int, int]:
    fills = sum(1 for count in fills_by_window.values() if int(count) > 0)
    attempts = sum(max(0, int(count)) for count in attempts_by_window.values())
    return fills, attempts


def _valid_latency_values(values: collections.deque[float]) -> list[float]:
    return [float(value) for value in values if math.isfinite(float(value)) and float(value) >= 0.0]


def _latency_percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(fraction * len(ordered)) - 1]


def _execution_health_status(clob_ack_ms: collections.deque[float]) -> str:
    clob = _valid_latency_values(clob_ack_ms)[-EXECUTION_HEALTH_WINDOW:]
    if len(clob) < EXECUTION_HEALTH_MIN_SAMPLES:
        return "Warming"
    clob_p50 = _latency_percentile(clob, 0.50)
    slow_over_1s = sum(value > 1000.0 for value in clob)
    slow_over_1_5s = sum(value > 1500.0 for value in clob)
    if clob_p50 > 800.0 or slow_over_1_5s >= 2 or max(clob) > 3000.0:
        return "Poor"
    if clob_p50 >= 450.0 or slow_over_1s >= 2:
        return "Degraded"
    return "Healthy"


def _entry_tolerance_ticks_for_execution_health(status: str) -> int:
    if status == "Poor":
        return POOR_ENTRY_TOLERANCE_TICKS
    if status == "Degraded":
        return DEGRADED_ENTRY_TOLERANCE_TICKS
    return PRESIGN_ENTRY_TOLERANCE_TICKS


def _required_limit_edge_for_execution_health(status: str, base_required_edge: float) -> float:
    if status == "Poor":
        return base_required_edge + POOR_ENTRY_EDGE_BUMP
    if status == "Degraded":
        return base_required_edge + DEGRADED_ENTRY_EDGE_BUMP
    return base_required_edge


def _execution_health_text(
    client_pipeline_ms: collections.deque[float],
    clob_ack_ms: collections.deque[float],
    decision_to_response_ms: collections.deque[float],
) -> str:
    """One concise health line, with local, venue, and end-to-end latency separated."""

    client = _valid_latency_values(client_pipeline_ms)[-EXECUTION_HEALTH_WINDOW:]
    clob = _valid_latency_values(clob_ack_ms)[-EXECUTION_HEALTH_WINDOW:]
    path = _valid_latency_values(decision_to_response_ms)[-EXECUTION_HEALTH_WINDOW:]
    count = len(clob)
    if count == 0:
        return "Warming (0/5 POSTs) | client: NA | CLOB: NA | path: NA"

    client_p95 = _latency_percentile(client, 0.95) if client else 0.0
    clob_p50 = _latency_percentile(clob, 0.50)
    clob_p90 = _latency_percentile(clob, 0.90)
    path_p90 = _latency_percentile(path, 0.90) if path else 0.0
    summary = (
        f"client P95 {client_p95:.0f}ms | CLOB P50/P90 {clob_p50:.0f}/{clob_p90:.0f}ms "
        f"| path P90 {path_p90:.0f}ms"
    )
    if count < EXECUTION_HEALTH_MIN_SAMPLES:
        return f"Warming ({count}/{EXECUTION_HEALTH_MIN_SAMPLES} POSTs) | {summary}"
    status = _execution_health_status(clob_ack_ms)
    return f"{status} ({count}/10 POSTs) | {summary}"


class StatsBot:
    def __init__(self, config: BotConfig):
        self.config = config
        t0 = time.time()
        log.info("  init: loading macro/local production policy...")
        self.macro_policy = MacroLocalPolicy(config.macro_policy_bundle)
        log.info(f"  init: macro/local policy loaded in {time.time() - t0:.2f}s")
        expected = self.macro_policy.entry
        configured_shape = (
            config.min_entry_price,
            config.max_entry_price,
            config.weekend_min_entry_price,
            config.weekend_max_entry_price,
            config.entry_min_tte_seconds,
            config.entry_max_tte_seconds,
            config.entry_start_block_seconds,
        )
        tested_shape = (
            float(expected["min_price"]),
            float(expected["max_price"]),
            float(expected["min_price"]),
            float(expected["max_price"]),
            int(expected["min_tte"]),
            int(expected["max_tte"]),
            45,
        )
        if configured_shape != tested_shape:
            raise ValueError(f"Runtime entry shape {configured_shape} does not match frozen policy {tested_shape}")
        if config.stake_mode not in {"min_order_size", "ladder_shares"} or config.min_order_size_fallback != float(expected["shares"]):
            raise ValueError(
                f"{self.macro_policy.strategy_id} requires a 5-share fallback-compatible stake mode"
            )
        t0 = time.time()
        if config.exit_policy_mode == "hold_to_resolution":
            log.info("  init: legacy exit-policy support skipped (hold_to_resolution)...")
            self.policy_cube = PolicyCube({})
        else:
            log.info("  init: loading legacy exit-policy support...")
            self.policy_cube = load_policy_cube(config.policy_file)
            log.info(f"  init: legacy exit support loaded in {time.time() - t0:.2f}s")
        t0 = time.time()
        log.info("  init: loading active volatility support...")
        self.exit_thresholds = (
            load_exit_thresholds(config.exit_thresholds_file)
            if config.exit_policy_mode != "hold_to_resolution"
            else {}
        )
        self.entry_edge_thresholds = {}
        self.volatility_edges = load_volatility_edges(config.volatility_edges_file)
        log.info(f"  init: active volatility support loaded in {time.time() - t0:.2f}s")

        self.balance = config.starting_balance
        self.wallet_balance_live: float | None = None
        self.wallet_balance_start_live: float | None = None
        self._last_balance_refresh_at = 0.0
        self.realized_pnl = 0.0
        self.session_pnl = 0.0
        self.total_wins = 0
        self.total_losses = 0
        self.trade_style_wins: collections.Counter[str] = collections.Counter()
        self.trade_style_losses: collections.Counter[str] = collections.Counter()
        self.staking_high_water: float | None = None
        self.staking_positive_streak_pnl = 0.0
        self.ladder_stake_tier_index: int | None = None
        self.ladder_pending_tier_index: int | None = None
        self.recovery_active = False
        self.recovery_anchor_bankroll: float | None = None
        self.regime_recovery = RegimeRecoveryState()
        self.last_staking_decision: DynamicStakeDecision | None = None
        self.presign_ladder = PresignLadderState(status="disabled" if config.paper_trading else "idle")
        self._presign_lock = threading.Lock()
        self._presign_generation = 0

        t0 = time.time()
        log.info(f"  init: creating Polymarket client ({'live' if not config.paper_trading else 'paper'})...")
        self.pm = PolymarketClient(env_path=REPO_ROOT / ".env", live=not config.paper_trading)
        log.info(f"  init: Polymarket client ready in {time.time() - t0:.2f}s")
        self._execution_wakeup = threading.Event()
        self._stream_events: queue.SimpleQueue[tuple[str, datetime, dict[str, Any]]] = queue.SimpleQueue()
        self._entry_submission_results: queue.SimpleQueue[EntrySubmissionResult] = queue.SimpleQueue()
        self._entry_submission_queue: queue.Queue[EntrySubmission | None] = queue.Queue(maxsize=1)
        self._entry_submission_lock = threading.Lock()
        self._entry_submission: EntrySubmission | None = None
        self._entry_submission_state = "IDLE"
        self._entry_submission_late_logged = False
        self._execution_worker_stop = threading.Event()
        self._execution_worker_ready = threading.Event()
        self._execution_worker_thread: threading.Thread | None = None
        self._latency_log_lock = threading.Lock()
        self._last_execution_event_at: datetime | None = None
        self._last_policy_trigger: tuple[str, datetime, datetime | None] | None = None
        self.coinbase = CoinbaseFeed(on_update=self._on_execution_event)
        self.binance_spot = BinanceTickerFeed(
            name="binance_spot",
            url="wss://stream.binance.com:9443/ws/btcusdt@trade",
            on_update=self._on_execution_event if config.binance_spot_trigger_enabled else None,
        )
        self.binance_perp = BinanceTickerFeed(
            name="binance_perp",
            url="wss://fstream.binance.com/ws/btcusdt@trade",
            on_update=self._on_execution_event if config.binance_perp_trigger_enabled else None,
        )
        self.pm_chainlink = PolymarketReferenceFeed(on_update=self._on_execution_event)
        self.guard = BpsYesGuard()
        self.drawdown_guard = DrawdownGuard()
        self.session_style_drawdown_guard = SessionStyleDrawdownGuard(
            drawdown_per_share_usd=SESSION_STYLE_DRAWDOWN_PER_SHARE_USD
        )
        self.peak_realized_pnl = 0.0
        self.peak_drawdown_usd = 0.0
        self.peak_drawdown_kill_active = False
        self.peak_drawdown_kill_triggered_at: datetime | None = None
        self.current_market: MarketInfo | None = None
        self.current_window_ts: int | None = None
        self.window_open_btc_price: float | None = None
        self.pm_rows: collections.OrderedDict[int, dict[str, Any]] = collections.OrderedDict()
        self.pending_entry: PendingEntryOrder | None = None
        self.active_position: OpenPosition | None = None
        self.settling_positions: list[OpenPosition] = []
        self.last_exit_time_by_window: dict[str, datetime] = {}
        self.entry_fills_by_window: dict[str, int] = {}
        self.entry_attempts_by_window: dict[str, int] = {}
        self.entry_lifecycle_by_window: dict[str, str] = {}
        self.entry_attempt_orders_by_window: dict[str, list[PendingEntryOrder]] = {}
        self.recent_client_pipeline_ms: collections.deque[float] = collections.deque(maxlen=EXECUTION_HEALTH_WINDOW)
        self.recent_clob_ack_ms: collections.deque[float] = collections.deque(maxlen=EXECUTION_HEALTH_WINDOW)
        self.recent_decision_to_response_ms: collections.deque[float] = collections.deque(maxlen=EXECUTION_HEALTH_WINDOW)
        self.last_entry_attempt_time_by_window: dict[str, datetime] = {}
        self.window_errors: list[str] = []
        self.recent_events: collections.deque[RuntimeEvent] = collections.deque(maxlen=10)
        self.last_entry_decision: EntryDecision | None = None
        self.last_exit_decision: ExitDecision | None = None
        self.last_guard_blocker: str | None = None
        self.last_pressure_defence: dict[str, Any] | None = None
        self.pressure_hedges_submitted = 0
        self.pressure_hedges_filled = 0
        self.pressure_hedges_blocked = 0
        self.last_session_style_guard_detail: dict[str, Any] | None = None
        self.startup_window_ts: int | None = None
        self.start_time = datetime.now(tz=timezone.utc)
        self.last_dashboard_at: datetime | None = None
        self.last_cadence_second_written: int | None = None
        self._live_startup_sync_done = False

        t0 = time.time()
        self._init_session_logs()
        log.info(f"  init: session logs ready in {time.time() - t0:.2f}s")

        t0 = time.time()
        ws_started = self.coinbase.start()
        log.info(f"  init: Coinbase feed setup in {time.time() - t0:.2f}s")
        t0 = time.time()
        binance_spot_started = self.binance_spot.start() if config.binance_spot_feed_enabled else False
        binance_perp_started = self.binance_perp.start() if config.binance_perp_feed_enabled else False
        pm_chainlink_started = self.pm_chainlink.start() if config.pm_chainlink_feed_enabled else False
        log.info(
            "  init: Binance/PM reference feeds %s in %.2fs",
            (
                ", ".join(
                    name
                    for name, started in (
                        ("binance_spot", binance_spot_started),
                        ("binance_perp", binance_perp_started),
                        ("pm_chainlink", pm_chainlink_started),
                    )
                    if started
                )
                or "disabled/unavailable"
            ),
            time.time() - t0,
        )
        t0 = time.time()
        minute_ready = self.coinbase.bootstrap_minute_history(self.start_time)
        log.info(
            f"  init: Coinbase minute-volatility history {'ready' if minute_ready else 'warming'} "
            f"in {time.time() - t0:.2f}s"
        )
        self._log_event("startup", "Coinbase WebSocket started" if ws_started else "Coinbase REST fallback active", {})
        self._log_event(
            "startup",
            "Polymarket Chainlink reference started" if pm_chainlink_started else "Polymarket Chainlink reference disabled/unavailable",
            {
                "started": pm_chainlink_started,
                "enabled": config.pm_chainlink_feed_enabled,
                "topic": "crypto_prices_chainlink",
            },
        )
        self._log_event(
            "startup",
            "Pressure defence enabled" if config.pressure_defence_enabled else "Pressure defence disabled",
            {
                "enabled": config.pressure_defence_enabled,
                "min_held_mid": config.pressure_min_held_mid,
                "max_held_mid": config.pressure_max_held_mid,
                "arm_tte_seconds": config.pressure_arm_tte_seconds,
                "trigger_tte_seconds": config.pressure_trigger_tte_seconds,
                "signed_volume_lookback_seconds": config.pressure_signed_volume_lookback_seconds,
                "adverse_volume_btc": config.pressure_adverse_volume_btc,
            },
        )

    def _init_session_logs(self) -> None:
        self.config.log_dir.mkdir(parents=True, exist_ok=True)
        local = self.start_time.astimezone(LONDON_ZONE)
        for _ in range(10):
            session_id = uuid.uuid4().hex[:8]
            suffix = "_paper" if self.config.paper_trading else "_live"
            session_name = f"{local.strftime('%Y%m%d_%H%M%S')}_session_{session_id}{suffix}"
            session_dir = self.config.log_dir / session_name
            if not session_dir.exists():
                session_dir.mkdir(parents=True)
                self.session_id = session_id
                self.session_name = session_name
                self.session_dir = session_dir
                self.cadence_log_path = session_dir / "stats_cadence_1s.csv"
                self.trades_log_path = session_dir / "stats_trades.csv"
                self.orders_log_path = session_dir / "stats_orders.csv"
                self.latency_log_path = session_dir / "stats_execution_latency.csv"
                return
        raise RuntimeError("Unable to create session log directory")

    def _log_event(self, event_type: str, message: str, fields: dict[str, Any]) -> None:
        event = RuntimeEvent(datetime.now(tz=timezone.utc), event_type, message, _event_fields(fields))
        self.recent_events.append(event)
        write_header = not self.orders_log_path.exists() or self.orders_log_path.stat().st_size == 0
        with self.orders_log_path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if write_header:
                writer.writerow(["timestamp_utc", "event_type", "message", "fields"])
            writer.writerow([event.timestamp_utc.isoformat(), event.event_type, event.message, event.fields])

    def _log_latency(self, event_type: str, **fields: Any) -> None:
        if not hasattr(self, "latency_log_path"):
            return
        now = datetime.now(tz=timezone.utc)
        market = getattr(self, "current_market", None)
        row = {
            "timestamp_utc": now.isoformat(),
            "session_id": getattr(self, "session_id", ""),
            "mode": "paper" if self.config.paper_trading else "live",
            "execution_mode": getattr(self.config, "execution_mode", "legacy_polling"),
            "event_type": event_type,
            "market_slug": fields.get("market_slug") or (market.slug if market else ""),
            "source": fields.get("source", ""),
            "source_event_ts_utc": fields.get("source_event_ts_utc", ""),
            "source_received_ts_utc": fields.get("source_received_ts_utc", ""),
            "source_age_ms": fields.get("source_age_ms", ""),
            "decision_ts_utc": fields.get("decision_ts_utc", ""),
            "submit_started_ts_utc": fields.get("submit_started_ts_utc", ""),
            "response_ts_utc": fields.get("response_ts_utc", ""),
            "submit_elapsed_ms": fields.get("submit_elapsed_ms", ""),
            "wire_elapsed_ms": fields.get("wire_elapsed_ms", ""),
            "local_submit_overhead_ms": fields.get("local_submit_overhead_ms", ""),
            "entry_path_elapsed_ms": fields.get("entry_path_elapsed_ms", ""),
            "decision_to_submit_ms": fields.get("decision_to_submit_ms", ""),
            "worker_queue_delay_ms": fields.get("worker_queue_delay_ms", ""),
            "quote_book_age_ms": fields.get("quote_book_age_ms", ""),
            "quote_source": fields.get("quote_source", ""),
            "connection_idle_seconds": fields.get("connection_idle_seconds", ""),
            "warm_probe_age_seconds": fields.get("warm_probe_age_seconds", ""),
            "warm_probe_ok": fields.get("warm_probe_ok", ""),
            "http_version": fields.get("http_version", ""),
            "http_status": fields.get("http_status", ""),
            "cf_ray": fields.get("cf_ray", ""),
            "server_timing": fields.get("server_timing", ""),
            "transport_error_type": fields.get("transport_error_type", ""),
            "execution_state": getattr(self, "_entry_submission_state", "IDLE"),
            "submission_id": fields.get("submission_id", ""),
            "order_id": fields.get("order_id", ""),
            "order_status": fields.get("order_status", ""),
            "side": fields.get("side", ""),
            "attempt": fields.get("attempt", ""),
            "observed_bid": fields.get("observed_bid", ""),
            "observed_ask": fields.get("observed_ask", ""),
            "limit_price": fields.get("limit_price", ""),
            "trade_value": fields.get("trade_value", ""),
            "required_edge": fields.get("required_edge", ""),
            "edge_at_ask": fields.get("edge_at_ask", ""),
            "edge_at_limit": fields.get("edge_at_limit", ""),
            "execution_health_status": fields.get("execution_health_status", ""),
            "entry_tolerance_ticks": fields.get("entry_tolerance_ticks", ""),
            "pending_entry": int(self.pending_entry is not None),
            "submission_inflight": int(getattr(self, "_entry_submission", None) is not None),
            "active_position": int(getattr(self, "active_position", None) is not None),
            "window_fill_count": getattr(self, "entry_fills_by_window", {}).get(market.slug, 0) if market else 0,
            "peak_realized_pnl": getattr(self, "peak_realized_pnl", 0.0),
            "peak_drawdown_usd": getattr(self, "peak_drawdown_usd", 0.0),
            "peak_drawdown_kill_active": int(getattr(self, "peak_drawdown_kill_active", False)),
            "detail": fields.get("detail", ""),
        }
        columns = list(row)
        with self._latency_log_lock:
            write_header = not self.latency_log_path.exists() or self.latency_log_path.stat().st_size == 0
            with self.latency_log_path.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns)
                if write_header:
                    writer.writeheader()
                writer.writerow(row)

    def _on_execution_event(self, event_type: str, received_at: datetime, fields: dict[str, Any]) -> None:
        if getattr(self.config, "execution_mode", "legacy_polling") != "event_driven_ws":
            return
        self._last_execution_event_at = received_at
        self._stream_events.put((event_type, received_at, dict(fields)))
        self._execution_wakeup.set()

    def _remember_error(self, message: str) -> None:
        clean = " ".join(str(message).split())
        if not clean or clean in self.window_errors:
            return
        self.window_errors.append(clean)
        self.window_errors = self.window_errors[-8:]

    def _refresh_wallet_balance(self, *, force: bool = False) -> float | None:
        if self.config.paper_trading:
            return None
        if self._entry_submission_inflight():
            return self.wallet_balance_live
        now = time.time()
        if not force and now - self._last_balance_refresh_at < self.config.balance_refresh_seconds:
            return self.wallet_balance_live
        self._last_balance_refresh_at = now
        balance = self.pm.fetch_wallet_balance()
        if balance is not None:
            self.wallet_balance_live = balance
            if self.wallet_balance_start_live is None:
                self.wallet_balance_start_live = balance
            self._sync_staking_high_water(self._bankroll_for_drawdown_guard(balance))
        return self.wallet_balance_live

    def _run_live_startup_sync(self) -> None:
        if self.config.paper_trading or self._live_startup_sync_done:
            return
        t0 = time.time()
        log.info("  live-init: refreshing wallet balance...")
        live_balance = self._refresh_wallet_balance(force=True)
        if live_balance is not None:
            self.balance = live_balance
            self.wallet_balance_start_live = live_balance
        log.info(f"  live-init: wallet refresh done in {time.time() - t0:.2f}s")

        t0 = time.time()
        log.info("  live-init: cancelling stale open orders...")
        cancelled = self.pm.cancel_all_open_orders()
        log.info(f"  live-init: stale-order sweep done in {time.time() - t0:.2f}s")
        if cancelled:
            self._log_event("startup_cancel", f"Cancelled {cancelled} stale open order(s)", {"cancelled": cancelled})
        self._live_startup_sync_done = True

    def _window_ts(self, now_ts: float) -> int:
        return (int(now_ts) // WINDOW_SECONDS) * WINDOW_SECONDS

    def _presign_price_levels(self, market: MarketInfo, min_price: float, max_price: float) -> list[float]:
        tick = market.tick_size if market.tick_size > 0 else 0.01
        start_tick = int(math.ceil((min_price - 1e-12) / tick))
        end_tick = int(math.floor((max_price + 1e-12) / tick))
        levels: list[float] = []
        seen: set[float] = set()
        for tick_index in range(start_tick, end_tick + 1):
            price = round_price(tick_index * tick, tick)
            if min_price - 1e-12 <= price <= max_price + 1e-12 and price not in seen:
                seen.add(price)
                levels.append(price)
        return levels

    def _market_day_type(self, market: MarketInfo) -> str:
        return "weekend" if market.market_start_utc.astimezone(LONDON_ZONE).weekday() >= 5 else "weekday"

    def _start_presign_ladder(self) -> None:
        market = self.current_market
        if self.config.paper_trading or market is None:
            status = "disabled" if self.config.paper_trading else "idle"
            with self._presign_lock:
                self.presign_ladder = PresignLadderState(status=status, generation=self._presign_generation)
            return
        self._presign_generation += 1
        generation = self._presign_generation
        min_price, max_price = self._entry_price_bounds_for_snapshot({"day_type": self._market_day_type(market)})
        levels = self._presign_price_levels(market, min_price, max_price)
        total_orders = len(levels) * 2
        started_at = datetime.now(tz=timezone.utc)
        with self._presign_lock:
            self.presign_ladder = PresignLadderState(
                market_slug=market.slug,
                status="running",
                started_at_utc=started_at,
                min_price=min_price,
                max_price=max_price,
                total_orders=total_orders,
                generation=generation,
            )
        thread = threading.Thread(
            target=self._presign_ladder_worker,
            args=(generation, market, levels, started_at),
            name=f"stats-bot-presign-{market.slug}",
            daemon=True,
        )
        thread.start()

    def _presign_ladder_worker(
        self,
        generation: int,
        market: MarketInfo,
        levels: list[float],
        started_at: datetime,
    ) -> None:
        for side, token_id in (("UP", market.token_up), ("DOWN", market.token_down)):
            for price in levels:
                if generation != self._presign_generation:
                    return
                shares, _, _ = self._entry_target_size(price=price, market=market)
                key = self._presign_order_key(side, price, shares)
                try:
                    signed = self.pm.create_signed_buy_order(
                        token_id=token_id,
                        price=price,
                        shares=shares,
                        market=market,
                    )
                except Exception as exc:
                    with self._presign_lock:
                        if generation == self.presign_ladder.generation:
                            self.presign_ladder.error_count += 1
                            self.presign_ladder.message = str(exc)[:160]
                    continue
                with self._presign_lock:
                    if generation != self.presign_ladder.generation:
                        return
                    self.presign_ladder.orders[key] = signed
                    self.presign_ladder.signed_orders += 1

        completed_at = datetime.now(tz=timezone.utc)
        elapsed = (completed_at - started_at).total_seconds()
        with self._presign_lock:
            if generation != self.presign_ladder.generation:
                return
            status = "complete" if self.presign_ladder.error_count == 0 and self.presign_ladder.signed_orders == self.presign_ladder.total_orders else "partial"
            self.presign_ladder.status = status
            self.presign_ladder.completed_at_utc = completed_at
            self.presign_ladder.elapsed_seconds = elapsed
            signed_orders = self.presign_ladder.signed_orders
            total_orders = self.presign_ladder.total_orders
            error_count = self.presign_ladder.error_count
            min_price = self.presign_ladder.min_price
            max_price = self.presign_ladder.max_price
        self._log_event(
            "presign_ladder_finished",
            status,
            {
                "market_slug": market.slug,
                "signed_orders": signed_orders,
                "total_orders": total_orders,
                "errors": error_count,
                "elapsed_seconds": round(elapsed, 3),
                "min_price": min_price,
                "max_price": max_price,
            },
        )

    def _presign_order_key(self, side: str, price: float, shares: float) -> tuple[str, float, float]:
        return (side.upper(), round(float(price), 4), round(float(shares), 4))

    def _presign_pop(self, market_slug: str, side: str, price: float, shares: float) -> SignedBuyOrder | None:
        key = self._presign_order_key(side, price, shares)
        with self._presign_lock:
            state = self.presign_ladder
            if state.market_slug != market_slug:
                return None
            return state.orders.pop(key, None)

    def _presigned_entries_available(self) -> bool:
        return (
            not self.config.paper_trading
            and hasattr(self, "presign_ladder")
            and hasattr(self, "_presign_lock")
            and hasattr(self.pm, "post_signed_buy_order")
        )

    def _presign_dashboard_text(self, now_utc: datetime) -> str | None:
        if not hasattr(self, "presign_ladder") or not hasattr(self, "_presign_lock"):
            return None
        with self._presign_lock:
            state = self.presign_ladder
            status = state.status
            signed = state.signed_orders
            total = state.total_orders
            elapsed = state.elapsed_seconds
            started_at = state.started_at_utc
            errors = state.error_count
        if status == "disabled":
            return None
        if status == "running":
            running = (now_utc - started_at).total_seconds() if started_at is not None else 0.0
            return f"running ({signed}/{total}, {running:.3f}s)"
        if status == "complete":
            return f"complete (UP/DOWN, {signed}/{total}, {elapsed or 0.0:.3f}s)"
        if status == "partial":
            return f"partial ({signed}/{total}, errors={errors}, {elapsed or 0.0:.3f}s)"
        if status == "idle":
            return "idle"
        return status

    def _on_new_window(self, now_utc: datetime, window_ts: int) -> None:
        if self.current_window_ts is not None and window_ts != self.current_window_ts:
            self._settle_ended_active_position(now_utc)
        self.current_window_ts = window_ts
        if self.startup_window_ts is None:
            self.startup_window_ts = window_ts
        self.guard.reset_window()
        self.window_errors = []
        self.last_pressure_defence = None
        self.pm_rows = collections.OrderedDict()
        self.window_open_btc_price = None
        self.current_market = self.pm.find_market(window_ts)
        if self.current_market:
            if self.config.execution_mode == "event_driven_ws":
                self.pm.start_streams(self.current_market, self._on_execution_event)
                self._log_latency("stream_subscribe", market_slug=self.current_market.slug, source="polymarket_ws")
            self.entry_fills_by_window.setdefault(self.current_market.slug, 0)
            self.entry_attempts_by_window.setdefault(self.current_market.slug, 0)
            self.entry_lifecycle_by_window.setdefault(self.current_market.slug, "NONE")
            self.entry_attempt_orders_by_window.setdefault(self.current_market.slug, [])
            self._log_event(
                "new_window",
                self.current_market.question,
                {
                    "slug": self.current_market.slug,
                    "tick_size": self.current_market.tick_size,
                    "min_order_size": self.current_market.min_order_size,
                    "fees_enabled": self.current_market.fees_enabled,
                    "fee_rate": self.current_market.fee_rate,
                },
            )
            log.info(LINE)
            log.info(f"  {self.current_market.question}")
            log.info(
                f"  mode={'paper' if self.config.paper_trading else 'live'}"
                f"  stake_mode={self.config.stake_mode}"
                f"  entry=pre-signed GTC fast limit when ready; fallback maker @ best_ask-1tick"
                f"  exit_policy={self.config.exit_policy_mode}"
            )
            if self.config.observe_first_window and self.current_window_ts == self.startup_window_ts:
                log.info("  startup_guard=OBSERVE_FIRST_WINDOW")
            log.info(LINE)
            self._start_presign_ladder()
        else:
            self._start_presign_ladder()
            self._remember_error("No Polymarket BTC 5m market found for current window")

    def _capture_pm_snapshot(self, now_utc: datetime) -> None:
        market = self.current_market
        if market is None:
            return
        up = self.pm.get_order_book_snapshot(market.token_up)
        down = self.pm.get_order_book_snapshot(market.token_down)
        if up.mid is None or down.mid is None:
            return
        row = {
            "timestamp_utc": now_utc,
            "up_bid": up.bid,
            "up_ask": up.ask,
            "up_mid": up.mid,
            "up_spread": up.spread,
            "up_bid_depth_total": up.bid_depth_total,
            "up_ask_depth_total": up.ask_depth_total,
            "up_best_bid_depth": up.best_bid_depth,
            "up_best_ask_depth": up.best_ask_depth,
            "down_bid": down.bid,
            "down_ask": down.ask,
            "down_mid": down.mid,
            "down_spread": down.spread,
            "down_bid_depth_total": down.bid_depth_total,
            "down_ask_depth_total": down.ask_depth_total,
            "down_best_bid_depth": down.best_bid_depth,
            "down_best_ask_depth": down.best_ask_depth,
        }
        self.pm_rows[int(now_utc.timestamp())] = row
        while len(self.pm_rows) > 600:
            self.pm_rows.popitem(last=False)

    def _build_snapshot(self, now_utc: datetime) -> dict[str, Any] | None:
        if self.current_market is None or self.window_open_btc_price is None:
            return None
        local_rv, local_rv_status, local_rv_age = self.coinbase.local_volatility_1h_bps(now_utc)
        return compute_live_snapshot(
            btc_history=self.coinbase.history_rows(),
            pm_history=list(self.pm_rows.values()),
            now_utc=now_utc,
            market_start_utc=self.current_market.market_start_utc,
            market_end_utc=self.current_market.market_end_utc,
            window_open_btc_price=float(self.window_open_btc_price),
            volatility_edges=self.volatility_edges,
            market_slug=self.current_market.slug,
            local_volatility_1h_bps=local_rv,
            local_volatility_status=local_rv_status,
            local_volatility_age_minutes=local_rv_age,
        )

    def _coinbase_pressure_z(self, held_side: str) -> tuple[float | None, float | None, float | None]:
        return adverse_z(
            self.coinbase.history_rows(),
            held_side=held_side,
            lookback_seconds=self.config.pressure_lookback_seconds,
            min_returns=600,
        )

    def _provider_pressure(self, held_side: str) -> dict[str, Any]:
        providers = {
            "coinbase": self._coinbase_pressure_z(held_side),
            "binance_spot": adverse_z(
                self.binance_spot.history_rows(),
                held_side=held_side,
                lookback_seconds=self.config.pressure_lookback_seconds,
                min_returns=600,
            ),
            "binance_perp": adverse_z(
                self.binance_perp.history_rows(),
                held_side=held_side,
                lookback_seconds=self.config.pressure_lookback_seconds,
                min_returns=600,
            ),
        }
        best_source = ""
        best_z: float | None = None
        detail: dict[str, Any] = {}
        for source, (ret_bps, z_value, rv_bps) in providers.items():
            detail[f"{source}_ret_bps"] = ret_bps
            detail[f"{source}_adverse_z"] = z_value
            detail[f"{source}_rv_1h_bps"] = rv_bps
            if z_value is not None and math.isfinite(z_value) and (best_z is None or z_value > best_z):
                best_source = source
                best_z = float(z_value)
        detail["pressure_source"] = best_source
        detail["pressure_z"] = best_z
        return detail

    def _feed_window_signed_bps(self, feed: Any) -> float | None:
        market = self.current_market
        if market is None:
            return None
        rows = []
        for row in feed.history_rows():
            ts = row.get("timestamp_utc")
            close = finite_float(row.get("close_1s"))
            if isinstance(ts, datetime) and close is not None and close > 0:
                rows.append((ts, close))
        if len(rows) < 2:
            return None
        rows.sort(key=lambda item: item[0])
        latest_price = rows[-1][1]
        anchor_price = None
        for ts, price in rows:
            if ts <= market.market_start_utc:
                anchor_price = price
            else:
                break
        if anchor_price is None:
            for ts, price in rows:
                if ts >= market.market_start_utc:
                    anchor_price = price
                    break
        if anchor_price is None or anchor_price <= 0:
            return None
        return (latest_price / anchor_price - 1.0) * 10_000.0

    def _dashboard_signed_bps_text(self, snapshot: dict[str, Any]) -> str:
        coinbase_bps = finite_float(snapshot.get("cb_signed_bps_raw"))
        return _fmt(coinbase_bps, 2)

    def _dashboard_net_signed_flow_text(self) -> str:
        lookback_seconds = getattr(self.config, "pressure_signed_volume_lookback_seconds", 30)
        rows = self.binance_spot.history_rows()

        def _signed_flow_part(seconds: int) -> str:
            _, signed_total, _, _, _ = adverse_signed_volume(
                rows,
                held_side="DOWN",
                lookback_seconds=seconds,
            )
            if signed_total is None or not math.isfinite(signed_total):
                return f"NA ({seconds}s)"
            return f"{signed_total:+.2f} BTC ({seconds}s)"

        return _signed_flow_part(lookback_seconds)

    def _ffm_signed_volume_pressure(self, held_side: str) -> dict[str, Any]:
        adverse, signed_total, buy_total, sell_total, seconds = adverse_signed_volume(
            self.binance_spot.history_rows(),
            held_side=held_side,
            lookback_seconds=self.config.pressure_signed_volume_lookback_seconds,
        )
        return {
            "pressure_source": "binance_spot_signed_volume",
            "pressure_adverse_volume_btc": adverse,
            "pressure_signed_volume_btc": signed_total,
            "pressure_buy_volume_btc": buy_total,
            "pressure_sell_volume_btc": sell_total,
            "pressure_volume_seconds": seconds,
            "pressure_volume_lookback_seconds": self.config.pressure_signed_volume_lookback_seconds,
            "pressure_volume_threshold_btc": self.config.pressure_adverse_volume_btc,
        }

    def _remember_pressure_defence(
        self,
        status: str,
        reason: str,
        *,
        held_mid: float | None,
        tte: float | None,
        pressure: dict[str, Any] | None = None,
        **extra: Any,
    ) -> None:
        detail: dict[str, Any] = {
            "status": status,
            "reason": reason,
            "held_mid": held_mid,
            "tte": tte,
            "pressure_volume_threshold_btc": self.config.pressure_adverse_volume_btc,
        }
        if pressure:
            detail.update(pressure)
        detail.update(extra)
        self.last_pressure_defence = detail

    def _polymarket_taker_fee(self, shares: float, price: float) -> float:
        market = self.current_market
        fee_rate = (
            finite_float(market.fee_rate) if market is not None and market.fee_rate is not None else CRYPTO_TAKER_FEE_RATE_DEFAULT
        )
        if fee_rate is None:
            fee_rate = CRYPTO_TAKER_FEE_RATE_DEFAULT
        return float(shares) * float(fee_rate) * float(price) * (1.0 - float(price))

    def _position_trade_style(self, position: OpenPosition) -> str:
        trade_style = str(position.entry_snapshot.get("entry_trade_style") or "").strip().lower()
        if trade_style in {"continuation", "mean_reversion"}:
            return trade_style
        key_parts = str(position.entry_policy_key_text or "").split("|")
        bps_bucket = key_parts[3] if len(key_parts) > 3 else None
        return str(trade_style_from_bps_bucket(position.held_side, bps_bucket) or "").strip().lower()

    def _maybe_execute_pressure_defence(self, now_utc: datetime, snapshot: dict[str, Any] | None) -> None:
        position = self.active_position
        market = self.current_market
        if position is None or market is None or snapshot is None:
            return
        if not self.config.pressure_defence_enabled:
            return
        if position.pressure_defence_status == "submitted":
            self._reconcile_pressure_defence(now_utc)
        if (
            position.hedge_leg is not None
            or position.pressure_defence_status in {"submitted", "pending", "filled", "failed"}
            or bool(position.extra.get("pressure_defence_attempted"))
        ):
            return
        held_prefix = position.held_side.lower()
        held_mid = finite_float(snapshot.get(f"{held_prefix}_mid"))
        tte = finite_float(snapshot.get("canonical_tte_seconds"))
        if held_mid is None or tte is None:
            return
        if tte > self.config.pressure_arm_tte_seconds:
            position.pressure_defence_status = "idle"
            position.pressure_defence_reason = ""
            self.last_pressure_defence = None
            return
        pressure = self._ffm_signed_volume_pressure(position.held_side)
        if not (self.config.pressure_min_held_mid <= held_mid <= self.config.pressure_max_held_mid):
            position.pressure_defence_status = "blocked"
            position.pressure_defence_reason = "HELD_MID_OUTSIDE_PRESSURE_BAND"
            self._remember_pressure_defence(
                "blocked",
                position.pressure_defence_reason,
                held_mid=held_mid,
                tte=tte,
                pressure=pressure,
            )
            return
        if tte > self.config.pressure_trigger_tte_seconds:
            position.pressure_defence_status = "armed"
            position.pressure_defence_reason = "WAITING_FOR_TRIGGER_TTE"
            self._remember_pressure_defence(
                "armed",
                position.pressure_defence_reason,
                held_mid=held_mid,
                tte=tte,
                pressure=pressure,
            )
            return
        adverse_volume = finite_float(pressure.get("pressure_adverse_volume_btc"))
        if adverse_volume is None or adverse_volume < self.config.pressure_adverse_volume_btc:
            position.pressure_defence_status = "armed"
            position.pressure_defence_reason = "ADVERSE_VOLUME_BELOW_THRESHOLD"
            self._remember_pressure_defence(
                "armed",
                position.pressure_defence_reason,
                held_mid=held_mid,
                tte=tte,
                pressure=pressure,
            )
            return
        hedge_side = "DOWN" if position.held_side == "UP" else "UP"
        hedge_book = self._side_book(hedge_side)
        hedge_ask = finite_float(hedge_book.ask)
        if hedge_ask is None:
            self.pressure_hedges_blocked += 1
            position.pressure_defence_status = "blocked"
            position.pressure_defence_reason = "NO_HEDGE_ASK"
            self.last_pressure_defence = {"status": "blocked", "reason": "NO_HEDGE_ASK", "held_mid": held_mid, "tte": tte, **pressure}
            return
        if hedge_ask > self.config.pressure_max_hedge_ask:
            self.pressure_hedges_blocked += 1
            position.pressure_defence_status = "blocked"
            position.pressure_defence_reason = "HEDGE_ASK_ABOVE_MAX"
            self.last_pressure_defence = {
                "status": "blocked",
                "reason": "HEDGE_ASK_ABOVE_MAX",
                "held_mid": held_mid,
                "hedge_ask": hedge_ask,
                "max_hedge_ask": self.config.pressure_max_hedge_ask,
                "tte": tte,
                **pressure,
            }
            return
        hedge_shares = max(0.0, position.shares * self.config.pressure_hedge_fraction)
        if hedge_shares <= self.config.dust_share_threshold:
            return
        hedge_token = self._selected_token_id(hedge_side)
        limit_price = round_price(self.config.pressure_hedge_limit_price, market.tick_size)
        position.pressure_defence_status = "submitted"
        position.pressure_defence_reason = ""
        position.extra["pressure_defence_attempted"] = True
        position.extra["pressure_defence_attempted_at_utc"] = now_utc.isoformat()
        position.extra["pressure_defence_hedge_side"] = hedge_side
        position.extra["pressure_defence_target_shares"] = hedge_shares
        position.extra["pressure_defence_limit_price"] = limit_price
        position.extra["pressure_defence_hedge_ask"] = hedge_ask
        position.extra["pressure_defence_trigger_source"] = pressure.get("pressure_source")
        position.extra["pressure_defence_trigger_adverse_volume_btc"] = pressure.get("pressure_adverse_volume_btc")
        position.extra["pressure_defence_trigger_signed_volume_btc"] = pressure.get("pressure_signed_volume_btc")
        self.pressure_hedges_submitted += 1
        if self.config.paper_trading:
            matched = hedge_shares
            fill_price = hedge_ask
            order_id = f"paper-pressure-{uuid.uuid4().hex[:10]}"
            status = "PAPER_MATCHED"
            ok = True
        else:
            try:
                signed = self.pm.create_signed_buy_order(
                    token_id=hedge_token,
                    price=limit_price,
                    shares=hedge_shares,
                    market=market,
                )
                result = self.pm.post_signed_buy_order(signed)
            except Exception as exc:
                result = OrderResult(False, None, "ERROR", str(exc), matched_shares=0.0, price=None)
            matched = float(result.matched_shares or 0.0)
            fill_price = finite_float(result.price) or hedge_ask
            order_id = result.order_id
            status = result.status
            ok = result.ok
        position.extra["pressure_defence_order_id"] = order_id
        position.extra["pressure_defence_order_status"] = status
        position.extra["pressure_defence_matched_shares_seen"] = 0.0
        self._log_event(
            "pressure_hedge_submitted",
            status,
            {
                "ok": ok,
                "held_side": position.held_side,
                "hedge_side": hedge_side,
                "held_mid": held_mid,
                "hedge_ask": hedge_ask,
                "limit_price": limit_price,
                "shares": hedge_shares,
                "matched_shares": matched,
                "fill_price": fill_price,
                "trigger_tte": tte,
                **pressure,
            },
        )
        if matched <= self.config.dust_share_threshold:
            if ok and order_id:
                position.pressure_defence_status = "submitted"
                position.pressure_defence_reason = f"AWAITING_MATCH:{status}"
                last_status = "submitted"
            else:
                position.pressure_defence_status = "failed"
                position.pressure_defence_reason = f"SUBMIT_FAILED:{status}"
                last_status = "failed"
            self.last_pressure_defence = {
                "status": last_status,
                "reason": position.pressure_defence_reason,
                "held_mid": held_mid,
                "hedge_side": hedge_side,
                "hedge_ask": hedge_ask,
                "limit_price": limit_price,
                "target_shares": hedge_shares,
                "matched_shares": matched,
                "order_id": order_id,
                "tte": tte,
                **pressure,
            }
            return
        self._record_pressure_hedge_fill(
            position=position,
            hedge_token=hedge_token,
            hedge_side=hedge_side,
            matched_total=matched,
            fill_price=fill_price,
            now_utc=now_utc,
            order_id=order_id,
            limit_price=limit_price,
            hedge_ask=hedge_ask,
            held_mid=held_mid,
            tte=tte,
            pressure=pressure,
        )

    def _record_pressure_hedge_fill(
        self,
        *,
        position: OpenPosition,
        hedge_token: str,
        hedge_side: str,
        matched_total: float,
        fill_price: float,
        now_utc: datetime,
        order_id: str | None,
        limit_price: float,
        hedge_ask: float,
        held_mid: float | None,
        tte: float | None,
        pressure: dict[str, Any],
    ) -> bool:
        target_shares = finite_float(position.extra.get("pressure_defence_target_shares")) or position.shares
        seen = finite_float(position.extra.get("pressure_defence_matched_shares_seen")) or 0.0
        if matched_total <= seen + 1e-9:
            return False
        delta = matched_total - seen
        fee = self._polymarket_taker_fee(delta, fill_price)
        cost = delta * fill_price
        self.balance -= cost + fee
        if not self.config.paper_trading and self.wallet_balance_live is not None:
            self.wallet_balance_live = max(0.0, self.wallet_balance_live - cost - fee)
        adverse_volume = finite_float(pressure.get("pressure_adverse_volume_btc")) or 0.0
        if position.hedge_leg is None:
            position.hedge_leg = HedgeLeg(
                token_id=hedge_token,
                side=hedge_side,
                shares=delta,
                avg_price=fill_price,
                cost=cost,
                fee=fee,
                order_id=order_id,
                trigger_source=str(pressure.get("pressure_source") or ""),
                trigger_z=float(adverse_volume),
                trigger_time_utc=now_utc,
                fill_time_utc=datetime.now(tz=timezone.utc),
                limit_price=limit_price,
                ask_at_trigger=hedge_ask,
                status="partial",
            )
        else:
            leg = position.hedge_leg
            total_shares = leg.shares + delta
            leg.cost += cost
            leg.fee += fee
            leg.shares = total_shares
            leg.avg_price = leg.cost / total_shares if total_shares > 0 else fill_price
            leg.fill_time_utc = datetime.now(tz=timezone.utc)
            leg.status = "partial"
        position.extra["pressure_defence_matched_shares_seen"] = matched_total
        filled = matched_total >= target_shares - self.config.dust_share_threshold
        position.pressure_defence_status = "filled" if filled else "submitted"
        position.pressure_defence_reason = "" if filled else "PARTIAL_FILL_AWAITING_MATCH"
        if position.hedge_leg is not None:
            position.hedge_leg.status = "filled" if filled else "partial"
        if not bool(position.extra.get("pressure_defence_counted_fill")):
            self.pressure_hedges_filled += 1
            position.extra["pressure_defence_counted_fill"] = True
        self._log_event(
            "pressure_hedge_fill_reconciled",
            "FILLED" if filled else "PARTIAL",
            {
                "order_id": order_id,
                "held_side": position.held_side,
                "hedge_side": hedge_side,
                "matched_total": matched_total,
                "delta": delta,
                "fill_price": fill_price,
                "hedge_cost_delta": cost,
                "hedge_fee_delta": fee,
                **pressure,
            },
        )
        self.last_pressure_defence = {
            "status": "filled" if filled else "partial",
            "held_mid": held_mid,
            "hedge_side": hedge_side,
            "hedge_ask": hedge_ask,
            "hedge_price": fill_price,
            "hedge_shares": matched_total,
            "hedge_fee": (position.hedge_leg.fee if position.hedge_leg is not None else fee),
            "tte": tte,
            **pressure,
        }
        return True

    def _reconcile_pressure_defence(self, now_utc: datetime) -> bool:
        position = self.active_position
        if position is None or self.config.paper_trading:
            return False
        order_id = str(position.extra.get("pressure_defence_order_id") or "")
        if not order_id:
            return False
        raw = self.pm.get_order(order_id)
        if raw is None:
            return False
        if isinstance(raw, dict):
            status = str(raw.get("status", "")).lower()
            matched = float(
                raw.get("size_matched")
                or raw.get("sizeMatched")
                or raw.get("matched_size")
                or raw.get("matchedSize")
                or raw.get("filled_size")
                or raw.get("filledSize")
                or raw.get("matched")
                or 0.0
            )
            price = float(
                raw.get("price")
                or raw.get("avg_price")
                or raw.get("avgPrice")
                or position.extra.get("pressure_defence_limit_price")
                or 0.0
            )
        else:
            status = str(getattr(raw, "status", "")).lower()
            matched = float(
                getattr(raw, "size_matched", None)
                or getattr(raw, "sizeMatched", None)
                or getattr(raw, "matched_size", None)
                or getattr(raw, "matchedSize", None)
                or getattr(raw, "filled_size", None)
                or getattr(raw, "filledSize", None)
                or getattr(raw, "matched", None)
                or 0.0
            )
            price = float(
                getattr(raw, "price", None)
                or getattr(raw, "avg_price", None)
                or getattr(raw, "avgPrice", None)
                or position.extra.get("pressure_defence_limit_price")
                or 0.0
            )
        target_shares = finite_float(position.extra.get("pressure_defence_target_shares")) or position.shares
        seen = finite_float(position.extra.get("pressure_defence_matched_shares_seen")) or 0.0
        if _order_status_is_fill_like(status) and matched <= seen + 1e-9:
            matched = max(target_shares, matched)
        position.extra["pressure_defence_order_status"] = status
        hedge_side = str(position.extra.get("pressure_defence_hedge_side") or ("DOWN" if position.held_side == "UP" else "UP"))
        limit_price = finite_float(position.extra.get("pressure_defence_limit_price")) or price
        hedge_ask = finite_float(position.extra.get("pressure_defence_hedge_ask")) or limit_price
        pressure = {
            "pressure_source": str(position.extra.get("pressure_defence_trigger_source") or "binance_spot_signed_volume"),
            "pressure_adverse_volume_btc": finite_float(position.extra.get("pressure_defence_trigger_adverse_volume_btc")),
            "pressure_signed_volume_btc": finite_float(position.extra.get("pressure_defence_trigger_signed_volume_btc")),
            "pressure_volume_threshold_btc": self.config.pressure_adverse_volume_btc,
        }
        changed = False
        if matched > seen + 1e-9:
            changed = self._record_pressure_hedge_fill(
                position=position,
                hedge_token=self._selected_token_id(hedge_side),
                hedge_side=hedge_side,
                matched_total=matched,
                fill_price=price or limit_price,
                now_utc=now_utc,
                order_id=order_id,
                limit_price=limit_price,
                hedge_ask=hedge_ask,
                held_mid=None,
                tte=None,
                pressure=pressure,
            )
        elif status in {"cancelled", "canceled", "rejected", "expired"}:
            position.pressure_defence_status = "failed"
            position.pressure_defence_reason = f"HEDGE_ORDER_{status.upper()}"
            self.last_pressure_defence = {"status": "failed", "reason": position.pressure_defence_reason, "order_id": order_id}
            changed = True
        return changed

    def _selected_token_id(self, side: str) -> str:
        assert self.current_market is not None
        return self.current_market.token_up if side.upper() == "UP" else self.current_market.token_down

    def _side_book(self, side: str) -> BookSnapshot:
        return self.pm.get_order_book_snapshot(self._selected_token_id(side))

    def _maker_quote(self, side: str) -> tuple[float | None, int | None, BookSnapshot]:
        market = self.current_market
        if market is None:
            return None, None, BookSnapshot(None, None, None, None, None, None, None, None, datetime.now(tz=timezone.utc))
        book = self._side_book(side)
        if book.bid is None or book.ask is None or book.ask <= book.bid:
            return None, None, book
        spread_ticks = int(round((float(book.ask) - float(book.bid)) / market.tick_size))
        if spread_ticks < self.config.maker_min_spread_ticks:
            return None, spread_ticks, book
        price = round_price(float(book.ask) - market.tick_size, market.tick_size)
        if not (price >= float(book.bid) and price < float(book.ask)):
            return None, spread_ticks, book
        return price, spread_ticks, book

    def _bankroll_for_sizing(self) -> float:
        config = getattr(self, "config", None)
        wallet_balance_live = getattr(self, "wallet_balance_live", None)
        if config is not None and not config.paper_trading and wallet_balance_live is not None:
            return float(wallet_balance_live)
        return float(getattr(self, "balance", 0.0))

    def _bankroll_for_drawdown_guard(self, raw_bankroll: float | None = None) -> float:
        if raw_bankroll is None:
            raw_bankroll = self._bankroll_for_sizing()
        base_bankroll = max(0.0, float(raw_bankroll))
        position_collateral = 0.0
        active_position = getattr(self, "active_position", None)
        if active_position is not None:
            position_collateral += max(0.0, float(getattr(active_position, "entry_cost", 0.0) or 0.0))
        for position in getattr(self, "settling_positions", []) or []:
            position_collateral += max(0.0, float(getattr(position, "entry_cost", 0.0) or 0.0))
        if position_collateral > 0.0:
            internal_balance = finite_float(getattr(self, "balance", None))
            if internal_balance is not None:
                base_bankroll = min(base_bankroll, max(0.0, internal_balance))
        collateral = position_collateral
        pending_entry = getattr(self, "pending_entry", None)
        if pending_entry is not None:
            target_notional = finite_float(getattr(pending_entry, "target_notional", None))
            if target_notional is None:
                target_notional = float(pending_entry.target_shares) * float(pending_entry.price)
            wallet_before = finite_float(getattr(pending_entry, "wallet_balance_before", None))
            if wallet_before is not None:
                observed_reserve = max(0.0, wallet_before - base_bankroll)
                collateral += min(max(0.0, float(target_notional)), observed_reserve)
        return base_bankroll + collateral

    def _ensure_staking_state(self) -> None:
        if not hasattr(self, "staking_high_water"):
            self.staking_high_water = None
        if not hasattr(self, "staking_positive_streak_pnl"):
            self.staking_positive_streak_pnl = 0.0
        if not hasattr(self, "ladder_stake_tier_index"):
            self.ladder_stake_tier_index = None
        if not hasattr(self, "ladder_pending_tier_index"):
            self.ladder_pending_tier_index = None
        if not hasattr(self, "recovery_active"):
            self.recovery_active = False
        if not hasattr(self, "recovery_anchor_bankroll"):
            self.recovery_anchor_bankroll = None
        if not hasattr(self, "last_staking_decision"):
            self.last_staking_decision = None

    def _ensure_regime_recovery_state(self) -> RegimeRecoveryState:
        state = getattr(self, "regime_recovery", None)
        if not isinstance(state, RegimeRecoveryState):
            state = RegimeRecoveryState()
            self.regime_recovery = state
        return state

    def _regime_recovery_status(self, now_utc: datetime) -> tuple[bool, bool, RegimeRecoveryState]:
        state = self._ensure_regime_recovery_state()
        state.active_until_utc = None
        state.forced_block_until_utc = None
        state.trigger_pnl = None
        return False, False, state

    def _record_regime_closed_trade(self, normalised_pnl: float, exit_time_utc: datetime) -> None:
        return

    def _side_momentum_block_details(
        self,
        snapshot: dict[str, Any] | None,
        selected_side: str | None,
    ) -> tuple[bool, float | None]:
        if snapshot is None or selected_side is None:
            return False, None
        side = str(selected_side).upper()
        momentum = finite_float(snapshot.get("btc_mom_20s_bps"))
        if side not in {"UP", "DOWN"} or momentum is None or abs(momentum) < SIDE_MOMENTUM_GUARD_THRESHOLD_BPS:
            return False, momentum
        against = (momentum > 0.0 and side == "DOWN") or (momentum < 0.0 and side == "UP")
        return against, momentum

    def _regime_entry_blockers(
        self,
        *,
        snapshot: dict[str, Any] | None,
        decision: EntryDecision | None,
        now_utc: datetime,
    ) -> tuple[str, ...]:
        return ()

    def _sync_staking_high_water(self, bankroll: float | None = None) -> float:
        self._ensure_staking_state()
        if bankroll is None:
            bankroll = self._bankroll_for_sizing()
        bankroll = max(0.0, float(bankroll))
        high_water = self.staking_high_water
        if high_water is None or high_water <= 0.0:
            self.staking_high_water = bankroll
        elif bankroll > high_water:
            self.staking_high_water = bankroll
        return float(self.staking_high_water or 0.0)

    def _staking_drawdown_pct(self, bankroll: float | None = None, high_water: float | None = None) -> float:
        if bankroll is None:
            bankroll = self._bankroll_for_sizing()
        if high_water is None:
            high_water = self._sync_staking_high_water(bankroll)
        if high_water <= 0.0:
            return 0.0
        return max(0.0, (float(high_water) - float(bankroll)) / float(high_water) * 100.0)

    def _dynamic_share_tier(self, bankroll: float) -> tuple[int, float]:
        tier_index = 0
        for index, (threshold, shares) in enumerate(DYNAMIC_SHARE_TIERS):
            if bankroll >= threshold:
                tier_index = index
        return tier_index, DYNAMIC_SHARE_TIERS[tier_index][1]

    def _ladder_share_tier(self, bankroll: float) -> tuple[int, float]:
        tier_index = 0
        for index, (threshold, shares, _downgrade_below) in enumerate(LADDER_SHARE_TIERS):
            if bankroll >= threshold:
                tier_index = index
        return tier_index, LADDER_SHARE_TIERS[tier_index][1]

    def _ladder_stake_decision(self) -> DynamicStakeDecision:
        self._ensure_staking_state()
        bankroll = self._bankroll_for_sizing()
        eligible_index, _eligible_shares = self._ladder_share_tier(bankroll)
        current_index = self.ladder_stake_tier_index
        reason: str | None = None

        if current_index is None:
            current_index = eligible_index
            self.ladder_stake_tier_index = current_index
            self.ladder_pending_tier_index = None
        else:
            _current_threshold, _current_shares, current_downgrade_below = LADDER_SHARE_TIERS[current_index]
            if current_downgrade_below is not None and bankroll < current_downgrade_below:
                current_index, _shares = self._ladder_share_tier(bankroll)
                self.ladder_stake_tier_index = current_index
                self.ladder_pending_tier_index = None
                reason = f"Downgraded below ${current_downgrade_below:,.0f} buffer"
            elif eligible_index > current_index:
                self.ladder_pending_tier_index = eligible_index
                pending_threshold = LADDER_SHARE_TIERS[eligible_index][0]
                reason = f"Scale-up pending above ${pending_threshold:,.0f} until one closed trade"
            elif eligible_index <= current_index:
                self.ladder_pending_tier_index = None

        threshold, target_shares, _downgrade_below = LADDER_SHARE_TIERS[int(self.ladder_stake_tier_index or 0)]
        decision = DynamicStakeDecision(
            target_shares=float(target_shares),
            bankroll=float(bankroll),
            high_water=float(threshold),
            drawdown_pct=0.0,
            reason=reason,
        )
        self.last_staking_decision = decision
        return decision

    def _record_ladder_closed_trade(self) -> None:
        self._ensure_staking_state()
        if self.ladder_stake_tier_index is None:
            self._ladder_stake_decision()
            return
        bankroll = self._bankroll_for_sizing()
        eligible_index, _eligible_shares = self._ladder_share_tier(bankroll)
        current_index = int(self.ladder_stake_tier_index)
        pending_index = self.ladder_pending_tier_index
        if pending_index is not None and eligible_index >= pending_index and pending_index > current_index:
            self.ladder_stake_tier_index = pending_index
            self.ladder_pending_tier_index = None
        elif pending_index is None and eligible_index > current_index:
            self.ladder_stake_tier_index = eligible_index
            self.ladder_pending_tier_index = None
        elif eligible_index <= current_index:
            self.ladder_pending_tier_index = None

    def _sync_recovery_mode(
        self,
        bankroll: float | None = None,
        high_water: float | None = None,
    ) -> tuple[bool, float | None, float, float]:
        self._ensure_staking_state()
        if bankroll is None:
            bankroll = self._bankroll_for_sizing()
        if high_water is None:
            high_water = self._sync_staking_high_water(bankroll)
        drawdown_pct = self._staking_drawdown_pct(bankroll, high_water)

        if self.recovery_active and drawdown_pct < STAKE_DRAWDOWN_RECOVERY_EXIT_PCT:
            self.recovery_active = False
            self.recovery_anchor_bankroll = None
        elif not self.recovery_active and drawdown_pct >= STAKE_DRAWDOWN_RECOVERY_PCT:
            self.recovery_active = True
            self.recovery_anchor_bankroll = float(bankroll)

        return bool(self.recovery_active), self.recovery_anchor_bankroll, float(high_water), float(drawdown_pct)

    def _dynamic_stake_decision(self) -> DynamicStakeDecision:
        self._ensure_staking_state()
        bankroll = self._bankroll_for_sizing()
        guard_bankroll = self._bankroll_for_drawdown_guard(bankroll)
        high_water = self._sync_staking_high_water(guard_bankroll)
        recovery_active, recovery_anchor, high_water, drawdown_pct = self._sync_recovery_mode(guard_bankroll, high_water)
        tier_index, target_shares = self._dynamic_share_tier(bankroll)
        reason: str | None = None

        if drawdown_pct >= STAKE_DRAWDOWN_DISABLE_BONUS_PCT:
            self.staking_positive_streak_pnl = 0.0

        if recovery_active:
            if target_shares != DYNAMIC_SHARE_TIERS[0][1]:
                reason = f"Recovery mode: 5 shares until DD < {STAKE_DRAWDOWN_RECOVERY_EXIT_PCT:.0f}%"
            else:
                reason = f"Recovery mode until DD < {STAKE_DRAWDOWN_RECOVERY_EXIT_PCT:.0f}%"
            target_shares = DYNAMIC_SHARE_TIERS[0][1]
        elif drawdown_pct >= STAKE_DRAWDOWN_FORCE_MIN_PCT:
            if target_shares != DYNAMIC_SHARE_TIERS[0][1]:
                reason = f"Reduced to 5 shares due to {drawdown_pct:.1f}% high-water drawdown"
            target_shares = DYNAMIC_SHARE_TIERS[0][1]
        elif self.staking_positive_streak_pnl > 0.0:
            target_shares += 1.0
            reason = "Increased stake tier due to positive trade streak near high-water"

        decision = DynamicStakeDecision(
            target_shares=float(target_shares),
            bankroll=float(bankroll),
            high_water=float(high_water),
            drawdown_pct=float(drawdown_pct),
            recovery_active=bool(recovery_active),
            recovery_anchor=float(recovery_anchor) if recovery_anchor is not None else None,
            recovery_floor=None,
            reason=reason,
        )
        self.last_staking_decision = decision
        return decision

    def _dynamic_target_shares(self) -> float:
        return self._dynamic_stake_decision().target_shares

    def _record_staking_closed_trade(self, pnl: float) -> None:
        self._ensure_staking_state()
        if getattr(getattr(self, "config", None), "stake_mode", "") == "ladder_shares":
            self._record_ladder_closed_trade()
            return
        guard_bankroll = self._bankroll_for_drawdown_guard()
        high_water = self._sync_staking_high_water(guard_bankroll)
        recovery_active, _, _, drawdown_pct = self._sync_recovery_mode(guard_bankroll, high_water)
        if pnl <= 0.0 or drawdown_pct >= STAKE_DRAWDOWN_DISABLE_BONUS_PCT or recovery_active:
            self.staking_positive_streak_pnl = 0.0
            return
        self.staking_positive_streak_pnl += float(pnl)

    def _staking_entry_blockers(self, snapshot: dict[str, Any] | None, now_utc: datetime | None = None) -> tuple[str, ...]:
        return ()

    def _entry_target_size(self, *, price: float, market: MarketInfo) -> tuple[float, float, float]:
        min_order_size = finite_float(market.min_order_size)
        if min_order_size is None or min_order_size <= 0:
            min_order_size = self.config.min_order_size_fallback
        if self.config.stake_mode == "min_order_size":
            shares = float(min_order_size)
        elif self.config.stake_mode == "dynamic_shares":
            shares = max(self._dynamic_target_shares(), float(min_order_size))
        elif self.config.stake_mode == "ladder_shares":
            shares = max(self._ladder_stake_decision().target_shares, float(min_order_size))
        else:
            shares = self.config.stake_usd / max(price, 0.001)
        shares = round(max(0.0, shares), 4)
        notional = round(shares * price, 6)
        return shares, notional, float(min_order_size)

    def _can_enter_after_cooldown(self, market_slug: str, now_utc: datetime) -> bool:
        last_exit = self.last_exit_time_by_window.get(market_slug)
        if last_exit is None:
            return True
        return (now_utc - last_exit).total_seconds() >= self.config.cooldown_seconds

    def _required_entry_edge_at_fill(self, now_utc: datetime, decision: EntryDecision) -> float | None:
        threshold = finite_float(decision.entry_edge_threshold)
        if threshold is None:
            return None
        return threshold

    def _pending_entry_still_has_edge(
        self,
        pending: PendingEntryOrder,
        decision: EntryDecision | None,
        snapshot: dict[str, Any] | None = None,
    ) -> tuple[bool, float | None, float | None]:
        if decision is None:
            return False, None, None
        blockers = set(decision.blockers or ())
        if blockers - {"POLICY_NO_SIDE_EDGE"}:
            return False, None, None
        if decision.selected_side == pending.policy_side:
            trade_value = finite_float(decision.selected_trade_value)
        else:
            trade_value = None
            if snapshot is not None:
                trade_value = finite_float(snapshot.get(f"{pending.policy_side.lower()}_policy_value"))
        required_edge = finite_float(pending.snapshot.get("entry_required_edge_at_limit"))
        if required_edge is None:
            required_edge = finite_float(decision.entry_edge_threshold)
        if trade_value is None or required_edge is None:
            return False, trade_value, required_edge
        edge_at_pending_price = trade_value - float(pending.price)
        return edge_at_pending_price + 1e-12 >= required_edge, edge_at_pending_price, required_edge

    def _pending_entry_can_latch_no_longer_viable(
        self,
        pending: PendingEntryOrder,
        decision: EntryDecision | None,
        *,
        age_seconds: float,
        pending_edge: float | None,
        pending_required_edge: float | None,
    ) -> bool:
        if decision is None:
            return False
        latch_seconds = finite_float(
            getattr(self.config, "policy_no_longer_viable_latch_seconds", POLICY_NO_LONGER_VIABLE_LATCH_SECONDS)
        )
        if latch_seconds is None:
            latch_seconds = POLICY_NO_LONGER_VIABLE_LATCH_SECONDS
        if age_seconds > latch_seconds:
            return False
        blockers = set(decision.blockers or ())
        if blockers - {"POLICY_NO_SIDE_EDGE"}:
            return False
        if pending_edge is None or pending_required_edge is None:
            return False
        return pending_edge + 1e-12 >= POLICY_NO_LONGER_VIABLE_LATCH_MIN_EDGE

    def _fast_entry_quote(
        self,
        now_utc: datetime,
        snapshot: dict[str, Any],
        decision: EntryDecision,
    ) -> FastEntryQuote | None:
        market = self.current_market
        if market is None or not decision.selected_side:
            return None
        book = self._side_book(decision.selected_side)
        if book.bid is None or book.ask is None or book.ask < book.bid:
            return None
        active_min_entry, active_max_entry = self._entry_price_bounds_for_snapshot(snapshot)
        ask = float(book.ask)
        if not (active_min_entry <= ask <= active_max_entry):
            return None
        tick = market.tick_size if market.tick_size > 0 else 0.01
        health_status = _execution_health_status(
            getattr(self, "recent_clob_ack_ms", collections.deque(maxlen=EXECUTION_HEALTH_WINDOW))
        )
        tolerance_ticks = _entry_tolerance_ticks_for_execution_health(health_status)
        tolerated = ask + (tolerance_ticks * tick)
        price = round_price(min(tolerated, active_max_entry), tick)
        if price + 1e-12 < ask:
            return None
        trade_value = finite_float(decision.selected_trade_value)
        base_required_edge = self._required_entry_edge_at_fill(now_utc, decision)
        if trade_value is None or base_required_edge is None:
            return None
        edge_at_ask = trade_value - ask
        required_edge_at_limit = _required_limit_edge_for_execution_health(health_status, base_required_edge)
        if edge_at_ask + 1e-12 < required_edge_at_limit:
            return None
        edge_at_limit = trade_value - price
        if edge_at_limit + 1e-12 < required_edge_at_limit:
            return None
        shares, target_notional, min_order_size = self._entry_target_size(price=price, market=market)
        if shares <= self.config.dust_share_threshold or shares + 1e-9 < min_order_size:
            return None
        spread_ticks = None
        if book.ask is not None and book.bid is not None and tick > 0:
            spread_ticks = int(round((float(book.ask) - float(book.bid)) / tick))
        return FastEntryQuote(
            price=price,
            shares=shares,
            target_notional=target_notional,
            min_order_size=min_order_size,
            spread_ticks=spread_ticks,
            book=book,
            edge_at_ask=edge_at_ask,
            edge_at_limit=edge_at_limit,
            required_edge=required_edge_at_limit,
            execution_health_status=health_status,
            tolerance_ticks=tolerance_ticks,
        )

    def _live_retry_cooldown_remaining(self, market_slug: str, now_utc: datetime) -> float:
        last_attempt = self.last_entry_attempt_time_by_window.get(market_slug)
        if last_attempt is None:
            return 0.0
        elapsed = (now_utc - last_attempt).total_seconds()
        return max(0.0, self.config.live_entry_retry_cooldown_seconds - elapsed)

    def _entry_lifecycle_state(self, market_slug: str) -> str:
        return str(getattr(self, "entry_lifecycle_by_window", {}).get(market_slug) or "NONE")

    def _set_entry_lifecycle_state(self, market_slug: str, state: str) -> None:
        if not hasattr(self, "entry_lifecycle_by_window"):
            self.entry_lifecycle_by_window = {}
        self.entry_lifecycle_by_window[market_slug] = state

    def _entry_lifecycle_blocks_entry(self, market_slug: str) -> bool:
        return self._entry_lifecycle_state(market_slug) in ENTRY_LIFECYCLE_BLOCKING_STATES

    def _entry_snapshot_with_meta(
        self,
        now_utc: datetime,
        snapshot: dict[str, Any],
        decision: EntryDecision,
        *,
        price: float,
        spread_ticks: int | None,
        min_order_size: float,
        target_notional: float,
        execution_mode: str,
        edge_at_ask: float | None = None,
        edge_at_limit: float | None = None,
        required_edge_at_limit: float | None = None,
        execution_health_status: str | None = None,
        entry_tolerance_ticks: int | None = None,
    ) -> dict[str, Any]:
        stake_decision = self.last_staking_decision if self.config.stake_mode in {"dynamic_shares", "ladder_shares"} else None
        snapshot_with_meta = dict(snapshot)
        snapshot_with_meta.update(
            {
                "policy_side": decision.selected_side,
                "policy_key": decision.policy_key_text,
                "entry_policy_trade_value": decision.selected_trade_value,
                "entry_policy_edge": decision.selected_policy_edge,
                "entry_edge_threshold": decision.entry_edge_threshold,
                "entry_session_enabled": decision.entry_session_enabled,
                "entry_trade_style": decision.selected_trade_style,
                "entry_quote_price": price,
                "entry_spread_ticks": spread_ticks,
                "entry_stake_mode": self.config.stake_mode,
                "entry_min_order_size": min_order_size,
                "entry_target_notional": target_notional,
                "entry_bps_threshold": decision.threshold_bps,
                "entry_staking_high_water": stake_decision.high_water if stake_decision else None,
                "entry_staking_drawdown_pct": stake_decision.drawdown_pct if stake_decision else None,
                "entry_execution_mode": execution_mode,
                "entry_edge_at_ask": edge_at_ask,
                "entry_edge_at_limit": edge_at_limit,
                "entry_required_edge_at_limit": required_edge_at_limit,
                "entry_execution_health_status": execution_health_status,
                "entry_tolerance_ticks": entry_tolerance_ticks,
            }
        )
        return snapshot_with_meta

    def _entry_submission_inflight(self) -> bool:
        lock = getattr(self, "_entry_submission_lock", None)
        if lock is None:
            return getattr(self, "_entry_submission", None) is not None
        with lock:
            return self._entry_submission is not None

    def _start_execution_worker(self) -> None:
        if self.config.execution_mode != "event_driven_ws":
            return
        thread = self._execution_worker_thread
        if thread is not None and thread.is_alive():
            return
        self._execution_worker_stop.clear()
        self._execution_worker_ready.clear()
        thread = threading.Thread(
            target=self._execution_worker,
            name="stats-entry-execution-worker",
            daemon=True,
        )
        self._execution_worker_thread = thread
        thread.start()
        self._execution_worker_ready.wait(timeout=3.0)

    def _execution_worker(self) -> None:
        self._execution_worker_ready.set()
        while not self._execution_worker_stop.is_set():
            try:
                submission = self._entry_submission_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if submission is None:
                self._entry_submission_queue.task_done()
                break
            worker_started = datetime.now(tz=timezone.utc)
            queue_delay_ms = max(0.0, (worker_started - submission.submitted_at_utc).total_seconds() * 1000.0)
            with self._entry_submission_lock:
                if self._entry_submission is not None and self._entry_submission.submission_id == submission.submission_id:
                    self._entry_submission_state = "SUBMITTING"
            started = time.perf_counter()
            try:
                if submission.execution_mode == "paper_fast_limit_async":
                    result = OrderResult(
                        True,
                        f"paper-fast-entry-{submission.submission_id}",
                        "MATCHED",
                        "paper shadow immediate ask fill",
                        matched_shares=submission.quote.shares,
                        price=float(submission.quote.book.ask or submission.quote.price),
                    )
                elif submission.signed_order is not None:
                    result = self.pm.post_signed_buy_order(submission.signed_order)
                else:
                    result = self.pm.place_maker_buy(
                        token_id=submission.token_id,
                        price=submission.quote.price,
                        shares=submission.quote.shares,
                        market=submission.market,
                    )
            except Exception as exc:
                result = OrderResult(False, None, "ERROR", str(exc))
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            completed = datetime.now(tz=timezone.utc)
            self._entry_submission_results.put(
                EntrySubmissionResult(
                    submission=submission,
                    result=result,
                    worker_started_at_utc=worker_started,
                    completed_at_utc=completed,
                    queue_delay_ms=queue_delay_ms,
                    submit_elapsed_ms=elapsed_ms,
                )
            )
            self._execution_wakeup.set()
            self._entry_submission_queue.task_done()

    def _update_late_submission_state(self, now_utc: datetime) -> None:
        with self._entry_submission_lock:
            submission = self._entry_submission
            if submission is None or self._entry_submission_late_logged:
                return
            age = (now_utc - submission.submitted_at_utc).total_seconds()
            if age < EXECUTION_LATE_WARNING_SECONDS:
                return
            self._entry_submission_state = "LATE"
            self._entry_submission_late_logged = True
            self._set_entry_lifecycle_state(submission.market_slug, "UNKNOWN")
        self._log_latency(
            "submission_late",
            market_slug=submission.market_slug,
            source="execution_watchdog",
            submission_id=submission.submission_id,
            side=submission.policy_side,
            attempt=submission.attempt_count,
            limit_price=submission.quote.price,
            entry_path_elapsed_ms=round(age * 1000.0, 3),
            detail="duplicate protection remains active; no retry",
        )
        self._log_event(
            "entry_submission_late",
            "Entry response exceeded 1.0s; duplicate protection remains active",
            {"submission_id": submission.submission_id, "market_slug": submission.market_slug, "age_seconds": round(age, 3)},
        )

    def _place_entry_event_driven(self, now_utc: datetime, snapshot: dict[str, Any], decision: EntryDecision) -> None:
        """Atomically reserve one market attempt and queue it for submission."""
        market = self.current_market
        if market is None or not decision.viable or not decision.selected_side:
            return
        if getattr(self, "peak_drawdown_kill_active", False):
            return
        if self.config.paper_trading and self.config.paper_fill_model != "fast_limit":
            self._place_entry(now_utc, snapshot, decision)
            return
        if self.config.observe_first_window and self.current_window_ts == self.startup_window_ts:
            return
        if not self._can_enter_after_cooldown(market.slug, now_utc):
            return
        if self.pending_entry is not None or self.active_position is not None or self._entry_submission_inflight():
            return
        if self._entry_lifecycle_blocks_entry(market.slug):
            return
        if self.entry_fills_by_window.get(market.slug, 0) > 0:
            return
        if not self.config.paper_trading:
            if self._reconcile_live_wallet_fills_for_window(now_utc, market.slug):
                return
        attempts = self.entry_attempts_by_window.get(market.slug, 0)
        attempt_limit = max(1, int(getattr(self.config, "live_max_entry_attempts_per_window", 1) or 1))
        if not self.config.paper_trading and attempts >= attempt_limit:
            self._set_entry_lifecycle_state(market.slug, "UNKNOWN")
            self._log_event(
                "entry_skipped",
                "SKIPPED_LIVE_ENTRY_ATTEMPT_LIMIT",
                {"market_slug": market.slug, "attempts": attempts, "attempt_limit": attempt_limit},
            )
            return
        if not self.config.paper_trading and self._live_retry_cooldown_remaining(market.slug, now_utc) > 0:
            return
        quote = self._fast_entry_quote(now_utc, snapshot, decision) if self.config.paper_trading or self._presigned_entries_available() else None
        signed_order = None
        execution_mode = "paper_fast_limit_async" if self.config.paper_trading else "presigned_fast_limit_async"
        if quote is not None and not self.config.paper_trading:
            signed_order = self._presign_pop(market.slug, decision.selected_side, quote.price, quote.shares)
        if quote is None or (signed_order is None and not self.config.paper_trading):
            price, spread_ticks, book = self._maker_quote(decision.selected_side)
            if price is None:
                return
            shares, target_notional, min_order_size = self._entry_target_size(price=price, market=market)
            if shares <= self.config.dust_share_threshold or shares + 1e-9 < min_order_size:
                return
            trade_value = finite_float(decision.selected_trade_value)
            required_edge = self._required_entry_edge_at_fill(now_utc, decision)
            if trade_value is None or required_edge is None:
                return
            edge_at_limit = trade_value - price
            if edge_at_limit + 1e-12 < required_edge:
                return
            quote = FastEntryQuote(
                price=price,
                shares=shares,
                target_notional=target_notional,
                min_order_size=min_order_size,
                spread_ticks=spread_ticks,
                book=book,
                edge_at_ask=edge_at_limit,
                edge_at_limit=edge_at_limit,
                required_edge=required_edge,
                execution_health_status="Maker",
                tolerance_ticks=0,
            )
            execution_mode = "maker_post_only_async"

        submitted_at = datetime.now(tz=timezone.utc)
        decision_to_submit_ms = max(0.0, (submitted_at - now_utc).total_seconds() * 1000.0)
        quote_book_age_ms = max(0.0, (submitted_at - quote.book.captured_at_utc).total_seconds() * 1000.0)
        attempt_count = attempts + 1
        submission_id = uuid.uuid4().hex[:12]
        snapshot_with_meta = self._entry_snapshot_with_meta(
            now_utc,
            snapshot,
            decision,
            price=quote.price,
            spread_ticks=quote.spread_ticks,
            min_order_size=quote.min_order_size,
            target_notional=quote.target_notional,
            execution_mode=execution_mode,
            edge_at_ask=quote.edge_at_ask,
            edge_at_limit=quote.edge_at_limit,
            required_edge_at_limit=quote.required_edge,
            execution_health_status=quote.execution_health_status,
            entry_tolerance_ticks=quote.tolerance_ticks,
        )
        submission = EntrySubmission(
            submission_id=submission_id,
            market_slug=market.slug,
            token_id=self._selected_token_id(decision.selected_side),
            policy_side=decision.selected_side,
            market=market,
            signed_order=signed_order,
            execution_mode=execution_mode,
            quote=quote,
            snapshot=snapshot_with_meta,
            policy_key_text=decision.policy_key_text,
            wallet_balance_before=finite_float(self.wallet_balance_live),
            attempt_count=attempt_count,
            decision_at_utc=now_utc,
            submitted_at_utc=submitted_at,
        )
        with self._entry_submission_lock:
            if self._entry_submission is not None or self.pending_entry is not None or self.active_position is not None:
                return
            if self._entry_lifecycle_blocks_entry(market.slug):
                return
            self._entry_submission = submission
            self._entry_submission_state = "QUEUED"
            self._entry_submission_late_logged = False
            self._set_entry_lifecycle_state(market.slug, "SUBMITTING")
            self.entry_attempts_by_window[market.slug] = attempt_count
            self.last_entry_attempt_time_by_window[market.slug] = submitted_at

        self._log_latency(
            "submit_start",
            market_slug=market.slug,
            source="event_loop",
            decision_ts_utc=now_utc.isoformat(),
            submit_started_ts_utc="",
            submission_id=submission_id,
            side=decision.selected_side,
            attempt=attempt_count,
            observed_bid=quote.book.bid,
            observed_ask=quote.book.ask,
            limit_price=quote.price,
            trade_value=decision.selected_trade_value,
            required_edge=quote.required_edge,
            edge_at_ask=quote.edge_at_ask,
            edge_at_limit=quote.edge_at_limit,
            execution_health_status=quote.execution_health_status,
            entry_tolerance_ticks=quote.tolerance_ticks,
            decision_to_submit_ms=round(decision_to_submit_ms, 3),
            quote_book_age_ms=round(quote_book_age_ms, 3),
            quote_source=quote.book.source,
            detail=execution_mode,
        )
        try:
            self._entry_submission_queue.put_nowait(submission)
        except queue.Full:
            with self._entry_submission_lock:
                if self._entry_submission is submission:
                    self._entry_submission = None
                    self._entry_submission_state = "IDLE"
                    self._set_entry_lifecycle_state(market.slug, "FAILED")
            self._remember_error("Execution queue unexpectedly full")

    def _drain_entry_submission_results(self, now_utc: datetime) -> None:
        while True:
            try:
                submission_result = self._entry_submission_results.get_nowait()
            except queue.Empty:
                return
            submission = submission_result.submission
            result = submission_result.result
            completed = submission_result.completed_at_utc
            elapsed_ms = submission_result.submit_elapsed_ms
            transport_metrics = result.transport_metrics or {}
            wire_elapsed_ms = finite_float(transport_metrics.get("wire_elapsed_ms"))
            if wire_elapsed_ms is None:
                wire_elapsed_ms = elapsed_ms
            local_overhead_ms = max(0.0, elapsed_ms - wire_elapsed_ms)
            client_pipeline_ms = submission_result.queue_delay_ms + local_overhead_ms
            decision_to_response_ms = max(0.0, (completed - submission.decision_at_utc).total_seconds() * 1000.0)
            recent_client_pipeline_ms = getattr(self, "recent_client_pipeline_ms", None)
            recent_clob_ack_ms = getattr(self, "recent_clob_ack_ms", None)
            recent_decision_to_response_ms = getattr(self, "recent_decision_to_response_ms", None)
            if recent_client_pipeline_ms is None or recent_clob_ack_ms is None or recent_decision_to_response_ms is None:
                recent_client_pipeline_ms = collections.deque(maxlen=EXECUTION_HEALTH_WINDOW)
                recent_clob_ack_ms = collections.deque(maxlen=EXECUTION_HEALTH_WINDOW)
                recent_decision_to_response_ms = collections.deque(maxlen=EXECUTION_HEALTH_WINDOW)
                self.recent_client_pipeline_ms = recent_client_pipeline_ms
                self.recent_clob_ack_ms = recent_clob_ack_ms
                self.recent_decision_to_response_ms = recent_decision_to_response_ms
            recent_client_pipeline_ms.append(client_pipeline_ms)
            recent_clob_ack_ms.append(wire_elapsed_ms)
            recent_decision_to_response_ms.append(decision_to_response_ms)
            with self._entry_submission_lock:
                current = self._entry_submission
                if current is None or current.submission_id != submission.submission_id:
                    continue
            self._log_latency(
                "submit_response",
                market_slug=submission.market_slug,
                source="paper_shadow" if self.config.paper_trading else "clob_rest",
                decision_ts_utc=submission.decision_at_utc.isoformat(),
                submit_started_ts_utc=submission_result.worker_started_at_utc.isoformat(),
                response_ts_utc=completed.isoformat(),
                submit_elapsed_ms=round(elapsed_ms, 3),
                wire_elapsed_ms=round(wire_elapsed_ms, 3),
                local_submit_overhead_ms=round(local_overhead_ms, 3),
                entry_path_elapsed_ms=round(decision_to_response_ms, 3),
                decision_to_submit_ms=round(
                    max(0.0, (submission.submitted_at_utc - submission.decision_at_utc).total_seconds() * 1000.0),
                    3,
                ),
                worker_queue_delay_ms=round(submission_result.queue_delay_ms, 3),
                quote_book_age_ms=round(
                    max(0.0, (submission.submitted_at_utc - submission.quote.book.captured_at_utc).total_seconds() * 1000.0),
                    3,
                ),
                quote_source=submission.quote.book.source,
                connection_idle_seconds=result.transport_metrics.get("connection_idle_seconds", "") if result.transport_metrics else "",
                warm_probe_age_seconds=result.transport_metrics.get("warm_probe_age_seconds", "") if result.transport_metrics else "",
                warm_probe_ok=result.transport_metrics.get("warm_probe_ok", "") if result.transport_metrics else "",
                http_version=result.transport_metrics.get("http_version", "") if result.transport_metrics else "",
                http_status=result.transport_metrics.get("http_status", "") if result.transport_metrics else "",
                cf_ray=result.transport_metrics.get("cf_ray", "") if result.transport_metrics else "",
                server_timing=result.transport_metrics.get("server_timing", "") if result.transport_metrics else "",
                transport_error_type=result.transport_metrics.get("transport_error_type", "") if result.transport_metrics else "",
                submission_id=submission.submission_id,
                order_id=result.order_id or "",
                order_status=result.status,
                side=submission.policy_side,
                attempt=submission.attempt_count,
                observed_bid=submission.quote.book.bid,
                observed_ask=submission.quote.book.ask,
                limit_price=submission.quote.price,
                required_edge=submission.quote.required_edge,
                edge_at_ask=submission.quote.edge_at_ask,
                edge_at_limit=submission.quote.edge_at_limit,
                execution_health_status=submission.quote.execution_health_status,
                entry_tolerance_ticks=submission.quote.tolerance_ticks,
                detail=(result.message or "")[:240],
            )
            if not result.ok or not result.order_id:
                self._remember_error(f"Entry submission failed: {result.status} {result.message[:160]}")
                self._log_event(
                    "fast_entry_failed" if submission.signed_order is not None else "maker_entry_failed",
                    result.status,
                    {
                        "side": submission.policy_side,
                        "price": submission.quote.price,
                        "attempts": submission.attempt_count,
                        "execution_health_status": submission.quote.execution_health_status,
                        "entry_tolerance_ticks": submission.quote.tolerance_ticks,
                        "submit_elapsed_ms": round(elapsed_ms, 1),
                        "entry_path_elapsed_ms": round((completed - submission.decision_at_utc).total_seconds() * 1000.0, 1),
                        "message": result.message[:500],
                    },
                )
                with self._entry_submission_lock:
                    if self._entry_submission is current:
                        self._entry_submission = None
                        self._entry_submission_state = "IDLE"
                        self._set_entry_lifecycle_state(submission.market_slug, "FAILED")
                continue

            pending = PendingEntryOrder(
                order_id=result.order_id,
                token_id=submission.token_id,
                side="BUY",
                price=submission.quote.price,
                target_shares=submission.quote.shares,
                created_at_utc=submission.submitted_at_utc,
                market_slug=submission.market_slug,
                policy_key_text=submission.policy_key_text,
                policy_side=submission.policy_side,
                snapshot=submission.snapshot,
                target_notional=submission.quote.target_notional,
                wallet_balance_before=submission.wallet_balance_before,
            )
            market_still_current = self.current_market is not None and self.current_market.slug == submission.market_slug
            already_filled = self.entry_fills_by_window.get(submission.market_slug, 0) > 0 or self.active_position is not None
            risk_blocked = getattr(self, "peak_drawdown_kill_active", False)
            if not market_still_current or already_filled or risk_blocked:
                if risk_blocked and market_still_current and not already_filled and not self.config.paper_trading:
                    if self._open_new_live_fill_if_any(pending, completed, "KILL_SWITCH_POST_RESPONSE"):
                        with self._entry_submission_lock:
                            if self._entry_submission is current:
                                self._entry_submission = None
                                self._entry_submission_state = "IDLE"
                        continue
                try:
                    if not self.config.paper_trading:
                        self.pm.cancel_order(result.order_id)
                except Exception:
                    pass
                self._log_event(
                    "fast_entry_residual_cancelled",
                    "stale or risk-blocked async submission response",
                    {"order_id": result.order_id, "risk_blocked": risk_blocked},
                )
                with self._entry_submission_lock:
                    if self._entry_submission is current:
                        self._entry_submission = None
                        self._entry_submission_state = "IDLE"
                        if already_filled:
                            self._set_entry_lifecycle_state(submission.market_slug, "FILLED")
                        else:
                            self._set_entry_lifecycle_state(submission.market_slug, "CANCELLED")
                continue

            self.pending_entry = pending
            self.entry_attempt_orders_by_window.setdefault(submission.market_slug, []).append(pending)
            self._set_entry_lifecycle_state(submission.market_slug, "RESTING")
            self._log_event(
                "fast_entry_placed" if submission.signed_order is not None or self.config.paper_trading else "maker_entry_placed",
                (
                    "[PAPER] fast-limit BUY submitted"
                    if self.config.paper_trading
                    else "pre-signed GTC BUY submitted"
                    if submission.signed_order is not None
                    else "post-only GTC BUY submitted asynchronously"
                ),
                {
                    "order_id": result.order_id,
                    "status": result.status,
                    "side": submission.policy_side,
                    "limit_price": submission.quote.price,
                    "shares": submission.quote.shares,
                    "target_notional": submission.quote.target_notional,
                    "spread_ticks": submission.quote.spread_ticks,
                    "edge_at_limit": submission.quote.edge_at_limit,
                    "required_edge_at_limit": submission.quote.required_edge,
                    "matched_shares": result.matched_shares,
                    "avg_price": result.price,
                    "submit_elapsed_ms": round(elapsed_ms, 1),
                    "entry_path_elapsed_ms": round((completed - submission.decision_at_utc).total_seconds() * 1000.0, 1),
                    "worker_queue_delay_ms": round(submission_result.queue_delay_ms, 1),
                    "submission_id": submission.submission_id,
                },
            )
            with self._entry_submission_lock:
                if self._entry_submission is current:
                    self._entry_submission = None
                    self._entry_submission_state = "IDLE"
            if result.matched_shares > 0 or _order_status_is_fill_like(result.status):
                if (
                    not self.config.paper_trading
                    and result.matched_shares > 0
                    and result.matched_shares + 1e-9 < submission.quote.shares
                ):
                    try:
                        self.pm.cancel_order(result.order_id)
                    except Exception:
                        pass
                fill_price = result.price if result.price is not None else submission.quote.price
                self._open_new_live_fill_from_progress(
                    pending, completed, "ASYNC_ORDER_POST", result.status.lower(), result.matched_shares, fill_price
                )

    def _drain_stream_events(self, now_utc: datetime) -> None:
        reconcile_pending = False
        latest_trigger: tuple[str, datetime, datetime | None] | None = None
        self._last_policy_trigger = None
        coalesced: dict[tuple[str, str, str], tuple[datetime, dict[str, Any], int]] = {}
        while True:
            try:
                event_type, received_at, fields = self._stream_events.get_nowait()
            except queue.Empty:
                break
            exchange_at = fields.get("exchange_ts_utc")
            age_ms = None
            if isinstance(exchange_at, datetime):
                age_ms = max(0.0, (received_at - exchange_at).total_seconds() * 1000.0)
            if latest_trigger is None or received_at >= latest_trigger[1]:
                latest_trigger = (event_type, received_at, exchange_at if isinstance(exchange_at, datetime) else None)
            key = (event_type, str(fields.get("token_id") or ""), str(fields.get("order_id") or ""))
            previous = coalesced.get(key)
            coalesced[key] = (received_at, fields, (previous[2] if previous else 0) + 1)
            if event_type == "user_order" and self.pending_entry is not None:
                order_id = str(fields.get("order_id") or "")
                if not order_id or order_id == self.pending_entry.order_id:
                    reconcile_pending = True
        for (event_type, token_id, _order_id), (received_at, fields, count) in coalesced.items():
            exchange_at = fields.get("exchange_ts_utc")
            age_ms = None
            if isinstance(exchange_at, datetime):
                age_ms = max(0.0, (received_at - exchange_at).total_seconds() * 1000.0)
            book = self.pm.stream_book_snapshot(token_id, max_age_seconds=10.0) if token_id else None
            source_label = {
                "coinbase": "coinbase_ws",
                "binance_spot": "binance_spot_ws",
                "binance_perp": "binance_perp_ws",
                "pm_chainlink": "polymarket_chainlink_ws",
            }.get(event_type, "polymarket_ws")
            self._log_latency(
                event_type,
                market_slug=fields.get("market_slug"),
                source=source_label,
                source_event_ts_utc=exchange_at.isoformat() if isinstance(exchange_at, datetime) else received_at.isoformat(),
                source_received_ts_utc=received_at.isoformat(),
                source_age_ms=round(age_ms, 3) if age_ms is not None else "",
                order_id=fields.get("order_id", ""),
                order_status=fields.get("status", ""),
                observed_bid=book.bid if book else "",
                observed_ask=book.ask if book else "",
                detail=(
                    f"{fields.get('user_event_type', '')};coalesced={count}"
                    if count > 1
                    else fields.get("user_event_type", "")
                ),
            )
        if latest_trigger is not None:
            self._last_policy_trigger = latest_trigger
        if reconcile_pending and self.pending_entry is not None:
            self._open_new_live_fill_if_any(self.pending_entry, now_utc, "USER_WS_TRIGGER")

    def _request_entry(self, now_utc: datetime, snapshot: dict[str, Any], decision: EntryDecision) -> None:
        if getattr(self.config, "execution_mode", "legacy_polling") == "event_driven_ws":
            self._place_entry_event_driven(now_utc, snapshot, decision)
        else:
            self._place_entry(now_utc, snapshot, decision)

    def _log_viable_decision(self, now_utc: datetime, decision: EntryDecision) -> None:
        if not decision.viable or not decision.selected_side or self.current_market is None:
            return
        book = self._side_book(decision.selected_side)
        trigger = self._last_policy_trigger
        trigger_type = trigger[0] if trigger else "timer"
        trigger_received = trigger[1] if trigger else book.captured_at_utc
        trigger_exchange = trigger[2] if trigger and trigger[2] is not None else trigger_received
        age_ms = max(0.0, (now_utc - trigger_received).total_seconds() * 1000.0)
        required_edge = self._required_entry_edge_at_fill(now_utc, decision)
        self._log_latency(
            "decision_viable",
            market_slug=self.current_market.slug,
            source=f"policy:{trigger_type}",
            source_event_ts_utc=trigger_exchange.isoformat(),
            source_received_ts_utc=trigger_received.isoformat(),
            source_age_ms=round(age_ms, 3),
            decision_ts_utc=now_utc.isoformat(),
            side=decision.selected_side,
            observed_bid=book.bid,
            observed_ask=book.ask,
            trade_value=decision.selected_trade_value,
            required_edge=required_edge,
            edge_at_ask=(
                float(decision.selected_trade_value) - float(book.ask)
                if decision.selected_trade_value is not None and book.ask is not None
                else ""
            ),
            edge_at_limit=(
                float(decision.selected_trade_value) - float(book.ask)
                if decision.selected_trade_value is not None and book.ask is not None
                else ""
            ),
            detail=decision.policy_key_text,
        )

    def _shutdown_execution(self) -> None:
        deadline = time.monotonic() + 10.0
        while self._entry_submission_inflight() and time.monotonic() < deadline:
            self._log_latency("shutdown_wait_submission", source="shutdown")
            self._execution_wakeup.wait(timeout=0.1)
            self._execution_wakeup.clear()
            self._drain_entry_submission_results(datetime.now(tz=timezone.utc))
        self._drain_entry_submission_results(datetime.now(tz=timezone.utc))
        if self.pending_entry is not None and not self.config.paper_trading:
            order_id = self.pending_entry.order_id
            self._cancel_pending_entry("BOT_SHUTDOWN")
            log.info(f"  Cancelled pending entry {order_id[:12]} on shutdown")
        if self._entry_submission_inflight():
            self._remember_error("Entry submission still unresolved after 10s shutdown wait")
            log.warning("  Entry submission is still unresolved; check open orders before restarting live mode.")
        self._execution_worker_stop.set()
        try:
            self._entry_submission_queue.put_nowait(None)
        except queue.Full:
            pass
        thread = self._execution_worker_thread
        if thread is not None:
            thread.join(timeout=2.0)
        self.pm.stop_streams()

    def _place_entry(self, now_utc: datetime, snapshot: dict[str, Any], decision: EntryDecision) -> None:
        entry_path_started = time.perf_counter()
        market = self.current_market
        if market is None or not decision.viable or not decision.selected_side:
            return
        if getattr(self, "peak_drawdown_kill_active", False):
            return
        if self.config.observe_first_window and self.current_window_ts == self.startup_window_ts:
            return
        if not self._can_enter_after_cooldown(market.slug, now_utc):
            return
        if self.pending_entry is not None or self.active_position is not None:
            return
        if self._entry_lifecycle_blocks_entry(market.slug):
            self._log_event(
                "entry_skipped",
                "SKIPPED_ENTRY_LIFECYCLE_ACTIVE",
                {"market_slug": market.slug, "entry_lifecycle": self._entry_lifecycle_state(market.slug)},
            )
            return
        if self.entry_fills_by_window.get(market.slug, 0) > 0:
            self._log_event("entry_skipped", "SKIPPED_WINDOW_ALREADY_FILLED", {"market_slug": market.slug})
            return
        wallet_balance_before: float | None = None
        if not self.config.paper_trading:
            if self._reconcile_live_wallet_fills_for_window(now_utc, market.slug):
                return
            attempts = self.entry_attempts_by_window.get(market.slug, 0)
            attempt_limit = max(1, int(getattr(self.config, "live_max_entry_attempts_per_window", 1) or 1))
            if attempts >= attempt_limit:
                self._set_entry_lifecycle_state(market.slug, "UNKNOWN")
                self._log_event(
                    "entry_skipped",
                    "SKIPPED_LIVE_ENTRY_ATTEMPT_LIMIT",
                    {"market_slug": market.slug, "attempts": attempts, "attempt_limit": attempt_limit},
                )
                return
            remaining = self._live_retry_cooldown_remaining(market.slug, now_utc)
            if remaining > 0:
                self._log_event(
                    "entry_skipped",
                    "SKIPPED_LIVE_ENTRY_RETRY_COOLDOWN",
                    {"market_slug": market.slug, "remaining_seconds": round(remaining, 3)},
                )
                return
            # A fresh balance request adds network latency before an otherwise ready-to-send
            # pre-signed order. Use the cached balance on the fast path; retries are still
            # reconciled above before another order can be submitted for this window.
            wallet_balance_before = finite_float(self.wallet_balance_live)
            fast_quote = self._fast_entry_quote(now_utc, snapshot, decision) if self._presigned_entries_available() else None
            if fast_quote is not None:
                signed_order = self._presign_pop(market.slug, decision.selected_side, fast_quote.price, fast_quote.shares)
                if signed_order is not None:
                    snapshot_with_meta = self._entry_snapshot_with_meta(
                        now_utc,
                        snapshot,
                        decision,
                        price=fast_quote.price,
                        spread_ticks=fast_quote.spread_ticks,
                        min_order_size=fast_quote.min_order_size,
                        target_notional=fast_quote.target_notional,
                        execution_mode="presigned_fast_limit",
                        edge_at_ask=fast_quote.edge_at_ask,
                        edge_at_limit=fast_quote.edge_at_limit,
                        required_edge_at_limit=fast_quote.required_edge,
                        execution_health_status=fast_quote.execution_health_status,
                        entry_tolerance_ticks=fast_quote.tolerance_ticks,
                    )
                    attempt_count = int(self.entry_attempts_by_window.get(market.slug, 0)) + 1
                    self.entry_attempts_by_window[market.slug] = attempt_count
                    submitted_at_utc = datetime.now(tz=timezone.utc)
                    self.last_entry_attempt_time_by_window[market.slug] = submitted_at_utc
                    self._set_entry_lifecycle_state(market.slug, "SUBMITTING")
                    submit_started = time.perf_counter()
                    result = self.pm.post_signed_buy_order(signed_order)
                    submit_elapsed_ms = (time.perf_counter() - submit_started) * 1000.0
                    entry_path_elapsed_ms = (time.perf_counter() - entry_path_started) * 1000.0
                    if not result.ok or not result.order_id:
                        self._remember_error(f"Pre-signed entry failed: {result.status} {result.message[:160]}")
                        self._log_event(
                            "fast_entry_failed",
                            result.status,
                            {
                                "side": decision.selected_side,
                                "price": fast_quote.price,
                                "shares": fast_quote.shares,
                                "target_notional": fast_quote.target_notional,
                                "min_order_size": fast_quote.min_order_size,
                                "stake_mode": self.config.stake_mode,
                                "attempts": attempt_count,
                                "edge_at_ask": fast_quote.edge_at_ask,
                                "edge_at_limit": fast_quote.edge_at_limit,
                                "required_edge_at_limit": fast_quote.required_edge,
                                "execution_health_status": fast_quote.execution_health_status,
                                "entry_tolerance_ticks": fast_quote.tolerance_ticks,
                                "submit_elapsed_ms": round(submit_elapsed_ms, 1),
                                "entry_path_elapsed_ms": round(entry_path_elapsed_ms, 1),
                                "message": result.message[:500],
                            },
                        )
                        self._set_entry_lifecycle_state(market.slug, "FAILED")
                        return
                    self.pending_entry = PendingEntryOrder(
                        order_id=result.order_id,
                        token_id=self._selected_token_id(decision.selected_side),
                        side="BUY",
                        price=fast_quote.price,
                        target_shares=fast_quote.shares,
                        created_at_utc=submitted_at_utc,
                        market_slug=market.slug,
                        policy_key_text=decision.policy_key_text,
                        policy_side=decision.selected_side,
                        snapshot=snapshot_with_meta,
                        target_notional=fast_quote.target_notional,
                        wallet_balance_before=wallet_balance_before,
                    )
                    self.entry_attempt_orders_by_window.setdefault(market.slug, []).append(self.pending_entry)
                    self._set_entry_lifecycle_state(market.slug, "RESTING")
                    self._log_event(
                        "fast_entry_placed",
                        "pre-signed GTC BUY submitted",
                        {
                            "order_id": result.order_id,
                            "status": result.status,
                            "side": decision.selected_side,
                            "limit_price": fast_quote.price,
                            "shares": fast_quote.shares,
                            "target_notional": fast_quote.target_notional,
                            "min_order_size": fast_quote.min_order_size,
                            "stake_mode": self.config.stake_mode,
                            "spread_ticks": fast_quote.spread_ticks,
                            "edge_at_ask": fast_quote.edge_at_ask,
                            "edge_at_limit": fast_quote.edge_at_limit,
                            "required_edge_at_limit": fast_quote.required_edge,
                            "execution_health_status": fast_quote.execution_health_status,
                            "entry_tolerance_ticks": fast_quote.tolerance_ticks,
                            "matched_shares": result.matched_shares,
                            "avg_price": result.price,
                            "submit_elapsed_ms": round(submit_elapsed_ms, 1),
                            "entry_path_elapsed_ms": round(entry_path_elapsed_ms, 1),
                            "wallet_balance_source": "cached",
                        },
                    )
                    if result.matched_shares > 0 or _order_status_is_fill_like(result.status):
                        if result.matched_shares > 0 and result.matched_shares + 1e-9 < fast_quote.shares:
                            try:
                                self.pm.cancel_order(result.order_id)
                                self._log_event(
                                    "fast_entry_residual_cancelled",
                                    "cancelled residual after partial immediate fill",
                                    {
                                        "order_id": result.order_id,
                                        "matched_shares": result.matched_shares,
                                        "target_shares": fast_quote.shares,
                                    },
                                )
                            except Exception as exc:
                                self._remember_error(f"Fast entry residual cancel failed: {exc}")
                        fill_price = result.price if result.price is not None else fast_quote.price
                        self._open_new_live_fill_from_progress(
                            self.pending_entry,
                            now_utc,
                            "ORDER_POST",
                            result.status.lower(),
                            result.matched_shares,
                            fill_price,
                        )
                    return
            # The fallback maker path still benefits from a fresh balance snapshot because
            # it is not latency-sensitive in the same way as a marketable pre-signed order.
            wallet_balance_before = self._refresh_wallet_balance(force=True)
        if self.config.paper_trading and self.config.paper_fill_model == "fast_limit":
            fast_quote = self._fast_entry_quote(now_utc, snapshot, decision)
            if fast_quote is not None and fast_quote.book.ask is not None:
                fill_price = float(fast_quote.book.ask)
                snapshot_with_meta = self._entry_snapshot_with_meta(
                    now_utc,
                    snapshot,
                    decision,
                    price=fast_quote.price,
                    spread_ticks=fast_quote.spread_ticks,
                    min_order_size=fast_quote.min_order_size,
                    target_notional=fast_quote.target_notional,
                    execution_mode="presigned_fast_limit",
                    edge_at_ask=fast_quote.edge_at_ask,
                    edge_at_limit=fast_quote.edge_at_limit,
                    required_edge_at_limit=fast_quote.required_edge,
                    execution_health_status=fast_quote.execution_health_status,
                    entry_tolerance_ticks=fast_quote.tolerance_ticks,
                )
                pseudo_id = f"paper-fast-entry-{int(time.time() * 1000)}"
                self.pending_entry = PendingEntryOrder(
                    order_id=pseudo_id,
                    token_id=self._selected_token_id(decision.selected_side),
                    side="BUY",
                    price=fast_quote.price,
                    target_shares=fast_quote.shares,
                    created_at_utc=now_utc,
                    market_slug=market.slug,
                    policy_key_text=decision.policy_key_text,
                    policy_side=decision.selected_side,
                    snapshot=snapshot_with_meta,
                    target_notional=fast_quote.target_notional,
                )
                self._log_event(
                    "fast_entry_placed",
                    "[PAPER] fast-limit BUY filled at current ask",
                    {
                        "order_id": pseudo_id,
                        "side": decision.selected_side,
                        "limit_price": fast_quote.price,
                        "fill_price": fill_price,
                        "shares": fast_quote.shares,
                        "target_notional": fast_quote.target_notional,
                        "min_order_size": fast_quote.min_order_size,
                        "stake_mode": self.config.stake_mode,
                        "spread_ticks": fast_quote.spread_ticks,
                        "edge_at_ask": fast_quote.edge_at_ask,
                        "edge_at_limit": fast_quote.edge_at_limit,
                        "required_edge_at_limit": fast_quote.required_edge,
                        "execution_health_status": fast_quote.execution_health_status,
                        "entry_tolerance_ticks": fast_quote.tolerance_ticks,
                    },
                )
                self._open_position_from_entry(self.pending_entry, fast_quote.shares, fill_price, now_utc)
                self.pending_entry = None
                return

        price, spread_ticks, book = self._maker_quote(decision.selected_side)
        if price is None:
            self._log_event(
                "entry_skipped",
                "SKIPPED_NO_MAKER_SPREAD" if spread_ticks is not None else "SKIPPED_NO_VALID_BOOK",
                {
                    "side": decision.selected_side,
                    "spread_ticks": spread_ticks,
                    "bid": book.bid,
                    "ask": book.ask,
                },
            )
            return
        shares, target_notional, min_order_size = self._entry_target_size(price=price, market=market)
        if shares <= self.config.dust_share_threshold:
            self._log_event("entry_skipped", "SKIPPED_TARGET_SHARES_DUST", {"shares": shares, "price": price})
            return
        if shares + 1e-9 < min_order_size:
            self._log_event(
                "entry_skipped",
                "SKIPPED_BELOW_MIN_ORDER_SIZE",
                {
                    "shares": shares,
                    "min_order_size": min_order_size,
                    "price": price,
                    "target_notional": target_notional,
                    "stake_mode": self.config.stake_mode,
                },
            )
            return
        snapshot_with_meta = self._entry_snapshot_with_meta(
            now_utc,
            snapshot,
            decision,
            price=price,
            spread_ticks=spread_ticks,
            min_order_size=min_order_size,
            target_notional=target_notional,
            execution_mode="maker_post_only",
            execution_health_status="Maker",
            entry_tolerance_ticks=0,
        )
        if self.config.paper_trading:
            pseudo_id = f"paper-entry-{int(time.time() * 1000)}"
            self.pending_entry = PendingEntryOrder(
                order_id=pseudo_id,
                token_id=self._selected_token_id(decision.selected_side),
                side="BUY",
                price=price,
                target_shares=shares,
                created_at_utc=now_utc,
                market_slug=market.slug,
                policy_key_text=decision.policy_key_text,
                policy_side=decision.selected_side,
                snapshot=snapshot_with_meta,
                target_notional=target_notional,
            )
            self._log_event(
                "maker_entry_placed",
                "[PAPER] post-only GTC BUY placed",
                {
                    "side": decision.selected_side,
                    "price": price,
                    "shares": shares,
                    "target_notional": target_notional,
                    "min_order_size": min_order_size,
                    "stake_mode": self.config.stake_mode,
                    "spread_ticks": spread_ticks,
                },
            )
            if self.config.paper_fill_model == "immediate":
                self._open_position_from_entry(self.pending_entry, shares, price, now_utc)
                self.pending_entry = None
            return

        attempt_count = int(self.entry_attempts_by_window.get(market.slug, 0)) + 1
        self.entry_attempts_by_window[market.slug] = attempt_count
        self.last_entry_attempt_time_by_window[market.slug] = now_utc
        self._set_entry_lifecycle_state(market.slug, "SUBMITTING")
        result = self.pm.place_maker_buy(token_id=self._selected_token_id(decision.selected_side), price=price, shares=shares, market=market)
        if not result.ok or not result.order_id:
            self._remember_error(f"Maker entry failed: {result.status} {result.message[:160]}")
            self._log_event(
                "maker_entry_failed",
                result.status,
                {
                    "side": decision.selected_side,
                    "price": price,
                    "shares": shares,
                    "target_notional": target_notional,
                    "min_order_size": min_order_size,
                    "stake_mode": self.config.stake_mode,
                    "attempts": attempt_count,
                    "message": result.message[:500],
                },
            )
            self._set_entry_lifecycle_state(market.slug, "FAILED")
            return
        self.pending_entry = PendingEntryOrder(
            order_id=result.order_id,
            token_id=self._selected_token_id(decision.selected_side),
            side="BUY",
            price=price,
            target_shares=shares,
            created_at_utc=now_utc,
            market_slug=market.slug,
            policy_key_text=decision.policy_key_text,
            policy_side=decision.selected_side,
            snapshot=snapshot_with_meta,
            target_notional=target_notional,
            wallet_balance_before=wallet_balance_before,
        )
        self.entry_attempt_orders_by_window.setdefault(market.slug, []).append(self.pending_entry)
        self._set_entry_lifecycle_state(market.slug, "RESTING")
        self._log_event(
            "maker_entry_placed",
            "post-only GTC BUY placed",
            {
                "order_id": result.order_id,
                "side": decision.selected_side,
                "price": price,
                "shares": shares,
                "target_notional": target_notional,
                "min_order_size": min_order_size,
                "stake_mode": self.config.stake_mode,
                "spread_ticks": spread_ticks,
            },
        )

    def _order_progress(self, order_raw: object | None, pending: PendingEntryOrder) -> tuple[str, float, float]:
        if order_raw is None:
            return "", pending.matched_shares_seen, pending.price
        if isinstance(order_raw, dict):
            status = str(order_raw.get("status", "")).lower()
            matched = float(
                order_raw.get("size_matched")
                or order_raw.get("sizeMatched")
                or order_raw.get("matched_size")
                or order_raw.get("matchedSize")
                or order_raw.get("filled_size")
                or order_raw.get("filledSize")
                or order_raw.get("matched")
                or 0.0
            )
            price = float(order_raw.get("price") or order_raw.get("avg_price") or order_raw.get("avgPrice") or pending.price)
            return status, matched, price
        status = str(getattr(order_raw, "status", "")).lower()
        matched = float(
            getattr(order_raw, "size_matched", None)
            or getattr(order_raw, "sizeMatched", None)
            or getattr(order_raw, "matched_size", None)
            or getattr(order_raw, "matchedSize", None)
            or getattr(order_raw, "filled_size", None)
            or getattr(order_raw, "filledSize", None)
            or getattr(order_raw, "matched", None)
            or 0.0
        )
        price = float(getattr(order_raw, "price", None) or getattr(order_raw, "avg_price", None) or getattr(order_raw, "avgPrice", None) or pending.price)
        return status, matched, price

    def _open_new_live_fill_if_any(self, pending: PendingEntryOrder, now_utc: datetime, context: str) -> bool:
        if self.config.paper_trading:
            return False
        status, matched, price = self._order_progress(self.pm.get_order(pending.order_id), pending)
        return self._open_new_live_fill_from_progress(pending, now_utc, context, status, matched, price)

    def _open_new_live_fill_from_progress(
        self,
        pending: PendingEntryOrder,
        now_utc: datetime,
        context: str,
        status: str,
        matched: float,
        price: float,
    ) -> bool:
        if _order_status_is_fill_like(status) and matched <= pending.matched_shares_seen + 1e-9:
            matched = max(float(pending.target_shares or 0.0), float(matched or 0.0))
            self._log_event(
                "maker_entry_fill_inferred",
                "ORDER_STATUS_MATCHED_WITHOUT_MATCHED_SHARES",
                {"order_id": pending.order_id, "status": status, "inferred_matched": matched, "price": price},
            )
        if matched <= pending.matched_shares_seen + 1e-9:
            return False
        delta = matched - pending.matched_shares_seen
        pending.matched_shares_seen = matched
        self._log_event(
            "maker_entry_fill_reconciled",
            context,
            {"order_id": pending.order_id, "status": status, "matched": matched, "delta": delta, "price": price},
        )
        self._log_latency(
            "fill_reconciled",
            market_slug=pending.market_slug,
            source=context,
            order_id=pending.order_id,
            order_status=status,
            side=pending.policy_side,
            limit_price=pending.price,
            detail=f"matched={matched:.6f};delta={delta:.6f};fill_price={price:.6f}",
        )
        self._open_position_from_entry(pending, delta, price, now_utc)
        self.pending_entry = None
        return True

    def _reconcile_live_wallet_fills_for_window(self, now_utc: datetime, market_slug: str) -> bool:
        if self.config.paper_trading:
            return False
        attempts = self.entry_attempt_orders_by_window.get(market_slug, [])
        pending_attempts = [attempt for attempt in attempts if not attempt.wallet_reconciled]
        if not pending_attempts:
            return False
        current_wallet = self._refresh_wallet_balance(force=False)
        if current_wallet is None:
            return False

        inferred_any = False
        assigned_cost = 0.0
        for attempt in pending_attempts:
            if attempt.wallet_balance_before is None or attempt.target_notional is None:
                continue
            wallet_drop = attempt.wallet_balance_before - current_wallet - assigned_cost
            if wallet_drop <= self.config.live_wallet_reconcile_tolerance_usd:
                continue
            inferred_cost = min(max(0.0, wallet_drop), float(attempt.target_notional))
            inferred_shares = round(inferred_cost / max(attempt.price, 0.001), 6)
            if inferred_shares <= self.config.dust_share_threshold:
                continue
            attempt.wallet_reconciled = True
            assigned_cost += inferred_shares * attempt.price
            inferred_any = True
            self._log_event(
                "maker_entry_wallet_reconciled",
                "WALLET_BALANCE_DECREASED_AFTER_ENTRY_ATTEMPT",
                {
                    "order_id": attempt.order_id,
                    "side": attempt.policy_side,
                    "price": attempt.price,
                    "inferred_shares": inferred_shares,
                    "inferred_cost": inferred_shares * attempt.price,
                    "wallet_before": attempt.wallet_balance_before,
                    "wallet_now": current_wallet,
                },
            )
            self._open_position_from_entry(attempt, inferred_shares, attempt.price, now_utc)
            if self.active_position is not None:
                return True
        return inferred_any

    def _sync_live_wallet_after_known_entry_fill(
        self,
        pending: PendingEntryOrder,
        *,
        filled_shares: float,
        fill_price: float,
    ) -> None:
        if self.config.paper_trading or getattr(self, "wallet_balance_live", None) is None:
            return
        expected_after: float | None = None
        wallet_before = finite_float(pending.wallet_balance_before)
        if wallet_before is not None:
            cumulative_shares = max(float(pending.matched_shares_seen or 0.0), float(filled_shares))
            expected_after = wallet_before - cumulative_shares * float(fill_price)
        else:
            expected_after = finite_float(getattr(self, "balance", None))
        if expected_after is not None and self.wallet_balance_live > expected_after:
            self.wallet_balance_live = max(0.0, float(expected_after))

    def _open_position_from_entry(self, pending: PendingEntryOrder, filled_shares: float, fill_price: float, now_utc: datetime) -> None:
        market = self.current_market
        if market is None:
            return
        filled_shares = max(0.0, filled_shares)
        if filled_shares <= self.config.dust_share_threshold:
            return
        cost = filled_shares * fill_price
        self.balance -= cost
        self._sync_live_wallet_after_known_entry_fill(pending, filled_shares=filled_shares, fill_price=fill_price)
        self.entry_fills_by_window[market.slug] = int(self.entry_fills_by_window.get(market.slug, 0)) + 1
        self._set_entry_lifecycle_state(market.slug, "FILLED")
        if (
            self.active_position is not None
            and self.active_position.market_slug == market.slug
            and self.active_position.token_id == pending.token_id
            and self.active_position.held_side == pending.policy_side
        ):
            position = self.active_position
            total_cost = position.entry_cost + cost
            total_shares = position.shares + filled_shares
            position.shares = total_shares
            position.entry_cost = total_cost
            position.avg_entry_price = total_cost / total_shares if total_shares > 0 else fill_price
            self._log_event(
                "entry_filled",
                f"ENTRY FILLED ADDITIONAL {pending.policy_side}",
                {
                    "side": pending.policy_side,
                    "shares": filled_shares,
                    "avg_entry_price": position.avg_entry_price,
                    "cost": cost,
                    "total_shares": position.shares,
                    "total_cost": position.entry_cost,
                },
            )
            return
        self.active_position = OpenPosition(
            token_id=pending.token_id,
            market_slug=market.slug,
            market_id=market.market_id,
            market_question=market.question,
            market_end_utc=market.market_end_utc,
            held_side=pending.policy_side,
            shares=filled_shares,
            avg_entry_price=fill_price,
            entry_cost=cost,
            entry_time_utc=now_utc,
            entry_bps=float(pending.snapshot.get("cb_signed_bps_raw", pending.snapshot.get("signed_bps_raw", float("nan")))),
            entry_day_type=str(pending.snapshot.get("day_type", "")),
            entry_session_bucket=str(pending.snapshot.get("session_bucket", "")),
            entry_edge_threshold=float(pending.snapshot.get("entry_edge_threshold")),
            entry_bps_threshold=float(pending.snapshot.get("entry_bps_threshold")),
            entry_policy_key_text=pending.policy_key_text,
            entry_snapshot=dict(pending.snapshot),
        )
        self._log_event(
            "entry_filled",
            f"ENTRY FILLED {pending.policy_side}",
            {
                "side": pending.policy_side,
                "shares": filled_shares,
                "avg_entry_price": fill_price,
                "cost": cost,
                "entry_bps": self.active_position.entry_bps,
                "entry_edge_threshold": self.active_position.entry_edge_threshold,
                "exit_bps_threshold": self.active_position.entry_bps_threshold,
            },
        )
        if not self.config.paper_trading:
            try:
                self.pm.cancel_order(pending.order_id)
            except Exception:
                pass

    def _cancel_pending_entry(self, reason: str) -> None:
        pending = self.pending_entry
        if pending is None:
            return
        self._log_latency(
            "cancel_start",
            market_slug=pending.market_slug,
            source="entry_manager",
            order_id=pending.order_id,
            side=pending.policy_side,
            limit_price=pending.price,
            detail=reason,
        )
        now_utc = datetime.now(tz=timezone.utc)
        if not self.config.paper_trading:
            if self._open_new_live_fill_if_any(pending, now_utc, f"PRE_CANCEL_{reason}"):
                return
            try:
                self.pm.cancel_order(pending.order_id)
            except Exception as exc:
                self._remember_error(f"Entry cancel failed: {exc}")
            if self._open_new_live_fill_if_any(pending, now_utc, f"POST_CANCEL_{reason}"):
                return
        age = (now_utc - pending.created_at_utc).total_seconds()
        self._log_event(
            "maker_entry_cancelled",
            reason,
            {"order_id": pending.order_id, "side": pending.policy_side, "price": pending.price, "age": round(age, 3)},
        )
        self._log_latency(
            "cancel_complete",
            market_slug=pending.market_slug,
            source="clob_rest",
            order_id=pending.order_id,
            side=pending.policy_side,
            limit_price=pending.price,
            detail=reason,
        )
        self.pending_entry = None
        self._set_entry_lifecycle_state(pending.market_slug, "CANCELLED")

    def _manage_pending_entry(self, now_utc: datetime, snapshot: dict[str, Any] | None, decision: EntryDecision | None) -> None:
        pending = self.pending_entry
        if pending is None:
            return
        if self.config.paper_trading and self.config.paper_fill_model == "maker_touch":
            book = self.pm.get_order_book_snapshot(pending.token_id)
            if book.ask is not None and book.ask <= pending.price:
                self._open_position_from_entry(pending, pending.target_shares, pending.price, now_utc)
                self.pending_entry = None
                return
        elif not self.config.paper_trading:
            status, matched, price = self._order_progress(self.pm.get_order(pending.order_id), pending)
            if self._open_new_live_fill_from_progress(pending, now_utc, "ORDER_POLL", status, matched, price):
                return
            if status in {"filled"}:
                self.pending_entry = None
                self._set_entry_lifecycle_state(pending.market_slug, "FILLED")
                return
            if status in {"cancelled", "canceled"}:
                self._log_event("maker_entry_cancelled", "exchange_cancelled", {"order_id": pending.order_id})
                self.pending_entry = None
                self._set_entry_lifecycle_state(pending.market_slug, "CANCELLED")
                return

        age = (now_utc - pending.created_at_utc).total_seconds()
        max_resting_age = finite_float(getattr(self.config, "entry_resting_max_age_seconds", math.inf))
        if max_resting_age is not None and age >= max_resting_age:
            self._cancel_pending_entry("ENTRY_RESTING_MAX_AGE")
            return
        pending_edge_ok, pending_edge, pending_required_edge = self._pending_entry_still_has_edge(pending, decision, snapshot)
        if snapshot is None or decision is None:
            self._cancel_pending_entry("POLICY_NO_LONGER_VIABLE")
            return
        if decision.selected_side and decision.selected_side != pending.policy_side:
            self._cancel_pending_entry("POLICY_SIDE_CHANGED")
            return
        no_longer_viable_latched = False
        policy_key_changed = decision.policy_key_text != pending.policy_key_text
        if policy_key_changed and pending_edge_ok:
            self._log_latency(
                "pending_entry_kept",
                market_slug=pending.market_slug,
                source="entry_manager",
                order_id=pending.order_id,
                side=pending.policy_side,
                limit_price=pending.price,
                detail=(
                    "policy_key_changed_kept;"
                    f"resting_edge={pending_edge:.6f};"
                    f"required={pending_required_edge:.6f};"
                    f"old_key={pending.policy_key_text};"
                    f"new_key={decision.policy_key_text}"
                ),
            )
        if not pending_edge_ok:
            no_longer_viable_latched = self._pending_entry_can_latch_no_longer_viable(
                pending,
                decision,
                age_seconds=age,
                pending_edge=pending_edge,
                pending_required_edge=pending_required_edge,
            )
            if not no_longer_viable_latched:
                self._cancel_pending_entry("POLICY_EDGE_NO_LONGER_VALID")
                return
            self._log_latency(
                "pending_entry_kept",
                market_slug=pending.market_slug,
                source="entry_manager",
                order_id=pending.order_id,
                side=pending.policy_side,
                limit_price=pending.price,
                detail=(
                    "policy_no_longer_viable_latch;"
                    f"age={age:.3f};resting_edge={pending_edge:.6f};"
                    f"normal_required={pending_required_edge:.6f};"
                    f"latch_seconds={getattr(self.config, 'policy_no_longer_viable_latch_seconds', POLICY_NO_LONGER_VIABLE_LATCH_SECONDS):.3f};"
                    f"latch_min={POLICY_NO_LONGER_VIABLE_LATCH_MIN_EDGE:.6f}"
                ),
            )
        if self.config.stake_mode in {"dynamic_shares", "ladder_shares"} and self.current_market is not None:
            current_shares, _, _ = self._entry_target_size(price=pending.price, market=self.current_market)
            if not math.isclose(current_shares, pending.target_shares, abs_tol=1e-9):
                self._cancel_pending_entry("STAKE_TARGET_CHANGED")
                if self.pending_entry is None and self.active_position is None:
                    self._request_entry(now_utc, snapshot, decision)
                return
        execution_mode = str(pending.snapshot.get("entry_execution_mode") or "maker_post_only")
        if execution_mode.startswith("presigned_fast_limit"):
            fast_quote = (
                self._fast_entry_quote(now_utc, snapshot, decision)
                if decision.selected_side == pending.policy_side
                else None
            )
            if fast_quote is None and (pending_edge_ok or no_longer_viable_latched):
                spread_ticks = None
            elif fast_quote is None:
                self._cancel_pending_entry("FAST_LIMIT_NO_LONGER_VALID")
                return
            else:
                desired_price = fast_quote.price
                spread_ticks = fast_quote.spread_ticks
                if not math.isclose(desired_price, pending.price, abs_tol=1e-9) and not pending_edge_ok:
                    self._cancel_pending_entry("FAST_LIMIT_CHANGED")
                    self._request_entry(now_utc, snapshot, decision)
                    return
            if fast_quote is not None and not math.isclose(fast_quote.price, pending.price, abs_tol=1e-9) and pending_edge_ok:
                self._log_latency(
                    "pending_entry_kept",
                    market_slug=pending.market_slug,
                    source="entry_manager",
                    order_id=pending.order_id,
                    side=pending.policy_side,
                    limit_price=pending.price,
                    detail=(
                        f"resting_edge={pending_edge:.6f};required={pending_required_edge:.6f};"
                        f"fresh_price={fast_quote.price:.6f}"
                    ),
                )
        else:
            desired_price, spread_ticks, _ = self._maker_quote(pending.policy_side)
            if desired_price is None:
                self._cancel_pending_entry("MAKER_SPREAD_NO_LONGER_VALID")
                return
            if not math.isclose(desired_price, pending.price, abs_tol=1e-9):
                self._cancel_pending_entry("MAKER_PRICE_CHANGED")
                self._request_entry(now_utc, snapshot, decision)
                return
        if age >= self.config.entry_quote_max_age_seconds:
            self._log_event(
                "maker_entry_revalidated",
                "quote still valid",
                {
                    "order_id": pending.order_id,
                    "age": round(age, 3),
                    "price": pending.price,
                    "spread_ticks": spread_ticks,
                    "edge_at_pending_price": pending_edge,
                    "required_edge": pending_required_edge,
                },
            )

    def _apply_exit_fill(self, shares_filled: float, price: float, now_utc: datetime, reason: str) -> None:
        position = self.active_position
        if position is None:
            return
        shares_filled = min(max(0.0, shares_filled), position.shares)
        if shares_filled <= 1e-9:
            return
        shares_before = position.shares
        cost_basis_delta = position.entry_cost * (shares_filled / shares_before)
        proceeds = shares_filled * price
        pnl_delta = proceeds - cost_basis_delta
        position.shares = max(0.0, position.shares - shares_filled)
        position.entry_cost = max(0.0, position.entry_cost - cost_basis_delta)
        position.exited_shares_accumulated += shares_filled
        position.exit_proceeds_accumulated += proceeds
        position.realized_pnl_accumulated += pnl_delta
        self.balance += proceeds
        self.session_pnl += pnl_delta
        self.realized_pnl += pnl_delta
        self._log_event(
            "exit_fill",
            f"EXIT FILL {position.held_side}",
            {"shares": shares_filled, "price": price, "pnl_delta": pnl_delta, "remaining": position.shares, "reason": reason},
        )
        if position.shares <= self.config.dust_share_threshold:
            position.shares = 0.0
            avg_exit = (
                position.exit_proceeds_accumulated / position.exited_shares_accumulated
                if position.exited_shares_accumulated > 0
                else price
            )
            self._finalize_trade(position, now_utc, avg_exit, reason, settled=False, settled_outcome=None)

    def _finalize_trade(
        self,
        position: OpenPosition,
        exit_time: datetime,
        exit_price: float,
        exit_reason: str,
        *,
        settled: bool,
        settled_outcome: str | None,
        settled_outcome_source: str | None = None,
    ) -> None:
        pnl = position.realized_pnl_accumulated
        self._update_peak_drawdown_kill(exit_time)
        trade_style = str(position.entry_snapshot.get("entry_trade_style") or "").strip().lower()
        if trade_style not in {"continuation", "mean_reversion"}:
            key_parts = str(position.entry_policy_key_text or "").split("|")
            bps_bucket = key_parts[3] if len(key_parts) > 3 else None
            trade_style = str(trade_style_from_bps_bucket(position.held_side, bps_bucket) or "").strip().lower()
        if trade_style not in {"continuation", "mean_reversion"}:
            trade_style = "unknown"
        if not hasattr(self, "drawdown_guard"):
            self.drawdown_guard = DrawdownGuard()
        normalised_pnl = self.drawdown_guard.record_closed_trade(
            day_type=position.entry_day_type,
            session_bucket=position.entry_session_bucket,
            trade_style=trade_style,
            pnl=pnl,
            shares=position.exited_shares_accumulated,
            market_slug=position.market_slug,
        )
        self._record_session_style_guard_closed_trade(position, trade_style, pnl, exit_time)
        self._record_regime_closed_trade(normalised_pnl, exit_time)
        self._record_staking_closed_trade(pnl)
        if pnl >= 0:
            self.total_wins += 1
            self.trade_style_wins[trade_style] += 1
        else:
            self.total_losses += 1
            self.trade_style_losses[trade_style] += 1
        self.last_exit_time_by_window[position.market_slug] = exit_time
        write_header = not self.trades_log_path.exists() or self.trades_log_path.stat().st_size == 0
        with self.trades_log_path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if write_header:
                writer.writerow(
                    [
                        "entry_time_utc",
                        "exit_time_utc",
                        "market_slug",
                        "market_id",
                        "side",
                        "shares_exited",
                        "entry_price",
                        "exit_price",
                        "entry_cost",
                        "pnl",
                        "entry_bps",
                        "exit_bps_threshold",
                        "entry_day_type",
                        "entry_session_bucket",
                        "entry_trade_style",
                        "entry_policy_key",
                        "exit_reason",
                        "settled",
                        "settled_outcome",
                        "settled_outcome_source",
                        "hedge_side",
                        "hedge_shares",
                        "hedge_price",
                        "hedge_cost",
                        "hedge_fee",
                        "hedge_trigger_source",
                        "hedge_trigger_z",
                    ]
                )
            writer.writerow(
                [
                    position.entry_time_utc.isoformat(),
                    exit_time.isoformat(),
                    position.market_slug,
                    position.market_id,
                    position.held_side,
                    f"{position.exited_shares_accumulated:.6f}",
                    f"{position.avg_entry_price:.6f}",
                    f"{exit_price:.6f}",
                    f"{position.avg_entry_price * position.exited_shares_accumulated:.6f}",
                    f"{pnl:.6f}",
                    f"{position.entry_bps:.6f}",
                    f"{position.entry_bps_threshold:.6f}",
                    position.entry_day_type,
                    position.entry_session_bucket,
                    trade_style,
                    position.entry_policy_key_text,
                    exit_reason,
                    int(settled),
                    settled_outcome or "",
                    settled_outcome_source or "",
                    position.hedge_leg.side if position.hedge_leg else "",
                    f"{position.hedge_leg.shares:.6f}" if position.hedge_leg else "",
                    f"{position.hedge_leg.avg_price:.6f}" if position.hedge_leg else "",
                    f"{position.hedge_leg.cost:.6f}" if position.hedge_leg else "",
                    f"{position.hedge_leg.fee:.6f}" if position.hedge_leg else "",
                    position.hedge_leg.trigger_source if position.hedge_leg else "",
                    f"{position.hedge_leg.trigger_z:.6f}" if position.hedge_leg else "",
                ]
            )
        self._log_event(
            "trade_closed",
            f"TRADE CLOSED pnl={pnl:+.4f}",
            {
                "pnl": pnl,
                "normalised_5_share_pnl": normalised_pnl,
                "exit_reason": exit_reason,
                "entry_trade_style": trade_style,
                "side": position.held_side,
                "settled_outcome": settled_outcome,
                "settled_outcome_source": settled_outcome_source,
                "hedged": position.hedge_leg is not None,
                "hedge_side": position.hedge_leg.side if position.hedge_leg else None,
                "hedge_trigger_source": position.hedge_leg.trigger_source if position.hedge_leg else None,
                "hedge_trigger_z": position.hedge_leg.trigger_z if position.hedge_leg else None,
            },
        )
        if self.active_position is position:
            self.active_position = None

    def _maybe_execute_exit(self, now_utc: datetime, snapshot: dict[str, Any] | None, exit_decision: ExitDecision | None, force_reason: str | None) -> None:
        position = self.active_position
        market = self.current_market
        if position is None or market is None:
            return
        if self.config.exit_policy_mode == "hold_to_resolution":
            if force_reason or (exit_decision and exit_decision.should_exit):
                self._log_event(
                    "exit_suppressed",
                    "HOLD_TO_RESOLUTION_EXIT_DISABLED",
                    {
                        "force_reason": force_reason,
                        "exit_reason": exit_decision.reason if exit_decision else None,
                    },
                )
            return
        trigger = force_reason or (exit_decision.reason if exit_decision and exit_decision.should_exit else None)
        if not trigger and not position.exit_triggered:
            return
        if trigger and not position.exit_triggered:
            position.exit_triggered = True
            position.exit_reason = trigger
            self._log_event(
                "exit_triggered",
                trigger,
                {
                    "side": position.held_side,
                    "entry_bps": position.entry_bps,
                    "current_bps": exit_decision.current_bps if exit_decision else None,
                    "threshold": position.entry_bps_threshold,
                },
            )
        if position.last_exit_attempt_utc is not None:
            if (now_utc - position.last_exit_attempt_utc).total_seconds() < self.config.exit_retry_cadence_seconds:
                return
        book = self.pm.get_order_book_snapshot(position.token_id)
        if book.bid is None:
            self._remember_error("EXIT_BLOCKED_NO_VALID_BID")
            return
        price_floor = round_price(max(float(book.bid) - market.tick_size, 0.01), market.tick_size)
        position.last_exit_attempt_utc = now_utc
        shares = max(0.0, position.shares)
        if shares <= self.config.dust_share_threshold:
            self._finalize_trade(position, now_utc, position.avg_entry_price, position.exit_reason or "DUST_CLOSED", settled=False, settled_outcome=None)
            return
        if self.config.paper_trading:
            self._log_event("exit_paper", "PAPER EXIT FILL", {"shares": shares, "price": price_floor, "reason": position.exit_reason})
            self._apply_exit_fill(shares, price_floor, now_utc, position.exit_reason)
            return
        if not position.fok_attempted:
            position.fok_attempted = True
            result = self.pm.place_immediate_sell(
                token_id=position.token_id,
                price=price_floor,
                shares=shares,
                order_type="FOK",
                market=market,
            )
            self._log_event(
                "exit_fok_submitted",
                result.status,
                {"ok": result.ok, "shares": shares, "price": price_floor, "message": result.message[:500]},
            )
            matched = result.matched_shares
            if result.order_id:
                status, polled_matched, polled_price = self._poll_immediate_order(result.order_id, price_floor)
                matched = max(matched, polled_matched)
                price_floor = polled_price or price_floor
            if result.ok and matched >= shares - self.config.dust_share_threshold:
                self._apply_exit_fill(shares, price_floor, now_utc, position.exit_reason or "FOK_EXIT")
                return
            if result.ok and result.order_id is None and matched <= 0:
                self._remember_error("FOK exit response did not include order id or matched size; waiting before FAK fallback")
                return
        # FAK fallback repeats until shares are nil excluding dust.
        shares = self.active_position.shares if self.active_position is not None else 0.0
        if shares <= self.config.dust_share_threshold:
            return
        result = self.pm.place_immediate_sell(
            token_id=position.token_id,
            price=price_floor,
            shares=shares,
            order_type="FAK",
            market=market,
        )
        self._log_event(
            "exit_fak_submitted",
            result.status,
            {"ok": result.ok, "shares": shares, "price": price_floor, "message": result.message[:500]},
        )
        matched = result.matched_shares
        if result.order_id:
            _, polled_matched, polled_price = self._poll_immediate_order(result.order_id, price_floor)
            matched = max(matched, polled_matched)
            price_floor = polled_price or price_floor
        if matched > 0:
            self._apply_exit_fill(matched, price_floor, now_utc, position.exit_reason or "FAK_EXIT")

    def _poll_immediate_order(self, order_id: str, fallback_price: float) -> tuple[str, float, float]:
        raw = self.pm.get_order(order_id)
        if raw is None:
            return "", 0.0, fallback_price
        if isinstance(raw, dict):
            return (
                str(raw.get("status", "")).lower(),
                float(raw.get("size_matched") or raw.get("matched") or 0.0),
                float(raw.get("price") or fallback_price),
            )
        return (
            str(getattr(raw, "status", "")).lower(),
            float(getattr(raw, "size_matched", 0.0) or 0.0),
            float(getattr(raw, "price", fallback_price) or fallback_price),
        )

    def _settle_ended_active_position(self, now_utc: datetime) -> None:
        position = self.active_position
        if position is None:
            return
        if position.market_end_utc > now_utc:
            return
        self._resolve_position(position, now_utc)

    def _resolve_position(self, position: OpenPosition, now_utc: datetime) -> bool:
        resolution = self.pm.resolved_market_outcome(
            slug=position.market_slug,
            market_id=position.market_id,
            condition_id=str(position.entry_snapshot.get("condition_id") or ""),
        )
        if resolution.outcome not in {"UP", "DOWN"}:
            if position not in self.settling_positions:
                self.settling_positions.append(position)
            if self.active_position is position:
                self.active_position = None
            self._log_event(
                "settlement_pending",
                "POLYMARKET_OUTCOME_NOT_RESOLVED",
                {"market_slug": position.market_slug, "source": resolution.source, "resolved": resolution.resolved},
            )
            return False
        outcome = resolution.outcome
        won = outcome == position.held_side
        primary_proceeds = position.shares if won else 0.0
        hedge_proceeds = 0.0
        hedge_cost = 0.0
        hedge_fee = 0.0
        if position.hedge_leg is not None:
            hedge_won = outcome == position.hedge_leg.side
            hedge_proceeds = position.hedge_leg.shares if hedge_won else 0.0
            hedge_cost = position.hedge_leg.cost
            hedge_fee = position.hedge_leg.fee
        proceeds = primary_proceeds + hedge_proceeds
        pnl = primary_proceeds - position.entry_cost + hedge_proceeds - hedge_cost - hedge_fee
        if proceeds > 0:
            self.balance += proceeds
        self.session_pnl += pnl
        self.realized_pnl += pnl
        position.exited_shares_accumulated += position.shares
        position.exit_proceeds_accumulated += proceeds
        position.realized_pnl_accumulated += pnl
        position.shares = 0.0
        self._finalize_trade(
            position,
            now_utc,
            1.0 if won else 0.0,
            "HOLD_TO_RESOLUTION",
            settled=True,
            settled_outcome=outcome,
            settled_outcome_source=resolution.source,
        )
        if position in self.settling_positions:
            self.settling_positions.remove(position)
        return True

    def _poll_settling_positions(self, now_utc: datetime) -> None:
        for position in list(self.settling_positions):
            self._resolve_position(position, now_utc)

    def _entry_decision_with_blockers(self, decision: EntryDecision, blockers: tuple[str, ...]) -> EntryDecision:
        if not blockers:
            return decision
        merged_blockers = tuple(dict.fromkeys([*decision.blockers, *blockers]))
        return EntryDecision(
            viable=False,
            blockers=merged_blockers,
            policy_key=decision.policy_key,
            policy_record=decision.policy_record,
            selected_side=decision.selected_side,
            selected_bid=decision.selected_bid,
            selected_ask=decision.selected_ask,
            selected_mid=decision.selected_mid,
            selected_spread=decision.selected_spread,
            selected_trade_value=decision.selected_trade_value,
            selected_policy_edge=decision.selected_policy_edge,
            selected_trade_style=decision.selected_trade_style,
            entry_edge_threshold=decision.entry_edge_threshold,
            entry_session_enabled=decision.entry_session_enabled,
            threshold_bps=decision.threshold_bps,
        )

    def _entry_decision_with_entry_threshold(
        self,
        decision: EntryDecision,
        entry_edge_threshold: float | None,
    ) -> EntryDecision:
        if entry_edge_threshold is None or decision.entry_edge_threshold == entry_edge_threshold:
            return decision
        return replace(decision, entry_edge_threshold=entry_edge_threshold)

    def _open_positions_for_drawdown_guard(self) -> list[OpenPosition]:
        positions: list[OpenPosition] = []
        if self.active_position is not None:
            positions.append(self.active_position)
        positions.extend(self.settling_positions)
        return positions

    def _entry_price_bounds_for_snapshot(self, snapshot: dict[str, Any] | None) -> tuple[float, float]:
        if snapshot is not None and str(snapshot.get("day_type") or "").strip().lower() == "weekend":
            return float(self.config.weekend_min_entry_price), float(self.config.weekend_max_entry_price)
        return float(self.config.min_entry_price), float(self.config.max_entry_price)

    def _max_entry_price_for_snapshot(self, snapshot: dict[str, Any] | None) -> float:
        return self._entry_price_bounds_for_snapshot(snapshot)[1]

    def _session_style_guard_context(
        self,
        london_dt: datetime | None,
        trade_style: str | None,
    ) -> dict[str, Any]:
        if london_dt is None:
            return {
                "london_date": "",
                "session_name": "",
                "session_label": "",
                "trade_style": str(trade_style or ""),
            }
        start = int(london_dt.hour) // 2 * 2
        end = start + 2
        session_label = f"h{start:02d}_{end:02d}" if end < 24 else f"h{start:02d}_24"
        session_name = session_label
        return {
            "london_date": london_dt.date().isoformat(),
            "session_name": str(session_name or session_label or ""),
            "session_label": str(session_label or ""),
            "trade_style": str(trade_style or "").strip().lower(),
        }

    def _session_style_guard_context_from_snapshot(
        self,
        now_utc: datetime,
        snapshot: dict[str, Any] | None,
        trade_style: str | None,
    ) -> dict[str, Any]:
        timestamp_london = snapshot.get("timestamp_london") if snapshot is not None else None
        if not isinstance(timestamp_london, datetime):
            timestamp_london = now_utc.astimezone(LONDON_ZONE)
        return self._session_style_guard_context(timestamp_london, trade_style)

    def _store_session_style_guard_detail(
        self,
        snapshot: dict[str, Any] | None,
        detail: dict[str, Any] | None,
    ) -> None:
        self.last_session_style_guard_detail = detail
        if snapshot is None:
            return
        detail = detail or {}
        snapshot["session_style_guard_key"] = detail.get("key", "")
        snapshot["session_style_guard_pnl"] = detail.get("closed_pnl")
        snapshot["session_style_guard_peak_pnl"] = detail.get("peak_pnl")
        snapshot["session_style_guard_drawdown"] = detail.get("drawdown_usd")
        snapshot["session_style_guard_threshold"] = detail.get("threshold_usd")
        snapshot["session_style_guard_drawdown_ratio"] = detail.get("drawdown_ratio")
        snapshot["session_style_guard_ladder_level"] = detail.get("ladder_level", "")
        snapshot["session_style_guard_edge_penalty"] = detail.get("edge_penalty")
        snapshot["session_style_guard_effective_edge"] = detail.get("effective_entry_edge_threshold")
        snapshot["session_style_guard_active"] = bool(detail.get("active", False))
        snapshot["session_style_guard_session"] = detail.get("session_name", "")
        snapshot["session_style_guard_style"] = detail.get("trade_style", "")

    def _session_style_guard_threshold_usd(
        self,
        decision: EntryDecision | None = None,
        *,
        shares: float | None = None,
    ) -> float:
        target_shares = finite_float(shares)
        if target_shares is None and decision is not None and self.current_market is not None:
            price = finite_float(decision.selected_ask or decision.selected_mid or decision.selected_bid)
            if price is not None:
                target_shares, _, _ = self._entry_target_size(price=price, market=self.current_market)
        if target_shares is None or target_shares <= 0:
            target_shares = float(getattr(self.config, "min_order_size_fallback", 5.0))
        return float(target_shares) * SESSION_STYLE_DRAWDOWN_PER_SHARE_USD

    def _session_style_entry_blockers(
        self,
        now_utc: datetime,
        snapshot: dict[str, Any] | None,
        decision: EntryDecision | None,
    ) -> tuple[tuple[str, ...], float | None]:
        if not bool(getattr(self.config, "session_style_drawdown_guard_enabled", False)):
            self._store_session_style_guard_detail(snapshot, None)
            return (), None
        if snapshot is None or decision is None:
            self._store_session_style_guard_detail(snapshot, None)
            return (), None
        trade_style = str(decision.selected_trade_style or "").strip().lower()
        if trade_style not in {"continuation", "mean_reversion"}:
            self._store_session_style_guard_detail(snapshot, None)
            return (), None
        if not hasattr(self, "session_style_drawdown_guard"):
            self.session_style_drawdown_guard = SessionStyleDrawdownGuard(
                drawdown_per_share_usd=SESSION_STYLE_DRAWDOWN_PER_SHARE_USD
            )
        context = self._session_style_guard_context_from_snapshot(now_utc, snapshot, trade_style)
        threshold_usd = self._session_style_guard_threshold_usd(decision)
        detail = self.session_style_drawdown_guard.details(**context, threshold_usd=threshold_usd)
        if detail["active"]:
            self._store_session_style_guard_detail(snapshot, detail)
            return ("SESSION_STYLE_DRAWDOWN",), None
        base_edge = finite_float(decision.entry_edge_threshold)
        penalty = finite_float(detail.get("edge_penalty"))
        effective_edge = None
        if base_edge is not None:
            effective_edge = base_edge + max(0.0, penalty or 0.0)
            detail["effective_entry_edge_threshold"] = effective_edge
        self._store_session_style_guard_detail(snapshot, detail)
        if effective_edge is None or effective_edge <= base_edge + 1e-12:
            return (), effective_edge
        selected_edge = finite_float(decision.selected_policy_edge)
        if selected_edge is None or selected_edge + 1e-12 < effective_edge:
            return ("SESSION_STYLE_DRAWDOWN_EDGE_LADDER",), effective_edge
        return (), effective_edge

    def _record_session_style_guard_closed_trade(
        self,
        position: OpenPosition,
        trade_style: str,
        pnl: float,
        exit_time: datetime,
    ) -> None:
        if not bool(getattr(self.config, "session_style_drawdown_guard_enabled", False)):
            return
        if not hasattr(self, "session_style_drawdown_guard"):
            self.session_style_drawdown_guard = SessionStyleDrawdownGuard(
                drawdown_per_share_usd=SESSION_STYLE_DRAWDOWN_PER_SHARE_USD
            )
        entry_london = position.entry_time_utc.astimezone(LONDON_ZONE)
        context = self._session_style_guard_context(entry_london, trade_style)
        threshold_usd = self._session_style_guard_threshold_usd(shares=position.exited_shares_accumulated)
        key, closed_pnl, activated = self.session_style_drawdown_guard.record_closed_trade(
            london_date=context["london_date"],
            session_label=context["session_label"],
            trade_style=context["trade_style"],
            pnl=pnl,
            closed_at_utc=exit_time,
            threshold_usd=threshold_usd,
        )
        if activated:
            detail = self.session_style_drawdown_guard.details(**context, threshold_usd=threshold_usd)
            self._log_event(
                "session_style_drawdown_activated",
                (
                    f"New {context['session_name']} {context['trade_style']} entries disabled "
                    f"after ${detail['drawdown_usd']:.2f} slice drawdown"
                ),
                {
                    "guard_key": key,
                    "london_date": context["london_date"],
                    "session_label": context["session_label"],
                    "session_name": context["session_name"],
                    "trade_style": context["trade_style"],
                    "closed_pnl": closed_pnl,
                    "peak_pnl": detail["peak_pnl"],
                    "drawdown_usd": detail["drawdown_usd"],
                    "threshold_usd": threshold_usd,
                    "drawdown_per_share_usd": SESSION_STYLE_DRAWDOWN_PER_SHARE_USD,
                },
            )

    def _entry_decision_with_guard(self, now_utc: datetime, snapshot: dict[str, Any] | None) -> EntryDecision | None:
        decision = self.macro_policy.evaluate(snapshot, start_block_seconds=self.config.entry_start_block_seconds)
        self.last_guard_blocker = None
        if getattr(self, "peak_drawdown_kill_active", False):
            self.last_guard_blocker = "PEAK_DRAWDOWN_KILL"
            decision = self._entry_decision_with_blockers(decision, ("PEAK_DRAWDOWN_KILL",))
        bps_yes_guard_enabled = bool(getattr(self.config, "bps_yes_guard_enabled", True))
        if bps_yes_guard_enabled:
            active, reason = self.guard.update_entry_guard(now_utc=now_utc, snapshot=snapshot)
            if active and reason:
                self.last_guard_blocker = reason
                decision = self._entry_decision_with_blockers(decision, (reason,))
        session_style_blockers, effective_entry_edge = self._session_style_entry_blockers(now_utc, snapshot, decision)
        if decision is not None and effective_entry_edge is not None:
            decision = self._entry_decision_with_entry_threshold(decision, effective_entry_edge)
        if session_style_blockers:
            self.last_guard_blocker = session_style_blockers[0]
            decision = self._entry_decision_with_blockers(decision, session_style_blockers)
        return decision

    def _update_peak_drawdown_kill(self, now_utc: datetime) -> None:
        self.peak_realized_pnl = max(float(getattr(self, "peak_realized_pnl", 0.0)), float(self.realized_pnl))
        self.peak_drawdown_usd = max(0.0, float(self.peak_realized_pnl) - float(self.realized_pnl))
        limit = float(getattr(getattr(self, "config", None), "peak_drawdown_kill_usd", 15.0))
        if getattr(self, "peak_drawdown_kill_active", False) or limit <= 0.0 or self.peak_drawdown_usd + 1e-9 < limit:
            return
        self.peak_drawdown_kill_active = True
        self.peak_drawdown_kill_triggered_at = now_utc
        self._log_event(
            "peak_drawdown_kill_activated",
            f"New entries disabled at ${self.peak_drawdown_usd:.2f} drawdown from session peak",
            {
                "realized_pnl": self.realized_pnl,
                "peak_realized_pnl": self.peak_realized_pnl,
                "drawdown_usd": self.peak_drawdown_usd,
                "limit_usd": limit,
            },
        )
        self._log_latency(
            "peak_drawdown_kill_activated",
            source="risk_guard",
            detail=f"drawdown={self.peak_drawdown_usd:.6f};limit={limit:.6f}",
        )

    def _hold_to_resolution_exit_decision(self, snapshot: dict[str, Any]) -> ExitDecision:
        assert self.active_position is not None
        side = self.active_position.held_side.lower()
        return ExitDecision(
            should_exit=False,
            reason="HOLD_TO_RESOLUTION_CONFIG",
            policy_no_longer_supportive=False,
            signed_bps_materially_against=False,
            current_policy_side=None,
            current_policy_key=build_policy_key(snapshot),
            current_bps=finite_float(snapshot.get("cb_signed_bps_raw", snapshot.get("signed_bps_raw"))),
            entry_bps=self.active_position.entry_bps,
            threshold_bps=self.active_position.entry_bps_threshold,
            exit_bid=finite_float(snapshot.get(f"{side}_bid")),
        )

    def _format_entry_blocker(
        self,
        blocker: str,
        snapshot: dict[str, Any] | None,
        now_utc: datetime,
        decision: EntryDecision | None = None,
    ) -> str:
        if blocker == "GLOBAL_SIDE_MOM_BLOCK":
            side = decision.selected_side if decision is not None else None
            _, momentum = self._side_momentum_block_details(snapshot, side)
            if side is not None and momentum is not None:
                return (
                    f"GLOBAL_SIDE_MOM_BLOCK "
                    f"({side} against {momentum:+.1f}bps/{SIDE_MOMENTUM_GUARD_WINDOW_SECONDS}s)"
                )
            return "GLOBAL_SIDE_MOM_BLOCK"
        if blocker == "SESSION_STYLE_DRAWDOWN":
            if snapshot is None:
                return "SESSION_STYLE_DRAWDOWN"
            session = str(snapshot.get("session_style_guard_session") or "session")
            style = str(snapshot.get("session_style_guard_style") or "style")
            drawdown = finite_float(snapshot.get("session_style_guard_drawdown"))
            threshold = finite_float(snapshot.get("session_style_guard_threshold"))
            if drawdown is not None and threshold is not None:
                return f"SESSION_STYLE_DRAWDOWN ({session} {style} DD ${drawdown:.2f} >= ${threshold:.2f})"
            return f"SESSION_STYLE_DRAWDOWN ({session} {style})"
        if blocker == "SESSION_STYLE_DRAWDOWN_EDGE_LADDER":
            if snapshot is None:
                return "SESSION_STYLE_DRAWDOWN_EDGE_LADDER"
            session = str(snapshot.get("session_style_guard_session") or "session")
            style = str(snapshot.get("session_style_guard_style") or "style")
            level = str(snapshot.get("session_style_guard_ladder_level") or "ladder")
            penalty = finite_float(snapshot.get("session_style_guard_edge_penalty"))
            if penalty is not None:
                return f"SESSION_STYLE_EDGE_LADDER ({session} {style} {level} +{penalty * 100:.0f}c)"
            return f"SESSION_STYLE_EDGE_LADDER ({session} {style})"
        return blocker

    def _format_entry_blockers(
        self,
        blockers: tuple[str, ...],
        snapshot: dict[str, Any] | None,
        now_utc: datetime,
        decision: EntryDecision | None = None,
    ) -> str:
        return ", ".join(self._format_entry_blocker(blocker, snapshot, now_utc, decision) for blocker in blockers[:10])

    def _record_cadence(self, now_utc: datetime, snapshot: dict[str, Any] | None, decision: EntryDecision | None, exit_decision: ExitDecision | None) -> None:
        second = int(now_utc.timestamp())
        if self.last_cadence_second_written == second:
            return
        self.last_cadence_second_written = second
        pressure_detail = self.last_pressure_defence if self.active_position is not None else {}
        write_header = not self.cadence_log_path.exists() or self.cadence_log_path.stat().st_size == 0
        with self.cadence_log_path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if write_header:
                writer.writerow(
                    [
                        "session_id",
                        "timestamp_utc",
                        "mode",
                        "market_slug",
                        "tte_seconds",
                        "elapsed_seconds_since_market_start",
                        "day_type",
                        "session_bucket",
                        "macro_session",
                        "cb_close_1s",
                        "btc_mom_20s_bps",
                        "cb_signed_bps_raw",
                        "signed_bps_bucket",
                        "volatility_smoothed_raw",
                        "volatility_bucket",
                        "local_volatility_1h_bps",
                        "local_volatility_status",
                        "local_volatility_age_minutes",
                        "up_bid",
                        "up_ask",
                        "up_mid",
                        "up_spread",
                        "down_bid",
                        "down_ask",
                        "down_mid",
                        "down_spread",
                        "policy_key",
                        "policy_matched",
                        "entry_viable",
                        "selected_side",
                        "selected_trade_value",
                        "selected_policy_edge",
                        "entry_edge_threshold",
                        "entry_session_enabled",
                        "exit_bps_threshold",
                        "selected_trade_style",
                        "policy_signal_name",
                        "up_move_dz",
                        "up_move_dz_bucket",
                        "down_move_dz",
                        "down_move_dz_bucket",
                        "selected_move_dz",
                        "selected_move_dz_bucket",
                        "distance_z_delta_2s",
                        "up_side_dz_delta_2s",
                        "down_side_dz_delta_2s",
                        "selected_side_dz_delta_2s",
                        "up_move_z",
                        "up_move_z_bucket",
                        "down_move_z",
                        "down_move_z_bucket",
                        "selected_move_z",
                        "selected_move_z_bucket",
                        "selected_price_bucket",
                        "policy_support",
                        "policy_unique_markets",
                        "policy_level",
                        "entry_blockers",
                        "bps_yes_guard_blocker",
                        "session_style_guard_key",
                        "session_style_guard_pnl",
                        "session_style_guard_peak_pnl",
                        "session_style_guard_drawdown",
                        "session_style_guard_threshold",
                        "session_style_guard_drawdown_ratio",
                        "session_style_guard_ladder_level",
                        "session_style_guard_edge_penalty",
                        "session_style_guard_effective_edge",
                        "session_style_guard_active",
                        "execution_mode",
                        "execution_state",
                        "submission_inflight",
                        "submission_age_seconds",
                        "peak_realized_pnl",
                        "peak_drawdown_usd",
                        "peak_drawdown_kill_limit_usd",
                        "peak_drawdown_kill_active",
                        "pending_entry_order",
                        "active_position",
                        "position_side",
                        "position_shares",
                        "position_entry_price",
                        "pressure_defence_status",
                        "pressure_defence_reason",
                        "pressure_held_mid",
                        "pressure_tte_seconds",
                        "pressure_adverse_btc",
                        "pressure_signed_btc",
                        "pressure_threshold_btc",
                        "exit_should_exit",
                        "exit_reason",
                        "policy_no_longer_supportive",
                        "signed_bps_materially_against",
                        "balance",
                        "wallet_balance_live",
                        "realized_pnl",
                        "window_errors",
                    ]
                )
            writer.writerow(
                [
                    self.session_id,
                    now_utc.isoformat(),
                    "paper" if self.config.paper_trading else "live",
                    self.current_market.slug if self.current_market else "",
                    _fmt(snapshot.get("canonical_tte_seconds"), 3, "") if snapshot else "",
                    _fmt(snapshot.get("elapsed_seconds_since_market_start"), 3, "") if snapshot else "",
                    snapshot.get("day_type", "") if snapshot else "",
                    snapshot.get("session_bucket", "") if snapshot else "",
                    snapshot.get("macro_session", "") if snapshot else "",
                    _fmt(snapshot.get("cb_close_1s"), 6, "") if snapshot else "",
                    _fmt(snapshot.get("btc_mom_20s_bps"), 6, "") if snapshot else "",
                    _fmt(snapshot.get("cb_signed_bps_raw"), 6, "") if snapshot else "",
                    snapshot.get("signed_bps_bucket", "") if snapshot else "",
                    _fmt(snapshot.get("volatility_smoothed_raw"), 6, "") if snapshot else "",
                    snapshot.get("volatility_bucket", "") if snapshot else "",
                    _fmt(snapshot.get("local_volatility_1h_bps"), 6, "") if snapshot else "",
                    snapshot.get("local_volatility_status", "") if snapshot else "",
                    snapshot.get("local_volatility_age_minutes", "") if snapshot else "",
                    _fmt(snapshot.get("up_bid"), 6, "") if snapshot else "",
                    _fmt(snapshot.get("up_ask"), 6, "") if snapshot else "",
                    _fmt(snapshot.get("up_mid"), 6, "") if snapshot else "",
                    _fmt(snapshot.get("up_spread"), 6, "") if snapshot else "",
                    _fmt(snapshot.get("down_bid"), 6, "") if snapshot else "",
                    _fmt(snapshot.get("down_ask"), 6, "") if snapshot else "",
                    _fmt(snapshot.get("down_mid"), 6, "") if snapshot else "",
                    _fmt(snapshot.get("down_spread"), 6, "") if snapshot else "",
                    decision.policy_key_text if decision else "",
                    int(bool(decision and decision.policy_record)),
                    int(bool(decision and decision.viable)),
                    decision.selected_side or "" if decision else "",
                    _fmt(decision.selected_trade_value, 6, "") if decision else "",
                    _fmt(decision.selected_policy_edge, 6, "") if decision else "",
                    _fmt(decision.entry_edge_threshold, 6, "") if decision else "",
                    int(bool(decision.entry_session_enabled)) if decision and decision.entry_session_enabled is not None else "",
                    _fmt(decision.threshold_bps, 6, "") if decision else "",
                    decision.selected_trade_style or "" if decision else "",
                    snapshot.get("policy_signal_name", "") if snapshot else "",
                    _fmt(snapshot.get("up_move_dz"), 6, "") if snapshot else "",
                    snapshot.get("up_move_dz_bucket", "") if snapshot else "",
                    _fmt(snapshot.get("down_move_dz"), 6, "") if snapshot else "",
                    snapshot.get("down_move_dz_bucket", "") if snapshot else "",
                    _fmt(snapshot.get("selected_move_dz"), 6, "") if snapshot else "",
                    snapshot.get("selected_move_dz_bucket", "") if snapshot else "",
                    _fmt(snapshot.get("distance_z_delta_2s"), 6, "") if snapshot else "",
                    _fmt(snapshot.get("up_side_dz_delta_2s"), 6, "") if snapshot else "",
                    _fmt(snapshot.get("down_side_dz_delta_2s"), 6, "") if snapshot else "",
                    _fmt(snapshot.get("selected_side_dz_delta_2s"), 6, "") if snapshot else "",
                    _fmt(snapshot.get("up_move_z"), 6, "") if snapshot else "",
                    snapshot.get("up_move_z_bucket", "") if snapshot else "",
                    _fmt(snapshot.get("down_move_z"), 6, "") if snapshot else "",
                    snapshot.get("down_move_z_bucket", "") if snapshot else "",
                    _fmt(snapshot.get("selected_move_z"), 6, "") if snapshot else "",
                    snapshot.get("selected_move_z_bucket", "") if snapshot else "",
                    snapshot.get("selected_price_bucket", "") if snapshot else "",
                    snapshot.get("policy_support", "") if snapshot else "",
                    snapshot.get("policy_unique_markets", "") if snapshot else "",
                    snapshot.get("policy_level", "") if snapshot else "",
                    "|".join(decision.blockers) if decision else "",
                    self.last_guard_blocker or "",
                    snapshot.get("session_style_guard_key", "") if snapshot else "",
                    _fmt(snapshot.get("session_style_guard_pnl"), 6, "") if snapshot else "",
                    _fmt(snapshot.get("session_style_guard_peak_pnl"), 6, "") if snapshot else "",
                    _fmt(snapshot.get("session_style_guard_drawdown"), 6, "") if snapshot else "",
                    _fmt(snapshot.get("session_style_guard_threshold"), 6, "") if snapshot else "",
                    _fmt(snapshot.get("session_style_guard_drawdown_ratio"), 6, "") if snapshot else "",
                    snapshot.get("session_style_guard_ladder_level", "") if snapshot else "",
                    _fmt(snapshot.get("session_style_guard_edge_penalty"), 6, "") if snapshot else "",
                    _fmt(snapshot.get("session_style_guard_effective_edge"), 6, "") if snapshot else "",
                    int(bool(snapshot.get("session_style_guard_active"))) if snapshot else "",
                    self.config.execution_mode,
                    self._entry_submission_state,
                    int(self._entry_submission_inflight()),
                    (
                        _fmt((now_utc - self._entry_submission.submitted_at_utc).total_seconds(), 3, "")
                        if self._entry_submission is not None
                        else ""
                    ),
                    f"{self.peak_realized_pnl:.6f}",
                    f"{self.peak_drawdown_usd:.6f}",
                    f"{self.config.peak_drawdown_kill_usd:.6f}",
                    int(self.peak_drawdown_kill_active),
                    self.pending_entry.order_id if self.pending_entry else "",
                    int(self.active_position is not None),
                    self.active_position.held_side if self.active_position else "",
                    _fmt(self.active_position.shares, 6, "") if self.active_position else "",
                    _fmt(self.active_position.avg_entry_price, 6, "") if self.active_position else "",
                    self.active_position.pressure_defence_status if self.active_position else "",
                    self.active_position.pressure_defence_reason if self.active_position else "",
                    _fmt(pressure_detail.get("held_mid"), 6, "") if pressure_detail else "",
                    _fmt(pressure_detail.get("tte"), 3, "") if pressure_detail else "",
                    _fmt(pressure_detail.get("pressure_adverse_volume_btc"), 6, "") if pressure_detail else "",
                    _fmt(pressure_detail.get("pressure_signed_volume_btc"), 6, "") if pressure_detail else "",
                    _fmt(pressure_detail.get("pressure_volume_threshold_btc"), 6, "") if pressure_detail else "",
                    int(bool(exit_decision and exit_decision.should_exit)),
                    exit_decision.reason if exit_decision else "",
                    int(bool(exit_decision and exit_decision.policy_no_longer_supportive)),
                    int(bool(exit_decision and exit_decision.signed_bps_materially_against)),
                    f"{self.balance:.6f}",
                    f"{self.wallet_balance_live:.6f}" if self.wallet_balance_live is not None else "",
                    f"{self.realized_pnl:.6f}",
                    " || ".join(self.window_errors),
                ]
            )

    def _dashboard(self, now_utc: datetime, snapshot: dict[str, Any] | None, decision: EntryDecision | None, exit_decision: ExitDecision | None) -> None:
        if self.last_dashboard_at is not None:
            if (now_utc - self.last_dashboard_at).total_seconds() < self.config.status_interval_seconds:
                return
        self.last_dashboard_at = now_utc
        self._refresh_wallet_balance()
        mode = "paper" if self.config.paper_trading else "live"
        balance_label = "paper_balance" if self.config.paper_trading else "wallet"
        balance_value = self.balance if self.config.paper_trading or self.wallet_balance_live is None else self.wallet_balance_live
        lines = [LINE2]
        lines.append(f"  STATS BOT")
        lines.append(f"    mode              : {mode}")
        lines.append(f"    execution_mode    : {self.config.execution_mode}")
        if self._entry_submission is not None:
            submission_age = max(0.0, (now_utc - self._entry_submission.submitted_at_utc).total_seconds())
            state = "late response; duplicate protection active" if self._entry_submission_state == "LATE" else self._entry_submission_state.lower()
            lines.append(
                f"    entry_state       : {state} {self._entry_submission.policy_side} ({submission_age:.2f}s)"
            )
        lines.append(f"    strategy          : {self.macro_policy.strategy_id} (frozen {self.macro_policy.training_end})")
        stake_decision: DynamicStakeDecision | None = None
        if self.config.stake_mode == "min_order_size":
            target_shares = self.config.min_order_size_fallback
            if self.current_market and finite_float(self.current_market.min_order_size) is not None:
                target_shares = float(self.current_market.min_order_size)
            lines.append(f"    stake_mode        : flat_shares")
            lines.append(f"    target_shares     : {target_shares:.0f}")
        elif self.config.stake_mode == "dynamic_shares":
            lines.append(f"    stake_mode        : dynamic_shares")
            stake_decision = self._dynamic_stake_decision()
            stake_reason = f" ({stake_decision.reason})" if stake_decision.reason else ""
            lines.append(f"    target_shares     : {stake_decision.target_shares:.0f}{stake_reason}")
        elif self.config.stake_mode == "ladder_shares":
            lines.append(f"    stake_mode        : ladder_shares")
            stake_decision = self._ladder_stake_decision()
            stake_reason = f" ({stake_decision.reason})" if stake_decision.reason else ""
            lines.append(f"    target_shares     : {stake_decision.target_shares:.0f}{stake_reason}")
        else:
            lines.append(f"    stake_mode        : fixed_usd")
            lines.append(f"    stake             : ${self.config.stake_usd:.2f}")
        presign_text = self._presign_dashboard_text(now_utc)
        if presign_text:
            lines.append(f"    presign           : {presign_text}")
        lines.append(f"    {balance_label:<18}: ${balance_value:.2f}")
        lines.append(f"    realized_pnl      : {self.realized_pnl:+.2f}")
        submission_fills, submission_attempts = _submission_fill_attempt_counts(
            self.entry_fills_by_window, self.entry_attempts_by_window
        )
        lines.append(f"    submission        : {submission_fills} / {submission_attempts} fills/attempts")
        lines.append(
            "    Execution health  : "
            + _execution_health_text(
                getattr(self, "recent_client_pipeline_ms", collections.deque(maxlen=EXECUTION_HEALTH_WINDOW)),
                getattr(self, "recent_clob_ack_ms", collections.deque(maxlen=EXECUTION_HEALTH_WINDOW)),
                getattr(self, "recent_decision_to_response_ms", collections.deque(maxlen=EXECUTION_HEALTH_WINDOW)),
            )
        )
        submission_state = self._entry_lifecycle_state(self.current_market.slug) if self.current_market else "NONE"
        lines.append(f"    Submission state  : {submission_state}")
        if self.config.pressure_defence_enabled:
            lines.append(
                f"    pressure_hedges   : {self.pressure_hedges_filled}/"
                f"{self.pressure_hedges_submitted} filled/submitted "
                f"({self.pressure_hedges_blocked} blocked)"
            )
        if self.peak_drawdown_kill_active:
            lines.append("    kill_switch       : ACTIVE (new entries disabled)")
        if stake_decision is not None and self.config.stake_mode == "dynamic_shares":
            lines.append(f"    high_water_dd     : {stake_decision.drawdown_pct:.1f}%")
            if stake_decision.recovery_active:
                floor = stake_decision.recovery_floor if stake_decision.recovery_floor is not None else 0.0
                anchor = stake_decision.recovery_anchor if stake_decision.recovery_anchor is not None else 0.0
                lines.append(f"    recovery          : active (anchor=${anchor:.2f}, floor={floor:+.2f})")
        lines.append(f"    wins/losses       : {self.total_wins}/{self.total_losses}")
        lines.append(
            f"    cont wins/losses  : "
            f"{self.trade_style_wins['continuation']}/{self.trade_style_losses['continuation']}"
        )
        lines.append(
            f"    MR wins/losses    : "
            f"{self.trade_style_wins['mean_reversion']}/{self.trade_style_losses['mean_reversion']}"
        )
        if self.current_market:
            lines.append("")
            lines.append("  MARKET")
            lines.append(f"    slug              : {self.current_market.slug}")
            lines.append(f"    question          : {self.current_market.question[:92]}")
            lines.append(f"    tte_seconds       : {int(snapshot['canonical_tte_seconds']) if snapshot else 'NA'}")
            lines.append(f"    tick_size         : {self.current_market.tick_size:g}")
        else:
            lines.append("")
            lines.append("  MARKET")
            lines.append("    status            : WAITING")
        if snapshot is None:
            wait = "market" if self.current_market is None else "btc/pm/local-vol snapshot"
            lines.append(f"    waiting_for       : {wait}")
        else:
            lines.append("")
            lines.append("  SIGNAL")
            day_type, session_name, cont_profile, mr_profile = self.macro_policy.entry_profile(snapshot)
            lines.append(
                f"    entry_profile     : {day_type}/{session_name} | "
                f"C={cont_profile.edge * 100:.0f}c@{cont_profile.min_tte}-{cont_profile.max_tte}s | "
                f"MR={mr_profile.edge * 100:.0f}c@{mr_profile.min_tte}-{mr_profile.max_tte}s"
            )
            if bool(snapshot.get("session_style_guard_active")):
                session = str(snapshot.get("session_style_guard_session") or "session")
                style = str(snapshot.get("session_style_guard_style") or "style")
                drawdown = finite_float(snapshot.get("session_style_guard_drawdown"))
                threshold = finite_float(snapshot.get("session_style_guard_threshold"))
                if drawdown is not None and threshold is not None:
                    lines.append(f"    style_kill        : active ({session} {style} DD ${drawdown:.2f} >= ${threshold:.2f})")
            else:
                penalty = finite_float(snapshot.get("session_style_guard_edge_penalty"))
                if penalty is not None and penalty > 0.0:
                    session = str(snapshot.get("session_style_guard_session") or "session")
                    style = str(snapshot.get("session_style_guard_style") or "style")
                    level = str(snapshot.get("session_style_guard_ladder_level") or "ladder")
                    effective = finite_float(snapshot.get("session_style_guard_effective_edge"))
                    if effective is not None:
                        lines.append(
                            f"    style_ladder      : {level} ({session} {style} +{penalty * 100:.0f}c, need {effective * 100:.0f}c)"
                        )
                    else:
                        lines.append(f"    style_ladder      : {level} ({session} {style} +{penalty * 100:.0f}c)")
            lines.append(f"    elapsed_seconds   : {_fmt(snapshot.get('elapsed_seconds_since_market_start'), 1)}")
            lines.append(f"    btc_close_1s      : {_fmt(snapshot.get('cb_close_1s'), 2)}")
            lines.append(f"    signed_bps        : {self._dashboard_signed_bps_text(snapshot)}")
            signal_name = str(snapshot.get("policy_signal_name") or "move_z")
            if signal_name not in {"move_z", "move_dz"}:
                signal_name = "move_z"
            signal_label = "move_dz" if signal_name == "move_dz" else "move_z"
            continuous_signal = (
                str(snapshot.get(f"up_{signal_name}_bucket") or "") == "continuous"
                or str(snapshot.get(f"down_{signal_name}_bucket") or "") == "continuous"
            )
            if continuous_signal:
                lines.append(
                    f"    {signal_label} UP/DOWN   : {_fmt(snapshot.get(f'up_{signal_name}'), 3)} / "
                    f"{_fmt(snapshot.get(f'down_{signal_name}'), 3)}"
                )
            else:
                lines.append(
                    f"    {signal_label} UP/DOWN   : {_fmt(snapshot.get(f'up_{signal_name}'), 3)} "
                    f"({snapshot.get(f'up_{signal_name}_bucket') or 'NA'}) / "
                    f"{_fmt(snapshot.get(f'down_{signal_name}'), 3)} ({snapshot.get(f'down_{signal_name}_bucket') or 'NA'})"
                )
            selected_signal = snapshot.get(f"selected_{signal_name}")
            if selected_signal is not None:
                selected_label = f"selected_{signal_label}"
                if continuous_signal:
                    lines.append(f"    {selected_label:<18}: {_fmt(selected_signal, 3)}")
                else:
                    lines.append(
                        f"    {selected_label:<18}: {_fmt(selected_signal, 3)} "
                        f"({snapshot.get(f'selected_{signal_name}_bucket') or 'NA'})"
                    )
            if snapshot.get("distance_z_delta_2s") is not None:
                lines.append(
                    f"    {'dz_mom_2s UP/DOWN':<18}: {_fmt(snapshot.get('up_side_dz_delta_2s'), 3)} / "
                    f"{_fmt(snapshot.get('down_side_dz_delta_2s'), 3)}"
                )
                selected_momentum = snapshot.get("selected_side_dz_delta_2s")
                if selected_momentum is not None:
                    lines.append(f"    {'selected_dz_mom_2s':<18}: {_fmt(selected_momentum, 3)}")
            if continuous_signal and decision is not None and decision.selected_side:
                side_prefix = decision.selected_side.lower()
                selected_bid = snapshot.get(f"{side_prefix}_bid")
                selected_ask = snapshot.get(f"{side_prefix}_ask")
                lines.append(
                    f"    selected_price    : ask {_fmt(selected_ask)} | bid {_fmt(selected_bid)} "
                    f"| bucket {snapshot.get('selected_price_bucket') or 'NA'}"
                )
                lines.append(
                    f"    surface_values    : UP {_fmt(snapshot.get('up_policy_value'), 3)} "
                    f"edge {_fmt(snapshot.get('up_policy_edge'), 3)} | "
                    f"DOWN {_fmt(snapshot.get('down_policy_value'), 3)} "
                    f"edge {_fmt(snapshot.get('down_policy_edge'), 3)}"
                )
                stressed_edge = finite_float(snapshot.get("selected_policy_stressed_edge"))
                if stressed_edge is not None:
                    edge = finite_float(decision.selected_policy_edge)
                    edge_text = f"{edge:+.3f}" if edge is not None else "NA"
                    lines.append(f"    edge              : {edge_text} | stressed {stressed_edge:+.3f}")
            lines.append(f"    net_signed_flow   : {self._dashboard_net_signed_flow_text()}")
            lines.append(
                f"    local_vol_1h      : {_fmt(snapshot.get('local_volatility_1h_bps'), 3)} bps "
                f"({snapshot.get('local_volatility_status')})"
            )
            lines.append("")
            lines.append("  QUOTES")
            lines.append(
                f"    UP                : bid={_fmt(snapshot.get('up_bid'))} ask={_fmt(snapshot.get('up_ask'))} "
                f"mid={_fmt(snapshot.get('up_mid'))} spread={_fmt(snapshot.get('up_spread'))}"
            )
            lines.append(
                f"    DOWN              : bid={_fmt(snapshot.get('down_bid'))} ask={_fmt(snapshot.get('down_ask'))} "
                f"mid={_fmt(snapshot.get('down_mid'))} spread={_fmt(snapshot.get('down_spread'))}"
            )
        if decision is not None:
            lines.append("")
            lines.append("  ENTRY POLICY")
            lines.append(f"    matched           : {bool(decision.policy_record)}")
            lines.append(f"    viable            : {decision.viable}")
            lines.append(
                f"    value / edge      : {_fmt(decision.selected_trade_value)} / "
                f"{_fmt(decision.selected_policy_edge)} (need {_fmt(decision.entry_edge_threshold)})"
            )
            lines.append(f"    trade_style       : {decision.selected_trade_style or 'NA'}")
            policy_support, policy_level = _policy_support_level(snapshot)
            lines.append(
                f"    support / level   : {policy_support} / {policy_level}"
            )
            label = "strategy_key" if (
                snapshot is not None and str(snapshot.get("selected_move_dz_bucket") or "") == "continuous"
            ) else "policy_key"
            lines.append(f"    {label:<18}: {decision.policy_key_text or 'NA'}")
            if decision.blockers:
                lines.append(f"    blockers          : {self._format_entry_blockers(decision.blockers, snapshot, now_utc, decision)}")
        if self.pending_entry:
            age = (now_utc - self.pending_entry.created_at_utc).total_seconds()
            lines.append("")
            lines.append("  PENDING ENTRY")
            lines.append(f"    order_id          : {self.pending_entry.order_id}")
            lines.append(f"    side              : {self.pending_entry.policy_side}")
            lines.append(f"    target_shares     : {self.pending_entry.target_shares:.4f}")
            lines.append(f"    price             : {self.pending_entry.price:.4f}")
            lines.append(f"    target_notional   : {self.pending_entry.target_shares * self.pending_entry.price:.4f}")
            lines.append(f"    age_seconds       : {age:.1f}")
            lines.append(f"    policy_key        : {self.pending_entry.policy_key_text}")
        if self.active_position:
            side = self.active_position.held_side
            current = snapshot.get(f"{side.lower()}_bid") if snapshot else None
            current_text = _fmt(current)
            upnl_text = "NA"
            if finite_float(current) is not None:
                upnl_text = f"{(float(current) - self.active_position.avg_entry_price) * self.active_position.shares:+.4f}"
            hold_seconds = (now_utc - self.active_position.entry_time_utc).total_seconds()
            lines.append("")
            lines.append("  POSITION")
            lines.append(f"    side              : {side}")
            lines.append(f"    shares            : {self.active_position.shares:.4f}")
            lines.append(f"    entry_price       : {self.active_position.avg_entry_price:.4f}")
            lines.append(f"    current_bid       : {current_text}")
            lines.append(f"    unrealized_pnl    : {upnl_text}")
            lines.append(f"    entry_cost        : {self.active_position.entry_cost:.4f}")
            lines.append(f"    hold_seconds      : {hold_seconds:.1f}")
            lines.append(f"    entry_bps         : {self.active_position.entry_bps:+.2f}")
            lines.append(f"    entry_edge_thresh : {self.active_position.entry_edge_threshold:.2f}")
            lines.append(f"    exit_policy_mode  : {self.config.exit_policy_mode}")
            lines.append(f"    policy_key        : {self.active_position.entry_policy_key_text}")
            if self.active_position.hedge_leg is not None:
                hedge = self.active_position.hedge_leg
                lines.append(f"    hedge             : {hedge.side} {hedge.shares:.4f} @ {hedge.avg_price:.4f}")
                lines.append(f"    hedge_fee         : {hedge.fee:.4f}")
                lines.append(f"    hedge_trigger     : {hedge.trigger_source} adverse_btc={hedge.trigger_z:.3f}")
            elif self.config.pressure_defence_enabled:
                lines.append(f"    pressure_defence  : {self.active_position.pressure_defence_status}")
                if self.active_position.pressure_defence_reason:
                    lines.append(f"    pressure_reason   : {self.active_position.pressure_defence_reason}")
            if self.active_position.exit_triggered:
                lines.append(f"    exit_active       : {self.active_position.exit_reason}")
                lines.append(f"    fok_attempted     : {self.active_position.fok_attempted}")
        if self.config.pressure_defence_enabled and self.last_pressure_defence:
            detail = self.last_pressure_defence
            lines.append("")
            lines.append("  PRESSURE DEFENCE")
            lines.append(f"    status            : {detail.get('status')}")
            if detail.get("reason"):
                lines.append(f"    reason            : {detail.get('reason')}")
            lines.append(f"    held_band         : {self.config.pressure_min_held_mid:.2f}-{self.config.pressure_max_held_mid:.2f}")
            lines.append(f"    held_mid          : {_fmt(detail.get('held_mid'))}")
            lines.append(f"    tte               : {_fmt(detail.get('tte'), 1)}")
            lines.append(f"    adverse_btc       : {_fmt(detail.get('pressure_adverse_volume_btc'), 3)}")
            lines.append(f"    signed_btc        : {_fmt(detail.get('pressure_signed_volume_btc'), 3)}")
            lines.append(f"    lookback_seconds  : {_fmt(detail.get('pressure_volume_lookback_seconds'), 0)}")
            lines.append(f"    threshold_btc     : {_fmt(detail.get('pressure_volume_threshold_btc'), 3)}")
            lines.append(f"    source            : {detail.get('pressure_source') or 'NA'}")
            if detail.get("hedge_side"):
                lines.append(f"    hedge_side        : {detail.get('hedge_side')}")
                lines.append(f"    hedge_ask/price   : {_fmt(detail.get('hedge_ask'))} / {_fmt(detail.get('hedge_price'))}")
                lines.append(f"    hedge_fee         : {_fmt(detail.get('hedge_fee'), 4)}")
        if bool(getattr(self.config, "bps_yes_guard_enabled", True)) and (
            self.guard.persistence_seconds or self.guard.cooldown_until_utc or self.guard.window_blocked
        ):
            lines.append("")
            lines.append("  BPS YES GUARD")
            lines.append(f"    persistence_secs  : {self.guard.persistence_seconds}")
            lines.append(f"    cooldown_remaining: {self.guard.cooldown_remaining_seconds(now_utc)}")
            lines.append(f"    window_blocked    : {self.guard.window_blocked}")
        if self.settling_positions:
            lines.append("")
            lines.append("  SETTLEMENT")
            lines.append(f"    settling_positions: {len(self.settling_positions)}")
        if self.recent_events:
            last = self.recent_events[-1]
            lines.append("")
            lines.append("  RECENT")
            lines.append(f"    last_event        : {last.event_type}")
            lines.append(f"    message           : {last.message}")
        if self.window_errors:
            lines.append("")
            lines.append("  ERRORS")
            lines.append(f"    window_errors     : {' || '.join(self.window_errors)}")
        lines.append(LINE2)
        for line in lines:
            log.info(line)

    def run(self) -> None:
        log.info(LINE)
        log.info("  STATS BOT — BTC 5-minute macro/local value strategy")
        log.info(f"  mode        : {'paper' if self.config.paper_trading else 'live'}")
        log.info(f"  execution   : {self.config.execution_mode}")
        log.info(f"  kill switch : ${self.config.peak_drawdown_kill_usd:g} drawdown from peak realised session equity")
        if self.config.stake_mode == "min_order_size":
            log.info(f"  stake mode  : flat_shares ({self.config.min_order_size_fallback:g} share fallback)")
        elif self.config.stake_mode == "dynamic_shares":
            log.info("  stake mode  : dynamic_shares (5-15 shares by raw wallet, high-water throttled)")
        elif self.config.stake_mode == "ladder_shares":
            log.info("  stake mode  : ladder_shares (wallet tier ladder with downgrade buffer)")
        else:
            log.info(f"  stake mode  : fixed_usd ${self.config.stake_usd:.2f}")
        log.info(
            f"  strategy    : {self.macro_policy.strategy_id} trained "
            f"{self.macro_policy.training_start}..{self.macro_policy.training_end}"
        )
        if getattr(self.macro_policy, "policy_type", "") == "continuous_surface":
            log.info(
                f"  model       : {self.config.macro_policy_bundle} "
                f"({len(self.macro_policy):,} continuous surface)"
            )
        else:
            log.info(f"  policy      : {self.config.macro_policy_bundle} ({len(self.macro_policy):,} hierarchical rows)")
        timed_edges = getattr(self.macro_policy, "timed_entry_overrides", [])
        if timed_edges:
            log.info(
                "  entry edges : conservative dynamic day-type/time table (%d bands, %s fallback)",
                len(timed_edges),
                self.macro_policy.strategy_id,
            )
        else:
            first_session = next(iter(self.macro_policy.session_display_names), "h00_06")
            continuation = self.macro_policy.entry_parameters("weekday", first_session, "continuation")
            mean_reversion = self.macro_policy.entry_parameters("weekday", first_session, "mean_reversion")
            log.info(
                "  entry edges : continuation=%.02f mean_reversion=%.02f (%s profile)",
                continuation.edge,
                mean_reversion.edge,
                self.macro_policy.strategy_id,
            )
        log.info(f"  exit mode   : {self.config.exit_policy_mode}")
        session_names = "/".join(dict.fromkeys(self.macro_policy.session_display_names.values()))
        if getattr(self.macro_policy, "policy_type", "") == "continuous_surface":
            log.info(
                f"  signal      : {self.macro_policy.signal_feature}; causal 1h Coinbase local RV; "
                f"{session_names} London display context"
            )
        else:
            log.info(
                f"  signal      : {self.macro_policy.signal_feature}; causal 1h Coinbase local RV; "
                f"{session_names} London calibration segments"
            )
        log.info(
            "  entry       : pre-signed GTC fast limit when ready "
            f"(ask+{PRESIGN_ENTRY_TOLERANCE_TICKS} tick, residual-edge guarded; "
            f"+{int(round(DEGRADED_ENTRY_EDGE_BUMP * 100))}c/"
            f"+{int(round(POOR_ENTRY_EDGE_BUMP * 100))}c edge in Degraded/Poor health); "
            "fallback post-only maker @ best_ask-1tick"
        )
        if not self.config.paper_trading:
            log.info(
                f"  live retry  : cooldown={self.config.live_entry_retry_cooldown_seconds:g}s "
                "entry_lifecycle=single unresolved/live/filled entry per window "
                f"wallet_tolerance=${self.config.live_wallet_reconcile_tolerance_usd:g}"
            )
        log.info(
            f"  entry window: elapsed>={self.config.entry_start_block_seconds}s, "
            f"tte={self.config.entry_min_tte_seconds}-{self.config.entry_max_tte_seconds}s "
            f"weekday price={self.config.min_entry_price:.2f}-{self.config.max_entry_price:.2f} "
            f"weekend price={self.config.weekend_min_entry_price:.2f}-{self.config.weekend_max_entry_price:.2f}"
        )
        bps_yes_guard_enabled = bool(getattr(self.config, "bps_yes_guard_enabled", True))
        if self.config.exit_policy_mode == "hold_to_resolution":
            log.info("  exit        : hold to resolution; all live tactical/guard sells disabled")
            if bps_yes_guard_enabled:
                log.info("  guard       : BPS_YES_GUARD entry blocking")
            else:
                log.info("  guard       : BPS_YES_GUARD disabled")
        else:
            log.info("  exit        : FOK SELL first; if it fails, repeated FAK SELL until shares are nil excluding dust")
            if bps_yes_guard_enabled:
                log.info("  guard       : BPS_YES_GUARD entry/position guards")
            else:
                log.info("  guard       : BPS_YES_GUARD disabled")
        log.info(f"  session     : {self.session_name}")
        log.info(f"  logs        : {self.session_dir}")
        if self.pm.wallet_address:
            log.info(f"  wallet      : {self.pm.wallet_address}")
        log.info(LINE)
        self._run_live_startup_sync()
        self._start_execution_worker()

        while True:
            try:
                if self.config.execution_mode == "event_driven_ws":
                    self._execution_wakeup.clear()
                now_utc = datetime.now(tz=timezone.utc)
                now_ts = now_utc.timestamp()
                if self.config.max_loop_seconds is not None and (now_utc - self.start_time).total_seconds() >= self.config.max_loop_seconds:
                    log.info("  Max loop seconds reached; stopping.")
                    break
                self._drain_entry_submission_results(now_utc)
                self._update_late_submission_state(now_utc)
                self._drain_stream_events(now_utc)
                self.coinbase.poll_rest_if_needed(now_ts)
                window_ts = self._window_ts(now_ts)
                if self.current_window_ts != window_ts:
                    self._on_new_window(now_utc, window_ts)
                if self.window_open_btc_price is None:
                    self.window_open_btc_price = self.coinbase.window_open_price(window_ts)
                self._capture_pm_snapshot(now_utc)
                self._poll_settling_positions(now_utc)
                snapshot = self._build_snapshot(now_utc)
                decision = self._entry_decision_with_guard(now_utc, snapshot)
                self.last_entry_decision = decision
                decision_at_utc = datetime.now(tz=timezone.utc)
                if decision is not None:
                    self._log_viable_decision(decision_at_utc, decision)
                self._manage_pending_entry(now_utc, snapshot, decision)
                if (
                    not self.config.paper_trading
                    and self.active_position is None
                    and self.pending_entry is None
                    and not self._entry_submission_inflight()
                    and self.current_market is not None
                ):
                    self._reconcile_live_wallet_fills_for_window(now_utc, self.current_market.slug)

                exit_decision = None
                force_exit_reason = None
                if self.active_position is not None and snapshot is not None:
                    self._maybe_execute_pressure_defence(now_utc, snapshot)
                    if self.config.exit_policy_mode == "hold_to_resolution":
                        exit_decision = self._hold_to_resolution_exit_decision(snapshot)
                    else:
                        exit_decision = evaluate_exit(
                            held_side=self.active_position.held_side,
                            entry_bps=self.active_position.entry_bps,
                            threshold_bps=self.active_position.entry_bps_threshold,
                            entry_edge_threshold=self.active_position.entry_edge_threshold,
                            snapshot=snapshot,
                            policy_cube=self.policy_cube,
                        )
                    if (
                        bool(getattr(self.config, "bps_yes_guard_enabled", True))
                        and self.config.exit_policy_mode != "hold_to_resolution"
                    ):
                        force_exit, force_reason = self.guard.update_position_exit_guard(
                            now_utc=now_utc,
                            snapshot=snapshot,
                            held_side=self.active_position.held_side,
                        )
                        if force_exit:
                            force_exit_reason = force_reason
                    self._maybe_execute_exit(now_utc, snapshot, exit_decision, force_exit_reason)
                elif (
                    self.active_position is None
                    and self.pending_entry is None
                    and not self._entry_submission_inflight()
                    and snapshot is not None
                    and decision is not None
                ):
                    if decision.viable:
                        self._request_entry(decision_at_utc, snapshot, decision)
                self.last_exit_decision = exit_decision
                self._record_cadence(now_utc, snapshot, decision, exit_decision)
                self._dashboard(now_utc, snapshot, decision, exit_decision)

                if self.config.once:
                    break
                if self.config.execution_mode == "event_driven_ws":
                    self._execution_wakeup.wait(timeout=self.config.poll_interval_seconds)
                else:
                    time.sleep(self.config.poll_interval_seconds)
            except KeyboardInterrupt:
                log.info("")
                log.info(LINE)
                log.info("  Stopped by user")
                log.info(f"  balance=${self.balance:.2f} rPnL={self.realized_pnl:+.2f} W/L={self.total_wins}/{self.total_losses}")
                log.info(f"  logs={self.session_dir}")
                log.info(LINE)
                break
            except Exception as exc:
                self._remember_error(f"Loop error: {exc}")
                log.warning(f"  Loop warning: {exc}")
                log.warning(traceback.format_exc().rstrip())
                if self.config.once:
                    raise
                time.sleep(1.0)
        self._shutdown_execution()


def main(argv: argparse.Namespace | None = None) -> None:
    args = argv or parse_args()
    config = build_config(args)
    log.info("  Initializing bot runtime...")
    bot = StatsBot(config)
    bot.run()

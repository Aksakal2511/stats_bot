from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv as _load_dotenv
except ModuleNotFoundError:
    _load_dotenv = None


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY_FILE = (
    REPO_ROOT / "strategy" / "policy_cube" / "policy_cube_dual_side_hold_values_offset250_12bucket_2h_cb_sources.jsonl"
)
DEFAULT_EXIT_THRESHOLDS_FILE = REPO_ROOT / "strategy" / "exit_policy" / "offset250_12bucket_2h_hold_exit_thresholds.csv"
DEFAULT_ENTRY_EDGE_THRESHOLDS_FILE = (
    REPO_ROOT / "strategy" / "entry_policy" / "offset250_12bucket_2h_cb_sources_hold_value_style_thresholds.csv"
)
DEFAULT_VOLATILITY_EDGES_FILE = (
    REPO_ROOT / "strategy" / "bucket_config" / "volatility_bucket_edges_offset250_12bucket_2h_cb_sources.json"
)
DEFAULT_LOG_DIR = REPO_ROOT / "logs"
DEFAULT_MACRO_POLICY_BUNDLE = REPO_ROOT / "strategy" / "z6_fullraw_hl90_weekday_v1" / "policy_bundle.json"


def load_env_file(path: Path) -> None:
    if _load_dotenv is not None:
        _load_dotenv(path)
        return
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return float(raw)


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class BotConfig:
    macro_policy_bundle: Path
    policy_file: Path
    exit_thresholds_file: Path
    entry_edge_thresholds_file: Path
    volatility_edges_file: Path
    log_dir: Path
    paper_trading: bool
    paper_fill_model: str
    execution_mode: str
    peak_drawdown_kill_usd: float
    exit_policy_mode: str
    stake_mode: str
    stake_usd: float
    min_order_size_fallback: float
    starting_balance: float
    poll_interval_seconds: float
    status_interval_seconds: int
    balance_refresh_seconds: int
    min_entry_price: float
    max_entry_price: float
    weekend_min_entry_price: float
    weekend_max_entry_price: float
    entry_start_block_seconds: int
    entry_min_tte_seconds: int
    entry_max_tte_seconds: int
    cooldown_seconds: int
    maker_min_spread_ticks: int
    entry_quote_max_age_seconds: float
    live_entry_retry_cooldown_seconds: float
    live_max_entry_attempts_per_window: int
    live_wallet_reconcile_tolerance_usd: float
    exit_retry_cadence_seconds: float
    dust_share_threshold: float
    observe_first_window: bool
    pressure_defence_enabled: bool
    pressure_min_held_mid: float
    pressure_max_held_mid: float
    pressure_arm_tte_seconds: int
    pressure_trigger_tte_seconds: int
    pressure_max_tte_seconds: int
    pressure_z_threshold: float
    pressure_lookback_seconds: int
    pressure_signed_volume_lookback_seconds: int
    pressure_adverse_volume_btc: float
    pressure_hedge_fraction: float
    pressure_max_hedge_ask: float
    pressure_hedge_limit_price: float
    session_style_drawdown_guard_enabled: bool
    max_loop_seconds: float | None
    once: bool


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stats Bot: BTC 5-minute Polymarket policy-cube bot.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--paper", action="store_true", help="Run in paper mode.")
    mode.add_argument("--live", action="store_true", help="Run in live mode.")
    parser.add_argument("--stake-usd", type=float, default=None, help="Fixed dollar stake per trade when stake mode is fixed_usd. Default: $5.")
    parser.add_argument(
        "--stake-mode",
        choices=["fixed_usd", "min_order_size", "dynamic_shares", "ladder_shares"],
        default=None,
        help="Entry sizing mode. Defaults to fixed_usd in paper and flat market min-order shares in live.",
    )
    parser.add_argument("--starting-balance", type=float, default=None, help="Paper starting balance.")
    parser.add_argument("--policy-file", default=None, help="Override policy cube JSONL path.")
    parser.add_argument("--macro-policy-bundle", default=None, help="Override macro/local production policy bundle JSON path.")
    parser.add_argument("--exit-thresholds-file", default=None, help="Override exit threshold CSV path.")
    parser.add_argument("--entry-edge-thresholds-file", default=None, help="Override entry edge threshold CSV path.")
    parser.add_argument("--volatility-edges-file", default=None, help="Override volatility edge JSON path.")
    parser.add_argument("--log-dir", default=None, help="Override log directory.")
    parser.add_argument("--poll-interval-seconds", type=float, default=None, help="Main loop sleep interval.")
    parser.add_argument("--status-interval-seconds", type=int, default=None, help="CLI dashboard refresh interval.")
    parser.add_argument("--min-entry-price", type=float, default=None, help="Minimum selected-side entry price.")
    parser.add_argument("--max-entry-price", type=float, default=None, help="Maximum selected-side entry price.")
    parser.add_argument("--weekend-min-entry-price", type=float, default=None, help="Weekend-only minimum selected-side entry price.")
    parser.add_argument("--weekend-max-entry-price", type=float, default=None, help="Weekend-only maximum selected-side entry price.")
    parser.add_argument(
        "--paper-fill-model",
        choices=["fast_limit", "maker_touch", "immediate"],
        default=None,
        help=(
            "Paper entry fill model. fast_limit mirrors live presigned fast-limit entry; "
            "maker_touch waits for ask <= maker bid; immediate fills maker placement."
        ),
    )
    parser.add_argument(
        "--execution-mode",
        choices=["event_driven_ws", "legacy_polling"],
        default=None,
        help="Execution runtime. event_driven_ws uses streamed books and non-blocking entry submission.",
    )
    parser.add_argument(
        "--peak-drawdown-kill-usd",
        type=float,
        default=None,
        help="Latch off new entries after this dollar drawdown from peak session realised equity.",
    )
    parser.add_argument(
        "--exit-policy-mode",
        choices=["hold_to_resolution", "bps_loss_only"],
        default=None,
        help="Tactical exit mode. Default holds to resolution; bps_loss_only enables the BPS loss exit table.",
    )
    parser.add_argument("--no-observe-first-window", action="store_true", help="Disable startup observe-only guard.")
    parser.add_argument("--max-loop-seconds", type=float, default=None, help="Stop after N seconds; useful for smoke tests.")
    parser.add_argument("--once", action="store_true", help="Run one loop iteration and stop.")
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> BotConfig:
    load_env_file(REPO_ROOT / ".env")

    env_paper = _bool_env("STATS_PAPER_TRADING", True)
    paper_trading = args.paper or (env_paper and not args.live)
    if args.live:
        paper_trading = False

    policy_file = Path(args.policy_file or os.getenv("STATS_POLICY_FILE", str(DEFAULT_POLICY_FILE))).resolve()
    macro_policy_bundle = Path(
        args.macro_policy_bundle
        or os.getenv("STATS_MACRO_POLICY_BUNDLE", str(DEFAULT_MACRO_POLICY_BUNDLE))
    ).resolve()
    exit_thresholds_file = Path(
        args.exit_thresholds_file or os.getenv("STATS_EXIT_THRESHOLDS_FILE", str(DEFAULT_EXIT_THRESHOLDS_FILE))
    ).resolve()
    entry_edge_thresholds_file = Path(
        args.entry_edge_thresholds_file
        or os.getenv("STATS_ENTRY_EDGE_THRESHOLDS_FILE", str(DEFAULT_ENTRY_EDGE_THRESHOLDS_FILE))
    ).resolve()
    volatility_edges_file = Path(
        args.volatility_edges_file or os.getenv("STATS_VOLATILITY_EDGES_FILE", str(DEFAULT_VOLATILITY_EDGES_FILE))
    ).resolve()
    log_dir = Path(args.log_dir or os.getenv("STATS_LOG_DIR", str(DEFAULT_LOG_DIR))).resolve()
    min_entry_price = float(
        args.min_entry_price if args.min_entry_price is not None else _float_env("STATS_MIN_ENTRY_PRICE", 0.15)
    )
    max_entry_price = float(
        args.max_entry_price if args.max_entry_price is not None else _float_env("STATS_MAX_ENTRY_PRICE", 0.60)
    )
    weekend_min_entry_price = float(
        args.weekend_min_entry_price
        if args.weekend_min_entry_price is not None
        else _float_env("STATS_WEEKEND_MIN_ENTRY_PRICE", min_entry_price)
    )
    weekend_max_entry_price = float(
        args.weekend_max_entry_price
        if args.weekend_max_entry_price is not None
        else _float_env("STATS_WEEKEND_MAX_ENTRY_PRICE", 0.65)
    )
    if not (0.0 < min_entry_price < max_entry_price < 1.0):
        raise SystemExit("Weekday entry price bounds must satisfy 0 < min < max < 1.")
    if not (0.0 < weekend_min_entry_price < weekend_max_entry_price < 1.0):
        raise SystemExit("Weekend entry price bounds must satisfy 0 < weekend_min < weekend_max < 1.")

    paper_fill_model = args.paper_fill_model or os.getenv("STATS_PAPER_FILL_MODEL", "fast_limit").strip().lower()
    if paper_fill_model not in {"fast_limit", "maker_touch", "immediate"}:
        raise SystemExit("STATS_PAPER_FILL_MODEL must be fast_limit, maker_touch, or immediate.")
    execution_mode = (
        args.execution_mode or os.getenv("STATS_EXECUTION_MODE", "event_driven_ws")
    ).strip().lower()
    if execution_mode not in {"event_driven_ws", "legacy_polling"}:
        raise SystemExit("STATS_EXECUTION_MODE must be event_driven_ws or legacy_polling.")
    exit_policy_mode = (args.exit_policy_mode or os.getenv("STATS_EXIT_POLICY_MODE", "hold_to_resolution")).strip().lower()
    if exit_policy_mode not in {"hold_to_resolution", "bps_loss_only"}:
        raise SystemExit("STATS_EXIT_POLICY_MODE must be hold_to_resolution or bps_loss_only.")
    stake_mode_default = "fixed_usd" if paper_trading else "min_order_size"
    stake_mode = (args.stake_mode or os.getenv("STATS_STAKE_MODE", stake_mode_default)).strip().lower()
    if stake_mode not in {"fixed_usd", "min_order_size", "dynamic_shares", "ladder_shares"}:
        raise SystemExit("STATS_STAKE_MODE must be fixed_usd, min_order_size, dynamic_shares, or ladder_shares.")

    observe_default = False if paper_trading else True
    observe_first_window = _bool_env("STATS_OBSERVE_FIRST_WINDOW", observe_default)
    if args.no_observe_first_window:
        observe_first_window = False

    max_loop_seconds = args.max_loop_seconds
    if max_loop_seconds is None:
        raw = os.getenv("STATS_MAX_LOOP_SECONDS")
        max_loop_seconds = float(raw) if raw not in (None, "") else None

    return BotConfig(
        macro_policy_bundle=macro_policy_bundle,
        policy_file=policy_file,
        exit_thresholds_file=exit_thresholds_file,
        entry_edge_thresholds_file=entry_edge_thresholds_file,
        volatility_edges_file=volatility_edges_file,
        log_dir=log_dir,
        paper_trading=paper_trading,
        paper_fill_model=paper_fill_model,
        execution_mode=execution_mode,
        peak_drawdown_kill_usd=max(
            0.0,
            float(
                args.peak_drawdown_kill_usd
                if args.peak_drawdown_kill_usd is not None
                else _float_env("STATS_PEAK_DRAWDOWN_KILL_USD", 15.0)
            ),
        ),
        exit_policy_mode=exit_policy_mode,
        stake_mode=stake_mode,
        stake_usd=float(args.stake_usd if args.stake_usd is not None else _float_env("STATS_STAKE_USD", 5.0)),
        min_order_size_fallback=max(0.0, _float_env("STATS_MIN_ORDER_SIZE_FALLBACK", 5.0)),
        starting_balance=float(
            args.starting_balance if args.starting_balance is not None else _float_env("STATS_STARTING_BALANCE", 1000.0)
        ),
        poll_interval_seconds=float(
            args.poll_interval_seconds
            if args.poll_interval_seconds is not None
            else _float_env("STATS_POLL_INTERVAL_SECONDS", 0.5)
        ),
        status_interval_seconds=max(
            1,
            int(
                args.status_interval_seconds
                if args.status_interval_seconds is not None
                else _int_env("STATS_STATUS_INTERVAL_SECONDS", 2)
            ),
        ),
        balance_refresh_seconds=max(5, _int_env("STATS_BALANCE_REFRESH_SECONDS", 15)),
        min_entry_price=min_entry_price,
        max_entry_price=max_entry_price,
        weekend_min_entry_price=weekend_min_entry_price,
        weekend_max_entry_price=weekend_max_entry_price,
        entry_start_block_seconds=max(0, _int_env("STATS_ENTRY_START_BLOCK_SECONDS", 45)),
        entry_min_tte_seconds=max(0, _int_env("STATS_ENTRY_MIN_TTE_SECONDS", 60)),
        entry_max_tte_seconds=max(0, _int_env("STATS_ENTRY_MAX_TTE_SECONDS", 255)),
        cooldown_seconds=max(0, _int_env("STATS_COOLDOWN_SECONDS", 10)),
        maker_min_spread_ticks=max(1, _int_env("STATS_MAKER_MIN_SPREAD_TICKS", 1)),
        entry_quote_max_age_seconds=max(0.1, _float_env("STATS_ENTRY_QUOTE_MAX_AGE_SECONDS", 1.5)),
        live_entry_retry_cooldown_seconds=max(0.0, _float_env("STATS_LIVE_ENTRY_RETRY_COOLDOWN_SECONDS", 8.0)),
        live_max_entry_attempts_per_window=max(1, _int_env("STATS_LIVE_MAX_ENTRY_ATTEMPTS_PER_WINDOW", 3)),
        live_wallet_reconcile_tolerance_usd=max(0.0, _float_env("STATS_LIVE_WALLET_RECONCILE_TOLERANCE_USD", 0.02)),
        exit_retry_cadence_seconds=max(0.1, _float_env("STATS_EXIT_RETRY_CADENCE_SECONDS", 1.0)),
        dust_share_threshold=max(0.0, _float_env("STATS_DUST_SHARE_THRESHOLD", 0.01)),
        observe_first_window=observe_first_window,
        pressure_defence_enabled=_bool_env(
            "STATS_PRESSURE_DEFENCE_ENABLED",
            _bool_env("STATS_FFM_DEFENCE_ENABLED", False),
        ),
        pressure_min_held_mid=max(0.0, _float_env("STATS_PRESSURE_MIN_HELD_MID", _float_env("STATS_FFM_MIN_HELD_MID", 0.75))),
        pressure_max_held_mid=min(1.0, _float_env("STATS_PRESSURE_MAX_HELD_MID", _float_env("STATS_FFM_MAX_HELD_MID", 1.0))),
        pressure_arm_tte_seconds=max(0, _int_env("STATS_PRESSURE_ARM_TTE_SECONDS", _int_env("STATS_FFM_ARM_TTE_SECONDS", 60))),
        pressure_trigger_tte_seconds=max(
            0,
            _int_env("STATS_PRESSURE_TRIGGER_TTE_SECONDS", _int_env("STATS_FFM_TRIGGER_TTE_SECONDS", 40)),
        ),
        pressure_max_tte_seconds=max(0, _int_env("STATS_PRESSURE_MAX_TTE_SECONDS", _int_env("STATS_FFM_ARM_TTE_SECONDS", 60))),
        pressure_z_threshold=max(0.0, _float_env("STATS_PRESSURE_Z_THRESHOLD", 2.0)),
        pressure_lookback_seconds=max(1, _int_env("STATS_PRESSURE_LOOKBACK_SECONDS", 10)),
        pressure_signed_volume_lookback_seconds=max(
            1,
            _int_env(
                "STATS_PRESSURE_SIGNED_VOLUME_LOOKBACK_SECONDS",
                _int_env("STATS_FFM_SIGNED_VOLUME_LOOKBACK_SECONDS", 30),
            ),
        ),
        pressure_adverse_volume_btc=max(
            0.0,
            _float_env("STATS_PRESSURE_ADVERSE_VOLUME_BTC", _float_env("STATS_FFM_ADVERSE_VOLUME_BTC", 8.0)),
        ),
        pressure_hedge_fraction=max(0.0, min(1.0, _float_env("STATS_PRESSURE_HEDGE_FRACTION", 1.0))),
        pressure_max_hedge_ask=max(0.0, min(1.0, _float_env("STATS_PRESSURE_MAX_HEDGE_ASK", 0.99))),
        pressure_hedge_limit_price=max(0.01, min(0.99, _float_env("STATS_PRESSURE_HEDGE_LIMIT_PRICE", 0.99))),
        session_style_drawdown_guard_enabled=_bool_env("STATS_SESSION_STYLE_DRAWDOWN_GUARD_ENABLED", False),
        max_loop_seconds=max_loop_seconds,
        once=bool(args.once),
    )

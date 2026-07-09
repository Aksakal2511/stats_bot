#!/usr/bin/env python3
from __future__ import annotations

import argparse
import bisect
import csv
import heapq
import math
import re
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


UTC = timezone.utc
NORMALISED_SHARES = 5.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Analyse global bad-regime signals using live trades plus cadence BTC data. "
            "This deliberately avoids policy-cube fields for guard decisions."
        )
    )
    parser.add_argument("--logs-dir", type=Path, default=Path("logs"), help="Directory containing stats_bot session logs.")
    parser.add_argument("--since", default="20260615", help="Earliest session date to include, YYYYMMDD.")
    parser.add_argument("--mode", choices=["live", "paper", "all"], default="live", help="Session mode to include.")
    parser.add_argument("--out-dir", type=Path, default=Path("analysis/regime_guard_audit"), help="Output directory.")
    parser.add_argument("--initial-wallet", type=float, default=60.0, help="Wallet used for drawdown-regime simulations.")
    parser.add_argument(
        "--momentum-windows",
        default="15,30,60,120,300",
        help=(
            "Comma-separated BTC lookback windows in seconds. "
            "Use --wide-grid for 5,10,15,...,180."
        ),
    )
    parser.add_argument(
        "--pause-minutes",
        default="10,15,30,45,60,90",
        help="Comma-separated global pause lengths to test.",
    )
    parser.add_argument(
        "--loss-streak-triggers",
        default="2,3,4",
        help="Comma-separated closed-loss streak triggers to test.",
    )
    parser.add_argument(
        "--rolling-trade-windows",
        default="2,3,5,8",
        help="Comma-separated closed-trade counts for rolling damage guards.",
    )
    parser.add_argument(
        "--rolling-damage-floors",
        default="2.5,4.0,5.0,7.5,10.0",
        help="Comma-separated normalised 5-share loss floors for rolling damage guards.",
    )
    parser.add_argument(
        "--btc-thresholds",
        default="2.5,5.0,7.5,10.0,15.0,20.0,30.0,40.0",
        help=(
            "Comma-separated BTC bps thresholds to test. "
            "Use --wide-grid for 0.5,1.0,1.5,...,25.0."
        ),
    )
    parser.add_argument(
        "--wide-grid",
        action="store_true",
        help=(
            "Run the larger grid requested for regime research: loss streaks 1-6, "
            "pauses 10,15,20,...,120, BTC windows 5,10,15,...,180, "
            "BTC thresholds 0.5,1.0,1.5,...,25.0, and rolling windows 1-6."
        ),
    )
    return parser.parse_args()


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).astimezone(UTC)
    except ValueError:
        return None


def finite_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        result = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(result):
        return default
    return result


def session_date(path: Path) -> str:
    return path.name.split("_", 1)[0]


def session_mode(path: Path) -> str:
    name = path.name.lower()
    if name.endswith("_live"):
        return "live"
    if name.endswith("_paper"):
        return "paper"
    return "unknown"


def discover_sessions(logs_dir: Path, since: str, mode: str) -> list[Path]:
    sessions: list[Path] = []
    for path in sorted(logs_dir.iterdir()):
        if not path.is_dir():
            continue
        if session_date(path) < since:
            continue
        if mode != "all" and session_mode(path) != mode:
            continue
        if (path / "stats_trades.csv").exists() and (path / "stats_cadence_1s.csv").exists():
            sessions.append(path)
    return sessions


def parse_policy_key(policy_key: str | None) -> dict[str, str]:
    parts = str(policy_key or "").split("|")
    names = ["day_type", "session_bucket", "tte_bucket", "signed_bps_bucket", "volatility_bucket", "up_ask_bucket", "down_ask_bucket"]
    return {name: parts[index] if index < len(parts) else "" for index, name in enumerate(names)}


def price_band(price: float | None) -> str:
    if price is None:
        return "unknown"
    if price < 0.30:
        return "0.15-0.29"
    if price < 0.45:
        return "0.30-0.44"
    if price <= 0.60:
        return "0.45-0.60"
    if price <= 0.70:
        return "0.61-0.70"
    if price <= 0.85:
        return "0.71-0.85"
    return ">0.85"


def signed_bucket(value: float | None, edges: tuple[float, ...]) -> str:
    if value is None:
        return "unknown"
    previous = -math.inf
    for edge in edges:
        if value < edge:
            return f"{previous:g}_to_{edge:g}"
        previous = edge
    return f">={edges[-1]:g}"


def abs_bucket(value: float | None, edges: tuple[float, ...]) -> str:
    if value is None:
        return "unknown"
    abs_value = abs(value)
    previous = 0.0
    for edge in edges:
        if abs_value < edge:
            return f"{previous:g}_to_{edge:g}"
        previous = edge
    return f">={edges[-1]:g}"


def streak_bucket(value: int) -> str:
    if value <= 0:
        return "0"
    if value == 1:
        return "1"
    if value == 2:
        return "2"
    return "3+"


def pnl_bucket(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value <= -10:
        return "<=-10"
    if value <= -5:
        return "-10_to_-5"
    if value < 0:
        return "-5_to_0"
    if value < 5:
        return "0_to_5"
    if value < 10:
        return "5_to_10"
    return ">=10"


def dd_bucket(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < 5:
        return "<5"
    if value < 8:
        return "5_to_8"
    if value < 12:
        return "8_to_12"
    if value < 16:
        return "12_to_16"
    return ">=16"


@dataclass
class PriceTape:
    timestamps: list[float]
    prices: list[float]

    def nearest_price(self, timestamp: datetime, max_gap_seconds: float = 3.0) -> float | None:
        if not self.timestamps:
            return None
        target = timestamp.timestamp()
        index = bisect.bisect_left(self.timestamps, target)
        candidates: list[int] = []
        if index < len(self.timestamps):
            candidates.append(index)
        if index:
            candidates.append(index - 1)
        if not candidates:
            return None
        best = min(candidates, key=lambda item: abs(self.timestamps[item] - target))
        if abs(self.timestamps[best] - target) > max_gap_seconds:
            return None
        return self.prices[best]

    def window_prices(self, end: datetime, seconds: int) -> list[float]:
        if not self.timestamps:
            return []
        end_ts = end.timestamp()
        start_ts = end_ts - seconds
        start_idx = bisect.bisect_left(self.timestamps, start_ts)
        end_idx = bisect.bisect_right(self.timestamps, end_ts)
        return self.prices[start_idx:end_idx]

    def window_bps(self, end: datetime, seconds: int) -> float | None:
        end_price = self.nearest_price(end)
        start_price = self.nearest_price(end - timedelta(seconds=seconds), max_gap_seconds=5.0)
        if start_price is None or end_price is None or start_price <= 0:
            return None
        return (end_price / start_price - 1.0) * 10000.0

    def window_range_bps(self, end: datetime, seconds: int) -> float | None:
        prices = self.window_prices(end, seconds)
        if len(prices) < 2:
            return None
        low = min(prices)
        high = max(prices)
        last = prices[-1]
        if last <= 0:
            return None
        return (high - low) / last * 10000.0

    def window_realized_vol_bps(self, end: datetime, seconds: int) -> float | None:
        prices = self.window_prices(end, seconds)
        if len(prices) < 3:
            return None
        returns: list[float] = []
        previous = prices[0]
        for price in prices[1:]:
            if previous > 0 and price > 0:
                returns.append((price / previous - 1.0) * 10000.0)
            previous = price
        if len(returns) < 2:
            return None
        mean = sum(returns) / len(returns)
        variance = sum((item - mean) ** 2 for item in returns) / (len(returns) - 1)
        return math.sqrt(variance)


def load_price_tape(session: Path) -> PriceTape:
    rows: list[tuple[float, float]] = []
    with (session / "stats_cadence_1s.csv").open(newline="") as handle:
        for row in csv.DictReader(handle):
            timestamp = parse_dt(row.get("timestamp_utc"))
            price = finite_float(row.get("cb_close_1s"))
            if timestamp is not None and price is not None:
                rows.append((timestamp.timestamp(), price))
    rows.sort()
    return PriceTape([item[0] for item in rows], [item[1] for item in rows])


def load_trades(session: Path, tape: PriceTape, windows: list[int]) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    with (session / "stats_trades.csv").open(newline="") as handle:
        for row in csv.DictReader(handle):
            entry_dt = parse_dt(row.get("entry_time_utc"))
            exit_dt = parse_dt(row.get("exit_time_utc"))
            if entry_dt is None or exit_dt is None:
                continue
            shares = finite_float(row.get("shares_exited"), 0.0) or 0.0
            pnl = finite_float(row.get("pnl"), 0.0) or 0.0
            entry_price = finite_float(row.get("entry_price"), 0.0) or 0.0
            policy = parse_policy_key(row.get("entry_policy_key"))
            normalised_pnl = pnl * (NORMALISED_SHARES / shares) if shares > 0 else 0.0
            side = str(row.get("side") or "").upper()
            trade: dict[str, Any] = {
                "session": session.name,
                "market_slug": row.get("market_slug", ""),
                "entry_time_utc": entry_dt.isoformat(),
                "exit_time_utc": exit_dt.isoformat(),
                "entry_ts": entry_dt.timestamp(),
                "exit_ts": exit_dt.timestamp(),
                "side": side,
                "shares": shares,
                "entry_price": entry_price,
                "price_band": price_band(entry_price),
                "pnl": pnl,
                "normalised_5_share_pnl": normalised_pnl,
                "win": int(pnl > 1e-12),
                "entry_bps": finite_float(row.get("entry_bps")),
                "day_type": row.get("entry_day_type", ""),
                "session_bucket": row.get("entry_session_bucket", ""),
                "trade_style": row.get("entry_trade_style", ""),
                "volatility_bucket": policy["volatility_bucket"],
            }
            entry_btc = tape.nearest_price(entry_dt)
            trade["btc_close_entry"] = entry_btc if entry_btc is not None else ""
            for seconds in windows:
                bps = tape.window_bps(entry_dt, seconds)
                range_bps = tape.window_range_bps(entry_dt, seconds)
                vol_bps = tape.window_realized_vol_bps(entry_dt, seconds)
                trade[f"btc_mom_{seconds}s_bps"] = bps if bps is not None else ""
                trade[f"btc_abs_mom_{seconds}s_bps"] = abs(bps) if bps is not None else ""
                trade[f"btc_range_{seconds}s_bps"] = range_bps if range_bps is not None else ""
                trade[f"btc_realized_vol_{seconds}s_bps"] = vol_bps if vol_bps is not None else ""
                if bps is None or side not in {"UP", "DOWN"}:
                    alignment = "unknown"
                elif abs(bps) < 0.25:
                    alignment = "flat"
                elif (bps > 0 and side == "UP") or (bps < 0 and side == "DOWN"):
                    alignment = "aligned"
                else:
                    alignment = "against"
                trade[f"side_vs_mom_{seconds}s"] = alignment
            trades.append(trade)
    return trades


def add_prior_trade_state(trades: list[dict[str, Any]], initial_wallet: float) -> None:
    ordered = sorted(range(len(trades)), key=lambda index: trades[index]["entry_ts"])
    exit_heap: list[tuple[float, int]] = []
    closed_pnls: list[float] = []
    closed_wins: list[int] = []
    closed_count = 0
    loss_streak = 0
    win_streak = 0
    cumulative_pnl = 0.0
    high_equity = initial_wallet

    for index in ordered:
        trade = trades[index]
        entry_ts = float(trade["entry_ts"])
        while exit_heap and exit_heap[0][0] <= entry_ts:
            _, closed_index = heapq.heappop(exit_heap)
            closed_trade = trades[closed_index]
            pnl = float(closed_trade["normalised_5_share_pnl"])
            closed_pnls.append(pnl)
            closed_wins.append(int(pnl > 0))
            closed_count += 1
            cumulative_pnl += pnl
            equity = initial_wallet + cumulative_pnl
            high_equity = max(high_equity, equity)
            if pnl > 0:
                win_streak += 1
                loss_streak = 0
            else:
                loss_streak += 1
                win_streak = 0

        equity = initial_wallet + cumulative_pnl
        high_equity = max(high_equity, equity)
        dd_pct = (high_equity - equity) / high_equity * 100.0 if high_equity > 0 else 0.0
        trade["closed_trades_before_entry"] = closed_count
        trade["loss_streak_before_entry"] = loss_streak
        trade["win_streak_before_entry"] = win_streak
        trade["rolling_pnl_2_before"] = sum(closed_pnls[-2:])
        trade["rolling_pnl_3_before"] = sum(closed_pnls[-3:])
        trade["rolling_pnl_5_before"] = sum(closed_pnls[-5:])
        trade["rolling_pnl_10_before"] = sum(closed_pnls[-10:])
        trade["rolling_wr_5_before"] = sum(closed_wins[-5:]) / min(5, len(closed_wins)) * 100.0 if closed_wins else ""
        trade["rolling_wr_10_before"] = sum(closed_wins[-10:]) / min(10, len(closed_wins)) * 100.0 if closed_wins else ""
        trade["equity_before_entry"] = equity
        trade["high_equity_before_entry"] = high_equity
        trade["drawdown_pct_before_entry"] = dd_pct
        trade["drawdown_bucket_before_entry"] = dd_bucket(dd_pct)
        heapq.heappush(exit_heap, (float(trade["exit_ts"]), index))


@dataclass
class Agg:
    n: int = 0
    wins: int = 0
    pnl: float = 0.0
    price_sum: float = 0.0

    def add(self, row: dict[str, Any]) -> None:
        pnl = float(row["normalised_5_share_pnl"])
        self.n += 1
        self.wins += int(pnl > 0)
        self.pnl += pnl
        self.price_sum += float(row["entry_price"])

    def as_row(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "wins": self.wins,
            "losses": self.n - self.wins,
            "wr_pct": round(self.wins / self.n * 100.0, 3) if self.n else 0.0,
            "normalised_5_share_pnl": round(self.pnl, 6),
            "avg_entry_price": round(self.price_sum / self.n, 6) if self.n else "",
        }


def grouped(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, ...], Agg] = defaultdict(Agg)
    for row in rows:
        key = tuple(str(row.get(field, "")) for field in fields)
        groups[key].add(row)
    output: list[dict[str, Any]] = []
    for key, agg in groups.items():
        item = {field: value for field, value in zip(fields, key)}
        item.update(agg.as_row())
        output.append(item)
    output.sort(key=lambda item: float(item["normalised_5_share_pnl"]))
    return output


def feature_rows(trades: list[dict[str, Any]], windows: list[int]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    feature_specs: list[tuple[str, str]] = [
        ("loss_streak_before_entry", "loss_streak_before_entry"),
        ("drawdown_bucket_before_entry", "drawdown_bucket_before_entry"),
        ("rolling_pnl_3_bucket", "rolling_pnl_3_bucket"),
        ("rolling_pnl_5_bucket", "rolling_pnl_5_bucket"),
        ("rolling_pnl_10_bucket", "rolling_pnl_10_bucket"),
    ]
    for row in trades:
        row["loss_streak_before_entry_bucket"] = streak_bucket(int(row.get("loss_streak_before_entry") or 0))
        row["rolling_pnl_3_bucket"] = pnl_bucket(float(row.get("rolling_pnl_3_before") or 0.0))
        row["rolling_pnl_5_bucket"] = pnl_bucket(float(row.get("rolling_pnl_5_before") or 0.0))
        row["rolling_pnl_10_bucket"] = pnl_bucket(float(row.get("rolling_pnl_10_before") or 0.0))
        for seconds in windows:
            row[f"btc_abs_mom_{seconds}s_bucket"] = abs_bucket(finite_float(row.get(f"btc_abs_mom_{seconds}s_bps")), (2.5, 5, 10, 20, 40))
            row[f"btc_range_{seconds}s_bucket"] = abs_bucket(finite_float(row.get(f"btc_range_{seconds}s_bps")), (5, 10, 20, 40, 80))
            row[f"btc_mom_{seconds}s_bucket"] = signed_bucket(finite_float(row.get(f"btc_mom_{seconds}s_bps")), (-20, -10, -5, -2.5, 2.5, 5, 10, 20))
            feature_specs.append((f"btc_abs_mom_{seconds}s_bucket", f"btc_abs_mom_{seconds}s_bucket"))
            feature_specs.append((f"btc_range_{seconds}s_bucket", f"btc_range_{seconds}s_bucket"))
            feature_specs.append((f"btc_mom_{seconds}s_bucket", f"btc_mom_{seconds}s_bucket"))
            feature_specs.append((f"side_vs_mom_{seconds}s", f"side_vs_mom_{seconds}s"))

    seen: set[str] = set()
    deduped_specs: list[tuple[str, str]] = []
    for label, field in feature_specs:
        if field not in seen:
            seen.add(field)
            deduped_specs.append((label, field))

    for label, field in deduped_specs:
        for item in grouped(trades, (field,)):
            output.append({"feature": label, "bucket": item[field], **{k: v for k, v in item.items() if k != field}})
    output.sort(key=lambda item: (str(item["feature"]), float(item["normalised_5_share_pnl"])))
    return output


def simulate_pause_guard(
    trades: list[dict[str, Any]],
    *,
    initial_wallet: float,
    rule: str,
    loss_streak_trigger: int | None = None,
    rolling_n: int | None = None,
    rolling_floor: float | None = None,
    dd_trigger_pct: float | None = None,
    pause_minutes: int = 60,
) -> dict[str, Any]:
    ordered = sorted(range(len(trades)), key=lambda index: trades[index]["entry_ts"])
    exit_heap: list[tuple[float, int]] = []
    taken_pnls: list[float] = []
    active_indices: set[int] = set()
    pause_until = 0.0
    loss_streak = 0
    cumulative_pnl = 0.0
    high_equity = initial_wallet
    skipped_pnl = 0.0
    skipped_wins = 0
    skipped_losses = 0
    skipped = 0
    taken = 0
    triggers = 0

    def maybe_trigger(exit_ts: float) -> None:
        nonlocal pause_until, triggers
        equity = initial_wallet + cumulative_pnl
        high = max(high_equity, equity)
        dd = (high - equity) / high * 100.0 if high > 0 else 0.0
        trigger = False
        if rule == "loss_streak":
            trigger = loss_streak_trigger is not None and loss_streak >= loss_streak_trigger
        elif rule == "rolling_damage":
            trigger = rolling_n is not None and rolling_floor is not None and len(taken_pnls) >= rolling_n and sum(taken_pnls[-rolling_n:]) <= -rolling_floor
        elif rule == "loss_or_damage":
            trigger = (
                loss_streak_trigger is not None
                and loss_streak >= loss_streak_trigger
            ) or (
                rolling_n is not None
                and rolling_floor is not None
                and len(taken_pnls) >= rolling_n
                and sum(taken_pnls[-rolling_n:]) <= -rolling_floor
            )
        elif rule == "dd_then_loss":
            trigger = dd_trigger_pct is not None and dd >= dd_trigger_pct and taken_pnls and taken_pnls[-1] <= 0
        if trigger:
            new_pause_until = exit_ts + pause_minutes * 60
            if new_pause_until > pause_until:
                triggers += 1
                pause_until = new_pause_until

    for index in ordered:
        trade = trades[index]
        entry_ts = float(trade["entry_ts"])
        while exit_heap and exit_heap[0][0] <= entry_ts:
            exit_ts, closed_index = heapq.heappop(exit_heap)
            if closed_index not in active_indices:
                continue
            active_indices.remove(closed_index)
            pnl = float(trades[closed_index]["normalised_5_share_pnl"])
            taken_pnls.append(pnl)
            cumulative_pnl += pnl
            equity = initial_wallet + cumulative_pnl
            high_equity = max(high_equity, equity)
            if pnl > 0:
                loss_streak = 0
            else:
                loss_streak += 1
            maybe_trigger(exit_ts)

        pnl = float(trade["normalised_5_share_pnl"])
        if entry_ts < pause_until:
            skipped += 1
            skipped_pnl += pnl
            skipped_wins += int(pnl > 0)
            skipped_losses += int(pnl <= 0)
            continue
        taken += 1
        active_indices.add(index)
        heapq.heappush(exit_heap, (float(trade["exit_ts"]), index))

    while exit_heap:
        exit_ts, closed_index = heapq.heappop(exit_heap)
        if closed_index not in active_indices:
            continue
        active_indices.remove(closed_index)
        pnl = float(trades[closed_index]["normalised_5_share_pnl"])
        taken_pnls.append(pnl)
        cumulative_pnl += pnl
        equity = initial_wallet + cumulative_pnl
        high_equity = max(high_equity, equity)
        if pnl > 0:
            loss_streak = 0
        else:
            loss_streak += 1
        maybe_trigger(exit_ts)

    baseline = sum(float(trade["normalised_5_share_pnl"]) for trade in trades)
    guarded = baseline - skipped_pnl
    return {
        "rule": rule,
        "loss_streak_trigger": loss_streak_trigger if loss_streak_trigger is not None else "",
        "rolling_n": rolling_n if rolling_n is not None else "",
        "rolling_floor": rolling_floor if rolling_floor is not None else "",
        "dd_trigger_pct": dd_trigger_pct if dd_trigger_pct is not None else "",
        "pause_minutes": pause_minutes,
        "baseline_norm_pnl": round(baseline, 6),
        "guarded_norm_pnl": round(guarded, 6),
        "delta_vs_baseline": round(guarded - baseline, 6),
        "skipped_trades": skipped,
        "skipped_wins": skipped_wins,
        "skipped_losses": skipped_losses,
        "skipped_norm_pnl": round(skipped_pnl, 6),
        "taken_trades": taken,
        "triggers": triggers,
        "skip_efficiency": round((guarded - baseline) / skipped, 6) if skipped else "",
    }


def simulate_btc_filter(
    trades: list[dict[str, Any]],
    *,
    feature: str,
    threshold: float,
    direction: str,
) -> dict[str, Any]:
    baseline = sum(float(trade["normalised_5_share_pnl"]) for trade in trades)
    skipped_pnl = 0.0
    skipped = 0
    skipped_wins = 0
    skipped_losses = 0
    for trade in trades:
        value = finite_float(trade.get(feature))
        if value is None:
            continue
        if direction == "abs_ge":
            should_skip = abs(value) >= threshold
        elif direction == "ge":
            should_skip = value >= threshold
        elif direction == "le":
            should_skip = value <= -threshold
        else:
            raise ValueError(direction)
        if should_skip:
            pnl = float(trade["normalised_5_share_pnl"])
            skipped += 1
            skipped_pnl += pnl
            skipped_wins += int(pnl > 0)
            skipped_losses += int(pnl <= 0)
    guarded = baseline - skipped_pnl
    return {
        "feature": feature,
        "direction": direction,
        "threshold_bps": threshold,
        "baseline_norm_pnl": round(baseline, 6),
        "guarded_norm_pnl": round(guarded, 6),
        "delta_vs_baseline": round(guarded - baseline, 6),
        "skipped_trades": skipped,
        "skipped_wins": skipped_wins,
        "skipped_losses": skipped_losses,
        "skipped_norm_pnl": round(skipped_pnl, 6),
        "skip_efficiency": round((guarded - baseline) / skipped, 6) if skipped else "",
    }


def parse_int_list(value: str) -> list[int]:
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def parse_float_list(value: str) -> list[float]:
    return [float(item.strip()) for item in value.split(",") if item.strip()]


def range_int(start: int, stop: int, step: int) -> list[int]:
    return list(range(start, stop + 1, step))


def range_float(start: float, stop: float, step: float) -> list[float]:
    values: list[float] = []
    current = start
    while current <= stop + 1e-9:
        values.append(round(current, 6))
        current += step
    return values


def build_guard_grid(
    trades: list[dict[str, Any]],
    initial_wallet: float,
    *,
    pause_minutes_values: list[int],
    loss_streak_values: list[int],
    rolling_window_values: list[int],
    rolling_floor_values: list[float],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for pause in pause_minutes_values:
        for streak in loss_streak_values:
            rows.append(
                simulate_pause_guard(
                    trades,
                    initial_wallet=initial_wallet,
                    rule="loss_streak",
                    loss_streak_trigger=streak,
                    pause_minutes=pause,
                )
            )
        for rolling_n in rolling_window_values:
            for floor in rolling_floor_values:
                rows.append(
                    simulate_pause_guard(
                        trades,
                        initial_wallet=initial_wallet,
                        rule="rolling_damage",
                        rolling_n=rolling_n,
                        rolling_floor=floor,
                        pause_minutes=pause,
                    )
                )
        combined_streak_values = [value for value in loss_streak_values if value <= 3]
        combined_window_values = [value for value in rolling_window_values if value in {2, 3, 5, 6}]
        combined_floor_values = [
            value for value in rolling_floor_values if value in {2.5, 4.0, 5.0, 7.5, 10.0}
        ]
        if not combined_floor_values:
            combined_floor_values = rolling_floor_values[: min(5, len(rolling_floor_values))]
        for streak in combined_streak_values:
            for rolling_n in combined_window_values:
                for floor in combined_floor_values:
                    rows.append(
                        simulate_pause_guard(
                            trades,
                            initial_wallet=initial_wallet,
                            rule="loss_or_damage",
                            loss_streak_trigger=streak,
                            rolling_n=rolling_n,
                            rolling_floor=floor,
                            pause_minutes=pause,
                    )
                )
        for dd in (5.0, 8.0, 10.0, 12.0, 14.0):
            rows.append(
                simulate_pause_guard(
                    trades,
                    initial_wallet=initial_wallet,
                    rule="dd_then_loss",
                    dd_trigger_pct=dd,
                    pause_minutes=pause,
                )
            )
    rows.sort(key=lambda item: (float(item["delta_vs_baseline"]), -int(item["skipped_trades"])), reverse=True)
    return rows


def build_btc_filter_grid(trades: list[dict[str, Any]], windows: list[int], thresholds: list[float]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for seconds in windows:
        for feature_prefix in ("btc_abs_mom", "btc_range", "btc_realized_vol"):
            feature = f"{feature_prefix}_{seconds}s_bps"
            for threshold in thresholds:
                rows.append(simulate_btc_filter(trades, feature=feature, threshold=threshold, direction="abs_ge"))
        feature = f"btc_mom_{seconds}s_bps"
        for threshold in thresholds:
            rows.append(simulate_btc_filter(trades, feature=feature, threshold=threshold, direction="ge"))
            rows.append(simulate_btc_filter(trades, feature=feature, threshold=threshold, direction="le"))
    rows.sort(key=lambda item: (float(item["delta_vs_baseline"]), -int(item["skipped_trades"])), reverse=True)
    return rows


def simulate_side_alignment_filter(trades: list[dict[str, Any]], *, window: int, skip_mode: str) -> dict[str, Any]:
    baseline = sum(float(trade["normalised_5_share_pnl"]) for trade in trades)
    skipped_pnl = 0.0
    skipped = 0
    skipped_wins = 0
    skipped_losses = 0
    field = f"side_vs_mom_{window}s"
    for trade in trades:
        alignment = str(trade.get(field) or "")
        if skip_mode == "against":
            should_skip = alignment == "against"
        elif skip_mode == "against_or_flat":
            should_skip = alignment in {"against", "flat"}
        elif skip_mode == "not_aligned":
            should_skip = alignment != "aligned"
        else:
            raise ValueError(skip_mode)
        if should_skip:
            pnl = float(trade["normalised_5_share_pnl"])
            skipped += 1
            skipped_pnl += pnl
            skipped_wins += int(pnl > 0)
            skipped_losses += int(pnl <= 0)
    guarded = baseline - skipped_pnl
    return {
        "window_seconds": window,
        "skip_mode": skip_mode,
        "baseline_norm_pnl": round(baseline, 6),
        "guarded_norm_pnl": round(guarded, 6),
        "delta_vs_baseline": round(guarded - baseline, 6),
        "skipped_trades": skipped,
        "skipped_wins": skipped_wins,
        "skipped_losses": skipped_losses,
        "skipped_norm_pnl": round(skipped_pnl, 6),
        "skip_efficiency": round((guarded - baseline) / skipped, 6) if skipped else "",
    }


def build_side_alignment_grid(trades: list[dict[str, Any]], windows: list[int]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for window in windows:
        for skip_mode in ("against", "against_or_flat", "not_aligned"):
            rows.append(simulate_side_alignment_filter(trades, window=window, skip_mode=skip_mode))
    rows.sort(key=lambda item: (float(item["delta_vs_baseline"]), -int(item["skipped_trades"])), reverse=True)
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        fieldnames = []
        seen: set[str] = set()
        for row in rows:
            for key in row:
                if key not in seen:
                    seen.add(key)
                    fieldnames.append(key)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_text(path: Path, lines: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def main() -> int:
    args = parse_args()
    if args.wide_grid:
        windows = range_int(5, 180, 5)
        pause_minutes_values = range_int(10, 120, 5)
        loss_streak_values = range_int(1, 6, 1)
        rolling_window_values = range_int(1, 6, 1)
        rolling_floor_values = range_float(0.5, 15.0, 0.5)
        btc_thresholds = range_float(0.5, 25.0, 0.5)
    else:
        windows = parse_int_list(args.momentum_windows)
        pause_minutes_values = parse_int_list(args.pause_minutes)
        loss_streak_values = parse_int_list(args.loss_streak_triggers)
        rolling_window_values = parse_int_list(args.rolling_trade_windows)
        rolling_floor_values = parse_float_list(args.rolling_damage_floors)
        btc_thresholds = parse_float_list(args.btc_thresholds)
    sessions = discover_sessions(args.logs_dir, args.since, args.mode)
    if not sessions:
        raise SystemExit(f"No sessions found in {args.logs_dir} since {args.since} mode={args.mode}")

    all_trades: list[dict[str, Any]] = []
    session_rows: list[dict[str, Any]] = []
    for session in sessions:
        tape = load_price_tape(session)
        trades = load_trades(session, tape, windows)
        all_trades.extend(trades)
        session_rows.append(
            {
                "session": session.name,
                "mode": session_mode(session),
                "trades": len(trades),
                "normalised_5_share_pnl": round(sum(float(row["normalised_5_share_pnl"]) for row in trades), 6),
                "raw_pnl": round(sum(float(row["pnl"]) for row in trades), 6),
            }
        )

    all_trades.sort(key=lambda row: row["entry_ts"])
    add_prior_trade_state(all_trades, args.initial_wallet)

    feature_summary = feature_rows(all_trades, windows)
    guard_grid = build_guard_grid(
        all_trades,
        args.initial_wallet,
        pause_minutes_values=pause_minutes_values,
        loss_streak_values=loss_streak_values,
        rolling_window_values=rolling_window_values,
        rolling_floor_values=rolling_floor_values,
    )
    btc_grid = build_btc_filter_grid(all_trades, windows, btc_thresholds)
    side_alignment_grid = build_side_alignment_grid(all_trades, windows)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.out_dir / "trade_regime_features.csv", all_trades)
    write_csv(args.out_dir / "session_summary.csv", session_rows)
    write_csv(args.out_dir / "feature_bucket_performance.csv", feature_summary)
    write_csv(args.out_dir / "global_guard_grid_all.csv", guard_grid)
    write_csv(args.out_dir / "global_guard_grid_top_50.csv", guard_grid[:50])
    write_csv(args.out_dir / "btc_filter_grid_all.csv", btc_grid)
    write_csv(args.out_dir / "btc_filter_grid_top_50.csv", btc_grid[:50])
    write_csv(args.out_dir / "side_alignment_filter_grid_all.csv", side_alignment_grid)
    write_csv(args.out_dir / "side_alignment_filter_grid_top_50.csv", side_alignment_grid[:50])

    baseline = sum(float(row["normalised_5_share_pnl"]) for row in all_trades)
    wins = sum(1 for row in all_trades if float(row["normalised_5_share_pnl"]) > 0)
    overview = [
        "Regime Guard Audit",
        f"sessions={len(sessions)}",
        f"trades={len(all_trades)}",
        f"baseline_normalised_5_share_pnl={baseline:.6f}",
        f"baseline_wr_pct={(wins / len(all_trades) * 100.0 if all_trades else 0.0):.3f}",
        f"initial_wallet={args.initial_wallet:.2f}",
        f"wide_grid={bool(args.wide_grid)}",
        f"loss_streak_triggers={loss_streak_values}",
        f"pause_minutes={pause_minutes_values}",
        f"btc_windows_seconds={windows}",
        f"btc_thresholds_bps={btc_thresholds}",
        "",
        f"best_global_guard={guard_grid[0] if guard_grid else {}}",
        f"best_btc_filter={btc_grid[0] if btc_grid else {}}",
        f"best_side_alignment_filter={side_alignment_grid[0] if side_alignment_grid else {}}",
        "",
        "Key files:",
        "  trade_regime_features.csv",
        "  feature_bucket_performance.csv",
        "  global_guard_grid_top_50.csv",
        "  btc_filter_grid_top_50.csv",
        "  side_alignment_filter_grid_top_50.csv",
        "",
        "Note:",
        "  Guard results are counterfactual on the actual trade stream.",
        "  They answer: if this global pause/filter had existed, which actual trades would it have skipped?",
        "  BTC filters use only cadence data available before entry.",
    ]
    write_text(args.out_dir / "README_summary.txt", overview)
    print("\n".join(overview))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import heapq
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Design-test global recovery guards on actual filled trades. "
            "Input should be trade_regime_features.csv from regime_guard_audit.py."
        )
    )
    parser.add_argument(
        "--features-csv",
        type=Path,
        default=Path("analysis/regime_guard_audit_20260615_wide/trade_regime_features.csv"),
        help="Actual filled trade feature CSV from scripts/regime_guard_audit.py.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("analysis/recovery_guard_design_20260615"),
        help="Output directory.",
    )
    parser.add_argument("--initial-wallet", type=float, default=60.0)
    parser.add_argument(
        "--expand-top",
        type=int,
        default=30,
        help="Number of best base trigger/block/timer configs to expand with during-recovery filters.",
    )
    parser.add_argument(
        "--side-windows",
        default="5,10,15,20,25,30,45,60",
        help="BTC momentum windows to test for side-vs-momentum recovery filters.",
    )
    parser.add_argument(
        "--side-thresholds",
        default="0.5,1,1.5,2,2.5,3,3.5,4,4.5,5,7.5,10",
        help="BTC bps thresholds to test for side-vs-momentum recovery filters.",
    )
    parser.add_argument(
        "--wide-side-grid",
        action="store_true",
        help="Use 5,10,...,180 second windows and 0.5,1.0,...,25.0 bps thresholds for side filters.",
    )
    return parser.parse_args()


def finite_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


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


def load_trades(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            item: dict[str, Any] = dict(row)
            item["entry_ts"] = finite_float(row.get("entry_ts"), 0.0) or 0.0
            item["exit_ts"] = finite_float(row.get("exit_ts"), 0.0) or 0.0
            item["normalised_5_share_pnl"] = finite_float(row.get("normalised_5_share_pnl"), 0.0) or 0.0
            item["entry_price"] = finite_float(row.get("entry_price"), 0.0) or 0.0
            item["side"] = str(row.get("side") or "").upper()
            rows.append(item)
    rows.sort(key=lambda item: (float(item["entry_ts"]), float(item["exit_ts"])))
    return rows


@dataclass(frozen=True)
class TriggerConfig:
    trigger_type: str
    loss_streak_trigger: int | None = None
    rolling_n: int | None = None
    rolling_floor: float | None = None

    def label(self) -> str:
        if self.trigger_type == "loss_streak":
            return f"loss_streak>={self.loss_streak_trigger}"
        if self.trigger_type == "rolling_damage":
            return f"last_{self.rolling_n}_pnl<=-{self.rolling_floor:g}"
        raise ValueError(self.trigger_type)

    def as_row(self) -> dict[str, Any]:
        return {
            "trigger_type": self.trigger_type,
            "loss_streak_trigger": self.loss_streak_trigger if self.loss_streak_trigger is not None else "",
            "rolling_n": self.rolling_n if self.rolling_n is not None else "",
            "rolling_floor": self.rolling_floor if self.rolling_floor is not None else "",
            "trigger_label": self.label(),
        }


@dataclass(frozen=True)
class DuringFilter:
    filter_type: str = "none"
    window_seconds: int | None = None
    threshold_bps: float | None = None
    scope: str = "recovery_only"

    def label(self) -> str:
        if self.filter_type == "none":
            return "none"
        return f"{self.scope}:skip_against_{self.window_seconds}s_{self.threshold_bps:g}bps"

    def as_row(self) -> dict[str, Any]:
        return {
            "during_filter": self.filter_type,
            "side_mom_window_seconds": self.window_seconds if self.window_seconds is not None else "",
            "side_mom_threshold_bps": self.threshold_bps if self.threshold_bps is not None else "",
            "side_filter_scope": self.scope,
            "during_filter_label": self.label(),
        }


@dataclass(frozen=True)
class RecoveryConfig:
    trigger: TriggerConfig
    forced_block_minutes: int
    recovery_minutes: int
    extend_on_loss_minutes: int
    exit_after_recovery_wins: int
    during_filter: DuringFilter

    def as_row(self) -> dict[str, Any]:
        row = self.trigger.as_row()
        row.update(
            {
                "forced_block_minutes": self.forced_block_minutes,
                "recovery_minutes": self.recovery_minutes,
                "extend_on_loss_minutes": self.extend_on_loss_minutes,
                "exit_after_recovery_wins": self.exit_after_recovery_wins,
            }
        )
        row.update(self.during_filter.as_row())
        return row


def trigger_fired(config: TriggerConfig, taken_pnls: list[float], loss_streak: int) -> bool:
    if config.trigger_type == "loss_streak":
        return config.loss_streak_trigger is not None and loss_streak >= config.loss_streak_trigger
    if config.trigger_type == "rolling_damage":
        if config.rolling_n is None or config.rolling_floor is None:
            return False
        if len(taken_pnls) < config.rolling_n:
            return False
        return sum(taken_pnls[-config.rolling_n :]) <= -config.rolling_floor
    raise ValueError(config.trigger_type)


def side_against_momentum(trade: dict[str, Any], window: int, threshold: float) -> bool:
    side = str(trade.get("side") or "")
    momentum = finite_float(trade.get(f"btc_mom_{window}s_bps"))
    if momentum is None or abs(momentum) < threshold or side not in {"UP", "DOWN"}:
        return False
    return (momentum > 0 and side == "DOWN") or (momentum < 0 and side == "UP")


def during_filter_skips(trade: dict[str, Any], filter_config: DuringFilter, recovery_active: bool) -> bool:
    if filter_config.filter_type == "none":
        return False
    if filter_config.scope == "recovery_only" and not recovery_active:
        return False
    if filter_config.filter_type == "side_against":
        assert filter_config.window_seconds is not None
        assert filter_config.threshold_bps is not None
        return side_against_momentum(trade, filter_config.window_seconds, filter_config.threshold_bps)
    raise ValueError(filter_config.filter_type)


def simulate_config(trades: list[dict[str, Any]], config: RecoveryConfig) -> dict[str, Any]:
    baseline = sum(float(trade["normalised_5_share_pnl"]) for trade in trades)
    ordered_indices = sorted(range(len(trades)), key=lambda index: float(trades[index]["entry_ts"]))
    exit_heap: list[tuple[float, int]] = []
    active_taken: set[int] = set()
    taken_pnls: list[float] = []
    recovery_until = 0.0
    forced_block_until = 0.0
    loss_streak = 0
    recovery_win_streak = 0
    recovery_active = False

    skipped_pnl = 0.0
    skipped_wins = 0
    skipped_losses = 0
    skipped_forced_block = 0
    skipped_during_filter = 0
    taken = 0
    triggers = 0
    extensions = 0
    early_exits = 0
    recovery_taken = 0

    def deactivate_if_expired(now_ts: float) -> None:
        nonlocal recovery_active, recovery_win_streak
        if recovery_active and now_ts >= recovery_until and now_ts >= forced_block_until:
            recovery_active = False
            recovery_win_streak = 0

    def activate(now_ts: float) -> None:
        nonlocal recovery_active, recovery_until, forced_block_until, recovery_win_streak, triggers
        if recovery_active:
            return
        recovery_active = True
        recovery_win_streak = 0
        triggers += 1
        recovery_until = max(recovery_until, now_ts + config.recovery_minutes * 60.0)
        forced_block_until = max(forced_block_until, now_ts + config.forced_block_minutes * 60.0)

    for index in ordered_indices:
        trade = trades[index]
        entry_ts = float(trade["entry_ts"])
        while exit_heap and exit_heap[0][0] <= entry_ts:
            exit_ts, closed_index = heapq.heappop(exit_heap)
            if closed_index not in active_taken:
                continue
            active_taken.remove(closed_index)
            closed = trades[closed_index]
            pnl = float(closed["normalised_5_share_pnl"])
            taken_pnls.append(pnl)
            if pnl > 0:
                loss_streak = 0
                if bool(closed.get("_entry_recovery_active")):
                    recovery_win_streak += 1
            else:
                loss_streak += 1
                recovery_win_streak = 0
                if recovery_active and config.extend_on_loss_minutes > 0:
                    new_until = exit_ts + config.extend_on_loss_minutes * 60.0
                    if new_until > recovery_until:
                        recovery_until = new_until
                        extensions += 1

            if recovery_active and config.exit_after_recovery_wins > 0 and recovery_win_streak >= config.exit_after_recovery_wins:
                recovery_active = False
                recovery_win_streak = 0
                early_exits += 1
            deactivate_if_expired(exit_ts)
            if not recovery_active and trigger_fired(config.trigger, taken_pnls, loss_streak):
                activate(exit_ts)

        deactivate_if_expired(entry_ts)
        pnl = float(trade["normalised_5_share_pnl"])
        if recovery_active and entry_ts < forced_block_until:
            skipped_pnl += pnl
            skipped_wins += int(pnl > 0)
            skipped_losses += int(pnl <= 0)
            skipped_forced_block += 1
            continue
        if during_filter_skips(trade, config.during_filter, recovery_active):
            skipped_pnl += pnl
            skipped_wins += int(pnl > 0)
            skipped_losses += int(pnl <= 0)
            skipped_during_filter += 1
            continue

        trade["_entry_recovery_active"] = bool(recovery_active)
        recovery_taken += int(recovery_active)
        active_taken.add(index)
        heapq.heappush(exit_heap, (float(trade["exit_ts"]), index))
        taken += 1

    while exit_heap:
        exit_ts, closed_index = heapq.heappop(exit_heap)
        if closed_index not in active_taken:
            continue
        active_taken.remove(closed_index)
        closed = trades[closed_index]
        pnl = float(closed["normalised_5_share_pnl"])
        taken_pnls.append(pnl)
        if pnl > 0:
            loss_streak = 0
            if bool(closed.get("_entry_recovery_active")):
                recovery_win_streak += 1
        else:
            loss_streak += 1
            recovery_win_streak = 0
        deactivate_if_expired(exit_ts)
        if not recovery_active and trigger_fired(config.trigger, taken_pnls, loss_streak):
            activate(exit_ts)

    guarded = baseline - skipped_pnl
    row = config.as_row()
    row.update(
        {
            "baseline_norm_pnl": round(baseline, 6),
            "guarded_norm_pnl": round(guarded, 6),
            "delta_vs_baseline": round(guarded - baseline, 6),
            "skipped_trades": skipped_wins + skipped_losses,
            "skipped_wins": skipped_wins,
            "skipped_losses": skipped_losses,
            "skipped_norm_pnl": round(skipped_pnl, 6),
            "skipped_forced_block": skipped_forced_block,
            "skipped_during_filter": skipped_during_filter,
            "taken_trades": taken,
            "recovery_taken_trades": recovery_taken,
            "triggers": triggers,
            "extensions": extensions,
            "early_exits": early_exits,
            "skip_efficiency": round((guarded - baseline) / (skipped_wins + skipped_losses), 6)
            if skipped_wins + skipped_losses
            else "",
        }
    )
    return row


def trigger_configs() -> list[TriggerConfig]:
    configs: list[TriggerConfig] = []
    for streak in range_int(1, 6, 1):
        configs.append(TriggerConfig("loss_streak", loss_streak_trigger=streak))
    for rolling_n in range_int(1, 6, 1):
        for floor in range_float(1.0, 15.0, 0.5):
            configs.append(TriggerConfig("rolling_damage", rolling_n=rolling_n, rolling_floor=floor))
    return configs


def base_grid(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    no_filter = DuringFilter()
    for trigger in trigger_configs():
        for forced_block in range_int(0, 120, 5):
            for recovery_minutes in range_int(15, 180, 15):
                config = RecoveryConfig(
                    trigger=trigger,
                    forced_block_minutes=forced_block,
                    recovery_minutes=recovery_minutes,
                    extend_on_loss_minutes=0,
                    exit_after_recovery_wins=0,
                    during_filter=no_filter,
                )
                rows.append(simulate_config(trades, config))
    rows.sort(key=lambda item: (float(item["delta_vs_baseline"]), -int(item["skipped_trades"])), reverse=True)
    return rows


def config_from_row(row: dict[str, Any]) -> RecoveryConfig:
    trigger_type = str(row["trigger_type"])
    if trigger_type == "loss_streak":
        trigger = TriggerConfig("loss_streak", loss_streak_trigger=int(row["loss_streak_trigger"]))
    else:
        trigger = TriggerConfig(
            "rolling_damage",
            rolling_n=int(row["rolling_n"]),
            rolling_floor=float(row["rolling_floor"]),
        )
    return RecoveryConfig(
        trigger=trigger,
        forced_block_minutes=int(row["forced_block_minutes"]),
        recovery_minutes=int(row["recovery_minutes"]),
        extend_on_loss_minutes=0,
        exit_after_recovery_wins=0,
        during_filter=DuringFilter(),
    )


def during_filters(windows: list[int], thresholds: list[float]) -> list[DuringFilter]:
    filters = [DuringFilter()]
    for window in windows:
        for threshold in thresholds:
            filters.append(DuringFilter("side_against", window_seconds=window, threshold_bps=threshold, scope="recovery_only"))
    return filters


def expanded_grid(
    trades: list[dict[str, Any]],
    base_rows: list[dict[str, Any]],
    *,
    expand_top: int,
    windows: list[int],
    thresholds: list[float],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    filters = during_filters(windows, thresholds)
    for base in base_rows[:expand_top]:
        base_config = config_from_row(base)
        for filter_config in filters:
            for extend in (0, 15, 30, 45, 60, 90):
                for exit_wins in (0, 2, 3):
                    config = RecoveryConfig(
                        trigger=base_config.trigger,
                        forced_block_minutes=base_config.forced_block_minutes,
                        recovery_minutes=base_config.recovery_minutes,
                        extend_on_loss_minutes=extend,
                        exit_after_recovery_wins=exit_wins,
                        during_filter=filter_config,
                    )
                    rows.append(simulate_config(trades, config))
    rows.sort(key=lambda item: (float(item["delta_vs_baseline"]), -int(item["skipped_trades"])), reverse=True)
    return rows


def constant_side_filter_grid(trades: list[dict[str, Any]], windows: list[int], thresholds: list[float]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    baseline = sum(float(trade["normalised_5_share_pnl"]) for trade in trades)
    for window in windows:
        for threshold in thresholds:
            skipped_pnl = 0.0
            skipped_wins = 0
            skipped_losses = 0
            for trade in trades:
                if side_against_momentum(trade, window, threshold):
                    pnl = float(trade["normalised_5_share_pnl"])
                    skipped_pnl += pnl
                    skipped_wins += int(pnl > 0)
                    skipped_losses += int(pnl <= 0)
            guarded = baseline - skipped_pnl
            skipped = skipped_wins + skipped_losses
            rows.append(
                {
                    "window_seconds": window,
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
            )
    rows.sort(key=lambda item: (float(item["delta_vs_baseline"]), -int(item["skipped_trades"])), reverse=True)
    return rows


def main() -> int:
    args = parse_args()
    if args.wide_side_grid:
        windows = range_int(5, 180, 5)
        thresholds = range_float(0.5, 25.0, 0.5)
    else:
        windows = parse_int_list(args.side_windows)
        thresholds = parse_float_list(args.side_thresholds)

    trades = load_trades(args.features_csv)
    if not trades:
        raise SystemExit(f"No trades found in {args.features_csv}")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    base_rows = base_grid(trades)
    expanded_rows = expanded_grid(
        trades,
        base_rows,
        expand_top=args.expand_top,
        windows=windows,
        thresholds=thresholds,
    )
    constant_side_rows = constant_side_filter_grid(trades, windows, thresholds)

    write_csv(args.out_dir / "base_recovery_grid_all.csv", base_rows)
    write_csv(args.out_dir / "base_recovery_grid_top_100.csv", base_rows[:100])
    write_csv(args.out_dir / "expanded_recovery_grid_all.csv", expanded_rows)
    write_csv(args.out_dir / "expanded_recovery_grid_top_100.csv", expanded_rows[:100])
    write_csv(args.out_dir / "constant_side_vs_mom_grid.csv", constant_side_rows)

    overview = [
        "Recovery Guard Design Audit",
        f"features_csv={args.features_csv}",
        f"actual_filled_trades={len(trades)}",
        f"baseline_normalised_5_share_pnl={sum(float(t['normalised_5_share_pnl']) for t in trades):.6f}",
        f"expand_top={args.expand_top}",
        f"side_windows={windows}",
        f"side_thresholds={thresholds}",
        "",
        f"best_base_recovery={base_rows[0] if base_rows else {}}",
        f"best_expanded_recovery={expanded_rows[0] if expanded_rows else {}}",
        f"best_constant_side_filter={constant_side_rows[0] if constant_side_rows else {}}",
        "",
        "Key files:",
        "  base_recovery_grid_top_100.csv",
        "  expanded_recovery_grid_top_100.csv",
        "  constant_side_vs_mom_grid.csv",
        "",
        "Interpretation:",
        "  base_recovery_grid tests global trigger + forced block + recovery timer.",
        "  expanded_recovery_grid adds during-recovery side-vs-momentum blocks, extension on losses, and early exit after wins.",
        "  constant_side_vs_mom_grid answers whether side-vs-mom should be always-on rather than recovery-only.",
    ]
    write_text(args.out_dir / "README_summary.txt", overview)
    print("\n".join(overview))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

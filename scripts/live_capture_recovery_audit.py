#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


DEFAULT_TIERS: tuple[tuple[float, float], ...] = (
    (0.0, 5.0),
    (50.0, 6.0),
    (60.0, 7.0),
    (70.0, 8.0),
    (85.0, 9.0),
    (100.0, 10.0),
    (125.0, 12.0),
    (150.0, 15.0),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Analyse whether the live policy surface is being captured by real fills, "
            "then replay sizing and recovery rules on the actual fill stream."
        )
    )
    parser.add_argument(
        "--audit-dir",
        type=Path,
        default=Path("analysis/live_policy_audit_20260615_max085"),
        help="Output directory created by scripts/live_policy_audit.py.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("analysis/live_capture_recovery_audit"),
        help="Directory for this script's outputs.",
    )
    parser.add_argument("--initial-wallet", type=float, default=60.0, help="Wallet used for replayed sizing/recovery.")
    parser.add_argument("--min-slice-n", type=int, default=20, help="Minimum candidate or fill count for slice recommendations.")
    return parser.parse_args()


def finite_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


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


def pct(numerator: float, denominator: float) -> float:
    return numerator / denominator * 100.0 if denominator else 0.0


def key_tuple(row: dict[str, Any], fields: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(str(row.get(field, "")) for field in fields)


def trade_time(row: dict[str, str]) -> str:
    return str(row.get("entry_time_utc") or row.get("timestamp_utc") or "")


@dataclass
class Summary:
    n: int = 0
    wins: int = 0
    pnl: float = 0.0
    price_sum: float = 0.0

    def add(self, *, pnl: float, win: bool, price: float) -> None:
        self.n += 1
        self.wins += int(win)
        self.pnl += pnl
        self.price_sum += price

    def row(self, prefix: str = "") -> dict[str, Any]:
        return {
            f"{prefix}n": self.n,
            f"{prefix}wins": self.wins,
            f"{prefix}wr_pct": round(pct(self.wins, self.n), 3),
            f"{prefix}norm_pnl": round(self.pnl, 6),
            f"{prefix}avg_price": round(self.price_sum / self.n, 6) if self.n else "",
        }


def enrich_numeric(rows: list[dict[str, str]], *, kind: str) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in rows:
        item: dict[str, Any] = dict(row)
        if kind == "trade":
            item["normalised_5_share_pnl"] = finite_float(row.get("normalised_5_share_pnl"))
            item["pnl"] = finite_float(row.get("pnl"))
            item["entry_price"] = finite_float(row.get("entry_price"))
            item["shares"] = finite_float(row.get("shares"))
            item["win"] = int(finite_float(row.get("win")) > 0)
            item["price"] = item["entry_price"]
            item["side"] = str(row.get("side", "")).upper()
        else:
            item["normalised_5_share_pnl"] = finite_float(row.get("normalised_5_share_pnl"))
            item["maker_price"] = finite_float(row.get("maker_price"))
            item["win"] = int(finite_float(row.get("win")) > 0)
            item["price"] = item["maker_price"]
            item["side"] = str(row.get("side", "")).upper()
            item["recovery_blocked"] = int(finite_float(row.get("recovery_blocked")) > 0)
        output.append(item)
    return output


def build_trade_lookup(trades: list[dict[str, Any]]) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    lookup: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for trade in sorted(trades, key=trade_time):
        session = str(trade.get("session", ""))
        slug = str(trade.get("market_slug", ""))
        side = str(trade.get("side", ""))
        key = (session, slug, side, trade_time(trade))
        lookup[key] = trade
    return lookup


def match_candidates_to_fills(
    candidates: list[dict[str, Any]],
    trades: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    trades_by_market: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    trades_by_market_side: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        session = str(trade.get("session", ""))
        slug = str(trade.get("market_slug", ""))
        side = str(trade.get("side", ""))
        trades_by_market[(session, slug)].append(trade)
        trades_by_market_side[(session, slug, side)].append(trade)

    matched: list[dict[str, Any]] = []
    for candidate in candidates:
        session = str(candidate.get("session", ""))
        slug = str(candidate.get("market_slug", ""))
        side = str(candidate.get("side", ""))
        same_side = trades_by_market_side.get((session, slug, side), [])
        same_market = trades_by_market.get((session, slug), [])
        fill = same_side[0] if same_side else None
        market_fill = same_market[0] if same_market else None
        status = "same_side_fill" if fill else "opposite_side_fill" if same_market else "unfilled"
        actual_norm = finite_float(fill.get("normalised_5_share_pnl")) if fill else 0.0
        actual_raw = finite_float(fill.get("pnl")) if fill else 0.0
        actual_price = finite_float(fill.get("entry_price")) if fill else 0.0
        candidate_pnl = finite_float(candidate.get("normalised_5_share_pnl"))
        matched.append(
            {
                **candidate,
                "fill_status": status,
                "filled_same_side": int(bool(fill)),
                "filled_any_side": int(bool(same_market)),
                "actual_side": str(market_fill.get("side", "")) if market_fill else "",
                "actual_entry_time_utc": str(market_fill.get("entry_time_utc", "")) if market_fill else "",
                "actual_price": actual_price if fill else "",
                "actual_norm_pnl": actual_norm if fill else "",
                "actual_raw_pnl": actual_raw if fill else "",
                "candidate_minus_actual_norm_pnl": round(candidate_pnl - actual_norm, 6) if fill else "",
                "actual_minus_candidate_norm_pnl": round(actual_norm - candidate_pnl, 6) if fill else "",
                "actual_minus_candidate_price": round(actual_price - finite_float(candidate.get("maker_price")), 6) if fill else "",
            }
        )
    return matched, trades


def capture_by_slice(matched: list[dict[str, Any]], slice_fields: tuple[str, ...]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, ...], dict[str, Summary | int | float]] = {}
    for row in matched:
        key = key_tuple(row, slice_fields)
        if key not in groups:
            groups[key] = {
                "candidate": Summary(),
                "filled": Summary(),
                "unfilled": Summary(),
                "opposite": Summary(),
                "actual": Summary(),
                "filled_same_side": 0,
                "filled_any_side": 0,
            }
        group = groups[key]
        candidate_pnl = finite_float(row.get("normalised_5_share_pnl"))
        win = bool(int(row.get("win") or 0))
        price = finite_float(row.get("maker_price"))
        group["candidate"].add(pnl=candidate_pnl, win=win, price=price)  # type: ignore[union-attr]
        if int(row.get("filled_same_side") or 0):
            group["filled"].add(pnl=candidate_pnl, win=win, price=price)  # type: ignore[union-attr]
            group["actual"].add(
                pnl=finite_float(row.get("actual_norm_pnl")),
                win=finite_float(row.get("actual_norm_pnl")) > 0,
                price=finite_float(row.get("actual_price")),
            )  # type: ignore[union-attr]
            group["filled_same_side"] = int(group["filled_same_side"]) + 1
        elif int(row.get("filled_any_side") or 0):
            group["opposite"].add(pnl=candidate_pnl, win=win, price=price)  # type: ignore[union-attr]
            group["filled_any_side"] = int(group["filled_any_side"]) + 1
        else:
            group["unfilled"].add(pnl=candidate_pnl, win=win, price=price)  # type: ignore[union-attr]

    output: list[dict[str, Any]] = []
    for key, group in groups.items():
        candidate = group["candidate"]
        filled = group["filled"]
        unfilled = group["unfilled"]
        opposite = group["opposite"]
        actual = group["actual"]
        assert isinstance(candidate, Summary)
        assert isinstance(filled, Summary)
        assert isinstance(unfilled, Summary)
        assert isinstance(opposite, Summary)
        assert isinstance(actual, Summary)
        row = {field: value for field, value in zip(slice_fields, key)}
        row.update(candidate.row("candidate_"))
        row.update(filled.row("filled_candidate_"))
        row.update(unfilled.row("unfilled_candidate_"))
        row.update(opposite.row("opposite_side_candidate_"))
        row.update(actual.row("actual_fill_"))
        row["same_side_fill_rate_pct"] = round(pct(filled.n, candidate.n), 3)
        row["any_side_fill_rate_pct"] = round(pct(filled.n + opposite.n, candidate.n), 3)
        row["actual_minus_filled_candidate_norm_pnl"] = round(actual.pnl - filled.pnl, 6)
        row["unfilled_candidate_minus_filled_candidate_avg_pnl"] = round(
            (unfilled.pnl / unfilled.n if unfilled.n else 0.0) - (filled.pnl / filled.n if filled.n else 0.0),
            6,
        )
        output.append(row)
    output.sort(key=lambda item: float(item["actual_minus_filled_candidate_norm_pnl"]))
    return output


def shares_for_wallet(wallet: float) -> float:
    shares = DEFAULT_TIERS[0][1]
    for threshold, tier_shares in DEFAULT_TIERS:
        if wallet >= threshold:
            shares = tier_shares
    return shares


def trade_unit_pnl(trade: dict[str, Any]) -> float:
    price = finite_float(trade.get("entry_price"))
    return (1.0 - price) if int(trade.get("win") or 0) else -price


def replay_sizing(trades: list[dict[str, Any]], initial_wallet: float) -> list[dict[str, Any]]:
    ordered = sorted(trades, key=trade_time)
    strategies = {
        "flat_5": lambda wallet, streak, dd: 5.0,
        "flat_6": lambda wallet, streak, dd: 6.0,
        "flat_7": lambda wallet, streak, dd: 7.0,
        "wallet_ladder": lambda wallet, streak, dd: shares_for_wallet(wallet),
        "ladder_force_5_dd8": lambda wallet, streak, dd: 5.0 if dd >= 8.0 else shares_for_wallet(wallet),
        "ladder_plus1_near_high": lambda wallet, streak, dd: (
            5.0 if dd >= 8.0 else shares_for_wallet(wallet) + (1.0 if streak > 0.0 and dd < 5.0 else 0.0)
        ),
    }
    rows: list[dict[str, Any]] = []
    actual_pnl = sum(finite_float(trade.get("pnl")) for trade in ordered)
    actual_norm = sum(finite_float(trade.get("normalised_5_share_pnl")) for trade in ordered)
    rows.append(
        {
            "strategy": "actual_logged_shares",
            "start_wallet": initial_wallet,
            "end_wallet": round(initial_wallet + actual_pnl, 6),
            "pnl": round(actual_pnl, 6),
            "normalised_reference_pnl": round(actual_norm, 6),
            "max_drawdown_pct": "",
            "trades": len(ordered),
            "avg_shares": round(sum(finite_float(t.get("shares")) for t in ordered) / len(ordered), 6) if ordered else "",
        }
    )

    for name, share_fn in strategies.items():
        wallet = initial_wallet
        high = initial_wallet
        max_dd = 0.0
        streak = 0.0
        total_pnl = 0.0
        share_sum = 0.0
        for trade in ordered:
            dd = pct(high - wallet, high)
            shares = share_fn(wallet, streak, dd)
            pnl = shares * trade_unit_pnl(trade)
            wallet += pnl
            total_pnl += pnl
            share_sum += shares
            high = max(high, wallet)
            max_dd = max(max_dd, pct(high - wallet, high))
            if pnl > 0:
                streak += pnl
            else:
                streak = 0.0
        rows.append(
            {
                "strategy": name,
                "start_wallet": initial_wallet,
                "end_wallet": round(wallet, 6),
                "pnl": round(total_pnl, 6),
                "normalised_reference_pnl": round(actual_norm, 6),
                "max_drawdown_pct": round(max_dd, 3),
                "trades": len(ordered),
                "avg_shares": round(share_sum / len(ordered), 6) if ordered else "",
            }
        )
    rows.sort(key=lambda item: float(item["pnl"]), reverse=True)
    return rows


def recovery_slice_key(trade: dict[str, Any], mode: str) -> tuple[str, ...]:
    if mode == "session":
        return key_tuple(trade, ("day_type", "session_bucket"))
    if mode == "style_price":
        return key_tuple(trade, ("trade_style", "price_band"))
    if mode == "style_vol_price":
        return key_tuple(trade, ("trade_style", "volatility_bucket", "price_band"))
    if mode == "session_style_price":
        return key_tuple(trade, ("day_type", "session_bucket", "trade_style", "price_band"))
    raise ValueError(mode)


def replay_recovery_grid(trades: list[dict[str, Any]], initial_wallet: float) -> list[dict[str, Any]]:
    ordered = sorted(trades, key=trade_time)
    baseline = sum(finite_float(trade.get("normalised_5_share_pnl")) for trade in ordered)
    rows: list[dict[str, Any]] = []
    modes = ["session", "style_price", "style_vol_price", "session_style_price"]
    activate_thresholds = [8.0, 10.0, 12.0, 14.0, 16.0]
    exit_thresholds = [4.0, 6.0, 8.0]
    damage_floors = [3.0, 4.0, 5.0, 6.0, 8.0]
    cooldown_trade_counts = [3, 6, 12]

    for mode in modes:
        for activate_dd in activate_thresholds:
            for exit_dd in exit_thresholds:
                if exit_dd >= activate_dd:
                    continue
                for floor in damage_floors:
                    for cooldown_trades in cooldown_trade_counts:
                        wallet = initial_wallet
                        high = initial_wallet
                        active = False
                        slice_damage: dict[tuple[str, ...], float] = defaultdict(float)
                        slice_cooldown: dict[tuple[str, ...], int] = defaultdict(int)
                        taken_pnl = 0.0
                        skipped_pnl = 0.0
                        skipped_wins = 0
                        skipped_losses = 0
                        pauses = 0
                        taken = 0
                        skipped = 0

                        for trade in ordered:
                            for key in list(slice_cooldown):
                                if slice_cooldown[key] > 0:
                                    slice_cooldown[key] -= 1
                                if slice_cooldown[key] <= 0:
                                    slice_cooldown.pop(key, None)

                            dd = pct(high - wallet, high)
                            if active and dd < exit_dd:
                                active = False
                                slice_damage.clear()
                                slice_cooldown.clear()
                            elif not active and dd >= activate_dd:
                                active = True
                                slice_damage.clear()

                            pnl = finite_float(trade.get("normalised_5_share_pnl"))
                            key = recovery_slice_key(trade, mode)
                            if active and key in slice_cooldown:
                                skipped += 1
                                skipped_pnl += pnl
                                skipped_wins += int(pnl > 0)
                                skipped_losses += int(pnl <= 0)
                                continue

                            taken += 1
                            taken_pnl += pnl
                            wallet += pnl
                            high = max(high, wallet)
                            if active:
                                slice_damage[key] += pnl
                                if pnl < 0 and slice_damage[key] <= -floor:
                                    slice_cooldown[key] = cooldown_trades
                                    slice_damage[key] = 0.0
                                    pauses += 1

                        rows.append(
                            {
                                "slice_mode": mode,
                                "activate_dd_pct": activate_dd,
                                "exit_dd_pct": exit_dd,
                                "damage_floor_norm": floor,
                                "cooldown_trades": cooldown_trades,
                                "baseline_norm_pnl": round(baseline, 6),
                                "guarded_norm_pnl": round(taken_pnl, 6),
                                "delta_vs_baseline": round(taken_pnl - baseline, 6),
                                "skipped_trades": skipped,
                                "skipped_wins": skipped_wins,
                                "skipped_losses": skipped_losses,
                                "skipped_norm_pnl": round(skipped_pnl, 6),
                                "taken_trades": taken,
                                "pauses": pauses,
                            }
                        )
    rows.sort(key=lambda item: (float(item["delta_vs_baseline"]), -int(item["skipped_trades"])), reverse=True)
    return rows


def slice_recommendations(capture_rows: list[dict[str, Any]], min_slice_n: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in capture_rows:
        candidate_n = int(row.get("candidate_n") or 0)
        filled_n = int(row.get("actual_fill_n") or 0)
        candidate_pnl = finite_float(row.get("candidate_norm_pnl"))
        actual_pnl = finite_float(row.get("actual_fill_norm_pnl"))
        fill_rate = finite_float(row.get("same_side_fill_rate_pct"))
        gap = finite_float(row.get("actual_minus_filled_candidate_norm_pnl"))
        if candidate_n < min_slice_n and filled_n < min_slice_n:
            action = "insufficient_sample"
        elif candidate_pnl <= 0 and actual_pnl <= 0:
            action = "block_or_min_size"
        elif candidate_pnl > 0 and actual_pnl < 0:
            action = "capture_problem_min_size"
        elif candidate_pnl > 0 and actual_pnl > 0 and gap >= -5:
            action = "allow_full_size"
        elif candidate_pnl > 0 and actual_pnl > 0:
            action = "allow_but_monitor_capture_gap"
        else:
            action = "min_size_until_more_data"
        rec = dict(row)
        rec["recommended_action"] = action
        rec["candidate_n"] = candidate_n
        rec["actual_fill_n"] = filled_n
        rec["same_side_fill_rate_pct"] = round(fill_rate, 3)
        rows.append(rec)
    order = {
        "capture_problem_min_size": 0,
        "block_or_min_size": 1,
        "allow_but_monitor_capture_gap": 2,
        "allow_full_size": 3,
        "min_size_until_more_data": 4,
        "insufficient_sample": 5,
    }
    rows.sort(key=lambda item: (order.get(str(item["recommended_action"]), 9), float(item.get("actual_fill_norm_pnl") or 0.0)))
    return rows


def main() -> int:
    args = parse_args()
    trades_path = args.audit_dir / "executed_trades_enriched.csv"
    candidates_path = args.audit_dir / "cadence_candidates_first.csv"
    if not trades_path.exists() or not candidates_path.exists():
        raise SystemExit(
            f"Missing audit CSVs in {args.audit_dir}. Run scripts/live_policy_audit.py first."
        )

    trades = enrich_numeric(read_csv(trades_path), kind="trade")
    candidates = enrich_numeric(read_csv(candidates_path), kind="candidate")
    matched, _ = match_candidates_to_fills(candidates, trades)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.out_dir / "candidate_fill_matches.csv", matched)

    slice_fields = ("trade_style", "volatility_bucket", "price_band")
    capture_rows = capture_by_slice(matched, slice_fields)
    write_csv(args.out_dir / "capture_by_style_vol_price.csv", capture_rows)
    write_csv(args.out_dir / "slice_recommendations.csv", slice_recommendations(capture_rows, args.min_slice_n))

    session_capture_rows = capture_by_slice(matched, ("day_type", "session_bucket", "trade_style", "price_band"))
    write_csv(args.out_dir / "capture_by_session_style_price.csv", session_capture_rows)

    sizing_rows = replay_sizing(trades, args.initial_wallet)
    write_csv(args.out_dir / "sizing_replay.csv", sizing_rows)

    recovery_rows = replay_recovery_grid(trades, args.initial_wallet)
    write_csv(args.out_dir / "recovery_grid_all.csv", recovery_rows)
    write_csv(args.out_dir / "recovery_grid_top_50.csv", recovery_rows[:50])

    total_candidates = Summary()
    total_filled_candidates = Summary()
    total_unfilled_candidates = Summary()
    total_actual = Summary()
    for row in matched:
        pnl = finite_float(row.get("normalised_5_share_pnl"))
        total_candidates.add(pnl=pnl, win=bool(int(row.get("win") or 0)), price=finite_float(row.get("maker_price")))
        if int(row.get("filled_same_side") or 0):
            total_filled_candidates.add(pnl=pnl, win=bool(int(row.get("win") or 0)), price=finite_float(row.get("maker_price")))
            actual_pnl = finite_float(row.get("actual_norm_pnl"))
            total_actual.add(pnl=actual_pnl, win=actual_pnl > 0, price=finite_float(row.get("actual_price")))
        elif not int(row.get("filled_any_side") or 0):
            total_unfilled_candidates.add(pnl=pnl, win=bool(int(row.get("win") or 0)), price=finite_float(row.get("maker_price")))

    best_recovery = recovery_rows[0] if recovery_rows else {}
    overview = [
        "Live Capture & Recovery Audit",
        f"audit_dir={args.audit_dir}",
        f"initial_wallet={args.initial_wallet:.2f}",
        "",
        f"candidate_surface={total_candidates.row()}",
        f"candidate_same_side_filled={total_filled_candidates.row()}",
        f"candidate_unfilled={total_unfilled_candidates.row()}",
        f"actual_fills_for_matched_candidates={total_actual.row()}",
        f"sizing_best={sizing_rows[0] if sizing_rows else {}}",
        f"recovery_grid_best={best_recovery}",
        "",
        "Interpretation:",
        "  candidate_surface is not a tradable PnL estimate; it assumes every first candidate fills.",
        "  candidate_unfilled being much better than candidate_same_side_filled is evidence of fill/adverse-selection drag.",
        "  actual_fills_for_matched_candidates below candidate_same_side_filled means live fills captured worse prices/outcomes than the signal surface.",
    ]
    write_text(args.out_dir / "README_summary.txt", overview)
    print("\n".join(overview))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

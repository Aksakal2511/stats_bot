#!/usr/bin/env python3
from __future__ import annotations

import argparse
import bisect
import csv
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


MARKET_TS_RE = re.compile(r"-(\d{10})$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Replay the current global BTC side-vs-momentum guard plus recent-damage recovery, "
            "and test recovery edge-threshold bumps."
        )
    )
    parser.add_argument(
        "--matches-csv",
        type=Path,
        default=Path("analysis/live_capture_recovery_audit_20260615_max060/candidate_fill_matches.csv"),
        help="candidate_fill_matches.csv from scripts/live_capture_recovery_audit.py.",
    )
    parser.add_argument("--logs-dir", type=Path, default=Path("logs"), help="Stats bot logs directory.")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("analysis/recovery_edge_bump_audit_20260615_max060"),
        help="Output directory.",
    )
    parser.add_argument("--recovery-minutes", type=int, default=120)
    parser.add_argument("--block-minutes", default="40,60,80,100")
    parser.add_argument("--edge-bumps", default="0,0.01,0.02,0.03,0.04,0.05,0.06,0.07,0.08,0.09,0.10,0.11,0.12,0.13,0.14,0.15")
    parser.add_argument("--trigger-count", type=int, default=4)
    parser.add_argument("--trigger-floor", type=float, default=-8.0)
    parser.add_argument("--mom-window-seconds", type=int, default=20)
    parser.add_argument("--mom-threshold-bps", type=float, default=2.5)
    return parser.parse_args()


def finite_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_float_list(text: str) -> list[float]:
    return [float(item.strip()) for item in text.split(",") if item.strip()]


def parse_int_list(text: str) -> list[int]:
    return [int(item.strip()) for item in text.split(",") if item.strip()]


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


def market_end_ts(slug: str) -> float:
    match = MARKET_TS_RE.search(str(slug or ""))
    if not match:
        return 0.0
    return float(int(match.group(1)) + 300)


@dataclass(frozen=True)
class BtcSeries:
    timestamps: list[float]
    closes: list[float]

    def close_at_or_before(self, ts: float) -> float | None:
        index = bisect.bisect_right(self.timestamps, ts) - 1
        if index < 0:
            return None
        return self.closes[index]

    def momentum_bps(self, ts: float, seconds: int) -> float | None:
        latest = self.close_at_or_before(ts)
        prior = self.close_at_or_before(ts - seconds)
        if latest is None or prior is None or latest <= 0.0 or prior <= 0.0:
            return None
        return 10000.0 * (latest - prior) / prior


def load_btc_series(logs_dir: Path) -> dict[str, BtcSeries]:
    by_session: dict[str, dict[int, float]] = {}
    for path in logs_dir.glob("*/stats_cadence_1s.csv"):
        session = path.parent.name
        points = by_session.setdefault(session, {})
        with path.open(newline="") as handle:
            for row in csv.DictReader(handle):
                dt = parse_dt(row.get("timestamp_utc"))
                close = finite_float(row.get("cb_close_1s"))
                if dt is None or close is None or close <= 0.0:
                    continue
                points[int(dt.timestamp())] = close
    output: dict[str, BtcSeries] = {}
    for session, points in by_session.items():
        timestamps = sorted(points)
        output[session] = BtcSeries([float(ts) for ts in timestamps], [float(points[ts]) for ts in timestamps])
    return output


def side_against_momentum(side: str, momentum: float | None, threshold: float) -> bool:
    side = str(side or "").upper()
    if momentum is None or abs(momentum) < threshold or side not in {"UP", "DOWN"}:
        return False
    return (momentum > 0.0 and side == "DOWN") or (momentum < 0.0 and side == "UP")


def build_replay_rows(matches: list[dict[str, str]], btc: dict[str, BtcSeries], *, mode: str, mom_window: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in matches:
        if mode == "actual_same_side" and int(finite_float(row.get("filled_same_side"), 0.0) or 0.0) <= 0:
            continue
        entry_dt = parse_dt(row.get("timestamp_utc"))
        if entry_dt is None:
            continue
        session = str(row.get("session") or "")
        entry_ts = entry_dt.timestamp()
        series = btc.get(session)
        momentum = series.momentum_bps(entry_ts, mom_window) if series is not None else None
        if mode == "actual_same_side":
            pnl = finite_float(row.get("actual_norm_pnl"))
            price = finite_float(row.get("actual_price"))
        else:
            pnl = finite_float(row.get("normalised_5_share_pnl"))
            price = finite_float(row.get("maker_price"))
        if pnl is None:
            continue
        selected_edge = finite_float(row.get("selected_policy_edge"))
        threshold = finite_float(row.get("entry_edge_threshold"))
        margin = finite_float(row.get("edge_margin"))
        if margin is None and selected_edge is not None and threshold is not None:
            margin = selected_edge - threshold
        rows.append(
            {
                "session": session,
                "market_slug": row.get("market_slug", ""),
                "entry_ts": entry_ts,
                "exit_ts": market_end_ts(str(row.get("market_slug", ""))) or entry_ts,
                "side": str(row.get("side") or "").upper(),
                "normalised_5_share_pnl": float(pnl),
                "win": int(float(pnl) > 0.0),
                "price": price if price is not None else "",
                "selected_policy_edge": selected_edge if selected_edge is not None else "",
                "entry_edge_threshold": threshold if threshold is not None else "",
                "edge_margin": margin if margin is not None else "",
                "btc_mom_20s_bps": momentum if momentum is not None else "",
            }
        )
    rows.sort(key=lambda item: (float(item["entry_ts"]), str(item["market_slug"])))
    return rows


def simulate(
    rows: list[dict[str, Any]],
    *,
    block_minutes: int,
    recovery_minutes: int,
    edge_bump: float,
    trigger_count: int,
    trigger_floor: float,
    mom_threshold: float,
) -> dict[str, Any]:
    baseline_pnl = sum(float(row["normalised_5_share_pnl"]) for row in rows)
    closed_pnls: list[float] = []
    active_positions: list[tuple[float, float, bool]] = []
    recovery_until = 0.0
    hard_block_until = 0.0
    triggers = 0
    taken = 0
    recovery_taken = 0
    skipped_side = 0
    skipped_hard = 0
    skipped_edge = 0
    skipped_side_pnl = 0.0
    skipped_hard_pnl = 0.0
    skipped_edge_pnl = 0.0
    taken_pnl = 0.0

    def close_until(now_ts: float) -> None:
        nonlocal recovery_until, hard_block_until, triggers
        active_positions.sort(key=lambda item: item[0])
        while active_positions and active_positions[0][0] <= now_ts:
            _, pnl, _entry_recovery = active_positions.pop(0)
            closed_pnls.append(float(pnl))
            if now_ts >= recovery_until:
                recovery_until = 0.0
                hard_block_until = 0.0
            if recovery_until > now_ts:
                continue
            if len(closed_pnls) >= trigger_count and sum(closed_pnls[-trigger_count:]) <= trigger_floor:
                triggers += 1
                recovery_until = now_ts + recovery_minutes * 60.0
                hard_block_until = now_ts + block_minutes * 60.0

    for row in rows:
        entry_ts = float(row["entry_ts"])
        close_until(entry_ts)
        if entry_ts >= recovery_until:
            recovery_until = 0.0
            hard_block_until = 0.0
        pnl = float(row["normalised_5_share_pnl"])
        momentum = finite_float(row.get("btc_mom_20s_bps"))
        if side_against_momentum(str(row.get("side", "")), momentum, mom_threshold):
            skipped_side += 1
            skipped_side_pnl += pnl
            continue
        recovery_active = recovery_until > entry_ts
        if recovery_active and entry_ts < hard_block_until:
            skipped_hard += 1
            skipped_hard_pnl += pnl
            continue
        if recovery_active and edge_bump > 0.0:
            margin = finite_float(row.get("edge_margin"))
            if margin is None or margin + 1e-12 < edge_bump:
                skipped_edge += 1
                skipped_edge_pnl += pnl
                continue

        taken += 1
        recovery_taken += int(recovery_active)
        taken_pnl += pnl
        active_positions.append((float(row["exit_ts"]), pnl, bool(recovery_active)))

    close_until(float("inf"))
    skipped_total = skipped_side + skipped_hard + skipped_edge
    skipped_pnl = skipped_side_pnl + skipped_hard_pnl + skipped_edge_pnl
    return {
        "block_minutes": block_minutes,
        "recovery_minutes": recovery_minutes,
        "edge_bump": round(edge_bump, 4),
        "baseline_pnl": round(baseline_pnl, 6),
        "guarded_pnl": round(taken_pnl, 6),
        "delta_vs_baseline": round(taken_pnl - baseline_pnl, 6),
        "rows": len(rows),
        "taken": taken,
        "recovery_taken": recovery_taken,
        "skipped_total": skipped_total,
        "skipped_side_mom": skipped_side,
        "skipped_hard_block": skipped_hard,
        "skipped_edge_bump": skipped_edge,
        "skipped_pnl": round(skipped_pnl, 6),
        "skipped_side_mom_pnl": round(skipped_side_pnl, 6),
        "skipped_hard_block_pnl": round(skipped_hard_pnl, 6),
        "skipped_edge_bump_pnl": round(skipped_edge_pnl, 6),
        "triggers": triggers,
    }


def main() -> int:
    args = parse_args()
    matches = read_csv(args.matches_csv)
    btc = load_btc_series(args.logs_dir)
    block_minutes = parse_int_list(args.block_minutes)
    edge_bumps = parse_float_list(args.edge_bumps)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    all_rows: list[dict[str, Any]] = []
    top_rows: list[dict[str, Any]] = []
    for mode in ("actual_same_side", "candidate_surface"):
        replay_rows = build_replay_rows(matches, btc, mode=mode, mom_window=args.mom_window_seconds)
        write_csv(args.out_dir / f"{mode}_replay_rows.csv", replay_rows)
        mode_rows: list[dict[str, Any]] = []
        for block in block_minutes:
            for edge_bump in edge_bumps:
                result = simulate(
                    replay_rows,
                    block_minutes=block,
                    recovery_minutes=args.recovery_minutes,
                    edge_bump=edge_bump,
                    trigger_count=args.trigger_count,
                    trigger_floor=args.trigger_floor,
                    mom_threshold=args.mom_threshold_bps,
                )
                result["mode"] = mode
                mode_rows.append(result)
                all_rows.append(result)
        mode_rows.sort(key=lambda item: (float(item["guarded_pnl"]), float(item["delta_vs_baseline"])), reverse=True)
        top_rows.extend(mode_rows[:20])
        write_csv(args.out_dir / f"{mode}_grid.csv", mode_rows)

    all_rows.sort(key=lambda item: (str(item["mode"]), -float(item["guarded_pnl"])))
    top_rows.sort(key=lambda item: (str(item["mode"]), -float(item["guarded_pnl"])))
    write_csv(args.out_dir / "combined_grid.csv", all_rows)
    write_csv(args.out_dir / "top_rows.csv", top_rows)

    by_mode = {}
    for row in top_rows:
        by_mode.setdefault(row["mode"], row)
    overview = [
        "Recovery Edge-Bump Audit",
        f"matches_csv={args.matches_csv}",
        f"logs_dir={args.logs_dir}",
        f"recovery_minutes={args.recovery_minutes}",
        f"block_minutes={block_minutes}",
        f"edge_bumps={edge_bumps}",
        f"trigger=last {args.trigger_count} closed trades <= {args.trigger_floor:+.2f}",
        f"global_side_mom=20s against selected side by >= {args.mom_threshold_bps:g} bps",
        "",
        f"best_actual_same_side={by_mode.get('actual_same_side', {})}",
        f"best_candidate_surface={by_mode.get('candidate_surface', {})}",
        "",
        "Files:",
        "  actual_same_side_grid.csv",
        "  candidate_surface_grid.csv",
        "  combined_grid.csv",
        "  top_rows.csv",
        "",
        "Caution:",
        "  actual_same_side is grounded in real fills, but it only tests trades that actually filled.",
        "  candidate_surface tests the edge-bump threshold on all first candidates, but assumes those candidates fill.",
    ]
    write_text(args.out_dir / "README_summary.txt", overview)
    print("\n".join(overview))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

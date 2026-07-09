#!/usr/bin/env python3
from __future__ import annotations

import argparse
import bisect
import csv
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


NORMALISED_SHARES = 5.0
UTC = timezone.utc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit live stats_bot logs for policy-cube predictive power, "
            "actual fill performance, adverse selection, and recovery blockers."
        )
    )
    parser.add_argument("--logs-dir", type=Path, default=Path("logs"), help="Directory containing session log folders.")
    parser.add_argument("--since", default="20260615", help="Earliest session date to include, formatted YYYYMMDD.")
    parser.add_argument("--out-dir", type=Path, default=Path("analysis/live_policy_audit"), help="Output directory.")
    parser.add_argument("--mode", default="live", choices=["live", "paper", "all"], help="Session mode filter.")
    parser.add_argument("--min-entry", type=float, default=0.15, help="Minimum candidate ask price.")
    parser.add_argument("--max-entry", type=float, default=0.60, help="Maximum candidate ask price.")
    parser.add_argument(
        "--first-candidate",
        choices=["market", "market_side"],
        default="market",
        help="Deduplicate cadence candidates by market or by market+side.",
    )
    parser.add_argument(
        "--include-nonlive-paper-fills",
        action="store_true",
        help="Include paper trade CSVs when --mode=all. Ignored for --mode=live.",
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


def truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def split_blockers(value: str | None) -> list[str]:
    blockers: list[str] = []
    for part in str(value or "").replace(",", "|").split("|"):
        part = part.strip()
        if part:
            blockers.append(part)
    return blockers


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
        path_mode = session_mode(path)
        if mode != "all" and path_mode != mode:
            continue
        if not (path / "stats_cadence_1s.csv").exists():
            continue
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


def edge_bucket(edge: float | None) -> str:
    if edge is None:
        return "unknown"
    if edge < 0.05:
        return "<0.05"
    if edge < 0.10:
        return "0.05-0.099"
    if edge < 0.15:
        return "0.10-0.149"
    if edge < 0.20:
        return "0.15-0.199"
    return ">=0.20"


def margin_bucket(edge: float | None, threshold: float | None) -> str:
    if edge is None or threshold is None:
        return "unknown"
    margin = edge - threshold
    if margin < 0:
        return "<0"
    if margin < 0.02:
        return "0-0.019"
    if margin < 0.05:
        return "0.02-0.049"
    if margin < 0.10:
        return "0.05-0.099"
    return ">=0.10"


def abs_bps_bucket(value: float | None) -> str:
    if value is None:
        return "unknown"
    abs_value = abs(value)
    if abs_value < 1:
        return "<1bps"
    if abs_value < 2.5:
        return "1-2.49bps"
    if abs_value < 5:
        return "2.5-4.99bps"
    return ">=5bps"


def side_price(row: dict[str, str], side: str, suffix: str) -> float | None:
    prefix = "up_" if side == "UP" else "down_"
    return finite_float(row.get(prefix + suffix))


def slug_start(slug: str | None) -> datetime | None:
    match = re.search(r"-(\d+)$", str(slug or ""))
    if not match:
        return None
    return datetime.fromtimestamp(int(match.group(1)), UTC)


@dataclass
class PriceTape:
    timestamps: list[float]
    prices: list[float]

    def nearest_price(self, timestamp: datetime, max_gap_seconds: float = 75.0) -> float | None:
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


def infer_outcome(slug: str, actual_outcomes: dict[str, str], tape: PriceTape) -> str:
    if slug in actual_outcomes:
        return actual_outcomes[slug]
    start = slug_start(slug)
    if start is None:
        return ""
    end = start + timedelta(seconds=300)
    start_price = tape.nearest_price(start)
    end_price = tape.nearest_price(end)
    if start_price is None or end_price is None:
        return ""
    if end_price > start_price:
        return "UP"
    if end_price < start_price:
        return "DOWN"
    return "PUSH"


def candidate_is_structural(
    row: dict[str, str],
    *,
    min_entry: float,
    max_entry: float,
    ignore_recovery_blockers: bool,
) -> tuple[bool, str, float | None, float | None, float | None]:
    side = str(row.get("selected_side") or "").strip().upper()
    if side not in {"UP", "DOWN"}:
        return False, "NO_SELECTED_SIDE", None, None, None
    if not truthy(row.get("policy_matched")):
        return False, "POLICY_NOT_MATCHED", None, None, None
    if not truthy(row.get("entry_session_enabled")):
        return False, "ENTRY_SESSION_DISABLED", None, None, None

    edge = finite_float(row.get("selected_policy_edge"))
    threshold = finite_float(row.get("entry_edge_threshold"))
    if edge is None or threshold is None:
        return False, "EDGE_MISSING", None, None, None
    if edge < threshold:
        return False, "EDGE_BELOW_THRESHOLD", None, edge, threshold

    tte = finite_float(row.get("tte_seconds"))
    elapsed = finite_float(row.get("elapsed_seconds_since_market_start"))
    if tte is None or tte < 60 or tte > 255:
        return False, "TTE_OUTSIDE_ENTRY_RANGE", None, edge, threshold
    if elapsed is None or elapsed < 45:
        return False, "ELAPSED_BEFORE_ENTRY_RANGE", None, edge, threshold

    blockers = split_blockers(row.get("entry_blockers"))
    non_recovery_blockers = [item for item in blockers if not item.startswith("RECOVERY")]
    if ignore_recovery_blockers:
        blockers_to_check = non_recovery_blockers
    else:
        blockers_to_check = blockers
    if row.get("bps_yes_guard_blocker") or any(item.startswith("BPS_YES") for item in blockers_to_check):
        return False, "BPS_YES_GUARD", None, edge, threshold
    hard_blockers = {
        "POLICY_KEY_NOT_FOUND",
        "POLICY_KEY_INCOMPLETE",
        "POLICY_NO_SIDE_EDGE",
        "ENTRY_PRICE_OUTSIDE_RANGE",
        "ENTRY_SESSION_DISABLED",
        "UP_QUOTE_INVALID",
        "DOWN_QUOTE_INVALID",
        "VOLATILITY_MISSING",
        "NO_SNAPSHOT",
    }
    for blocker in blockers_to_check:
        if blocker in hard_blockers:
            return False, blocker, None, edge, threshold

    ask = side_price(row, side, "ask")
    bid = side_price(row, side, "bid")
    spread = side_price(row, side, "spread")
    if ask is None or bid is None or spread is None:
        return False, "QUOTE_INVALID", None, edge, threshold
    if ask < min_entry or ask > max_entry:
        return False, "ENTRY_PRICE_OUTSIDE_RANGE", None, edge, threshold
    if spread < 0.01:
        return False, "SPREAD_TOO_TIGHT", None, edge, threshold

    maker_price = round(ask - 0.01, 4)
    if maker_price < bid - 1e-12:
        return False, "MAKER_PRICE_CROSSES_BID", None, edge, threshold
    return True, "", maker_price, edge, threshold


def enrich_trade(session: Path, row: dict[str, str]) -> dict[str, Any]:
    policy = parse_policy_key(row.get("entry_policy_key"))
    shares = finite_float(row.get("shares_exited"), 0.0) or 0.0
    pnl = finite_float(row.get("pnl"), 0.0) or 0.0
    price = finite_float(row.get("entry_price"), 0.0) or 0.0
    normalised_pnl = pnl * (NORMALISED_SHARES / shares) if shares > 0 else 0.0
    entry_bps = finite_float(row.get("entry_bps"))
    out = {
        "session": session.name,
        "entry_time_utc": row.get("entry_time_utc", ""),
        "exit_time_utc": row.get("exit_time_utc", ""),
        "market_slug": row.get("market_slug", ""),
        "side": row.get("side", ""),
        "shares": shares,
        "entry_price": price,
        "entry_cost": finite_float(row.get("entry_cost"), 0.0) or 0.0,
        "pnl": pnl,
        "normalised_5_share_pnl": normalised_pnl,
        "win": int(pnl > 1e-12),
        "entry_bps": entry_bps if entry_bps is not None else "",
        "abs_bps_bucket": abs_bps_bucket(entry_bps),
        "price_band": price_band(price),
        "day_type": row.get("entry_day_type", ""),
        "session_bucket": row.get("entry_session_bucket", ""),
        "trade_style": row.get("entry_trade_style", ""),
        "policy_key": row.get("entry_policy_key", ""),
        "tte_bucket": policy["tte_bucket"],
        "signed_bps_bucket": policy["signed_bps_bucket"],
        "volatility_bucket": policy["volatility_bucket"],
        "up_ask_bucket": policy["up_ask_bucket"],
        "down_ask_bucket": policy["down_ask_bucket"],
        "settled_outcome": row.get("settled_outcome", ""),
        "settled_outcome_source": row.get("settled_outcome_source", ""),
    }
    return out


def read_session_trades(session: Path) -> tuple[list[dict[str, Any]], dict[str, str]]:
    path = session / "stats_trades.csv"
    if not path.exists():
        return [], {}
    rows: list[dict[str, Any]] = []
    outcomes: dict[str, str] = {}
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            enriched = enrich_trade(session, row)
            rows.append(enriched)
            slug = str(row.get("market_slug") or "")
            outcome = str(row.get("settled_outcome") or "")
            if slug and outcome:
                outcomes[slug] = outcome
    return rows, outcomes


def read_session_candidates(
    session: Path,
    *,
    actual_outcomes: dict[str, str],
    min_entry: float,
    max_entry: float,
    first_candidate: str,
) -> tuple[list[dict[str, Any]], Counter[str], Counter[str]]:
    path = session / "stats_cadence_1s.csv"
    price_points: list[tuple[float, float]] = []
    raw_candidate_rows: list[tuple[datetime, dict[str, str], float, float, float]] = []
    blocker_counts: Counter[str] = Counter()
    structural_rejects: Counter[str] = Counter()

    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            timestamp = parse_dt(row.get("timestamp_utc"))
            price = finite_float(row.get("cb_close_1s"))
            if timestamp is not None and price is not None:
                price_points.append((timestamp.timestamp(), price))

            for blocker in split_blockers(row.get("entry_blockers")):
                blocker_counts[blocker] += 1

            ok, reason, maker_price, edge, threshold = candidate_is_structural(
                row,
                min_entry=min_entry,
                max_entry=max_entry,
                ignore_recovery_blockers=True,
            )
            if not ok:
                structural_rejects[reason] += 1
                continue
            if timestamp is None or maker_price is None or edge is None or threshold is None:
                structural_rejects["TIMESTAMP_OR_PRICE_MISSING"] += 1
                continue
            raw_candidate_rows.append((timestamp, row, maker_price, edge, threshold))

    price_points.sort()
    tape = PriceTape([item[0] for item in price_points], [item[1] for item in price_points])

    first_rows: dict[tuple[str, ...], tuple[datetime, dict[str, str], float, float, float]] = {}
    for item in sorted(raw_candidate_rows, key=lambda value: value[0]):
        _, row, _, _, _ = item
        slug = str(row.get("market_slug") or "")
        side = str(row.get("selected_side") or "").strip().upper()
        key = (slug, side) if first_candidate == "market_side" else (slug,)
        if slug and key not in first_rows:
            first_rows[key] = item

    candidates: list[dict[str, Any]] = []
    for timestamp, row, maker_price, edge, threshold in first_rows.values():
        side = str(row.get("selected_side") or "").strip().upper()
        slug = str(row.get("market_slug") or "")
        policy_key = str(row.get("policy_key") or "")
        policy = parse_policy_key(policy_key)
        outcome = infer_outcome(slug, actual_outcomes, tape)
        win = outcome == side if outcome in {"UP", "DOWN"} else False
        pnl5 = (NORMALISED_SHARES if win else 0.0) - NORMALISED_SHARES * maker_price if outcome in {"UP", "DOWN"} else ""
        blockers = split_blockers(row.get("entry_blockers"))
        recovery_blockers = [item for item in blockers if item.startswith("RECOVERY")]
        candidates.append(
            {
                "session": session.name,
                "timestamp_utc": timestamp.isoformat(),
                "market_slug": slug,
                "side": side,
                "maker_price": maker_price,
                "ask": round(maker_price + 0.01, 4),
                "price_band": price_band(maker_price),
                "day_type": row.get("day_type", ""),
                "session_bucket": row.get("session_bucket", ""),
                "trade_style": row.get("selected_trade_style", ""),
                "tte_bucket": policy["tte_bucket"],
                "signed_bps_bucket": policy["signed_bps_bucket"],
                "volatility_bucket": row.get("volatility_bucket", policy["volatility_bucket"]),
                "up_ask_bucket": policy["up_ask_bucket"],
                "down_ask_bucket": policy["down_ask_bucket"],
                "policy_key": policy_key,
                "selected_policy_edge": edge,
                "entry_edge_threshold": threshold,
                "edge_margin": edge - threshold,
                "edge_bucket": edge_bucket(edge),
                "edge_margin_bucket": margin_bucket(edge, threshold),
                "entry_blockers": "|".join(blockers),
                "recovery_blockers": "|".join(recovery_blockers),
                "recovery_blocked": int(bool(recovery_blockers)),
                "outcome": outcome,
                "win": int(win),
                "normalised_5_share_pnl": pnl5,
            }
        )
    return candidates, blocker_counts, structural_rejects


class Agg:
    def __init__(self) -> None:
        self.n = 0
        self.wins = 0
        self.pnl = 0.0
        self.normalised_pnl = 0.0
        self.cost = 0.0
        self.price_sum = 0.0
        self.edge_sum = 0.0
        self.edge_n = 0

    def add_trade(self, row: dict[str, Any]) -> None:
        self.n += 1
        self.wins += int(row.get("win") or 0)
        self.pnl += float(row.get("pnl") or 0.0)
        self.normalised_pnl += float(row.get("normalised_5_share_pnl") or 0.0)
        self.cost += float(row.get("entry_cost") or 0.0)
        self.price_sum += float(row.get("entry_price") or 0.0)

    def add_candidate(self, row: dict[str, Any]) -> None:
        pnl = row.get("normalised_5_share_pnl")
        if pnl == "":
            return
        self.n += 1
        self.wins += int(row.get("win") or 0)
        self.normalised_pnl += float(pnl)
        self.pnl += float(pnl)
        self.cost += NORMALISED_SHARES * float(row.get("maker_price") or 0.0)
        self.price_sum += float(row.get("maker_price") or 0.0)
        edge = finite_float(row.get("selected_policy_edge"))
        if edge is not None:
            self.edge_sum += edge
            self.edge_n += 1

    def as_row(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "wins": self.wins,
            "losses": self.n - self.wins,
            "wr_pct": round(self.wins / self.n * 100.0, 3) if self.n else 0.0,
            "pnl": round(self.pnl, 6),
            "normalised_5_share_pnl": round(self.normalised_pnl, 6),
            "avg_price": round(self.price_sum / self.n, 6) if self.n else "",
            "avg_edge": round(self.edge_sum / self.edge_n, 6) if self.edge_n else "",
            "cost": round(self.cost, 6),
        }


def grouped_summary(
    rows: Iterable[dict[str, Any]],
    keys: list[str],
    *,
    kind: str,
    min_n: int = 1,
) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], Agg] = defaultdict(Agg)
    for row in rows:
        group_key = tuple(row.get(key, "") for key in keys)
        if kind == "trade":
            groups[group_key].add_trade(row)
        elif kind == "candidate":
            groups[group_key].add_candidate(row)
        else:
            raise ValueError(f"Unknown summary kind: {kind}")

    output: list[dict[str, Any]] = []
    for group_key, agg in groups.items():
        if agg.n < min_n:
            continue
        row = {key: value for key, value in zip(keys, group_key)}
        row.update(agg.as_row())
        output.append(row)
    output.sort(key=lambda item: (float(item.get("normalised_5_share_pnl") or 0.0), -int(item.get("n") or 0)))
    return output


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        keys: list[str] = []
        seen: set[str] = set()
        for row in rows:
            for key in row:
                if key not in seen:
                    seen.add(key)
                    keys.append(key)
        fieldnames = keys
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_counter(path: Path, counter: Counter[str]) -> None:
    rows = [{"key": key, "count": count} for key, count in counter.most_common()]
    write_csv(path, rows, ["key", "count"])


def write_text(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def fill_adverse_summary(trades: list[dict[str, Any]], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = ["trade_style", "volatility_bucket", "price_band"]
    trade_groups = {tuple(row.get(key, "") for key in keys): row for row in grouped_summary(trades, keys, kind="trade")}
    candidate_groups = {tuple(row.get(key, "") for key in keys): row for row in grouped_summary(candidates, keys, kind="candidate")}
    all_keys = sorted(set(trade_groups) | set(candidate_groups))
    rows: list[dict[str, Any]] = []
    for group_key in all_keys:
        trade = trade_groups.get(group_key, {})
        candidate = candidate_groups.get(group_key, {})
        row = {key: value for key, value in zip(keys, group_key)}
        row.update(
            {
                "filled_n": trade.get("n", 0),
                "filled_wr_pct": trade.get("wr_pct", 0),
                "filled_norm_pnl": trade.get("normalised_5_share_pnl", 0),
                "candidate_n": candidate.get("n", 0),
                "candidate_wr_pct": candidate.get("wr_pct", 0),
                "candidate_norm_pnl": candidate.get("normalised_5_share_pnl", 0),
            }
        )
        row["fill_minus_candidate_norm_pnl"] = round(
            float(row["filled_norm_pnl"] or 0.0) - float(row["candidate_norm_pnl"] or 0.0),
            6,
        )
        rows.append(row)
    rows.sort(key=lambda item: float(item["fill_minus_candidate_norm_pnl"]))
    return rows


def main() -> int:
    args = parse_args()
    sessions = discover_sessions(args.logs_dir, args.since, args.mode)
    if not sessions:
        raise SystemExit(f"No sessions found in {args.logs_dir} since {args.since} for mode={args.mode}.")

    all_trades: list[dict[str, Any]] = []
    all_candidates: list[dict[str, Any]] = []
    blocker_counts: Counter[str] = Counter()
    structural_rejects: Counter[str] = Counter()
    session_rows: list[dict[str, Any]] = []

    for session in sessions:
        trades, outcomes = read_session_trades(session)
        if args.mode == "all" and session_mode(session) == "paper" and not args.include_nonlive_paper_fills:
            trades = []
            outcomes = {}
        candidates, blockers, rejects = read_session_candidates(
            session,
            actual_outcomes=outcomes,
            min_entry=args.min_entry,
            max_entry=args.max_entry,
            first_candidate=args.first_candidate,
        )
        all_trades.extend(trades)
        all_candidates.extend(candidates)
        blocker_counts.update(blockers)
        structural_rejects.update(rejects)
        session_rows.append(
            {
                "session": session.name,
                "mode": session_mode(session),
                "trades": len(trades),
                "trade_pnl": round(sum(float(row.get("pnl") or 0.0) for row in trades), 6),
                "trade_norm_pnl": round(sum(float(row.get("normalised_5_share_pnl") or 0.0) for row in trades), 6),
                "candidates": len(candidates),
                "candidate_norm_pnl": round(
                    sum(float(row.get("normalised_5_share_pnl") or 0.0) for row in candidates if row.get("normalised_5_share_pnl") != ""),
                    6,
                ),
            }
        )

    out = args.out_dir
    out.mkdir(parents=True, exist_ok=True)

    write_csv(out / "executed_trades_enriched.csv", all_trades)
    write_csv(out / "cadence_candidates_first.csv", all_candidates)
    write_csv(out / "session_summary.csv", session_rows)
    write_counter(out / "cadence_blocker_counts.csv", blocker_counts)
    write_counter(out / "candidate_structural_reject_counts.csv", structural_rejects)

    trade_specs = {
        "trade_summary_by_style_price.csv": ["trade_style", "price_band"],
        "trade_summary_by_session.csv": ["day_type", "session_bucket"],
        "trade_summary_by_style_vol_price.csv": ["trade_style", "volatility_bucket", "price_band"],
        "trade_summary_by_abs_bps_style_price.csv": ["abs_bps_bucket", "trade_style", "price_band"],
        "trade_summary_by_policy_key.csv": ["policy_key"],
    }
    for filename, keys in trade_specs.items():
        min_n = 5 if filename.endswith("policy_key.csv") else 1
        write_csv(out / filename, grouped_summary(all_trades, keys, kind="trade", min_n=min_n))

    candidate_specs = {
        "candidate_summary_by_edge_bucket.csv": ["edge_bucket"],
        "candidate_summary_by_edge_margin_bucket.csv": ["edge_margin_bucket"],
        "candidate_summary_by_style_price.csv": ["trade_style", "price_band"],
        "candidate_summary_by_style_vol_price.csv": ["trade_style", "volatility_bucket", "price_band"],
        "candidate_summary_by_session.csv": ["day_type", "session_bucket"],
        "candidate_summary_by_recovery_blocker.csv": ["recovery_blocked", "recovery_blockers"],
        "candidate_summary_by_policy_key.csv": ["policy_key"],
    }
    for filename, keys in candidate_specs.items():
        min_n = 5 if filename.endswith("policy_key.csv") else 1
        write_csv(out / filename, grouped_summary(all_candidates, keys, kind="candidate", min_n=min_n))

    recovery_blocked = [row for row in all_candidates if int(row.get("recovery_blocked") or 0)]
    write_csv(
        out / "recovery_blocked_candidate_summary.csv",
        grouped_summary(recovery_blocked, ["recovery_blockers", "trade_style", "volatility_bucket", "price_band"], kind="candidate"),
    )
    write_csv(out / "fill_adverse_summary.csv", fill_adverse_summary(all_trades, all_candidates))

    trade_total = Agg()
    for row in all_trades:
        trade_total.add_trade(row)
    candidate_total = Agg()
    for row in all_candidates:
        candidate_total.add_candidate(row)
    recovery_total = Agg()
    for row in recovery_blocked:
        recovery_total.add_candidate(row)

    overview = [
        "Live Policy Audit",
        f"sessions={len(sessions)}",
        f"logs_dir={args.logs_dir}",
        f"since={args.since}",
        f"mode={args.mode}",
        f"candidate_price_range={args.min_entry:.2f}-{args.max_entry:.2f}",
        "",
        f"executed_trades={trade_total.as_row()}",
        f"cadence_first_candidates={candidate_total.as_row()}",
        f"recovery_blocked_candidates={recovery_total.as_row()}",
        "",
        "Key files:",
        "  executed_trades_enriched.csv",
        "  cadence_candidates_first.csv",
        "  trade_summary_by_style_vol_price.csv",
        "  candidate_summary_by_edge_bucket.csv",
        "  fill_adverse_summary.csv",
        "  recovery_blocked_candidate_summary.csv",
    ]
    write_text(out / "README_summary.txt", overview)

    print("\n".join(overview))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Stream and reduce the 2026-09-29 frozen-strategy live session.

This is an offline forensic utility. It never instantiates the bot or an API client.
"""

from __future__ import annotations

import argparse
import ast
import csv
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


JOURNAL_COLUMNS = (
    "timestamp_utc", "event_type", "market_slug", "window_ts", "tte_seconds",
    "elapsed_seconds_since_market_start", "cb_close_1s", "cb_spot_signed_bps_raw",
    "raw_move_dz", "up_raw_move_dz", "down_raw_move_dz", "local_volatility_1h_bps",
    "up_bid", "up_ask", "down_bid", "down_ask", "selected_side", "selected_bid",
    "selected_ask", "selected_spread", "selected_price_bucket", "TWAP_DZ",
    "up_TWAP_DZ", "down_TWAP_DZ", "p_base_raw", "p_base_iso", "BPS_15",
    "BPS_60", "BPS_ACCEL_5_15", "BPS_15__available", "BPS_60__available",
    "BPS_ACCEL_5_15__available", "bps_branch", "p_final", "model_edge",
    "model_edge_at_limit", "C_required", "policy_matched", "entry_viable",
    "entry_blockers", "execution_state", "submission_inflight", "window_attempts",
    "window_fills", "detector_status", "detector_state", "detector_value",
    "detector_q20", "detector_q30", "detector_unique_markets",
    "detector_recovery_hits", "detector_recovery_required", "detector_armed",
    "detector_last_transition", "p15_pause_active", "p15_pause_until_utc",
    "p15_pause_remaining_seconds", "p15_counterfactual_blocked_opportunities",
    "p15_counterfactual_opportunity", "p15_counterfactual_intended_ladder_shares",
    "p15_counterfactual_model_state", "order_id", "submission_id", "side",
    "fill_price", "filled_shares", "entry_cost", "limit_price", "trade_value",
    "edge_at_ask", "edge_at_limit", "required_edge", "wallet_balance_before",
)

JOURNAL_STRING_COLUMNS = {
    "timestamp_utc", "event_type", "market_slug", "selected_side",
    "selected_price_bucket", "entry_blockers", "execution_state",
    "detector_status", "detector_state", "detector_last_transition",
    "p15_pause_until_utc", "p15_counterfactual_model_state", "order_id",
    "submission_id", "side", "bps_branch",
}
JOURNAL_BOOL_COLUMNS = {
    "BPS_15__available", "BPS_60__available", "BPS_ACCEL_5_15__available",
    "detector_armed", "p15_pause_active", "p15_counterfactual_opportunity",
}
JOURNAL_SCHEMA = pa.schema([
    pa.field(name, pa.string() if name in JOURNAL_STRING_COLUMNS else pa.bool_() if name in JOURNAL_BOOL_COLUMNS else pa.float64())
    for name in JOURNAL_COLUMNS
])


def scalar(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, separators=(",", ":"), default=str)
    return value


def batches(items: Iterable[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def journal_rows(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                source = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Invalid JSONL at line {line_number}: {exc}") from exc
            yield {name: scalar(source.get(name)) for name in JOURNAL_COLUMNS}


def compact_journal(session: Path, output: Path) -> None:
    target = output / "journal_compact.parquet"
    writer: pq.ParquetWriter | None = None
    count = 0
    for batch in batches(journal_rows(session / "decision_journal.jsonl"), 10_000):
        frame = pd.DataFrame.from_records(batch, columns=JOURNAL_COLUMNS)
        for name in JOURNAL_STRING_COLUMNS:
            frame[name] = frame[name].astype("string")
        table = pa.Table.from_pandas(frame, schema=JOURNAL_SCHEMA, preserve_index=False, safe=False)
        if writer is None:
            writer = pq.ParquetWriter(target, table.schema, compression="zstd")
        writer.write_table(table)
        count += len(batch)
        if count % 50_000 == 0:
            print(f"journal rows={count:,}", flush=True)
    if writer is not None:
        writer.close()
    print(f"wrote {target} rows={count:,}", flush=True)


def parse_order_fields(path: Path) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["event_type"] == "settlement_pending":
                continue
            try:
                fields = ast.literal_eval(row["fields"])
            except (ValueError, SyntaxError):
                fields = {}
            record = {"timestamp_utc": row["timestamp_utc"], "event_type": row["event_type"], "message": row["message"]}
            record.update(fields if isinstance(fields, dict) else {})
            records.append(record)
    return pd.DataFrame(records)


def detector_timeline(journal: pd.DataFrame, output: Path) -> None:
    j = journal.copy()
    j["timestamp_utc"] = pd.to_datetime(j.timestamp_utc, utc=True, format="mixed")
    evals = j.dropna(subset=["detector_value"]).copy()
    evals["slot"] = evals.timestamp_utc.dt.floor("5min")
    evals = evals.sort_values("timestamp_utc").groupby("slot", as_index=False).tail(1)
    cols = [
        "slot", "detector_state", "detector_value", "detector_unique_markets",
        "detector_recovery_hits", "detector_armed", "detector_last_transition",
        "p15_pause_active", "p15_pause_until_utc", "p15_counterfactual_blocked_opportunities",
    ]
    evals[cols].to_csv(output / "p15_evaluation_timeline.csv", index=False)

    state_change = evals.detector_state.ne(evals.detector_state.shift())
    transitions = evals[state_change].copy()
    transitions[cols].to_csv(output / "p15_reconstructed_transitions.csv", index=False)

    blocked = j[j.entry_blockers.fillna("").str.contains("P15_PAUSE", regex=False)].copy()
    blocked.to_parquet(output / "p15_blocked_journal_rows.parquet", index=False)


def summaries(session: Path, output: Path) -> None:
    journal = pd.read_parquet(output / "journal_compact.parquet")
    trades = pd.read_csv(session / "stats_trades.csv")
    orders = parse_order_fields(session / "stats_orders.csv")
    latency = pd.read_csv(session / "stats_execution_latency.csv", low_memory=False)
    cadence = pd.read_csv(session / "stats_cadence_1s.csv", low_memory=False)

    detector_timeline(journal, output)
    orders.to_parquet(output / "orders_events.parquet", index=False)

    event_counts = orders.event_type.value_counts().rename_axis("event_type").reset_index(name="count")
    event_counts.to_csv(output / "order_event_counts.csv", index=False)

    branch = journal.dropna(subset=["bps_branch"]).groupby("bps_branch").agg(
        rows=("bps_branch", "size"), markets=("market_slug", "nunique")
    ).reset_index()
    branch.to_csv(output / "live_branch_mix.csv", index=False)

    filled = journal[journal.event_type.eq("entry_filled")].copy()
    filled.to_parquet(output / "live_entry_fills_from_journal.parquet", index=False)

    attempts = latency[latency.event_type.eq("submit_response")].copy()
    attempts.to_csv(output / "live_submit_responses.csv", index=False)

    wallet = cadence[["timestamp_utc", "market_slug", "balance", "wallet_balance_live", "realized_pnl"]].copy()
    for name in ("balance", "wallet_balance_live", "realized_pnl"):
        wallet[name] = pd.to_numeric(wallet[name], errors="coerce")
    changes = wallet[
        wallet.wallet_balance_live.diff().abs().gt(1e-9)
        | wallet.balance.diff().abs().gt(1e-9)
        | wallet.realized_pnl.diff().abs().gt(1e-9)
    ]
    changes.to_csv(output / "wallet_accounting_changes.csv", index=False)

    stats = {
        "journal_rows": int(len(journal)),
        "journal_event_types": dict(Counter(journal.event_type.fillna(""))),
        "trade_rows": int(len(trades)),
        "unique_trade_markets": int(trades.market_slug.nunique()),
        "duplicate_trade_markets": int(trades.market_slug.duplicated().sum()),
        "entry_cost_total": float(trades.entry_cost.sum()),
        "pnl_total": float(trades.pnl.sum()),
        "nominal_proceeds_total": float((trades.entry_cost + trades.pnl).sum()),
        "wins": int((trades.pnl > 0).sum()),
        "losses": int((trades.pnl < 0).sum()),
        "first_cash_wallet": float(wallet.wallet_balance_live.dropna().iloc[0]),
        "last_cash_wallet": float(wallet.wallet_balance_live.dropna().iloc[-1]),
        "last_internal_balance": float(wallet.balance.dropna().iloc[-1]),
        "last_realized_pnl": float(wallet.realized_pnl.dropna().iloc[-1]),
        "fast_entry_placed": int((orders.event_type == "fast_entry_placed").sum()),
        "maker_fill_reconciled": int((orders.event_type == "maker_entry_fill_reconciled").sum()),
        "entry_filled": int((orders.event_type == "entry_filled").sum()),
        "trade_closed": int((orders.event_type == "trade_closed").sum()),
        "cancelled": int((orders.event_type == "maker_entry_cancelled").sum()),
    }
    (output / "audit_summary.json").write_text(json.dumps(stats, indent=2) + "\n")
    print(json.dumps(stats, indent=2), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--skip-compact", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if not args.skip_compact:
        compact_journal(args.session, args.output)
    summaries(args.session, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

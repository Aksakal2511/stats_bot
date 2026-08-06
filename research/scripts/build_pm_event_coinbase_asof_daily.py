#!/usr/bin/env python3
"""Join PM quote-event replay rows to raw Coinbase trades at the same cadence.

For every Polymarket quote-event timestamp, this script attaches the latest raw
Coinbase trade at or before ``timestamp - feed_lag`` and recomputes the
Coinbase-derived move_dz fields.  PM prices remain the event-level Telonex
prices from ``build_pm_event_expanded_daily.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.research.build_distance_to_beat_audit_dataset import (  # noqa: E402
    add_bucket_columns,
    add_distance_columns,
    regime_lookup,
    side_observations,
    write_parquet,
)


DEFAULT_PM_EVENTS = (
    REPO_ROOT
    / "data/combined_new/session_splits/statsbot_12_bucket_2h_london/combined_asof_offset250_pm_events/intermediate/daily"
)
DEFAULT_RAW_CB = REPO_ROOT / "data/raw/coinbase_spot_btc_usd/trades"
DEFAULT_DISTANCE_ROOT = REPO_ROOT / "data/artifacts/distance_to_beat_offset250_20260214_20260725"
DEFAULT_RESOLUTION_LABELS = REPO_ROOT / "data/artifacts/polymarket_resolution_labels_20260214_20260725.parquet"
DEFAULT_OUTPUT = (
    REPO_ROOT
    / "data/combined_new/session_splits/statsbot_12_bucket_2h_london/combined_asof_offset250_pm_events_cb_tick"
)

EVENT_CLOCK_PM_ONLY = "pm_only"
EVENT_CLOCK_PM_AND_CB_PRICE_CHANGES = "pm_and_coinbase_price_changes"
EVENT_CLOCK_PM_AND_CB_BPS_2DP_CHANGES = "pm_and_coinbase_bps_2dp_changes"
EVENT_CLOCK_PM_AND_CB_TRADES = "pm_and_coinbase_trades"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pm-events-dir", default=str(DEFAULT_PM_EVENTS))
    parser.add_argument("--raw-coinbase-dir", default=str(DEFAULT_RAW_CB))
    parser.add_argument("--distance-root", default=str(DEFAULT_DISTANCE_ROOT))
    parser.add_argument("--resolution-labels", default=str(DEFAULT_RESOLUTION_LABELS))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--start-date", default="2026-02-14")
    parser.add_argument("--end-date", default="2026-07-25")
    parser.add_argument("--feed-lag-ms", type=int, default=100)
    parser.add_argument(
        "--event-clock",
        choices=(
            EVENT_CLOCK_PM_ONLY,
            EVENT_CLOCK_PM_AND_CB_PRICE_CHANGES,
            EVENT_CLOCK_PM_AND_CB_BPS_2DP_CHANGES,
            EVENT_CLOCK_PM_AND_CB_TRADES,
        ),
        default=EVENT_CLOCK_PM_AND_CB_PRICE_CHANGES,
        help=(
            "pm_only keeps existing PM quote-event rows. "
            "pm_and_coinbase_price_changes adds full-precision Coinbase price-change receive timestamps "
            "with PM quotes carried as-of. "
            "pm_and_coinbase_bps_2dp_changes adds Coinbase timestamps only when rounded signed_bps changes. "
            "pm_and_coinbase_trades adds every raw Coinbase trade timestamp."
        ),
    )
    parser.add_argument("--vol-floor-bps", type=float, default=None)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit-days", type=int, default=0)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--compression", default="zstd", choices=["zstd", "snappy", "gzip", "brotli", "none"])
    return parser.parse_args()


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def fmt(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}h:{seconds % 3600 // 60:02d}m:{seconds % 60:02d}s"


def discover_days(pm_events_dir: Path, start_date: str, end_date: str) -> list[str]:
    days: list[str] = []
    with os.scandir(pm_events_dir) as entries:
        for entry in entries:
            if not entry.is_dir() or not entry.name.startswith(("date_utc=", "date=")):
                continue
            day = entry.name.split("=", 1)[-1]
            if start_date <= day <= end_date and (Path(entry.path) / "part.parquet").exists():
                days.append(day)
    return sorted(set(days))


def daily_part(root: Path, day: str) -> Path:
    for prefix in ("date_utc", "date"):
        path = root / f"{prefix}={day}" / "part.parquet"
        if path.exists():
            return path
    raise FileNotFoundError(f"missing daily part for {day} under {root}")


def raw_coinbase_part(root: Path, day: str) -> Path | None:
    path = root / f"date={day}" / "part.parquet"
    return path if path.exists() else None


def previous_day(day: str) -> str:
    return str((pd.Timestamp(day) - pd.Timedelta(days=1)).date())


def load_raw_coinbase(raw_root: Path, day: str) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for candidate in (previous_day(day), day):
        path = raw_coinbase_part(raw_root, candidate)
        if path is None:
            continue
        part = pd.read_parquet(path, columns=["trade_id", "price", "size", "side", "timestamp_utc"])
        frames.append(part)
    if not frames:
        return pd.DataFrame(columns=["coinbase_trade_ts_utc", "coinbase_trade_id", "coinbase_trade_price", "coinbase_trade_size", "coinbase_trade_side"])
    raw = pd.concat(frames, ignore_index=True, sort=False)
    raw["timestamp_utc"] = pd.to_datetime(raw["timestamp_utc"], utc=True, errors="coerce")
    raw = raw.dropna(subset=["timestamp_utc", "price", "trade_id"])
    raw["price"] = pd.to_numeric(raw["price"], errors="coerce")
    raw["size"] = pd.to_numeric(raw["size"], errors="coerce")
    raw = raw.dropna(subset=["price"])
    raw = raw.drop_duplicates(subset=["trade_id"], keep="last").sort_values(["timestamp_utc", "trade_id"], kind="stable")
    return raw.rename(
        columns={
            "timestamp_utc": "coinbase_trade_ts_utc",
            "trade_id": "coinbase_trade_id",
            "price": "coinbase_trade_price",
            "size": "coinbase_trade_size",
            "side": "coinbase_trade_side",
        }
    ).reset_index(drop=True)


def load_config(distance_root: Path, vol_floor_bps: float | None) -> dict[str, object]:
    path = distance_root / "distance_to_beat_feature_config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    if vol_floor_bps is not None:
        config["vol_floor_bps"] = float(vol_floor_bps)
    return config


def event_tte_seconds(frame: pd.DataFrame) -> pd.Series:
    ts = pd.to_datetime(frame["timestamp_utc"], utc=True, errors="coerce")
    end = pd.to_datetime(frame.get("polymarket_market_end_utc"), utc=True, errors="coerce")
    if end.isna().all() and "cb_synthetic_window_end_utc" in frame.columns:
        end = pd.to_datetime(frame["cb_synthetic_window_end_utc"], utc=True, errors="coerce")
    out = (end - ts).dt.total_seconds()
    return out.clip(lower=0.0, upper=300.0)


def window_start_times(frame: pd.DataFrame) -> pd.Series:
    if "polymarket_market_start_utc" in frame.columns:
        start = pd.to_datetime(frame["polymarket_market_start_utc"], utc=True, errors="coerce")
    else:
        start = pd.Series(pd.NaT, index=frame.index)
    if start.isna().any() and "canonical_window_id" in frame.columns:
        parsed = pd.to_datetime(
            frame["canonical_window_id"].astype(str).str.replace("Z", "", regex=False),
            utc=True,
            errors="coerce",
        )
        start = start.where(start.notna(), parsed)
    return start


def raw_anchor_prices(frame: pd.DataFrame, raw: pd.DataFrame, feed_lag_ms: int) -> pd.Series:
    if raw.empty or frame.empty:
        return pd.Series(np.nan, index=frame.index, dtype="float64")
    starts = window_start_times(frame)
    unique = (
        pd.DataFrame({"window_start_utc": starts.dropna().drop_duplicates().sort_values(kind="stable")})
        .reset_index(drop=True)
    )
    if unique.empty:
        return pd.Series(np.nan, index=frame.index, dtype="float64")
    unique["coinbase_lookup_ts_utc"] = unique["window_start_utc"] - pd.Timedelta(milliseconds=int(feed_lag_ms))
    right = raw[["coinbase_trade_ts_utc", "coinbase_trade_price"]].copy()
    right["coinbase_trade_ts_utc"] = pd.to_datetime(right["coinbase_trade_ts_utc"], utc=True, errors="coerce")
    right = right.dropna(subset=["coinbase_trade_ts_utc", "coinbase_trade_price"]).sort_values(
        "coinbase_trade_ts_utc", kind="stable"
    )
    joined = pd.merge_asof(
        unique.sort_values("coinbase_lookup_ts_utc", kind="stable"),
        right,
        left_on="coinbase_lookup_ts_utc",
        right_on="coinbase_trade_ts_utc",
        direction="backward",
        allow_exact_matches=True,
    )
    mapping = joined.set_index("window_start_utc")["coinbase_trade_price"]
    return starts.map(mapping).astype("float64")


def align_coinbase(frame: pd.DataFrame, raw: pd.DataFrame, feed_lag_ms: int) -> pd.DataFrame:
    out = frame.copy()
    out["timestamp_utc"] = pd.to_datetime(out["timestamp_utc"], utc=True, errors="coerce")
    out["_row_id"] = np.arange(len(out), dtype=np.int64)
    out["coinbase_lookup_ts_utc"] = out["timestamp_utc"] - pd.Timedelta(milliseconds=int(feed_lag_ms))

    if raw.empty:
        for col in ("coinbase_trade_ts_utc", "coinbase_trade_id", "coinbase_trade_price", "coinbase_trade_size", "coinbase_trade_side"):
            out[col] = pd.NA
        return out.drop(columns=["_row_id"])

    left = out[["_row_id", "coinbase_lookup_ts_utc"]].sort_values("coinbase_lookup_ts_utc", kind="stable")
    right = raw.sort_values("coinbase_trade_ts_utc", kind="stable")
    merged = pd.merge_asof(
        left,
        right,
        left_on="coinbase_lookup_ts_utc",
        right_on="coinbase_trade_ts_utc",
        direction="backward",
        allow_exact_matches=True,
    ).sort_values("_row_id", kind="stable")
    for col in ("coinbase_trade_ts_utc", "coinbase_trade_id", "coinbase_trade_price", "coinbase_trade_size", "coinbase_trade_side"):
        out[col] = merged[col].to_numpy()
    out["coinbase_trade_lag_ms"] = (
        (pd.to_datetime(out["coinbase_lookup_ts_utc"], utc=True) - pd.to_datetime(out["coinbase_trade_ts_utc"], utc=True))
        .dt.total_seconds()
        .mul(1000.0)
    )
    return out.drop(columns=["_row_id"])


def coinbase_event_rows(frame: pd.DataFrame, raw: pd.DataFrame, event_clock: str, feed_lag_ms: int) -> pd.DataFrame:
    if event_clock == EVENT_CLOCK_PM_ONLY or frame.empty or raw.empty:
        return frame

    base = frame.copy()
    base["timestamp_utc"] = pd.to_datetime(base["timestamp_utc"], utc=True, errors="coerce")
    base = base.dropna(subset=["timestamp_utc"]).sort_values("timestamp_utc", kind="stable").reset_index(drop=True)
    if base.empty:
        return base

    events = raw.copy()
    events["coinbase_trade_ts_utc"] = pd.to_datetime(events["coinbase_trade_ts_utc"], utc=True, errors="coerce")
    events = events.dropna(subset=["coinbase_trade_ts_utc", "coinbase_trade_price"])
    events = events.sort_values(["coinbase_trade_ts_utc", "coinbase_trade_id"], kind="stable").reset_index(drop=True)
    if event_clock in {EVENT_CLOCK_PM_AND_CB_PRICE_CHANGES, EVENT_CLOCK_PM_AND_CB_BPS_2DP_CHANGES}:
        price = pd.to_numeric(events["coinbase_trade_price"], errors="coerce")
        events = events.loc[price.ne(price.shift())].copy()

    lag = pd.Timedelta(milliseconds=int(feed_lag_ms))
    events["coinbase_receive_ts_utc"] = events["coinbase_trade_ts_utc"] + lag

    start = base["timestamp_utc"].min()
    end = pd.to_datetime(base.get("polymarket_market_end_utc"), utc=True, errors="coerce").max()
    if pd.isna(end):
        end = base["timestamp_utc"].max()
    events = events.loc[
        events["coinbase_receive_ts_utc"].ge(start) & events["coinbase_receive_ts_utc"].lt(end)
    ].copy()
    if events.empty:
        base["event_clock_source"] = base.get("event_clock_source", "pm_quote")
        return base

    event_columns = ["coinbase_receive_ts_utc"]
    if event_clock == EVENT_CLOCK_PM_AND_CB_BPS_2DP_CHANGES:
        event_columns.append("coinbase_trade_price")
    left = (
        events[event_columns]
        .drop_duplicates()
        .rename(columns={"coinbase_receive_ts_utc": "timestamp_utc"})
        .sort_values("timestamp_utc", kind="stable")
    )
    carried = pd.merge_asof(
        left,
        base,
        on="timestamp_utc",
        direction="backward",
        allow_exact_matches=True,
    )
    carried = carried.loc[carried["canonical_window_id"].notna()].copy()
    market_start = pd.to_datetime(carried.get("polymarket_market_start_utc"), utc=True, errors="coerce")
    market_end = pd.to_datetime(carried.get("polymarket_market_end_utc"), utc=True, errors="coerce")
    carried = carried.loc[
        market_start.notna()
        & market_end.notna()
        & carried["timestamp_utc"].ge(market_start)
        & carried["timestamp_utc"].lt(market_end)
    ].copy()
    if carried.empty:
        base["event_clock_source"] = base.get("event_clock_source", "pm_quote")
        return base

    if event_clock == EVENT_CLOCK_PM_AND_CB_BPS_2DP_CHANGES:
        anchor = pd.to_numeric(carried.get("cb_price_to_beat_anchor"), errors="coerce")
        fallback = pd.to_numeric(carried.get("cb_window_open_price"), errors="coerce")
        anchor = anchor.where(anchor.notna(), fallback)
        raw_bps = 10_000.0 * (
            pd.to_numeric(carried["coinbase_trade_price"], errors="coerce") - anchor
        ) / anchor.replace(0.0, np.nan)
        carried["_coinbase_bps_2dp"] = raw_bps.round(2)
        carried = carried.loc[
            carried["_coinbase_bps_2dp"].notna()
            & carried["_coinbase_bps_2dp"].ne(
                carried.groupby("canonical_window_id", sort=False)["_coinbase_bps_2dp"].shift()
            )
        ].copy()
        carried = carried.drop(columns=["_coinbase_bps_2dp"])
        if "coinbase_trade_price" in carried.columns:
            carried = carried.drop(columns=["coinbase_trade_price"])
        if carried.empty:
            base["event_clock_source"] = base.get("event_clock_source", "pm_quote")
            return base

    if "pm_event_time_utc" in carried.columns:
        carried["pm_event_time_utc"] = pd.NaT
    if "pm_event_expanded" in carried.columns:
        carried["pm_event_expanded"] = False
    carried["coinbase_event_expanded"] = True
    if event_clock == EVENT_CLOCK_PM_AND_CB_PRICE_CHANGES:
        carried["event_clock_source"] = "coinbase_price_change"
    elif event_clock == EVENT_CLOCK_PM_AND_CB_BPS_2DP_CHANGES:
        carried["event_clock_source"] = "coinbase_bps_2dp_change"
    else:
        carried["event_clock_source"] = "coinbase_trade"

    base["coinbase_event_expanded"] = False
    base["event_clock_source"] = base.get("event_clock_source", "pm_quote")
    out = pd.concat([base, carried], ignore_index=True, sort=False)
    out["_event_priority"] = np.where(out["event_clock_source"].astype(str).eq("pm_quote"), 0, 1)
    out = (
        out.sort_values(["timestamp_utc", "canonical_window_id", "_event_priority"], kind="stable")
        .drop_duplicates(subset=["timestamp_utc", "canonical_window_id"], keep="first")
        .drop(columns=["_event_priority"])
        .reset_index(drop=True)
    )
    return out


def refill_missing_anchor_columns(frame: pd.DataFrame, raw: pd.DataFrame, feed_lag_ms: int) -> pd.DataFrame:
    out = frame.copy()
    anchor_from_raw = raw_anchor_prices(out, raw, feed_lag_ms)
    for col in ("cb_price_to_beat_anchor", "cb_window_open_price"):
        if col not in out.columns:
            out[col] = np.nan
        current = pd.to_numeric(out[col], errors="coerce")
        out[col] = current.where(current.notna(), anchor_from_raw)
    return out


def rebuild_coinbase_columns(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    for col in ("cb_close_1s", "cb_signed_bps_raw", "canonical_tte_seconds", "cb_tte_seconds", "polymarket_tte_seconds"):
        if col in out.columns and f"{col}_base_1s" not in out.columns:
            out[f"{col}_base_1s"] = out[col]

    price = pd.to_numeric(out["coinbase_trade_price"], errors="coerce")
    out["cb_close_1s"] = price
    out["cb_event_price"] = price
    out["cb_event_aligned"] = price.notna()
    if "dq_cb_row_present" in out.columns:
        out["dq_cb_row_present"] = out["cb_event_aligned"]

    tte = event_tte_seconds(out)
    out["canonical_tte_seconds"] = tte
    if "cb_tte_seconds" in out.columns:
        out["cb_tte_seconds"] = tte
    if "polymarket_tte_seconds" in out.columns:
        out["polymarket_tte_seconds"] = tte

    anchor = pd.to_numeric(out.get("cb_price_to_beat_anchor"), errors="coerce")
    fallback = pd.to_numeric(out.get("cb_window_open_price"), errors="coerce")
    anchor = anchor.where(anchor.notna(), fallback)
    out["cb_signed_bps_raw"] = 10_000.0 * (price - anchor) / anchor.replace(0.0, np.nan)
    return out


def attach_polymarket_resolution_labels(frame: pd.DataFrame, labels_path: Path) -> pd.DataFrame:
    out = frame.copy()
    if out.empty:
        out["polymarket_UP_outcome"] = np.nan
        out["polymarket_resolved_flag"] = False
        return out
    if not labels_path.exists():
        raise FileNotFoundError(f"missing Polymarket resolution labels: {labels_path}")
    labels = pd.read_parquet(labels_path)
    required = {"canonical_window_id", "polymarket_up_outcome", "resolution_status"}
    missing = sorted(required - set(labels.columns))
    if missing:
        raise KeyError(f"Polymarket resolution labels missing columns: {missing}")
    labels = labels.copy()
    labels["canonical_window_id"] = labels["canonical_window_id"].astype(str)
    keep = [
        col
        for col in (
            "canonical_window_id",
            "polymarket_up_outcome",
            "resolution_status",
            "resolution_source",
            "outcome_agrees",
        )
        if col in labels.columns
    ]
    labels = labels[keep].drop_duplicates("canonical_window_id", keep="last")
    labels = labels.rename(
        columns={
            "polymarket_up_outcome": "polymarket_UP_outcome",
            "resolution_status": "polymarket_resolution_status",
            "resolution_source": "polymarket_resolution_source",
            "outcome_agrees": "polymarket_outcome_agrees_coinbase",
        }
    )

    out["canonical_window_id"] = out["canonical_window_id"].astype(str)
    out = out.drop(
        columns=[
            "polymarket_UP_outcome",
            "polymarket_resolution_status",
            "polymarket_resolution_source",
            "polymarket_outcome_agrees_coinbase",
            "polymarket_resolved_flag",
        ],
        errors="ignore",
    )
    out = out.merge(labels, on="canonical_window_id", how="left")
    out["polymarket_UP_outcome"] = pd.to_numeric(out["polymarket_UP_outcome"], errors="coerce")
    out["polymarket_resolved_flag"] = (
        out.get("polymarket_resolution_status", pd.Series("", index=out.index)).astype(str).eq("resolved")
        & out["polymarket_UP_outcome"].isin([0.0, 1.0])
    )
    return out


def add_event_update_flags(frame: pd.DataFrame) -> pd.DataFrame:
    """Mark whether each event row was caused by PM, Coinbase, or both.

    The live bot carries the latest state from both feeds into every decision:
    a Coinbase websocket wakeup keeps the latest PM book, and a PM book wakeup
    keeps the latest Coinbase price.  These flags make that union clock visible
    without changing the carried-forward values themselves.
    """
    out = frame.copy()
    if out.empty:
        return out

    out["timestamp_utc"] = pd.to_datetime(out["timestamp_utc"], utc=True, errors="coerce")
    out = out.sort_values(["canonical_window_id", "timestamp_utc"], kind="stable").reset_index(drop=True)
    groups = out.groupby("canonical_window_id", sort=False)

    cb_price = pd.to_numeric(out.get("cb_close_1s"), errors="coerce")
    prev_cb_price = groups["cb_close_1s"].shift() if "cb_close_1s" in out.columns else pd.Series(np.nan, index=out.index)
    cb_value_changed = cb_price.notna() & pd.to_numeric(prev_cb_price, errors="coerce").notna() & cb_price.ne(
        pd.to_numeric(prev_cb_price, errors="coerce")
    )
    cb_source_row = (
        out.get("coinbase_event_expanded", pd.Series(False, index=out.index)).fillna(False).astype(bool)
    )

    pm_cols = [
        col
        for col in (
            "polymarket_up_bid",
            "polymarket_up_ask",
            "polymarket_down_bid",
            "polymarket_down_ask",
            "polymarket_up_mid",
            "polymarket_down_mid",
        )
        if col in out.columns
    ]
    pm_value_changed = pd.Series(False, index=out.index)
    for col in pm_cols:
        current = pd.to_numeric(out[col], errors="coerce")
        previous = pd.to_numeric(groups[col].shift(), errors="coerce")
        pm_value_changed = pm_value_changed | (current.notna() & previous.notna() & current.ne(previous))
    pm_source_row = out.get("pm_event_expanded", pd.Series(False, index=out.index)).fillna(False).astype(bool)
    pm_source_row = pm_source_row | out.get("event_clock_source", pd.Series("", index=out.index)).astype(str).eq("pm_quote")

    out["event_has_coinbase_move"] = (cb_source_row | cb_value_changed).astype(bool)
    out["event_has_pm_move"] = (pm_source_row | pm_value_changed).astype(bool)
    out["event_has_both_moves"] = (out["event_has_coinbase_move"] & out["event_has_pm_move"]).astype(bool)
    out["event_update_source"] = np.select(
        [
            out["event_has_both_moves"],
            out["event_has_coinbase_move"],
            out["event_has_pm_move"],
        ],
        ["both", "coinbase", "polymarket"],
        default="carried_state",
    )
    return out


def attach_event_flags_to_observations(observations: pd.DataFrame, aligned: pd.DataFrame) -> pd.DataFrame:
    flag_cols = [
        col
        for col in (
            "timestamp_utc",
            "canonical_window_id",
            "event_clock_source",
            "event_update_source",
            "event_has_pm_move",
            "event_has_coinbase_move",
            "event_has_both_moves",
            "coinbase_trade_ts_utc",
            "coinbase_trade_id",
            "coinbase_trade_lag_ms",
            "cb_event_price",
            "pm_event_time_utc",
        )
        if col in aligned.columns
    ]
    if observations.empty or len(flag_cols) <= 2:
        return observations
    flags = aligned[flag_cols].drop_duplicates(subset=["timestamp_utc", "canonical_window_id"], keep="last")
    out = observations.merge(flags, on=["timestamp_utc", "canonical_window_id"], how="left")
    return out


def build_day(
    day: str,
    pm_events_dir: Path,
    raw_coinbase_dir: Path,
    resolution_labels: Path,
    output_dir: Path,
    distance_root: Path,
    config: dict[str, object],
    feed_lag_ms: int,
    event_clock: str,
    compression: str,
    overwrite: bool,
) -> dict[str, object]:
    combined_target = output_dir / "intermediate" / "daily" / f"date_utc={day}" / "part.parquet"
    observations_target = output_dir / "observations" / f"date_utc={day}" / "part.parquet"
    if combined_target.exists() and observations_target.exists() and not overwrite:
        return {
            "date_utc": day,
            "status": "REUSE",
            "combined_rows": int(pd.read_parquet(combined_target, columns=["timestamp_utc"]).shape[0]),
            "observation_rows": int(pd.read_parquet(observations_target, columns=["timestamp_utc"]).shape[0]),
            "coinbase_matched_rows": None,
        }

    frame = pd.read_parquet(daily_part(pm_events_dir, day))
    raw = load_raw_coinbase(raw_coinbase_dir, day)
    frame = refill_missing_anchor_columns(frame, raw, feed_lag_ms)
    frame = coinbase_event_rows(frame, raw, event_clock, feed_lag_ms)
    frame = refill_missing_anchor_columns(frame, raw, feed_lag_ms)
    aligned = align_coinbase(frame, raw, feed_lag_ms)
    aligned = refill_missing_anchor_columns(aligned, raw, feed_lag_ms)
    aligned = rebuild_coinbase_columns(aligned)
    aligned = attach_polymarket_resolution_labels(aligned, resolution_labels)
    aligned = add_event_update_flags(aligned)

    regimes = pd.read_parquet(distance_root / "minute_regimes.parquet")
    regimes["minute_utc"] = pd.to_datetime(regimes["minute_utc"], utc=True, errors="coerce")
    lookup = regime_lookup(regimes)
    aligned = add_distance_columns(aligned, lookup, float(config.get("vol_floor_bps", 0.05)))
    aligned = add_bucket_columns(aligned, config)
    observations = side_observations(
        aligned,
        tte_min=int(config.get("observation_tte_min", 0)),
        tte_max=int(config.get("observation_tte_max", 300)),
    )
    observations = attach_event_flags_to_observations(observations, aligned)

    write_parquet(aligned, combined_target, compression)
    write_parquet(observations, observations_target, compression)
    return {
        "date_utc": day,
        "status": "WRITE",
        "combined_rows": int(len(aligned)),
        "observation_rows": int(len(observations)),
        "coinbase_matched_rows": int(aligned["cb_event_aligned"].sum()),
        "polymarket_label_rows": int(aligned["polymarket_resolved_flag"].sum()),
        "raw_coinbase_rows_loaded": int(len(raw)),
        "event_clock": event_clock,
    }


def main() -> int:
    args = parse_args()
    pm_events_dir = Path(args.pm_events_dir).expanduser().resolve()
    raw_coinbase_dir = Path(args.raw_coinbase_dir).expanduser().resolve()
    distance_root = Path(args.distance_root).expanduser().resolve()
    resolution_labels = Path(args.resolution_labels).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    days = discover_days(pm_events_dir, args.start_date, args.end_date)
    if args.limit_days:
        days = days[: int(args.limit_days)]
    if not days:
        raise FileNotFoundError(f"no PM event daily files in {pm_events_dir} for {args.start_date}..{args.end_date}")

    if output_dir.exists() and args.overwrite:
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    config = load_config(distance_root, args.vol_floor_bps)

    print(
        f"[{stamp()}] PLAN PM-event Coinbase-asof days={len(days)} start={days[0]} end={days[-1]} "
        f"feed_lag_ms={args.feed_lag_ms} event_clock={args.event_clock} output={output_dir}",
        flush=True,
    )
    started = time.perf_counter()
    rows: list[dict[str, object]] = []
    with ProcessPoolExecutor(max_workers=max(1, int(args.workers))) as pool:
        futures = {
            pool.submit(
                build_day,
                day,
                pm_events_dir,
                raw_coinbase_dir,
                resolution_labels,
                output_dir,
                distance_root,
                config,
                int(args.feed_lag_ms),
                str(args.event_clock),
                args.compression,
                bool(args.overwrite),
            ): day
            for day in days
        }
        for idx, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            rows.append(result)
            elapsed = time.perf_counter() - started
            eta = elapsed / idx * (len(futures) - idx)
            print(
                f"[{stamp()}] {result['status']} day={result['date_utc']} rows={int(result['combined_rows']):,} "
                f"obs={int(result['observation_rows']):,} cb_matched={result.get('coinbase_matched_rows')} "
                f"progress={idx}/{len(futures)} elapsed={fmt(elapsed)} eta={fmt(eta)}",
                flush=True,
            )

    summary = pd.DataFrame(rows).sort_values("date_utc", kind="stable")
    summary.to_csv(output_dir / "coinbase_event_asof_summary.csv", index=False)
    manifest = {
        "source_pm_events_dir": str(pm_events_dir),
        "raw_coinbase_dir": str(raw_coinbase_dir),
        "distance_root": str(distance_root),
        "resolution_labels": str(resolution_labels),
        "output_dir": str(output_dir),
        "start_date": args.start_date,
        "end_date": args.end_date,
        "feed_lag_ms": int(args.feed_lag_ms),
        "event_clock": str(args.event_clock),
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "settlement_label": "polymarket_UP_outcome",
        "note": "PM prices are Telonex event-level; Coinbase price/move_dz are raw-trade asof on the configured event clock; side_win uses Polymarket resolution labels.",
    }
    (output_dir / "coinbase_event_asof_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"[{stamp()}] COMPLETE output={output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
